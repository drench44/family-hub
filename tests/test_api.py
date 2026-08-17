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


def test_hub_carries_a_stable_build_token(client):
    """/api/hub exposes a `build` token — a 12-char hex hash of the baked frontend
    assets — that the wall diffs across polls to auto-reload after a deploy. It must
    be a non-empty, well-formed hex string, and stable within a running process."""
    import re
    body = client.get("/api/hub").json()
    assert "build" in body, "the /api/hub payload must carry a build token"
    build = body["build"]
    assert isinstance(build, str) and re.fullmatch(r"[0-9a-f]{12}", build), \
        f"build must be a 12-char hex token, got {build!r}"
    assert client.get("/api/hub").json()["build"] == build, \
        "the build token is stable while the process (and its baked assets) is unchanged"


def test_compute_build_survives_broken_bake_and_shouts(app_mod, tmp_path, monkeypatch, caplog):
    """BUILD is computed at import time, so _compute_build must never raise on an
    unreadable/missing static dir. A totally broken bake logs an ERROR loudly (its
    whole reason to exist per the silent-failure gate) and still returns a
    well-formed token distinct from the real one, rather than crashing the app."""
    import logging
    import re
    real = app_mod._compute_build()
    assert re.fullmatch(r"[0-9a-f]{12}", real)
    monkeypatch.setattr(app_mod, "STATIC_DIR", str(tmp_path / "no-such-static"))
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        broken = app_mod._compute_build()   # nothing readable -> ERROR, no raise
    assert isinstance(broken, str) and re.fullmatch(r"[0-9a-f]{12}", broken)
    assert broken != real, "a broken bake must not collide with the real build token"
    assert any("bake is broken" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR), \
        "a totally broken bake must be logged LOUDLY (error), not swallowed"


def test_compute_build_warns_on_one_bad_asset_but_hashes_the_rest(
        app_mod, tmp_path, monkeypatch, caplog):
    """A single unreadable asset among readable ones WARNS (naming it) and still
    produces a token from the rest — it must not silently drop the file, and one
    bad asset is a warning, not a total-failure error."""
    import logging
    import re
    static = tmp_path / "static"
    static.mkdir()
    (static / "ok.css").write_text("body{}")
    # A directory that matches the *.js glob: open() on it raises IsADirectoryError
    # (an OSError), exercising the per-file warning branch deterministically.
    (static / "broken.js").mkdir()
    monkeypatch.setattr(app_mod, "STATIC_DIR", str(static))
    with caplog.at_level(logging.WARNING, logger="family_hub"):
        token = app_mod._compute_build()
    assert re.fullmatch(r"[0-9a-f]{12}", token)
    assert any("broken.js" in r.getMessage() and "unreadable" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING), \
        "the unreadable asset must be named in a WARNING, not silently skipped"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records), \
        "one bad asset among good ones is a warning, not a total-failure error"


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
    fallback: the wall keeps theme.js's shipped grey/green/none default."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as c:
        body = c.get("/api/hub").json()
    assert "theme" in body and body["theme"] is None


def test_hub_theme_invalid_axes_dropped(tmp_path, monkeypatch):
    """Bad values are dropped per-axis rather than crashing: an invalid mode and
    an unknown column value fall away, a valid accent survives."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"theme": {"mode": "neon", "accent": "green", "columns": "stripes"}})
    with TestClient(appmod.app) as c:
        theme = c.get("/api/hub").json()["theme"]
    assert theme == {"accent": "green"}


def test_hub_theme_lines_column_survives(tmp_path, monkeypatch):
    """The 'lines' column option ships across the whole frontend (theme.js, both
    HTML pages, CSS), so a house-default columns:lines must round-trip through
    config validation. Regression: _THEME_AXES omitted 'lines', so _clean_theme
    silently dropped it and fresh devices fell back to 'none'."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"theme": {"accent": "cyan", "columns": "lines"}})
    with TestClient(appmod.app) as c:
        theme = c.get("/api/hub").json()["theme"]
    assert theme == {"accent": "cyan", "columns": "lines"}


def test_hub_theme_new_modes_survive(tmp_path, monkeypatch):
    """All five wall modes (light/soft/dark/grey/black) round-trip through config
    validation. Regression: _THEME_AXES['mode'] listed only light/dark, so a
    house default of grey/soft/black was silently dropped and fresh devices fell
    back to the hardcoded default instead of the configured mode."""
    for mode in ("soft", "grey", "black"):
        appmod = _reload_with(tmp_path, monkeypatch,
                              {"theme": {"mode": mode, "accent": "green"}})
        with TestClient(appmod.app) as c:
            theme = c.get("/api/hub").json()["theme"]
        assert theme == {"mode": mode, "accent": "green"}, f"{mode} was dropped"


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
    # camera_page (the Cameras-tab 2x2 grid) is unset in this config, so it
    # falls back to the wall cameras with the identical link shape.
    assert hub["links"]["camera_page"] == cams


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
    # past date that was never served/logged -> did not occur -> 422
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
    # past days render from the frozen occurrence log, so history is seeded
    # there (the wall writes it live each served day)
    fdb.replace_day_log(c, yesterday, [
        {"chore_id": cid, "person_id": pid, "title": "Sweep", "icon": "",
         "rot": 0}])
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


def test_hub_keeps_in_progress_multiday_event(client, app_mod):
    """A multi-day event that started before today but is still running must stay
    on the wall's home feed; one that already ended must not. Regression: the
    feed filtered on start date alone and dropped in-progress spans."""
    c = app_mod._db()
    today = _today()
    fdb.replace_events(c, [
        # all-day span: started 4 days ago, end_ts (exclusive) is tomorrow, so
        # its last visible day is today — in progress right now.
        {"id": "trip", "calendar_id": "cal", "title": "Vacation", "all_day": 1,
         "start_ts": (today - dt.timedelta(days=4)).isoformat(),
         "end_ts": (today + dt.timedelta(days=1)).isoformat()},
        # all-day span that ended yesterday (exclusive end = today), last visible
        # day was the day before yesterday — must be gone.
        {"id": "past", "calendar_id": "cal", "title": "Old Trip", "all_day": 1,
         "start_ts": (today - dt.timedelta(days=3)).isoformat(),
         "end_ts": today.isoformat()},
    ])
    ids = {e["id"] for e in client.get("/api/hub").json()["calendar"]["events"]}
    assert "trip" in ids, "an in-progress multi-day event must stay on the wall"
    assert "past" not in ids, "an event that ended before today must not appear"


def test_hub_todos_failure_flags_not_ok(client, app_mod, monkeypatch):
    """When the todos read/group throws, /api/hub still serves (empty buckets)
    but flags todos_ok=false so the wall shows 'couldn't load' rather than an
    empty card the family would read as 'all caught up'."""
    def boom(*a, **k):
        raise RuntimeError("todos read failed")
    monkeypatch.setattr(app_mod.fdb, "list_todos", boom)
    body = client.get("/api/hub").json()
    assert body["todos_ok"] is False
    assert body["todos"] == {b: [] for b in app_mod.tdlogic.BUCKETS}


def test_hub_todos_ok_by_default(client, app_mod):
    assert client.get("/api/hub").json()["todos_ok"] is True


def test_admin_people_crud_and_validation(client, app_mod):
    r = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/api/admin/people", json={"name": "x", "color": "blue"}).status_code == 422
    assert client.post("/api/admin/people", json={"name": "", "color": "#5BC9F0"}).status_code == 422
    # A trailing newline must NOT slip through the hex check ($ vs \Z): a
    # malformed color would otherwise reach the client as a CSS value.
    assert client.post("/api/admin/people",
                       json={"name": "x", "color": "#5BC9F0\n"}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}", json={"name": "Remy2"}).status_code == 200
    assert client.patch("/api/admin/people/9999", json={"name": "z"}).status_code == 404
    state = client.get("/api/admin/state").json()
    assert state["people"][0]["name"] == "Remy2"


def test_admin_patch_rejects_explicit_null_on_nonnullable_fields(client, app_mod):
    """Regression for issue #35: an explicit JSON null for a field backed by a
    NOT NULL column is a 422, not a 500 from the DB write. fixed_person_id (the
    one nullable chore field) still accepts null."""
    pid = client.post("/api/admin/people",
                      json={"name": "Remy", "color": "#5BC9F0"}).json()["id"]
    assert client.patch(f"/api/admin/people/{pid}", json={"sort": None}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}", json={"active": None}).status_code == 422
    cid = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    assert client.patch(f"/api/admin/chores/{cid}", json={"icon": None}).status_code == 422
    assert client.patch(f"/api/admin/chores/{cid}", json={"active": None}).status_code == 422
    # fixed_person_id: null is legitimate (clearing the fixed assignee) -> 200
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"assign_kind": "rotation", "rotation_order": [pid],
                              "fixed_person_id": None}).status_code == 200


def test_admin_delete_person_hard_removes_and_404s(client, app_mod):
    """The hard-delete endpoint removes the person entirely (distinct from the
    PATCH active=0 deactivate path); an unknown id is a 404."""
    pid = client.post("/api/admin/people",
                      json={"name": "Remy", "color": "#5BC9F0"}).json()["id"]
    # a chore fixed to them, to prove the delete clears assignments too
    cid = client.post("/api/admin/chores", json={
        "title": "Trash", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]

    assert client.delete(f"/api/admin/people/{pid}").status_code == 200
    state = client.get("/api/admin/state").json()
    assert all(p["id"] != pid for p in state["people"])           # gone entirely
    assert next(c for c in state["chores"]
                if c["id"] == cid)["fixed_person_id"] is None     # assignment cleared
    assert client.delete(f"/api/admin/people/{pid}").status_code == 404  # already gone


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


def test_delete_chore_keeps_past_days_and_clears_today(client, app_mod):
    """Frozen history: deleting a chore removes it from today onward but past
    days keep showing it, done flags intact."""
    c = app_mod._db()
    today = app_mod._today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    pr = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    pid = pr.json()["id"]
    ok = client.post("/api/admin/chores", json={
        "title": "Dishes", "icon": "🍽️", "schedule_kind": "daily", "days_mask": 0,
        "assign_kind": "fixed", "fixed_person_id": pid, "rotation_order": []})
    cid = ok.json()["id"]
    fdb.replace_day_log(c, yesterday, [
        {"chore_id": cid, "person_id": pid, "title": "Dishes", "icon": "🍽️",
         "rot": 0}])
    fdb.set_completion(c, cid, yesterday, pid)

    r = client.delete(f"/api/admin/chores/{cid}")
    assert r.status_code == 200 and r.json() == {"ok": True}

    state = client.get("/api/admin/state").json()
    assert all(ch["id"] != cid for ch in state["chores"])   # gone from admin list
    # today's plan no longer carries it
    hub = client.get("/api/hub").json()
    assert all(row["chores"] == [] for row in hub["people"])
    # ...but yesterday still shows it, done
    day = client.get(f"/api/chores/day?date={yesterday}").json()
    remy = day["people"][0]
    assert [(x["title"], x["done"]) for x in remy["chores"]] == [("Dishes", True)]
    assert fdb.completions_between(c, yesterday, yesterday) != []


def test_admin_once_chore_add_patch_and_validation(client, app_mod):
    """A one-time chore: its `date` stores as rotation_epoch, it shows only on
    that day, and its validation rejects a missing/bad date and any rotation."""
    today = app_mod._today()
    due = (today + dt.timedelta(days=2)).isoformat()
    pr = client.post("/api/admin/people", json={"name": "Remy", "color": "#5BC9F0"})
    pid = pr.json()["id"]

    ok = client.post("/api/admin/chores", json={
        "title": "Return books", "icon": "📚", "schedule_kind": "once",
        "assign_kind": "fixed", "fixed_person_id": pid, "date": due})
    assert ok.status_code == 200
    cid = ok.json()["id"]
    assert ok.json()["schedule_kind"] == "once"
    assert ok.json()["rotation_epoch"] == due     # the date is stored as the epoch

    # a one-time chore with no date -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "once", "assign_kind": "fixed",
        "fixed_person_id": pid}).status_code == 422
    # a malformed date -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "once", "assign_kind": "fixed",
        "fixed_person_id": pid, "date": "not-a-date"}).status_code == 422
    # a one-time chore can't be a rotation -> 422
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "once", "assign_kind": "rotation",
        "rotation_order": [pid], "date": due}).status_code == 422

    # it occurs only on its due date
    day = client.get(f"/api/chores/day?date={due}").json()
    assert [x["title"] for x in day["people"][0]["chores"]] == ["Return books"]
    before = (today + dt.timedelta(days=1)).isoformat()
    day = client.get(f"/api/chores/day?date={before}").json()
    assert day["people"][0]["chores"] == []

    # patching the date moves it (still stored as rotation_epoch)
    moved = (today + dt.timedelta(days=5)).isoformat()
    r = client.patch(f"/api/admin/chores/{cid}", json={"date": moved})
    assert r.status_code == 200 and r.json()["rotation_epoch"] == moved
    day = client.get(f"/api/chores/day?date={moved}").json()
    assert [x["title"] for x in day["people"][0]["chores"]] == ["Return books"]
    day = client.get(f"/api/chores/day?date={due}").json()
    assert day["people"][0]["chores"] == []


def test_once_chore_patch_edge_cases(client, app_mod):
    """The date-translation path's failure modes (found in review): an empty
    date must not corrupt rotation_epoch, kind conversions must re-anchor
    correctly, past dates are rejected, and an already-past chore stays
    editable."""
    c = app_mod._db()
    today = app_mod._today()
    due = (today + dt.timedelta(days=3)).isoformat()
    pid = client.post("/api/admin/people",
                      json={"name": "Remy", "color": "#5BC9F0"}).json()["id"]
    cid = client.post("/api/admin/chores", json={
        "title": "Books", "icon": "", "schedule_kind": "once",
        "assign_kind": "fixed", "fixed_person_id": pid, "date": due}).json()["id"]

    # clearing the date is rejected — must NOT write '' into rotation_epoch and
    # 500 the wall (the critical bug both reviewers reproduced)
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"date": ""}).status_code == 422
    assert client.get(f"/api/chores/day?date={due}").status_code == 200  # not corrupted
    # a past date is rejected on patch
    yest = (today - dt.timedelta(days=1)).isoformat()
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"date": yest}).status_code == 422
    # a title-only edit doesn't touch the (still valid) date
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"title": "Library books"}).status_code == 200
    assert app_mod._chore_row(c, cid)["rotation_epoch"] == due

    # converting the one-time chore to daily re-anchors it to today, so it shows
    # today rather than staying pinned to its (future) one-time date
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"schedule_kind": "daily"}).status_code == 200
    assert app_mod._chore_row(c, cid)["rotation_epoch"] == today.isoformat()
    hub = client.get("/api/hub").json()
    assert any(ch["title"] == "Library books"
               for p in hub["people"] for ch in p["chores"])

    # converting a daily chore to once with NO date is rejected (its epoch is a
    # creation anchor, not a due date) — would otherwise land in the past
    d2 = client.post("/api/admin/chores", json={
        "title": "Sweep", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    assert client.patch(f"/api/admin/chores/{d2}",
                        json={"schedule_kind": "once"}).status_code == 422
    # ...but with a valid future date it converts cleanly
    r = client.patch(f"/api/admin/chores/{d2}",
                     json={"schedule_kind": "once", "date": due})
    assert r.status_code == 200 and r.json()["rotation_epoch"] == due


def test_once_chore_rejects_past_date_on_add(client, app_mod):
    today = app_mod._today()
    past = (today - dt.timedelta(days=1)).isoformat()
    pid = client.post("/api/admin/people",
                      json={"name": "Remy", "color": "#5BC9F0"}).json()["id"]
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "once", "assign_kind": "fixed",
        "fixed_person_id": pid, "date": past}).status_code == 422
    # today is allowed (boundary)
    assert client.post("/api/admin/chores", json={
        "title": "X", "schedule_kind": "once", "assign_kind": "fixed",
        "fixed_person_id": pid, "date": today.isoformat()}).status_code == 200


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
          "weatherStale": False,
          "tempSeries": {"temps": [70.0, 71.5, 73.0], "nowIndex": 1}}
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
    assert wj["spark"] == [70.0, 71.5, 73.0] and wj["spark_now"] == 1

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


def test_camera_snapshot_allows_camera_page_only_streams(tmp_path, monkeypatch):
    """A camera that lives only in camera_page (the grid), not the wall `cameras`
    column, must still be probe-able — otherwise its grid tile is stuck showing
    'offline' even though the stream is live (regression: grid-only Mailbox)."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138, "go2rtc_base": "http://cam", "calendars": [],
        "cameras": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"}],
        "camera_page": [
            {"src": "cam", "label": "Driveway", "hd": "cam_hd"},
            {"src": "cam2", "label": "Mailbox", "hd": "cam2_hd"},
        ],
        "panels": [],
    }))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    import family_hub.app as appmod
    importlib.reload(appmod)
    seen = []

    async def ok(hclient, cfg, src="cam"):
        seen.append(src)
        return (b"\xff\xd8jpeg", "image/jpeg")
    monkeypatch.setattr("family_hub.tiles.camera_snapshot", ok)
    with TestClient(appmod.app) as c:
        # grid-only camera + its hd twin are allowlisted (both were 404 before the fix)
        assert c.get("/api/tiles/camera.jpg?src=cam2").status_code == 200
        assert c.get("/api/tiles/camera.jpg?src=cam2_hd").status_code == 200
        # a truly unknown src is still refused
        assert c.get("/api/tiles/camera.jpg?src=evil").status_code == 404
    assert "cam2" in seen and "cam2_hd" in seen


def test_html_is_never_heuristically_cached(client):
    """Phones cached a stale index.html past a deploy (2026-08-13, missing tab
    bar): the HTML must say no-cache so browsers revalidate; busted assets
    (?v=N) and API JSON are left alone."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert "cache-control" not in {k.lower() for k in client.get("/styles.css").headers}


def test_retired_admin_page_route_is_gone(client):
    """admin.html/admin.js were retired 2026-08-15 (all management moved onto the
    wall's Chores page). The static file is deleted, so the route 404s — a real
    "gone", not the wall silently served in its place. The HTTP half of the
    retirement guard; the filesystem/reference half is
    test_static.py::test_admin_html_is_retired. The /api/admin/* routes stay —
    they back the inline editor — so spot-check one still answers."""
    assert client.get("/admin.html").status_code == 404
    assert client.get("/admin.js").status_code == 404
    assert client.get("/api/admin/state").status_code == 200


def test_camera_page_grid_is_independent_of_wall_cameras(tmp_path, monkeypatch):
    """The Cameras-tab 2x2 grid (`camera_page`) can list a different set/order
    than the wall's `cameras` column — e.g. a camera that isn't on the wall —
    and each entry gets the same tile/full link shape as a wall camera."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138, "go2rtc_base": "http://cam", "calendars": [],
        "cameras": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"}],
        "camera_page": [
            {"src": "cam", "label": "Driveway", "hd": "cam_hd"},
            {"src": "cam2", "label": "Mailbox", "hd": "cam2_hd"},
            {"src": "wyze_l", "label": "Side Gate"},
            {"src": "wyze_p", "label": "Garage"},
        ],
        "panels": [],
    }))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    import family_hub.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        links = c.get("/api/hub").json()["links"]
    # the wall column stays the single Driveway; the grid is the full four, in order
    assert [x["src"] for x in links["cameras"]] == ["cam"]
    assert [x["src"] for x in links["camera_page"]] == ["cam", "cam2", "wyze_l", "wyze_p"]
    # a grid-only camera (Mailbox) carries a distinct HD twin for full-screen
    assert links["camera_page"][1] == {
        "src": "cam2", "label": "Mailbox",
        "tile": "http://cam/stream.html?src=cam2&mode=webrtc",
        "full": "http://cam/stream.html?src=cam2_hd",
        "has_hd": True, "hd_src": "cam2_hd"}
    # a grid cam with no hd twin falls back to its own src for full-screen
    assert links["camera_page"][2]["has_hd"] is False
    assert links["camera_page"][2]["hd_src"] == "wyze_l"


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


# --- frozen chore history (occurrence log) ---------------------------------

def _seed_person_chore(client, title="Dishes", icon="", **chore_kw):
    pid = client.post("/api/admin/people",
                      json={"name": "Remy", "color": "#5BC9F0"}).json()["id"]
    body = {"title": title, "icon": icon, "schedule_kind": "daily",
            "days_mask": 0, "assign_kind": "fixed", "fixed_person_id": pid,
            "rotation_order": []}
    body.update(chore_kw)
    if body["assign_kind"] == "rotation" and body["rotation_order"] == []:
        body["rotation_order"] = [pid]
    cid = client.post("/api/admin/chores", json=body).json()["id"]
    return pid, cid


def _log(c, date, cid, pid, title="Dishes", icon="", rot=0):
    fdb.replace_day_log(c, date, [
        {"chore_id": cid, "person_id": pid, "title": title, "icon": icon,
         "rot": rot}])


def test_hub_writes_todays_occurrence_log(client, app_mod):
    c = app_mod._db()
    today = app_mod._today().isoformat()
    pid, cid = _seed_person_chore(client)
    assert fdb.day_log(c, today) == []          # nothing served yet
    client.get("/api/hub")
    rows = fdb.day_log(c, today)
    assert [(r["chore_id"], r["person_id"], r["title"]) for r in rows] == \
        [(cid, pid, "Dishes")]
    # future days are never frozen
    tomorrow = (app_mod._today() + dt.timedelta(days=1)).isoformat()
    client.get(f"/api/chores/day?date={tomorrow}")
    assert fdb.day_log(c, tomorrow) == []


def test_schedule_edit_freezes_history(client, app_mod):
    """The audit's headline bug: adding a weekday used to zero streaks by
    retroactively marking every past new-weekday as missed. Past days now come
    from the log, so the edit changes nothing before today."""
    c = app_mod._db()
    today = app_mod._today()
    pid, cid = _seed_person_chore(client)
    for i in (3, 2, 1):
        d = (today - dt.timedelta(days=i)).isoformat()
        _log(c, d, cid, pid)
        fdb.set_completion(c, cid, d, pid)
    before = client.get("/api/hub").json()["people"][0]["streak"]
    assert before == 3
    # shrink the schedule to one weekday — an edit that used to rewrite history
    assert client.patch(f"/api/admin/chores/{cid}", json={
        "schedule_kind": "days", "days_mask": 0b0000010}).status_code == 200
    after = client.get("/api/hub").json()["people"][0]
    assert after["streak"] == 3
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    day = client.get(f"/api/chores/day?date={yesterday}").json()
    assert day["people"][0]["chores"][0]["done"] is True


def test_rotation_edit_does_not_reshuffle_past_days(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    p1 = client.post("/api/admin/people",
                     json={"name": "A", "color": "#111111"}).json()["id"]
    p2 = client.post("/api/admin/people",
                     json={"name": "B", "color": "#222222"}).json()["id"]
    cid = client.post("/api/admin/chores", json={
        "title": "Cat", "schedule_kind": "daily", "assign_kind": "rotation",
        "rotation_order": [p1, p2]}).json()["id"]
    _log(c, yesterday, cid, p1, title="Cat", rot=1)
    fdb.set_completion(c, cid, yesterday, p1)
    # reorder + extend the rotation — used to reassign past days
    p3 = client.post("/api/admin/people",
                     json={"name": "C", "color": "#333333"}).json()["id"]
    client.patch(f"/api/admin/chores/{cid}",
                 json={"rotation_order": [p3, p2, p1]})
    day = client.get(f"/api/chores/day?date={yesterday}").json()
    by_name = {row["person"]["name"]: row for row in day["people"]}
    assert [x["done"] for x in by_name["A"]["chores"]] == [True]
    assert by_name["B"]["chores"] == [] and by_name["C"]["chores"] == []


def test_deactivate_chore_keeps_past_days(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    pid, cid = _seed_person_chore(client)
    _log(c, yesterday, cid, pid)
    fdb.set_completion(c, cid, yesterday, pid)
    client.patch(f"/api/admin/chores/{cid}", json={"active": 0})
    hub = client.get("/api/hub").json()
    assert all(row["chores"] == [] for row in hub["people"])   # gone today
    day = client.get(f"/api/chores/day?date={yesterday}").json()
    assert day["people"][0]["chores"][0]["done"] is True       # kept yesterday


def test_rotation_skips_deactivated_person_from_today(client, app_mod):
    """A deactivated person's rotation turns fall to the remaining members
    instead of producing an unassignable ghost day (audit finding)."""
    p1 = client.post("/api/admin/people",
                     json={"name": "A", "color": "#111111"}).json()["id"]
    p2 = client.post("/api/admin/people",
                     json={"name": "B", "color": "#222222"}).json()["id"]
    client.post("/api/admin/chores", json={
        "title": "Cat", "schedule_kind": "daily", "assign_kind": "rotation",
        "rotation_order": [p1, p2]})
    # today is occurrence 0 -> p1's turn; deactivate p1 -> falls to p2
    client.patch(f"/api/admin/people/{p1}", json={"active": 0})
    hub = client.get("/api/hub").json()
    assert [row["person"]["name"] for row in hub["people"]] == ["B"]
    assert [x["title"] for x in hub["people"][0]["chores"]] == ["Cat"]


def test_complete_past_day_uses_log_even_for_deleted_chore(client, app_mod):
    c = app_mod._db()
    today = app_mod._today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    pid, cid = _seed_person_chore(client)
    _log(c, yesterday, cid, pid)
    client.delete(f"/api/admin/chores/{cid}")
    # toggle done on the frozen row: person defaults to the logged assignee
    r = client.post(f"/api/chores/{cid}/complete", json={"date": yesterday})
    assert r.status_code == 200
    assert fdb.completions_between(c, yesterday, yesterday)[0]["person_id"] == pid
    # ...and back off
    assert client.delete(
        f"/api/chores/{cid}/complete?date={yesterday}").status_code == 200
    assert fdb.completions_between(c, yesterday, yesterday) == []


def test_complete_rejects_out_of_range_dates(client, app_mod):
    pid, cid = _seed_person_chore(client)
    far = (app_mod._today() + dt.timedelta(days=400)).isoformat()
    r = client.post(f"/api/chores/{cid}/complete", json={"date": far})
    assert r.status_code == 422


def test_legacy_db_backfills_occurrence_log_once(app_mod):
    """A pre-log deployment (completions but an empty occurrence_log) gets its
    recent history reconstructed from current definitions on first boot, so
    existing streaks survive the upgrade; the backfill never runs again."""
    from fastapi.testclient import TestClient
    conn = fdb.connect(app_mod.DB_PATH)
    fdb.ensure_schema(conn)
    today = dt.date.fromisoformat(app_mod._today().isoformat())
    epoch = (today - dt.timedelta(days=10)).isoformat()
    pid = fdb.add_person(conn, "Remy", "#5BC9F0")
    cid = fdb.add_chore(conn, title="Dishes", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch=epoch)
    for i in (2, 1):
        fdb.set_completion(conn, cid, (today - dt.timedelta(days=i)).isoformat(), pid)
    conn.close()
    with TestClient(app_mod.app) as tc:
        hub = tc.get("/api/hub").json()
        assert hub["people"][0]["streak"] == 2
    c = app_mod._db()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    assert [r["chore_id"] for r in fdb.day_log(c, yesterday)] == [cid]
    # the backfill is one-shot: wiping a day and re-connecting must not
    # resurrect it from live definitions
    fdb.replace_day_log(c, yesterday, [])
    app_mod._db_initialized = False   # force the one-time backfill path to re-run
    with TestClient(app_mod.app) as tc:
        tc.get("/api/hub")
    assert fdb.day_log(app_mod._db(), yesterday) == []


def test_db_connection_is_per_thread(app_mod):
    """Regression guard for issue #29: request handlers run on a thread pool, so
    _db() must hand each thread its OWN connection. One shared connection let
    two threads' transactions interleave (a commit on one committing the other's
    half-done work). Different threads -> different connection objects."""
    import threading
    main_conn = app_mod._db()
    other = {}

    def grab():
        other["conn"] = app_mod._db()

    t = threading.Thread(target=grab)
    t.start()
    t.join()
    assert other["conn"] is not main_conn
    # same thread, same connection (cached, not reconnected every call)
    assert app_mod._db() is main_conn


def test_open_sync_conn_retries_on_transient_startup_failure(app_mod, monkeypatch):
    """Regression for issue #32: a transient ensure_schema failure at sync
    startup must not kill the thread; _open_sync_conn retries until it succeeds
    instead of freezing calendar sync behind a still-healthy badge."""
    real_ensure = app_mod.fdb.ensure_schema
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_ensure(conn)

    monkeypatch.setattr(app_mod.fdb, "ensure_schema", flaky)
    monkeypatch.setattr(app_mod.time, "sleep", lambda *_: None)   # no real backoff wait
    conn = app_mod._open_sync_conn()
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert calls["n"] == 2   # failed once, retried, succeeded


def test_demo_disables_background_sync(tmp_path, monkeypatch):
    """Regression for issue #38: DEMO mode must not start the sync thread, which
    would overwrite the seeded calendar_status with 'not configured'."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    import family_hub.app as appmod
    importlib.reload(appmod)
    assert appmod._sync_enabled() is False        # DEMO -> no sync thread
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("DISABLE_SYNC", "1")
    importlib.reload(appmod)
    assert appmod._sync_enabled() is False         # DISABLE_SYNC -> no sync thread


def test_interrupted_backfill_rolls_back_whole_then_retries(app_mod, monkeypatch):
    """An interrupted backfill must commit NOTHING (no partial day-log, flag
    unset) so it re-runs from scratch on the next boot — a half-written history
    would read missing days as rest days and silently INFLATE streaks."""
    from fastapi.testclient import TestClient
    conn = fdb.connect(app_mod.DB_PATH)
    fdb.ensure_schema(conn)
    today = dt.date.fromisoformat(app_mod._today().isoformat())
    epoch = (today - dt.timedelta(days=10)).isoformat()
    pid = fdb.add_person(conn, "Remy", "#5BC9F0")
    cid = fdb.add_chore(conn, title="Dishes", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch=epoch)
    for i in (2, 1):
        fdb.set_completion(conn, cid, (today - dt.timedelta(days=i)).isoformat(), pid)
    conn.close()

    # blow up once, partway through the day loop
    real_plan_rows = app_mod.chlogic.plan_rows
    boom_day = today - dt.timedelta(days=5)
    state = {"raised": False}

    def flaky(chores, people, d):
        if d == boom_day and not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated crash mid-backfill")
        return real_plan_rows(chores, people, d)
    monkeypatch.setattr(app_mod.chlogic, "plan_rows", flaky)

    with TestClient(app_mod.app, raise_server_exceptions=False) as tc:
        assert tc.get("/api/hub").status_code == 500       # backfill aborted
    # a separate handle (no app backfill) proves nothing partial was committed
    raw = fdb.connect(app_mod.DB_PATH)
    assert fdb.logs_between(raw, "2000-01-01", "2100-01-01") == []
    assert not fdb.kv_get(raw, "occlog_backfill_done")
    raw.close()
    # next boot: the fault is spent, so the retry runs to completion
    assert state["raised"] is True
    with TestClient(app_mod.app) as tc:
        assert tc.get("/api/hub").json()["people"][0]["streak"] == 2
    assert fdb.kv_get(app_mod._db(), "occlog_backfill_done") is True


def test_hub_drops_timed_event_that_ended_at_midnight(client, app_mod):
    """A timed event whose end is exactly 00:00 today was over before today
    began — it must not be kept by the span-overlap filter (which slices the
    end DATE and would otherwise count it as 'today')."""
    c = app_mod._db()
    today = app_mod._today()
    yest = today - dt.timedelta(days=1)
    fdb.replace_events(c, [
        {"id": "mid", "calendar_id": "cal", "title": "Late show", "all_day": 0,
         "start_ts": f"{yest.isoformat()}T20:00:00-07:00",
         "end_ts": f"{today.isoformat()}T00:00:00-07:00"},
        {"id": "run", "calendar_id": "cal", "title": "Overnighter", "all_day": 0,
         "start_ts": f"{yest.isoformat()}T22:00:00-07:00",
         "end_ts": f"{today.isoformat()}T06:00:00-07:00"},
    ])
    ids = {e["id"] for e in client.get("/api/hub").json()["calendar"]["events"]}
    assert "run" in ids, "a timed event still running today must be kept"
    assert "mid" not in ids, "a timed event that ended AT midnight is yesterday's"


def test_calendar_endpoint_rejects_absurd_windows(client):
    """days/past beyond the sync window are a client bug; clamp-by-422 rather
    than letting a huge timedelta 500 the endpoint."""
    assert client.get("/api/calendar?days=1000000000").status_code == 422
    assert client.get("/api/calendar?past=99999").status_code == 422
    assert client.get("/api/calendar?days=-1").status_code == 422
    assert client.get("/api/calendar?past=-1").status_code == 422
    assert client.get("/api/calendar?days=90&past=45").status_code == 200


def test_today_freeze_updates_when_the_plan_changes(client, app_mod):
    """Today is live until it becomes past: a mid-day plan change (here a
    reassignment) must OVERWRITE today's frozen log on the next serve, or
    tomorrow's 'yesterday' view would show the stale first-serve plan."""
    c = app_mod._db()
    today = app_mod._today().isoformat()
    pa = client.post("/api/admin/people",
                     json={"name": "A", "color": "#111111"}).json()["id"]
    pb = client.post("/api/admin/people",
                     json={"name": "B", "color": "#222222"}).json()["id"]
    cid = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pa}).json()["id"]
    client.get("/api/hub")   # freeze today with A assigned
    assert [(r["chore_id"], r["person_id"]) for r in fdb.day_log(c, today)] == \
        [(cid, pa)]
    client.patch(f"/api/admin/chores/{cid}", json={"fixed_person_id": pb})
    client.get("/api/hub")   # re-serve -> today's frozen row must follow to B
    assert [(r["chore_id"], r["person_id"]) for r in fdb.day_log(c, today)] == \
        [(cid, pb)]


def test_future_day_overlays_live_plan_into_streak_and_week(client, app_mod):
    """A future day isn't frozen, but the day browser overlays its live plan so
    the prospective streak/week reflect that day's occurring chores."""
    today = app_mod._today()
    tomorrow = today + dt.timedelta(days=1)
    pid = client.post("/api/admin/people",
                      json={"name": "Sam", "color": "#C39BEA"}).json()["id"]
    cid = client.post("/api/admin/chores", json={
        "title": "Sweep", "schedule_kind": "days",
        "days_mask": 1 << tomorrow.weekday(), "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    sam = client.get(
        f"/api/chores/day?date={tomorrow.isoformat()}").json()["people"][0]
    assert [ch["title"] for ch in sam["chores"]] == ["Sweep"]   # occurs tomorrow
    assert len(sam["week"]) == 7
    # tomorrow is the last week-strip cell; the chore occurs + isn't done ->
    # "none" (not "rest"), which only holds if the live overlay populated the
    # future day's occurrence set
    assert sam["week"][-1] == "none"
    assert "streak" in sam
