"""FastAPI app: hub, chores, admin, calendar and tile routes.

The wall page polls /api/hub and drives the /api/admin/* routes from its Chores
edit mode (reachable from a phone too — same page); tiles proxy the box's other
services. A background thread syncs Google Calendar every 5 min
(disabled by DISABLE_SYNC=1 in tests). LAN-only, no auth — the established
trust model for every service on this box.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chores as chlogic
from . import db as fdb
from . import demo as fdemo
from . import integrations as fintegrations
from . import reminders as remlogic
from . import tiles
from . import todos as tdlogic
from . import version as fversion
from . import caldav_service
from . import caldav_sync
from . import chore_mirror
from .calendar_sync import GoogleCalendarClient, sync_once
from .config import load_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("family_hub")

cfg = load_config(os.environ.get("CONFIG_PATH", "config.json"))
# Server-side camera fetches reach go2rtc over the shared compose network
# (http://go2rtc:1984): a container cannot hairpin its OWN stack's published
# LAN port (same-bridge NAT reply mismatch — found live 2026-08-12). Browser
# links keep the LAN URL from config.
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


def _compute_build() -> str:
    """Short token that changes whenever any baked frontend asset changes, so the
    wall can auto-reload after a deploy. The frontend is BAKED into the image and a
    deploy rebuilds + restarts the container, so hashing the served asset files at
    startup yields a fresh value each deploy (and a stable one between deploys)."""
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
    return h.hexdigest()[:12]


BUILD = _compute_build()
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
    if not fdemo.is_unseeded(conn):
        return
    try:
        fdemo.seed_demo(conn, _today())
    except Exception:
        # The fdb helpers self-commit, so a seed that raises partway has already
        # written some rows (people first). Wipe them so the empty-db guard fires
        # again next open and re-seeds cleanly, instead of seeing the half-written
        # people and serving a permanently half-populated demo.
        fdemo.clear_demo(conn)
        raise
    log.info("DEMO mode: seeded the sample family wall")


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
            fdb.seed_integration(conn, integ["id"], integ["kind"], sort=i)
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
    yield
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


def _calendar_status_agg(c) -> dict:
    """Aggregate calendar health across every ENABLED source (Google/ICS + iCloud
    CalDAV), so the wall's banner reflects whether ANY calendar is connected, not
    just Google. Without this, an unused/broken Google config showed "no calendar
    connected" while iCloud was synced and rendering. ok if any source is ok;
    else needs_auth if any needs it; else the real error; else not configured."""
    statuses = []
    if cfg.calendars and (_integration_on(c, "google_calendar")
                          or _integration_on(c, "ics_calendar")):
        statuses.append(fdb.kv_get(c, "calendar_status")
                        or {"ok": False, "error": "not configured"})
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
    for e in fdb.list_events(c):
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
    # The range for which THIS payload is authoritative (a missing day is really
    # free): the INTERSECTION of what was fetched (past_days .. days) and what
    # the sync actually caches (cfg.calendar_past_days .. cfg.calendar_window_days).
    # Reporting the raw config window would falsely mark a day that the sync
    # caches but this request never fetched (e.g. calendar_past_days raised above
    # the frontend's fixed past=45) as free instead of "not synced" (issue #37).
    synced_back = min(past_days, cfg.calendar_past_days)
    synced_fwd = min(days, cfg.calendar_window_days)
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


def _available_only():
    """The available integrations, with iCloud CalDAV availability reflecting the
    REAL credential state (env OR the server-side creds file), so UI-entered
    credentials make the integration appear without an env var or restart.
    DEMO overlay: the demo serves placeholder cameras and canned weather/climate
    (see _camera_links and the DEMO branches) even though config leaves them
    unset, so the registry must call them available or the layout engine and
    tab bar hide the demo's columns."""
    items = fintegrations.available_integrations(
        cfg, os.environ, caldav_ok=caldav_service.configured(os.environ))
    demo_ids = {"cameras", "weather", "climate", "laundry"} if DEMO else set()
    return [dict(i, available=True) if (i["id"] in demo_ids and not i["available"])
            else i
            for i in items
            if i["available"] or i["id"] in demo_ids]


def _integration_on(c, iid: str) -> bool:
    """One definition of 'this integration is on': available (configured in
    config/env) AND its toggle enabled. Every render/sync gate goes through here
    so their notions of 'on' cannot drift (Fable architecture review, rec 4)."""
    available = any(i["id"] == iid
                    for i in _available_only())
    return available and fdb.integration_enabled(c, iid, default=True)


def _visible_reminders(c) -> list:
    """iCloud reminders for the wall, rendered from cal_objects — the SINGLE
    source of truth that holds both the synced server state AND un-pushed wall
    edits (PENDING_*). Rendering from it (rather than a separate kv snapshot plus
    an overlay) is what makes a wall edit show instantly AND stay put: there's no
    stale pulled-snapshot to transiently revert to once the push lands and the row
    flips back to SYNCED. Respects the calendar picker (a reminder list unchecked
    in settings is hidden) and skips objects queued for deletion."""
    cols = fdb.list_caldav_collections(c)
    names = {col["id"]: col["display_name"] for col in cols}
    disabled = {col["id"] for col in cols if not col["enabled"]}
    out = []
    for o in fdb.list_cal_objects(c, "VTODO"):
        if o["sync_state"] == "PENDING_DELETE" or o["collection_id"] in disabled:
            continue
        if not o.get("raw_ics"):
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


def _integ_status(iid: str, caldav_status: dict, cal_status: dict,
                  mirror_status: dict | None = None):
    """A compact health string for an integration: 'ok' | 'needs_auth' | 'error',
    or None for one with no sync (cameras/weather/climate/laundry). Drives the settings
    menu's 'Reconnect iCloud' / warning affordance on auth-failure or error.

    The chore mirror rides on the iCloud integration, so a failing mirror tick
    shows as an error on that row — it used to fail forever with the row still
    reading 'ok'."""
    # calendar_status is shared by Google + ICS and can't tell them apart, and
    # needs_auth is a Google-only concept — so only surface it on google_calendar
    # (ICS gets no status rather than mis-inheriting Google's auth state).
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
    lst = []
    enabled_ids = set()
    for integ in _available_only():
        en = fdb.integration_enabled(c, integ["id"], default=True)
        if en:
            enabled_ids.add(integ["id"])
        entry = {"id": integ["id"], "kind": integ["kind"],
                 "name": integ["name"], "enabled": en,
                 "group": integ.get("group", "integration"),
                 "status": _integ_status(integ["id"], caldav_status, cal_status,
                                         mirror_status)}
        if integ["id"] == "icloud_caldav":
            # the connected Apple ID (not a secret) so settings can show it; the
            # password is never included. readonly = 1-way (read-only) vs 2-way.
            entry["account"] = caldav_service.caldav_credentials(os.environ)[0]
            entry["readonly"] = fdb.integration_config(
                c, "icloud_caldav").get("readonly", True)
            # un-pushed wall edits still queued (0 normally); lets settings warn
            # "N changes not yet synced" instead of the backlog being invisible.
            entry["pending"] = caldav_status.get("pending", 0)
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
                "tile": f"{cfg.go2rtc_base}/stream.html?src={src}&mode=webrtc",
                "full": f"{cfg.go2rtc_base}/stream.html?src={hd_src}",
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/version")
def api_version():
    """The deployed version + build hash — a debug/ops readout ("what's actually
    running?"). The changelog itself lives on GitHub, not here."""
    return {"version": APP_VERSION, "build": BUILD}


def _freeze_day(c, d_str: str, rows: list[dict]) -> None:
    """Write the day's live-resolved plan into the occurrence log — the moment
    history becomes frozen. Skips the write when the frozen rows already match,
    so the wall's constant polling doesn't churn the DB."""
    def key(r):
        return (r["chore_id"], r["person_id"], r["title"], r["icon"], r["rot"])
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

    if d < today:
        rows = fdb.day_log(c, d_str)
    else:
        rows = chlogic.plan_rows(fdb.list_chores(c), people, d, away_view)
        if d == today:
            _freeze_day(c, d_str, rows)

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
    for entry in plan:
        pid = entry["person"]["id"]
        occ: dict[str, set] = {}
        for r in logs:
            if r["person_id"] == pid:
                occ.setdefault(r["date"], set()).add(r["chore_id"])
        if d > today:
            # future days aren't logged; overlay d's live rows so the browser
            # can show a prospective streak/week for that day
            live = {r["chore_id"] for r in rows if r["person_id"] == pid}
            if live:
                occ[d_str] = live
        cbd: dict[str, set] = {}
        for r in history:
            # a day with no log row at all (server was down, pre-install) falls
            # back to the completion's own person_id rather than vanishing
            if owner.get((r["date"], r["chore_id"]), r["person_id"]) == pid:
                cbd.setdefault(r["date"], set()).add(r["chore_id"])
        away_dates = amap.get(pid, {}).get("dates", set())
        entry["away"] = pid in away_today
        entry["streak"] = chlogic.streak(occ, cbd, d, away_dates)
        entry["week"] = chlogic.week_strip(occ, cbd, d, away_dates)
    return plan, away_ok


def _backup_status(last_success, now, stale_s):
    """Pure: (last-success datetime or None, now, threshold secs) -> the /api/hub
    `backup` block. 'known' is False before any heartbeat exists, so a fresh
    deploy shows a muted 'unknown', never a false alarm."""
    if last_success is None:
        return {"known": False, "last_success": None, "age_s": None,
                "stale": False, "threshold_s": stale_s}
    age = int((now - last_success).total_seconds())
    return {"known": True, "last_success": last_success.isoformat(), "age_s": age,
            "stale": age > stale_s, "threshold_s": stale_s}


def _build_backup(conn, now=None, stale_s=BACKUP_STALE_S):
    """Read the 'backup_status' heartbeat the backup script writes into hub.db on
    every successful snapshot ({"at": ISO, ...}) and derive staleness. family-hub
    `kv` has no updated_at column, so the timestamp lives in the value. A stale
    heartbeat also catches 'backups stopped running at all'."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = fdb.kv_get(conn, "backup_status")
    last = None
    if isinstance(rec, dict) and rec.get("at"):
        try:
            last = dt.datetime.fromisoformat(rec["at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            last = None
    return _backup_status(last, now, stale_s)


@app.get("/api/hub")
def hub():
    c = _db()
    today = _today()
    # same fails-soft philosophy as _links(): a single bad todos row (or any
    # other unexpected failure in the group/read path) must not 500 the whole
    # wall over one broken bucket. GET /api/todos keeps NO such wrapper: a
    # 500 there is visible and correct, since it's a direct read of that data.
    todos_ok = True
    try:
        todos_block = tdlogic.group(fdb.list_todos(c), today)
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
    reminders_block = (remlogic.group(_visible_reminders(c), today)
                       if caldav_on else {b: [] for b in remlogic.BUCKETS})
    people, away_ok = _people_day(c, today)
    # Backup health for the header badge — fails-soft like the todos block above:
    # a read error must not 500 the whole wall over a status indicator.
    try:
        backup_block = _build_backup(c)
    except Exception:
        log.error("backup status read failed; serving unknown", exc_info=True)
        backup_block = _backup_status(None, dt.datetime.now(dt.timezone.utc), BACKUP_STALE_S)
    return {
        "date": today.isoformat(),
        "people": people,
        # Mirrors todos_ok: False when the away overlay build threw and the wall
        # is rendering with nobody marked away, so it can show a small note
        # instead of silently presenting a genuinely-away person as present.
        "away_ok": away_ok,
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
    people, _ = _people_day(c, d)
    return {"date": d.isoformat(), "people": people}


# --- chores completion ----------------------------------------------------

class CompleteBody(BaseModel):
    date: str | None = None
    person_id: int | None = None


@app.post("/api/chores/{chore_id}/complete")
def complete(chore_id: int, body: CompleteBody | None = None):
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
            _, away_view, _ = _away_view(c, d)
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
def uncomplete(chore_id: int, date: str | None = None):
    c = _db()
    date_str = date or _today().isoformat()
    # Resolve the current owner BEFORE clearing, so the reopen can't be pushed
    # onto a mirror ledger row that still names the other person (M3).
    owner = _resolved_owner(c, chore_id, date_str)
    fdb.clear_completion(c, chore_id, date_str)
    # Reopen the mirrored iCloud reminder too (no-op if not mirrored).
    chore_mirror.push_completion(c, chore_id, date_str, False,
                                 expected_person_id=owner)
    return {"ok": True}


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
    for row in fdb.list_todos(c):
        if row["id"] == tid:
            return row
    raise HTTPException(404, "unknown todo")


@app.get("/api/todos")
def todos_list():
    c = _db()
    today = _today()
    rows = fdb.list_todos(c)
    return {"buckets": tdlogic.group(rows, today),
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
    fdb.update_todo(c, tid, **fields)
    return _todo_row(c, tid)


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


# --- integrations (the settings menu / extension toggles) -----------------

class IntegrationPatch(BaseModel):
    enabled: bool | None = None
    readonly: bool | None = None   # CalDAV: 1-way (read-only, True) vs 2-way


@app.get("/api/integrations")
def integrations_list():
    """Every available integration with its on/off state, for the settings menu."""
    return {"integrations": _integrations_state(_db())["list"]}


@app.patch("/api/integrations/{iid}")
def integrations_patch(iid: str, body: IntegrationPatch):
    c = _db()
    avail = {i["id"]: i for i in _available_only()}
    if iid not in avail:
        raise HTTPException(404, "unknown integration")
    # ensure a row exists (a never-toggled integration has none yet), then set it
    fdb.seed_integration(c, iid, avail[iid]["kind"])
    if body.enabled is not None:
        fdb.set_integration_enabled(c, iid, body.enabled)
    if body.readonly is not None:
        conf = fdb.integration_config(c, iid)
        conf["readonly"] = bool(body.readonly)
        fdb.set_integration_config(c, iid, conf)
    return {"id": iid, "enabled": fdb.integration_enabled(c, iid),
            "readonly": fdb.integration_config(c, iid).get("readonly", True)}


# --- iCloud CalDAV credentials (entered in settings, stored server-side) ---

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
    caldav_service.store_credentials(user, pw)
    global _caldav_client_built
    _caldav_client_built = False          # next sync/test rebuilds with new creds
    fdb.seed_integration(_db(), "icloud_caldav", "caldav")
    return {"ok": True, "user": user}     # the Apple ID is not a secret; no password


@app.delete("/api/integrations/icloud_caldav/credentials")
def caldav_clear_credentials():
    c = _db()
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
    return caldav_sync.sync_once(client, _db(), cfg, _now_local())


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
    return {"buckets": remlogic.group(_visible_reminders(c), _today()),
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
    fdb.queue_cal_object_update(c, body.id, ics, obj["summary"], now.isoformat())
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
    active: int | None = None
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
    date: str | None = None
    sort: int | None = None
    active: int | None = None


# An explicit JSON null for a field backed by a NOT NULL column is a bad request
# (422), not a 500 from the DB write (issue #35). fixed_person_id is the one
# chore field that legitimately accepts null (clearing a fixed assignee); `date`
# is an API-only field whose null means "no change".
_PERSON_NONNULL_PATCH = {"name", "color", "sort", "active"}
_CHORE_NONNULL_PATCH = {"title", "icon", "schedule_kind", "days_mask",
                        "week_interval", "due_times", "assign_kind",
                        "rotation_order", "sort", "active"}
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
    return {"people": fdb.list_people(c, include_inactive=True),
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
    return _person_row(c, pid)


@app.delete("/api/admin/people/{pid}")
def admin_delete_person(pid: int):
    """Hard-delete a person (history-safe — see db.delete_person). Distinct from
    the deactivate path (PATCH active=0), which keeps the row for reactivation."""
    c = _db()
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
        rotation_epoch=epoch)
    return _chore_row(c, cid)


@app.patch("/api/admin/chores/{cid}")
def admin_patch_chore(cid: int, ch: ChorePatch):
    c = _db()
    row = _chore_row(c, cid)
    fields = ch.model_dump(exclude_unset=True)
    _reject_null_nonnullable(fields, _CHORE_NONNULL_PATCH)
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
    fdb.update_chore(c, cid, **fields)
    return _chore_row(c, cid)


@app.delete("/api/admin/chores/{cid}")
def admin_delete_chore(cid: int):
    c = _db()
    _chore_row(c, cid)  # raises 404 if it doesn't exist (existing helper behavior)
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


@app.get("/api/admin/away")
def admin_away_list():
    return {"away_periods": _away_rows(_db())}


@app.post("/api/admin/away")
def admin_away_open(a: AwayIn):
    c = _db()
    _person_row(c, a.person_id)                     # 404 if unknown
    if a.backup_person_id is not None:
        if a.backup_person_id == a.person_id:
            raise HTTPException(422, "backup cannot be the same person")
        _person_row(c, a.backup_person_id)
    # One open period per person: a second "going away" while the first is still
    # open would create overlapping rows for the same person (the wall would
    # resolve them nondeterministically without the away_map ORDER BY, and the
    # UI has no way to show two). Mirror /everyone's skip behavior with a clear
    # 409 for the single-person API.
    if any(r["person_id"] == a.person_id
           for r in fdb.list_away_periods(c, include_closed=False)):
        raise HTTPException(409, "person already has an open away period")
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    pid = fdb.add_away_period(c, a.person_id, start, None, a.backup_person_id)
    return next(r for r in _away_rows(c) if r["id"] == pid)


@app.post("/api/admin/away/everyone")
def admin_away_everyone(a: AwayEveryoneIn):
    c = _db()
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    open_pids = {r["person_id"] for r in fdb.list_away_periods(c,
                 include_closed=False)}
    created = []
    for p in fdb.list_people(c):                    # active only
        if p["id"] in open_pids:
            continue
        created.append(fdb.add_away_period(c, p["id"], start, None, None))
    return {"created": created}


@app.patch("/api/admin/away/{pid}")
def admin_away_patch(pid: int, a: AwayPatch):
    c = _db()
    row = fdb.get_away_period(c, pid)
    if row is None:
        raise HTTPException(404, "unknown away period")
    fields = a.model_dump(exclude_unset=True)
    for k in ("start_date", "end_date"):
        if fields.get(k) is not None:
            fields[k] = _valid_date(fields[k])
    # A backup change must pass the same checks as opening: real person, not the
    # away person themselves. (None clears the backup and is always allowed.)
    if fields.get("backup_person_id") is not None:
        if fields["backup_person_id"] == row["person_id"]:
            raise HTTPException(422, "backup cannot be the same person")
        _person_row(c, fields["backup_person_id"])          # 404 if unknown
    # The effective end_date must not precede the effective start_date, else
    # away_map silently voids the whole period (its a>b skip). "Effective"
    # covers both directions: an incoming end_date earlier than the (possibly
    # also-incoming) start, AND an incoming start_date pushed later than the
    # row's already-stored end_date when the patch doesn't re-supply end_date.
    effective_start = fields.get("start_date", row["start_date"])
    effective_end = fields.get("end_date", row["end_date"])
    if effective_end is not None and effective_end < effective_start:
        raise HTTPException(422, "end_date must not be before start_date")
    fdb.update_away_period(c, pid, **fields)
    return {"ok": True}


@app.post("/api/admin/away/{pid}/back")
def admin_away_back(pid: int, a: AwayBackIn | None = None):
    c = _db()
    row = fdb.get_away_period(c, pid)
    if row is None:
        raise HTTPException(404, "unknown away period")
    end = (_valid_date(a.end_date) if a and a.end_date
           else (_today() - dt.timedelta(days=1)).isoformat())
    # Guard the fast "Going away" (start=today) then immediate "I'm back"
    # (end defaults to yesterday) double-tap: end < start would silently void
    # the period via away_map's a>b skip.
    if end < row["start_date"]:
        raise HTTPException(422, "end_date must not be before start_date")
    fdb.close_away_period(c, pid, end)
    return {"ok": True}


@app.delete("/api/admin/away/{pid}")
def admin_away_delete(pid: int):
    if not fdb.delete_away_period(_db(), pid):
        raise HTTPException(404, "unknown away period")
    return {"ok": True}


# --- calendar + tiles -----------------------------------------------------

@app.get("/api/calendar")
def calendar(days: int = 90, past: int = 45):
    # Bounded to the order of the sync window; a huge value would otherwise
    # overflow the date math into a 500.
    if not (0 <= days <= 366 and 0 <= past <= 366):
        raise HTTPException(422, "days/past out of range")
    c = _db()
    return _calendar_block(c, _today(), days, past_days=past)


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


@app.get("/api/tiles/laundry")
async def tile_laundry():
    if DEMO:
        return fdemo.demo_laundry()   # canned machines; no HA hit
    t = await tiles.laundry_tile(_http, cfg, os.environ.get("HA_TOKEN", ""))
    if not t.get("available"):
        return t
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
        for m in machines:
            done_key = f"laundry_done_{m['id']}"
            phase_key = f"laundry_phase_{m['id']}"
            missed_key = f"laundry_missed_{m['id']}"
            nonoff_key = f"laundry_nonoff_{m['id']}"
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
            if phase in ("running", "paused", "reserved", "done", "error"):
                # any sign of machine activity retires the synthetic Done (a
                # real observed done replaces it; a new cycle supersedes it).
                # Deliberately NOT cleared on offline: an HA blip mid-hold
                # must not erase a finish the family hasn't seen yet.
                if fdb.kv_get(c, missed_key):
                    fdb.kv_set(c, missed_key, None)
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
            if phase and phase != "offline":
                fdb.kv_set(c, nonoff_key, phase)
            if phase == "idle":
                # Present a remembered missed finish as the Done it really
                # was — green ring, "at 9:02pm" — for the hold window, then
                # decay to idle + the quiet "last load" line.
                ms = fdb.kv_get(c, missed_key)
                mt = tiles._laundry_minutes_to(ms) if ms else None
                if mt is not None and -tiles.LAUNDRY_MISSED_DONE_HOLD_MIN <= mt <= 0:
                    m["phase"] = "done"
                    m["status_since"] = ms
            m["last_done"] = fdb.kv_get(c, done_key)
    except Exception:
        log.warning("laundry: completion-memory kv / cycle-log write failed; "
                    "serving the tile without last_done (the interrupted "
                    "transition retries next poll)", exc_info=True)
        for m in machines:
            m.setdefault("last_done", None)
    return {**t, "machines": machines}


@app.get("/api/laundry/log")
async def laundry_log_route(machine: str | None = None, limit: int = 200):
    """The laundry cycle log: observed phase transitions newest-first — the
    evidence base for tuning finish detection (projection accuracy, endgame
    window, missed-done hold) and diagnosing any finish the wall got wrong.
    Fail-soft like every tile read: a DB hiccup serves an empty list loudly
    logged, never a 500."""
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


@app.get("/api/tiles/camera.jpg")
async def tile_camera(src: str = "cam"):
    # only configured streams may be probed — no free-form proxying. Both the
    # primary src (tile liveness) and any "hd" twin (full-screen readiness probe)
    # are allowed; nothing else.
    # skip a malformed entry (no "src") the same way _links() does, rather than
    # 500-ing every probe over one config typo. Both the wall column (`cameras`)
    # and the Cameras-tab / camera-page grid (`camera_page`) are probe-able, so a
    # grid-only camera (e.g. one shown only on the camera page) can report live.
    allowed = set()
    for entry in (*cfg.cameras, *cfg.camera_page):
        if entry.get("src"):
            allowed.add(entry["src"])
        if entry.get("hd"):
            allowed.add(entry["hd"])
    allowed = allowed or {"cam"}
    if src not in allowed:
        raise HTTPException(404, "unknown camera")
    result = await tiles.camera_snapshot(_http, _fetch_cfg, src)
    if result is None:
        raise HTTPException(502, "camera unavailable")
    content, media = result
    return Response(content=content, media_type=media)


# --- background sync ------------------------------------------------------

_caldav_client = None
_caldav_client_built = False


def _get_caldav_client():
    """The iCloud CalDAV client from the environment, built once and reused so
    its principal connection is cached across ticks. None when no credentials are
    set — the whole CalDAV subsystem is then inert (the feature flag)."""
    global _caldav_client, _caldav_client_built
    if not _caldav_client_built:
        _caldav_client = caldav_service.client_from_env()
        _caldav_client_built = True
    return _caldav_client


def _sync_tick(client, conn, cfg):
    """One sync iteration: run sync_once; on failure log + self-heal the DB
    connection. Returns the connection to use next tick (a fresh one after a
    failure). Extracted from sync_loop so the reconnect path is unit-testable —
    the old bare `pass` froze calendar sync forever on a dropped DB handle with
    no log line (sync_once captures per-source errors itself, but its own final
    kv_set can still raise if the DB went unwritable)."""
    try:
        sync_once(client, conn, cfg, _now_local())
        # CalDAV runs in the same tick but is isolated: it never raises (records
        # its own caldav_status), and a defensive guard keeps any surprise from
        # disrupting the Google sync's reconnect logic. Inert without credentials.
        try:
            cdav = _get_caldav_client()
            if cdav is not None:
                caldav_sync.sync_once(cdav, conn, cfg, _now_local())
        except Exception:
            log.exception("caldav sync tick error (non-fatal)")
        return conn
    except Exception:
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
        return conn


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
                    fdb.kv_set(sc, "calendar_status",
                               {"ok": False, "error": f"sync startup: {e}",
                                "last_sync": prior.get("last_sync")})
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

# HTML must always revalidate (no-cache still allows ETag 304s): the ?v=N
# busters version the css/js, but the HTML that references them has no
# buster of its own — heuristic caching served phones a stale page on
# 2026-08-13 (no tab bar) after a deploy.
@app.middleware("http")
async def html_no_cache(request, call_next):
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# Static frontend mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
