"""GET /health/full: the report a deploy gate reads (deep_health.py).

/health said "ok" for a day on 2026-09-17 while the hub ran with an empty
HA_TOKEN and the laundry card was gone. These tests pin that the full report
fails, by name, for every way the hub can be up and still not working: stale
or missing data, a read the previous container made, a missing setting, a
config.json that is not the one the deploy shipped.
"""
import asyncio
import datetime as dt
import importlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from family_hub import db as fdb
from family_hub import deep_health as dh
from family_hub import tiles as ftiles

NOW = 1_790_000_000.0
START = NOW - 600


# --- pure judgements -------------------------------------------------------

def test_tile_source_off_when_not_configured_or_disabled():
    for configured, enabled in ((False, False), (True, False)):
        s = dh.tile_source("weather", configured=configured, enabled=enabled,
                           state=None, available=False, now=NOW, max_age_s=600)
        assert s["ok"] is True and s["status"] == "off"
        assert s["configured"] is configured


def test_tile_source_ok_needs_a_fresh_upstream_stamp():
    st = {"last_ok": NOW - 5, "data_ts": NOW - 30}
    s = dh.tile_source("weather", configured=True, enabled=True, state=st,
                       available=True, now=NOW, max_age_s=600)
    assert s["ok"] is True and s["status"] == "ok", (s["status"], s.get("error"), s.get("machines_offline"))
    assert s["data_age_s"] == 30.0
    assert s["data_ts"] == dh.iso(NOW - 30)


def test_tile_source_answering_with_old_data_is_stale():
    """The feed answering is not enough: an adapter that stopped writing keeps
    serving its last wx.json forever."""
    st = {"last_ok": NOW - 5, "data_ts": NOW - 601}
    s = dh.tile_source("weather", configured=True, enabled=True, state=st,
                       available=True, now=NOW, max_age_s=600)
    assert s["ok"] is False and s["status"] == "stale"


def test_tile_source_without_a_data_stamp_fails():
    st = {"last_ok": NOW - 5, "data_ts": None}
    s = dh.tile_source("climate", configured=True, enabled=True, state=st,
                       available=True, now=NOW, max_age_s=900)
    assert s["ok"] is False and s["status"] == "error"
    assert "no timestamp" in s["last_error"]


@pytest.mark.parametrize("kind,word", [("unreachable", "upstream_down"),
                                        ("upstream", "upstream_down"),
                                        ("invalid", "error"), (None, "waiting")])
def test_tile_source_failure_words(kind, word):
    st = {"last_error": "x", "last_error_kind": kind} if kind else {}
    s = dh.tile_source("fleet", configured=True, enabled=True, state=st,
                       available=False, now=NOW, max_age_s=300)
    assert s["ok"] is False and s["status"] == word


def test_tile_source_never_trusts_an_old_success_when_unavailable_now():
    st = {"last_ok": NOW - 5, "data_ts": NOW - 5, "last_error_kind": "unreachable"}
    s = dh.tile_source("fleet", configured=True, enabled=True, state=st,
                       available=False, now=NOW, max_age_s=300)
    assert s["ok"] is False


def _cal(ok=True, last=None, **kw):
    d = {"ok": ok, "last_sync": dt.datetime.fromtimestamp(
        NOW - 60 if last is None else last, dt.timezone.utc).isoformat()}
    d.update(kw)
    return d


def test_calendar_ok_only_for_a_sync_this_process_made():
    ok = dh.calendar_source(configured=True, enabled={"google": True, "icloud": False},
                            statuses={"google": _cal()}, now=NOW, started_at=START)
    assert ok["ok"] is True and ok["status"] == "ok"
    # the previous container's last good sync, persisted in hub.db: proves nothing
    old = dh.calendar_source(configured=True, enabled={"google": True, "icloud": False},
                             statuses={"google": _cal(last=START - 1)},
                             now=NOW, started_at=START)
    assert old["ok"] is False and old["status"] == "waiting"


def test_calendar_is_judged_raw_not_through_the_walls_grace():
    """The wall hides a fresh error for an hour; the gate must not."""
    s = dh.calendar_source(configured=True, enabled={"google": True, "icloud": False},
                           statuses={"google": _cal(ok=False, error="HTTP 503")},
                           now=NOW, started_at=START)
    assert s["ok"] is False and s["status"] == "error"
    assert s["sources"]["google"]["error"] == "HTTP 503"


def test_calendar_worst_source_wins_and_needs_auth_is_loudest():
    s = dh.calendar_source(
        configured=True, enabled={"google": True, "icloud": True},
        statuses={"google": _cal(), "icloud": _cal(ok=False, needs_auth=True)},
        now=NOW, started_at=START)
    assert s["status"] == "needs_auth" and s["ok"] is False
    stale = dh.calendar_source(configured=True, enabled={"google": True, "icloud": False},
                               statuses={"google": _cal(last=NOW - 901)},
                               now=NOW, started_at=NOW - 2000)
    assert stale["status"] == "stale"


def test_calendar_disabled_everywhere_is_off():
    s = dh.calendar_source(configured=True, enabled={"google": False, "icloud": False},
                           statuses={}, now=NOW, started_at=START)
    assert s["status"] == "off" and s["ok"] is True


def _laundry(**kw):
    base = dict(configured=True, enabled=True, config_error=None, token_present=True,
                auth_rejected=False, watching=True, last_ok=NOW - 3,
                snapshot={"available": True, "machines": [{"id": "washer", "phase": "idle"}]},
                now=NOW, started_at=START)
    base.update(kw)
    return dh.laundry_source(**base)


def test_laundry_ok():
    s = _laundry()
    assert s["ok"] is True and s["status"] == "ok" and s["data_age_s"] == 3.0


@pytest.mark.parametrize("kw,word", [
    ({"token_present": False}, "needs_auth"),           # the 2026-09-17 incident
    ({"auth_rejected": True}, "needs_auth"),            # a revoked token
    ({"config_error": "no valid machines"}, "error"),
    ({"watching": False}, "error"),
    ({"last_ok": None}, "waiting"),
    ({"last_ok": NOW - 61}, "stale"),
    ({"snapshot": {"available": True, "machines": [
        {"id": "washer", "phase": "offline"}, {"id": "dryer", "phase": "idle"}]}},
     "degraded"),
])
def test_laundry_failures(kw, word):
    s = _laundry(**kw)
    assert s["ok"] is False and s["status"] == word


def test_laundry_offline_machine_is_named():
    s = _laundry(snapshot={"available": True, "machines": [
        {"id": "washer", "phase": "offline"}]})
    assert s["machines_offline"] == ["washer"]


def test_cameras_need_every_configured_stream():
    ok = dh.cameras_source(configured=True, enabled=True, wanted=["a", "a_hd"],
                           streams={"a": {}, "a_hd": {}}, error=None)
    assert ok["ok"] is True
    miss = dh.cameras_source(configured=True, enabled=True, wanted=["a", "b"],
                             streams={"a": {}}, error=None)
    assert miss["ok"] is False and miss["streams_missing"] == ["b"]
    down = dh.cameras_source(configured=True, enabled=True, wanted=["a"],
                             streams=None, error="refused")
    assert down["ok"] is False and down["error"] == "refused"


def test_config_block_matches_only_when_shipped_loaded_and_on_disk_agree():
    info = {"config_sha256": "abc"}
    assert dh.config_block("c", "abc", "abc", info)["matches_deploy"] is True
    # the single-file bind mount trap: the process still runs the old bytes
    stale = dh.config_block("c", "old", "old", info)
    assert stale["matches_deploy"] is False
    # changed under the running process
    moved = dh.config_block("c", "abc", "new", info)
    assert moved["matches_deploy"] is False and moved["changed_since_start"] is True
    # no deploy record (a dev run): unknown, which a gate treats as a failure
    assert dh.config_block("c", "abc", "abc", None)["matches_deploy"] is None
    assert dh.config_block("c", None, None, info)["matches_deploy"] is False


def test_read_build_info(tmp_path):
    p = tmp_path / "build_info.json"
    assert dh.read_build_info(p) is None
    p.write_text("not json")
    assert dh.read_build_info(p) is None
    p.write_text("[1]")
    assert dh.read_build_info(p) is None
    p.write_text(json.dumps({"engine_commit": "abc1234", "overlay_commit": 5,
                             "config_sha256": "f" * 64, "extra": "x"}))
    assert dh.read_build_info(p) == {"engine_commit": "abc1234", "overlay_commit": None,
                                     "config_sha256": "f" * 64, "built_at": None}


def test_assemble_lists_every_failure_by_name():
    r = dh.assemble(
        now=NOW, started_at=START, version="1.0.0", build="b", build_info=None,
        config={"matches_deploy": False}, db_ok=False, db_error="locked",
        settings={"ha_token": dh.setting(True, False, "HA_TOKEN must reach it")},
        sources={"weather": {"ok": False, "status": "stale"},
                 "fleet": {"ok": True, "status": "ok"}},
        notes={})
    assert r["status"] == "degraded"
    assert r["problems"] == [
        "db: locked",
        "config: the running config.json is not the one the deploy shipped",
        "setting ha_token: HA_TOKEN must reach it",
        "weather: stale"]
    ok = dh.assemble(now=NOW, started_at=START, version="1", build="b", build_info=None,
                     config={"matches_deploy": None}, db_ok=True, db_error=None,
                     settings={}, sources={"fleet": {"ok": True}}, notes={})
    assert ok["status"] == "ok" and ok["problems"] == []


def test_iso_and_parse_ts_round_trip():
    assert dh.parse_ts(dh.iso(NOW)) == NOW
    assert dh.parse_ts("2026-09-23T00:00:00-07:00") == dh.parse_ts("2026-09-23T07:00:00Z")
    assert dh.parse_ts(NOW * 1000) == NOW
    assert dh.parse_ts(True) is None and dh.parse_ts("junk") is None


# --- tiles record each real fetch ---------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://x")
            raise httpx.HTTPStatusError("boom", request=req,
                                        response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._p


class _Client:
    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    async def get(self, url, **k):
        self.calls += 1
        return self.fn(url)


def _cfg(**kw):
    from family_hub.config import Config
    return Config(**kw)


@pytest.fixture(autouse=True)
def _clean_tiles():
    ftiles.reset_caches()
    yield
    ftiles.reset_caches()


def test_weather_tile_records_the_feeds_own_timestamp():
    async def _t():
        wx = {"ts": NOW - 20, "temp": 60.0, "tempSeries": []}
        client = _Client(lambda url: _Resp(wx))
        t = await ftiles.weather_tile(client, _cfg(weather_base="http://w"))
        assert t["available"] is True
        st = ftiles.SOURCE_STATE["weather"]
        assert st["data_ts"] == NOW - 20 and st["last_ok"] >= time.time() - 5
        # a cache hit is not a fetch and records nothing new
        st["last_ok"] = 1.0
        await ftiles.weather_tile(client, _cfg(weather_base="http://w"))
        assert client.calls == 1 and ftiles.SOURCE_STATE["weather"]["last_ok"] == 1.0
    asyncio.run(_t())


def test_tile_errors_are_recorded_with_their_kind():
    async def _t():
        def refused(url):
            raise httpx.ConnectError("refused")
        await ftiles.weather_tile(_Client(refused), _cfg(weather_base="http://w"))
        assert ftiles.SOURCE_STATE["weather"]["last_error_kind"] == "unreachable"
        await ftiles.climate_tile(_Client(lambda u: _Resp({}, 503)), _cfg(climate_base="http://c"))
        assert ftiles.SOURCE_STATE["climate"]["last_error_kind"] == "upstream"
        await ftiles.weather_tile(_Client(lambda u: _Resp({"temp": None})), _cfg(weather_base="http://w2"))
        assert ftiles.SOURCE_STATE["weather"]["last_error"] == "wx.json has no usable temp"
    asyncio.run(_t())


def test_climate_tile_stamps_the_freshest_room():
    async def _t():
        rooms = {"rooms": [{"name": "Up", "age_s": 400}, {"name": "Down", "age_s": 90},
                           {"name": "Gone", "age_s": None}]}
        before = time.time()
        t = await ftiles.climate_tile(_Client(lambda u: _Resp(rooms)), _cfg(climate_base="http://c"))
        assert t["available"] is True
        ts = ftiles.SOURCE_STATE["climate"]["data_ts"]
        assert before - 91 <= ts <= time.time() - 89
    asyncio.run(_t())


def test_fleet_tile_stamps_generated_at():
    async def _t():
        body = {"generatedAt": "2026-09-23T07:47:46.502Z", "fleet": {}, "printer": {}}
        await ftiles.fleet_tile(_Client(lambda u: _Resp(body)),
                                _cfg(fleet={"base": "http://f"}))
        assert ftiles.SOURCE_STATE["fleet"]["data_ts"] == pytest.approx(
            dh.parse_ts("2026-09-23T07:47:46.502Z"))
    asyncio.run(_t())


# --- the route ---------------------------------------------------------------

def _write_cfg(tmp_path, **extra):
    c = {
        "climate_base": "http://climate", "weather_base": "http://weather",
        "go2rtc_base": "http://cam", "fleet": {"base": "http://fleet"},
        "calendars": [{"id": "cal", "label": "Fam", "color": "#5BC9F0"}],
        "cameras": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"}],
    }
    c.update(extra)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(c))
    return p


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """The app with a working house around it: every upstream answers with
    fresh data and the calendar synced after this process started."""
    cfgp = _write_cfg(tmp_path)
    (tmp_path / "token.json").write_text("{}")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(cfgp))
    monkeypatch.delenv("HA_TOKEN", raising=False)
    import family_hub.app as appmod
    importlib.reload(appmod)
    state = {"wx_ts": time.time() - 10, "streams": {"cam": {}, "cam_hd": {}}}

    def handler(req):
        url = str(req.url)
        if url.endswith("/wx.json"):
            return httpx.Response(200, json={"ts": state["wx_ts"], "temp": 61.0,
                                             "tempSeries": [{"t": 1, "v": 60}]})
        if url.endswith("/api/rooms"):
            return httpx.Response(200, json={"rooms": [{"name": "Up", "age_s": 60}]})
        if url.endswith("/api/humidity"):
            return httpx.Response(200, json={})
        if url.endswith("/api/rollup"):
            return httpx.Response(200, json={
                "generatedAt": dh.iso(time.time()), "fleet": {}, "printer": {}})
        if url.endswith("/api/streams"):
            if state["streams"] is None:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json=state["streams"])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(appmod, "_http",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(appmod.app) as client:
        client.get("/health")          # the app creates its schema on first use
        c = fdb.connect(str(tmp_path / "hub.db"))
        fdb.kv_set(c, "calendar_status", {
            "ok": True, "last_sync": dt.datetime.now(dt.timezone.utc).isoformat()})
        c.close()
        yield appmod, client, state, tmp_path
    ftiles.reset_caches()


def _full(client):
    r = client.get("/health/full")
    assert r.status_code == 200
    return r.json()


def test_route_all_green(hub):
    appmod, client, _, _ = hub
    r = _full(client)
    assert r["status"] == "ok", r["problems"]
    for name in ("calendar", "weather", "climate", "fleet", "cameras"):
        assert r["sources"][name]["ok"] is True, name
        assert r["sources"][name]["status"] == "ok", name
    assert r["sources"]["laundry"]["status"] == "off"     # not configured here
    assert r["db"]["ok"] is True
    assert r["settings"]["google_token"]["ok"] is True
    assert r["deploy"] is None and r["config"]["matches_deploy"] is None
    assert dh.parse_ts(r["sources"]["weather"]["data_ts"]) > time.time() - 60
    assert r["process_started_at"] == dh.iso(appmod.PROCESS_STARTED_AT)


def test_route_liveness_is_unchanged(hub):
    _, client, _, _ = hub
    assert client.get("/health").json() == {"status": "ok"}


def test_route_old_weather_feed_degrades(hub):
    _, client, state, _ = hub
    state["wx_ts"] = time.time() - 3600
    r = _full(client)
    assert r["status"] == "degraded"
    assert r["sources"]["weather"]["status"] == "stale"
    assert any(p.startswith("weather: stale") for p in r["problems"])


def test_route_calendar_synced_only_by_the_previous_container(hub, tmp_path):
    appmod, client, _, path = hub
    c = fdb.connect(str(path / "hub.db"))
    fdb.kv_set(c, "calendar_status", {"ok": True, "last_sync": dh.iso(
        appmod.PROCESS_STARTED_AT - 5)})
    c.close()
    r = _full(client)
    assert r["sources"]["calendar"]["status"] == "waiting"
    assert r["status"] == "degraded"


def test_route_missing_google_token(hub):
    appmod, client, _, path = hub
    (path / "token.json").unlink()
    r = _full(client)
    assert r["settings"]["google_token"]["ok"] is False
    assert r["status"] == "degraded"


def test_route_go2rtc_down_or_missing_a_stream(hub):
    _, client, state, _ = hub
    state["streams"] = {"cam": {}}
    r = _full(client)
    assert r["sources"]["cameras"]["streams_missing"] == ["cam_hd"]
    state["streams"] = None
    r = _full(client)
    assert r["sources"]["cameras"]["ok"] is False
    assert "ConnectError" in r["sources"]["cameras"]["error"]


def test_route_disabled_integration_is_off_not_failing(hub, tmp_path):
    appmod, client, state, path = hub
    c = fdb.connect(str(path / "hub.db"))
    fdb.set_integration_enabled(c, "weather", False)
    c.close()
    state["wx_ts"] = 0
    r = _full(client)
    assert r["sources"]["weather"] == {"configured": True, "enabled": False,
                                       "ok": True, "status": "off"}


def test_route_config_matches_the_deploy_record(hub, monkeypatch):
    appmod, client, _, _ = hub
    sha = appmod.CONFIG_LOADED_SHA256
    info = {"engine_commit": "a" * 40, "overlay_commit": "b" * 40,
            "config_sha256": sha, "built_at": "2026-09-23T00:00:00Z"}
    monkeypatch.setattr(appmod, "BUILD_INFO", info)
    r = _full(client)
    assert r["config"]["matches_deploy"] is True
    assert r["deploy"]["engine_commit"] == "a" * 40
    # the deploy shipped other bytes than the process runs on
    monkeypatch.setattr(appmod, "BUILD_INFO", dict(info, config_sha256="0" * 64))
    r = _full(client)
    assert r["config"]["matches_deploy"] is False
    assert r["status"] == "degraded"


def test_route_config_file_changed_under_the_process(hub, monkeypatch):
    appmod, client, _, path = hub
    monkeypatch.setattr(appmod, "BUILD_INFO", {
        "config_sha256": appmod.CONFIG_LOADED_SHA256})
    (path / "config.json").write_text(json.dumps({"calendars": []}))
    r = _full(client)
    assert r["config"]["changed_since_start"] is True
    assert r["config"]["matches_deploy"] is False


def test_route_db_unusable_fails_everything_it_cannot_judge(hub, monkeypatch):
    appmod, client, _, _ = hub

    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(appmod, "_db", boom)
    r = _full(client)
    assert r["db"]["ok"] is False and "disk gone" in r["db"]["error"]
    assert r["status"] == "degraded"
    # toggles unknown: judged as on, so the calendar cannot read as off
    assert r["sources"]["calendar"]["status"] != "off"


@pytest.fixture
def laundry_hub(tmp_path, monkeypatch):
    cfgp = _write_cfg(tmp_path, laundry={
        "ha_base": "http://ha:8123", "machines": [
            {"id": "washer", "label": "Washer", "kind": "washer",
             "status_entity": "sensor.w_status", "remaining_entity": "sensor.w_rem"}]})
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(cfgp))
    import family_hub.app as appmod
    return appmod, monkeypatch


def _offline(appmod, mp):
    """Every upstream refuses: these tests are about laundry alone."""
    def refuse(req):
        raise httpx.ConnectError("refused")
    mp.setattr(appmod, "_http", httpx.AsyncClient(transport=httpx.MockTransport(refuse)))


def test_route_laundry_with_an_empty_token_is_the_incident(laundry_hub):
    """2026-09-17: the box lost its .env and the hub ran with no HA_TOKEN. The
    card vanished and /health stayed ok. /health/full names it twice."""
    appmod, mp = laundry_hub
    mp.setenv("HA_TOKEN", "  ")
    importlib.reload(appmod)
    _offline(appmod, mp)
    with TestClient(appmod.app) as client:
        r = _full(client)
        assert client.get("/health").json() == {"status": "ok"}   # liveness is fine
    assert r["settings"]["ha_token"] == {
        "required": True, "present": False, "ok": False,
        "why": "laundry is configured, so HA_TOKEN must reach the container"}
    assert r["sources"]["laundry"]["status"] == "needs_auth"
    assert "setting ha_token: laundry is configured, so HA_TOKEN must reach the container" \
        in r["problems"]


def test_route_laundry_ok_needs_a_live_watcher_and_a_fresh_read(laundry_hub):
    appmod, mp = laundry_hub
    mp.setenv("HA_TOKEN", "t" * 60)
    importlib.reload(appmod)
    _offline(appmod, mp)
    with TestClient(appmod.app) as client:
        # DISABLE_SYNC: no watcher, so the card cannot be kept current
        assert _full(client)["sources"]["laundry"]["status"] == "error"

        class _Alive:
            def done(self):
                return False
        mp.setattr(appmod, "_laundry_watch_task", _Alive())
        assert _full(client)["sources"]["laundry"]["status"] == "waiting"
        mp.setattr(appmod, "_laundry_last_ok_wall", time.time())
        mp.setattr(appmod, "_laundry_snapshot", {"available": True, "machines": [
            {"id": "washer", "phase": "running"}]})
        s = _full(client)["sources"]["laundry"]
    assert s["ok"] is True and s["status"] == "ok", (s["status"], s.get("error"), s.get("machines_offline"))


def test_laundry_watcher_stamps_only_real_reads(laundry_hub):
    async def _t():
        """The hold re-stamps _laundry_snapshot_ts through a blip; the health
        stamp must only move when HA really answered."""
        appmod, mp = laundry_hub
        mp.setenv("HA_TOKEN", "t" * 60)
        importlib.reload(appmod)
        replies = iter([{"available": True, "machines": []}, {"available": False}])

        async def fake_tile(*a, **k):
            return next(replies)

        async def no_annotate(t):
            return t
        mp.setattr(appmod.tiles, "laundry_tile", fake_tile)
        mp.setattr(appmod, "_laundry_annotate_off_loop", no_annotate)
        await appmod._laundry_watch_tick()
        first = appmod._laundry_last_ok_wall
        assert first is not None and first >= time.time() - 5
        await appmod._laundry_watch_tick()          # unavailable: held, not stamped
        assert appmod._laundry_last_ok_wall == first
    asyncio.run(_t())
