import asyncio
import logging

import httpx

from family_hub import tiles
from family_hub.config import Config


def cfg():
    return Config(climate_base="http://climate", weather_base="http://weather",
                  go2rtc_base="http://cam")


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_tile(coro_fn, handler):
    async def run():
        async with make_client(handler) as c:
            return await coro_fn(c, cfg())
    return asyncio.run(run())


# Synthetic /api/rooms + /api/humidity payloads — generic room labels only, no
# house data. "Outside" is a generic sensor label; it is included in the
# passthrough on purpose (Task 9 filters it in the frontend, not this proxy).
ROOMS_OK = {"available": True, "rooms": [
    {"name": "Living Room", "channel": 1, "temp_f": 71.0, "humidity": 48,
     "stale": False, "battery_low": False},
    {"name": "Garage", "channel": 2, "temp_f": 66.5, "humidity": 52,
     "stale": True, "battery_low": True},
    {"name": "Outside", "channel": 3, "temp_f": 84.0, "humidity": 40,
     "stale": False, "battery_low": False}]}
HUMIDITY_OK = {"indoor_rh": 45, "indoor_dp": 51.2, "outdoor_aqi": 30}


def test_climate_happy_maps_rooms_and_humidity():
    tiles.reset_caches()

    def handler(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json=ROOMS_OK)
        if req.url.path == "/api/humidity":
            return httpx.Response(200, json=HUMIDITY_OK)
        return httpx.Response(404)
    t = run_tile(tiles.climate_tile, handler)
    assert t["available"] is True
    # each room trimmed to name/channel/temp_f/humidity/stale (no battery_low)
    assert t["rooms"] == [
        {"name": "Living Room", "channel": 1, "temp_f": 71.0,
         "humidity": 48, "stale": False},
        {"name": "Garage", "channel": 2, "temp_f": 66.5,
         "humidity": 52, "stale": True},
        {"name": "Outside", "channel": 3, "temp_f": 84.0,
         "humidity": 40, "stale": False}]
    # the Outside sensor is PRESENT in the passthrough — T9 filters it, not T8
    assert any(rm["name"] == "Outside" for rm in t["rooms"])
    # stale flags survive per-room
    assert t["rooms"][1]["stale"] is True
    # indoor RH/DP come from the secondary /api/humidity fetch
    assert t["indoor_rh"] == 45
    assert t["indoor_dp"] == 51.2


def test_climate_humidity_down_still_returns_rooms_rh_dp_none():
    # The SECONDARY /api/humidity fetch fails: indoor_rh/indoor_dp degrade to
    # None but the rooms (the PRIMARY) still come through — a secondary failure
    # must NOT sink the whole card.
    tiles.reset_caches()

    def handler(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json=ROOMS_OK)
        return httpx.Response(500)   # /api/humidity down
    t = run_tile(tiles.climate_tile, handler)
    assert t["available"] is True
    assert len(t["rooms"]) == 3
    assert t["indoor_rh"] is None and t["indoor_dp"] is None


def test_climate_humidity_non_dict_body_degrades_to_none():
    # /api/humidity served as valid-but-non-dict JSON must degrade indoor_rh/dp
    # to None, not throw — the rooms still come through.
    tiles.reset_caches()

    def handler(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json=ROOMS_OK)
        return httpx.Response(200, json=[])   # non-dict humidity body
    t = run_tile(tiles.climate_tile, handler)
    assert t["available"] is True
    assert len(t["rooms"]) == 3
    assert t["indoor_rh"] is None and t["indoor_dp"] is None


def test_climate_non_dict_rooms_body_unavailable():
    # A flaky LAN device can serve valid-but-non-dict JSON from /api/rooms.
    # rooms.get(...) would raise AttributeError; the tile must fail soft.
    for body in ([], None, "error", 42):
        tiles.reset_caches()

        def handler(req, _b=body):
            if req.url.path == "/api/rooms":
                return httpx.Response(200, json=_b)
            return httpx.Response(404)
        assert run_tile(tiles.climate_tile, handler) == {"available": False}


def test_climate_rooms_list_and_items_guarded():
    # A non-list "rooms" -> empty list; non-dict room items are skipped. Neither
    # throws (which would 500 the route).
    tiles.reset_caches()

    def not_a_list(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json={"available": True, "rooms": "oops"})
        return httpx.Response(200, json=HUMIDITY_OK)
    t = run_tile(tiles.climate_tile, not_a_list)
    assert t["available"] is True and t["rooms"] == []

    tiles.reset_caches()

    def mixed_items(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json={"available": True, "rooms": [
                {"name": "Kitchen", "channel": 4, "temp_f": 70.0,
                 "humidity": 44, "stale": False},
                "not-a-dict", None]})
        return httpx.Response(200, json=HUMIDITY_OK)
    t = run_tile(tiles.climate_tile, mixed_items)
    assert [rm["name"] for rm in t["rooms"]] == ["Kitchen"]


def test_climate_base_unset_no_fetch():
    tiles.reset_caches()

    def fail(req):
        raise AssertionError("must not fetch when climate_base is empty")

    async def run():
        async with make_client(fail) as c:
            return await tiles.climate_tile(c, Config(climate_base=""))
    assert asyncio.run(run()) == {"available": False}


def test_climate_rooms_500_unavailable():
    tiles.reset_caches()

    def handler(req):
        return httpx.Response(500)
    assert run_tile(tiles.climate_tile, handler) == {"available": False}


def test_climate_connect_error_unavailable_not_cached():
    tiles.reset_caches()

    def boom(req):
        raise httpx.ConnectError("refused")
    assert run_tile(tiles.climate_tile, boom) == {"available": False}

    # a transient blip must not poison the cache: the next poll succeeds
    def ok(req):
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json=ROOMS_OK)
        return httpx.Response(200, json=HUMIDITY_OK)
    assert len(run_tile(tiles.climate_tile, ok)["rooms"]) == 3


# Synthetic wx.json summary — generic values only, no house data. Includes a
# tempSeries so the temperature-chart mapping path is exercised.
WX_OK = {
    "temp": 72.0, "tempUnit": "F", "conditions": "Partly Cloudy",
    "feelsLike": 74.0, "feelsDesc": "Comfortable",
    "fcLow": 58.0, "fcHigh": 81.0,
    "uvIndex": 6, "uvDesc": "High",
    "aqi": 42, "aqiCategory": "Good",
    "humidity": 55, "dewPoint": 54.0,
    "weatherStale": False,
    "tempSeries": {"temps": [70.0, 71.5, 73.0, 74.0, 72.0], "nowIndex": 2},
    "sunrise": "06:15", "sunset": "20:15",
    "moonPhase": "Waxing Crescent", "moonIllum": 29.1,
}


def test_weather_maps_wx_json_to_trimmed_shape():
    tiles.reset_caches()

    def ok(req):
        assert req.url.path == "/wx.json"
        return httpx.Response(200, json=WX_OK)
    t = run_tile(tiles.weather_tile, ok)
    assert t == {
        "available": True, "temp": 72.0, "unit": "F",
        "conditions": "Partly Cloudy", "feels": 74.0, "feels_desc": "Comfortable",
        "low": 58.0, "high": 81.0, "uv": 6, "uv_desc": "High",
        "aqi": 42, "aqi_cat": "Good", "humidity": 55, "dew_point": 54.0,
        "spark": [70.0, 71.5, 73.0, 74.0, 72.0], "spark_now": 2, "stale": False,
        "sunrise": "06:15", "sunset": "20:15",
        "moon_phase": "Waxing Crescent", "moon_illum": 29.1,
    }


def test_weather_missing_keys_become_none_not_crash():
    tiles.reset_caches()

    def sparse(req):
        return httpx.Response(200, json={"temp": 60.0})
    t = run_tile(tiles.weather_tile, sparse)
    assert t["available"] is True
    assert t["temp"] == 60.0
    assert t["unit"] is None and t["high"] is None and t["stale"] is None
    assert t["spark"] == []   # no tempSeries -> empty, frontend hides the chart
    assert t["spark_now"] is None
    # sky-scene fields degrade to None too: the frontend then uses its fixed
    # phase boundaries and draws a full moon
    assert t["sunrise"] is None and t["sunset"] is None
    assert t["moon_phase"] is None and t["moon_illum"] is None


def test_weather_non_dict_body_unavailable_not_500():
    # A flaky LAN device can serve valid-but-non-dict JSON. wx.get(...) would
    # raise AttributeError; the tile must still fail soft to {available:false}.
    for body in ([], None, "error", 42):
        tiles.reset_caches()

        def served(req, _b=body):
            return httpx.Response(200, json=_b)
        assert run_tile(tiles.weather_tile, served) == {"available": False}


def test_weather_spark_handles_tempseries_shapes():
    # valid series -> temps + now passed through
    assert tiles._weather_spark(
        {"tempSeries": {"temps": [70.0, 71.0, 72.0], "nowIndex": 1}}
    ) == {"temps": [70.0, 71.0, 72.0], "now": 1}
    # out-of-range / non-int / bool nowIndex -> now is None (never a bad marker)
    for bad in (5, -1, "1", True, None):
        assert tiles._weather_spark(
            {"tempSeries": {"temps": [70.0, 71.0], "nowIndex": bad}}
        )["now"] is None
    # ANY non-numeric point makes the whole series malformed -> hidden, rather
    # than silently dropped (which would shift the positional nowIndex marker)
    assert tiles._weather_spark(
        {"tempSeries": {"temps": [70.0, "x", 72.0], "nowIndex": 0}}
    ) == {"temps": [], "now": None}
    # a non-iterable / non-list / empty temps degrades to empty, never crashes
    for temps in (42, True, None, "70", {}, []):
        assert tiles._weather_spark(
            {"tempSeries": {"temps": temps, "nowIndex": 0}}
        ) == {"temps": [], "now": None}
    # absent / non-dict tempSeries (and an empty dict) -> empty, no marker
    for wx in ({}, {"tempSeries": None}, {"tempSeries": [70, 71]}, {"tempSeries": {}}):
        assert tiles._weather_spark(wx) == {"temps": [], "now": None}


def test_weather_spark_warns_on_good_temps_with_bad_now_index(caplog):
    # A valid series whose nowIndex is missing/malformed renders UNANCHORED on
    # the wall (no time ticks, no "now" marker) — legitimate fail-soft, but a
    # feed-shape drift (like the hourlyTemps guess before it) must be loud in
    # the logs, not discovered by squinting at a chart with no clock labels.
    import logging
    with caplog.at_level(logging.WARNING, logger="family_hub.tiles"):
        out = tiles._weather_spark(
            {"tempSeries": {"temps": [70.0, 71.0, 72.0], "nowIndex": "2"}})
    assert out == {"temps": [70.0, 71.0, 72.0], "now": None}
    assert any("nowIndex" in r.message for r in caplog.records)
    # a fully valid anchor stays quiet
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="family_hub.tiles"):
        tiles._weather_spark(
            {"tempSeries": {"temps": [70.0, 71.0, 72.0], "nowIndex": 1}})
    assert not caplog.records


def test_weather_warns_when_temp_present_but_no_chart_series(caplog):
    # The original bug: a valid temp with no usable series silently blanked the
    # chart and nobody noticed. A recurrence must now show up in the logs.
    tiles.reset_caches()

    def served(req):
        return httpx.Response(200, json={"temp": 61.0, "tempUnit": "F"})  # no tempSeries
    with caplog.at_level(logging.WARNING, logger="family_hub.tiles"):
        t = run_tile(tiles.weather_tile, served)
    assert t["available"] is True and t["spark"] == [] and t["spark_now"] is None
    assert any("no usable tempSeries" in r.getMessage() for r in caplog.records), \
        "a valid-temp-but-empty-chart feed must warn (the original silent bug)"


def test_weather_base_unset_no_fetch():
    tiles.reset_caches()

    def fail(req):
        raise AssertionError("must not fetch when weather_base is empty")

    async def run():
        async with make_client(fail) as c:
            return await tiles.weather_tile(c, Config(weather_base=""))
    assert asyncio.run(run()) == {"available": False}


def test_weather_fetch_error_unavailable_not_cached():
    tiles.reset_caches()

    def boom(req):
        raise httpx.ConnectError("refused")
    assert run_tile(tiles.weather_tile, boom) == {"available": False}

    # a transient blip must not poison the cache: the next poll succeeds
    def ok(req):
        return httpx.Response(200, json=WX_OK)
    assert run_tile(tiles.weather_tile, ok)["temp"] == 72.0


def test_weather_200_but_empty_temp_is_unavailable_and_not_cached():
    # A 200 response that is a dict but carries no usable temperature ({},
    # {"error": ...}, or temp missing/null) must be treated as OFFLINE, not
    # painted as a live all-dashes card. It must also NOT be cached as good, so
    # a subsequent good poll returns real data (a warming-up feed recovers).
    for body in ({}, {"error": "warming up"}, {"temp": None, "tempUnit": "F"}):
        tiles.reset_caches()

        def empty(req, _b=body):
            return httpx.Response(200, json=_b)
        assert run_tile(tiles.weather_tile, empty) == {"available": False}

        # the empty read was NOT cached: the next (good) poll returns real data
        def ok(req):
            return httpx.Response(200, json=WX_OK)
        assert run_tile(tiles.weather_tile, ok)["temp"] == 72.0


def test_weather_success_is_cached_within_ttl_no_refetch():
    # PT2: prove SUCCESS is cached — a second poll within the TTL returns the
    # FIRST cached result without re-fetching (call count stays 1).
    tiles.reset_caches()
    calls = {"n": 0}

    def ok(req):
        calls["n"] += 1
        return httpx.Response(200, json=WX_OK)
    first = run_tile(tiles.weather_tile, ok)
    assert first["temp"] == 72.0 and calls["n"] == 1

    # within the TTL, swap in a handler that would raise if hit — the cache must
    # serve the first result and never call it.
    def boom(req):
        calls["n"] += 1
        raise AssertionError("must not re-fetch within the TTL")
    second = run_tile(tiles.weather_tile, boom)
    assert second == first and calls["n"] == 1


def test_climate_success_is_cached_within_ttl_no_refetch():
    # PT2: same success-is-cached proof for the climate tile.
    tiles.reset_caches()
    calls = {"n": 0}

    def ok(req):
        calls["n"] += 1
        if req.url.path == "/api/rooms":
            return httpx.Response(200, json=ROOMS_OK)
        return httpx.Response(200, json=HUMIDITY_OK)
    first = run_tile(tiles.climate_tile, ok)
    assert len(first["rooms"]) == 3 and calls["n"] >= 1
    seen = calls["n"]

    def boom(req):
        calls["n"] += 1
        raise AssertionError("must not re-fetch within the TTL")
    second = run_tile(tiles.climate_tile, boom)
    assert second == first and calls["n"] == seen


def test_camera_happy_and_error():
    jpeg = b"\xff\xd8\xff\xe0jpegbytes"

    def ok(req):
        assert req.url.path == "/api/frame.jpeg"
        return httpx.Response(200, content=jpeg)
    assert run_tile(tiles.camera_snapshot, ok) == (jpeg, "image/jpeg")

    def boom(req):
        return httpx.Response(502)
    assert run_tile(tiles.camera_snapshot, boom) is None

    # go2rtc answers 200 with an EMPTY body while a producer is connected but
    # frameless (camera down behind the bridge) — that is NOT a live camera
    def empty(req):
        return httpx.Response(200, content=b"")
    assert run_tile(tiles.camera_snapshot, empty) is None
