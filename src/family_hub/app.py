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
import threading
import time
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
from . import caldav_service
from . import caldav_sync
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
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "web", "static")

# DEMO=1 turns the whole app into a self-contained sample wall (fake family,
# placeholder cameras, canned weather/climate) for a README screenshot or a
# "try it" run: no real calendars, cameras, or feeds needed. Every DEMO branch
# below is gated on this flag, so an unset DEMO changes zero behavior.
DEMO = os.environ.get("DEMO", "") == "1"


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
        for i, integ in enumerate(fintegrations.available_only(cfg, os.environ)):
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


def _calendar_block(c, today: dt.date, days: int, past_days: int = 0) -> dict:
    status = fdb.kv_get(c, "calendar_status") or {"ok": False, "error": "not configured"}
    cal_map = {cal["id"]: cal for cal in cfg.calendars}
    # Integration gating: a disabled calendar source's events are hidden (not
    # deleted) — its cache stays, so re-enabling shows them instantly.
    cal_google_on = fdb.integration_enabled(c, "google_calendar", default=True)
    cal_ics_on = fdb.integration_enabled(c, "ics_calendar", default=True)
    # CalDAV events gate on availability AND the toggle (same as reminders), so
    # pulling the credentials hides stale cached events instead of showing them.
    cal_caldav_on = (fintegrations.caldav_configured(os.environ)
                     and fdb.integration_enabled(c, "icloud_caldav", default=True))
    # CalDAV events carry a 'caldav:<slug>' calendar_id; their name/color come
    # from the discovered-collection metadata the caldav sync records, not config.
    caldav_cols = fdb.kv_get(c, "caldav_collections") or {}
    # rail color: the user's own Google sidebar color for the calendar wins;
    # config color is the pre-first-sync fallback
    google_colors = fdb.kv_get(c, "calendar_colors") or {}
    lo = (today - dt.timedelta(days=past_days)).isoformat()
    horizon = (today + dt.timedelta(days=days)).isoformat()
    events = []
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
            if not cal_caldav_on:
                continue
            meta = caldav_cols.get(cid, {})
            color, label = meta.get("color"), meta.get("name")
        else:
            cal = cal_map.get(cid, {})
            cal_kind = cal.get("kind", "google")
            if cal_kind == "google" and not cal_google_on:
                continue
            if cal_kind == "ics" and not cal_ics_on:
                continue
            color = google_colors.get(cid) or cal.get("color")
            label = cal.get("label")
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


def _integ_status(iid: str, caldav_status: dict, cal_status: dict):
    """A compact health string for an integration: 'ok' | 'needs_auth' | 'error',
    or None for one with no sync (cameras/weather/climate). Drives the settings
    menu's 'Reconnect iCloud' / warning affordance on auth-failure or error."""
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
    return "ok"


def _integrations_state(c) -> dict:
    """The available integrations plus their enable/disable state and health, and
    the set of enabled ids for render gating. Available comes from config/env
    (the registry); the enabled flag comes from the integrations table (default
    True for a not-yet-seeded one, so gating never hides an un-toggled source)."""
    caldav_status = fdb.kv_get(c, "caldav_status") or {}
    cal_status = fdb.kv_get(c, "calendar_status") or {}
    lst = []
    enabled_ids = set()
    for integ in fintegrations.available_only(cfg, os.environ):
        en = fdb.integration_enabled(c, integ["id"], default=True)
        if en:
            enabled_ids.add(integ["id"])
        lst.append({"id": integ["id"], "kind": integ["kind"],
                    "name": integ["name"], "enabled": en,
                    "status": _integ_status(integ["id"], caldav_status, cal_status)})
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


def _freeze_day(c, d_str: str, rows: list[dict]) -> None:
    """Write the day's live-resolved plan into the occurrence log — the moment
    history becomes frozen. Skips the write when the frozen rows already match,
    so the wall's constant polling doesn't churn the DB."""
    def key(r):
        return (r["chore_id"], r["person_id"], r["title"], r["icon"], r["rot"])
    if sorted(key(r) for r in fdb.day_log(c, d_str)) != \
            sorted(key(r) for r in rows):
        fdb.replace_day_log(c, d_str, rows)


def _people_day(c, d: dt.date) -> list[dict]:
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
    if d < today:
        rows = fdb.day_log(c, d_str)
    else:
        rows = chlogic.plan_rows(fdb.list_chores(c), people, d)
        if d == today:
            _freeze_day(c, d_str, rows)

    completed_ids = {r["chore_id"]
                     for r in fdb.completions_between(c, d_str, d_str)}
    plan = chlogic.day_plan(rows, people, completed_ids)

    window_from = (d - dt.timedelta(days=370)).isoformat()
    logs = fdb.logs_between(c, window_from, d_str)
    history = fdb.completions_between(c, window_from, d_str)
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
            if r["person_id"] == pid:
                cbd.setdefault(r["date"], set()).add(r["chore_id"])
        entry["streak"] = chlogic.streak(occ, cbd, d)
        entry["week"] = chlogic.week_strip(occ, cbd, d)
    return plan


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
    # iCloud Reminders (read-only), grouped; empty unless the CalDAV integration
    # is available AND enabled. A separate surface from the local To-Dos.
    caldav_on = "icloud_caldav" in istate["enabled_ids"]
    reminders_block = (remlogic.group(fdb.kv_get(c, "caldav_reminders") or [], today)
                       if caldav_on else {b: [] for b in remlogic.BUCKETS})
    return {
        "date": today.isoformat(),
        "people": _people_day(c, today),
        "todos": todos_block,
        "todos_ok": todos_ok,
        "reminders": reminders_block,
        "calendar": _calendar_block(c, today, 14),
        "links": _links(istate["enabled_ids"]),
        # The wall's settings menu reads this; tiles for disabled integrations
        # (weather/climate/cameras) are hidden client-side from the enabled flags.
        "integrations": istate["list"],
        # A deploy-changing token: the wall reloads itself when it changes, so a
        # baked frontend update reaches the kiosk without a manual refresh.
        "build": BUILD,
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
    return {"date": d.isoformat(), "people": _people_day(c, d)}


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
            active_ids = {p["id"] for p in fdb.list_people(c)}
            person_id = chlogic.assignee_id(chore, d, active_ids)
        if person_id is None:
            raise HTTPException(422, "no resolvable assignee")
    # Validate the (possibly client-supplied) person_id against a real person
    # before writing, so a malformed request can't record an invisible orphan
    # completion. _person_row raises 404 for an unknown id. (Fresh DBs also
    # enforce this via a FK; this gives a clean error on any DB.)
    _person_row(c, person_id)
    fdb.set_completion(c, chore_id, date_str, person_id)
    return {"ok": True}


@app.delete("/api/chores/{chore_id}/complete")
def uncomplete(chore_id: int, date: str | None = None):
    c = _db()
    fdb.clear_completion(c, chore_id, date or _today().isoformat())
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


# --- integrations (the settings menu / extension toggles) -----------------

class IntegrationPatch(BaseModel):
    enabled: bool


@app.get("/api/integrations")
def integrations_list():
    """Every available integration with its on/off state, for the settings menu."""
    return {"integrations": _integrations_state(_db())["list"]}


@app.patch("/api/integrations/{iid}")
def integrations_patch(iid: str, body: IntegrationPatch):
    c = _db()
    avail = {i["id"]: i for i in fintegrations.available_only(cfg, os.environ)}
    if iid not in avail:
        raise HTTPException(404, "unknown integration")
    # ensure a row exists (a never-toggled integration has none yet), then set it
    fdb.seed_integration(c, iid, avail[iid]["kind"])
    fdb.set_integration_enabled(c, iid, body.enabled)
    return {"id": iid, "enabled": body.enabled}


# --- reminders (read-only iCloud VTODO) -----------------------------------

@app.get("/api/reminders")
def reminders_full():
    """The full grouped iCloud Reminders view. `configured` is False (empty
    buckets) when CalDAV has no credentials or its integration is off."""
    c = _db()
    on = (fintegrations.caldav_configured(os.environ)
          and fdb.integration_enabled(c, "icloud_caldav", default=True))
    if not on:
        return {"buckets": {b: [] for b in remlogic.BUCKETS}, "configured": False}
    rem = fdb.kv_get(c, "caldav_reminders") or []
    return {"buckets": remlogic.group(rem, _today()), "configured": True}


# --- admin ----------------------------------------------------------------

class PersonIn(BaseModel):
    name: str
    color: str


class PersonPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    sort: int | None = None
    active: int | None = None


class ChoreIn(BaseModel):
    title: str
    icon: str = ""
    schedule_kind: str
    days_mask: int = 0
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
                        "assign_kind", "rotation_order", "sort", "active"}


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


def _validate_chore(merged: dict) -> None:
    title = (merged.get("title") or "").strip()
    if not (1 <= len(title) <= 60):
        raise HTTPException(422, "title must be 1–60 characters")
    if len(merged.get("icon") or "") > 4:
        raise HTTPException(422, "icon must be at most 4 characters")
    kind = merged["schedule_kind"]
    if kind not in ("daily", "days", "once"):
        raise HTTPException(422, "schedule_kind must be daily, days or once")
    mask = merged.get("days_mask") or 0
    if not (0 <= mask <= 127):
        raise HTTPException(422, "days_mask must be 0–127")
    if kind == "days" and mask == 0:
        raise HTTPException(422, "pick at least one day for a weekly chore")
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
            "chores": fdb.list_chores(c, include_inactive=True)}


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
