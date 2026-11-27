"""FastAPI app: hub, chores, admin, calendar and tile routes.

The wall page polls /api/hub and drives the /api/admin/* routes from its Chores
edit mode (reachable from a phone too — same page); tiles proxy the box's other
services. A background thread syncs Google Calendar every 5 min, and a
background asyncio task watches Home Assistant's laundry sensors every 5s,
pushing changes to open walls over SSE (both disabled by DISABLE_SYNC=1 in
tests). LAN-only, no auth — the established trust model for every service
on this box.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool

from . import access_log
from . import backups as fbackups
from . import chores as chlogic
from . import db as fdb
from . import demo as fdemo
from . import go2rtc_proxy
from . import integrations as fintegrations
from . import parent_pin
from . import rewards
from . import wallet
from . import reminders as remlogic
from . import tiles
from . import todos as tdlogic
from . import version as fversion
from . import caldav_service
from . import caldav_sync
from . import chore_mirror
from . import deep_health
from . import dimming
from .calendar_sync import GoogleCalendarClient, sync_once
from .config import load_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("family_hub")
# httpx (and httpcore under it) log every request at INFO. The laundry watcher
# polls Home Assistant every 5s, so those lines were two thirds of the hub's
# log, ~19 MB a day. Their warnings and errors still come through, and every
# failed upstream fetch is also logged by our own code where it is handled.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# The same for our own access log: drop successful camera probes, tile polls
# and health checks, keep their errors and every other request (access_log.py).
access_log.install()

# When this process started: /health/full only counts a calendar sync or a
# laundry read made after it (the previous container's work proves nothing).
PROCESS_STARTED_AT = time.time()
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
cfg = load_config(CONFIG_PATH)
# The bytes this process loaded, fingerprinted right after loading them, and
# what the deploy recorded when it built the image (deep_health.py).
CONFIG_LOADED_SHA256 = deep_health.file_sha256(CONFIG_PATH)
BUILD_INFO = deep_health.read_build_info()
# Where the hub reaches go2rtc. Browsers never do: they get the player and its
# WebSocket through the hub's /go2rtc/ proxy (go2rtc_proxy.py), because
# go2rtc's own API has no access control and must not be on the LAN. In the
# compose stack that is http://go2rtc:1984 over the compose network
# (GO2RTC_FETCH_BASE; a container also cannot hairpin its OWN stack's
# published LAN port, found live 2026-08-12); config's go2rtc_base is the
# fallback for a hub run outside docker.
_fetch_cfg = dataclasses.replace(
    cfg, go2rtc_base=os.environ.get("GO2RTC_FETCH_BASE", cfg.go2rtc_base))
DB_PATH = os.environ.get("DB_PATH", "data/hub.db")
TOKEN_PATH = os.environ.get("TOKEN_PATH", "data/token.json")
# Server-side store for UI-entered iCloud CalDAV credentials (default: next to
# the DB / Google token, in the git-ignored data dir). caldav_service reads it.
os.environ.setdefault(
    "CALDAV_CREDS_PATH",
    os.path.join(os.path.dirname(DB_PATH) or ".", "caldav.json"))
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "web", "static")

# DEMO=1 turns the whole app into a self-contained sample wall (fake family,
# placeholder cameras, canned weather/climate) for a README screenshot or a
# "try it" run: no real calendars, cameras, or feeds needed. Every DEMO branch
# below is gated on this flag, so an unset DEMO changes zero behavior.
DEMO = os.environ.get("DEMO", "") == "1"

# Dashboard backup-health: the header shows a "Backup stale" badge once the last
# successful backup is older than this. 36h clears the nightly snapshot, so only
# a genuinely missed/failed backup trips it.
BACKUP_STALE_S = int(os.environ.get("BACKUP_STALE_HOURS", "36")) * 3600


def _config_fingerprint(config) -> str:
    """A stable text form of the loaded config, folded into BUILD. The wall builds
    its camera tiles and panels ONCE per page load, so without this a config
    change (a new camera, a moved panel) restarted the app with the same asset
    hash and never reached an open wall. Never raises: an unserializable config
    logs and contributes nothing, which only loses the config half of the token."""
    try:
        return json.dumps(dataclasses.asdict(config), sort_keys=True, default=str)
    except Exception as e:   # noqa: BLE001 - import-time, must not take the app down
        log.warning("build hash: config not fingerprinted (%s); a config change "
                    "will not reload open walls", e)
        return ""


def _compute_build(config_fp: str = "") -> str:
    """Short token that changes whenever any baked frontend asset changes, so the
    wall can auto-reload after a deploy. The frontend is BAKED into the image and a
    deploy rebuilds + restarts the container, so hashing the served asset files at
    startup yields a fresh value each deploy (and a stable one between deploys).
    `config_fp` (see _config_fingerprint) folds the loaded config in too: a config
    change also restarts the app, and must reload the wall the same way."""
    import glob
    import hashlib
    h = hashlib.sha256()
    hashed = 0
    # Glob the served asset types (sorted for a deterministic, cross-filesystem
    # order) rather than a hardcoded list, so a NEWLY ADDED asset is tracked
    # automatically. A literal tuple would silently miss it — the new file's
    # changes would never bump the token and never reach the kiosk.
    paths = sorted(p for ext in ("*.html", "*.css", "*.js")
                   for p in glob.glob(os.path.join(STATIC_DIR, ext)))
    for path in paths:
        name = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            # Globbed but unreadable (a race, a permission change, or a dir that
            # matched). The token can't track this file — say so LOUDLY rather
            # than swallow it.
            log.warning("build hash: asset %s unreadable (%s) — "
                        "auto-reload will miss changes to it", name, e)
            continue
        # Fold the NAME in too, so an add/remove/rename bumps the token even when
        # the surviving bytes are identical.
        h.update(name.encode())
        h.update(data)
        hashed += 1
    if not hashed:
        # No asset was readable: STATIC_DIR is wrong or the bake is broken.
        # Returning the empty-input hash here would be a stable, plausible-looking
        # token that freezes auto-reload with no signal at all. Shout instead.
        log.error("build hash: NO static assets readable under %s — the frontend "
                  "bake is broken; deploy auto-reload is disabled", STATIC_DIR)
    if config_fp:
        h.update(b"\0config\0")
        h.update(config_fp.encode())
    return h.hexdigest()[:12]


BUILD = _compute_build(_config_fingerprint(cfg))
# The human-facing release identity (distinct from BUILD, the asset-content
# hash): the SemVer from VERSION. Read once at import like BUILD — a deploy
# restarts the process and picks up the new version.
APP_VERSION = fversion.read_version()
# \Z (end of string), not $ — in non-MULTILINE mode $ also matches just before a
# trailing newline, so "#ff0000\n" would slip through and reach the client as a
# CSS color. \Z anchors the true end.
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# Per-thread SQLite connections. Request handlers are sync `def`, so FastAPI
# runs them on a thread pool; one shared connection let their transactions
# interleave (one thread's commit committing another thread's half-done
# `with conn:` block). See issue #29. Each thread gets its own connection, and
# SQLite (WAL + busy_timeout) serializes writers across them. The one-time,
# whole-DB setup (schema migrations, history backfill, DEMO seed) runs once
# under a lock so concurrent first-requests can't race the table rebuilds.
_tls = threading.local()
_init_lock = threading.Lock()
_db_initialized = False


def _now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def _today() -> dt.date:
    return _now_local().date()


def _ensure_history_backfill(conn) -> None:
    """One-time upgrade for deployments that predate the occurrence log:
    best-effort reconstruct the last 370 days of chore history from the
    CURRENT chore/person definitions so streaks survive the upgrade. (Best
    effort, not exact: a chore edited or a person deactivated between a
    historical day and the upgrade is reconstructed with today's values —
    there is no prior record to consult. It's a one-time approximation.)

    Guarded by a kv flag so it never runs twice — a later empty log day must
    mean 'rest', not 're-derive from live defs'. The write is atomic (rows +
    flag in one transaction via db.backfill_occurrence_log), so an interrupted
    run commits nothing and retries whole next boot rather than freezing a
    streak-inflating partial history. Fresh DBs just set the flag."""
    if fdb.kv_get(conn, "occlog_backfill_done"):
        return
    has_completions = conn.execute(
        "SELECT 1 FROM completions LIMIT 1").fetchone() is not None
    has_log = conn.execute(
        "SELECT 1 FROM occurrence_log LIMIT 1").fetchone() is not None
    if not (has_completions and not has_log):
        fdb.kv_set(conn, "occlog_backfill_done", True)   # fresh / already-logged
        return
    people = fdb.list_people(conn)
    chores = fdb.list_chores(conn)
    today = _today()
    # Build every day eagerly BEFORE touching the DB: a failure here (bad row)
    # raises with zero writes done; the atomic helper then commits all days and
    # the flag together, or nothing.
    day_rows = []
    for i in range(370, 0, -1):
        d = today - dt.timedelta(days=i)
        rows = chlogic.plan_rows(chores, people, d)
        if rows:
            day_rows.append((d.isoformat(), rows))
    fdb.backfill_occurrence_log(conn, day_rows, "occlog_backfill_done")
    log.info("occurrence log backfilled from legacy completions (%d days)",
             len(day_rows))


def _ensure_demo_seed(conn) -> None:
    """DEMO=1 only: seed the fake sample family into an EMPTY db on first open,
    so a fresh `DEMO=1` run comes up as a fully populated wall. Guarded on EVERY
    seeded table being empty (not just people), so it never re-seeds or touches a
    real db (issue #36) — and a plain unset-DEMO run never reaches here at all."""
    if fdemo.is_unseeded(conn):
        try:
            fdemo.seed_demo(conn, _today())
        except Exception:
            # The fdb helpers self-commit, so a seed that raises partway has
            # already written some rows (people first). Wipe them so the empty-db
            # guard fires again next open and re-seeds cleanly, instead of seeing
            # the half-written people and serving a permanently half-populated
            # demo.
            fdemo.clear_demo(conn)
            raise
        log.info("DEMO mode: seeded the sample family wall")
    # DEMO never runs a sync (_sync_enabled() is false), so nothing would ever
    # record calendar coverage and /api/calendar would report an EMPTY window —
    # hatching every empty day on the demo wall, the README screenshot and every
    # visual gate, with status.ok still true so no banner explains it. Vouch for
    # the demo's own configured window.
    #
    # Gated on the db being DEMO-SEEDED, which is NOT is_unseeded's "is this db
    # empty" — getting that distinction wrong bites both ways. Keyed on
    # emptiness, DEMO=1 against a REAL db (the README tells compose users to set
    # it on a service using the real volume) stamps coverage nothing ever
    # fetched. Keyed on the fresh-seed path alone, a persisted demo volume seeded
    # before this record existed never gets one, and its whole wall hatches.
    # Re-stamped on EVERY open so the span tracks today instead of freezing at
    # first-seed date and rotting a day per day of volume life.
    if fdemo.is_demo_seeded(conn):
        demo_today = _today()
        fdb.kv_set(conn, "calendar_covered", {
            "from": (demo_today - dt.timedelta(days=cfg.calendar_past_days)).isoformat(),
            "to": (demo_today + dt.timedelta(days=cfg.calendar_window_days)).isoformat(),
        })


def _init_db_once(conn) -> None:
    """Run the one-time, whole-DB setup exactly once per process: schema
    migrations, the legacy history backfill, and (DEMO only) the sample seed.
    Serialized under _init_lock so concurrent first-requests can't race the
    table-rebuild migrations. On failure it leaves _db_initialized False and
    re-raises, so the next request retries the upgrade rather than serving
    half-set-up data. (The backfill is atomic and the seed self-wipes a partial
    write, so a retry starts clean.)"""
    global _db_initialized
    with _init_lock:
        if _db_initialized:
            return
        fdb.ensure_schema(conn)
        # Seed a row (enabled) for each available integration that has none yet.
        # Idempotent: never flips an existing toggle. This is the non-breaking
        # hinge — an existing install seeds every configured source ON.
        for i, integ in enumerate(_available_only()):
            fdb.seed_integration(conn, integ["id"], integ["kind"], sort=i,
                                 enabled=integ.get("default_on", True))
        _ensure_history_backfill(conn)   # kv-guarded, atomic; raises on failure
        if DEMO:
            _ensure_demo_seed(conn)      # people-guarded; raises on failure
        _db_initialized = True


def _db():
    """A per-thread SQLite connection with a cheap self-heal ping per use, so a
    corrupted/closed handle recovers instead of erroring forever. Per-thread
    (not one shared handle) so the thread pool's request handlers never
    interleave each other's transactions (issue #29)."""
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except Exception:
            log.warning("db handle unhealthy; reconnecting", exc_info=True)
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            _tls.conn = None
    if conn is None:
        conn = fdb.connect(DB_PATH)
        _tls.conn = conn
    elif conn.in_transaction:
        # A write that failed mid-way (e.g. "database is locked") leaves its
        # implicit transaction open on this thread's handle. Handed out as-is,
        # every later read here sees that frozen snapshot and the WAL can't
        # checkpoint past it. Nothing between requests is meant to be pending.
        log.warning("db handle had an open transaction; rolling it back")
        conn.rollback()
    # Ensure the one-time whole-DB setup has run — retried here if a prior
    # attempt failed, or if a test reset _db_initialized to force a fresh init.
    if not _db_initialized:
        try:
            _init_db_once(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _tls.conn = None
            raise
    return conn


# One shared async client for the tile proxies instead of building/tearing
# down a fresh connection pool on every request. Per-request timeouts are still
# set by tiles.py at each .get() call. Closed via the lifespan on shutdown.
_http = httpx.AsyncClient()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    global _laundry_watch_task, _presence_watch_task
    # Configured but unusable is a misconfiguration, not a runtime failure:
    # say it once, here, before anything tries to use it.
    if _laundry_env_broken():
        log.error("laundry is configured in config.json but HA_TOKEN is "
                  "empty: every laundry read will fail and the wall can only "
                  "show 'Laundry unavailable'. In a container this usually "
                  "means the environment never reached it.")
    elif not DEMO and getattr(cfg, "laundry_config_error", None):
        log.error("laundry is configured in config.json but %s, so the "
                  "integration is OFF and the wall shows an unavailable card. "
                  "The per-entry reasons are logged above.",
                  cfg.laundry_config_error)
    # The laundry watcher (see "laundry: annotation, background watcher"
    # below) lives on the app's own loop so it shares _http and the SSE
    # waiters' Event. Armed only where the calendar sync would run and only
    # with laundry configured; cancelled cleanly on shutdown. The done-
    # callback is the loud backstop for the "impossible" exit: the loop
    # armors every tick, so anything that still kills the task (a
    # BaseException like MemoryError) must at least leave a log line.
    watch = None
    if _laundry_watch_enabled():
        watch = asyncio.create_task(laundry_watch_loop())
        _laundry_watch_task = watch
        watch.add_done_callback(
            lambda t: t.cancelled()
            or log.error("laundry watch: task exited (real-time lane dead; "
                         "tile route falls back to inline fetch)",
                         exc_info=t.exception()))
    # A presence block with no Home Assistant to ask (no ha_base of its own
    # and no laundry block): the switch can't exist, so say why once.
    if (getattr(cfg, "presence", None) is not None
            and not fintegrations.presence_base(cfg)):
        log.warning("presence is set in config.json but there is no Home "
                    "Assistant to ask: give it an ha_base (or configure "
                    "laundry). Dim when nobody is home stays hidden.")
    # Who is home, for "dim when nobody is home": armored the same way.
    presence = None
    if _presence_watch_enabled():
        presence = asyncio.create_task(presence_watch_loop())
        _presence_watch_task = presence
        presence.add_done_callback(
            lambda t: t.cancelled()
            or log.error("away dimming: the presence task exited (the wall "
                         "will not dim for nobody home until a restart)",
                         exc_info=t.exception()))
    # The hourly backup (backups.py), armored and cancelled the same way.
    backup_task = None
    if _backup_loop_enabled():
        backup_task = asyncio.create_task(backup_loop())
        backup_task.add_done_callback(
            lambda t: t.cancelled()
            or log.error("backups: the hourly task exited (no more scheduled "
                         "backups until a restart)", exc_info=t.exception()))
    try:
        yield
    finally:
        if backup_task is not None:
            backup_task.cancel()
            with contextlib.suppress(BaseException):
                await backup_task
        if presence is not None:
            presence.cancel()
            with contextlib.suppress(BaseException):
                await presence
        # aclose() must run even if shutdown itself raises — and awaiting a
        # crashed (already-done) task re-raises its stored exception, which
        # is NOT a CancelledError, so keep that from eating the cleanup too.
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(BaseException):
                await watch
        await _http.aclose()


app = FastAPI(title="family-hub", lifespan=_lifespan)


# --- hub ------------------------------------------------------------------

# Google Calendar's fixed event-color palette (colorId 1-11). An event only
# carries a colorId when someone explicitly colors it — those override the
# calendar's rail color on the wall.
GOOGLE_EVENT_COLORS = {
    "1": "#7986CB", "2": "#33B679", "3": "#8E24AA", "4": "#E67C73",
    "5": "#F6BF26", "6": "#F4511E", "7": "#039BE5", "8": "#616161",
    "9": "#3F51B5", "10": "#0B8043", "11": "#D50000",
}


# A Google/ICS sync error is held back from the wall this long, while a
# previous good sync's events are still showing: a one-tick network or Google
# 5xx blip used to flash "Calendar sync hit a snag" on the first failure
# (review, 2026-09-22). iCloud has its own, longer hold (caldav_sync).
CALENDAR_ERROR_GRACE_MIN = 60


def _google_status_for_wall(s: dict, now: dt.datetime) -> dict:
    """The Google/ICS status as the wall should read it: ok during the grace
    window of a fresh non-auth error, when there is a last good sync to show."""
    if s.get("ok") or s.get("needs_auth") or not s.get("last_sync"):
        return s
    since = s.get("error_since")
    if not since:
        return s
    try:
        age_min = (now - dt.datetime.fromisoformat(since)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return s
    return {**s, "ok": True} if age_min < CALENDAR_ERROR_GRACE_MIN else s


def _calendar_status_agg(c) -> dict:
    """Aggregate calendar health across every ENABLED source (Google/ICS + iCloud
    CalDAV), so the wall's banner reflects whether ANY calendar is connected, not
    just Google. Without this, an unused/broken Google config showed "no calendar
    connected" while iCloud was synced and rendering. ok if any source is ok;
    else needs_auth if any needs it; else the real error; else not configured."""
    statuses = []
    if cfg.calendars and (_integration_on(c, "google_calendar")
                          or _integration_on(c, "ics_calendar")):
        statuses.append(_google_status_for_wall(
            fdb.kv_get(c, "calendar_status") or {"ok": False, "error": "not configured"},
            _now_local()))
    if _integration_on(c, "icloud_caldav"):
        statuses.append(fdb.kv_get(c, "caldav_status") or {"ok": False})
    if not statuses:
        return {"ok": False, "error": "not configured"}
    if any(s.get("ok") for s in statuses):
        # One healthy source must NOT hide another's expired sign-in. On a mixed
        # setup (Google ok + iCloud's app password revoked) the aggregate is still
        # ok — Google renders — but needs_auth has to survive so the wall shows the
        # reconnect banner; otherwise the iCloud half silently drifts stale behind
        # a "connected" wall and nobody ever reconnects it.
        agg = {"ok": True}
        if any(s.get("needs_auth") for s in statuses):
            agg["needs_auth"] = True
        elif any(s.get("sustained") for s in statuses):
            # A source has been failing (non-auth: network/5xx/TLS) long enough to
            # be genuinely stuck, not a blip. Surface it behind the healthy source
            # so the family knows that calendar may be behind — needs_auth, when
            # present, is the louder signal and wins.
            agg["degraded"] = True
        return agg
    if any(s.get("needs_auth") for s in statuses):
        return {"ok": False, "needs_auth": True}
    errs = [s.get("error") for s in statuses if s.get("error")
            and s.get("error") not in ("disabled", "not configured")]
    return {"ok": False, "error": "; ".join(errs) if errs else "not configured"}


def _calendar_block(c, today: dt.date, days: int, past_days: int = 0) -> dict:
    status = _calendar_status_agg(c)
    cal_map = {cal["id"]: cal for cal in cfg.calendars}
    # Integration gating: a disabled calendar source's events are hidden (not
    # deleted) — its cache stays, so re-enabling shows them instantly.
    cal_google_on = fdb.integration_enabled(c, "google_calendar", default=True)
    cal_ics_on = fdb.integration_enabled(c, "ics_calendar", default=True)
    # CalDAV events gate on availability AND the toggle (same as reminders), so
    # pulling the credentials hides stale cached events instead of showing them.
    cal_caldav_on = _integration_on(c, "icloud_caldav")
    # CalDAV events carry a 'caldav:<slug>' calendar_id; their name/color + the
    # per-calendar visibility toggle (the settings picker) come from the
    # caldav_collections table the sync records, not config.
    caldav_cols = {col["id"]: col for col in fdb.list_caldav_collections(c)}
    # rail color: the user's own Google sidebar color for the calendar wins;
    # config color is the pre-first-sync fallback
    google_colors = fdb.kv_get(c, "calendar_colors") or {}
    lo = (today - dt.timedelta(days=past_days)).isoformat()
    horizon = (today + dt.timedelta(days=days)).isoformat()
    events = []
    # An event configured on two calendars is stored as two rows sharing the same
    # event id (composite events PK, issue #30) — render it ONCE. Keyed on
    # identity (id + span) so genuinely distinct events never collapse; the first
    # VISIBLE copy wins its calendar's color (a copy hidden by the picker doesn't
    # claim the row).
    seen_keys: set = set()
    # SQL narrows to rows whose raw span touches the window (a superset); the
    # exact per-row trim below still decides. Every wall polls this each
    # minute, so loading the whole cached table each time was pure waste.
    for e in fdb.events_overlapping(c, lo, horizon):
        # Keep an event whose SPAN overlaps [lo, horizon] — not just its start.
        # A multi-day event that began before the window but is still running
        # today must stay on the wall (e.g. a vacation the family is living);
        # filtering on the start alone silently dropped those. All-day end_ts is
        # exclusive, so its last VISIBLE day is end-1; a timed end_ts is the real
        # end. Compare the last visible day to the low bound and the start to the
        # high bound; the frontend still expands the kept row per-day itself.
        start_day = e["start_ts"][:10]
        end_day = e["end_ts"][:10]
        if end_day > start_day and (
                e["all_day"] or e["end_ts"][11:16] == "00:00"):
            # all-day end dates are exclusive; a timed end at exactly midnight
            # likewise belongs to the previous day (a 8pm–12am show is not
            # "on" the next morning)
            end_day = (dt.date.fromisoformat(end_day) - dt.timedelta(days=1)).isoformat()
        if end_day < lo or start_day > horizon:
            continue
        cid = e["calendar_id"]
        if cid.startswith("caldav:"):
            meta = caldav_cols.get(cid)
            # hidden if the integration is off OR this specific calendar is
            # unchecked in the picker (a known collection with enabled=0)
            if not cal_caldav_on or (meta is not None and not meta["enabled"]):
                continue
            color = meta.get("color") if meta else None
            label = meta.get("display_name") if meta else None
        else:
            cal = cal_map.get(cid, {})
            cal_kind = cal.get("kind", "google")
            if cal_kind == "google" and not cal_google_on:
                continue
            if cal_kind == "ics" and not cal_ics_on:
                continue
            color = google_colors.get(cid) or cal.get("color")
            label = cal.get("label")
        dedup_key = (e["id"], e["start_ts"], e["end_ts"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        events.append({
            **e,
            "color": color,
            "label": label,
            "event_color": GOOGLE_EVENT_COLORS.get(e["color_id"] or ""),
        })
    # The range for which THIS payload is authoritative (a missing day in it is
    # really free): the INTERSECTION of what was fetched (past_days .. days),
    # what config asks the sync to cache, and what the last SUCCESSFUL sync
    # ACTUALLY covered.
    #
    # Reporting the raw config window would falsely mark a day the sync caches
    # but this request never fetched (e.g. calendar_past_days raised above the
    # frontend's fixed past=45) as free instead of "not synced" (issue #37).
    # Trusting config ALONE is that same bug from the other side: raising
    # calendar_window_days advertises the wider range the instant the app
    # restarts, while the events table still holds only the old one until a tick
    # lands, and a source that keeps failing holds its stale rows indefinitely
    # (keep_ids) while config still promises the full window. Those uncovered
    # days would render "nothing scheduled" — a confident lie about a calendar
    # nobody asked Google for.
    #
    # So each sync records the span it really pulled, and an ACTIVE source with
    # no record yet contributes NO authority: an empty window (every day hatches)
    # is the honest answer before the first successful sync. A source that isn't
    # active can't leave a hole, so it never narrows the window.
    synced_back = min(past_days, cfg.calendar_past_days)
    synced_fwd = min(days, cfg.calendar_window_days)

    def _cap_to_coverage(key, fwd, back):
        cov = fdb.kv_get(c, key)
        if cov is None:
            return -1, -1      # never synced cleanly yet — claim nothing
        if not isinstance(cov, dict):
            # Belt-and-braces, and deliberately kept: the except below already
            # catches the TypeError that indexing a str/list/int would raise, so
            # this branch is behaviorally redundant. It stays so the ordinary
            # shape check doesn't ride on exception control flow, and so the log
            # line can name the real problem instead of a subscript error.
            log.warning("%s holds a malformed coverage record (%r); reporting "
                        "no synced window", key, cov)
            return -1, -1
        try:
            fwd = min(fwd, (dt.date.fromisoformat(cov["to"]) - today).days)
            back = min(back, (today - dt.date.fromisoformat(cov["from"])).days)
        except (KeyError, TypeError, ValueError) as e:
            # This block is inlined into the /api/hub payload, so raising here
            # would blank the ENTIRE wall — chores, to-dos and all — over one bad
            # kv row. Claim nothing instead: never over-claim, never 500.
            # All three are reachable from a hand-edited, partially-written or
            # restored kv row: a non-string date raises TypeError, a truncated
            # row KeyError, an unparseable date ValueError.
            log.warning("%s holds a malformed coverage record (%r): %s; "
                        "reporting no synced window", key, cov, e)
            return -1, -1
        return fwd, back

    # google/ICS rows come from cfg.calendars; CalDAV rows only exist once a
    # discover has recorded collections. Either module being switched off (or
    # having nothing configured) means it contributes no rows to hide behind.
    if cfg.calendars and (cal_google_on or cal_ics_on):
        synced_fwd, synced_back = _cap_to_coverage(
            "calendar_covered", synced_fwd, synced_back)
    # Only an ENABLED collection can be hiding a hole: an unchecked one's events
    # are filtered out above, so letting it narrow the window would hatch a
    # perfectly healthy Google-backed calendar over a source the operator
    # deliberately switched off.
    # VEVENT rows ONLY. caldav_covered records how far the EVENT fetch reached,
    # but list_caldav_collections returns reminder lists (VTODO) too, and they are
    # never pruned. Counting a VTODO row let an enabled reminders list — on an
    # install with every actual iCloud calendar unchecked — collapse the window
    # and hatch a perfectly healthy Google-backed wall.
    if cal_caldav_on and any(col["enabled"] for col in caldav_cols.values()
                             if col.get("comp_type") == "VEVENT"):
        synced_fwd, synced_back = _cap_to_coverage(
            "caldav_covered", synced_fwd, synced_back)
    return {
        "status": status,
        "events": events,
        "window": {
            "from": (today - dt.timedelta(days=synced_back)).isoformat(),
            "to": (today + dt.timedelta(days=synced_fwd)).isoformat(),
        },
    }


def _links(enabled_ids: set | None = None) -> dict:
    # cameras: config-driven list of go2rtc streams. Each tile embeds the
    # stream's WebRTC player; full-screen prefers a "hd" stream when the
    # config names one (e.g. a Protect cam's 4K twin), else the same src.
    # Each entry is validated INDIVIDUALLY: a single malformed camera/panel
    # (missing "src"/"id"/"url", or a non-integer vw) must not raise out of
    # /api/hub and blank the ENTIRE wall (chores, calendar, every tile) over one
    # typo. A bad entry is skipped and logged; the good ones still render.
    # The Cameras integration toggle (enabled_ids) blanks the camera lists when
    # off; None means "no gating" (older callers / tests). DEMO cameras are a
    # canned showcase with no real `cameras` integration to seed, so they bypass
    # the toggle and always render.
    cameras_on = DEMO or enabled_ids is None or "cameras" in enabled_ids
    if not cameras_on:
        cameras = camera_page = []
    elif DEMO:
        # Placeholder camera tiles (no go2rtc, no liveness probe): the frontend
        # paints a static gradient for each (see hub.js tileCamera). Panels
        # still come from config below (empty is fine in demo).
        cameras = fdemo.demo_cameras()
        camera_page = fdemo.demo_camera_page()
    else:
        cameras = _camera_links(cfg.cameras)
        # The Cameras-tab grid: its own config list, or the wall cameras when
        # unset, so an existing config without `camera_page` still fills the grid.
        camera_page = _camera_links(cfg.camera_page or cfg.cameras)
    panels = _config_panel_links()
    return {"cameras": cameras, "panels": panels, "camera_page": camera_page}


# One request's answer from _available_only(), set by _available_once(). The
# answer includes the iCloud credential state, which reads and parses a file
# on disk, and one /api/hub build asks for it about six times (every
# _integration_on gate). A ContextVar, so it is scoped to the request that set
# it and never leaks to another thread or the next poll.
_avail_memo: contextvars.ContextVar = contextvars.ContextVar(
    "family_hub_avail_memo", default=None)


@contextlib.contextmanager
def _available_once():
    """Compute _available_only() once and reuse it for the rest of the block."""
    token = _avail_memo.set(_available_only())
    try:
        yield
    finally:
        _avail_memo.reset(token)


def _available_only():
    """The available integrations, with iCloud CalDAV availability reflecting the
    REAL credential state (env OR the server-side creds file), so UI-entered
    credentials make the integration appear without an env var or restart.
    DEMO overlay: the demo serves placeholder cameras and canned weather/climate
    (see _camera_links and the DEMO branches) even though config leaves them
    unset, so the registry must call them available or the layout engine and
    tab bar hide the demo's columns. Inside _available_once() the request's
    one computed answer is returned instead (callers only read it)."""
    memo = _avail_memo.get()
    if memo is not None:
        return memo
    items = fintegrations.available_integrations(
        cfg, os.environ, caldav_ok=caldav_service.configured(os.environ))
    demo_ids = ({"cameras", "weather", "climate", "laundry", "fleet", "printer",
                 "away_dim"}
                if DEMO else set())
    return [dict(i, available=True) if (i["id"] in demo_ids and not i["available"])
            else i
            for i in items
            if i["available"] or i["id"] in demo_ids]


def _integration_on(c, iid: str) -> bool:
    """One definition of 'this integration is on': available (configured in
    config/env) AND its toggle enabled. Every render/sync gate goes through here
    so their notions of 'on' cannot drift (Fable architecture review, rec 4)."""
    integ = next((i for i in _available_only() if i["id"] == iid), None)
    return integ is not None and fdb.integration_enabled(
        c, iid, default=integ.get("default_on", True))


def _visible_reminders(c) -> list:
    """iCloud reminders for the wall, rendered from cal_objects — the SINGLE
    source of truth that holds both the synced server state AND un-pushed wall
    edits (PENDING_*). Rendering from it (rather than a separate kv snapshot plus
    an overlay) is what makes a wall edit show instantly AND stay put: there's no
    stale pulled-snapshot to transiently revert to once the push lands and the row
    flips back to SYNCED. Respects the calendar picker (a reminder list unchecked
    in settings is hidden) and skips objects queued for deletion.

    Completed reminders are left out in SQL, before any parse: they pile up
    (the chore mirror leaves one per chore per person per day) and the wall
    never shows them, yet this runs on every /api/hub poll. Rows of a list
    iCloud no longer has (deleted or unshared; caldav_sync drops it and parks
    its unsent edits) are skipped too, rather than shown under no list."""
    cols = fdb.list_caldav_collections(c)
    names = {col["id"]: col["display_name"] for col in cols}
    shown = {col["id"] for col in cols if col["enabled"]}
    out = []
    for o in fdb.list_open_vtodo_objects(c):
        if o["collection_id"] not in shown:
            continue
        try:
            out.extend(remlogic.parse_vtodo(o["raw_ics"], o["collection_id"],
                                            names.get(o["collection_id"], "")))
        except Exception:
            log.warning("reminder render skipped: %s", o["id"], exc_info=True)
    return out


def _reminder_lists(c) -> list:
    """Enabled iCloud reminder (VTODO) lists as [{id, name}] — the targets a
    wall-added reminder can be filed under (the To-Do surface's add control)."""
    return [{"id": col["id"], "name": col["display_name"]}
            for col in fdb.list_caldav_collections(c)
            if col["comp_type"] == "VTODO" and col["enabled"]]


def _laundry_row_status() -> str | None:
    """The laundry integration's health for the settings row: 'needs_auth',
    'error', or None when it is fine (or when there is nothing to complain
    about, as in DEMO).

    Every way laundry can be configured-and-not-working lands here, because
    each of them otherwise shows up as a card that simply is not there:

      - no HA token at all (the 2026-09-17 lost-.env incident);
      - a token Home Assistant REJECTS, which looks identical on the wall and
        is the likelier case, since tokens are revoked from HA's own UI;
      - a `laundry` config block that nothing valid survived (a typo'd entity
        key used to delete the integration outright);
      - a machine stuck offline while its sibling reports, which keeps the
        tile "available" and would otherwise never be mentioned anywhere;
      - the whole feed being unavailable long enough to have earned an ERROR,
        which is the loudest lane in the log and used to be the quietest one
        here: HA down, or every configured entity renamed at once, left the row
        reading healthy next to a wall that said "Laundry unavailable".

    DEMO serves canned laundry with no Home Assistant at all, so a demo wall
    is not broken and must not nag."""
    if DEMO:
        return None
    if getattr(cfg, "laundry_config_error", None):
        return "error"
    if fintegrations.laundry_needs_auth(cfg, os.environ):
        return "needs_auth"
    if tiles.laundry_auth_rejected():
        return "needs_auth"
    if _laundry_machines_stuck or _laundry_unavail_alerted:
        return "error"
    return None


def _integ_status(iid: str, caldav_status: dict, cal_status: dict,
                  mirror_status: dict | None = None,
                  laundry_status: str | None = None,
                  away_status: str | None = None):
    """A compact health string for an integration: 'ok' | 'needs_auth' | 'error',
    or None for one with no sync (cameras/weather/climate). Drives the settings
    menu's 'Reconnect iCloud' / warning affordance on auth-failure or error.

    Laundry has no sync of its own, but it DOES have a credential and a config
    block, so `laundry_status` (computed once per request by
    _laundry_row_status) is passed straight through: the row explains a card
    that reads "unavailable" instead of the operator finding no laundry row at
    all. It applies to the laundry row ONLY -- passing it in here for every id
    would badge every integration on the wall.

    The chore mirror rides on the iCloud integration, so a failing mirror tick
    shows as an error on that row — it used to fail forever with the row still
    reading 'ok'."""
    # calendar_status is shared by Google + ICS and can't tell them apart, and
    # needs_auth is a Google-only concept — so only surface it on google_calendar
    # (ICS gets no status rather than mis-inheriting Google's auth state).
    if iid == "laundry":
        return laundry_status
    if iid == "away_dim":
        return away_status
    src = (caldav_status if iid == "icloud_caldav"
           else cal_status if iid == "google_calendar" else None)
    if not src:
        return None
    if src.get("needs_auth"):
        return "needs_auth"
    err = src.get("error")
    if src.get("ok") is False and err not in (None, "", "not configured", "disabled"):
        return "error"
    if iid == "icloud_caldav" and (mirror_status or {}).get("ok") is False:
        return "error"
    return "ok"


def _integrations_state(c) -> dict:
    """The available integrations plus their enable/disable state and health, and
    the set of enabled ids for render gating. Available comes from config/env
    (the registry); the enabled flag comes from the integrations table (default
    True for a not-yet-seeded one, so gating never hides an un-toggled source)."""
    caldav_status = fdb.kv_get(c, "caldav_status") or {}
    cal_status = fdb.kv_get(c, "calendar_status") or {}
    mirror_status = fdb.kv_get(c, "chore_mirror_status") or {}
    laundry_status = _laundry_row_status()
    away_status = _away_row_status()
    lst = []
    enabled_ids = set()
    for integ in _available_only():
        en = fdb.integration_enabled(c, integ["id"],
                                     default=integ.get("default_on", True))
        if en:
            enabled_ids.add(integ["id"])
        entry = {"id": integ["id"], "kind": integ["kind"],
                 "name": integ["name"], "enabled": en,
                 "group": integ.get("group", "integration"),
                 "status": _integ_status(integ["id"], caldav_status, cal_status,
                                         mirror_status, laundry_status,
                                         away_status if en else None)}
        if integ["id"] == "icloud_caldav":
            # the connected Apple ID (not a secret) so settings can show it; the
            # password is never included. readonly = 1-way (read-only) vs 2-way.
            entry["account"] = caldav_service.caldav_credentials(os.environ)[0]
            entry["readonly"] = fdb.integration_config(
                c, "icloud_caldav").get("readonly", True)
            # un-pushed wall edits still queued (0 normally); lets settings warn
            # "N changes not yet synced" instead of the backlog being invisible.
            entry["pending"] = caldav_status.get("pending", 0)
            # wall edits iCloud refused for good, or whose list is gone: kept
            # but no longer sent, so they get their own note.
            entry["parked"] = caldav_status.get("parked", 0)
            # people whose chore list is gone from iCloud: left out of the
            # mirror until a new list is picked. Read live, so picking one
            # clears the settings line at once.
            entry["lists_gone"] = [
                p["name"] for p in chore_mirror.people_with_gone_lists(c)]
        lst.append(entry)
    return {"list": lst, "enabled_ids": enabled_ids}


def _camera_links(entries: list[dict]) -> list[dict]:
    cameras = []
    for cam in entries:
        try:
            src = cam["src"]
            hd_src = cam.get("hd", src)
            cameras.append({
                "src": src,
                "label": cam.get("label", src),
                # The hub's own go2rtc proxy, same origin as the wall.
                "tile": f"/go2rtc/stream.html?src={quote(src, safe='')}&mode=webrtc",
                "full": f"/go2rtc/stream.html?src={quote(hd_src, safe='')}",
                # Full-screen shows the warm tile stream first, then upgrades to a
                # distinct HD twin only when one is configured. has_hd is the
                # explicit signal (the tile/full URLs always differ by query
                # string, so the frontend can't infer it); hd_src lets the wall
                # probe the HD stream's readiness before revealing it.
                "has_hd": hd_src != src,
                "hd_src": hd_src,
            })
        except (KeyError, ValueError, TypeError) as e:
            log.warning("skipping malformed camera config entry %r: %s", cam, e)
    return cameras


def _config_panel_links() -> list[dict]:
    # panels: config-driven always-on dashboard embeds (see Config.panels for
    # the field semantics). Passed through with defaults resolved so the
    # frontend never guesses.
    panels = []
    for p in cfg.panels:
        try:
            panels.append({
                "id": p["id"],
                "label": p.get("label", p["id"]),
                "url": p["url"],
                "vw": int(p["vw"]),
                "vh": int(p["vh"]),
                "page_w": int(p.get("page_w", p["vw"])),
                "crop_top": int(p.get("crop_top", 0)),
                "crop_left": int(p.get("crop_left", 0)),
                "full": p.get("full", "native"),
                "full_url": p.get("full_url", p["url"]),
            })
        except (KeyError, ValueError, TypeError) as e:
            log.warning("skipping malformed panel config entry %r: %s", p, e)
    return panels


def _db_usable() -> Exception | None:
    """None when the hub can use its database, else the error. One read of a
    table the app always has, on the same per-thread connection the routes
    use. SELECT 1 alone never reads the file, and a schema count passes on the
    empty file sqlite quietly creates when hub.db has gone missing."""
    try:
        _db().execute("SELECT 1 FROM kv LIMIT 1").fetchone()
        # A connection opened before hub.db was deleted keeps reading the
        # unlinked file, so check the path too (after _db(), which creates the
        # file on a fresh install).
        if not os.path.exists(DB_PATH):
            raise FileNotFoundError("hub.db is missing")
    except Exception as e:
        log.error("health: database unusable: %s", e)
        return e
    return None


@app.get("/health")
def health():
    """Liveness for the container healthcheck, and it means the hub can use its
    database. It used to return ok without touching the db, so a missing or
    corrupt hub.db read healthy while every real request failed. Stays
    sub-millisecond. Whether the hub WORKS (fresh data, settings, the shipped
    config) is /health/full; this stays liveness so an upstream outage never
    marks the container unhealthy."""
    e = _db_usable()
    if e is not None:
        return JSONResponse({"status": "error", "db": type(e).__name__},
                            status_code=503)
    return {"status": "ok"}


def _health_full_local() -> dict:
    """The parts of /health/full that read the database and the filesystem,
    run off the event loop."""
    e = _db_usable()
    out = {"db_ok": e is None,
           "db_error": None if e is None else f"{type(e).__name__}: {e}",
           "config_now": deep_health.file_sha256(CONFIG_PATH),
           "google_token": deep_health.token_file_present(TOKEN_PATH)}
    if e is not None:
        # Without the database the toggles and sync statuses are unknown: judge
        # every configured source as on, so nothing passes by default.
        avail = {i["id"] for i in _available_only()}
        out.update(on=avail, cal_status={}, caldav_status={}, backup=None)
        return out
    c = _db()
    out["on"] = {i["id"] for i in _available_only()
                 if fdb.integration_enabled(c, i["id"],
                                            default=i.get("default_on", True))}
    out["cal_status"] = fdb.kv_get(c, "calendar_status") or {}
    out["caldav_status"] = fdb.kv_get(c, "caldav_status") or {}
    try:
        out["backup"] = _build_backup(c)
    except Exception:
        log.error("health: backup status read failed", exc_info=True)
        out["backup"] = None
    try:
        out["backups_in_app"] = _backups_health_note(c)
    except Exception:
        log.error("health: in-app backup state read failed", exc_info=True)
        out["backups_in_app"] = None
    try:
        out["backups_restore_test"] = _restore_test_health_note(c)
    except Exception:
        log.error("health: restore test state read failed", exc_info=True)
        out["backups_restore_test"] = None
    return out


async def _go2rtc_streams() -> tuple[dict | None, str | None]:
    """go2rtc's stream list (name -> details), or None and why."""
    base = _fetch_cfg.go2rtc_base
    try:
        r = await _http.get(f"{base}/api/streams", timeout=tiles.TIMEOUT)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError(f"non-dict body {type(body).__name__}")
        return body, None
    except Exception as e:
        return None, f"{base}/api/streams: {type(e).__name__}: {e}"[:200]


def _camera_srcs() -> list[str]:
    """Every go2rtc stream name config.json points the wall or phone at."""
    out = []
    for cam in list(cfg.cameras or []) + list(getattr(cfg, "camera_page", []) or []):
        if isinstance(cam, dict):
            out += [v for v in (cam.get("src"), cam.get("hd"))
                    if isinstance(v, str) and v]
    return out


@app.get("/health/full")
async def health_full():
    """Does the hub WORK? Fresh data from every source the wall shows, read by
    THIS process; the settings those sources need; config.json being the one
    the deploy shipped; the commit the image was built from. See deep_health.py.
    The deploy gate (homelab-deploy, in the private deploy overlay) reads it. Always
    200: a report, not a liveness probe."""
    now = time.time()
    try:
        local = await asyncio.to_thread(_health_full_local)
    except Exception as e:     # a bug here must be a named problem, not a 500
        log.exception("health/full: reading the database and files crashed")
        local = {"db_ok": False, "db_error": f"health check crashed: {type(e).__name__}: {e}",
                 "config_now": deep_health.file_sha256(CONFIG_PATH),
                 "google_token": deep_health.token_file_present(TOKEN_PATH),
                 "on": {i["id"] for i in _available_only()},
                 "cal_status": {}, "caldav_status": {}, "backup": None}
    on = local["on"]
    avail = {i["id"] for i in _available_only()}

    async def _skip():
        return {"available": False}

    weather_on = "weather" in on and not DEMO
    climate_on = "climate" in on and not DEMO
    # the printer card shows the same rollup, so it keeps the source checked
    fleet_on = ("fleet" in on or "printer" in on) and not DEMO
    cams_on = "cameras" in on and not DEMO and bool(_fetch_cfg.go2rtc_base)
    results = await asyncio.gather(
        tiles.weather_tile(_http, cfg) if weather_on else _skip(),
        tiles.climate_tile(_http, cfg) if climate_on else _skip(),
        tiles.fleet_tile(_http, cfg) if fleet_on else _skip(),
        _go2rtc_streams() if cams_on else asyncio.sleep(0, (None, None)),
        return_exceptions=True)
    # A tile that RAISES (fleet_tile does on purpose for a build bug) becomes
    # that source's named error in the report instead of a 500 with no reason.
    for name, r in zip(("weather", "climate", "fleet", "cameras"), results):
        if isinstance(r, BaseException):
            log.error("health/full: %s check crashed", name, exc_info=r)
            tiles._note_error(name, f"health check crashed: {type(r).__name__}: {r}")
    weather, climate, fleet = [r if isinstance(r, dict) else {"available": False}
                               for r in results[:3]]
    if isinstance(results[3], BaseException):
        streams, streams_err = None, f"health check crashed: {type(results[3]).__name__}: {results[3]}"
    else:
        streams, streams_err = results[3]

    has_google = any(c.get("kind", "google") == "google" for c in cfg.calendars or [])
    google_on = bool(cfg.calendars) and ("google_calendar" in on or "ics_calendar" in on)
    laundry_configured = fintegrations.laundry_configured(cfg)
    sources = {
        "calendar": deep_health.calendar_source(
            configured=bool(cfg.calendars) or "icloud_caldav" in avail,
            enabled={"google": google_on, "icloud": "icloud_caldav" in on},
            statuses={"google": local["cal_status"], "icloud": local["caldav_status"]},
            now=now, started_at=PROCESS_STARTED_AT),
        "laundry": deep_health.laundry_source(
            configured=laundry_configured, enabled="laundry" in on and not DEMO,
            config_error=getattr(cfg, "laundry_config_error", None),
            token_present=bool(fintegrations.ha_token(os.environ)),
            auth_rejected=tiles.laundry_auth_rejected(),
            watching=_laundry_watch_task is not None and not _laundry_watch_task.done(),
            last_ok=_laundry_last_ok_wall, snapshot=_laundry_snapshot,
            now=now, started_at=PROCESS_STARTED_AT),
        "weather": deep_health.tile_source(
            "weather", configured="weather" in avail, enabled=weather_on,
            state=tiles.SOURCE_STATE.get("weather"), available=weather.get("available"),
            now=now, max_age_s=deep_health.WEATHER_MAX_AGE_S),
        "climate": deep_health.tile_source(
            "climate", configured="climate" in avail, enabled=climate_on,
            state=tiles.SOURCE_STATE.get("climate"), available=climate.get("available"),
            now=now, max_age_s=deep_health.CLIMATE_MAX_AGE_S),
        "fleet": deep_health.tile_source(
            "fleet", configured="fleet" in avail or "printer" in avail,
            enabled=fleet_on,
            state=tiles.SOURCE_STATE.get("fleet"), available=fleet.get("available"),
            now=now, max_age_s=deep_health.FLEET_MAX_AGE_S),
        "presence": deep_health.presence_source(
            configured="away_dim" in avail and not DEMO,
            enabled="away_dim" in on and not DEMO,
            token_present=bool(fintegrations.ha_token(os.environ)),
            watching=_presence_watching(),
            snap=_presence_snap, last_ok=_presence_last_ok,
            now=now, started_at=PROCESS_STARTED_AT),
        "cameras": deep_health.cameras_source(
            configured="cameras" in avail, enabled=cams_on,
            wanted=_camera_srcs(), streams=streams, error=streams_err),
    }
    config_err = getattr(cfg, "laundry_config_error", None)
    settings = {
        "ha_token": deep_health.setting(
            laundry_configured and not DEMO,
            bool(fintegrations.ha_token(os.environ)),
            "laundry is configured, so HA_TOKEN must reach the container"),
        "google_token": deep_health.setting(
            has_google and google_on and not DEMO, local["google_token"],
            f"Google calendars are configured, so {TOKEN_PATH} must exist"),
        "config": deep_health.setting(
            True, not config_err,
            "config.json must load cleanly"
            + (f": {config_err}" if config_err else "")),
    }
    return deep_health.assemble(
        now=now, started_at=PROCESS_STARTED_AT, version=APP_VERSION, build=BUILD,
        build_info=BUILD_INFO,
        config=deep_health.config_block(CONFIG_PATH, CONFIG_LOADED_SHA256,
                                        local["config_now"], BUILD_INFO),
        db_ok=local["db_ok"], db_error=local["db_error"],
        settings=settings, sources=sources,
        notes={"backup": local["backup"], "backups_in_app": local.get("backups_in_app"),
               "backups_restore_test": local.get("backups_restore_test")})


@app.get("/api/version")
def api_version():
    """The deployed version + build hash — a debug/ops readout ("what's actually
    running?"). The changelog itself lives on GitHub, not here."""
    return {"version": APP_VERSION, "build": BUILD}


# Recent client viewport reports for the iOS Chrome tab-bar-gap self-heal
# (hub.js). Bounded ring buffers — pure diagnostics, never persisted. Actual
# reload events are kept in their OWN deque so a burst of routine wake reports
# can never evict the one occurrence this readout exists to show.
_VIEWPORT_DIAG: "collections.deque[dict]" = collections.deque(maxlen=50)
_VIEWPORT_RELOADS: "collections.deque[dict]" = collections.deque(maxlen=20)


# A viewport report is small (a dozen scalar fields); anything larger is junk
# and is dropped before it is buffered into memory.
_MAX_DIAG_BODY = 8192


@app.post("/api/diag/viewport")
async def diag_viewport(request: Request):
    """Client viewport telemetry for the iOS Chrome tab-bar gap (#45/#53/#80).
    hub.js fire-and-forgets the raw innerHeight / visualViewport / learned-max
    numbers on every wake so a real stuck-short occurrence is READ off the box
    instead of guessed a fifth time. Deliberately never raises and never
    hard-validates — a diagnostic that 422s or 500s is worse than useless. Only
    finite scalar fields are kept, strings truncated, oversized bodies dropped,
    and the buffers are capped, so a malformed or oversized body can't grow
    memory OR wedge the read side."""
    # Drop a declared-oversized body before buffering it into RAM.
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_DIAG_BODY:
        return {"ok": True}
    # A chunked body declares no length, so read the stream ourselves and
    # stop at the cap: request.body() would buffer all of it first.
    buf = bytearray()
    try:
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > _MAX_DIAG_BODY:
                return {"ok": True}
    except Exception:
        # client went away mid-body; nothing to record, and this endpoint
        # never raises (see the docstring)
        log.debug("viewport diag: body read failed", exc_info=True)
        return {"ok": True}
    try:
        raw = bytes(buf)
        # parse_constant neutralizes NaN / Infinity / -Infinity — json.loads
        # accepts those non-standard tokens, but Starlette's JSONResponse
        # serializes with allow_nan=False, so a stored NaN would 500 the GET.
        data = json.loads(raw or b"{}", parse_constant=lambda _c: None)
    except Exception:
        data = {"_unparseable": True}
    if not isinstance(data, dict):
        data = {"_raw": str(data)[:80]}
    entry = {"at": dt.datetime.now(TZ).isoformat(timespec="seconds")}
    for k in list(data)[:20]:
        kk = str(k)[:40]
        if kk == "at":
            continue   # the server owns the timestamp; a client "at" can't clobber it
        v = data[k]
        if isinstance(v, float) and not math.isfinite(v):
            v = None   # belt-and-suspenders past parse_constant, so GET never 500s
        elif isinstance(v, str):
            v = v[:80]
        elif not isinstance(v, (int, float, bool, type(None))):
            v = str(v)[:80]
        entry[kk] = v
    _VIEWPORT_DIAG.append(entry)
    if data.get("reason") == "reload":
        # The load-bearing event: a self-heal reload actually fired. Kept in its
        # own buffer so routine wake reports can't evict it, and logged loudly.
        _VIEWPORT_RELOADS.append(entry)
        log.info("viewport self-heal RELOAD fired: %s", entry)
    else:
        log.debug("viewport diag (%s): inner=%s max=%s short=%s",
                  data.get("reason"), data.get("inner"),
                  data.get("learnedMax"), data.get("shortfall"))
    return {"ok": True}


@app.get("/api/diag/viewport")
def diag_viewport_recent():
    """The recent viewport reports plus the protected list of actual reload
    events (both newest last) — reads the tab-bar-gap state off the box without
    needing the container logs."""
    return {"recent": list(_VIEWPORT_DIAG), "reloads": list(_VIEWPORT_RELOADS)}


def _keep_done_rows(c, d_str: str, rows: list[dict]) -> list[dict]:
    """Today's plan, with every chore ALREADY checked off today kept exactly as
    it was served when it was done. Re-resolving today after the fact (someone
    paused, a chore's days edited) must not erase finished work: on 2026-08-26
    thirteen morning check-offs vanished from that day when "Pause everyone"
    started, and on 09-12 a Saturday check-off was dropped when the chore was
    edited to Fridays (review, 2026-09-22).

    Only a done chore the new plan DROPS is kept. A done chore that is still
    in the plan follows it, even to a new owner: a returning owner (or a backup
    taking over mid-day) must own that finished row so their streak counts it
    (test_return_mid_day_keeps_the_owners_streak)."""
    locked = _locked_done_ids(c, d_str, {r["chore_id"] for r in rows})
    if not locked:
        return rows
    served = {r["chore_id"]: r for r in fdb.day_log(c, d_str)}
    # Kept rows are LOCKED: shown done, not tappable, and uncomplete() refuses
    # them (same helper). Unticking one would drop it from today with no way
    # to tick it back (it is no longer due, or its owner is paused).
    return rows + [{**served[cid], "locked": True} for cid in sorted(locked)]


def _locked_done_ids(c, d_str: str, planned: set) -> set:
    """Chores done on ``d_str`` that the plan no longer has, but that were
    served (frozen) that day and still exist: the rows _keep_done_rows keeps
    and uncomplete() refuses. One rule for both, so the wall never shows a
    row as tappable that the server then refuses, or the reverse. A chore
    that was DELETED or turned off is not kept (db.delete_chore)."""
    done = {r["chore_id"] for r in fdb.completions_between(c, d_str, d_str)}
    if not done - planned:
        return set()
    live = {ch["id"] for ch in fdb.list_chores(c)}
    served = {r["chore_id"] for r in fdb.day_log(c, d_str)}
    return (done - planned) & live & served


def _freeze_day(c, d_str: str, rows: list[dict]) -> None:
    """Write the day's live-resolved plan into the occurrence log — the moment
    history becomes frozen. Skips the write when the frozen rows already match,
    so the wall's constant polling doesn't churn the DB."""
    def key(r):
        # covering_for is part of the identity of a frozen row: person_id
        # already changes whenever the covering does, but including it keeps
        # the comparison exact (and catches a covering change that somehow
        # didn't move the assignee).
        # kind and cents too: re-tagging a chore today must re-freeze today
        return (r["chore_id"], r["person_id"], r["title"], r["icon"], r["rot"],
                r.get("covering_for"), r.get("kind") or "house",
                int(r.get("cents") or 0), r.get("earner"),
                0 if r.get("earning", 1) in (0, False) else 1)
    if sorted(key(r) for r in fdb.day_log(c, d_str)) != \
            sorted(key(r) for r in rows):
        fdb.replace_day_log(c, d_str, rows)


def _away_view(c, d: dt.date):
    """Build the away overlay for date ``d``: who's away (and their backup for
    fixed chores) plus the full per-person away-date map used for streak/week
    rest-day math. Returns ``(amap, away_view, away_ok)`` where ``away_view`` is
    the ``{"ids", "backup"}`` dict ``plan_rows`` consumes.

    Fails soft, same spirit as the todos block in hub() / _links(): a bad
    overlay build must not blank the whole wall over one broken row. On failure
    it returns an empty overlay with ``away_ok=False`` so callers can surface a
    degraded-state note instead of silently rendering an away person as present.
    Shared by _people_day (the wall render) AND complete() (so a backup tapping
    a covering chore resolves to the SAME assignee the wall showed)."""
    d_str = d.isoformat()
    window_from = (d - dt.timedelta(days=370)).isoformat()
    try:
        amap = fdb.away_map(c, window_from, d_str)
        return amap, chlogic.away_view_on(amap, d_str), True
    except Exception:
        log.error("away overlay failed; serving with no away overlay",
                  exc_info=True)
        return {}, {"ids": set(), "backup": {}}, False


def _people_day(c, d: dt.date) -> tuple[list[dict], bool]:
    """The per-person chore plan for date ``d`` — done flags, rotation tags,
    and streak/week computed AS OF that day. Shared by the hub home feed
    (d=today) and the full-screen chores day browser.

    History is FROZEN: past days render from the occurrence log exactly as
    they were served, so editing or deleting a chore only changes today and
    the future. Today is resolved live from current definitions and frozen
    into the log on each serve; future days are resolved live, never logged.
    A past day the wall never served (server down, pre-install) has no log
    rows and reads as a rest day — streak-neutral by design."""
    today = _today()
    d_str = d.isoformat()
    people = fdb.list_people(c)

    window_from = (d - dt.timedelta(days=370)).isoformat()
    amap, away_view, away_ok = _away_view(c, d)
    away_today = away_view["ids"]

    wallet_on = False
    if d < today:
        rows = fdb.day_log(c, d_str)
    else:
        rows = chlogic.plan_rows(fdb.list_chores(c), people, d, away_view)
        if d == today:
            rows = _keep_done_rows(c, d_str, rows)
        wallet_on = _rewards_on_safe(c) if d == today else False
        priced_ok = wallet_on is not None    # unknown state: don't freeze
        if wallet_on:
            rows, priced_ok = _price_rows(c, rows, today)
        # Only freeze a plan built WITH the away overlay. A degraded build
        # treats everyone as present, and the log is permanent history: it
        # would record the wrong owner for good. The next healthy serve
        # freezes the real plan.
        if d == today and away_ok and not priced_ok:
            log.warning("today's chore log not saved (chore wallet pricing "
                        "failed); the next serve retries")
        elif d == today and away_ok:
            _freeze_day(c, d_str, rows)
            # served even when nobody had anything today: the chore wallet
            # counts a rest day as kept, but an outage day as neither
            fdb.mark_served(c, d_str)
        elif d == today:
            log.warning("today's chore log not saved (away overlay failed); "
                        "if it keeps failing, %s drops out of chore history",
                        d_str)

    completed_ids = {r["chore_id"]
                     for r in fdb.completions_between(c, d_str, d_str)}
    plan = chlogic.day_plan(rows, people, completed_ids)

    logs = fdb.logs_between(c, window_from, d_str)
    history = fdb.completions_between(c, window_from, d_str)
    # Who OWNED each (day, chore) — straight from the frozen log, which is the
    # record of what the wall actually asked of whom. The streak input below is
    # keyed off this, NOT off completions.person_id: a completion means "that
    # chore got done that day", and today's owner can legitimately change after
    # it was tapped (the away overlay re-freezes today whenever someone leaves
    # or comes back mid-day). Keying the streak off the tapper instead broke the
    # returning person's day — the card drew the tick while the streak counted
    # the day unfinished — and the mirror-image case broke the backup's. The
    # completion rows themselves are never rewritten, so the ledger still says
    # who physically did it, and already-written histories read correctly.
    owner = {(r["date"], r["chore_id"]): r["person_id"] for r in logs}
    if d > today:
        # future days aren't logged; their live plan is the owner of record
        for r in rows:
            owner[(d_str, r["chore_id"])] = r["person_id"]
    # Group the ~370 days of log and completion rows by person ONCE, instead
    # of re-walking both lists for every person on every poll.
    occ_by_pid: dict[int, dict[str, set]] = {}
    for r in logs:
        by_day = occ_by_pid.setdefault(r["person_id"], {})
        by_day.setdefault(r["date"], set()).add(r["chore_id"])
    cbd_by_pid: dict[int, dict[str, set]] = {}
    for r in history:
        # a day with no log row at all (server was down, pre-install) falls
        # back to the completion's own person_id rather than vanishing
        who = owner.get((r["date"], r["chore_id"]), r["person_id"])
        by_day = cbd_by_pid.setdefault(who, {})
        by_day.setdefault(r["date"], set()).add(r["chore_id"])
    # With the wallet on, the old chore-day streak is a different count from
    # the wallet streak on today's card, so other days show none (audit,
    # 2026-11-26). Today's card carries the wallet streak in "reward".
    old_streak = d == today or not _rewards_on_safe(c)
    for entry in plan:
        pid = entry["person"]["id"]
        occ = occ_by_pid.get(pid, {})
        if d > today:
            # future days aren't logged; overlay d's live rows so the browser
            # can show a prospective streak/week for that day
            live = {r["chore_id"] for r in rows if r["person_id"] == pid}
            if live:
                occ[d_str] = live
        cbd = cbd_by_pid.get(pid, {})
        away_dates = amap.get(pid, {}).get("dates", set())
        entry["away"] = pid in away_today
        entry["streak"] = (chlogic.streak(occ, cbd, d, away_dates)
                           if old_streak else 0)
        entry["week"] = chlogic.week_strip(occ, cbd, d, away_dates)
    if d == today and wallet_on:
        _add_rewards(c, plan, rows, today, priced_ok)
    return plan, away_ok


def _rewards_on_safe(c) -> bool | None:
    """True / False, or None when the wallet's state can't be read (then the
    caller shows the wall without it and does not freeze today)."""
    try:
        return rewards_active(c)
    except Exception:
        log.error("chore wallet state unreadable; showing the wall without it "
                  "and not saving today's chore log", exc_info=True)
        return None


def _wallet_away(c, launch_day: str, today: dt.date) -> dict:
    """Away dates per person since launch (the wall's own away window only
    reaches back about a year; the wallet walks back to launch)."""
    amap = fdb.away_map(c, launch_day, today.isoformat())
    return {pid: v.get("dates", set()) for pid, v in amap.items()}


def _price_rows(c, rows: list[dict], today: dt.date):
    """Stamp what each of today's rows pays (house rows only). Returns
    (rows, ok); on a failure the rows come back unpriced and ok is False so
    the caller does not freeze a day that would then pay nothing for good.
    (The cards work out their own runs after today is frozen; sharing this
    run with them read the log before the freeze. Review, 2026-11-26.)"""
    try:
        settings, launch = reward_settings(c), fdb.kv_get(c, wallet.LAUNCH_KEY)
        away_by = _wallet_away(c, launch["day"], today)
        # "Earns money" per person: off is streak only (rows serve at 0)
        earns = {p["id"]: bool(p["earns"])
                 for p in fdb.list_people(c, include_inactive=True)}
        d_str = today.isoformat()
        done = {r["chore_id"] for r in fdb.completions_between(c, d_str, d_str)}
        served = {r["chore_id"]: r for r in fdb.day_log(c, d_str)} if done else {}
        rate: dict[int, int] = {}
        out = []
        for r in rows:
            r = dict(r)
            was = served.get(r["chore_id"]) if r["chore_id"] in done else None
            if r.get("locked"):
                pass                     # a kept done row keeps its frozen pay and earner
            elif was is not None and was.get("earner") is not None:
                # A done chore that was priced with the wallet on is SETTLED: it
                # pays its earner (whoever the wall showed it for when it was
                # ticked), as the kind it was and at the price shown then. A
                # later re-plan (an owner back from a trip the same day), a
                # retag or a rate change moves only its streak owner
                # (person_id), never its pay; so a payout that counted it stays
                # right (owner, 2026-11-27, wallet known limits). Unticking
                # unsettles it: the next serve prices it afresh. A row with no
                # earner was frozen while the wallet was off (or before this
                # rule): it is priced below like any other.
                # The same goes for the Earns money switch: a chore ticked
                # while earning keeps its pay after the switch goes off, and
                # one ticked while streak only stays unpaid after it is back on.
                r["kind"] = was.get("kind") or "house"
                r["earner"] = was["earner"]
                r["cents"] = int(was.get("cents") or 0) if r["kind"] == "house" else 0
                r["earning"] = 0 if was.get("earning", 1) in (0, False) else 1
            elif (r.get("kind") or "house") == "house" and not earns.get(
                    r["person_id"], False):
                r["cents"] = 0            # streak only: counts, never pays
                r["earning"] = 0
                r["earner"] = r["person_id"]
            elif (r.get("kind") or "house") == "house":
                pid = r["person_id"]
                if pid not in rate:
                    run = wallet.person_run(c, pid, today, settings, launch,
                                            away_by.get(pid, set()))
                    rate[pid] = _rate_today(run, today, settings)
                r["cents"] = rate[pid]
                r["earning"] = 1
                r["earner"] = pid        # priced: who this row pays if done now
            else:
                r["cents"] = 0
                r["earning"] = 1
                r["earner"] = r["person_id"]
            out.append(r)
        return out, True
    except Exception:
        log.error("chore wallet could not price today's chores", exc_info=True)
        return rows, False


def _rate_today(run, today: dt.date, settings: dict) -> int:
    """Pay per chore today: the rate for the streak as it stood at the end of
    yesterday (wallet.cents_for_today, from a run already in hand)."""
    kept_today = run.outcomes.get(today.isoformat()) == rewards.KEPT
    return rewards.rate_for(run.streak - (1 if kept_today else 0), settings)


def _reward_block(c, pid: int, today: dt.date, settings: dict, launch: dict,
                  away: set) -> dict:
    """One card's wallet. ``earns`` False is streak only: the streak, goal
    and shields as usual, and no money at all (no rate, bonus or bonus
    progress; the card draws no wallet line). ``wallet_cents`` is still the
    true balance: what was earned before the switch went off stays owed,
    and Settings shows it until it is paid."""
    earns = wallet.earns(c, pid)
    run = wallet.person_run(c, pid, today, settings, launch, away)
    if earns:
        wallet.award(c, pid, run, settings, today)
    ts = today.isoformat()
    kept_today = run.outcomes.get(ts) == rewards.KEPT
    bonus_today = sum(r["cents"] for r in fdb.ledger_for(c, pid)
                      if r["kind"] == "bonus" and r["day"] == ts) if earns else 0
    # streak only: the goal is the plain one, never a bonus or pay step
    goal_settings = settings if earns else {**settings, "bonus_on": False,
                                             "levels_on": False}
    nb = (rewards.next_milestone(run.streak, settings.get("milestones") or [])
          if settings.get("bonus_on") and earns else None)
    nl = rewards.next_level(run.streak, settings) if earns else None
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    return {
        "earns": earns,
        "streak": run.streak,
        "goal": rewards.goal_for(run.streak, goal_settings),
        "shields": run.shields,
        "shield_max": int(settings["shield_max"]) if settings.get("shields_on") else 0,
        "wallet_cents": wallet.balance_cents(c, pid, launch["day"]),
        "rate_cents": rewards.rate_for(run.streak - (1 if kept_today else 0),
                                       settings) if earns else 0,
        "next_bonus": {"at": nb[0], "cents": nb[1]} if nb else None,
        "next_level": {"at": nl[0], "cents": nl[1]} if nl else None,
        "bonus_today_cents": bonus_today,
        "outcomes7": [run.outcomes.get(ds, "before") for ds in days],
    }


def _add_rewards(c, plan: list, rows: list, today: dt.date,
                 priced_ok: bool = True) -> None:
    """Add each child's wallet to today's plan, and what each row pays. A
    failure says so on the card ({"error": True}: "Wallet unavailable")
    instead of failing the wall or quietly showing no wallet."""
    failed = {"error": True}
    try:
        if not priced_ok:
            raise RuntimeError("today's chores could not be priced")
        settings, launch = reward_settings(c), fdb.kv_get(c, wallet.LAUNCH_KEY)
        away_by = _wallet_away(c, launch["day"], today)
    except Exception:
        log.error("chore wallet unavailable; every card says so", exc_info=True)
        for entry in plan:
            entry["reward"] = dict(failed)
        return
    by_id = {r["chore_id"]: r for r in rows}
    for entry in plan:
        pid = entry["person"]["id"]
        try:
            block = _reward_block(c, pid, today, settings, launch,
                                  away_by.get(pid, set()))
        except Exception:
            log.error("chore wallet failed for person %s; card says so",
                      pid, exc_info=True)
            entry["reward"] = dict(failed)
            continue
        entry["reward"] = block
        for ch in entry["chores"]:
            r = by_id.get(ch["id"], {})
            ch["kind"] = r.get("kind") or "house"
            # a chore settled to someone else (the backup who did it before
            # this owner came back) shows no pay on this card: it isn't theirs
            mine = (r.get("earner") or pid) == pid
            ch["cents"] = int(r.get("cents") or 0) if mine else 0


def _backup_status(last_success, now, stale_s, remote=None):
    """Pure: (last-success datetime or None, now, threshold secs) -> the /api/hub
    `backup` block. 'known' is False before any heartbeat exists, so a fresh
    deploy shows a muted 'unknown', never a false alarm.

    `remote` is None when no off-box mirror is configured, else
    {"ok": last attempt succeeded, "last_ok": datetime of the last good copy or
    None}. A failing last attempt, or no good copy within the threshold, marks
    the remote unhealthy: the local snapshot alone is not a healthy backup when
    the operator asked for an off-box one."""
    remote_block = None
    if remote is not None:
        last_ok = remote.get("last_ok")
        r_age = int((now - last_ok).total_seconds()) if last_ok else None
        remote_block = {
            "ok": bool(remote.get("ok")),
            "last_ok": last_ok.isoformat() if last_ok else None,
            "age_s": r_age,
            "stale": r_age is None or r_age > stale_s,
            "failing": not remote.get("ok"),
        }
    if last_success is None:
        return {"known": False, "last_success": None, "age_s": None,
                "stale": False, "threshold_s": stale_s, "remote": remote_block}
    age = int((now - last_success).total_seconds())
    return {"known": True, "last_success": last_success.isoformat(), "age_s": age,
            "stale": age > stale_s, "threshold_s": stale_s,
            "remote": remote_block}


def _heartbeat_ts(value):
    """An ISO timestamp from the heartbeat, as an aware UTC datetime, or None
    when missing or unparseable (fails safe to 'unknown', never false-fresh)."""
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _build_backup(conn, now=None, stale_s=BACKUP_STALE_S):
    """Read the 'backup_status' heartbeat the backup script writes into hub.db on
    every successful snapshot ({"at": ISO, ...}) and derive staleness. family-hub
    `kv` has no updated_at column, so the timestamp lives in the value. A stale
    heartbeat also catches 'backups stopped running at all'. When an off-box
    mirror is configured the script also records its outcome (remote_ok,
    remote_ok_at); the key's presence is what says a mirror is configured."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = fdb.kv_get(conn, "backup_status")
    if not isinstance(rec, dict):
        return _backup_status(None, now, stale_s)
    remote = None
    if "remote_ok" in rec:
        remote = {"ok": rec.get("remote_ok") is True,
                  "last_ok": _heartbeat_ts(rec.get("remote_ok_at"))}
    return _backup_status(_heartbeat_ts(rec.get("at")), now, stale_s, remote)


# --- dimming: the household dim hours and "nobody is home" ---------------
#
# The wall dims itself (body.is-night) inside the household dim hours, and,
# with the away_dim switch on, while Home Assistant says nobody is home. The
# hours are one kv record every wall and phone reads from /api/hub. Who is
# home comes from a slow background watcher (every PRESENCE_WATCH_S) that
# keeps the last answer here; /api/hub only reads it, so a slow Home
# Assistant can never slow the wall. Every failure leaves the wall bright.

PRESENCE_WATCH_S = dimming.WATCH_S
# The last answer from Home Assistant ({"ok", "home"} or {"ok": False,
# "error", "auth"}) and when it came (wall clock). None until the first read
# after the switch goes on; cleared whenever it goes off.
_presence_snap: dict | None = None
_presence_at: float | None = None
# When Home Assistant last gave a readable answer, for /health/full.
_presence_last_ok: float | None = None
# One warning per outage, not one per minute; re-armed on recovery.
_presence_warned = False
# Set by the away_dim switch so the watcher asks at once instead of waiting
# out its minute (the Settings line would read "Checking" until then).
_presence_kick = False
_presence_watch_task: "asyncio.Task | None" = None
# Nobody-home reads in a row: one zero can be a blip, so the wall dims on the
# second (dimming.away_block reads the snapshot's `confirmed`).
_presence_zero_streak = 0
# Bumped whenever the answer is dropped (the switch flipped), so a read that
# started before the flip can't write its answer after it.
_presence_gen = 0


def _presence_watch_enabled() -> bool:
    """The watcher runs where the calendar sync would (not in DEMO, not under
    DISABLE_SYNC) and only on a hub that has a Home Assistant. It still asks
    HA nothing while the switch is off (see _presence_tick)."""
    return _sync_enabled() and bool(fintegrations.presence_base(cfg))


def _presence_forget() -> None:
    """Drop the last answer, so turning the switch back on never revives an
    old "nobody is home", and start over: the outage warning re-arms, the
    nobody-home count restarts, and any read still in flight is void."""
    global _presence_snap, _presence_at, _presence_warned, \
        _presence_zero_streak, _presence_gen
    _presence_snap, _presence_at = None, None
    _presence_warned = False
    _presence_zero_streak = 0
    _presence_gen += 1


def _presence_watching() -> bool:
    """The background watcher is alive (started and not exited)."""
    t = _presence_watch_task
    return t is not None and not t.done()


def _away_on_now() -> bool:
    return _integration_on(_db(), "away_dim")


async def _presence_tick() -> None:
    """One read of who is home. Off, or no token: ask nothing, keep nothing."""
    global _presence_snap, _presence_at, _presence_last_ok, _presence_warned, \
        _presence_zero_streak
    gen = _presence_gen
    if not await asyncio.to_thread(_away_on_now):
        _presence_forget()
        return
    token = fintegrations.ha_token(os.environ)
    if not token:
        _presence_forget()
        return
    people = list((getattr(cfg, "presence", None) or {}).get("people") or [])
    snap = await dimming.read_presence(
        _http, fintegrations.presence_base(cfg), token, people)
    if gen != _presence_gen:
        return            # the switch flipped while we asked: not our answer
    now = time.time()
    if snap.get("ok") and snap.get("home") == 0:
        _presence_zero_streak += 1
        snap = dict(snap, confirmed=_presence_zero_streak >= 2)
    else:
        _presence_zero_streak = 0
    _presence_snap, _presence_at = snap, now
    if snap.get("ok"):
        _presence_last_ok = now
        if _presence_warned:
            _presence_warned = False
            log.warning("away dimming: Home Assistant answers again")
    elif not _presence_warned:
        _presence_warned = True
        log.warning("away dimming: Home Assistant could not be read (%s); the "
                    "wall will not dim for nobody home until it can",
                    snap.get("error"))


async def presence_watch_loop() -> None:
    """Ask every PRESENCE_WATCH_S, armored like the laundry loop: a crashing
    tick is logged and the loop carries on; only cancel stops it. The wait is
    sliced so the switch going on is answered within seconds."""
    global _presence_kick
    while True:
        try:
            await _presence_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("away dimming: presence tick crashed; continuing")
        waited = 0.0
        while waited < PRESENCE_WATCH_S and not _presence_kick:
            await asyncio.sleep(2.0)
            waited += 2.0
        _presence_kick = False


def _away_row_status() -> str | None:
    """The away_dim settings row: 'needs_auth' with no token or a refused one,
    'error' while Home Assistant can't be read, else None. DEMO has no Home
    Assistant and nothing to complain about."""
    if DEMO:
        return None
    if not fintegrations.ha_token(os.environ):
        return "needs_auth"
    st = dimming.away_block(enabled=True, token=True, snap=_presence_snap,
                            at=_presence_at, now=time.time(),
                            watching=_presence_watching())["state"]
    return {"needs_auth": "needs_auth", "unreadable": "error",
            "stopped": "error"}.get(st)


def _dim_block(c, istate: dict) -> dict:
    """/api/hub's `dim`: {"hours": the household dim hours, "away": the
    nobody-home answer, or None on a hub with no Home Assistant}."""
    try:
        hours = dimming.clean_hours(fdb.kv_get(c, dimming.KV_KEY))
    except Exception:
        log.error("dim hours unreadable; serving the default", exc_info=True)
        hours = dict(dimming.DIM_DEFAULT)
    if not any(i["id"] == "away_dim" for i in istate["list"]):
        return {"hours": hours, "away": None}
    on = "away_dim" in istate["enabled_ids"]
    if DEMO:
        return {"hours": hours, "away": dimming.demo_away(enabled=on)}
    return {"hours": hours, "away": dimming.away_block(
        enabled=on, token=bool(fintegrations.ha_token(os.environ)),
        snap=_presence_snap, at=_presence_at, now=time.time(),
        watching=_presence_watching())}


@app.put("/api/dim-hours")
def dim_hours_put(body: dict = Body(...)):
    """Set the household dim hours: any of {"on": bool, "start": "HH:MM",
    "end": "HH:MM"}. Every wall and phone picks it up on its next poll."""
    c = _db()
    try:
        hours = dimming.merge_hours(fdb.kv_get(c, dimming.KV_KEY), body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    fdb.kv_set(c, dimming.KV_KEY, hours)
    return hours


@app.get("/api/hub")
def hub():
    with _available_once():
        return _hub_payload()


def _hub_payload() -> dict:
    c = _db()
    today = _today()
    # same fails-soft philosophy as _links(): a single bad todos row (or any
    # other unexpected failure in the group/read path) must not 500 the whole
    # wall over one broken bucket. GET /api/todos keeps NO such wrapper: a
    # 500 there is visible and correct, since it's a direct read of that data.
    todos_ok = True
    try:
        todos_block = tdlogic.group(fdb.list_todos(c))
    except Exception:
        # A real bug (bad row, read failure) — not an expected "empty list", so
        # log at ERROR. Empty buckets look identical to "nothing to do", which
        # would tell the family they're caught up when the list is actually
        # intact but unrenderable; ship a todos_ok flag so the wall can show a
        # "couldn't load" note instead of a reassuring empty card.
        log.error("todos block failed; serving empty buckets", exc_info=True)
        todos_block = {b: [] for b in tdlogic.BUCKETS}
        todos_ok = False
    istate = _integrations_state(c)
    # iCloud Reminders, grouped; empty unless the CalDAV integration is available
    # AND enabled. A separate surface from the local To-Dos; two-way when the
    # operator has enabled writes (readonly=False).
    caldav_on = "icloud_caldav" in istate["enabled_ids"]
    reminders_block = (remlogic.group(_visible_reminders(c), today, TZ)
                       if caldav_on else {b: [] for b in remlogic.BUCKETS})
    people, away_ok = _people_day(c, today)
    # Live trips for the Chores card banner. Fail-soft like the blocks above:
    # trips_ok False makes the wall say so instead of hiding a trip.
    try:
        trips, trips_ok = _trip_rows(c, today), True
    except Exception:
        log.error("trip list failed; serving none", exc_info=True)
        trips, trips_ok = [], False
    # Backup health for the header badge — fails-soft like the todos block above:
    # a read error must not 500 the whole wall over a status indicator.
    try:
        backup_block = _build_backup(c)
    except Exception:
        log.error("backup status read failed; serving unknown", exc_info=True)
        backup_block = _backup_status(None, dt.datetime.now(dt.timezone.utc), BACKUP_STALE_S)
    wallet_rules = None
    try:
        if rewards_active(c):
            s = reward_settings(c)
            wallet_rules = {k: s[k] for k in (
                "cents_per_chore", "bonus_on", "milestones", "levels_on", "levels",
                "shields_on", "shield_every", "shield_max", "fix_cutoff")}
    except Exception:
        log.error("chore wallet rules unreadable for the help text", exc_info=True)
        wallet_rules = {"error": True}   # the help says so, never guesses
    extra = {"wallet": wallet_rules} if wallet_rules else {}
    return {
        **extra,
        "date": today.isoformat(),
        "people": people,
        # Mirrors todos_ok: False when the away overlay build threw and the wall
        # is rendering with nobody marked away, so it can show a small note
        # instead of silently presenting a genuinely-away person as present.
        "away_ok": away_ok,
        "trips": trips,
        "trips_ok": trips_ok,
        "todos": todos_block,
        "todos_ok": todos_ok,
        "reminders": reminders_block,
        # Two-way state for the To-Do surface when it's showing iCloud: whether
        # writes are on, and the enabled reminder lists a wall-added reminder can
        # target (empty => read-only or no lists, so the add control hides).
        "reminders_writable": caldav_on and _reminders_writable(c),
        "reminder_lists": _reminder_lists(c) if caldav_on else [],
        # Which source backs the To-Do surface: 'local' (the built-in whiteboard,
        # default) or 'icloud' (the iCloud Reminders list, two-way). The frontend
        # renders the todos block or the reminders block accordingly.
        "todo_source": fdb.kv_get(c, "todo_source") or "local",
        "calendar": _calendar_block(c, today, 14),
        "links": _links(istate["enabled_ids"]),
        # The wall's settings menu reads this; tiles for disabled integrations
        # (weather/climate/cameras) are hidden client-side from the enabled flags.
        "integrations": istate["list"],
        # A deploy-changing token: the wall reloads itself when it changes, so a
        # baked frontend update reaches the kiosk without a manual refresh.
        "build": BUILD,
        # Backup-health for the header badge: {known, last_success, age_s, stale,
        # threshold_s}. From the heartbeat the backup script writes on success.
        "backup": backup_block,
        # House-default display theme (or None). The wall/admin stamp it live
        # on a fresh device with no localStorage override; None => the shipped
        # grey/green/none stays. Never persisted client-side.
        "theme": cfg.theme,
        # When this wall dims: the household dim hours and, where a Home
        # Assistant is configured, whether nobody is home (_dim_block).
        "dim": _dim_block(c, istate),
    }


@app.get("/api/chores/day")
def chores_day(date: str):
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "bad date")
    if abs((d - _today()).days) > 366:
        raise HTTPException(422, "date out of range")
    c = _db()
    people, away_ok = _people_day(c, d)
    # Same degraded-state flag the hub payload carries: without it the day
    # browser showed an away person as present, with a full chore list and no
    # note, whenever the overlay build failed.
    return {"date": d.isoformat(), "people": people, "away_ok": away_ok}


# --- chores completion ----------------------------------------------------

class CompleteBody(BaseModel):
    date: str | None = None
    person_id: int | None = None


@app.post("/api/chores/{chore_id}/complete")
def complete(chore_id: int, request: Request, body: CompleteBody | None = None):
    c = _db()
    body = body or CompleteBody()
    date_str = body.date or _today().isoformat()
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "bad date")
    today = _today()
    if abs((d - today).days) > 366:
        raise HTTPException(422, "date out of range")
    if d > today:
        # The wall shows future days read-only; a future check-off would make
        # that day start already done (review, 2026-09-22).
        raise HTTPException(422, "can't check off a day that hasn't come yet")
    _guard_old_day(c, d, request)
    person_id = body.person_id
    if d < today:
        # Frozen day: the occurrence log is the truth about what occurred and
        # who was assigned — the chore may since have been edited or even
        # deleted, and its frozen rows must stay toggleable.
        row = fdb.log_row(c, chore_id, date_str)
        if row is None:
            raise HTTPException(422, "chore did not occur on that date")
        if person_id is None:
            person_id = row["person_id"]
    else:
        chore = _chore_row(c, chore_id)
        if not chlogic.occurs(chore, d):
            raise HTTPException(422, "chore does not occur on that date")
        if person_id is None:
            # Resolve the assignee the SAME way the wall did (plan_rows +
            # _people_day), applying the away overlay -- otherwise a backup
            # tapping a covering FIXED chore (client sends no person_id) would
            # be credited to the away owner via assignee_id's fixed_person_id,
            # silently breaking the backup's streak once the day ages. The row
            # plan_rows produces already carries the backup as person_id.
            _, away_view, away_ok = _away_view(c, d)
            if not away_ok:
                # The overlay build failed, so we cannot tell who owns this
                # chore today. Reading the wall is allowed to degrade (it shows
                # a note); WRITING a completion under the wrong person is not --
                # it would credit the away owner and break the real person's
                # streak. Refuse and let the tap be retried. A client that
                # supplies person_id explicitly is unaffected.
                raise HTTPException(503, "away status unavailable; retry")
            rows = chlogic.plan_rows(fdb.list_chores(c), fdb.list_people(c),
                                     d, away_view)
            row = next((r for r in rows if r["chore_id"] == chore_id), None)
            if row is None:
                # No resolvable assignee (e.g. away owner with no available
                # backup -> the chore paused for the day).
                raise HTTPException(422, "no resolvable assignee")
            person_id = row["person_id"]
    # Validate the (possibly client-supplied) person_id against a real person
    # before writing, so a malformed request can't record an invisible orphan
    # completion. _person_row raises 404 for an unknown id. (Fresh DBs also
    # enforce this via a FK; this gives a clean error on any DB.)
    _person_row(c, person_id)
    fdb.set_completion(c, chore_id, date_str, person_id)
    # Reflect onto the mirrored iCloud reminder (no-op if not mirrored, or if
    # the ledger row is stale — see push_completion's expected_person_id).
    chore_mirror.push_completion(c, chore_id, date_str, True,
                                 expected_person_id=person_id)
    return {"ok": True}


def _resolved_owner(c, chore_id: int, date_str: str) -> int | None:
    """Who the wall currently shows this occurrence for — the frozen log for a
    past day, else the live plan under the away overlay. None when it can't be
    resolved (no such occurrence, a broken overlay); callers treat that as "no
    expectation" rather than failing. Never raises."""
    try:
        d = dt.date.fromisoformat(date_str)
        if d < _today():
            row = fdb.log_row(c, chore_id, date_str)
            return row["person_id"] if row else None
        if d == _today() and fdb.completion_exists(c, chore_id, date_str):
            # a chore done today is kept as it was served (_keep_done_rows)
            row = fdb.log_row(c, chore_id, date_str)
            if row is not None:
                return row["person_id"]
        _, away_view, _ = _away_view(c, d)
        rows = chlogic.plan_rows(fdb.list_chores(c), fdb.list_people(c), d,
                                 away_view)
        row = next((r for r in rows if r["chore_id"] == chore_id), None)
        return row["person_id"] if row else None
    except Exception:
        log.warning("could not resolve the owner of chore %s on %s",
                    chore_id, date_str, exc_info=True)
        return None


@app.delete("/api/chores/{chore_id}/complete")
def uncomplete(chore_id: int, request: Request, date: str | None = None):
    c = _db()
    date_str = date or _today().isoformat()
    # Same checks as complete(): the wall sends the day it is showing.
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "bad date")
    if abs((d - _today()).days) > 366:
        raise HTTPException(422, "date out of range")
    _guard_old_day(c, d, request)
    if d == _today() and fdb.completion_exists(c, chore_id, date_str):
        _, away_view, away_ok = _away_view(c, d)
        if not away_ok:
            # can't tell whether this row is a locked one; same refusal
            # complete() gives when the away list can't be read
            raise HTTPException(503, "away status unavailable; retry")
        planned = {r["chore_id"] for r in chlogic.plan_rows(
            fdb.list_chores(c), fdb.list_people(c), d, away_view)}
        if chore_id in _locked_done_ids(c, date_str, planned):
            # a finished chore kept on the wall after a pause or an edit
            # (_keep_done_rows): unticking it would lose it for the day
            raise HTTPException(409, "this chore was finished before it came off today's plan; it stays done")
    # Resolve the current owner BEFORE clearing, so the reopen can't be pushed
    # onto a mirror ledger row that still names the other person (M3).
    owner = _resolved_owner(c, chore_id, date_str)
    fdb.clear_completion(c, chore_id, date_str)
    # Reopen the mirrored iCloud reminder too (no-op if not mirrored).
    chore_mirror.push_completion(c, chore_id, date_str, False,
                                 expected_person_id=owner)
    return {"ok": True}


# --- chore wallet: settings, on/off, the old-day lock ----------------------

REWARD_DEFAULTS = {
    "cents_per_chore": 30,
    "bonus_on": True, "milestones": [[100, 10000]],
    "levels_on": False, "levels": [[25, 35], [50, 40], [100, 45]],
    "shields_on": True, "shield_every": 20, "shield_max": 2,
    "fix_cutoff": wallet.FIX_CUTOFF_DEFAULT,
}


def reward_settings(c) -> dict:
    return {**REWARD_DEFAULTS, **(fdb.kv_get(c, "rewards.settings") or {})}


def rewards_active(c) -> bool:
    """The wallet is on: its feature switch is on and it has been turned on
    (launched). A parent PIN is optional (the owner, 2026-11-27: "I don't
    even need the pin"). Off, the wall is exactly as before."""
    return (_integration_on(c, "rewards")
            and bool(fdb.kv_get(c, wallet.LAUNCH_KEY)))


def _edit_allowed(d: dt.date, now: dt.datetime, cutoff: str) -> bool:
    """Today, or yesterday before the morning cutoff (house time)."""
    if d == now.date():
        return True
    hh, mm = (int(x) for x in cutoff.split(":"))
    return (d == now.date() - dt.timedelta(days=1)
            and (now.hour, now.minute) < (hh, mm))


def _guard_old_day(c, d: dt.date, request: Request) -> None:
    """With the wallet on, ticking or unticking an older day changes money,
    so it needs the parent PIN. Off, the old rules stand."""
    if (d < _today() and rewards_active(c) and not _edit_allowed(
            d, _now_local(), reward_settings(c)["fix_cutoff"])):
        _pin_gate(c, request.headers.get(PIN_HEADER), _pin_source(request))


def _guard_paid_today(c, cid: int, request: Request) -> None:
    """A chore already done today, and paid, drops out of today's pay if it
    is deleted, turned off or made one-time: with the wallet on, that needs
    the parent PIN. A chore that earned nothing today needs none."""
    if not rewards_active(c):
        return
    today = _today().isoformat()
    row = fdb.log_row(c, cid, today)
    if (row and int(row.get("cents") or 0) > 0
            and fdb.completion_exists(c, cid, today)):
        require_parent(request)


def _fix_floor(c) -> dt.date:
    """The oldest day anyone may still change without the PIN."""
    now = _now_local()
    y = now.date() - dt.timedelta(days=1)
    return y if _edit_allowed(y, now, reward_settings(c)["fix_cutoff"]) else now.date()


def _away_days(rows, lo: dt.date, hi: dt.date) -> set:
    out = set()
    for r in rows:
        a = max(lo, dt.date.fromisoformat(r["start_date"]))
        b = hi if not r.get("end_date") else min(hi, dt.date.fromisoformat(r["end_date"]))
        while a <= b:
            out.add((r["person_id"], a.isoformat()))
            a += dt.timedelta(days=1)
    return out


def _guard_away(c, request: Request, *, inserts=(), updates=(), deletes=()) -> None:
    """An away change that alters which OLD days (before the fix window,
    since launch) count as away rewrites a wallet streak: with the wallet on,
    it needs the parent PIN. Changes to today and later never do."""
    if not rewards_active(c):
        return
    lo = dt.date.fromisoformat(fdb.kv_get(c, wallet.LAUNCH_KEY)["day"])
    hi = _fix_floor(c) - dt.timedelta(days=1)
    if hi < lo:
        return
    rows = {r["id"]: dict(r) for r in fdb.list_away_periods(c)}
    before = _away_days(rows.values(), lo, hi)
    for i in deletes:
        rows.pop(i, None)
    for i, fields in updates:
        if i in rows:
            rows[i].update(fields)
    after = list(rows.values()) + [dict(r) for r in inserts]
    if _away_days(after, lo, hi) != before:
        _pin_gate(c, request.headers.get(PIN_HEADER), _pin_source(request))


# --- chore wallet: parent PIN ---------------------------------------------

PIN_HEADER = "X-Parent-Pin"


def _pin_source(request: Request | None) -> str:
    """Which screen a PIN try came from, so a pause on one screen (the
    kitchen wall) never locks another (a parent's phone)."""
    return (request.client.host if request is not None and request.client else "")


def _pin_gate(c, pin, source: str = "") -> None:
    """Raise the PIN refusal for ``pin`` unless it is right: 403 required (no
    PIN sent), wrong, key_missing; 429 locked with the wait in seconds. With
    no PIN set, the PIN is off and every action goes through."""
    if not parent_pin.is_set(c):
        return
    if parent_pin.key_missing(c):
        # fail closed, and say why: no PIN can be checked without pin.key
        raise HTTPException(403, {"pin": "key_missing"})
    ok, wait = parent_pin.check(c, pin, _now_local(), source=source)
    if wait:
        raise HTTPException(429, {"pin": "locked", "wait_s": wait})
    if ok:
        return
    raise HTTPException(403, {"pin": "required" if not pin else "wrong"})


def require_parent(request: Request) -> None:
    _pin_gate(_db(), request.headers.get(PIN_HEADER), _pin_source(request))


class PinBody(BaseModel):
    pin: str
    current: str | None = None


@app.post("/api/rewards/pin", status_code=204)
def rewards_set_pin(body: PinBody, request: Request):
    c = _db()
    if parent_pin.is_set(c):
        _pin_gate(c, body.current, _pin_source(request))
    try:
        parent_pin.set_pin(c, body.pin, current=body.current)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(status_code=204)


@app.delete("/api/rewards/pin", status_code=204)
def rewards_remove_pin(request: Request):
    """Turn the PIN off (it is optional). Needs the PIN itself."""
    c = _db()
    require_parent(request)
    parent_pin.clear(c)
    return Response(status_code=204)


@app.post("/api/rewards/pin/check", status_code=204)
def rewards_check_pin(request: Request):
    require_parent(request)
    return Response(status_code=204)


# --- chore wallet: settings, turning it on, payouts ------------------------

_REWARD_INT_RANGES = {"cents_per_chore": (0, 500), "shield_every": (1, 365),
                      "shield_max": (0, 5)}
_REWARD_BOOLS = {"bonus_on", "levels_on", "shields_on"}


def _check_steps(name: str, v, max_cents: int) -> list:
    if not isinstance(v, list) or len(v) > 10:
        raise HTTPException(422, f"{name} must be a list of up to 10 steps")
    out, last = [], 0
    for step in v:
        if (not isinstance(step, list) or len(step) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool)
                           for x in step)):
            raise HTTPException(422, f"each {name} step is [days, cents]")
        days, cents = step
        if not 1 <= days <= 1000 or not 0 <= cents <= max_cents or days <= last:
            raise HTTPException(422, f"{name}: days 1 to 1000 in order, "
                                     f"cents 0 to {max_cents}")
        last = days
        out.append([days, cents])
    return out


def _validated_reward_settings(c, patch: dict) -> dict:
    unknown = set(patch) - set(REWARD_DEFAULTS)
    if unknown:
        raise HTTPException(422, f"unknown settings: {sorted(unknown)}")
    merged = {**reward_settings(c), **patch}
    for k, (lo, hi) in _REWARD_INT_RANGES.items():
        v = merged[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise HTTPException(422, f"{k} must be a whole number {lo} to {hi}")
    for k in _REWARD_BOOLS:
        if not isinstance(merged[k], bool):
            raise HTTPException(422, f"{k} must be true or false")
    merged["milestones"] = _check_steps("milestones", merged["milestones"], 100000)
    merged["levels"] = _check_steps("levels", merged["levels"], 500)
    cut = merged["fix_cutoff"]
    if not (isinstance(cut, str) and re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", cut)):
        raise HTTPException(422, "fix_cutoff must be HH:MM")
    return merged


def _summary_streak(c, pid: int, today: dt.date, launch: dict, away_by) -> int | None:
    """The streak for a wallet summary, or None (logged) when it can't be
    worked out: the summary's money still shows, only the streak drops."""
    try:
        if away_by is None:
            raise RuntimeError("away days could not be read")
        return wallet.person_run(c, pid, today, reward_settings(c), launch,
                                 away_by.get(pid, set())).streak
    except Exception:
        log.error("wallet summary: no streak for person %s", pid, exc_info=True)
        return None


def _wallet_people(c, today: dt.date) -> list[dict]:
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    away_by = None
    if launch:
        try:
            away_by = _wallet_away(c, launch["day"], today)
        except Exception:
            log.error("wallet summary: away days unreadable", exc_info=True)
    out = []
    for p in fdb.list_people(c, include_inactive=True):
        item = {"id": p["id"], "name": p["name"], "color": p["color"],
                "active": bool(p["active"]), "earns": bool(p["earns"]),
                "pace": wallet.pace(c, p["id"], today)}
        if launch and item["earns"]:
            # what came in since the last payout. The balance shown is its
            # total, read once, so the two can never disagree.
            item["summary"] = wallet.summary(
                c, p["id"], launch["day"],
                _summary_streak(c, p["id"], today, launch, away_by))
            item["balance_cents"] = item["summary"]["total_cents"]
            item["history"] = wallet.history(c, p["id"], launch["day"])
        elif launch:
            # streak only: no summary. Any balance from before the switch
            # went off is still owed, and shows until it is paid.
            item["balance_cents"] = wallet.balance_cents(c, p["id"], launch["day"])
            item["history"] = wallet.history(c, p["id"], launch["day"])
        else:
            item["balance_cents"], item["history"] = 0, {"weeks": [], "ledger": []}
        if not item["active"] and not item["balance_cents"] \
                and not item["history"]["ledger"] and not item["history"]["weeks"]:
            continue                      # gone, and nothing left to settle
        out.append(item)
    return out


def _rewards_state(c) -> dict:
    today = _today()
    return {"settings": reward_settings(c),
            "today": today.isoformat(),     # the house day, for "Since today"
            "pin_set": parent_pin.is_set(c),
            "pin_key_missing": parent_pin.key_missing(c),
            "feature_on": _integration_on(c, "rewards"),
            "launch": fdb.kv_get(c, wallet.LAUNCH_KEY),
            "active": rewards_active(c),
            "people": _wallet_people(c, today)}


@app.get("/api/rewards")
def rewards_get():
    """Read-only: settings, balances and history (the wall cards already
    show each balance). Every change below needs the parent PIN when one is set."""
    return _rewards_state(_db())


@app.put("/api/rewards/settings")
def rewards_put_settings(patch: dict, request: Request):
    """Amounts apply from today on: earlier days keep the pay they were
    frozen with, the shield rule they were judged by, and a milestone a run
    has already passed is never paid by adding it now."""
    c = _db()
    require_parent(request)
    old = reward_settings(c)
    new = _validated_reward_settings(c, patch)
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    today = _today()
    if launch:
        if any(old[k] != new[k] for k in ("shields_on", "shield_every", "shield_max")):
            wallet.record_shield_rule(c, new, launch, today)
        if old["bonus_on"] != new["bonus_on"] or old["milestones"] != new["milestones"]:
            away_by = _wallet_away(c, launch["day"], today)
            for p in fdb.list_people(c):
                run = wallet.person_run(c, p["id"], today, old, launch,
                                        away_by.get(p["id"], set()))
                wallet.record_milestone_floor(c, p["id"], run)
    fdb.kv_set(c, "rewards.settings", new)
    return _rewards_state(c)


@app.post("/api/rewards/projection")
def rewards_projection(draft: dict):
    """What the draft amounts would cost, before they are saved. Read-only."""
    c = _db()
    settings = _validated_reward_settings(c, draft)
    today = _today()
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    away_by = _wallet_away(c, launch["day"], today) if launch else {}
    kids, partial = [], []
    for p in fdb.list_people(c):
        if not p["earns"]:
            continue                    # streak only: costs nothing
        try:
            pace = wallet.pace(c, p["id"], today)
            streak = (wallet.person_run(c, p["id"], today, settings, launch,
                                        away_by.get(p["id"], set())).streak
                      if launch else 0)
        except Exception:
            log.error("projection: could not count person %s", p["id"],
                      exc_info=True)
            partial.append(p["id"])     # left out, and the card says who
            continue
        kids.append({"id": p["id"], **pace, "streak": streak})
    return {**rewards.project(kids, settings), "partial": partial}


class EarnsBody(BaseModel):
    earns: StrictBool


@app.put("/api/rewards/people/{pid}/earns")
def rewards_set_earns(pid: int, body: EarnsBody, request: Request):
    """The per-person "Earns money" switch, a parent action like amounts and
    payouts. Off is streak only from now on: the run keeps counting, their
    chores add no money and no bonus pays. What they already earned stays
    owed (Settings shows it until Paid), and a chore done before the switch
    keeps its pay. Back on, chores from now pay, and a milestone the run
    passed while off never does. Today is re-priced at once, so a tick
    before the wall's next refresh can't land at the old price."""
    c = _db()
    require_parent(request)
    was = fdb.get_person_earns(c, pid)
    if was is None:
        raise HTTPException(404, "no such person")
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    today = _today()
    if body.earns != was and launch:
        away_by = _wallet_away(c, launch["day"], today)
        settings = reward_settings(c)
        run = wallet.person_run(c, pid, today, settings, launch,
                                away_by.get(pid, set()))
        if body.earns:
            wallet.record_earning_on(c, pid, run)
        else:
            # a milestone reached while still earning is theirs, even if
            # no wall refreshed between the tick and the switch
            wallet.award(c, pid, run, settings, today)
    fdb.set_person_earns(c, pid, body.earns)
    if body.earns != was and rewards_active(c):
        try:
            _people_day(c, today)
        except Exception:
            log.error("earns switch saved, but today could not be re-priced "
                      "now; the wall's next refresh does it", exc_info=True)
    return _rewards_state(c)


@app.post("/api/rewards/enable")
def rewards_enable(request: Request):
    """Turn the wallet on, once: each child's run carries on from the streak
    the wall shows right now (the old count), and money starts at $0."""
    c = _db()
    require_parent(request)
    if not fdb.kv_get(c, wallet.LAUNCH_KEY):
        plan, _ = _people_day(c, _today())
        # The old count includes today once today is all done, and the new
        # rules count launch day again: seed from the count as of yesterday.
        seeds = {e["person"]["id"]: max(0, int(e.get("streak") or 0) - (
                     1 if e.get("total") and e.get("done_count") == e.get("total")
                     else 0))
                 for e in plan}
        wallet.launch(c, _today(), reward_settings(c), seeds)
    return _rewards_state(c)


@app.post("/api/rewards/payout/{pid}")
def rewards_payout(pid: int, request: Request):
    c = _db()
    require_parent(request)
    if not any(p["id"] == pid for p in fdb.list_people(c, include_inactive=True)):
        raise HTTPException(404, "no such person")
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    if not launch:
        raise HTTPException(409, "the chore wallet is not on")
    paid = wallet.pay_out(c, pid, _today())
    return {"paid": paid,
            "balance_cents": wallet.balance_cents(c, pid, launch["day"]) if launch else 0}


# --- todos ----------------------------------------------------------------

class TodoIn(BaseModel):
    title: str
    bucket: str = "now"


class TodoPatch(BaseModel):
    title: str | None = None
    bucket: str | None = None


def _validate_todo(merged: dict) -> None:
    title = (merged.get("title") or "").strip()
    if not (1 <= len(title) <= 120):
        raise HTTPException(422, "title must be 1-120 characters")
    if merged["bucket"] not in tdlogic.BUCKETS:
        raise HTTPException(422, "bucket must be now, soon or later")


def _todo_row(c, tid: int) -> dict:
    row = fdb.get_todo(c, tid)
    if row is None:
        raise HTTPException(404, "unknown todo")
    return row


@app.get("/api/todos")
def todos_list():
    c = _db()
    today = _today()
    rows = fdb.list_todos(c)
    return {"buckets": tdlogic.group(rows),
            "recent_done": tdlogic.recent_done(rows, today)}


@app.post("/api/todos")
def todos_add(t: TodoIn):
    c = _db()
    merged = t.model_dump()
    _validate_todo(merged)
    tid = fdb.add_todo(c, merged["title"].strip(), merged["bucket"])
    return _todo_row(c, tid)


@app.patch("/api/todos/{tid}")
def todos_patch(tid: int, t: TodoPatch):
    c = _db()
    row = _todo_row(c, tid)
    fields = t.model_dump(exclude_unset=True)
    merged = {**row, **fields}
    _validate_todo(merged)
    if "title" in fields:
        fields["title"] = fields["title"].strip()
    # A move to another list lands at the END of that list (db.move_todo),
    # in the same transaction as any rename; the plain column write would
    # drop it in by creation date instead.
    new_bucket = fields.pop("bucket", None)
    if new_bucket is not None and new_bucket != row["bucket"]:
        fdb.move_todo(c, tid, new_bucket, title=fields.get("title"))
    else:
        fdb.update_todo(c, tid, **fields)
    return _todo_row(c, tid)


class TodoOrderIn(BaseModel):
    bucket: str
    ids: list[int]


@app.post("/api/todo-order")   # not under /api/todos/: see /api/todo-source
def todos_order(body: TodoOrderIn):
    """A drag and drop: put these open items into `bucket` in this order.
    Every id must be a real, open item and appear once; anything else is
    refused before anything is written, never a half-applied shuffle. Open
    items of `bucket` the request didn't name (added on another device
    since this one last looked) keep their order after the named ones."""
    c = _db()
    if body.bucket not in tdlogic.BUCKETS:
        raise HTTPException(422, "bucket must be now, soon or later")
    if not body.ids or len(set(body.ids)) != len(body.ids):
        raise HTTPException(422, "ids must list each item once")
    if len(body.ids) > 1000:      # a list on a wall, not a bulk import
        raise HTTPException(422, "too many items in one move")
    rows = {t["id"]: t for t in fdb.list_todos(c)}
    if any(i not in rows for i in body.ids):
        raise HTTPException(404, "unknown todo")
    if any(rows[i]["done_at"] is not None for i in body.ids):
        raise HTTPException(422, "a checked-off item can't be moved")
    fdb.place_todos(c, body.bucket, body.ids)
    return {"ok": True}


@app.post("/api/todos/{tid}/complete")
def todos_complete(tid: int):
    c = _db()
    _todo_row(c, tid)
    fdb.set_todo_done(c, tid, _today().isoformat())
    return {"ok": True}


@app.delete("/api/todos/{tid}/complete")
def todos_uncomplete(tid: int):
    c = _db()
    _todo_row(c, tid)
    fdb.clear_todo_done(c, tid)
    return {"ok": True}


@app.delete("/api/todos/{tid}")
def todos_delete(tid: int):
    c = _db()
    _todo_row(c, tid)
    fdb.delete_todo(c, tid)
    return {"ok": True}


class TodoSourceIn(BaseModel):
    source: str


@app.patch("/api/todo-source")   # NOT /api/todos/source: that collides with the
def todos_set_source(body: TodoSourceIn):   # /api/todos/{tid:int} route (422s)
    """Choose what backs the To-Do surface: the local whiteboard or iCloud
    Reminders. Local todos are untouched either way (no migration)."""
    if body.source not in ("local", "icloud"):
        raise HTTPException(422, "source must be 'local' or 'icloud'")
    fdb.kv_set(_db(), "todo_source", body.source)
    return {"source": body.source}


# --- backups: the hub's own hourly job (backups.py) ----------------------

# The folder the copies go to. In the container that is /backups, which
# docker-compose.yml mounts from the host's BACKUP_DIR (default ./backups).
# The default only counts when something is mounted there (a folder baked
# into the image would vanish with the container); a path set on purpose
# with BACKUPS_PATH (a hub run outside docker) just has to be writable.
BACKUPS_PATH = os.environ.get("BACKUPS_PATH") or "/backups"
_BACKUPS_PATH_SET = bool(os.environ.get("BACKUPS_PATH"))
# What the card calls that folder: the host path compose mounted, when it
# says (the container's own /backups means nothing to the owner).
BACKUP_DIR_LABEL = os.environ.get("BACKUP_DIR") or BACKUPS_PATH
# The salt and key made from the backup password, beside hub.db (like pin.key).
BACKUP_KEY_PATH = os.environ.get("BACKUP_KEY_PATH", os.path.join(
    os.path.dirname(DB_PATH) or ".", "backup.key"))
# .env, for the settings bundle. compose mounts it read-only at /app/.env
# and points this there; outside docker it is ./.env.
SETTINGS_ENV_PATH = os.environ.get("SETTINGS_ENV_PATH", ".env")
BACKUP_FIRST_S = 300        # first run five minutes after start
BACKUP_EVERY_S = 3600       # then hourly
_BACKUP_PW_KEY = "backups.password_set"   # a password was set once (a lost key is then a problem)
# One run at a time: the hourly task and Back up now share it.
_backup_lock = threading.Lock()
# When the run holding the lock started (monotonic), to spot a hung NAS.
_backup_started_at = None
_backup_stuck_noted = False      # the current stuck run was already recorded
BACKUP_STUCK_S = 20 * 60
BACKUP_STUCK_MSG = ("Backup stuck: a backup has been running for more than 20 "
                    "minutes. Is the NAS hung?")
_backup_last_skip = None         # the last reason an hourly run was skipped (logged once)

_BACKUP_WHY = {
    "demo": "Backups are off in the demo.",
    "switched_off": "Backups are switched off in Features & integrations.",
    "no_folder": "No backup folder is set up.",
    "not_mounted": "No backup folder is mounted into the hub.",
    "not_writable": "The hub can't write to the backup folder.",
    "marker_missing": fbackups.MARKER_MISSING,
    "running": "A backup is already running.",
}


def _backup_sources() -> dict:
    data_dir = os.path.dirname(DB_PATH) or "."
    return {"config.json": CONFIG_PATH, "env": SETTINGS_ENV_PATH,
            "pin.key": parent_pin.KEY_PATH,
            "wyze.env": os.path.join(data_dir, "wyze.env"),
            "token.json": TOKEN_PATH}


def _backup_why_off(c) -> str | None:
    """Why the job can't run on this hub now, or None."""
    if DEMO:
        return "demo"
    if not _integration_on(c, "backups"):
        return "switched_off"
    return fbackups.folder_problem(BACKUPS_PATH, must_be_mount=not _BACKUPS_PATH_SET,
                                   marker=fdb.kv_get(c, fbackups.MARKER_KEY))


def _backup_loop_enabled() -> bool:
    """The hourly task runs where the calendar sync would (not in DEMO, not
    under DISABLE_SYNC). Back up now works either way."""
    return _sync_enabled()


def _backup_run(c) -> tuple[dict, Exception | None]:
    """One backup and its record: (result, the record's failure or None).
    The caller holds _backup_lock. Stamps use house time, like the box script."""
    global _backup_started_at, _backup_stuck_noted
    _backup_started_at, _backup_stuck_noted = time.monotonic(), False
    try:
        result = fbackups.run_once(
            _now_local().replace(tzinfo=None), db_path=DB_PATH, out_dir=BACKUPS_PATH,
            sources=_backup_sources(), key_path=BACKUP_KEY_PATH,
            password_expected=bool(fdb.kv_get(c, _BACKUP_PW_KEY)),
            marker=fdb.kv_get(c, fbackups.MARKER_KEY))
    finally:
        _backup_started_at = None
    try:
        fbackups.record(c, result, dt.datetime.now(dt.timezone.utc), BACKUP_DIR_LABEL)
    except Exception as e:
        # The copy itself may be good. Without the record the wall's badge
        # goes stale and the card shows the old time, so say it loudly here.
        log.error("backups: the run finished but its record could not be written "
                  "(the wall may show the backup as stale)", exc_info=True)
        return result, e
    return result, None


def _backup_note_failure(c, message: str) -> None:
    """Record a run that could not happen (a dropped NAS, a stuck run): the
    card and the heartbeat's in_app_error say so, and `at` stays put so the
    wall's stale badge fires on time."""
    try:
        fbackups.record(c, {"ok": False, "error": message},
                        dt.datetime.now(dt.timezone.utc), BACKUP_DIR_LABEL)
    except Exception:
        log.error("backups: could not record that %s", message, exc_info=True)


def _backup_skip(reason: str | None) -> None:
    """Log once each time the reason the hourly run is skipped changes."""
    global _backup_last_skip
    if reason == _backup_last_skip:
        return
    if reason:
        log.warning("backups: hourly backup skipped: %s", _BACKUP_WHY.get(reason, reason))
    elif _backup_last_skip:
        log.info("backups: hourly backups are running again")
    _backup_last_skip = reason


def _backup_tick() -> dict | None:
    """One scheduled run, or None when it is off or a run is already going."""
    global _backup_stuck_noted
    c = _db()
    why = _backup_why_off(c)
    if why:
        _backup_skip(why)
        if why == "marker_missing":
            _backup_note_failure(c, fbackups.MARKER_MISSING)
        return None
    if not _backup_lock.acquire(blocking=False):
        _backup_skip("running")
        started = _backup_started_at
        if (started is not None and not _backup_stuck_noted
                and time.monotonic() - started > BACKUP_STUCK_S):
            _backup_stuck_noted = True
            log.error("backups: %s", BACKUP_STUCK_MSG)
            _backup_note_failure(c, BACKUP_STUCK_MSG)
        return None
    try:
        _backup_skip(None)
        result = _backup_run(c)[0]
        _restore_test_if_due(c)
        return result
    finally:
        _backup_lock.release()


def _restore_test_run(c) -> dict:
    """One restore test of the newest backup, and its record. The caller
    holds _backup_lock. Only the test's own kv row is written. It reads the
    NAS like a backup does, so a hung read shows as a stuck run too."""
    global _backup_started_at, _backup_stuck_noted
    rec = fdb.kv_get(c, fbackups.STATE_KEY)
    rec = rec if isinstance(rec, dict) else {}
    files = (rec.get("settings") or {}).get("files")
    _backup_started_at, _backup_stuck_noted = time.monotonic(), False
    try:
        result = fbackups.restore_test(
            BACKUPS_PATH, live=fbackups.live_counts(c), key_path=BACKUP_KEY_PATH,
            settings_files=files if isinstance(files, list) else None)
    except Exception as e:
        # Saved as a failed test all the same, so the card shows it and the
        # hourly loop retries in a day instead of every hour.
        log.exception("backups: the restore test could not run")
        result = {"ok": False, "snapshot": None, "settings_file": None,
                  "reason": f"The restore test couldn't run ({e})."}
    finally:
        _backup_started_at = None
    fbackups.record_restore_test(c, result, dt.datetime.now(dt.timezone.utc))
    return result


def _restore_test_if_due(c) -> None:
    """The monthly restore test, from the hourly run right after its copy.
    Armored: a crash here is logged and never fails the backup."""
    try:
        rec = fdb.kv_get(c, fbackups.STATE_KEY)
        last = fdb.kv_get(c, fbackups.RESTORE_TEST_KEY)
        if fbackups.restore_test_due(rec if isinstance(rec, dict) else {},
                                     last if isinstance(last, dict) else None,
                                     dt.datetime.now(dt.timezone.utc)):
            _restore_test_run(c)
    except Exception:
        log.error("backups: the restore test crashed; it tries again next hour",
                  exc_info=True)


async def backup_loop() -> None:
    """The hourly backup, on the app's loop like the laundry watcher. The work
    runs in a thread so a slow NAS never holds up the wall. Every run is
    armored: a crash is logged and the next hour tries again. CancelledError
    passes through (it is the lifespan's shutdown)."""
    await asyncio.sleep(BACKUP_FIRST_S)
    while True:
        try:
            await asyncio.to_thread(_backup_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backups: the hourly run crashed; trying again next hour")
        await asyncio.sleep(BACKUP_EVERY_S)


def _password_set_at(c) -> str | None:
    """When the backup password was last set (ISO, UTC). Newer saves store the
    time; an older one stored just True, so fall back to the key file's own
    modified time (set_password rewrites it on every change)."""
    at = fdb.kv_get(c, _BACKUP_PW_KEY)
    if isinstance(at, str) and at:
        return at
    try:
        mtime = os.path.getmtime(BACKUP_KEY_PATH)
    except OSError:
        return None
    return dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat()


def _backups_state(c) -> dict:
    """What Settings > Backups shows."""
    if DEMO:
        return fdemo.demo_backups()
    why = _backup_why_off(c)
    rec = fdb.kv_get(c, fbackups.STATE_KEY)
    rec = rec if isinstance(rec, dict) else {}
    counts, counts_error = None, None
    if why not in ("no_folder", "not_mounted", "marker_missing"):
        try:
            counts = fbackups.counts(BACKUPS_PATH)
        except OSError as e:
            counts_error = f"Couldn't read the backup folder ({e})."
            log.warning("backups: %s", counts_error)
    key = fbackups.key_state(BACKUP_KEY_PATH)
    return {
        "enabled": why is None, "why_off": why,
        "why_off_text": _BACKUP_WHY.get(why) if why else None,
        "switch_on": _integration_on(c, "backups"),
        "scheduled": _backup_loop_enabled(), "every_s": BACKUP_EVERY_S,
        "dir": BACKUP_DIR_LABEL, "running": _backup_lock.locked(),
        "last_ok": rec.get("last_ok"), "last_error": rec.get("last_error"),
        "settings": rec.get("settings"), "settings_error": rec.get("settings_error"),
        "counts": counts, "counts_error": counts_error,
        "keep": {**fbackups.KEEP, "settings": fbackups.SETTINGS_KEEP},
        "password_set": key == "ok",
        "password_set_at": _password_set_at(c) if key == "ok" else None,
        "password_problem": key in ("damaged", "unreadable")
        or (key == "unset" and bool(fdb.kv_get(c, _BACKUP_PW_KEY))),
        "restore_test": _restore_test_state(c),
        "restore_test_overdue": _restore_test_overdue(c, why),
    }


_RESTORE_FIELDS = ("at", "ok", "snapshot", "reason", "settings_file", "settings_checked",
                   "settings_skip", "last_pass_at")


def _restore_test_state(c) -> dict | None:
    """The last restore test for the card, or None."""
    rt = fdb.kv_get(c, fbackups.RESTORE_TEST_KEY)
    if not isinstance(rt, dict):
        return None
    return {k: rt.get(k) for k in _RESTORE_FIELDS}


def _restore_test_overdue(c, why) -> bool:
    """No pass in 35 days while backups are on and the hourly job runs: the
    scheduler may never be firing, which must not look like quiet success."""
    if why is not None or not _backup_loop_enabled():
        return False
    rec = fdb.kv_get(c, fbackups.STATE_KEY)
    last = fdb.kv_get(c, fbackups.RESTORE_TEST_KEY)
    return fbackups.restore_test_overdue(rec if isinstance(rec, dict) else {},
                                         last if isinstance(last, dict) else None,
                                         dt.datetime.now(dt.timezone.utc))


def _restore_test_health_note(c) -> dict:
    """/health/full's note: the last result (fields None before any) and
    whether it is overdue. Information only, never a failing status."""
    note = _restore_test_state(c) or {k: None for k in _RESTORE_FIELDS}
    note["overdue"] = _restore_test_overdue(c, _backup_why_off(c))
    return note


@app.get("/api/backups")
def backups_get():
    return _backups_state(_db())


@app.post("/api/backups/run")
def backups_run():
    """Back up now. 409 when backups are off or a run is already going."""
    c = _db()
    why = _backup_why_off(c)
    if why:
        raise HTTPException(409, _BACKUP_WHY[why])
    if not _backup_lock.acquire(blocking=False):
        raise HTTPException(409, "A backup is already running.")
    try:
        result, record_err = _backup_run(c)
    finally:
        _backup_lock.release()
    if record_err is not None:
        # never "ok" next to a state that still shows the old run
        raise HTTPException(500, f"The backup ran, but the hub couldn’t record it ({record_err}).")
    return {"result": result, "state": _backups_state(c)}


@app.post("/api/backups/restore-test")
def backups_restore_test():
    """Test restore now: prove the newest backup restores. Shares the backup
    lock (409 while one runs) and refuses when backups are off or there is
    nothing to test yet."""
    c = _db()
    why = _backup_why_off(c)
    if why:
        raise HTTPException(409, _BACKUP_WHY[why])
    rec = fdb.kv_get(c, fbackups.STATE_KEY)
    if not (isinstance(rec, dict) and rec.get("last_ok")):
        raise HTTPException(409, "There is no backup to test yet. Back up now first.")
    if not _backup_lock.acquire(blocking=False):
        raise HTTPException(409, "A backup is already running.")
    try:
        result = _restore_test_run(c)
    finally:
        _backup_lock.release()
    return {"result": result, "state": _backups_state(c)}


@app.post("/api/backups/adopt")
def backups_adopt(request: Request):
    """Use this folder: mark the current backup folder as the right one after
    a deliberate move. Needs the parent PIN when one is set."""
    if DEMO:
        raise HTTPException(409, _BACKUP_WHY["demo"])
    c = _db()
    require_parent(request)
    why = fbackups.folder_problem(BACKUPS_PATH, must_be_mount=not _BACKUPS_PATH_SET)
    if why:
        raise HTTPException(409, _BACKUP_WHY[why])
    fdb.kv_set(c, fbackups.MARKER_KEY, fbackups.adopt_folder(BACKUPS_PATH))
    log.warning("backups: %s is now the backup folder (Use this folder)", BACKUP_DIR_LABEL)
    return _backups_state(c)


def _backups_health_note(c) -> dict:
    """The in-app job for /health/full's notes: information, never a problem."""
    rec = fdb.kv_get(c, fbackups.STATE_KEY)
    rec = rec if isinstance(rec, dict) else {}
    why = _backup_why_off(c)
    return {"enabled": why is None, "why_off": why, "dir": BACKUP_DIR_LABEL,
            "last_ok_at": (rec.get("last_ok") or {}).get("at"),
            "last_error": rec.get("last_error"), "running": _backup_lock.locked()}


class BackupPassword(BaseModel):
    password: str


@app.post("/api/backups/password")
def backups_set_password(body: BackupPassword, request: Request):
    """Set or change the backup password (never stored; backup.key holds the
    key made from it). Needs the parent PIN when one is set."""
    if DEMO:
        raise HTTPException(409, _BACKUP_WHY["demo"])
    c = _db()
    require_parent(request)
    try:
        fbackups.set_password(BACKUP_KEY_PATH, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e).capitalize() + ".")
    fdb.kv_set(c, _BACKUP_PW_KEY, dt.datetime.now(dt.timezone.utc).isoformat())
    log.info("backups: the backup password was set")
    return _backups_state(c)


# --- integrations (the settings menu / extension toggles) -----------------

class IntegrationPatch(BaseModel):
    enabled: bool | None = None
    readonly: bool | None = None   # CalDAV: 1-way (read-only, True) vs 2-way


@app.get("/api/integrations")
def integrations_list():
    """Every available integration with its on/off state, for the settings menu."""
    return {"integrations": _integrations_state(_db())["list"]}


@app.patch("/api/integrations/{iid}")
def integrations_patch(iid: str, body: IntegrationPatch, request: Request):
    c = _db()
    avail = {i["id"]: i for i in _available_only()}
    if iid not in avail:
        raise HTTPException(404, "unknown integration")
    # Switching the chore wallet off lifts its old-day lock, so once it has
    # been turned on, the switch is a parent action in both directions.
    if (iid == "rewards" and body.enabled is not None
            and fdb.kv_get(c, wallet.LAUNCH_KEY)
            and body.enabled != fdb.integration_enabled(c, iid)):
        require_parent(request)
    # ensure a row exists (a never-toggled integration has none yet), then set it
    fdb.seed_integration(c, iid, avail[iid]["kind"],
                         enabled=avail[iid].get("default_on", True))
    if body.enabled is not None:
        fdb.set_integration_enabled(c, iid, body.enabled)
        if iid == "away_dim":
            # never let an old answer stand across a flip; ask HA at once
            global _presence_kick
            _presence_forget()
            _presence_kick = True
    if body.readonly is not None:
        conf = fdb.integration_config(c, iid)
        conf["readonly"] = bool(body.readonly)
        fdb.set_integration_config(c, iid, conf)
    return {"id": iid, "enabled": fdb.integration_enabled(c, iid),
            "readonly": fdb.integration_config(c, iid).get("readonly", True)}


# --- iCloud CalDAV credentials (entered in settings, stored server-side) ---

_CALDAV_ENV_CREDS_MSG = ("iCloud is set by the ICLOUD_CALDAV_USER and "
                         "ICLOUD_CALDAV_APP_PASSWORD environment variables on "
                         "the server; change or remove them there")


class CalDavCreds(BaseModel):
    user: str
    app_password: str


@app.post("/api/integrations/icloud_caldav/credentials")
def caldav_set_credentials(body: CalDavCreds):
    """Store the iCloud bot credentials the operator types in settings. The
    app-specific password is written to a server-side file (mode 0600) and is
    NEVER returned by any endpoint; only its presence is ever reported."""
    user = (body.user or "").strip()
    pw = (body.app_password or "").strip()
    if not (user and pw):
        raise HTTPException(422, "user and app_password are required")
    # Env credentials win over the settings file, so a save here would answer
    # ok while the env account stayed in use. Refuse it and say why.
    if caldav_service.env_credentials_set(os.environ):
        raise HTTPException(409, _CALDAV_ENV_CREDS_MSG)
    caldav_service.store_credentials(user, pw)
    global _caldav_client_built
    _caldav_client_built = False          # next sync/test rebuilds with new creds
    fdb.seed_integration(_db(), "icloud_caldav", "caldav")
    return {"ok": True, "user": user}     # the Apple ID is not a secret; no password


@app.delete("/api/integrations/icloud_caldav/credentials")
def caldav_clear_credentials():
    c = _db()
    # Disconnect only removes the settings file; with env credentials set the
    # hub would stay connected behind an ok reply.
    if caldav_service.env_credentials_set(os.environ):
        raise HTTPException(409, _CALDAV_ENV_CREDS_MSG)
    caldav_service.clear_credentials()
    global _caldav_client_built
    _caldav_client_built = False
    # iCloud is gone, so the source picker disappears with it: if the To-Do
    # surface was pointed at iCloud, fall back to local — otherwise it strands on
    # an empty iCloud card with no visible control to switch back.
    if fdb.kv_get(c, "todo_source") == "icloud":
        fdb.kv_set(c, "todo_source", "local")
    return {"ok": True}


@app.post("/api/integrations/icloud_caldav/test")
def caldav_test_connection():
    """Run a CalDAV sync now and report the outcome so credentials can be
    verified from settings. Returns status only (ok / needs_auth / error /
    counts) — never the password. Network call is deliberate (a user action)."""
    global _caldav_client_built
    _caldav_client_built = False
    client = _get_caldav_client()
    if client is None:
        return {"ok": False, "error": "no credentials"}
    # Wait for a background sync already in flight rather than running a
    # second one beside it; a sync that never finishes gets an honest error.
    if not _caldav_sync_lock.acquire(timeout=CALDAV_TEST_WAIT_S):
        return {"ok": False,
                "error": "a sync is already running; try again in a minute"}
    try:
        return caldav_sync.sync_once(client, _db(), cfg, _now_local())
    finally:
        _caldav_sync_lock.release()


@app.get("/api/integrations/icloud_caldav/collections")
def caldav_collections():
    """The discovered iCloud calendars + reminder lists for the settings picker:
    each with name, color, kind (VEVENT/VTODO), and its visibility toggle."""
    return {"collections": [
        {"id": col["id"], "name": col["display_name"], "color": col["color"],
         "comp_type": col["comp_type"], "enabled": col["enabled"]}
        for col in fdb.list_caldav_collections(_db())]}


class CollectionPatch(BaseModel):
    enabled: bool


@app.patch("/api/integrations/icloud_caldav/collections/{cid}")
def caldav_collection_patch(cid: str, body: CollectionPatch):
    """Show/hide one iCloud calendar or reminder list on the wall (cache kept)."""
    if not fdb.set_caldav_collection_enabled(_db(), cid, body.enabled):
        raise HTTPException(404, "unknown collection")
    return {"id": cid, "enabled": body.enabled}


# --- reminders (read-only iCloud VTODO) -----------------------------------

@app.get("/api/reminders")
def reminders_full():
    """The full grouped iCloud Reminders view. `configured` is False (empty
    buckets) when CalDAV has no credentials or its integration is off."""
    c = _db()
    if not _integration_on(c, "icloud_caldav"):
        return {"buckets": {b: [] for b in remlogic.BUCKETS}, "configured": False}
    return {"buckets": remlogic.group(_visible_reminders(c), _today(), TZ),
            "configured": True, "writable": _reminders_writable(c)}


# --- reminders (two-way iCloud VTODO writes) ------------------------------

class ReminderToggle(BaseModel):
    id: str
    completed: bool


class ReminderAdd(BaseModel):
    list_id: str
    title: str
    due: str | None = None      # 'YYYY-MM-DD' (all-day) or None


class ReminderDelete(BaseModel):
    id: str


def _reminders_writable(c) -> bool:
    """Two-way is on: CalDAV available+enabled AND the operator has switched it
    off read-only (readonly=False) in settings."""
    return (_integration_on(c, "icloud_caldav")
            and not fdb.integration_config(c, "icloud_caldav").get("readonly", True))


def _require_reminders_write(c) -> None:
    """Guard shared by the write endpoints — a clear 409 (not a silent no-op) when
    iCloud is off or reminders are still read-only, so the wall can explain why."""
    if not _integration_on(c, "icloud_caldav"):
        raise HTTPException(409, "iCloud is not connected")
    if fdb.integration_config(c, "icloud_caldav").get("readonly", True):
        raise HTTPException(409, "iCloud reminders are read-only "
                                 "(enable two-way in settings)")


def _parse_due(s: str | None):
    """An all-day due date from 'YYYY-MM-DD', or None. 422 on a malformed string
    rather than silently dropping the date."""
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        raise HTTPException(422, "due must be YYYY-MM-DD")


@app.post("/api/reminders/toggle")
def reminders_toggle(body: ReminderToggle):
    """Check off / reopen an iCloud reminder from the wall: mutate the stored
    VTODO and queue it for the next sync's push. The read overlay reflects it at
    once, so the wall updates without waiting for the round-trip."""
    c = _db()
    _require_reminders_write(c)
    obj = fdb.get_cal_object(c, body.id)
    if obj is None or obj["comp_type"] != "VTODO":
        raise HTTPException(404, "unknown reminder")
    now = dt.datetime.now(dt.timezone.utc)
    ics = remlogic.set_completed(obj["raw_ics"], body.completed, now)
    if not fdb.queue_cal_object_update(c, body.id, ics, obj["summary"],
                                       now.isoformat()):
        # deleted since it was read (or queued for delete): nothing was queued,
        # so don't answer as if the tap worked. 'unknown reminder' is the
        # string common.js reminderFailMessage turns into its "already changed
        # on another device" toast.
        raise HTTPException(404, "unknown reminder (deleted)")
    return {"id": body.id, "completed": body.completed}


@app.post("/api/reminders/add")
def reminders_add(body: ReminderAdd):
    """Add a reminder to an iCloud list from the wall (queued, pushed next sync)."""
    c = _db()
    _require_reminders_write(c)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(422, "title required")
    col = next((x for x in fdb.list_caldav_collections(c)
                if x["id"] == body.list_id and x["comp_type"] == "VTODO"), None)
    if col is None:
        raise HTTPException(404, "unknown reminder list")
    due = _parse_due(body.due)
    now = dt.datetime.now(dt.timezone.utc)
    uid = f"familyhub-{uuid.uuid4()}".upper()
    oid = f"{body.list_id}/{uid}"
    ics = remlogic.build_vtodo(uid, title, now, due=due)
    fdb.queue_cal_object_create(c, {
        "id": oid, "collection_id": body.list_id, "comp_type": "VTODO",
        "uid": uid, "summary": title, "raw_ics": ics}, now.isoformat())
    return {"id": oid, "title": title, "due": due.isoformat() if due else None}


@app.post("/api/reminders/delete")
def reminders_delete(body: ReminderDelete):
    """Delete an iCloud reminder from the wall (queued, removed server-side next
    sync)."""
    c = _db()
    _require_reminders_write(c)
    # Events share the cal_objects store. Only a reminder may be deleted here,
    # or a stray id would queue a family calendar event for removal in iCloud.
    obj = fdb.get_cal_object(c, body.id)
    if obj is None or obj["comp_type"] != "VTODO":
        raise HTTPException(404, "unknown reminder")
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    if not fdb.queue_cal_object_delete(c, body.id, now_iso):
        raise HTTPException(404, "unknown reminder")
    return {"id": body.id, "deleted": True}


# --- admin ----------------------------------------------------------------

class PersonIn(BaseModel):
    name: str
    color: str


class PersonPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    sort: int | None = None
    # 0/1 only: the `active = 1` filters would silently read any other number
    # as inactive. JSON true/false still parse to 1/0.
    active: Literal[0, 1] | None = None
    # The iCloud reminder list (caldav:<slug>) this person's chores mirror into;
    # null clears the mapping. Nullable, so absent from _PERSON_NONNULL_PATCH.
    reminder_list_id: str | None = None


class AwayIn(BaseModel):
    person_id: int
    start_date: str | None = None
    backup_person_id: int | None = None


class AwayPatch(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    backup_person_id: int | None = None


class AwayEveryoneIn(BaseModel):
    start_date: str | None = None


class AwayBackIn(BaseModel):
    end_date: str | None = None


class TripIn(BaseModel):
    """Save a trip: who is going, the first away day, and the LAST away day
    (end_date; chores come back the day after). `ids` names the periods of an
    existing trip being edited; omit it to start a new one. The backup covers
    the fixed chores of everyone going and must be someone staying home."""
    person_ids: list[int]
    start_date: str | None = None
    end_date: str | None = None
    backup_person_id: int | None = None
    ids: list[int] | None = None


class TripEndIn(BaseModel):
    ids: list[int]


class ChoreIn(BaseModel):
    title: str
    icon: str = ""
    schedule_kind: str
    days_mask: int = 0
    week_interval: int = 1              # 'days': 1=weekly, 2=biweekly
    interval_days: int | None = None   # 'interval': every N days from epoch
    due_times: list[str] = []          # ["HH:MM",...] -> iOS notifications
    assign_kind: str
    fixed_person_id: int | None = None
    rotation_order: list[int] = []
    # When a rotation passes the turn on: 'turn' (every time it comes up) or
    # 'week' (one person all week, the next person next week).
    rotate_every: Literal["turn", "week"] = "turn"
    # 'house' earns and counts for the chore wallet's streak; 'personal' is a
    # personal goal (vitamins, reading): on the card, but neither.
    kind: Literal["house", "personal"] = "house"
    # A one-time chore's single due date ('YYYY-MM-DD'); stored as rotation_epoch.
    # Ignored for daily/weekly chores (those anchor to today).
    date: str | None = None


class ChorePatch(BaseModel):
    title: str | None = None
    icon: str | None = None
    schedule_kind: str | None = None
    days_mask: int | None = None
    week_interval: int | None = None
    interval_days: int | None = None
    due_times: list[str] | None = None
    assign_kind: str | None = None
    fixed_person_id: int | None = None
    rotation_order: list[int] | None = None
    rotate_every: Literal["turn", "week"] | None = None
    kind: Literal["house", "personal"] | None = None
    date: str | None = None
    sort: int | None = None
    active: Literal[0, 1] | None = None   # 0/1 only, as on PersonPatch
    # Not a column: whoever is up next before this edit stays up next after
    # it (see _keep_turn). The chore form sends it on every edit until the
    # person editing changes the turns themselves.
    keep_turn: bool = False


# An explicit JSON null for a field backed by a NOT NULL column is a bad request
# (422), not a 500 from the DB write (issue #35). fixed_person_id is the one
# chore field that legitimately accepts null (clearing a fixed assignee); `date`
# is an API-only field whose null means "no change".
_PERSON_NONNULL_PATCH = {"name", "color", "sort", "active"}
_CHORE_NONNULL_PATCH = {"title", "icon", "schedule_kind", "days_mask",
                        "week_interval", "due_times", "assign_kind",
                        "rotation_order", "rotate_every", "kind", "sort",
                        "active"}
# interval_days is nullable (only set for the 'interval' kind), so it is NOT in
# the non-null set — an explicit null clears it, which is correct.


def _reject_null_nonnullable(fields: dict, nonnullable: set) -> None:
    bad = sorted(k for k in fields if k in nonnullable and fields[k] is None)
    if bad:
        raise HTTPException(422, f"{', '.join(bad)} may not be null")


def _validate_person(name: str, color: str) -> str:
    name = (name or "").strip()
    if not (1 <= len(name) <= 30):
        raise HTTPException(422, "name must be 1–30 characters")
    if not _HEX.match(color or ""):
        raise HTTPException(422, "color must be a #rrggbb hex value")
    return name


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_due_times(times) -> None:
    """Reminder times are 'HH:MM' 24h, at most a handful (each becomes an iOS
    notification). A bad time is a 422, never a silent drop."""
    if not isinstance(times, list):
        raise HTTPException(422, "due_times must be a list of HH:MM strings")
    if len(times) > 6:
        raise HTTPException(422, "at most 6 reminder times")
    for t in times:
        if not (isinstance(t, str) and _HHMM.match(t)):
            raise HTTPException(422, f"invalid time {t!r} — use HH:MM (00:00–23:59)")


def _validate_chore(merged: dict) -> None:
    title = (merged.get("title") or "").strip()
    if not (1 <= len(title) <= 60):
        raise HTTPException(422, "title must be 1–60 characters")
    if len(merged.get("icon") or "") > 4:
        raise HTTPException(422, "icon must be at most 4 characters")
    kind = merged["schedule_kind"]
    if kind not in ("daily", "days", "once", "interval"):
        raise HTTPException(422, "schedule_kind must be daily, days, once or interval")
    mask = merged.get("days_mask") or 0
    if not (0 <= mask <= 127):
        raise HTTPException(422, "days_mask must be 0–127")
    if kind == "days":
        if mask == 0:
            raise HTTPException(422, "pick at least one day for a weekly chore")
        if (merged.get("week_interval") or 1) not in (1, 2):
            raise HTTPException(422, "week_interval must be 1 (weekly) or 2 (biweekly)")
    if kind == "interval":
        n = merged.get("interval_days")
        if not isinstance(n, int) or not (1 <= n <= 365):
            raise HTTPException(422, "interval_days must be 1–365")
    _validate_due_times(merged.get("due_times") or [])
    if kind == "once":
        # A one-time chore is one person on one date — no rotation.
        if merged["assign_kind"] != "fixed":
            raise HTTPException(422, "a one-time chore is for one person")
        # The date arrives as `date`. When the key is absent (a patch that
        # isn't touching the date), fall back to the stored rotation_epoch so
        # the existing due date stands. An explicitly EMPTY date is a clear,
        # not an absence — reject it rather than silently reusing the old date
        # (which would also let the write corrupt rotation_epoch to "").
        supplied = merged.get("date")
        due = supplied if supplied is not None else merged.get("rotation_epoch")
        try:
            due_date = dt.date.fromisoformat(due or "")
        except (TypeError, ValueError):
            raise HTTPException(422, "pick a valid date for a one-time chore")
        # A date the user is actively setting can't be in the past — the chore
        # would never appear. A title-only edit of an already-past one-time
        # chore falls back to the stored epoch (supplied is None) and skips
        # this, so past chores stay editable.
        if supplied is not None and due_date < _today():
            raise HTTPException(422, "pick today or a later date for a one-time chore")
    if merged["assign_kind"] not in ("fixed", "rotation"):
        raise HTTPException(422, "assign_kind must be fixed or rotation")
    if merged["assign_kind"] == "fixed" and merged.get("fixed_person_id") is None:
        raise HTTPException(422, "pick a person for a fixed chore")
    if merged["assign_kind"] == "rotation" and not merged.get("rotation_order"):
        raise HTTPException(422, "add people to the rotation")
    if (merged.get("rotate_every") or "turn") not in ("turn", "week"):
        raise HTTPException(422, "rotate_every must be turn or week")
    # Every assignee must be a real person: an unknown id was accepted and made
    # the chore invisible on every card (review, 2026-09-22). A turned-off
    # person is still a person (editing a chore that names one must keep
    # working); a chore left with no ACTIVE owner shows in the chores edit
    # mode under "No one to do these". A person listed twice in a rotation is
    # allowed on purpose (two turns in the cycle).
    known = {p["id"] for p in fdb.list_people(_db(), include_inactive=True)}
    ids = ([merged.get("fixed_person_id")] if merged["assign_kind"] == "fixed"
           else list(merged.get("rotation_order") or []))
    if any(not isinstance(i, int) or i not in known for i in ids):
        raise HTTPException(422, "that person doesn't exist")


def _person_row(c, pid: int) -> dict:
    for row in fdb.list_people(c, include_inactive=True):
        if row["id"] == pid:
            return row
    raise HTTPException(404, "unknown person")


def _chore_row(c, cid: int) -> dict:
    for row in fdb.list_chores(c, include_inactive=True):
        if row["id"] == cid:
            return row
    raise HTTPException(404, "unknown chore")


@app.get("/api/admin/state")
def admin_state():
    c = _db()
    # list_gone: mapped to a list the sync dropped. Decided here, not on the
    # wall: an empty list array means "every list gone" as much as "not
    # connected", and only the server can tell them apart.
    gone = {p["id"] for p in
            chore_mirror.people_with_gone_lists(c, include_inactive=True)}
    people = [{**p, "list_gone": p["id"] in gone}
              for p in fdb.list_people(c, include_inactive=True)]
    return {"people": people,
            "chores": fdb.list_chores(c, include_inactive=True),
            "away_periods": fdb.list_away_periods(c),
            # iCloud VTODO lists a person's chores can mirror into (P2 picker);
            # empty until iCloud is connected and its reminder lists are synced.
            "reminder_lists": [{"id": col["id"], "name": col["display_name"]}
                               for col in fdb.list_caldav_collections(c)
                               if col["comp_type"] == "VTODO"],
            # Last chore-mirror tick: {ok, at, created, moved, updated, deleted}
            # ({} before the first two-way sync). A mirror that dies every tick
            # used to be completely invisible.
            "chore_mirror_status": fdb.kv_get(c, "chore_mirror_status") or {}}


@app.post("/api/admin/people")
def admin_add_person(p: PersonIn):
    c = _db()
    name = _validate_person(p.name, p.color)
    pid = fdb.add_person(c, name, p.color)
    return _person_row(c, pid)


@app.patch("/api/admin/people/{pid}")
def admin_patch_person(pid: int, p: PersonPatch):
    c = _db()
    row = _person_row(c, pid)
    fields = p.model_dump(exclude_unset=True)
    _reject_null_nonnullable(fields, _PERSON_NONNULL_PATCH)
    if "name" in fields or "color" in fields:
        name = fields.get("name", row["name"])
        color = fields.get("color", row["color"])
        fields["name"] = _validate_person(name, color)
    if fields.get("reminder_list_id"):   # non-empty must be a real VTODO list
        vtodo = {col["id"] for col in fdb.list_caldav_collections(c)
                 if col["comp_type"] == "VTODO"}
        if fields["reminder_list_id"] not in vtodo:
            raise HTTPException(422, "unknown reminder list")
    fdb.update_person(c, pid, **fields)
    if ("active" in fields and bool(fields["active"]) != bool(row["active"])
            and fdb.kv_get(c, wallet.LAUNCH_KEY)):
        # Days switched off count like away days on the wallet (audit,
        # 2026-11-26), so a child switched back on gets no free kept days.
        wallet.record_active_change(c, pid, bool(fields["active"]), _today())
    return _person_row(c, pid)


@app.delete("/api/admin/people/{pid}")
def admin_delete_person(pid: int, request: Request):
    """Hard-delete a person (history-safe — see db.delete_person). Distinct from
    the deactivate path (PATCH active=0), which keeps the row for reactivation."""
    c = _db()
    launch = fdb.kv_get(c, wallet.LAUNCH_KEY)
    if (launch and rewards_active(c)
            and wallet.balance_cents(c, pid, launch["day"]) != 0):
        # Deleting drops their earnings for good: with money still owed (or
        # owing), that needs the parent PIN (audit, 2026-11-26).
        require_parent(request)
    if not fdb.delete_person(c, pid):
        raise HTTPException(404, "unknown person")
    return {"ok": True}


@app.post("/api/admin/chores")
def admin_add_chore(ch: ChoreIn):
    c = _db()
    merged = ch.model_dump()
    _validate_chore(merged)
    kind = merged["schedule_kind"]
    # A one-time chore stores its single due date as rotation_epoch; daily/weekly
    # chores anchor to today.
    epoch = merged["date"] if kind == "once" else _today().isoformat()
    cid = fdb.add_chore(
        c, title=merged["title"].strip(), icon=merged["icon"],
        schedule_kind=kind,
        days_mask=merged["days_mask"] if kind == "days" else 0,
        week_interval=merged["week_interval"] if kind == "days" else 1,
        interval_days=merged["interval_days"] if kind == "interval" else None,
        due_times=merged["due_times"],
        assign_kind=merged["assign_kind"],
        fixed_person_id=merged["fixed_person_id"] if merged["assign_kind"] == "fixed" else None,
        rotation_order=merged["rotation_order"] if merged["assign_kind"] == "rotation" else [],
        rotate_every=merged["rotate_every"] if merged["assign_kind"] == "rotation" else "turn",
        kind=merged["kind"],
        rotation_epoch=epoch)
    return _chore_row(c, cid)


@app.patch("/api/admin/chores/{cid}")
def admin_patch_chore(cid: int, ch: ChorePatch, request: Request):
    c = _db()
    row = _chore_row(c, cid)
    fields = ch.model_dump(exclude_unset=True)
    _reject_null_nonnullable(fields, _CHORE_NONNULL_PATCH)
    # Turning a house chore into a personal goal (or back) changes what a
    # child earns, so with the wallet on it needs the parent PIN.
    if ("kind" in fields and fields["kind"] != row.get("kind", "house")
            and rewards_active(c)):
        require_parent(request)
    # Turning it off, or into a one-time item, drops what it paid today;
    # giving it to someone else moves that pay.
    owner_change = any(k in fields and fields[k] != row.get(k) for k in (
        "assign_kind", "fixed_person_id", "rotation_order", "rotate_every"))
    if owner_change or fields.get("active") == 0 or (
            fields.get("schedule_kind") == "once" and row["schedule_kind"] != "once"):
        _guard_paid_today(c, cid, request)
    merged = {**row, **fields}
    kind = merged["schedule_kind"]
    # Converting a daily/weekly chore to one-time needs a real due date. Its
    # existing rotation_epoch is a creation anchor (a past date for any chore
    # older than today), not a due date — inheriting it would land the one-time
    # chore in the past, invisible forever. _validate_chore's fallback can't
    # tell this conversion from a title-only edit of an already-once chore, so
    # the old-kind check lives here where the row is in scope.
    if kind == "once" and row["schedule_kind"] != "once" and not fields.get("date"):
        raise HTTPException(422, "pick a date for a one-time chore")
    _validate_chore(merged)
    # keep dependent columns coherent with the resolved kind
    if kind != "days":
        fields["days_mask"] = 0
        fields["week_interval"] = 1     # biweekly only applies to a 'days' chore
    if kind != "interval":
        fields["interval_days"] = None  # every-N-days only for the 'interval' kind
    if merged["assign_kind"] == "fixed":
        fields["rotation_order"] = []
        fields["rotate_every"] = "turn"   # only a rotation passes a turn on
    else:
        fields["fixed_person_id"] = None
    # A one-time chore's due date lives in rotation_epoch; translate the API's
    # `date` field onto it. `date` isn't a chore column, so drop it either way.
    # An empty date can't reach a write (validation rejects it), so gate on a
    # truthy value defensively.
    due = fields.pop("date", None)
    if kind == "once":
        if due:
            fields["rotation_epoch"] = due
    elif row["schedule_kind"] == "once":
        # Leaving one-time for daily/weekly: re-anchor to today, matching
        # add_chore. The stored rotation_epoch was the one-time due date (maybe
        # future), which would otherwise hide the now-recurring chore until it.
        fields["rotation_epoch"] = _today().isoformat()
    if "title" in fields:
        fields["title"] = fields["title"].strip()
    if fields.pop("keep_turn", False):
        active_ids = {p["id"] for p in fdb.list_people(c)}
        kept = _keep_turn(row, {**row, **fields}, _today(), active_ids)
        if kept is not None:
            fields["rotation_order"] = kept
            log.info("chore %s: edit kept the next turn (order %s)", cid, kept)
    fdb.update_chore(c, cid, **fields)
    return _chore_row(c, cid)


class ChorePreviewIn(ChoreIn):
    title: str = ""
    chore_id: int | None = None   # editing: keep that chore's own anchor date
    keep_turn: bool = False       # editing: what the PATCH's keep_turn would do


def _upcoming(chore: dict, today: dt.date, active_ids: set) -> tuple[list, int]:
    """A chore's next days from ``today`` and who does each, as the wall
    would assign them: ([{"date", "person_id"}], days looked ahead)."""
    kind = chore["schedule_kind"]
    horizon = _PREVIEW_DAYS
    out = []
    if kind == "once":
        # one dated day, however far off: read it directly, never walk to it
        # (a walk to a year-9999 date would tie the server up for seconds)
        due = dt.date.fromisoformat(chore["rotation_epoch"])
        horizon = max(horizon, (due - today).days + 1)
        if due >= today:
            out.append({"date": due.isoformat(),
                        "person_id": chlogic.assignee_id(chore, due, active_ids)})
        return out, horizon
    if kind == "interval":   # interval_days is at most 365: at most 731 days
        horizon = max(horizon, 2 * chore["interval_days"] + 1)
    d = today
    for _ in range(horizon):
        if len(out) >= _PREVIEW_MAX:
            break
        if chlogic.occurs(chore, d):
            out.append({"date": d.isoformat(),
                        "person_id": chlogic.assignee_id(chore, d, active_ids)})
        d += dt.timedelta(days=1)
    return out, horizon


def _turn_holder(old: dict, new: dict, today: dt.date,
                 active_ids: set) -> tuple[str, int] | None:
    """(date, person) for the edited chore's first upcoming day, where the
    person is whose turn that SAME date was under the chore as saved, or None
    when there is no such day or turn.

    The same date on both sides on purpose: "Fridays only" turned into "every
    day" has a different first day, and comparing the old first Friday with
    the new first Monday would keep (or name) the wrong person."""
    if not old.get("active", 1) or old["schedule_kind"] == "once":
        return None
    now = _upcoming(new, today, active_ids)[0]
    if not now:
        return None
    d = dt.date.fromisoformat(now[0]["date"])
    pid = chlogic.assignee_id(old, d, active_ids)
    return (now[0]["date"], pid) if pid is not None else None


def _keep_turn(old: dict, new: dict, today: dt.date, active_ids: set) -> list | None:
    """The turn order that keeps the person whose turn the edited chore's
    first upcoming day was before the edit, or None when there is nothing to
    keep (or it is already theirs).

    Changing how often a rotation repeats (or when it switches people) also
    changes how many turns have gone by, so without this an edit like "every
    other Monday" to "every Monday" quietly hands this week to someone else.
    Only where the cycle starts moves: the same people, in the same order."""
    if new["assign_kind"] != "rotation":
        return None
    order = list(new["rotation_order"])
    if len(order) < 2 or not all(i in active_ids for i in order):
        return None   # with someone inactive the wall turns a shorter list
    held = _turn_holder(old, new, today, active_ids)
    if held is None or held[1] not in order:
        return None
    day, pid = held
    d = dt.date.fromisoformat(day)
    if chlogic.assignee_id(new, d, active_ids) == pid:
        return None
    slot = chlogic.turn_slot(new, d) % len(order)
    shift = (order.index(pid) - slot) % len(order)
    return order[shift:] + order[:shift]


# How far ahead the form's preview looks (at least; a chore that comes up
# less often looks far enough to find its next two days), and how many days
# it lists at most (four weeks and change of a daily chore: the form groups
# a weekly rotation by week).
_PREVIEW_DAYS = 120
_PREVIEW_MAX = 31


@app.post("/api/admin/chores/preview")
def admin_preview_chore(ch: ChorePreviewIn):
    """Who does a chore on its next few days, for the chore form's live
    preview, computed by the SAME code the wall uses (chores.assignee_id),
    so what the form promises is what the wall does. Nothing is saved.

    Editing an existing chore keeps its stored anchor date, exactly as the
    PATCH would: a rotation's turns count from it. Away periods are left out
    on purpose: the preview shows the plan, and the wall shows who covers.

    Returns {"upcoming": [{"date", "person_id"}], "slot": n,
    "rotation_order": [...], "before": id} where `slot` is the place in
    rotation_order whose turn the FIRST upcoming day is (the form uses it for
    "start with ..."), or null for a one-person chore. With `keep_turn` an
    edit previews the PATCH's keep_turn: `rotation_order` is the restarted
    order the save would store. Editing (`chore_id`), `before` is whose turn
    the first upcoming day was on the chore as saved (null for a new chore),
    so the form can say when an edit hands that turn to someone else."""
    c = _db()
    merged = ch.model_dump()
    merged["title"] = merged["title"].strip() or "preview"
    _validate_chore(merged)
    kind = merged["schedule_kind"]
    today = _today()
    epoch = merged["date"] if kind == "once" else today.isoformat()
    if merged["chore_id"] is not None:
        row = _chore_row(c, merged["chore_id"])
        # the PATCH's re-anchor rules: once <-> recurring changes the anchor
        if kind != "once" and row["schedule_kind"] != "once":
            epoch = row["rotation_epoch"]
    chore = {
        "id": 0, "title": merged["title"], "icon": merged["icon"], "active": 1,
        "schedule_kind": kind,
        "days_mask": merged["days_mask"] if kind == "days" else 0,
        "week_interval": merged["week_interval"] if kind == "days" else 1,
        "interval_days": merged["interval_days"] if kind == "interval" else None,
        "assign_kind": merged["assign_kind"],
        "fixed_person_id": merged["fixed_person_id"],
        "rotation_order": merged["rotation_order"],
        "rotate_every": merged["rotate_every"],
        "rotation_epoch": epoch,
    }
    active_ids = {p["id"] for p in fdb.list_people(c)}
    before = None
    if merged["chore_id"] is not None:
        held = _turn_holder(row, chore, today, active_ids)
        before = held[1] if held else None
        if merged["keep_turn"]:
            kept = _keep_turn(row, chore, today, active_ids)
            if kept is not None:
                chore["rotation_order"] = kept
    out, horizon = _upcoming(chore, today, active_ids)
    # The slot indexes rotation_order as stored. With someone in it no longer
    # active, the wall turns over the shorter, active-only list instead
    # (assignee_id), so a slot into the stored list would name the wrong
    # person: say nothing rather than something wrong.
    slot = None
    order = chore["rotation_order"]
    if chore["assign_kind"] == "rotation" and out and all(i in active_ids for i in order):
        first = dt.date.fromisoformat(out[0]["date"])
        slot = chlogic.turn_slot(chore, first) % len(order)
    # rotation_order: the order the save would store (keep_turn may have
    # restarted it); before: whose turn that first day was before the edit.
    return {"upcoming": out, "slot": slot, "days": horizon,
            "rotation_order": order, "before": before}


@app.delete("/api/admin/chores/{cid}")
def admin_delete_chore(cid: int, request: Request):
    c = _db()
    _chore_row(c, cid)  # raises 404 if it doesn't exist (existing helper behavior)
    _guard_paid_today(c, cid, request)
    fdb.delete_chore(c, cid)
    return {"ok": True}


def _valid_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise HTTPException(422, "bad date")


def _away_rows(c):
    people = {p["id"]: p["name"]
              for p in fdb.list_people(c, include_inactive=True)}
    out = []
    for r in fdb.list_away_periods(c):
        row = dict(r)
        row["person_name"] = people.get(r["person_id"])
        row["backup_name"] = people.get(r["backup_person_id"])
        out.append(row)
    return out


# An explicit JSON null for start_date (a NOT NULL column) is a bad request,
# not a 500 from the DB write — same rule as the person/chore patches.
# end_date and backup_person_id are legitimately nullable (clearing them).
_AWAY_NONNULL_PATCH = {"start_date"}


def _validate_backup(c, backup_id: int, away_person_id: int,
                     span: tuple[str, str | None],
                     ignore_period_id: int | None = None) -> None:
    """A backup must be someone who can actually do the chores: a real person,
    not the away person, ACTIVE, and not themselves away. Picking someone who
    is inactive or away is accepted by the DB but then silently pauses every
    covered chore at resolve time — the family sees the chore vanish with no
    explanation. Reject it at the source instead. (A backup who goes away
    LATER is still handled by that resolve-time pause.)

    "Away themselves" means away on a day the backup must cover: ``span`` is
    the (start, end) of the period being covered, end None for open-ended. So
    a dated period that ends before the backup's own planned trip is fine,
    while an open-ended one runs into that trip and is refused."""
    if backup_id == away_person_id:
        raise HTTPException(422, "backup cannot be the same person")
    row = _person_row(c, backup_id)                 # 404 if unknown
    if not row["active"]:
        raise HTTPException(422, "backup must be an active person")
    if any(r["person_id"] == backup_id and r["id"] != ignore_period_id
           and _spans_overlap(span[0], span[1], r["start_date"], r["end_date"])
           for r in fdb.list_away_periods(c)):
        raise HTTPException(422, "backup is away themselves")


@app.get("/api/admin/away")
def admin_away_list():
    return {"away_periods": _away_rows(_db())}


@app.post("/api/admin/away")
def admin_away_open(a: AwayIn, request: Request):
    c = _db()
    _person_row(c, a.person_id)                     # 404 if unknown
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    _guard_away(c, request, inserts=[{"person_id": a.person_id,
                                      "start_date": start, "end_date": None}])
    if a.backup_person_id is not None:
        _validate_backup(c, a.backup_person_id, a.person_id, (start, None))
    # No overlapping periods per person: a second "going away" that overlaps one
    # already set would create overlapping rows for the same person (the wall
    # would resolve them nondeterministically without the away_map ORDER BY,
    # and the UI has no way to show two). This open-ended period runs from
    # `start` on, so it overlaps any period that hasn't ended before `start`.
    if any(r["person_id"] == a.person_id
           and _spans_overlap(start, None, r["start_date"], r["end_date"])
           for r in fdb.list_away_periods(c)):
        raise HTTPException(409, "person is already away or has a trip then")
    pid = fdb.add_away_period(c, a.person_id, start, None, a.backup_person_id)
    return next(r for r in _away_rows(c) if r["id"] == pid)


@app.post("/api/admin/away/everyone")
def admin_away_everyone(a: AwayEveryoneIn, request: Request):
    """API only since the wall's "Pause everyone" became the Trip sheet (which
    posts /api/trip). Kept for scripts and existing clients."""
    c = _db()
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    open_pids = {r["person_id"] for r in fdb.list_away_periods(c)
                 if _spans_overlap(start, None, r["start_date"], r["end_date"])}
    _guard_away(c, request, inserts=[
        {"person_id": p["id"], "start_date": start, "end_date": None}
        for p in fdb.list_people(c) if p["id"] not in open_pids])
    created = []
    for p in fdb.list_people(c):                    # active only
        if p["id"] in open_pids:
            continue
        created.append(fdb.add_away_period(c, p["id"], start, None, None))
    return {"created": created}


@app.patch("/api/admin/away/{pid}")
def admin_away_patch(pid: int, a: AwayPatch, request: Request):
    c = _db()
    row = fdb.get_away_period(c, pid)
    if row is None:
        raise HTTPException(404, "unknown away period")
    fields = a.model_dump(exclude_unset=True)
    _reject_null_nonnullable(fields, _AWAY_NONNULL_PATCH)
    for k in ("start_date", "end_date"):
        if fields.get(k) is not None:
            fields[k] = _valid_date(fields[k])
    # A backup change must pass the same checks as opening: real person, not the
    # away person themselves, active and not away. (None clears the backup and
    # is always allowed.) This row's own period is excluded from the away check
    # — it is the away person's, never the backup's.
    # Re-checked on a date change too: moving the dates onto the backup's own
    # trip would otherwise leave their covered chores paused with no word.
    backup = fields.get("backup_person_id", row["backup_person_id"])
    if backup is not None and ({"backup_person_id", "start_date", "end_date"}
                               & set(fields)):
        _validate_backup(c, backup, row["person_id"],
                         (fields.get("start_date", row["start_date"]),
                          fields.get("end_date", row["end_date"])),
                         ignore_period_id=pid)
    # The effective end_date must not precede the effective start_date, else
    # away_map silently voids the whole period (its a>b skip). "Effective"
    # covers both directions: an incoming end_date earlier than the (possibly
    # also-incoming) start, AND an incoming start_date pushed later than the
    # row's already-stored end_date when the patch doesn't re-supply end_date.
    effective_start = fields.get("start_date", row["start_date"])
    effective_end = fields.get("end_date", row["end_date"])
    if effective_end is not None and effective_start is not None \
            and effective_end < effective_start:
        raise HTTPException(422, "end_date must not be before start_date")
    _guard_away(c, request, updates=[(pid, fields)])
    fdb.update_away_period(c, pid, **fields)
    return {"ok": True}


@app.post("/api/admin/away/{pid}/back")
def admin_away_back(pid: int, request: Request, a: AwayBackIn | None = None):
    c = _db()
    row = fdb.get_away_period(c, pid)
    if row is None:
        raise HTTPException(404, "unknown away period")
    explicit = bool(a and a.end_date)
    if not explicit and row["start_date"] == _today().isoformat():
        # "Going away" then "I'm back" on the same day: the default end,
        # yesterday, falls before the start, so the period never took effect.
        # Remove it instead of 422ing every tap. A trip planned for a LATER
        # start still 422s below: one tap must not quietly delete a plan.
        _guard_away(c, request, deletes=[pid])
        fdb.delete_away_period(c, pid)
        return {"ok": True}
    end = (_valid_date(a.end_date) if explicit
           else (_today() - dt.timedelta(days=1)).isoformat())
    # An explicit end before the start would silently void the period via
    # away_map's a>b skip.
    if end < row["start_date"]:
        raise HTTPException(422, "end_date must not be before start_date")
    _guard_away(c, request, updates=[(pid, {"end_date": end})])
    fdb.close_away_period(c, pid, end)
    return {"ok": True}


@app.delete("/api/admin/away/{pid}")
def admin_away_delete(pid: int, request: Request):
    c = _db()
    _guard_away(c, request, deletes=[pid])
    if not fdb.delete_away_period(c, pid):
        raise HTTPException(404, "unknown away period")
    return {"ok": True}


# --- trips ------------------------------------------------------------------
# A trip is the household-facing face of away periods: one or more people away
# over the same dates, set from the ✈ Trip button on the Chores card. It is
# stored as one away_periods row per person (the overlay every chore path
# already reads), so nothing downstream changes. Rows that share a start and
# end date and are still live (not yet over) are shown as one trip.

def _period_live(r, today_s: str) -> bool:
    """Still matters: running now or planned for later (not already over)."""
    return r["end_date"] is None or r["end_date"] >= today_s


def _spans_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Inclusive date-string spans; a None end runs forever."""
    return ((b_end is None or a_start <= b_end)
            and (a_end is None or b_start <= a_end))


def _trip_rows(c, today: dt.date) -> list[dict]:
    """Every live trip, soonest first: {ids, person_ids, people, start_date,
    end_date, backup_person_id, backup_mixed, state}. state is "now" once it
    has started, else "planned". backup_person_id is the shared backup;
    when the travellers have different backups it is None and backup_mixed is
    True, so an edit can keep them instead of wiping them. `people` names
    every traveller, including one deactivated since, so the sheet can show
    (and untick) them."""
    today_s = today.isoformat()
    everyone = {p["id"]: p for p in fdb.list_people(c, include_inactive=True)}
    groups: dict[tuple, list[dict]] = {}
    for r in fdb.list_away_periods(c):
        if _period_live(r, today_s):
            groups.setdefault((r["start_date"], r["end_date"]), []).append(r)
    trips = []
    for (start, end), rows in groups.items():
        backups = {r["backup_person_id"] for r in rows}
        mixed = len(backups) > 1
        trips.append({
            "ids": [r["id"] for r in rows],
            "person_ids": [r["person_id"] for r in rows],
            "people": [{"id": r["person_id"],
                        "name": everyone.get(r["person_id"], {}).get("name") or "?",
                        "color": everyone.get(r["person_id"], {}).get("color") or "",
                        "active": bool(everyone.get(r["person_id"], {}).get("active"))}
                       for r in rows],
            "start_date": start,
            "end_date": end,
            "backup_person_id": None if mixed else backups.pop(),
            "backup_mixed": mixed,
            "state": "now" if start <= today_s else "planned",
        })
    trips.sort(key=lambda t: (t["start_date"], t["end_date"] or "9999"))
    return trips


def _end_period_change(r, today: dt.date):
    """How "back home" ends one period: a period that has not started yet (or
    started today, so never took effect) is removed; one already running ends
    yesterday, so today's chores come back; one already over is left alone.
    Returns ("delete", id), ("update", (id, fields)) or None."""
    today_s = today.isoformat()
    if r["start_date"] >= today_s:
        return ("delete", r["id"])
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    if r["end_date"] is not None and r["end_date"] <= yesterday:
        return None
    return ("update", (r["id"], {"end_date": yesterday}))


def _periods_by_id(c, ids: list[int]) -> dict[int, dict]:
    rows = {}
    for i in ids:
        r = fdb.get_away_period(c, i)
        if r is None:
            raise HTTPException(404, "unknown away period")
        rows[i] = r
    return rows


@app.post("/api/trip")
def trip_save(t: TripIn, request: Request):
    """Start, plan or edit a trip in one transaction. Editing (ids given)
    keeps each staying traveller's row (history intact), ends the rows of
    anyone taken off the trip the way "back home" does, and adds rows for
    anyone added. Leaving backup_person_id out of an edit keeps each
    traveller's own backup (an explicit null clears it)."""
    c = _db()
    today = _today()
    if not t.person_ids:
        raise HTTPException(422, "pick at least one person")
    if len(set(t.person_ids)) != len(t.person_ids):
        raise HTTPException(422, "a person is listed twice")
    start = _valid_date(t.start_date) if t.start_date else today.isoformat()
    end = _valid_date(t.end_date) if t.end_date else None
    if end is not None and end < start:
        raise HTTPException(422, "the back date must be after the leaving date")
    # A trip whose last away day is already past would be saved, never shown
    # (only live trips are), and quietly rewrite old days. Ending a trip now
    # is Back early's job.
    if end is not None and end < today.isoformat():
        raise HTTPException(
            422, "that trip is already over; use Back early to end a trip now")
    existing = _periods_by_id(c, t.ids or [])
    if len({r["person_id"] for r in existing.values()}) != len(existing):
        raise HTTPException(422, "those trip ids list a person twice")
    keep_backup = t.ids and "backup_person_id" not in t.model_fields_set
    names = {p["id"]: p["name"]
             for p in fdb.list_people(c, include_inactive=True)}
    kept = {r["person_id"]: r for r in existing.values()}
    for pid in t.person_ids:
        row = _person_row(c, pid)                   # 404 if unknown
        # Someone already on this trip may stay on it while inactive (their
        # row is history); nobody inactive is newly added to one.
        if not row["active"] and pid not in kept:
            raise HTTPException(422, f"{row['name']} is not active")
    # The backups this save will leave in place: the one given, or (keeping)
    # each staying traveller's own. Every one must be home for the whole trip,
    # whichever way it got there, or their covered chores pause silently.
    if keep_backup:
        backups = {r["backup_person_id"] for r in existing.values()
                   if r["person_id"] in t.person_ids} - {None}
    else:
        backups = {t.backup_person_id} - {None}
        if t.backup_person_id is not None:
            b = _person_row(c, t.backup_person_id)  # 404 if unknown
            if not b["active"]:
                raise HTTPException(422, "backup must be an active person")
    for b in backups:
        if b in t.person_ids:
            raise HTTPException(
                422, f"{names.get(b, 'the backup')} is a backup on this trip, "
                     "so they have to stay home")
    others = [r for r in fdb.list_away_periods(c) if r["id"] not in existing]
    for r in others:
        if (r["person_id"] in t.person_ids
                and _spans_overlap(start, end, r["start_date"], r["end_date"])):
            raise HTTPException(
                409, f"{names.get(r['person_id'], 'someone')} is already away then")
        if (r["person_id"] in backups
                and _spans_overlap(start, end, r["start_date"], r["end_date"])):
            raise HTTPException(
                422, f"the backup, {names.get(r['person_id'], 'someone')}, "
                     "is away during this trip")
    inserts, updates, deletes = [], [], []
    for pid in t.person_ids:
        fields = {"start_date": start, "end_date": end}
        if not keep_backup:
            fields["backup_person_id"] = t.backup_person_id
        if pid in kept:
            updates.append((kept[pid]["id"], fields))
        else:
            inserts.append({"person_id": pid, **fields})
    for pid, r in kept.items():
        if pid in t.person_ids:
            continue
        change = _end_period_change(r, today)
        if change and change[0] == "delete":
            deletes.append(change[1])
        elif change:
            updates.append(change[1])
    _guard_away(c, request, inserts=inserts, updates=updates, deletes=deletes)
    fdb.apply_away_changes(c, inserts=inserts, updates=updates, deletes=deletes)
    return {"trips": _trip_rows(c, today)}


@app.post("/api/trip/end")
def trip_end(t: TripEndIn, request: Request):
    """Back early, or cancel a planned trip: every named period ends the way
    "back home" does (see _end_period_change), all in one transaction."""
    c = _db()
    today = _today()
    if not t.ids:
        raise HTTPException(422, "no trip given")
    updates, deletes = [], []
    for r in _periods_by_id(c, t.ids).values():
        change = _end_period_change(r, today)
        if change and change[0] == "delete":
            deletes.append(change[1])
        elif change:
            updates.append(change[1])
    _guard_away(c, request, updates=updates, deletes=deletes)
    fdb.apply_away_changes(c, updates=updates, deletes=deletes)
    return {"trips": _trip_rows(c, today)}


# --- calendar + tiles -----------------------------------------------------

# The window the WALL asks for on every calendar load — `fetchCalWindow` in
# hub.js fetches exactly these numbers. Named here so a static guard can pin the
# two together in BOTH directions, because each is silent on its own: a fetch
# above the ceiling 422s the calendar on every load, and a fetch BELOW the
# configured window silently caps the reported window, hatching days that are
# cached — the very bug that widening this window set out to fix.
CAL_FETCH_DAYS = 400
CAL_FETCH_PAST = 45

# Hard ceiling on a single /api/calendar fetch, forward or back. A huge value
# would overflow the date math into a 500.
CAL_MAX_DAYS = 400

# Not fatal (the window stays honest — those days just render "not synced"), but
# it IS the state where the wall hatches days for a reason no banner explains, so
# say it once at startup rather than leaving the operator to infer it from their
# config file. BOTH directions: the static guard pins the shipped configs, but an
# existing install's private config.json is the one file it can never see, and
# that is the only place this misconfiguration actually lives.
for _key, _have, _want in (
        ("calendar_window_days", cfg.calendar_window_days, CAL_FETCH_DAYS),
        ("calendar_past_days", cfg.calendar_past_days, CAL_FETCH_PAST)):
    if _have < _want:
        log.warning(
            "%s=%d is below the %d days the wall fetches; days beyond it will "
            "show as 'not synced'. Raise it in config.json.",
            _key, _have, _want)


@app.get("/api/calendar")
def calendar(days: int = CAL_FETCH_DAYS, past: int = CAL_FETCH_PAST):
    if not (0 <= days <= CAL_MAX_DAYS and 0 <= past <= CAL_MAX_DAYS):
        raise HTTPException(422, "days/past out of range")
    c = _db()
    with _available_once():
        return _calendar_block(c, _today(), days, past_days=past)


@app.post("/api/calendar/sync")
def calendar_sync_now():
    """The wall's refresh button calls this just before it reloads the page: one
    pass of the same sync the background loop runs (Google and ICS, then iCloud),
    so the reload shows current data rather than whatever the 5-minute loop last
    fetched. Always answers 200 (the page reloads regardless): {"synced": true},
    or {"synced": false, "reason": ...} where reason is demo, cooldown, busy (a
    sync was already running and did not let go in time) or error (with
    needs_auth when the Google sign-in has expired). `synced` speaks for Google
    and ICS; iCloud syncs in the same pass but reports through its own status in
    Settings. A failed sync still uses the cooldown; a busy one does not. It does
    not bypass sync_once's empty-calendar guard, so a calendar emptied in Google
    can still take up to a day to clear."""
    global _sync_now_last
    if DEMO:
        return {"synced": False, "reason": "demo"}
    with _sync_now_gate:
        now = time.monotonic()
        if now - _sync_now_last < SYNC_NOW_COOLDOWN_S:
            return {"synced": False, "reason": "cooldown"}
        previous, _sync_now_last = _sync_now_last, now
    conn = None
    try:
        _db()   # runs the one-time schema setup if this is the process's first request
        # its own connection, like the loop's: _sync_all may close and replace it
        conn = fdb.connect(DB_PATH)
        conn, status = _sync_all(GoogleCalendarClient(TOKEN_PATH), conn, cfg,
                                 lock_wait=SYNC_NOW_LOCK_WAIT_S)
    except Exception:
        log.exception("refresh-button sync failed")
        return {"synced": False, "reason": "error", "needs_auth": False}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                log.warning("refresh-button sync: closing its DB connection failed",
                            exc_info=True)
    if status.get("busy"):
        with _sync_now_gate:
            # nothing ran, so nothing to cool down from (unless a newer tap
            # has stamped the gate since; leave that one alone)
            if _sync_now_last == now:
                _sync_now_last = previous
        log.info("refresh-button sync skipped: a calendar sync was already running")
        return {"synced": False, "reason": "busy"}
    if not status.get("ok"):
        log.warning("refresh-button sync did not succeed: %s",
                    status.get("error") or "no status")
        return {"synced": False, "reason": "error",
                "needs_auth": bool(status.get("needs_auth"))}
    return {"synced": True}


@app.get("/api/tiles/climate")
async def tile_climate():
    if DEMO:
        return fdemo.demo_climate()   # canned rooms; no feed hit
    return await tiles.climate_tile(_http, cfg)


@app.get("/api/tiles/weather")
async def tile_weather():
    if DEMO:
        return fdemo.demo_weather()   # canned forecast; no feed hit
    return await tiles.weather_tile(_http, cfg)


@app.get("/api/tiles/fleet")
async def tile_fleet():
    if DEMO:
        return fdemo.demo_fleet()   # canned rollup; no fleet console hit
    return await tiles.fleet_tile(_http, cfg)


# --- laundry: annotation, background watcher, live stream ------------------
#
# The laundry pipeline is PUSH-shaped end to end: LG ThinQ pushes into Home
# Assistant within seconds (verified live 2026-08-18 — the remaining-time
# sensor retimes mid-cycle), a background watcher here re-reads HA every
# LAUNDRY_WATCH_S and keeps an annotated snapshot, and /api/laundry/stream
# pushes each change to open walls over SSE. Before the watcher, the server
# only looked at HA when a browser polled (60s cadence, 25s cache): every
# status change took up to ~85s to reach the wall, and with no wall open the
# cycle log + completion memory observed nothing at all.

LAUNDRY_WATCH_S = 5.0
# A running status that changed within this many minutes of our first sight
# of the cycle counts as a start we watched (see _laundry_annotate). Wide
# enough for a watcher that was a few ticks late, far narrower than any
# sub-status (sensing ~1 min is the only one this short, and it IS the start).
LAUNDRY_START_EXACT_MIN = 2.0
# How stale the persisted "last watcher tick" may get before it is rewritten.
# Well under LAUNDRY_START_EXACT_MIN, so the gap check above stays meaningful.
LAUNDRY_TICK_PERSIST_S = 30.0
# The placeholder finish only appears in a cycle's first minutes (live
# history: 1 to 6 min); past this, a disagreement is the machine revising
# its own estimate and is left alone.
LAUNDRY_PLACEHOLDER_WINDOW_MIN = 15.0
# ...and it is always a tiny time-left (1 or 6 min seen). A washer whose
# load sensing cuts a long default course short reports a real, large time
# left, which must never be pushed back out to the default.
LAUNDRY_PLACEHOLDER_MAX_LEFT_MIN = 10.0
# A snapshot older than this is treated as absent (watcher disabled or
# wedged) and the tile route falls back to fetching HA inline, exactly the
# pre-watcher behavior — the card must never go dark because a background
# task died. Three missed ticks means genuinely stuck, not just busy.
LAUNDRY_SNAPSHOT_FRESH_S = 15.0
# At a 5s cadence a transient HA blip is far more likely to land on a tick
# than under the old 60s poll, and an instant available:false push would
# flicker "Laundry unavailable" across the kitchen for a hiccup that heals
# itself. Hold the last good card this long before going honestly
# unavailable (the frontend's own last-good discipline, TILE_FAIL_LIMIT,
# covers fetch failures the same way).
LAUNDRY_UNAVAIL_HOLD_S = 30.0
# ...and how long it must stay unavailable before the log says so once, loudly.
# Comfortably longer than a Home Assistant restart (tens of seconds) so a
# routine HA update is never called an incident, short enough that a revoked
# token or a renamed entity is named the same morning it happens.
LAUNDRY_UNAVAIL_ALERT_S = 300.0

_laundry_snapshot: dict | None = None
_laundry_snapshot_ts: float = 0.0
# Wall-clock time of the watcher's last Home Assistant read that came back
# available, for /health/full. Unlike _laundry_snapshot_ts it is NOT
# re-stamped while the hold keeps a last-good card standing through a blip:
# it says when HA last really answered.
_laundry_last_ok_wall: float | None = None
# The running watcher task (set by the lifespan), so /health/full can tell a
# watcher that is running from one that was never started or has died.
_laundry_watch_task: "asyncio.Task | None" = None
_laundry_unavail_since: float | None = None
# One-shot latch for the prolonged-outage ERROR below: a watcher ticking every
# 5s must not reprint it 12 times a minute, and it re-arms on recovery.
_laundry_unavail_alerted: bool = False
# TWO clocks, deliberately. `_laundry_unavail_since` above drives what the WALL
# shows and resets on the first good tick, because the hold exists to stop the
# card flickering. `_laundry_alert_since` drives the LOG and only resets after
# sustained recovery: a feed that works one tick in twenty would otherwise
# reset the display clock often enough to never escalate, flapping all day
# with nothing above DEBUG to show for it.
_laundry_alert_since: float | None = None
_laundry_ok_streak: int = 0
LAUNDRY_RECOVERY_TICKS = 3
# Per-machine outage tracking. `available` is an OR across machines, so one
# healthy dryer keeps the tile available while a renamed or deleted washer
# entity reads "offline" forever: no escalation, no status change, nothing but
# a dash on the wall. These give a single stuck machine the same treatment the
# whole-feed outage gets.
_laundry_machine_offline_since: dict[str, float] = {}
_laundry_machines_stuck: set[str] = set()
# Consecutive failures of the annotation step (kv completion memory + cycle
# log). It cannot make the card disappear, but it CAN make a finished load
# render as a bare "Idle", so it is not allowed to fail quietly forever, and
# at one tick every 5s it is not allowed to shout on every tick either.
_laundry_annotate_failures: int = 0
LAUNDRY_ANNOTATE_STRIKES = 6
# The change signal for SSE waiters: each CHANGED tick swaps in a fresh
# Event and sets the old one, so every waiter wakes exactly once per change
# and re-arms on the new event. (Bound to the running loop at wait time;
# in production the watcher and the stream handlers share the app's loop.)
_laundry_change: asyncio.Event = asyncio.Event()


def _laundry_env_broken() -> bool:
    """Laundry is configured in config.json but the process has no usable
    HA_TOKEN: every read fails and the card can only ever say "unavailable".

    This is what a lost env file looks like from inside the app: on 2026-09-17
    a deploy box's went missing and the container came up with an empty token,
    so it gets a loud startup line instead of a dead tile nobody can explain.

    DEMO serves canned laundry with no HA at all (_laundry_payload), so a demo
    wall is not broken and must not be shouted at, the same carve-out
    _laundry_watch_enabled makes via _sync_enabled(). One predicate for "no
    token" (integrations.laundry_needs_auth) so the log line and the settings
    row can never disagree about it."""
    return not DEMO and fintegrations.laundry_needs_auth(cfg, os.environ)


def _laundry_watch_enabled() -> bool:
    """The watcher runs only where the calendar sync would (not in DEMO, not
    under DISABLE_SYNC) and only with laundry actually configured — an
    unconfigured hub must not poll a nonexistent HA every 5s forever."""
    return _sync_enabled() and bool(getattr(cfg, "laundry", None))


async def _laundry_watch_tick() -> None:
    """One watcher iteration: fetch from HA, run the transition synthesis
    (kv completion memory + cycle log — see _laundry_annotate), publish the
    snapshot, and wake stream waiters iff the payload changed. Every failure
    is soft: log and leave the previous snapshot standing for the next tick.

    Soft must not mean silent, so three lanes escalate here, each latched so a
    5s cadence cannot turn a real problem into a log flood that buries it:
    the whole feed being unavailable, one machine stuck offline while the
    others report, and the annotation step failing (which silently downgrades
    a finished load to a bare "Idle")."""
    global _laundry_snapshot, _laundry_snapshot_ts, _laundry_unavail_since, \
        _laundry_change, _laundry_unavail_alerted, _laundry_ok_streak, \
        _laundry_annotate_failures, _laundry_alert_since, _laundry_last_ok_wall
    try:
        t = await tiles.laundry_tile(_http, cfg, os.environ.get("HA_TOKEN", ""))
    except Exception:
        log.warning("laundry watch: fetch failed; retrying on cadence",
                    exc_info=True)
        return
    if t.get("available"):
        _laundry_last_ok_wall = time.time()
        try:
            snap = await _laundry_annotate_off_loop(t)
            if _laundry_annotate_failures >= LAUNDRY_ANNOTATE_STRIKES:
                log.warning("laundry: completion history is working again")
            _laundry_annotate_failures = 0
        except Exception:
            _laundry_annotate_failures += 1
            if _laundry_annotate_failures == LAUNDRY_ANNOTATE_STRIKES:
                log.error("laundry: the completion history has failed %d ticks "
                          "in a row. The card still renders, but a load that "
                          "finished and powered itself off will read 'Idle' "
                          "instead of 'Done at ...' until this is fixed.",
                          _laundry_annotate_failures, exc_info=True)
            elif _laundry_annotate_failures < LAUNDRY_ANNOTATE_STRIKES:
                log.warning("laundry: completion history failed this tick",
                            exc_info=True)
            snap = t   # the live card, minus the synthesized history
    else:
        snap = t
    now = time.monotonic()
    if snap.get("available"):
        _laundry_unavail_since = None          # the card is live again, now
        _laundry_ok_streak += 1
        # ...but the ALERT clock needs sustained recovery, or a feed that
        # succeeds one tick in twenty resets it forever and never escalates.
        if _laundry_ok_streak >= LAUNDRY_RECOVERY_TICKS:
            if _laundry_unavail_alerted:
                log.warning("laundry: the feed recovered after being "
                            "unavailable")
            _laundry_alert_since = None
            _laundry_unavail_alerted = False
    else:
        _laundry_ok_streak = 0
        if _laundry_unavail_since is None:
            _laundry_unavail_since = now
        if _laundry_alert_since is None:
            _laundry_alert_since = now
        # A card that says "unavailable" forever is the incident this exists
        # for: a revoked HA token, a renamed entity, an HA that never came
        # back. Each of those logs at most one WARNING per entity in tiles.py
        # ("quiet until it recovers"), which reads exactly like HA rebooting
        # for 20 seconds. One ERROR once the outage is minutes long tells the
        # two apart, and the recovery line above closes it.
        #
        # ORDER IS LOAD-BEARING: this sits ABOVE the hold's early return, or a
        # long outage that still has a last-good card standing would never
        # escalate at all. (LAUNDRY_UNAVAIL_ALERT_S > LAUNDRY_UNAVAIL_HOLD_S,
        # pinned by a test, so the card is honestly "unavailable" by the time
        # this fires and the message cannot contradict the screen.)
        if (not _laundry_unavail_alerted
                and now - _laundry_alert_since >= LAUNDRY_UNAVAIL_ALERT_S):
            _laundry_unavail_alerted = True
            # TWO numbers, because they mean different things and have
            # different fixes: the alert clock survives short recoveries, so a
            # feed that comes up for a tick and drops again would otherwise be
            # reported as one long outage the wall never actually showed.
            log.error("laundry: the feed has been failing for %ds and is "
                      "unavailable now (this stretch: %ds). If those numbers "
                      "differ it is flapping, not down. Check that Home "
                      "Assistant is up, that HA_TOKEN is still valid (a "
                      "revoked token never recovers), and that the configured "
                      "entities still exist.",
                      int(now - _laundry_alert_since),
                      int(now - _laundry_unavail_since))
        if (now - _laundry_unavail_since < LAUNDRY_UNAVAIL_HOLD_S
                and _laundry_snapshot is not None
                and _laundry_snapshot.get("available")):
            # brief blip: keep the last good card standing (re-stamped fresh
            # so the route keeps serving it); honest once the hold expires
            _laundry_snapshot_ts = now
            return
    _laundry_watch_machines(snap, now)
    changed = snap != _laundry_snapshot
    _laundry_snapshot = snap
    _laundry_snapshot_ts = now
    if changed:
        waiters, _laundry_change = _laundry_change, asyncio.Event()
        waiters.set()


def _laundry_watch_machines(snap: dict, now: float) -> None:
    """Per-machine outage escalation. `available` is an OR across machines, so
    a washer whose entity was renamed reads `offline` forever while the dryer
    keeps the tile healthy: no whole-feed alert, no status change, just a dash
    on the wall that nobody can explain. One ERROR per machine once it has
    been offline as long as a whole-feed outage would need, cleared (and
    re-armed) when it reports again."""
    if not snap.get("available"):
        # The whole-feed lane owns this case, so nothing is judged here. The
        # per-machine clocks are LEFT RUNNING on purpose: clearing them meant a
        # machine offline for hours never escalated as long as the feed dropped
        # often enough to keep resetting it, which is the same starvation the
        # two-clock split fixed on the feed lane.
        return
    seen = set()
    for m in snap.get("machines") or []:
        mid = m.get("id")
        if not mid:
            continue
        seen.add(mid)
        if m.get("phase") != "offline":
            if mid in _laundry_machines_stuck:
                _laundry_machines_stuck.discard(mid)
                log.warning("laundry: %s is reporting again", mid)
            _laundry_machine_offline_since.pop(mid, None)
            continue
        since = _laundry_machine_offline_since.setdefault(mid, now)
        if (mid not in _laundry_machines_stuck
                and now - since >= LAUNDRY_UNAVAIL_ALERT_S):
            _laundry_machines_stuck.add(mid)
            log.error("laundry: %s has been offline for %ds while the rest of "
                      "the card works. Its Home Assistant entities are most "
                      "likely renamed or gone (config.json names them); the "
                      "wall shows a dash for it until they are fixed.",
                      mid, int(now - since))
    for gone in set(_laundry_machine_offline_since) - seen:
        _laundry_machine_offline_since.pop(gone, None)
        _laundry_machines_stuck.discard(gone)


async def laundry_watch_loop() -> None:
    # The tick guards its own fetch/annotate, but the loop still armors the
    # whole body: an exception escaping the tick would otherwise kill this
    # task PERMANENTLY and silently — the lifespan holds the task reference,
    # so asyncio's "exception was never retrieved" warning never fires, and
    # the route's inline fallback keeps the card looking healthy while the
    # cycle log + completion memory quietly stop observing (the exact
    # pre-watcher regression this loop exists to fix). CancelledError must
    # pass through: it's the lifespan's shutdown signal, not a failure.
    while True:
        try:
            await _laundry_watch_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("laundry watch: tick crashed; continuing")
        await asyncio.sleep(LAUNDRY_WATCH_S)


async def _laundry_payload() -> dict:
    """The current laundry tile: demo when canned, the watcher's snapshot
    while fresh, else an inline fetch+annotate — the pre-watcher path, kept
    both as the no-watcher mode (tests, DISABLE_SYNC) and as the fallback
    that keeps the card alive if the watcher ever wedges."""
    if DEMO:
        return fdemo.demo_laundry()   # canned machines; no HA hit
    if (_laundry_snapshot is not None
            and time.monotonic() - _laundry_snapshot_ts < LAUNDRY_SNAPSHOT_FRESH_S):
        return _laundry_snapshot
    t = await tiles.laundry_tile(_http, cfg, os.environ.get("HA_TOKEN", ""))
    if not t.get("available"):
        return t
    return await _laundry_annotate_off_loop(t)


@app.get("/api/tiles/laundry")
async def tile_laundry():
    return await _laundry_payload()


# How long a stream waiter sleeps before emitting a keepalive comment. Under
# the 60s idle timeouts of the proxies/browsers that might sit between a
# phone and the hub; also the cadence at which a waiter re-checks the
# payload itself, so a stream opened while the watcher is disabled (or a
# change that raced the waiter's re-arm) still converges on the truth.
LAUNDRY_STREAM_PING_S = 20.0


@app.get("/api/laundry/stream")
async def laundry_stream():
    """Server-sent events: the annotated laundry tile, pushed. Emits the
    current payload immediately on connect (the wall paints without waiting
    for a change), then a new event whenever the watcher observes one, with
    `: ping` keepalives between. The payload is exactly /api/tiles/laundry's
    — the frontend feeds both through the same applyLaundry. EventSource
    handles reconnection natively, and the 60s poll remains as the fallback
    for anything that can't hold a stream open."""
    async def gen():
        if DEMO:
            # canned machines never transition — greet once, then only
            # keepalives. Re-reading demo_laundry() would re-time every
            # payload and push a "change" each cycle, and each push is an
            # innerHTML rebuild that restarts the tumble mid-spin on the
            # demo wall (the churn applyLaundry exists to prevent).
            yield f"data: {json.dumps(fdemo.demo_laundry())}\n\n"
            while True:
                await asyncio.sleep(LAUNDRY_STREAM_PING_S)
                yield ": ping\n\n"
        last = None
        while True:
            waiter = _laundry_change   # arm BEFORE reading: no lost wakeup
            try:
                payload = await _laundry_payload()
                data = json.dumps(payload)
            except Exception:
                # fail-soft like every tile read: hold the stream open and
                # heartbeat; the next good read pushes the recovery
                log.warning("laundry stream: payload read failed",
                            exc_info=True)
                data = None
            if data is not None and data != last:
                last = data
                yield f"data: {data}\n\n"
            else:
                yield ": ping\n\n"
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter.wait(),
                                       timeout=LAUNDRY_STREAM_PING_S)
    # X-Accel-Buffering: today the hub serves browsers directly, but the
    # moment any reverse proxy lands in front, response buffering would
    # queue these events indefinitely and the stream would "work" while
    # delivering nothing — cheap to preempt now.
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# One annotation at a time. On the event loop the watcher and the route's
# inline fallback were serialized for free; in worker threads (each with its
# own per-thread connection) two of them could both read the same previous
# phase and log the same transition twice.
_laundry_annotate_lock = threading.Lock()


def _laundry_annotate_serial(t: dict) -> dict:
    with _laundry_annotate_lock:
        return _laundry_annotate(t)


async def _laundry_annotate_off_loop(t: dict) -> dict:
    """Run _laundry_annotate in a worker thread. It does blocking SQLite reads
    and commits (with a 5s busy timeout), and on the event loop every tick of
    the 5s watcher stalled every open stream and async request with it."""
    return await asyncio.to_thread(_laundry_annotate_serial, t)


def _laundry_annotate(t: dict) -> dict:
    # Completion memory: a machine sitting in "end" carries WHEN it finished
    # (status_since). Stamp that into the kv store so "finished at 2:14"
    # survives the machine being opened / powered off — and later restarts of
    # this server — then attach the remembered stamp to every machine.
    #
    # Stamp ONLY on an observed TRANSITION (previous phase kept in kv): into
    # done from neither done nor offline, or — the missed-finish path — from
    # running/paused straight to idle (see the elif below). status_since is
    # HA's last_changed, which resets on an HA restart or a
    # done→unavailable→done cloud blip — naive re-stamping would overwrite
    # the real 9:02pm finish with the 3am restart time. A genuine new cycle
    # always passes through running/idle first; a blip or restart never does
    # (it reads done→offline→done, or done→done with a moved last_changed —
    # both refused here).
    #
    # The tile itself is cached in-process (tiles._laundry_cache), so copy
    # before annotating: the cached dict must stay un-mutated. And the kv
    # work must never 500 an otherwise scrupulously fail-soft endpoint: on
    # any DB hiccup, serve the tile un-annotated and say so in the log.
    machines = [dict(m) for m in t.get("machines", [])]
    try:
        c = _db()
        # Was the hub watching just now? The previous annotate pass left its
        # time here; a gap longer than the start window (a restart, a
        # wedged watcher) means a "fresh" status change can't be trusted to
        # be a cycle's start (see the start stamp below).
        last_tick = _laundry_dt(fdb.kv_get(c, "laundry_last_tick"))
        now_utc = dt.datetime.now(dt.timezone.utc)
        watching = (last_tick is not None and
                    (now_utc - last_tick).total_seconds() / 60
                    <= LAUNDRY_START_EXACT_MIN)
        # Refresh the stamp only once it is LAUNDRY_TICK_PERSIST_S old, not
        # on every 5s tick (that alone was ~17k commits a day). The stored
        # time then trails the real last tick by at most that much, so a
        # gap can only read slightly LONGER than it was: the safe direction
        # (not a watched start), and far inside the 2 min window.
        if (last_tick is None or last_tick > now_utc
                or (now_utc - last_tick).total_seconds()
                >= LAUNDRY_TICK_PERSIST_S):
            fdb.kv_set(c, "laundry_last_tick", now_utc.isoformat())
        for m in machines:
            done_key = f"laundry_done_{m['id']}"
            phase_key = f"laundry_phase_{m['id']}"
            missed_key = f"laundry_missed_{m['id']}"
            nonoff_key = f"laundry_nonoff_{m['id']}"
            start_key = f"laundry_start_{m['id']}"
            # A washer's finished load is WET and waits to be moved; the key
            # holds the finish moment until something shows it was moved (see
            # _laundry_present_waiting). Washers only: a dry load isn't urgent.
            wait_key = (f"laundry_wait_{m['id']}"
                        if m.get("kind") == "washer" else None)
            phase = m.get("phase")
            prev = fdb.kv_get(c, phase_key)
            # Provenance across an HA blip: prev collapses to "offline"
            # while HA is blind, losing what the machine was DOING before —
            # so the last non-offline phase is tracked in its own key. A
            # finish straddled by a blip (running -> offline -> idle) is
            # still a finish and gets the full missed-done treatment below;
            # a blip on an idle or freshly-emptied machine is still nothing.
            came_from = (prev if prev != "offline"
                         else fdb.kv_get(c, nonoff_key))
            note = None
            if (phase == "done" and m.get("status_since")
                    and prev not in ("done", "offline")
                    and fdb.kv_get(c, done_key) != m["status_since"]):
                fdb.kv_set(c, done_key, m["status_since"])
                if wait_key:
                    fdb.kv_set(c, wait_key, m["status_since"])
            elif phase == "idle" and came_from in ("running", "paused"):
                # The missed finish: LG machines auto-power-off a minute or
                # two after "end", so a 60s poll can watch running -> power_off
                # and never see done at all — leaving the wall on a bare
                # "Idle" with no completion memory (live board, 2026-08-17).
                # A machine that WAS in a cycle (directly, or across an HA
                # blip — came_from) and now reports idle has ended it. From
                # RUNNING with the projection recently passed (within the
                # Done-hold window — any staler and it's likely a LATCHED
                # previous-cycle value from a flaky remaining-time sensor,
                # not this load's finish) = a genuine finished load: stamp
                # that exact moment AND remember it as a missed done
                # (presented below as the real Done it was). Anything else —
                # a canceled cycle (projection still future), a stale
                # projection (warned, refused), or an exit from PAUSED
                # (pause freezes the drum while the projection keeps aging,
                # so a "past" projection there is fiction) — stamps only the
                # moment the machine left the cycle, and never fakes a Done.
                mt = tiles._laundry_minutes_to(m.get("finishes_at"))
                if (came_from == "running" and mt is not None
                        and -tiles.LAUNDRY_MISSED_DONE_HOLD_MIN <= mt <= 0):
                    fdb.kv_set(c, done_key, m["finishes_at"])
                    fdb.kv_set(c, missed_key, m["finishes_at"])
                    if wait_key:
                        fdb.kv_set(c, wait_key, m["finishes_at"])
                    note = "missed_finish"
                else:
                    if came_from == "running" and mt is not None and mt < 0:
                        log.warning(
                            "laundry %s: left its cycle with a stale finish "
                            "projection (%s); stamping the exit moment "
                            "instead", m["id"], m["finishes_at"])
                        note = "stale_projection"
                    else:
                        # cancel (future projection), a paused exit, or an
                        # exit with no usable projection at all
                        note = "cycle_exit"
                    if m.get("status_since"):
                        fdb.kv_set(c, done_key, m["status_since"])
                if prev == "offline":
                    note += "+offline_bridge"
            elif (phase == "idle" and came_from == "done"
                    and m.get("status") in ("power_off",
                                            "frozen_prevent_initial")):
                # The OBSERVED-finish twin of the missed-done hold: these LG
                # machines turn THEMSELVES off 30-90s after "end" (measured
                # live 2026-08-18: 29s and 79s) with the load still inside,
                # and done -> idle(power_off) is that self-act — nobody has
                # been to the machine (frozen_prevent_initial is likewise
                # the machine's own freeze-prevention standby, not a person
                # — grouped here so a cold-day finish doesn't lose its
                # hold). Without a hold here, perfectly catching the finish
                # shows Done for barely a minute while MISSING it holds 30
                # (operator report, 2026-08-18: "washer shows idle instead
                # of done"). Re-present the already-stamped observed end
                # for the same hold window. An exit to "initial" (a person
                # powering the machine on — the one human-contact signal
                # this enum offers) decays instead, both here and mid-hold
                # (see the presentation block below); and a stamp already
                # older than the hold (server blind between end and
                # power-off), unparseable, or missing is REFUSED — never a
                # fresh green Done on a 40-minute-old finish — loudly, like
                # its stale-projection twin: the refusal is exactly what
                # log-based tuning needs to see.
                held = fdb.kv_get(c, done_key)
                mt = tiles._laundry_minutes_to(held) if held else None
                if (mt is not None
                        and -tiles.LAUNDRY_MISSED_DONE_HOLD_MIN <= mt <= 0):
                    fdb.kv_set(c, missed_key, held)
                    note = "auto_off_hold"
                else:
                    log.warning(
                        "laundry %s: powered itself off after an observed "
                        "end but the end stamp (%r) is unusable for a Done "
                        "hold; decaying to idle", m["id"], held)
                    note = "auto_off_refused"
                if prev == "offline":
                    note += "+offline_bridge"
            if phase in ("running", "paused", "reserved", "done", "error"):
                # any sign of machine activity retires the synthetic Done (a
                # real observed done replaces it; a new cycle supersedes it).
                # Deliberately NOT cleared on offline: an HA blip mid-hold
                # must not erase a finish the family hasn't seen yet.
                if fdb.kv_get(c, missed_key):
                    fdb.kv_set(c, missed_key, None)
                # ...and a wait: the washer is in use again, so the old load
                # is out. NOT on "done": that is the finish that just set it.
                if (wait_key and phase != "done"
                        and fdb.kv_get(c, wait_key)):
                    fdb.kv_set(c, wait_key, None)
            if (phase == "running"
                    and came_from not in ("running", "paused", "error")
                    and m.get("status") != "wrinkle_care"):
                # A cycle began. Remembered so a dryer START can tell a
                # waiting washer load that it was moved, and so the
                # placeholder finish LG reports for a cycle's first minutes
                # can be recognized (_laundry_fix_placeholder). Not re-
                # stamped across an HA blip: came_from bridges offline.
                # Wrinkle care is the dryer tumbling a FINISHED load now and
                # then, not a new load, so it never counts as a start.
                #
                # Only a start we WATCHED is kept: the machine came from a
                # rest state AND its status changed moments ago. A hub that
                # was down when the cycle began first sees it mid-rinse,
                # where status_since is the last SUB-status change, not the
                # start: as a start it would push the finish out by however
                # far into the cycle it was, and a dryer seen mid-cycle
                # could "clear" a wash that finished after it really began
                # (review, 2026-09-22). An unwatched start is stored as
                # unknown, and both uses skip it.
                #
                # Three conditions, each closing a real hole (review waves
                # 1 and 2, 2026-09-22): the PREVIOUS observation itself was a
                # rest state (not the offline-bridged came_from: a cycle that
                # began during an HA outage reappears with a fresh
                # last_changed); the hub was watching continuously right up
                # to now (a restart can land moments after a sub-status
                # change); and the status changed moments ago.
                since = _laundry_dt(m.get("status_since"))
                exact = (prev in ("idle", "done", "reserved")
                         and watching
                         and since is not None
                         and abs(tiles._laundry_minutes_to(m["status_since"]))
                         <= LAUNDRY_START_EXACT_MIN)
                fdb.kv_set(c, start_key, m["status_since"] if exact else None)
            if phase and phase != prev:
                # the cycle log records every observed RAW transition (the
                # synthesis below is presentation, never logged as fact) —
                # the evidence base for tuning the finish heuristics and
                # diagnosing any finish the wall got wrong. Logged BEFORE
                # phase_key consumes the transition, with a deliberate
                # failure asymmetry:
                #  - locked/busy (OperationalError) aborts this machine's
                #    pass, so kv AND log retry WHOLE next poll (every kv
                #    write above is idempotent on that retry) — the row is
                #    not lost exactly when the DB is flaky, which is when
                #    the log matters most;
                #  - any other error warns and advances anyway — the
                #    essential completion memory must never be wedged
                #    behind the diagnostic log (SQLITE_FULL can block an
                #    INSERT while in-place kv updates still succeed);
                #  - a failure AFTER the row lands duplicates it next poll
                #    — deliberate: a duplicate is detectable in analysis,
                #    a lost row isn't.
                try:
                    fdb.laundry_log_add(c, m["id"], prev, phase,
                                        m.get("status"), m.get("finishes_at"),
                                        m.get("status_since"), note)
                except sqlite3.OperationalError:
                    raise
                except Exception:
                    log.warning("laundry %s: cycle-log write failed; "
                                "advancing the transition anyway",
                                m["id"], exc_info=True)
                fdb.kv_set(c, phase_key, phase)
            # Write only on change: this runs every 5s per machine, and an
            # unconditional write was ~35k commits a day for a phase that
            # had not moved.
            if (phase and phase != "offline"
                    and fdb.kv_get(c, nonoff_key) != phase):
                fdb.kv_set(c, nonoff_key, phase)
            if phase == "idle":
                # A person powering the machine on is the one human-contact
                # signal this status enum offers — and it arrives as a
                # STATUS change inside phase "idle" (power_off -> initial),
                # never as a phase transition, so it must be honored here by
                # current status or a standing hold keeps glowing green for
                # the rest of its window while someone is at the machine
                # emptying it (caught in review, 2026-08-18: the branch
                # comment claimed this and the code didn't do it).
                if m.get("status") == "initial" and (
                        fdb.kv_get(c, missed_key)
                        or (wait_key and fdb.kv_get(c, wait_key))):
                    # ...and the clear is LOGGED — the one status-keyed row
                    # in a phase-keyed log (the note says so in db.py): a
                    # hold a person killed at minute 3 and one that ran its
                    # full window must be tellable apart, because the
                    # collection moment is the number that sizes the hold.
                    # Which note depends on what was LIVE, not on which key
                    # exists: the missed key outlives its window until the
                    # machine is next used, so after the window a power-on
                    # ends the washer's wait, not the Done hold.
                    # Same failure asymmetry as the transition log above,
                    # and the row lands BEFORE the kv clear so a locked-DB
                    # retry re-runs both.
                    held = fdb.kv_get(c, missed_key)
                    hmt = tiles._laundry_minutes_to(held) if held else None
                    live_hold = (hmt is not None and
                                 -tiles.LAUNDRY_MISSED_DONE_HOLD_MIN <= hmt <= 0)
                    waiting = bool(wait_key and fdb.kv_get(c, wait_key))
                    note = ("hold_cleared_by_power_on" if live_hold
                            else "wait_cleared_by_power_on" if waiting
                            else None)
                    if note:
                        _laundry_log_note(c, m, note)
                    fdb.kv_set(c, missed_key, None)
                    if wait_key:
                        fdb.kv_set(c, wait_key, None)
                # Present a remembered finish (missed, or observed-then-
                # auto-powered-off) as the Done it really was — green ring,
                # "at 9:02pm" — for the hold window, then decay to idle +
                # the quiet "last load" line.
                ms = fdb.kv_get(c, missed_key)
                mt = tiles._laundry_minutes_to(ms) if ms else None
                if mt is not None and -tiles.LAUNDRY_MISSED_DONE_HOLD_MIN <= mt <= 0:
                    m["phase"] = "done"
                    m["status_since"] = ms
            m["last_done"] = fdb.kv_get(c, done_key)
        for present in (_laundry_present_waiting, _laundry_fix_placeholder):
            # Presentation only, and its own failure: last_done is already
            # attached, so the generic warning below would be false. Kv-
            # driven, so it simply retries next tick.
            try:
                present(c, machines)
            except Exception:
                log.warning("laundry: %s failed this tick; serving the "
                            "machines' raw state", present.__name__,
                            exc_info=True)
    except Exception:
        log.warning("laundry: completion-memory kv / cycle-log write failed; "
                    "serving the tile without last_done (the interrupted "
                    "transition retries next poll)", exc_info=True)
        for m in machines:
            m.setdefault("last_done", None)
    return {**t, "machines": machines}


def _laundry_log_note(c, m: dict, note: str) -> None:
    """A status-keyed row in the phase-keyed cycle log (idle -> idle, like
    hold_cleared_by_power_on): how long wet loads really wait, and what ended
    the wait, is the evidence that sizes LAUNDRY_WAIT_MAX_H. Same failure
    asymmetry as the transition log: a locked DB aborts the pass so it
    retries whole; anything else warns and lets the state change proceed."""
    try:
        fdb.laundry_log_add(c, m["id"], "idle", "idle", m.get("status"),
                            m.get("finishes_at"), m.get("status_since"), note)
    except sqlite3.OperationalError:
        raise
    except Exception:
        log.warning("laundry %s: %s log write failed; continuing", m["id"],
                    note, exc_info=True)


def _laundry_dt(iso: str | None) -> dt.datetime | None:
    """An aware datetime from a stored stamp, else None (naive refused)."""
    try:
        t = dt.datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None
    return t if t is not None and t.tzinfo is not None else None


def _laundry_present_waiting(c, machines: list[dict]) -> None:
    """A finished washer load that nobody has moved yet. The real data
    (2026-08-18..09-21, 34 washes) is why this exists: the dryer started a
    median ~100 minutes after the washer finished, often 2 to 5 hours, and
    only 5 of 34 inside the 30-minute Done hold. So the wall read "Idle" for
    hours while wet clothes sat in the drum.

    Once the Done hold is over, a washer with a remembered finish presents as
    "waiting" (status_since = the finish) until something shows the load was
    moved: a DRYER start the hub watched happen after that finish (the load
    went in it; see _laundry_annotate for "watched"), the
    washer was powered on or started again (handled in _laundry_annotate),
    or LAUNDRY_WAIT_MAX_H passed (a load hung up to dry leaves no trace, so
    the claim must expire). A dryer start also ends a still-running Done
    hold: the load is visibly dealt with. Each way out is logged."""
    dryer_starts = [t for t in (
        _laundry_dt(fdb.kv_get(c, f"laundry_start_{m['id']}"))
        for m in machines if m.get("kind") == "dryer") if t is not None]
    for m in machines:
        if m.get("kind") != "washer":
            continue
        wait_key = f"laundry_wait_{m['id']}"
        waited = fdb.kv_get(c, wait_key)
        since = _laundry_dt(waited)
        if waited and since is None:
            log.warning("laundry %s: unusable wait stamp %r; dropping it",
                        m["id"], waited)
            fdb.kv_set(c, wait_key, None)
            continue
        if since is None:
            continue
        if any(t > since for t in dryer_starts):
            _laundry_log_note(c, m, "wait_cleared_by_dryer")
            fdb.kv_set(c, wait_key, None)
            missed_key = f"laundry_missed_{m['id']}"
            if m.get("phase") == "done" and fdb.kv_get(c, missed_key):
                # the synthetic Done hold (the machine itself is off): the
                # load is in the dryer now, so stop calling it waiting-done
                fdb.kv_set(c, missed_key, None)
                m["phase"] = "idle"
                m["status_since"] = None
            continue
        age_h = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 3600
        if age_h > tiles.LAUNDRY_WAIT_MAX_H:
            _laundry_log_note(c, m, "wait_expired")
            fdb.kv_set(c, wait_key, None)
            continue
        if m.get("phase") == "idle":
            m["phase"] = "waiting"
            m["status_since"] = waited


def _laundry_fix_placeholder(c, machines: list[dict]) -> None:
    """LG reports a placeholder finish for the first minutes of a cycle: the
    dryer says "1 minute left" (once, "6 minutes") right after it starts, on
    every load in the live history, before the real projection lands. The
    machine's own cycle length gives it away: a finish that implies more of
    the cycle has ELAPSED than really has since the start can't be right
    (a pause only ever goes the other way). Present start + cycle length
    until the machine reports something consistent. Presentation only: the
    cycle log keeps the raw value."""
    now = dt.datetime.now(dt.timezone.utc)
    for m in machines:
        total = m.get("total_min")
        fin = _laundry_dt(m.get("finishes_at"))
        if m.get("phase") != "running" or not total or fin is None:
            continue
        if (fin - now).total_seconds() / 60 > LAUNDRY_PLACEHOLDER_MAX_LEFT_MIN:
            continue                 # the placeholder is always a tiny "left"
        start = _laundry_dt(fdb.kv_get(c, f"laundry_start_{m['id']}"))
        if start is None or start > now:
            continue
        elapsed = (now - start).total_seconds() / 60
        if elapsed > LAUNDRY_PLACEHOLDER_WINDOW_MIN:
            continue                 # the placeholder only ever opens a cycle
        implied = total - (fin - now).total_seconds() / 60
        if implied - elapsed > tiles.LAUNDRY_PLACEHOLDER_SLACK_MIN:
            m["finishes_at"] = (start + dt.timedelta(minutes=total)).isoformat()


@app.get("/api/laundry/log")
def laundry_log_route(machine: str | None = None, limit: int = 200):
    """The laundry cycle log: observed phase transitions newest-first — the
    evidence base for tuning finish detection (projection accuracy, watcher
    cadence, missed-done hold) and diagnosing any finish the wall got wrong.
    Fail-soft like every tile read: a DB hiccup serves an empty list loudly
    logged, never a 500. A plain `def` so FastAPI runs the blocking sqlite
    read in its thread pool instead of on the event loop."""
    if not (1 <= limit <= 1000):
        # loud 422 like the calendar route — silent truncation is the wrong
        # default for a log someone pages through by hand (the db-side
        # clamp stays as the defensive floor)
        raise HTTPException(422, "limit out of range (1-1000)")
    if DEMO:
        return fdemo.demo_laundry_log()
    try:
        return {"entries": fdb.laundry_log_recent(_db(), machine, limit)}
    except Exception:
        log.warning("laundry: cycle log unavailable", exc_info=True)
        return {"entries": []}


def _camera_stream_names() -> set[str]:
    """The go2rtc streams the wall and phone may show: every camera's src and
    its "hd" twin, from both the wall column (`cameras`) and the Cameras-tab /
    camera-page grid (`camera_page`), so a grid-only camera works too. The
    snapshot probe and the /go2rtc/ proxy serve these and nothing else: no
    free-form proxying. A malformed entry (no "src") is skipped the same way
    _links() skips it, rather than 500-ing over one config typo."""
    names = set()
    for entry in (*cfg.cameras, *cfg.camera_page):
        if not isinstance(entry, dict):
            continue
        for key in ("src", "hd"):
            if isinstance(entry.get(key), str) and entry[key]:
                names.add(entry[key])
    return names


@app.get("/api/tiles/camera.jpg")
async def tile_camera(src: str = "cam"):
    # Only configured streams may be probed (the src and any "hd" twin, from
    # both the wall column and the camera page), nothing else, and no cameras
    # configured means nothing is probe-able.
    if src not in _camera_stream_names():
        raise HTTPException(404, "unknown camera")
    result = await tiles.camera_snapshot(_http, _fetch_cfg, src)
    if result is None:
        raise HTTPException(502, "camera unavailable")
    content, media = result
    return Response(content=content, media_type=media)


# --- go2rtc, through the hub (go2rtc_proxy.py says why) ---------------------

@app.get("/go2rtc/{name}")
async def go2rtc_player(name: str):
    """go2rtc's player page and its two scripts, and nothing else of it."""
    media = go2rtc_proxy.PLAYER_FILES.get(name)
    if media is None or DEMO or not _fetch_cfg.go2rtc_base:
        raise HTTPException(404, "not found")
    return await _go2rtc_get(name, {}, media, pass_4xx=False)


@app.get("/go2rtc/api/hls/{name}")
async def go2rtc_hls(name: str, request: Request):
    """The player's HLS fallback (old iPhones): the files of a session the
    WebSocket below already opened, by its id."""
    if name not in go2rtc_proxy.HLS_FILES or DEMO or not _fetch_cfg.go2rtc_base:
        raise HTTPException(404, "not found")
    params = {k: request.query_params[k] for k in go2rtc_proxy.HLS_PARAMS
              if k in request.query_params}
    return await _go2rtc_get(f"api/hls/{name}", params, None, pass_4xx=True)


async def _go2rtc_get(path: str, params: dict, media: str | None,
                      pass_4xx: bool) -> Response:
    """GET one go2rtc file for the browser. No answer, a 5xx, or (for the
    player files, which must always exist) any status but 200 is a 502, and
    logged: the tiles go black over it. With pass_4xx, go2rtc's own 4xx goes
    to the browser as-is: an HLS session that ended is a 404 the player
    handles by reconnecting."""
    url = f"{_fetch_cfg.go2rtc_base}/{path}"
    try:
        r = await _http.get(url, params=params, timeout=tiles.CAMERA_TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("go2rtc proxy: GET %s failed: %s: %s", url, type(e).__name__, e)
        raise HTTPException(502, "go2rtc did not answer") from None
    if r.status_code != 200 and not (pass_4xx and 400 <= r.status_code < 500):
        log.warning("go2rtc proxy: GET %s answered %s", url, r.status_code)
        raise HTTPException(502, f"go2rtc answered {r.status_code}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=media or r.headers.get("content-type"))


@app.websocket("/go2rtc/api/ws")
async def go2rtc_ws(websocket: WebSocket):
    """The player's WebSocket (WebRTC signaling, or the MSE/MJPEG stream
    itself), piped to go2rtc for a configured stream only."""
    src = websocket.query_params.get("src", "")
    base = "" if DEMO else _fetch_cfg.go2rtc_base
    await go2rtc_proxy.bridge(websocket, base, src, _camera_stream_names())


# --- background sync ------------------------------------------------------

_caldav_client = None
_caldav_client_built = False
# Every CalDAV sync (the background tick and settings' "Test connection") runs
# under this lock. Each uses its own connection, so without it two syncs could
# pull and push the same outbox at once: double PUTs, and one sync's writes
# landing over the other's.
_caldav_sync_lock = threading.Lock()
# How long "Test connection" waits for a background sync that is mid-run.
# Kept well under the wall's 12s request timeout (J_TIMEOUT_MS in common.js),
# so a busy lock reads as "already running", not as a generic failed request.
CALDAV_TEST_WAIT_S = 5
# Every Google/ICS sync (the 5-minute loop and the wall's refresh button) runs
# under this lock, for the same reason as the CalDAV one above: each uses its
# own connection, and two at once would fetch and replace the same rows twice.
_google_sync_lock = threading.Lock()
# The wall's refresh button syncs before it reloads. A second tap inside this
# window just reloads, so mashing it can't hammer Google.
SYNC_NOW_COOLDOWN_S = 15
# How long the button's request waits, in all, for the locks (Google, then
# iCloud) held by a sync already mid-run, before answering "busy" or skipping
# iCloud. Bounded because the request holds one of the shared worker threads,
# and a lock that is never released would otherwise park one per tap. This
# bounds the wait for a lock only: once the request holds it, the sync itself
# takes as long as Google and the ICS feeds take. Under the wall's 10 s wait
# (WALL_SYNC_WAIT_MS in hub.js).
SYNC_NOW_LOCK_WAIT_S = 8
# Makes the button's cooldown check-and-stamp one step, so two taps landing
# together on the thread pool can't both pass it.
_sync_now_gate = threading.Lock()
_sync_now_last = float("-inf")     # time.monotonic() of the last button sync


def _get_caldav_client():
    """The iCloud CalDAV client from the environment, built once and reused so
    its principal connection is cached across ticks. None when no credentials are
    set — the whole CalDAV subsystem is then inert (the feature flag)."""
    global _caldav_client, _caldav_client_built
    if not _caldav_client_built:
        _caldav_client = caldav_service.client_from_env()
        _caldav_client_built = True
    return _caldav_client


def _sync_all(client, conn, cfg, lock_wait=None):
    """One sync pass over every source: Google/ICS (under the Google lock), then
    iCloud CalDAV (under its own lock, never inside the Google one, so a slow
    iCloud can't hold up the next Google sync). Returns (conn, status): the
    connection to use next (a fresh one after a failure) and the Google/ICS
    status dict sync_once recorded, or {"ok": False, "busy": True} when
    `lock_wait` seconds passed without getting the Google lock. `lock_wait` is
    one budget for both locks; None waits as long as it takes (the background
    loop). The status is Google/ICS only: iCloud keeps its own (caldav_status,
    shown in Settings), and a busy iCloud lock just skips that leg this pass.
    Never raises: on failure it logs and self-heals the DB connection, so the
    loop doesn't freeze on a dropped DB handle with no log line (the old bare
    `pass` did; sync_once captures per-source errors itself, but its own final
    kv_set can still raise if the DB went unwritable)."""
    deadline = None if lock_wait is None else time.monotonic() + lock_wait

    def _wait_left():
        return -1 if deadline is None else max(0.0, deadline - time.monotonic())

    try:
        if not _google_sync_lock.acquire(timeout=_wait_left()):
            return conn, {"ok": False, "busy": True}
        try:
            status = sync_once(client, conn, cfg, _now_local())
        finally:
            _google_sync_lock.release()
        # CalDAV runs in the same pass but is isolated: it never raises (records
        # its own caldav_status), and a defensive guard keeps any surprise from
        # disrupting the Google sync's reconnect logic. Inert without credentials.
        try:
            cdav = _get_caldav_client()
            if cdav is not None:
                if _caldav_sync_lock.acquire(timeout=_wait_left()):
                    try:
                        caldav_sync.sync_once(cdav, conn, cfg, _now_local())
                    finally:
                        _caldav_sync_lock.release()
                else:
                    log.info("iCloud sync already running; skipped this pass")
        except Exception:
            log.exception("caldav sync tick error (non-fatal)")
        return conn, status if isinstance(status, dict) else {}
    except Exception as e:
        log.exception("calendar sync tick error; reconnecting DB")
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn = fdb.connect(DB_PATH)
            fdb.ensure_schema(conn)
        except Exception:
            log.exception("sync tick DB reconnect failed; will retry")
        return conn, {"ok": False, "error": str(e)}


def _sync_tick(client, conn, cfg):
    """The background loop's iteration: _sync_all, waiting as long as it takes
    for the locks. Returns the connection to use next tick."""
    return _sync_all(client, conn, cfg)[0]


def _open_sync_conn():
    """Open the sync thread's own DB connection, retrying with backoff. The old
    code ran connect/ensure_schema outside any try, so a transient startup
    failure (e.g. ensure_schema racing a request thread's migration ->
    'database is locked') killed the daemon thread permanently and froze calendar
    sync forever behind a still-healthy 'last synced' badge (issue #32). Retry
    instead, and best-effort flag the failure in calendar_status so the wall
    shows staleness rather than a false-healthy state."""
    backoff = 5
    while True:
        conn = None
        try:
            conn = fdb.connect(DB_PATH)
            fdb.ensure_schema(conn)
            return conn
        except Exception as e:
            log.exception("sync startup failed; retrying in %ss", backoff)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            try:   # surface the failure; skip silently if the DB is unwritable
                # closing() guarantees the fd is released even when kv_set raises
                # on the same lock that triggered this retry — else the infinite
                # backoff loop would leak one connection per iteration.
                with contextlib.closing(fdb.connect(DB_PATH)) as sc:
                    prior = fdb.kv_get(sc, "calendar_status") or {}
                    st = {"ok": False, "error": f"sync startup: {e}",
                          "last_sync": prior.get("last_sync"),
                          # keep the running error's clock and a known
                          # expired sign-in, so neither is reset here
                          "error_since": prior.get("error_since")
                          or prior.get("last_sync")}
                    if prior.get("needs_auth"):
                        st["needs_auth"] = True
                    fdb.kv_set(sc, "calendar_status", st)
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def sync_loop():
    conn = _open_sync_conn()
    client = GoogleCalendarClient(TOKEN_PATH)
    while True:
        conn = _sync_tick(client, conn, cfg)
        time.sleep(300)


def _sync_enabled() -> bool:
    """The background sync thread runs unless disabled for tests (DISABLE_SYNC)
    or in DEMO mode. DEMO's canned data must not be overwritten by a real sync
    that would flag the seeded calendar as 'not configured' (issue #38)."""
    return os.environ.get("DISABLE_SYNC") != "1" and not DEMO


if _sync_enabled():
    threading.Thread(target=sync_loop, daemon=True).start()

# HTML must always revalidate (no-cache still allows ETag 304s): the HTML
# has no buster of its own: heuristic caching served phones a stale page on
# 2026-08-13 (no tab bar) after a deploy.
# Scripts, stylesheets and the manifest revalidate too. Their ?v= busters only
# move on a release, so a hub.js change deployed without a version bump kept
# its old URL, and the deploy reload could run the browser's heuristically
# cached old script under the new build token.
# The seasonal art under /seasons/ is referenced from INSIDE styles.css, where
# no ?v= reaches it, and a photo can be swapped under the same name, so it
# revalidates too. A 304 costs a phone or the wall almost nothing.
_REVALIDATE_SUFFIXES = (".js", ".css", ".webmanifest")


@app.middleware("http")
async def html_no_cache(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if resp.headers.get("content-type", "").startswith("text/html") \
            or path.startswith("/seasons/") \
            or (not path.startswith("/api/") and path.endswith(_REVALIDATE_SUFFIXES)):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# Static frontend mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
