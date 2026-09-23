"""The full health report (GET /health/full): does the hub actually WORK?

/health is liveness for the container healthcheck: the process answers and
can read its database. It said "ok" on 2026-09-17 while a deploy had deleted
the box's .env, the hub ran with an empty HA_TOKEN, and the laundry card was
gone for a day. This report is what a deploy gate (and a person) reads
instead. It proves, per thing the wall shows:

- the data is FRESH: each source's last good read, and where the upstream
  stamps its own data (wx.json `ts`, the climate rooms' ages, the fleet
  rollup's `generatedAt`), how old that data is;
- the reads happened in THIS process: a calendar sync or a laundry read the
  previous container did is not evidence the new one works;
- the settings it needs are present (HA_TOKEN when laundry is configured, the
  Google token when Google calendars are);
- config.json is the one the deploy shipped: the deploy bakes the sha256 of
  the config it staged into build_info.json, and this compares it with the
  bytes the process loaded and the bytes at the path now.

Every gated check lands in `problems` and turns `status` to "degraded"; a
gate reads the per-item `ok` fields (so its message names the item) and
`status`. Informational items (the backup heartbeat) go in `notes` and never
fail anything. The route always answers 200: this is a report, and liveness
stays with /health.

Pure: the route in app.py gathers the inputs, this module judges them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("family_hub.deep_health")

# How old each source's data may be before it counts as stale. Each is a few
# refresh periods of the thing that produces it, so one slow cycle is not a
# failure but a stopped producer is.
WEATHER_MAX_AGE_S = 600      # the weather adapter rewrites wx.json every ~20 s
CLIMATE_MAX_AGE_S = 900      # house-climate polls its room sensors every 3 min
FLEET_MAX_AGE_S = 300        # the fleet rollup is computed per request
LAUNDRY_MAX_AGE_S = 60       # the laundry watcher reads HA every 5 s
CALENDAR_MAX_AGE_S = 900     # the calendar sync runs every 5 min

# build_info.json sits next to this file in the image. deploy.sh writes it
# into the staged tree (never into the repo), so a dev checkout has none.
BUILD_INFO_PATH = Path(__file__).resolve().parent / "build_info.json"


def iso(ts: float | None) -> str | None:
    """Epoch seconds as ISO 8601 UTC with a Z (what a deploy gate parses)."""
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> float | None:
    """An ISO 8601 string (Z, offset, or naive = UTC) or epoch seconds/ms, as
    epoch seconds. None for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 100_000_000_000 else float(value)
    if isinstance(value, str) and value:
        try:
            t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.timestamp()
    return None


def file_sha256(path: str) -> str | None:
    """sha256 of a file's bytes, or None when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError as e:
        log.warning("health: cannot read %s to fingerprint it: %s", path, e)
        return None


def read_build_info(path: Path = BUILD_INFO_PATH) -> dict | None:
    """What the deploy recorded when it built this image: engine_commit,
    overlay_commit, config_sha256, built_at. None when absent (a dev run, or an
    image built by hand), and None plus a warning when unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("health: %s unreadable: %s", path, e)
        return None
    try:
        info = json.loads(raw)
    except ValueError as e:
        log.warning("health: %s is not JSON: %s", path, e)
        return None
    if not isinstance(info, dict):
        log.warning("health: %s is not a JSON object", path)
        return None
    keep = ("engine_commit", "overlay_commit", "config_sha256", "built_at")
    return {k: info.get(k) if isinstance(info.get(k), str) else None for k in keep}


def config_block(path: str, loaded_sha256: str | None, current_sha256: str | None,
                 build_info: dict | None) -> dict:
    """Is the config the process runs on the one the deploy shipped?

    `matches_deploy` is None when there is no deploy record to compare with,
    True only when the shipped hash, the loaded bytes and the file at the path
    now are all the same, else False. The loaded-vs-now pair catches a file
    changed under a running process; the shipped-vs-loaded pair catches the
    single-file bind mount trap (a replaced file stays invisible to a running
    container, which kept reading the old one after a config-only deploy on
    2026-09-17)."""
    expected = (build_info or {}).get("config_sha256")
    if expected is None:
        matches = None
    else:
        matches = bool(loaded_sha256) and loaded_sha256 == expected == current_sha256
    return {
        "path": path,
        "sha256": current_sha256,
        "loaded_sha256": loaded_sha256,
        "expected_sha256": expected,
        "matches_deploy": matches,
        "changed_since_start": (loaded_sha256 is not None and current_sha256 is not None
                                and loaded_sha256 != current_sha256),
    }


def setting(required: bool, present: bool, why: str) -> dict:
    return {"required": bool(required), "present": bool(present),
            "ok": bool(present) or not required, "why": why}


def _age(now: float, ts: float | None) -> float | None:
    return None if ts is None else round(now - ts, 1)


def _off(configured: bool) -> dict:
    return {"configured": bool(configured), "enabled": False, "ok": True, "status": "off"}


def tile_source(name: str, *, configured: bool, enabled: bool, state: dict | None,
                available: bool, now: float, max_age_s: float,
                stamps_data: bool = True) -> dict:
    """One proxied upstream (weather, climate, fleet).

    `state` is tiles.SOURCE_STATE[name]: the last real fetch's outcome in this
    process (in memory, so never the previous container's). `available` is
    what the tile returned just now (the route calls it, so a hub with no wall
    open still gets a fresh read). With `stamps_data`, the upstream's own data
    time must be present and within max_age_s: a feed that answers with
    hour-old data is not working.
    """
    if not configured or not enabled:
        return _off(configured)
    st = state or {}
    last_ok = st.get("last_ok")
    data_ts = st.get("data_ts")
    out = {
        "configured": True, "enabled": True,
        "last_ok": iso(last_ok), "data_ts": iso(data_ts),
        "data_age_s": _age(now, data_ts), "max_age_s": max_age_s,
        "last_error": st.get("last_error"), "last_error_at": iso(st.get("last_error_at")),
    }
    if not available or last_ok is None:
        # Classified from the last error, for the reader only: an unreachable
        # upstream and a hub that cannot use a live one both fail the gate.
        kind = st.get("last_error_kind")
        out["status"] = ("upstream_down" if kind in ("unreachable", "upstream")
                         else "error" if kind else "waiting")
    elif stamps_data and data_ts is None:
        out["status"] = "error"
        out["last_error"] = out["last_error"] or f"{name} data carries no timestamp"
    elif stamps_data and data_ts - now > 300:
        out["status"] = "error"
        out["last_error"] = f"{name} data is stamped {int(data_ts - now)} s in the future (a clock or the value is wrong)"
    elif stamps_data and now - data_ts > max_age_s:
        out["status"] = "stale"
    elif st.get("stale_items"):
        out["status"] = "degraded"
        out["stale_items"] = list(st["stale_items"])
        out["last_error"] = "stale: " + ", ".join(st["stale_items"])
    else:
        out["status"] = "ok"
    out["ok"] = out["status"] == "ok"
    return out


def calendar_source(*, configured: bool, enabled: dict, statuses: dict,
                    now: float, started_at: float,
                    max_age_s: float = CALENDAR_MAX_AGE_S) -> dict:
    """The calendar sources the wall shows (Google/ICS share one status, iCloud
    has its own).

    `enabled` maps source -> bool, `statuses` maps source -> its kv status
    ({ok, last_sync, needs_auth, error}). Judged RAW, without the wall's
    one-hour grace for a fresh error: the wall hides a blip from the family,
    a deploy gate must not. Every enabled source must be ok with a last_sync
    both recent and made by THIS process (a persisted last_sync from the
    previous container proves nothing about the new one).
    """
    on = [s for s, e in enabled.items() if e]
    if not configured or not on:
        return _off(configured)
    per = {}
    worst = "ok"
    order = ["ok", "waiting", "stale", "error", "needs_auth"]
    oldest = None
    for src in on:
        s = statuses.get(src) or {}
        last = parse_ts(s.get("last_sync"))
        if s.get("needs_auth"):
            word = "needs_auth"
        elif s.get("ok") is not True:
            word = "error" if s.get("error") else "waiting"
        elif last is None or last < started_at:
            word = "waiting"
        elif now - last > max_age_s:
            word = "stale"
        else:
            word = "ok"
        if last is not None:
            oldest = last if oldest is None else min(oldest, last)
        per[src] = {"status": word, "last_sync": iso(last),
                    "error": s.get("error") if word != "ok" else None}
        if order.index(word) > order.index(worst):
            worst = word
    return {"configured": True, "enabled": True, "ok": worst == "ok", "status": worst,
            "last_sync": iso(oldest), "max_age_s": max_age_s, "sources": per}


def laundry_source(*, configured: bool, enabled: bool, config_error: str | None,
                   token_present: bool, auth_rejected: bool, watching: bool,
                   last_ok: float | None, snapshot: dict | None, now: float,
                   started_at: float, max_age_s: float = LAUNDRY_MAX_AGE_S) -> dict:
    """The washer/dryer card. Fresh = the watcher's last good Home Assistant
    read is at most max_age_s old (it reads every 5 s), and every configured
    machine reports (a machine whose entities were renamed reads offline
    forever while the other keeps the card looking alive)."""
    if not configured or not enabled:
        return _off(configured)
    out = {"configured": True, "enabled": True, "last_ok": iso(last_ok),
           "data_age_s": _age(now, last_ok), "max_age_s": max_age_s,
           "machines_offline": []}
    if config_error:
        out["status"], out["error"] = "error", f"config: {config_error}"
    elif not token_present or auth_rejected:
        out["status"] = "needs_auth"
        out["error"] = ("HA_TOKEN is empty" if not token_present
                        else "Home Assistant rejects HA_TOKEN")
    elif not watching:
        out["status"], out["error"] = "error", "the laundry watcher is not running"
    elif last_ok is None or last_ok < started_at:
        out["status"] = "waiting"
    elif now - last_ok > max_age_s:
        out["status"] = "stale"
    else:
        offline = [m.get("id") for m in (snapshot or {}).get("machines") or []
                   if isinstance(m, dict) and m.get("phase") == "offline"]
        out["machines_offline"] = offline
        out["status"] = "degraded" if offline else "ok"
    out["ok"] = out["status"] == "ok"
    return out


def cameras_source(*, configured: bool, enabled: bool, wanted: list[str],
                   streams: dict | None, error: str | None) -> dict:
    """The camera tiles: go2rtc (in the same compose stack) answers, and knows
    every stream config.json names. Frames are not fetched: a cold camera takes
    seconds to start, and a camera that is itself offline is not the hub's
    fault."""
    if not configured or not enabled:
        return _off(configured)
    out = {"configured": True, "enabled": True, "streams_missing": []}
    if streams is None:
        out["status"], out["error"] = "error", error or "go2rtc did not answer"
    else:
        missing = sorted(s for s in set(wanted) if s not in streams)
        out["streams_missing"] = missing
        out["status"] = "error" if missing else "ok"
        if missing:
            out["error"] = "go2rtc has no stream for " + ", ".join(missing)
    out["ok"] = out["status"] == "ok"
    return out


def assemble(*, now: float, started_at: float, version: str, build: str,
             build_info: dict | None, config: dict, db_ok: bool, db_error: str | None,
             settings: dict, sources: dict, notes: dict) -> dict:
    """The report. `problems` lists every failed gated item in words."""
    problems = []
    if not db_ok:
        problems.append(f"db: {db_error or 'unusable'}")
    if config.get("matches_deploy") is False:
        problems.append("config: the running config.json is not the one the deploy shipped")
    for name, s in settings.items():
        if not s.get("ok"):
            problems.append(f"setting {name}: {s.get('why')}")
    for name, s in sources.items():
        if not s.get("ok"):
            detail = s.get("error") or s.get("last_error")
            problems.append(f"{name}: {s.get('status')}" + (f" ({detail})" if detail else ""))
    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "checked_at": iso(now),
        "process_started_at": iso(started_at),
        "version": version,
        "build": build,
        "deploy": build_info,
        "config": config,
        "db": {"ok": bool(db_ok), "error": db_error},
        "settings": settings,
        "sources": sources,
        "notes": notes,
    }


def token_file_present(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False
