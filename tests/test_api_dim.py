"""The dim settings through the app: the household dim hours (/api/dim-hours
and /api/hub's `dim` block) and "dim when nobody is home" (the away_dim
switch, the presence watcher tick, its fail-soft states, DEMO, /health/full).
"""
import asyncio
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from family_hub import dimming

LAUNDRY = {"ha_base": "http://ha.example:8123", "machines": [
    {"id": "washer", "label": "Washer", "kind": "washer",
     "status_entity": "sensor.w_status", "remaining_entity": "sensor.w_left"}]}


def _app(tmp_path, monkeypatch, extra=None, token="tok", demo=False):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 8138, "calendars": [], **(extra or {})}))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    if token is None:
        monkeypatch.delenv("HA_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HA_TOKEN", token)
    if demo:
        monkeypatch.setenv("DEMO", "1")
    else:
        monkeypatch.delenv("DEMO", raising=False)
    import family_hub.app as appmod
    importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_presence_watch_task", _Running())
    return appmod


class _Running:
    """Stands in for a live presence watcher task."""
    def done(self):
        return False


# Everyone's tracker reads away: a zero zone.home is then a real empty house.
EMPTY = [{"entity_id": "person.a", "state": "not_home"}]


def _zone(n, people=EMPTY):
    return {"zone.home": {"state": str(n)}, "states": people}


def _ticks(appmod, n=1):
    for _ in range(n):
        asyncio.run(appmod._presence_tick())


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """A hub with no Home Assistant at all."""
    appmod = _app(tmp_path, monkeypatch, token=None)
    with TestClient(appmod.app) as c:
        yield appmod, c


@pytest.fixture
def ha(tmp_path, monkeypatch):
    """A hub with a Home Assistant (via the laundry block) and a token."""
    appmod = _app(tmp_path, monkeypatch, {"laundry": LAUNDRY})
    with TestClient(appmod.app) as c:
        yield appmod, c


def _ha_answers(appmod, monkeypatch, states):
    """Point the app's shared client at a fake HA; returns the list of entity
    paths it was asked for."""
    asked = []

    def handler(req):
        ent = req.url.path.rsplit("/", 1)[-1]
        asked.append(ent)
        got = states.get(ent, 404)
        if isinstance(got, int):
            return httpx.Response(got)
        return httpx.Response(200, json=got)
    monkeypatch.setattr(appmod, "_http",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


def _row(c, iid):
    return next((i for i in c.get("/api/hub").json()["integrations"]
                 if i["id"] == iid), None)


# --- dim hours -------------------------------------------------------------

def test_hub_carries_the_default_dim_hours(plain):
    _, c = plain
    dim = c.get("/api/hub").json()["dim"]
    assert dim == {"hours": {"on": True, "start": "22:00", "end": "06:00"},
                   "away": None}


def test_dim_hours_round_trip_and_off(plain):
    _, c = plain
    r = c.put("/api/dim-hours", json={"start": "21:30", "end": "07:00"})
    assert r.status_code == 200
    assert r.json() == {"on": True, "start": "21:30", "end": "07:00"}
    assert c.get("/api/hub").json()["dim"]["hours"] == r.json()
    # a window inside one day
    r = c.put("/api/dim-hours", json={"start": "13:00", "end": "15:00"})
    assert r.json()["start"] == "13:00"
    # Off keeps the times so On brings them back
    assert c.put("/api/dim-hours", json={"on": False}).json() == \
        {"on": False, "start": "13:00", "end": "15:00"}
    assert c.get("/api/hub").json()["dim"]["hours"]["on"] is False
    assert c.put("/api/dim-hours", json={"on": True}).json()["on"] is True


@pytest.mark.parametrize("body", [
    {"start": "9pm"}, {"start": "22:00", "end": "22:00"}, {"on": "yes"}, {},
])
def test_dim_hours_refuses_bad_input(plain, body):
    _, c = plain
    r = c.put("/api/dim-hours", json=body)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    # nothing was saved
    assert c.get("/api/hub").json()["dim"]["hours"] == dimming.DIM_DEFAULT


def test_a_damaged_stored_record_never_breaks_the_wall(plain):
    appmod, c = plain
    from family_hub import db as fdb
    fdb.kv_set(appmod._db(), dimming.KV_KEY, "garbage")
    assert c.get("/api/hub").json()["dim"]["hours"] == dimming.DIM_DEFAULT


# --- away dimming ----------------------------------------------------------

def test_no_home_assistant_means_no_away_switch(plain):
    _, c = plain
    assert _row(c, "away_dim") is None
    assert c.get("/api/hub").json()["dim"]["away"] is None


def test_away_switch_is_listed_and_off_by_default(ha):
    _, c = ha
    row = _row(c, "away_dim")
    assert row["enabled"] is False and row["name"] == "Dim when nobody is home"
    assert c.get("/api/hub").json()["dim"]["away"] == \
        {"enabled": False, "state": "off", "home": None, "dim": False}
    # off means off on the switch's own body class too
    assert c.patch("/api/integrations/away_dim", json={"enabled": True}).json()["enabled"] is True
    assert _row(c, "away_dim")["enabled"] is True


def test_the_watcher_does_not_ask_home_assistant_while_switched_off(ha, monkeypatch):
    appmod, _ = ha
    asked = _ha_answers(appmod, monkeypatch, {"zone.home": {"state": "0"}})
    asyncio.run(appmod._presence_tick())
    assert asked == []


def test_nobody_home_dims_and_somebody_home_does_not(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    asked = _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)
    assert asked == ["zone.home", "states"] * 2
    assert c.get("/api/hub").json()["dim"]["away"] == \
        {"enabled": True, "state": "away", "home": 0, "dim": True}
    assert _row(c, "away_dim")["status"] is None
    _ha_answers(appmod, monkeypatch, _zone(3))
    asyncio.run(appmod._presence_tick())
    away = c.get("/api/hub").json()["dim"]["away"]
    assert away["state"] == "home" and away["home"] == 3 and away["dim"] is False


def test_home_assistant_unreadable_never_dims_and_the_row_says_so(ha, monkeypatch, caplog):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is True
    # HA goes away: the wall comes back up at once, the row says error
    _ha_answers(appmod, monkeypatch, {"zone.home": 500})
    with caplog.at_level("WARNING"):
        asyncio.run(appmod._presence_tick())
        asyncio.run(appmod._presence_tick())
    away = c.get("/api/hub").json()["dim"]["away"]
    assert away == {"enabled": True, "state": "unreadable", "home": None, "dim": False}
    assert _row(c, "away_dim")["status"] == "error"
    # one warning for the outage, not one per tick
    assert caplog.text.count("away dimming: Home Assistant") == 1
    # and it heals
    _ha_answers(appmod, monkeypatch, _zone(1))
    asyncio.run(appmod._presence_tick())
    assert c.get("/api/hub").json()["dim"]["away"]["state"] == "home"
    assert _row(c, "away_dim")["status"] is None


def test_a_stale_answer_stops_dimming(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is True
    monkeypatch.setattr(appmod, "_presence_at", appmod._presence_at - dimming.FRESH_S - 5)
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is False


def test_turning_it_off_forgets_the_last_answer(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)
    c.patch("/api/integrations/away_dim", json={"enabled": False})
    asyncio.run(appmod._presence_tick())
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    # an old "nobody home" must not come back when it is turned on again
    assert c.get("/api/hub").json()["dim"]["away"]["state"] == "checking"


def test_a_people_list_from_config_is_used(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, {"laundry": LAUNDRY, "presence": {
        "people": ["person.one", "person.two"]}})
    with TestClient(appmod.app) as c:
        c.patch("/api/integrations/away_dim", json={"enabled": True})
        asked = _ha_answers(appmod, monkeypatch, {
            "person.one": {"state": "not_home"}, "person.two": {"state": "Work"}})
        _ticks(appmod, 2)
        assert sorted(set(asked)) == ["person.one", "person.two"]
        assert c.get("/api/hub").json()["dim"]["away"]["dim"] is True


def test_a_presence_block_alone_makes_home_assistant_configured(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, {"presence": {"ha_base": "http://ha.example:8123"}})
    with TestClient(appmod.app) as c:
        assert _row(c, "away_dim") is not None


def test_no_token_stays_listed_and_says_needs_auth(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, {"laundry": LAUNDRY}, token=None)
    with TestClient(appmod.app) as c:
        c.patch("/api/integrations/away_dim", json={"enabled": True})
        assert _row(c, "away_dim")["status"] == "needs_auth"
        assert c.get("/api/hub").json()["dim"]["away"]["state"] == "needs_auth"
        asked = _ha_answers(appmod, monkeypatch, {})
        asyncio.run(appmod._presence_tick())
        assert asked == []                 # never sends an empty token


def test_a_refused_token_says_needs_auth(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, {"zone.home": 401})
    asyncio.run(appmod._presence_tick())
    assert _row(c, "away_dim")["status"] == "needs_auth"
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is False


def test_watch_gate_and_lifespan(ha, monkeypatch):
    appmod, _ = ha
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    assert appmod._presence_watch_enabled() is True
    monkeypatch.setenv("DISABLE_SYNC", "1")
    assert appmod._presence_watch_enabled() is False
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    monkeypatch.setattr(appmod, "DEMO", True)
    assert appmod._presence_watch_enabled() is False
    monkeypatch.setattr(appmod, "DEMO", False)
    monkeypatch.setattr(appmod.cfg, "laundry", None)
    assert appmod._presence_watch_enabled() is False      # no Home Assistant
    assert appmod.PRESENCE_WATCH_S == dimming.WATCH_S == 60.0
    ran = asyncio.Event()

    async def loop_stub():
        ran.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(appmod, "_presence_watch_enabled", lambda: True)
    monkeypatch.setattr(appmod, "presence_watch_loop", loop_stub)
    with TestClient(appmod.app):
        pass
    assert ran.is_set(), "lifespan never started the presence loop"


def test_the_loop_survives_a_crashing_tick(ha, monkeypatch):
    appmod, _ = ha
    calls = []

    async def boom():
        calls.append(1)
        if len(calls) >= 2:
            raise asyncio.CancelledError
        raise RuntimeError("bug")

    async def fast_sleep(_s):
        return None
    monkeypatch.setattr(appmod, "_presence_tick", boom)
    monkeypatch.setattr(appmod.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(appmod.presence_watch_loop())
    assert len(calls) == 2


def test_health_full_reports_presence_while_switched_on(ha, monkeypatch):
    appmod, c = ha
    assert c.get("/health/full").json()["sources"]["presence"]["enabled"] is False
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    monkeypatch.setattr(appmod, "_presence_watch_task", None)
    # switched on with no watcher running is a named problem
    src = c.get("/health/full").json()["sources"]["presence"]
    assert src["ok"] is False and "not running" in src["error"]

    class Running:
        def done(self):
            return False
    monkeypatch.setattr(appmod, "_presence_watch_task", Running())
    _ha_answers(appmod, monkeypatch, _zone(2))
    asyncio.run(appmod._presence_tick())
    src = c.get("/health/full").json()["sources"]["presence"]
    assert src["ok"] is True and src["status"] == "ok" and src["last_ok"]
    _ha_answers(appmod, monkeypatch, {"zone.home": 500})
    asyncio.run(appmod._presence_tick())
    src = c.get("/health/full").json()["sources"]["presence"]
    assert src["ok"] is False and src["status"] == "error" and src["error"]


# --- DEMO ----------------------------------------------------------------

def test_demo_lists_the_switch_with_a_canned_someone_home(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, token=None, demo=True)
    with TestClient(appmod.app) as c:
        row = _row(c, "away_dim")
        assert row is not None and row["enabled"] is False and row["status"] is None
        c.patch("/api/integrations/away_dim", json={"enabled": True})
        assert c.get("/api/hub").json()["dim"]["away"] == \
            {"enabled": True, "state": "home", "home": 2, "dim": False}


# --- review fixes: restarts, a stopped watcher, races, seeding, the kick ----

def test_one_nobody_home_read_is_not_enough_to_dim(ha, monkeypatch):
    """A single zero can be a blip; the wall dims on the second in a row, and
    a read of someone home in between starts the count again."""
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod)
    away = c.get("/api/hub").json()["dim"]["away"]
    assert away == {"enabled": True, "state": "confirming", "home": 0, "dim": False}
    _ha_answers(appmod, monkeypatch, _zone(1))
    _ticks(appmod)
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod)
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is False
    _ticks(appmod)
    assert c.get("/api/hub").json()["dim"]["away"]["dim"] is True
    # an unreadable read in between starts it over too
    _ha_answers(appmod, monkeypatch, {"zone.home": 500})
    _ticks(appmod)
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod)
    assert c.get("/api/hub").json()["dim"]["away"]["state"] == "confirming"


def test_a_home_assistant_restart_never_reads_as_nobody_home(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0, [{"entity_id": "person.a", "state": "unknown"}]))
    _ticks(appmod, 3)
    away = c.get("/api/hub").json()["dim"]["away"]
    assert away["state"] == "unreadable" and away["dim"] is False


def test_a_stopped_watcher_says_so_instead_of_blaming_home_assistant(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)

    class Dead:
        def done(self):
            return True
    monkeypatch.setattr(appmod, "_presence_watch_task", Dead())
    away = c.get("/api/hub").json()["dim"]["away"]
    assert away == {"enabled": True, "state": "stopped", "home": None, "dim": False}
    assert _row(c, "away_dim")["status"] == "error"


def test_forgetting_rearms_the_outage_warning(ha, monkeypatch, caplog):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, {"zone.home": 500})
    _ticks(appmod)
    assert appmod._presence_warned is True
    c.patch("/api/integrations/away_dim", json={"enabled": False})
    assert appmod._presence_warned is False
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    with caplog.at_level("WARNING"):
        _ticks(appmod)
    assert "could not be read" in caplog.text


def test_a_tick_started_before_a_flip_cannot_write_after_it(ha, monkeypatch):
    """The read is slow; the switch goes off and back on while it waits. Its
    answer belongs to the old switch-on and must be dropped."""
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})

    async def slow_read(*_a, **_k):
        c.patch("/api/integrations/away_dim", json={"enabled": False})
        c.patch("/api/integrations/away_dim", json={"enabled": True})
        return {"ok": True, "home": 0}
    monkeypatch.setattr(appmod.dimming, "read_presence", slow_read)
    _ticks(appmod)
    assert appmod._presence_snap is None
    assert c.get("/api/hub").json()["dim"]["away"]["state"] == "checking"


def test_presence_configured_without_any_home_assistant_is_logged(tmp_path, monkeypatch, caplog):
    appmod = _app(tmp_path, monkeypatch, {"presence": {"people": ["person.a"]}})
    with caplog.at_level("WARNING"):
        with TestClient(appmod.app):
            pass
    assert "presence" in caplog.text and "ha_base" in caplog.text


def test_the_away_choice_survives_a_reseed_and_a_fresh_hub_seeds_it_off(ha):
    appmod, c = ha
    from family_hub import db as fdb
    row = appmod._db().execute(
        "SELECT enabled FROM integrations WHERE id = 'away_dim'").fetchone()
    assert row is not None and row["enabled"] == 0, "a fresh database seeds it off"
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    appmod._db_initialized = False
    appmod._db()                                    # runs the seeding again
    assert fdb.integration_enabled(appmod._db(), "away_dim", default=False) is True


def test_the_kick_reruns_the_tick_at_once_and_clears_the_old_answer(ha, monkeypatch):
    appmod, c = ha
    c.patch("/api/integrations/away_dim", json={"enabled": True})
    _ha_answers(appmod, monkeypatch, _zone(0))
    _ticks(appmod, 2)
    monkeypatch.setattr(appmod, "_presence_kick", False)
    ticks, sleeps = [], []

    async def tick():
        ticks.append(appmod._presence_snap)
        if len(ticks) == 1:
            # the switch is flipped while the loop runs
            c.patch("/api/integrations/away_dim", json={"enabled": False})
        else:
            raise asyncio.CancelledError

    async def sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(appmod, "_presence_tick", tick)
    monkeypatch.setattr(appmod.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(appmod.presence_watch_loop())
    assert len(ticks) == 2 and sleeps == [], "the kick skips the minute's wait"
    assert ticks[0] is not None and ticks[1] is None, "and the old answer is gone"
