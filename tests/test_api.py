import datetime as dt
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from family_hub import db as fdb
from family_hub import tiles as ftiles


def _write_cfg(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138,
        "climate_base": "http://climate",
        "weather_base": "http://weather",
        "go2rtc_base": "http://cam",
        "calendar_window_days": 28,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#5BC9F0", "person": None}],
        "cameras": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"},
                    {"src": "wyze", "label": "Back Yard"}],
        "panels": [
            {"id": "weather", "label": "Almanac",
             "url": "http://weather/?theme=night",
             "vw": 1024, "vh": 600, "full": "fit"},
            {"id": "climate", "label": "Climate", "url": "http://climate/",
             "vw": 732, "vh": 502, "page_w": 1160,
             "crop_top": 68, "crop_left": 26},
        ],
    }))
    return str(p)


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    import family_hub.app as appmod
    importlib.reload(appmod)
    return appmod


@pytest.fixture
def client(app_mod):
    with TestClient(app_mod.app) as c:
        yield c


def _today():
    import family_hub.app as appmod
    return appmod._today()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def _reload_with(tmp_path, monkeypatch, extra):
    """Reload the app against a minimal config plus `extra` keys (e.g. a theme
    block). Mirrors the app_mod fixture but lets a test pick the config."""
    cfg = {"port": 8138, "calendars": [], "cameras": [], "panels": []}
    cfg.update(extra)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    import family_hub.app as appmod
    importlib.reload(appmod)
    return appmod


def test_hub_theme_house_default_present(tmp_path, monkeypatch):
    """When config sets a house theme, /api/hub carries it verbatim so the wall
    can stamp it on a fresh device."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"theme": {"mode": "light", "accent": "green", "columns": "wells"}})
    with TestClient(appmod.app) as c:
        theme = c.get("/api/hub").json()["theme"]
    assert theme == {"mode": "light", "accent": "green", "columns": "wells"}


def test_hub_theme_absent_is_null(tmp_path, monkeypatch):
    """No theme in config => /api/hub reports null. That's the documented
    fallback: the wall keeps theme.js's shipped dark/cyan/none default."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as c:
        body = c.get("/api/hub").json()
    assert "theme" in body and body["theme"] is None


def test_hub_theme_invalid_axes_dropped(tmp_path, monkeypatch):
    """Bad values are dropped per-axis rather than crashing: an invalid mode and
    the decided-out 'lines' column fall away, a valid accent survives."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"theme": {"mode": "neon", "accent": "green", "columns": "lines"}})
    with TestClient(appmod.app) as c:
        theme = c.get("/api/hub").json()["theme"]
    assert theme == {"accent": "green"}


@pytest.mark.parametrize("bad_theme", ["dark", []])
def test_hub_theme_non_dict_is_null(tmp_path, monkeypatch, bad_theme):
    """PT6: a config `theme` that isn't a dict (a bare string, a list) is dropped
    entirely — /api/hub reports null, never crashes on the non-mapping value."""
    appmod = _reload_with(tmp_path, monkeypatch, {"theme": bad_theme})
    with TestClient(appmod.app) as c:
        body = c.get("/api/hub").json()
    assert "theme" in body and body["theme"] is None


def test_hub_shape_end_to_end(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    epoch = today.isoformat()
    p1 = fdb.add_person(c, "Remy", "#5BC9F0")
    p2 = fdb.add_person(c, "Dad", "#8AE0AD")
    cid1 = fdb.add_chore(c, title="Dishes", icon="🍽️", schedule_kind="daily",
                         days_mask=0, assign_kind="fixed", fixed_person_id=p1,
                         rotation_order=[], rotation_epoch=epoch)
    fdb.add_chore(c, title="Trash", icon="", schedule_kind="daily", days_mask=0,
                  assign_kind="rotation", fixed_person_id=None,
                  rotation_order=[p1, p2], rotation_epoch=epoch)
    fdb.set_completion(c, cid1, epoch, p1)

    hub = client.get("/api/hub").json()
    assert hub["date"] == epoch
    people = {row["person"]["name"]: row for row in hub["people"]}
    assert people["Remy"]["done_count"] >= 1
    # Dishes should be marked done for Remy
    dishes = [ch for ch in people["Remy"]["chores"] if ch["title"] == "Dishes"][0]
    assert dishes["done"] is True
    assert "streak" in people["Remy"] and "week" in people["Remy"]
    assert len(people["Remy"]["week"]) == 7
    # rotation chore (Trash) carries the rot flag; fixed (Dishes) does not
    assert dishes["rot"] is False
    trash = [ch for row in hub["people"] for ch in row["chores"] if ch["title"] == "Trash"]
    assert trash and all(ch["rot"] is True for ch in trash)
    # calendar not configured yet
    assert hub["calendar"]["status"]["ok"] is False
    # panels come from config with every default resolved for the frontend
    panels = hub["links"]["panels"]
    assert [p["id"] for p in panels] == ["weather", "climate"]
    assert panels[0] == {"id": "weather", "label": "Almanac",
                         "url": "http://weather/?theme=night",
                         "vw": 1024, "vh": 600, "page_w": 1024,
                         "crop_top": 0, "crop_left": 0,
                         "full": "fit", "full_url": "http://weather/?theme=night"}
    assert panels[1]["page_w"] == 1160 and panels[1]["crop_top"] == 68
    assert panels[1]["full"] == "native"           # the default
    assert panels[1]["full_url"] == "http://climate/"
    cams = hub["links"]["cameras"]
    assert cams[0] == {"src": "cam", "label": "Driveway",
                       "tile": "http://cam/stream.html?src=cam&mode=webrtc",
                       "full": "http://cam/stream.html?src=cam_hd",
                       "has_hd": True, "hd_src": "cam_hd"}   # distinct 4K twin
    assert cams[1]["src"] == "wyze" and cams[1]["label"] == "Back Yard"
    assert cams[1]["full"] == "http://cam/stream.html?src=wyze"  # no hd stream
    # no distinct HD twin -> the wall won't run the full-screen upgrade
    assert cams[1]["has_hd"] is False and cams[1]["hd_src"] == "wyze"


def test_hub_calendar_events_joined(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    start = f"{today.isoformat()}T10:00:00-07:00"
    fdb.replace_events(c, [{"id": "e1", "calendar_id": "cal", "title": "Dentist",
                            "start_ts": start, "end_ts": start, "all_day": 0}])
    hub = client.get("/api/hub").json()
    ev = hub["calendar"]["events"][0]
    assert ev["title"] == "Dentist" and ev["color"] == "#5BC9F0" and ev["label"] == "Fam"


def test_complete_and_uncomplete_roundtrip(client, app_mod):
    c = app_mod._db()
    today = app_mod._today().isoformat()
    pid = fdb.add_person(c, "Remy", "#5BC9F0")
    cid = fdb.add_chore(c, title="Bed", icon="", schedule_kind="daily", days_mask=0,
                        assign_kind="fixed", fixed_person_id=pid, rotation_order=[],
                        rotation_epoch=today)
    assert client.post(f"/api/chores/{cid}/complete").json() == {"ok": True}
    assert fdb.completions_between(c, today, today)[0]["person_id"] == pid
    assert client.delete(f"/api/chores/{cid}/complete?date={today}").json() == {"ok": True}
    assert fdb.completions_between(c, today, today) == []


def test_complete_404_and_422(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    pid = fdb.add_person(c, "Remy", "#5BC9F0")
    cid = fdb.add_chore(c, title="Bed", icon="", schedule_kind="daily", days_mask=0,
                        assign_kind="fixed", fixed_person_id=pid, rotation_order=[],
                        rotation_epoch=today.isoformat())
    assert client.post("/api/chores/9999/complete").status_code == 404
    # date before epoch -> does not occur -> 422
    past = (today - dt.timedelta(days=5)).isoformat()
    r = client.post(f"/api/chores/{cid}/complete", json={"date": past})
    assert r.status_code == 422
    # empty-rotation chore inserted directly -> no assignee -> 422
    bad = fdb.add_chore(c, title="Nobody", icon="", schedule_kind="daily", days_mask=0,
                        assign_kind="rotation", fixed_person_id=None,
                        rotation_order=[], rotation_epoch=today.isoformat())
    assert client.post(f"/api/chores/{bad}/complete").status_code == 422


def test_complete_rejects_unknown_person_id(client, app_mod):
    """A client-supplied person_id is validated before writing, so a malformed
    request can't record an invisible orphan completion (audit finding)."""
    c = app_mod._db()
    today = app_mod._today().isoformat()
    pid = fdb.add_person(c, "Remy", "#5BC9F0")
    cid = fdb.add_chore(c, title="Bed", icon="", schedule_kind="daily", days_mask=0,
                        assign_kind="fixed", fixed_person_id=pid, rotation_order=[],
                        rotation_epoch=today)
    r = client.post(f"/api/chores/{cid}/complete", json={"person_id": 99999})
    assert r.status_code == 404
    assert fdb.completions_between(c, today, today) == []   # no orphan row written


def test_sync_tick_reconnects_on_failure(app_mod, monkeypatch):
    """The sync loop self-heals a dropped DB handle instead of freezing forever
    (the old bare `pass`). On a sync_once failure, _sync_tick returns a fresh,
    usable connection."""
    conn = app_mod.fdb.connect(app_mod.DB_PATH)
    app_mod.fdb.ensure_schema(conn)

    def boom(*a, **k):
        raise RuntimeError("db went away")

    monkeypatch.setattr(app_mod, "sync_once", boom)
    new_conn = app_mod._sync_tick(None, conn, app_mod.cfg)
    assert new_conn is not conn                              # reconnected
    assert new_conn.execute("SELECT 1").fetchone()[0] == 1   # and the new one is usable


def test_sync_tick_keeps_conn_on_success(app_mod, monkeypatch):
    conn = app_mod.fdb.connect(app_mod.DB_PATH)
    app_mod.fdb.ensure_schema(conn)
    monkeypatch.setattr(app_mod, "sync_once", lambda *a, **k: {"ok": True})
    assert app_mod._sync_tick(None, conn, app_mod.cfg) is conn   # same conn reused


def test_chores_day_past_future_and_validation(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    epoch = (today - dt.timedelta(days=10)).isoformat()
    pid = fdb.add_person(c, "Sam", "#C39BEA")
    cid = fdb.add_chore(c, title="Sweep", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch=epoch)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    fdb.set_completion(c, cid, yesterday, pid)

    day = client.get(f"/api/chores/day?date={yesterday}").json()
    sam = day["people"][0]
    assert day["date"] == yesterday
    assert sam["chores"][0]["done"] is True
    assert sam["streak"] == 1                      # as-of that day
    assert len(sam["week"]) == 7 and sam["week"][-1] == "done"

    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    day = client.get(f"/api/chores/day?date={tomorrow}").json()
    assert day["people"][0]["chores"][0]["done"] is False

    assert client.get("/api/chores/day?date=nope").status_code == 422
    far = (today + dt.timedelta(days=999)).isoformat()
    assert client.get(f"/api/chores/day?date={far}").status_code == 422


def test_admin_people_crud_and_validation(client, app_mod):
    r = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/api/admin/people", json={"name": "x", "color": "blue"}).status_code == 422
    assert client.post("/api/admin/people", json={"name": "", "color": "#5BC9F0"}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}", json={"name": "Remy2"}).status_code == 200
    assert client.patch("/api/admin/people/9999", json={"name": "z"}).status_code == 404
    state = client.get("/api/admin/state").json()
    assert state["people"][0]["name"] == "Remy2"


def test_admin_chores_crud_and_validation(client, app_mod):
    pr = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    pid = pr.json()["id"]
    ok = client.post("/api/admin/chores", json={
        "title": "Dishes", "icon": "🍽️", "schedule_kind": "daily", "days_mask": 0,
        "assign_kind": "fixed", "fixed_person_id": pid, "rotation_order": []})
    assert ok.status_code == 200 and ok.json()["title"] == "Dishes"
    cid = ok.json()["id"]
    # days kind with mask 0 -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "days", "days_mask": 0,
        "assign_kind": "fixed", "fixed_person_id": pid}).status_code == 422
    # rotation with empty order -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "daily", "assign_kind": "rotation",
        "rotation_order": []}).status_code == 422
    # fixed without person -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": None}).status_code == 422
    # patch to weekly with valid mask
    assert client.patch(f"/api/admin/chores/{cid}", json={
        "schedule_kind": "days", "days_mask": 0b0000101}).status_code == 200
    assert client.patch("/api/admin/chores/9999", json={"title": "z"}).status_code == 404
    state = client.get("/api/admin/state").json()
    assert any(ch["days_mask"] == 0b0000101 for ch in state["chores"])


def test_delete_chore_removes_it_and_its_completions(client, app_mod):
    c = app_mod._db()
    pr = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    pid = pr.json()["id"]
    ok = client.post("/api/admin/chores", json={
        "title": "Dishes", "icon": "🍽️", "schedule_kind": "daily", "days_mask": 0,
        "assign_kind": "fixed", "fixed_person_id": pid, "rotation_order": []})
    cid = ok.json()["id"]
    today = app_mod._today().isoformat()
    assert client.post(f"/api/chores/{cid}/complete").status_code == 200
    assert fdb.completions_between(c, today, today) != []   # completion recorded

    r = client.delete(f"/api/admin/chores/{cid}")
    assert r.status_code == 200 and r.json() == {"ok": True}

    state = client.get("/api/admin/state").json()
    assert all(ch["id"] != cid for ch in state["chores"])         # gone from admin list
    assert fdb.completions_between(c, today, today) == []         # no orphaned completion


def test_delete_unknown_chore_404(client):
    assert client.delete("/api/admin/chores/999999").status_code == 404


def test_calendar_endpoint(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    start = f"{today.isoformat()}T09:00:00-07:00"
    fdb.replace_events(c, [{"id": "e1", "calendar_id": "cal", "title": "Camp",
                            "start_ts": start, "end_ts": start, "all_day": 0}])
    out = client.get("/api/calendar?days=28").json()
    assert out["events"][0]["title"] == "Camp"


def test_calendar_event_colors_and_details(client, app_mod):
    c = app_mod._db()
    today = app_mod._today().isoformat()
    fdb.replace_events(c, [
        {"id": "e1", "calendar_id": "cal", "title": "Party",
         "start_ts": f"{today}T15:00:00-07:00", "end_ts": f"{today}T17:00:00-07:00",
         "all_day": 0, "location": "Grandma's", "description": "cake",
         "color_id": "6"},
        {"id": "e2", "calendar_id": "cal", "title": "Plain",
         "start_ts": f"{today}T18:00:00-07:00", "end_ts": f"{today}T19:00:00-07:00",
         "all_day": 0}])
    evs = {e["id"]: e for e in client.get("/api/calendar").json()["events"]}
    # explicit Google event color (Tangerine) overrides nothing server-side —
    # both the calendar color and the event color ship to the frontend
    assert evs["e1"]["event_color"] == "#F4511E"
    assert evs["e1"]["color"] == "#5BC9F0"          # calendar rail color
    assert (evs["e1"]["location"], evs["e1"]["description"]) == ("Grandma's", "cake")
    assert evs["e2"]["event_color"] is None


def test_google_calendar_color_beats_config(client, app_mod):
    c = app_mod._db()
    today = app_mod._today().isoformat()
    fdb.replace_events(c, [{"id": "e1", "calendar_id": "cal", "title": "X",
                            "start_ts": f"{today}T09:00:00-07:00",
                            "end_ts": f"{today}T10:00:00-07:00", "all_day": 0}])
    ev = client.get("/api/calendar").json()["events"][0]
    assert ev["color"] == "#5BC9F0"                    # config fallback pre-sync
    fdb.kv_set(c, "calendar_colors", {"cal": "#9FE1E7"})
    ev = client.get("/api/calendar").json()["events"][0]
    assert ev["color"] == "#9FE1E7"                    # the user's Google color


def test_calendar_past_window(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    past = (today - dt.timedelta(days=5)).isoformat()
    fdb.replace_events(c, [{"id": "p1", "calendar_id": "cal", "title": "Was",
                            "start_ts": f"{past}T09:00:00-07:00",
                            "end_ts": f"{past}T10:00:00-07:00", "all_day": 0}])
    with_past = client.get("/api/calendar?past=45").json()["events"]
    assert any(e["id"] == "p1" for e in with_past)
    no_past = client.get("/api/calendar?past=0").json()["events"]
    assert not any(e["id"] == "p1" for e in no_past)
    # the hub's home feed never includes the past
    hub_evs = client.get("/api/hub").json()["calendar"]["events"]
    assert not any(e["id"] == "p1" for e in hub_evs)


def test_tiles_routes_monkeypatched(client, monkeypatch):
    async def fake_climate(hclient, cfg):
        return {"available": True,
                "rooms": [{"name": "Living Room", "channel": 1, "temp_f": 71.0,
                           "humidity": 48, "stale": False}],
                "indoor_rh": 45, "indoor_dp": 51.2}

    async def fake_weather(hclient, cfg):
        return {"available": True, "temp": 79.0, "unit": "F", "uv": 6}

    monkeypatch.setattr("family_hub.tiles.climate_tile", fake_climate)
    monkeypatch.setattr("family_hub.tiles.weather_tile", fake_weather)
    cj = client.get("/api/tiles/climate").json()
    assert cj["rooms"][0]["name"] == "Living Room" and cj["indoor_rh"] == 45
    wj = client.get("/api/tiles/weather").json()
    assert wj["temp"] == 79.0 and wj["uv"] == 6


def test_tiles_routes_happy_end_to_end(client, monkeypatch):
    # PT4: the real success wiring — patch the HTTP client (NOT the tile fn) so
    # the route runs the tile against a live-shaped upstream and returns the
    # trimmed shape. Closes the gap left by the monkeypatched-tile happy path.
    ftiles.reset_caches()
    wx = {"temp": 72.0, "tempUnit": "F", "conditions": "Partly Cloudy",
          "feelsLike": 74.0, "feelsDesc": "Comfortable", "fcLow": 58.0,
          "fcHigh": 81.0, "uvIndex": 6, "uvDesc": "High", "aqi": 42,
          "aqiCategory": "Good", "humidity": 55, "dewPoint": 54.0,
          "weatherStale": False, "hourlyTemps": [70.0, 71.5, 73.0]}
    rooms = {"available": True, "rooms": [
        {"name": "Living Room", "channel": 1, "temp_f": 71.0, "humidity": 48,
         "stale": False, "battery_low": False}]}
    humidity = {"indoor_rh": 45, "indoor_dp": 51.2}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    async def fake_get(url, *a, **k):
        if url.endswith("/wx.json"):
            return FakeResp(wx)
        if url.endswith("/api/rooms"):
            return FakeResp(rooms)
        if url.endswith("/api/humidity"):
            return FakeResp(humidity)
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr("family_hub.app._http.get", fake_get)

    w = client.get("/api/tiles/weather")
    assert w.status_code == 200
    wj = w.json()
    assert wj["available"] is True
    assert wj["temp"] == 72.0 and wj["unit"] == "F"
    assert wj["conditions"] == "Partly Cloudy" and wj["feels"] == 74.0
    assert wj["high"] == 81.0 and wj["low"] == 58.0
    assert wj["uv"] == 6 and wj["aqi"] == 42 and wj["aqi_cat"] == "Good"
    assert wj["spark"] == [70.0, 71.5, 73.0]

    c = client.get("/api/tiles/climate")
    assert c.status_code == 200
    cj = c.json()
    assert cj["available"] is True
    assert cj["rooms"] == [{"name": "Living Room", "channel": 1,
                            "temp_f": 71.0, "humidity": 48, "stale": False}]
    assert cj["indoor_rh"] == 45 and cj["indoor_dp"] == 51.2


def test_weather_route_fail_soft_never_500(client, monkeypatch):
    # A dead feed must yield 200 {"available": False}, never a 500 — the wall
    # card just hides itself. Simulate the fetch raising inside the real tile.
    ftiles.reset_caches()   # a cached success from another test must not mask this

    async def boom_get(*a, **k):
        raise RuntimeError("feed down")
    monkeypatch.setattr("family_hub.app._http.get", boom_get)
    r = client.get("/api/tiles/weather")
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_weather_route_non_dict_body_never_500(client, monkeypatch):
    # wx.json served as valid-but-non-dict JSON (empty list, null) must still
    # yield HTTP 200 {"available": False}, not a 500 from an uncaught AttributeError.
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    for payload in ([], None):
        ftiles.reset_caches()

        async def fake_get(*a, _p=payload, **k):
            return FakeResp(_p)
        monkeypatch.setattr("family_hub.app._http.get", fake_get)
        r = client.get("/api/tiles/weather")
        assert r.status_code == 200
        assert r.json() == {"available": False}


def test_climate_route_fail_soft_never_500(client, monkeypatch):
    # A dead house-climate feed must yield 200 {"available": False}, never a 500 —
    # the wall card just hides itself. Simulate the /api/rooms fetch raising.
    ftiles.reset_caches()   # a cached success from another test must not mask this

    async def boom_get(*a, **k):
        raise RuntimeError("climate down")
    monkeypatch.setattr("family_hub.app._http.get", boom_get)
    r = client.get("/api/tiles/climate")
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_climate_route_non_dict_rooms_never_500(client, monkeypatch):
    # /api/rooms served as valid-but-non-dict JSON (empty list, null) must still
    # yield HTTP 200 {"available": False}, not a 500 from an uncaught AttributeError.
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    for payload in ([], None):
        ftiles.reset_caches()

        async def fake_get(*a, _p=payload, **k):
            return FakeResp(_p)
        monkeypatch.setattr("family_hub.app._http.get", fake_get)
        r = client.get("/api/tiles/climate")
        assert r.status_code == 200
        assert r.json() == {"available": False}


def test_camera_route_happy_and_502(client, monkeypatch):
    seen = []

    async def ok(hclient, cfg, src="cam"):
        seen.append(src)
        return (b"\xff\xd8jpeg", "image/jpeg")
    monkeypatch.setattr("family_hub.tiles.camera_snapshot", ok)
    r = client.get("/api/tiles/camera.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/api/tiles/camera.jpg?src=wyze").status_code == 200
    # the "hd" twin is probeable too — the full-screen upgrade checks its readiness
    assert client.get("/api/tiles/camera.jpg?src=cam_hd").status_code == 200
    assert seen == ["cam", "wyze", "cam_hd"]
    # unconfigured stream names are refused, not proxied
    assert client.get("/api/tiles/camera.jpg?src=evil").status_code == 404

    async def none(hclient, cfg, src="cam"):
        return None
    monkeypatch.setattr("family_hub.tiles.camera_snapshot", none)
    assert client.get("/api/tiles/camera.jpg").status_code == 502


def test_html_is_never_heuristically_cached(client):
    """Phones cached a stale index.html past a deploy (2026-08-13, missing tab
    bar): the HTML must say no-cache so browsers revalidate; busted assets
    (?v=N) and API JSON are left alone."""
    for path in ("/", "/admin.html"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache", path
    assert "cache-control" not in {k.lower() for k in client.get("/styles.css").headers}


def test_hub_survives_a_malformed_config_entry(tmp_path, monkeypatch):
    """One bad camera/panel entry must NOT 500 the whole /api/hub payload
    (chores + calendar + every other tile). The bad entry is skipped; the good
    ones still render. Exercised through the real endpoint."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138, "climate_base": "http://c", "weather_base": "http://w",
        "go2rtc_base": "http://cam", "calendar_window_days": 28, "calendars": [],
        "cameras": [{"src": "good", "label": "Good"},
                    {"label": "NoSrc"}],                    # missing "src"
        "panels": [
            {"id": "ok", "label": "OK", "url": "http://x", "vw": 800, "vh": 600},
            {"id": "bad", "label": "Bad", "url": "http://y",
             "vw": "not-a-number", "vh": 600},              # non-integer vw
        ],
    }))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    import family_hub.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        r = c.get("/api/hub")
        assert r.status_code == 200, "one malformed entry must not blank the whole hub"
        links = r.json()["links"]
        assert [cam["src"] for cam in links["cameras"]] == ["good"]   # bad camera skipped
        assert [pan["id"] for pan in links["panels"]] == ["ok"]       # bad panel skipped


def test_hub_survives_a_broken_todos_block(client, app_mod, monkeypatch):
    """A single exception in the todos read/group path must not 500 the whole
    hub payload (people + calendar still render). GET /api/todos keeps NO
    such wrapper on purpose: a 500 there is visible and correct."""
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(app_mod.tdlogic, "group", _boom)
    r = client.get("/api/hub")
    assert r.status_code == 200
    hub = r.json()
    assert hub["todos"] == {"now": [], "soon": [], "later": []}
    assert "people" in hub and "calendar" in hub


# --- todos ----------------------------------------------------------------

def test_todos_add_list_and_hub_block(client):
    r = client.post("/api/todos", json={"title": "  Fix gate latch  "})
    assert r.status_code == 200
    row = r.json()
    assert row["title"] == "Fix gate latch"      # stripped
    assert row["bucket"] == "now"                # default bucket
    client.post("/api/todos", json={"title": "Plan trip", "bucket": "later"})

    data = client.get("/api/todos").json()
    assert [t["title"] for t in data["buckets"]["now"]] == ["Fix gate latch"]
    assert [t["title"] for t in data["buckets"]["later"]] == ["Plan trip"]
    assert data["buckets"]["soon"] == []
    assert data["recent_done"] == []

    hub = client.get("/api/hub").json()
    assert [t["title"] for t in hub["todos"]["now"]] == ["Fix gate latch"]
    assert "recent_done" not in hub["todos"]


def test_todos_validation_and_404(client):
    assert client.post("/api/todos", json={"title": "   "}).status_code == 422
    assert client.post("/api/todos", json={"title": "x" * 120}).status_code == 200
    assert client.post("/api/todos", json={"title": "x" * 121}).status_code == 422
    assert client.post("/api/todos",
                       json={"title": "ok", "bucket": "someday"}).status_code == 422
    assert client.patch("/api/todos/999", json={"bucket": "soon"}).status_code == 404
    assert client.post("/api/todos/999/complete").status_code == 404
    assert client.delete("/api/todos/999/complete").status_code == 404
    assert client.delete("/api/todos/999").status_code == 404


def test_todos_patch_moves_bucket_and_renames(client):
    tid = client.post("/api/todos", json={"title": "Sharpen mower blade"}).json()["id"]
    r = client.patch(f"/api/todos/{tid}", json={"bucket": "soon"})
    assert r.json()["bucket"] == "soon"
    r = client.patch(f"/api/todos/{tid}", json={"title": " Sharpen blades "})
    assert r.json()["title"] == "Sharpen blades"
    assert client.patch(f"/api/todos/{tid}",
                        json={"bucket": "whenever"}).status_code == 422


def test_todos_complete_lingers_today_then_hides(client, app_mod, monkeypatch):
    tid = client.post("/api/todos", json={"title": "Water plants"}).json()["id"]
    assert client.post(f"/api/todos/{tid}/complete").json() == {"ok": True}

    # done today: still visible in buckets, struck via done_at, in recent_done
    data = client.get("/api/todos").json()
    assert [t["id"] for t in data["buckets"]["now"]] == [tid]
    assert data["buckets"]["now"][0]["done_at"] is not None
    assert [t["id"] for t in data["recent_done"]] == [tid]

    # the next local day: gone from buckets (and hub), still restorable
    real_today = app_mod._today()
    monkeypatch.setattr(app_mod, "_today",
                        lambda: real_today + dt.timedelta(days=1))
    data = client.get("/api/todos").json()
    assert data["buckets"]["now"] == []
    assert [t["id"] for t in data["recent_done"]] == [tid]
    assert client.get("/api/hub").json()["todos"]["now"] == []

    # restore reopens it
    assert client.delete(f"/api/todos/{tid}/complete").json() == {"ok": True}
    data = client.get("/api/todos").json()
    assert [t["id"] for t in data["buckets"]["now"]] == [tid]
    assert data["buckets"]["now"][0]["done_at"] is None
    assert data["recent_done"] == []


def test_todos_uncomplete_open_item_is_noop_and_delete_removes(client):
    tid = client.post("/api/todos", json={"title": "Call plumber"}).json()["id"]
    assert client.delete(f"/api/todos/{tid}/complete").json() == {"ok": True}
    assert client.delete(f"/api/todos/{tid}").json() == {"ok": True}
    data = client.get("/api/todos").json()
    assert data["buckets"]["now"] == [] and data["recent_done"] == []
