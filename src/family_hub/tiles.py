"""Server-side proxies for the hub tiles. Each fetches an upstream on the box's
LAN and returns a small dict the frontend renders natively (no CORS, no iframes
on the home screen). Every fetch fails soft: a dead upstream yields an
``{"available": False}`` tile, never an exception."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

log = logging.getLogger("family_hub.tiles")

TIMEOUT = 3.0
# A cold go2rtc snapshot must first start the camera's RTSP producer, which
# takes >3s; once the wall's live tile is attached the stream stays warm.
CAMERA_TIMEOUT = 10.0

# Short in-process cache so repeated wall polls (every 60s, plus every device on
# the wall) don't hammer the almanac feed. Keyed by weather_base -> (expiry
# monotonic, trimmed result). Only successful reads are cached; a fetch error is
# never cached so a transient blip retries on the next poll.
WEATHER_TTL = 60.0
_weather_cache: dict[str, tuple[float, dict]] = {}

# Same short-cache discipline for the house-climate proxy, keyed by climate_base.
CLIMATE_TTL = 60.0
_climate_cache: dict[str, tuple[float, dict]] = {}

# Laundry cache, keyed by ha_base. Sized just UNDER the background watcher's
# cadence (app.LAUNDRY_WATCH_S, 5s): every watcher tick gets a genuinely
# fresh HA read, while any request landing between ticks (the route's
# fallback fetch when the watcher is disabled or hasn't produced a snapshot
# yet) reuses the still-warm result instead of doubling the HA traffic. The
# old two-speed endgame TTL is gone — the watcher polls at finish-catching
# speed all cycle long, so the brief lg_thinq "end" status can't slip
# between reads anywhere, not just near the projected finish.
LAUNDRY_TTL = 4.0
# How long a finished-but-uncollected load keeps presenting as Done — for a
# MISSED finish (running -> idle with the projection passed) AND for an
# observed "end" followed by the machine's own auto power-off (these LG
# machines turn themselves off 30-90s after the chime, so waiting for a
# person-shaped signal before decaying would wait until the NEXT cycle);
# both in app._laundry_annotate. A
# person powering the machine on clears the hold; otherwise it decays to
# idle + the "last load" line after this window — long enough to be seen
# across the kitchen, not forever.
LAUNDRY_MISSED_DONE_HOLD_MIN = 30.0
_laundry_cache: dict[str, tuple[float, dict]] = {}


def reset_caches() -> None:
    """Clear the in-process tile caches. Tests call this for deterministic
    behavior when they monkeypatch the HTTP client and poll more than once."""
    _weather_cache.clear()
    _climate_cache.clear()
    _laundry_cache.clear()
    _ha_warned.clear()


async def climate_tile(client, cfg) -> dict:
    """Proxy the house-climate service into a trimmed, fail-soft tile.

    PRIMARY: ``{climate_base}/api/rooms`` -> ``{available, rooms:[{name, channel,
    temp_f, humidity, stale, battery_low, ...}]}``. Each room is trimmed to
    ``{name, channel, temp_f, humidity, stale}``. This is a FAITHFUL passthrough:
    every room (including any "Outside" sensor) is included — the frontend filters
    which sensors it shows, not this proxy.

    SECONDARY (best-effort): ``{climate_base}/api/humidity`` ->
    ``{indoor_rh, indoor_dp, ...}`` supplies ``indoor_rh``/``indoor_dp``. A
    secondary failure degrades those to None but must NOT sink the whole card.

    Returns ``{"available": False}`` when ``climate_base`` is unset or the PRIMARY
    fetch fails/returns a non-dict body — never raises (the route has no global
    exception handler, so a raise would 500)."""
    base = cfg.climate_base
    if not base:
        return {"available": False}   # climate proxy off; no fetch attempted
    cached = _climate_cache.get(base)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        r = await client.get(f"{base}/api/rooms", timeout=TIMEOUT)
        r.raise_for_status()
        rooms = r.json()
        # Build the trimmed shape INSIDE the try: a flaky LAN device can serve
        # valid-but-non-dict JSON (``[]``, ``null``, a scalar, an error string).
        # ``rooms.get(...)`` then raises AttributeError, and a non-list ``rooms``
        # or a non-dict room item would raise too — catching here keeps the
        # fail-soft contract instead of letting it propagate out of the route.
        raw_rooms = rooms.get("rooms", [])
        if not isinstance(raw_rooms, list):
            raw_rooms = []
        mapped = [
            {"name": rm.get("name"), "channel": rm.get("channel"),
             "temp_f": rm.get("temp_f"), "humidity": rm.get("humidity"),
             "stale": rm.get("stale")}
            for rm in raw_rooms if isinstance(rm, dict)
        ]
    except Exception as e:
        log.warning("climate tile /api/rooms unavailable: %s", e)
        return {"available": False}   # not cached: retry on the next poll
    # Secondary indoor humidity/dew-point: best-effort. A failure — or a
    # valid-but-non-dict body — leaves indoor_rh/indoor_dp None and still returns
    # the rooms; it must never sink the card the primary already built.
    indoor_rh = None
    indoor_dp = None
    try:
        r = await client.get(f"{base}/api/humidity", timeout=TIMEOUT)
        r.raise_for_status()
        hum = r.json()
        if isinstance(hum, dict):
            indoor_rh = hum.get("indoor_rh")
            indoor_dp = hum.get("indoor_dp")
    except Exception as e:
        log.warning("climate tile /api/humidity unavailable "
                    "(indoor RH/DP unknown): %s", e)
    result = {
        "available": True,
        "rooms": mapped,
        "indoor_rh": indoor_rh,
        "indoor_dp": indoor_dp,
    }
    _climate_cache[base] = (time.monotonic() + CLIMATE_TTL, result)
    return result


def _weather_spark(wx: dict) -> dict:
    """Temperature curve for the weather card from the almanac feed's
    ``tempSeries`` (``{"temps": [hourly °, oldest->newest], "nowIndex": i}``).
    The adapter builds a 24h window — ~12h of real observations behind and ~12h
    of model forecast ahead — with ``nowIndex`` marking the current hour so the
    chart can dot "now" and split solid-past / dashed-future.

    Returns ``{"temps": [...], "now": i|None}``; an absent or malformed series
    yields ``{"temps": [], "now": None}`` (the frontend hides the chart below 2
    points). Verified against the live feed 2026-08-14 — the prior
    ``hourlyTemps`` key was a guess that never existed in the feed."""
    ts = wx.get("tempSeries")
    if not isinstance(ts, dict):
        return {"temps": [], "now": None}
    raw = ts.get("temps")
    raw = raw if isinstance(raw, list) else []   # a non-list (scalar/dict) => empty, not a crash
    # Require EVERY point to be a real number (bool is an int subclass, so exclude
    # it). Dropping interior points would shift nowIndex — a POSITIONAL marker —
    # off its hour and render a plausible but WRONG "now" dot, so a single bad
    # point makes the whole series malformed: hide the chart rather than
    # misplace the marker.
    temps = [t for t in raw if isinstance(t, (int, float)) and not isinstance(t, bool)]
    if len(temps) != len(raw):
        temps = []
    raw_now = ts.get("nowIndex")
    now = raw_now if (isinstance(raw_now, int) and not isinstance(raw_now, bool)
                      and 0 <= raw_now < len(temps)) else None
    # A good series with a bad/missing anchor deserves a loud log: the chart
    # falls back to an unanchored render (no time ticks, no "now" marker), and
    # the next feed-shape drift (like the hourlyTemps guess before it) should
    # be caught here, not by someone squinting at a wall with no clock labels.
    if temps and now is None:
        log.warning("weather feed tempSeries has %d temps but no usable "
                    "nowIndex (%r); temp chart renders unanchored", len(temps), raw_now)
    return {"temps": temps, "now": now}


async def weather_tile(client, cfg) -> dict:
    base = cfg.weather_base
    if not base:
        return {"available": False}   # weather proxy off; no fetch attempted
    cached = _weather_cache.get(base)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        r = await client.get(f"{base}/wx.json", timeout=TIMEOUT)
        r.raise_for_status()
        wx = r.json()
        # Build the trimmed shape INSIDE the try: a flaky LAN device can serve
        # valid-but-non-dict JSON (``[]``, ``null``, a scalar, an error string),
        # which would make wx.get(...) raise AttributeError. Catching it here
        # keeps the fail-soft contract (never a 500) instead of letting it
        # propagate out of the route.
        spark = _weather_spark(wx)
        result = {
            "available": True,
            "temp": wx.get("temp"),
            "unit": wx.get("tempUnit"),
            "conditions": wx.get("conditions"),
            "feels": wx.get("feelsLike"),
            "feels_desc": wx.get("feelsDesc"),
            "low": wx.get("fcLow"),
            "high": wx.get("fcHigh"),
            "uv": wx.get("uvIndex"),
            "uv_desc": wx.get("uvDesc"),
            "aqi": wx.get("aqi"),
            "aqi_cat": wx.get("aqiCategory"),
            "humidity": wx.get("humidity"),
            "dew_point": wx.get("dewPoint"),
            "spark": spark["temps"],
            "spark_now": spark["now"],
            "stale": wx.get("weatherStale"),
            # Sky-scene inputs (verified against the live feed 2026-08-17):
            # sunrise/sunset are "HH:MM" local strings that drive the sky's
            # dawn/day/dusk/night phase (absent -> the frontend's fixed civil
            # boundaries); moonPhase is a name ("Waxing Crescent") and
            # moonIllum a lit percentage that shape the drawn moon (absent ->
            # a full disc).
            "sunrise": wx.get("sunrise"),
            "sunset": wx.get("sunset"),
            "moon_phase": wx.get("moonPhase"),
            "moon_illum": wx.get("moonIllum"),
        }
        # A 200-but-empty upstream ({}, {"error": "warming up"}, temp
        # missing/null) would paint an all-dashes card that still looks LIVE.
        # Treat a non-finite temp as offline so it is neither shown as good nor
        # cached as good. bool is an int subclass — exclude it explicitly.
        temp = result.get("temp")
        if not isinstance(temp, (int, float)) or isinstance(temp, bool):
            return {"available": False}
        if not result["spark"]:
            # A valid temp but no chart series is exactly the silent failure that
            # hid the temp chart for weeks (the old code read a feed key that
            # never existed). Say so LOUDLY so the next feed-shape drift is caught
            # in the logs instead of just quietly blanking the chart.
            log.warning("weather feed has a valid temp but no usable tempSeries "
                        "(temp chart hidden); wx keys: %s", sorted(wx)[:20])
    except Exception as e:
        log.warning("weather tile wx.json unavailable: %s", e)
        return {"available": False}   # not cached: retry on the next poll
    _weather_cache[base] = (time.monotonic() + WEATHER_TTL, result)
    return result


# --- laundry (washer/dryer via Home Assistant) -----------------------------
#
# Phase sets for the lg_thinq "Current status" enum, verified live against
# HA 2026.8.1 + an LG washer/dryer pair (2026-08-17). A state none of these
# sets has seen (firmware vocabulary drift) fails toward "running" when a
# finish time exists — never hide an active cycle — and to "idle" otherwise
# (see _laundry_phase).
LAUNDRY_DONE = {"end"}
LAUNDRY_PAUSED = {"pause", "frozen_prevent_pause", "rinse_hold"}
LAUNDRY_IDLE = {"initial", "power_off", "frozen_prevent_initial"}
LAUNDRY_ERROR = {"error"}
# HA's own not-a-reading states (integration lost the appliance / entity gone)
LAUNDRY_GONE = {"unknown", "unavailable", ""}
# The in-cycle vocabulary observed on the live pair.
LAUNDRY_RUNNING = {
    "running", "spinning", "rinsing", "prewash", "detecting", "drying",
    "cooling", "wrinkle_care", "refreshing", "add_drain", "detergent_amount",
    "frozen_prevent_running",
}


def _laundry_phase(status: str | None, finishes_at: str | None) -> str:
    s = str(status or "").strip().lower()   # str(): a non-string state must not raise
    if s in LAUNDRY_GONE:
        return "offline"
    if s in LAUNDRY_DONE:
        return "done"
    if s in LAUNDRY_PAUSED:
        return "paused"
    if s in LAUNDRY_ERROR:
        return "error"
    if s in LAUNDRY_IDLE:
        return "idle"
    if s == "reserved":          # delayed start scheduled, drum not yet moving
        return "reserved"
    if s in LAUNDRY_RUNNING:
        return "running"
    # An unrecognized status (firmware vocabulary drift) fails toward
    # "running" when a FUTURE finish time exists — never hide an active
    # cycle — and "idle" otherwise. Future matters: remaining-time sensors
    # that latch their last value instead of going unknown would otherwise
    # pin an off machine in a perpetual tumbling "Any minute".
    log.warning("laundry: unrecognized status %r (phase from finish time)", s)
    return "running" if _laundry_future(finishes_at) else "idle"


def _laundry_minutes_to(iso: str | None) -> float | None:
    """Signed minutes from now until the ISO instant (negative = already
    passed). None for anything unparseable — and for naive (no offset)
    timestamps, which cannot honestly be compared to now."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60.0


def _laundry_future(iso: str | None) -> bool:
    """True iff the instant parses and lies in the future. NOT the negation
    of _laundry_past: both are False for naive/unparseable timestamps."""
    mt = _laundry_minutes_to(iso)
    return mt is not None and mt > 0


def _laundry_past(iso: str | None) -> bool:
    """True iff the instant parses and has already passed."""
    mt = _laundry_minutes_to(iso)
    return mt is not None and mt <= 0


def _laundry_ts(raw: object) -> str | None:
    """An HA timestamp state, validated: the ISO string as-is when it parses,
    else None. HA reports 'unknown'/'unavailable' as states too — those are
    not timestamps. Non-strings (a flaky upstream serving a number/null) are
    refused here rather than raising TypeError out of the fail-soft tile."""
    if not isinstance(raw, str) or not raw or raw in LAUNDRY_GONE:
        return None
    try:
        dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return raw


# Entities currently in a warned-about outage — failure logging is EDGE-
# triggered (one warning going down, one info coming back) because the 5s
# background watcher would otherwise turn a prolonged HA outage into ~48
# warning lines a minute, drowning the log used to diagnose that very
# outage (and burying any one-time crash line under the flood).
_ha_warned: set[str] = set()


async def _ha_state(client, base: str, token: str, entity: str) -> dict | None:
    """One HA entity state, or None on any failure (auth, LAN, non-dict body).
    Failures are per-entity so one flaky sensor can't sink the whole card."""
    try:
        r = await client.get(f"{base}/api/states/{entity}", timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError(f"non-dict body {type(body).__name__}")
    except Exception as e:
        if entity not in _ha_warned:
            _ha_warned.add(entity)
            log.warning("laundry: HA state %s unavailable: %s "
                        "(quiet until it recovers)", entity, e)
        else:
            log.debug("laundry: HA state %s still unavailable: %s", entity, e)
        return None
    if entity in _ha_warned:
        _ha_warned.discard(entity)
        log.info("laundry: HA state %s recovered", entity)
    return body


async def laundry_tile(client, cfg, token: str) -> dict:
    """Washer/dryer status proxied from Home Assistant into a trimmed,
    fail-soft tile: ``{available, machines: [{id, label, kind, phase, status,
    finishes_at, status_since}]}``.

    Per machine, two entity reads (concurrent across all machines): the
    Current-status enum drives ``phase`` (running / paused / done / idle /
    reserved / error / offline) with the raw ``status`` passed through for the
    label; the Remaining-time timestamp sensor becomes ``finishes_at`` (an
    absolute ISO "finishes at" moment — the frontend counts down against it
    live between polls). ``status_since`` is the status entity's
    ``last_changed``, so a machine sitting in "end" carries WHEN it finished.

    A failed status read marks that machine ``offline``; a failed remaining
    read only drops ``finishes_at``. Only the whole-HA case — every status
    read failing at once — returns ``{"available": False}``, and errors are
    never cached, so a transient blip retries on the next poll."""
    laundry = getattr(cfg, "laundry", None)
    if not laundry or not token:
        return {"available": False}   # not configured; no fetch attempted
    base = laundry["ha_base"]
    cached = _laundry_cache.get(base)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    machines = laundry["machines"]
    fetches = []
    for m in machines:
        fetches.append(_ha_state(client, base, token, m["status_entity"]))
        fetches.append(_ha_state(client, base, token, m["remaining_entity"]))
    states = await asyncio.gather(*fetches)
    out = []
    any_status = False
    for i, m in enumerate(machines):
        st, rem = states[2 * i], states[2 * i + 1]
        status = st.get("state") if st else None
        finishes = _laundry_ts(rem.get("state") if rem else None)
        if st is not None:
            any_status = True
        phase = _laundry_phase(status, finishes)
        out.append({
            "id": m["id"], "label": m["label"], "kind": m["kind"],
            "phase": phase,
            "status": str(status or "").strip().lower() or None,
            "finishes_at": finishes,
            "status_since": _laundry_ts(st.get("last_changed") if st else None),
        })
    if not any_status:
        # HA itself is unreachable (or the token is dead): the whole card is
        # offline. Not cached, so recovery shows on the next poll.
        return {"available": False}
    result = {"available": True, "machines": out}
    _laundry_cache[base] = (time.monotonic() + LAUNDRY_TTL, result)
    return result


async def camera_snapshot(client, cfg, src: str = "cam") -> tuple[bytes, str] | None:
    try:
        r = await client.get(f"{cfg.go2rtc_base}/api/frame.jpeg?src={src}",
                             timeout=CAMERA_TIMEOUT)
        r.raise_for_status()
        if not r.content:
            return None   # go2rtc 200s with an empty body when the camera
        return (r.content, "image/jpeg")   # behind it is down — that's offline
    except Exception as e:
        log.debug("camera %s snapshot unavailable: %s", src, e)
        return None
