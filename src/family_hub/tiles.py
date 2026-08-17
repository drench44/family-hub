"""Server-side proxies for the hub tiles. Each fetches an upstream on the box's
LAN and returns a small dict the frontend renders natively (no CORS, no iframes
on the home screen). Every fetch fails soft: a dead upstream yields an
``{"available": False}`` tile, never an exception."""
from __future__ import annotations

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


def reset_caches() -> None:
    """Clear the in-process tile caches. Tests call this for deterministic
    behavior when they monkeypatch the HTTP client and poll more than once."""
    _weather_cache.clear()
    _climate_cache.clear()


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
