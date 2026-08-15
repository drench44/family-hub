"""FastAPI app: hub, chores, admin, calendar and tile routes.

The wall page polls /api/hub; phones drive the /api/admin/* routes; tiles proxy
the box's other services. A background thread syncs Google Calendar every 5 min
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
from . import tiles
from . import todos as tdlogic
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


def _compute_build() -> str:
    """Short token that changes whenever any baked frontend asset changes, so the
    wall can auto-reload after a deploy. The frontend is BAKED into the image and a
    deploy rebuilds + restarts the container, so hashing the served asset files at
    startup yields a fresh value each deploy (and a stable one between deploys)."""
    import hashlib
    h = hashlib.sha256()
    for name in ("index.html", "admin.html", "styles.css", "hub.js",
                 "common.js", "admin.js", "theme.js"):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


BUILD = _compute_build()
_HEX = re.compile(r"#[0-9a-fA-F]{6}$")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

_conn = None
_conn_lock = threading.Lock()


def _now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def _today() -> dt.date:
    return _now_local().date()


def _db():
    """Module-level SQLite connection with a cheap self-heal ping per use, so a
    corrupted/closed handle recovers instead of erroring forever."""
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = fdb.connect(DB_PATH)
            fdb.ensure_schema(_conn)
        else:
            try:
                _conn.execute("SELECT 1")
            except Exception:
                log.warning("db handle unhealthy; reconnecting", exc_info=True)
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = fdb.connect(DB_PATH)
                fdb.ensure_schema(_conn)
        return _conn


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
    # rail color: the user's own Google sidebar color for the calendar wins;
    # config color is the pre-first-sync fallback
    google_colors = fdb.kv_get(c, "calendar_colors") or {}
    lo = (today - dt.timedelta(days=past_days)).isoformat()
    horizon = (today + dt.timedelta(days=days)).isoformat()
    events = []
    for e in fdb.list_events(c):
        day = e["start_ts"][:10]
        if day < lo or day > horizon:
            continue
        cal = cal_map.get(e["calendar_id"], {})
        events.append({
            **e,
            "color": google_colors.get(e["calendar_id"]) or cal.get("color"),
            "label": cal.get("label"),
            "event_color": GOOGLE_EVENT_COLORS.get(e["color_id"] or ""),
        })
    return {"status": status, "events": events}


def _links() -> dict:
    # cameras: config-driven list of go2rtc streams. Each tile embeds the
    # stream's WebRTC player; full-screen prefers a "hd" stream when the
    # config names one (e.g. a Protect cam's 4K twin), else the same src.
    # Each entry is validated INDIVIDUALLY: a single malformed camera/panel
    # (missing "src"/"id"/"url", or a non-integer vw) must not raise out of
    # /api/hub and blank the ENTIRE wall (chores, calendar, every tile) over one
    # typo. A bad entry is skipped and logged; the good ones still render.
    cameras = []
    for cam in cfg.cameras:
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
    return {"cameras": cameras, "panels": panels}


@app.get("/health")
def health():
    return {"status": "ok"}


def _people_day(c, d: dt.date) -> list[dict]:
    """The per-person chore plan for date ``d`` — done flags, rotation tags,
    and streak/week computed AS OF that day. Shared by the hub home feed
    (d=today) and the full-screen chores day browser."""
    d_str = d.isoformat()
    people = fdb.list_people(c)
    chores = fdb.list_chores(c)

    days = fdb.completions_between(c, d_str, d_str)
    completed_ids = {r["chore_id"] for r in days}
    plan = chlogic.day_plan(chores, people, d, completed_ids)

    # annotate each chore row with whether it is rotation-assigned, so the wall
    # can show the ↻ tag (day_plan keeps its minimal tested shape).
    chore_by_id = {ch["id"]: ch for ch in chores}
    for entry in plan:
        for row in entry["chores"]:
            src = chore_by_id.get(row["id"])
            row["rot"] = bool(src and src["assign_kind"] == "rotation")

    window_from = (d - dt.timedelta(days=370)).isoformat()
    history = fdb.completions_between(c, window_from, d_str)
    for entry in plan:
        pid = entry["person"]["id"]
        cbd: dict[str, set] = {}
        for r in history:
            if r["person_id"] == pid:
                cbd.setdefault(r["date"], set()).add(r["chore_id"])
        entry["streak"] = chlogic.streak(pid, chores, cbd, d)
        entry["week"] = chlogic.week_strip(pid, chores, cbd, d)
    return plan


@app.get("/api/hub")
def hub():
    c = _db()
    today = _today()
    # same fails-soft philosophy as _links(): a single bad todos row (or any
    # other unexpected failure in the group/read path) must not 500 the whole
    # wall over one broken bucket. GET /api/todos keeps NO such wrapper: a
    # 500 there is visible and correct, since it's a direct read of that data.
    try:
        todos_block = tdlogic.group(fdb.list_todos(c), today)
    except Exception:
        log.warning("todos block failed; serving empty buckets", exc_info=True)
        todos_block = {b: [] for b in tdlogic.BUCKETS}
    return {
        "date": today.isoformat(),
        "people": _people_day(c, today),
        "todos": todos_block,
        "calendar": _calendar_block(c, today, 14),
        "links": _links(),
        # A deploy-changing token: the wall reloads itself when it changes, so a
        # baked frontend update reaches the kiosk without a manual refresh.
        "build": BUILD,
        # House-default display theme (or None). The wall/admin stamp it live
        # on a fresh device with no localStorage override; None => the shipped
        # dark/cyan/none stays. Never persisted client-side.
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
    chore = _chore_row(c, chore_id)
    date_str = body.date or _today().isoformat()
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "bad date")
    if not chlogic.occurs(chore, d):
        raise HTTPException(422, "chore does not occur on that date")
    person_id = body.person_id
    if person_id is None:
        person_id = chlogic.assignee_id(chore, d)
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


class ChorePatch(BaseModel):
    title: str | None = None
    icon: str | None = None
    schedule_kind: str | None = None
    days_mask: int | None = None
    assign_kind: str | None = None
    fixed_person_id: int | None = None
    rotation_order: list[int] | None = None
    sort: int | None = None
    active: int | None = None


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
    if merged["schedule_kind"] not in ("daily", "days"):
        raise HTTPException(422, "schedule_kind must be daily or days")
    mask = merged.get("days_mask") or 0
    if not (0 <= mask <= 127):
        raise HTTPException(422, "days_mask must be 0–127")
    if merged["schedule_kind"] == "days" and mask == 0:
        raise HTTPException(422, "pick at least one day for a weekly chore")
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
    if "name" in fields or "color" in fields:
        name = fields.get("name", row["name"])
        color = fields.get("color", row["color"])
        fields["name"] = _validate_person(name, color)
    fdb.update_person(c, pid, **fields)
    return _person_row(c, pid)


@app.post("/api/admin/chores")
def admin_add_chore(ch: ChoreIn):
    c = _db()
    merged = ch.model_dump()
    _validate_chore(merged)
    cid = fdb.add_chore(
        c, title=merged["title"].strip(), icon=merged["icon"],
        schedule_kind=merged["schedule_kind"],
        days_mask=merged["days_mask"] if merged["schedule_kind"] == "days" else 0,
        assign_kind=merged["assign_kind"],
        fixed_person_id=merged["fixed_person_id"] if merged["assign_kind"] == "fixed" else None,
        rotation_order=merged["rotation_order"] if merged["assign_kind"] == "rotation" else [],
        rotation_epoch=_today().isoformat())
    return _chore_row(c, cid)


@app.patch("/api/admin/chores/{cid}")
def admin_patch_chore(cid: int, ch: ChorePatch):
    c = _db()
    row = _chore_row(c, cid)
    fields = ch.model_dump(exclude_unset=True)
    merged = {**row, **fields}
    _validate_chore(merged)
    # keep dependent columns coherent with the resolved kind
    if merged["schedule_kind"] != "days":
        fields["days_mask"] = 0
    if merged["assign_kind"] == "fixed":
        fields["rotation_order"] = []
    else:
        fields["fixed_person_id"] = None
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
    c = _db()
    return _calendar_block(c, _today(), days, past_days=past)


@app.get("/api/tiles/climate")
async def tile_climate():
    return await tiles.climate_tile(_http, cfg)


@app.get("/api/tiles/weather")
async def tile_weather():
    return await tiles.weather_tile(_http, cfg)


@app.get("/api/tiles/camera.jpg")
async def tile_camera(src: str = "cam"):
    # only configured streams may be probed — no free-form proxying. Both the
    # primary src (tile liveness) and any "hd" twin (full-screen readiness probe)
    # are allowed; nothing else.
    # skip a malformed entry (no "src") the same way _links() does, rather than
    # 500-ing every probe over one config typo.
    allowed = {cam["src"] for cam in cfg.cameras if cam.get("src")}
    allowed |= {cam["hd"] for cam in cfg.cameras if cam.get("hd")}
    allowed = allowed or {"cam"}
    if src not in allowed:
        raise HTTPException(404, "unknown camera")
    result = await tiles.camera_snapshot(_http, _fetch_cfg, src)
    if result is None:
        raise HTTPException(502, "camera unavailable")
    content, media = result
    return Response(content=content, media_type=media)


# --- background sync ------------------------------------------------------

def _sync_tick(client, conn, cfg):
    """One sync iteration: run sync_once; on failure log + self-heal the DB
    connection. Returns the connection to use next tick (a fresh one after a
    failure). Extracted from sync_loop so the reconnect path is unit-testable —
    the old bare `pass` froze calendar sync forever on a dropped DB handle with
    no log line (sync_once captures per-source errors itself, but its own final
    kv_set can still raise if the DB went unwritable)."""
    try:
        sync_once(client, conn, cfg, _now_local())
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


def sync_loop():
    conn = fdb.connect(DB_PATH)
    fdb.ensure_schema(conn)
    client = GoogleCalendarClient(TOKEN_PATH)
    while True:
        conn = _sync_tick(client, conn, cfg)
        time.sleep(300)


if os.environ.get("DISABLE_SYNC") != "1":
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
