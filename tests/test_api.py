import datetime as dt
import importlib
import json
import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from family_hub import db as fdb
from family_hub import tiles as ftiles
from family_hub import todos as tdlogic


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


def test_app_quiets_routine_polls_in_the_access_log(app_mod):
    """The camera probes, tile polls and health checks were the biggest
    source of container log lines. Importing the app attaches the filter that
    drops their successful access-log lines (tests/test_access_log.py)."""
    from family_hub import access_log
    assert any(isinstance(f, access_log.QuietPollFilter)
               for f in logging.getLogger("uvicorn.access").filters)


def test_httpx_request_lines_are_quiet_but_its_warnings_are_not(app_mod):
    """httpx logs every request at INFO. With the laundry watcher polling Home
    Assistant every 5s that was two thirds of the hub's log (~19 MB a day).
    Its INFO lines are dropped; its warnings and errors still come through."""
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        assert not lg.isEnabledFor(logging.INFO), name
        assert lg.isEnabledFor(logging.WARNING), name
        assert lg.level == logging.WARNING, f"{name} is pinned, not inherited"


def test_health_fails_when_the_db_cannot_open(client, app_mod, monkeypatch):
    """/health never touched the database, so a hub whose hub.db was gone or
    locked out stayed "healthy" to Docker while every request 500ed."""
    def boom():
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(app_mod, "_db", boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "error"


def test_health_reads_the_file_not_just_select_1(client, app_mod, monkeypatch,
                                                 tmp_path):
    """SELECT 1 never reads the file, so it passes even on a corrupt db. The
    check must read a real page (the schema) to mean anything."""
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a sqlite database" * 200)
    bad = sqlite3.connect(str(junk), check_same_thread=False)
    assert bad.execute("SELECT 1").fetchone() == (1,)   # the weak check passes
    monkeypatch.setattr(app_mod, "_db", lambda: bad)
    assert client.get("/health").status_code == 503


def test_health_fails_when_hub_db_was_deleted_under_an_open_connection(
        client, app_mod, monkeypatch, tmp_path):
    """A pooled connection opened before the file vanished still reads the
    unlinked file happily, so the path itself is checked."""
    monkeypatch.setattr(app_mod, "DB_PATH", str(tmp_path / "gone" / "hub.db"))
    assert client.get("/health").status_code == 503


def test_health_fails_on_the_empty_file_a_missing_db_leaves(
        client, app_mod, monkeypatch, tmp_path):
    """If hub.db is deleted, the next connect quietly creates an empty file
    with no tables. That must read unhealthy, not ok."""
    empty = sqlite3.connect(str(tmp_path / "fresh.db"), check_same_thread=False)
    monkeypatch.setattr(app_mod, "_db", lambda: empty)
    assert client.get("/health").status_code == 503


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


def test_hub_theme_layout_and_idle_return_survive(tmp_path, monkeypatch):
    """A house can set default layout + idle auto-return for fresh devices, so
    both must round-trip through config validation (the frontend applyHouseTheme
    re-stamps them on an un-overridden device). Regression guard: _THEME_AXES
    omitting either would silently drop it, and the documented house default
    would never reach any device."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"theme": {"layout": "desktop", "idleReturn": "off"}})
    with TestClient(appmod.app) as c:
        theme = c.get("/api/hub").json()["theme"]
    assert theme == {"layout": "desktop", "idleReturn": "off"}
    # invalid values are dropped like every other axis (never crash, never leak)
    appmod2 = _reload_with(tmp_path, monkeypatch,
                           {"theme": {"layout": "sideways", "idleReturn": "maybe"}})
    with TestClient(appmod2.app) as c:
        assert c.get("/api/hub").json()["theme"] is None


def test_hub_theme_season_survives(tmp_path, monkeypatch):
    """A house can turn seasonal looks on for fresh devices: theme.season must
    round-trip through config validation, and a junk value is dropped."""
    appmod = _reload_with(tmp_path, monkeypatch, {"theme": {"season": "on"}})
    with TestClient(appmod.app) as c:
        assert c.get("/api/hub").json()["theme"] == {"season": "on"}
    appmod2 = _reload_with(tmp_path, monkeypatch, {"theme": {"season": "halloween"}})
    with TestClient(appmod2.app) as c:
        assert c.get("/api/hub").json()["theme"] is None


def test_seasonal_art_revalidates(tmp_path, monkeypatch):
    """The seasonal photos are referenced from inside styles.css, where no ?v=
    reaches them, and a photo can be swapped under the same name: they must revalidate
    or phones keep stale art after a release. Plain images outside /seasons/
    (the favicon, the home-screen icon) keep their default caching."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as c:
        r = c.get("/seasons/fall-aspen-grove.webp")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"
        assert "cache-control" not in c.get("/apple-touch-icon.png").headers


def test_hub_build_token_tracks_the_served_config(tmp_path, monkeypatch):
    """Cameras and panels are built ONCE per page load on the wall, so a config
    change (a new camera, a moved panel) never reached an open wall: the build
    token hashed only the static assets, the deploy restart kept it the same, and
    the wall never reloaded. The token now folds in the loaded config, so a
    config change reloads the wall through the same idle-reload path a deploy
    uses. Same config => same token (no reload loop); /api/version agrees."""
    cams_a = {"go2rtc_base": "http://cam", "cameras": [{"src": "a", "label": "A"}]}
    cams_b = {"go2rtc_base": "http://cam", "cameras": [{"src": "b", "label": "B"}]}
    appmod = _reload_with(tmp_path, monkeypatch, cams_a)
    with TestClient(appmod.app) as c:
        build_a = c.get("/api/hub").json()["build"]
        assert c.get("/api/version").json()["build"] == build_a
    appmod = _reload_with(tmp_path, monkeypatch, cams_a)
    with TestClient(appmod.app) as c:
        assert c.get("/api/hub").json()["build"] == build_a, \
            "an unchanged config must keep the token, or every restart reloads the wall"
    appmod = _reload_with(tmp_path, monkeypatch, cams_b)
    with TestClient(appmod.app) as c:
        build_b = c.get("/api/hub").json()["build"]
    assert re.fullmatch(r"[0-9a-f]{12}", build_b)
    assert build_b != build_a, "a config change must change the reload token"


def test_config_fingerprint_failure_logs_and_keeps_a_token(app_mod, caplog):
    """An unserializable config must never take the app down at import: it
    logs a warning (the config half of the token is lost, and says so) and
    the build token is still a well-formed asset hash."""
    class Weird:
        pass
    with caplog.at_level(logging.WARNING, logger="family_hub"):
        fp = app_mod._config_fingerprint(Weird())   # not a dataclass: asdict raises
    assert fp == ""
    assert any("not fingerprinted" in r.getMessage() for r in caplog.records)
    assert re.fullmatch(r"[0-9a-f]{12}", app_mod._compute_build(fp))


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
    p1 = fdb.add_person(c, "Ben", "#5BC9F0")
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
    assert people["Ben"]["done_count"] >= 1
    # Dishes should be marked done for Ben
    dishes = [ch for ch in people["Ben"]["chores"] if ch["title"] == "Dishes"][0]
    assert dishes["done"] is True
    assert "streak" in people["Ben"] and "week" in people["Ben"]
    assert len(people["Ben"]["week"]) == 7
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
                       "tile": "/go2rtc/stream.html?src=cam&mode=webrtc",
                       "full": "/go2rtc/stream.html?src=cam_hd",
                       "has_hd": True, "hd_src": "cam_hd"}   # distinct 4K twin
    assert cams[1]["src"] == "wyze" and cams[1]["label"] == "Back Yard"
    assert cams[1]["full"] == "/go2rtc/stream.html?src=wyze"  # no hd stream
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
    pid = fdb.add_person(c, "Ben", "#5BC9F0")
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
    pid = fdb.add_person(c, "Ben", "#5BC9F0")
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
    pid = fdb.add_person(c, "Ben", "#5BC9F0")
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
    r = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/api/admin/people", json={"name": "x", "color": "blue"}).status_code == 422
    assert client.post("/api/admin/people", json={"name": "", "color": "#5BC9F0"}).status_code == 422
    # A trailing newline must NOT slip through the hex check ($ vs \Z): a
    # malformed color would otherwise reach the client as a CSS value.
    assert client.post("/api/admin/people",
                       json={"name": "x", "color": "#5BC9F0\n"}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}", json={"name": "Ben2"}).status_code == 200
    assert client.patch("/api/admin/people/9999", json={"name": "z"}).status_code == 404
    state = client.get("/api/admin/state").json()
    assert state["people"][0]["name"] == "Ben2"


def test_calendar_exposes_synced_window(tmp_path, monkeypatch):
    """Regression for issue #37: /api/calendar reports the range the sync caches
    (from config) so the frontend can mark days outside it as not-synced instead
    of rendering them as free."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"calendar_window_days": 10, "calendar_past_days": 5})
    with TestClient(appmod.app) as c:
        win = c.get("/api/calendar").json()["window"]
    today = appmod._today()
    assert win["to"] == (today + dt.timedelta(days=10)).isoformat()
    assert win["from"] == (today - dt.timedelta(days=5)).isoformat()


def test_calendar_window_never_promises_more_than_the_sync_covered(tmp_path, monkeypatch):
    """The window is a promise that a missing day in it is really free. Config is
    not evidence of that: right after calendar_window_days is raised (or while a
    source keeps failing and holds stale rows) the cache covers less than config
    claims, and those days would render "nothing scheduled" — a confident lie
    about a calendar nobody fetched. Intersect with what a sync really covered."""
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        today = appmod._today()
        # Nothing synced yet: claim NOTHING. An empty window (to < from) hatches
        # every day, which is the honest answer before the first successful pass.
        win = c.get("/api/calendar").json()["window"]
        assert win["to"] < win["from"]
        assert win["to"] < today.isoformat(), "today itself is not yet vouched for"

        # A pass that really covered 30 days caps the window to 30, not the 400
        # config asks for.
        appmod.fdb.kv_set(appmod._db(), "calendar_covered",
                          {"from": (today - dt.timedelta(days=45)).isoformat(),
                           "to": (today + dt.timedelta(days=30)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=30)).isoformat()
    assert win["from"] == (today - dt.timedelta(days=45)).isoformat()


def _with_icloud(monkeypatch):
    """Make the CalDAV integration AVAILABLE: _integration_on requires real
    credentials (env or the creds file), not just an enabled toggle, so without
    these the CalDAV gate is off and the coverage cap never runs."""
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@example.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "app-specific-pw")


def test_calendar_window_is_capped_by_caldav_coverage_too(tmp_path, monkeypatch):
    """The CalDAV half of the intersection. An iCloud collection that only pulled
    28 days must cap the window at 28 even with Google's coverage AND the config
    both at 400 — otherwise the wall reports days iCloud was never asked about as
    free. (The Google half is covered above; this half had no API test at all.)"""
    _with_icloud(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:abc", "VEVENT", "Family", None, today.isoformat())
        full = {"from": (today - dt.timedelta(days=45)).isoformat(),
                "to": (today + dt.timedelta(days=400)).isoformat()}
        appmod.fdb.kv_set(conn, "calendar_covered", full)
        appmod.fdb.kv_set(conn, "caldav_covered",
                          {"from": full["from"],
                           "to": (today + dt.timedelta(days=28)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=28)).isoformat(), \
        "the narrowest ACTIVE source decides how far the window may claim"


def test_calendar_window_is_not_narrowed_by_a_disabled_caldav_collection(tmp_path, monkeypatch):
    """A collection the operator unchecked in the picker contributes no events —
    they're filtered out of the payload — so it cannot be hiding a hole. Letting
    it narrow the window hatched an entire healthy Google-backed calendar over a
    source deliberately switched off."""
    _with_icloud(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:abc", "VEVENT", "Family", None, today.isoformat())
        appmod.fdb.set_caldav_collection_enabled(conn, "caldav:abc", False)
        # Google covered the full window; CalDAV has NO coverage record at all,
        # which for an active source would collapse the window to empty.
        appmod.fdb.kv_set(conn, "calendar_covered",
                          {"from": (today - dt.timedelta(days=45)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=400)).isoformat()
    assert win["from"] == (today - dt.timedelta(days=45)).isoformat()


def test_calendar_window_is_not_narrowed_when_both_calendar_toggles_are_off(tmp_path, monkeypatch):
    """The gate's OTHER limb: its sibling test covers only "no calendars
    configured". With calendars configured but Google AND ICS both switched off,
    their events are filtered out of the payload entirely, so a missing coverage
    record cannot be hiding anything and the window must not collapse.

    integration_enabled defaults to True on an UNSEEDED row, so the rows have to
    be seeded before set_integration_enabled does anything at all — skip that and
    this test passes for the wrong reason (toggles still on, gate still capping)."""
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 30, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        for iid in ("google_calendar", "ics_calendar"):
            appmod.fdb.seed_integration(conn, iid, "calendar")
            assert appmod.fdb.set_integration_enabled(conn, iid, False), \
                "no row to toggle — the rest of this test would prove nothing"
            assert appmod.fdb.integration_enabled(conn, iid) is False
        # No calendar_covered record exists at all, which for an ACTIVE source
        # collapses the window to empty. Both toggles off means it must not.
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=30)).isoformat()
    assert win["from"] == (today - dt.timedelta(days=45)).isoformat()


def test_calendar_window_is_not_narrowed_when_caldav_is_unavailable(tmp_path, monkeypatch):
    """The AVAILABILITY half of the CalDAV gate (its sibling pins the `enabled`
    half). Collection rows are never pruned, so they outlive pulled credentials:
    without this check a disconnected iCloud would keep collapsing the window of
    a healthy Google-backed calendar, forever."""
    monkeypatch.delenv("ICLOUD_CALDAV_USER", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_APP_PASSWORD", raising=False)
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:abc", "VEVENT", "Family", None, today.isoformat())
        appmod.fdb.kv_set(conn, "calendar_covered",
                          {"from": (today - dt.timedelta(days=45)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=400)).isoformat()


def test_calendar_window_is_not_narrowed_by_an_enabled_reminders_list(tmp_path, monkeypatch):
    """caldav_covered tracks the EVENT fetch, but collection rows include VTODO
    reminder lists. With every real calendar unchecked and only a reminders list
    enabled, the gate must not fire — it would collapse the window to empty and
    hatch the entire wall over a source that carries no events at all."""
    _with_icloud(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:rem", "VTODO", "Reminders", None, today.isoformat())
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:abc", "VEVENT", "Family", None, today.isoformat())
        appmod.fdb.set_caldav_collection_enabled(conn, "caldav:abc", False)
        # No caldav_covered at all: for an ACTIVE source that collapses to empty.
        appmod.fdb.kv_set(conn, "calendar_covered",
                          {"from": (today - dt.timedelta(days=45)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["to"] == (today + dt.timedelta(days=400)).isoformat()


def test_caldav_coverage_caps_the_past_side_too(tmp_path, monkeypatch):
    """The CalDAV back half of the intersection, the twin of the Google one."""
    _with_icloud(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        conn, today = appmod._db(), appmod._today()
        appmod.fdb.upsert_caldav_collection(
            conn, "caldav:abc", "VEVENT", "Family", None, today.isoformat())
        appmod.fdb.kv_set(conn, "calendar_covered",
                          {"from": (today - dt.timedelta(days=45)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        appmod.fdb.kv_set(conn, "caldav_covered",
                          {"from": (today - dt.timedelta(days=7)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["from"] == (today - dt.timedelta(days=7)).isoformat()


def test_startup_warns_when_a_configured_window_is_below_the_fetch(tmp_path, monkeypatch, caplog):
    """An existing install's private config.json is the ONE file the static chain
    guard can never see, and the only place this misconfiguration lives. Both
    directions warn, or the hatching has no explanation anywhere."""
    with caplog.at_level(logging.WARNING):
        _reload_with(tmp_path, monkeypatch,
                     {"calendar_window_days": 30, "calendar_past_days": 7})
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    # Assert the THRESHOLD each one breached, not just the configured value:
    # pairing calendar_past_days against CAL_FETCH_DAYS (400) instead of
    # CAL_FETCH_PAST (45) would warn forever on a correct install, and a
    # value-only assertion cannot see that.
    assert "calendar_window_days=30 is below the 400 days" in msgs
    assert "calendar_past_days=7 is below the 45 days" in msgs


def test_startup_is_silent_when_the_configured_windows_match_the_fetch(tmp_path, monkeypatch, caplog):
    """The boundary, which the warning test above cannot reach: a config sitting
    exactly ON the fetch is correct and must stay silent. With `<` relaxed to
    `<=`, every correctly configured install warns at every boot, which trains
    the operator to ignore the one signal this warning exists to send."""
    with caplog.at_level(logging.WARNING):
        _reload_with(tmp_path, monkeypatch,
                     {"calendar_window_days": 400, "calendar_past_days": 45})
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "days the wall fetches" not in msgs


def test_calendar_window_coverage_caps_the_past_side_too(tmp_path, monkeypatch):
    """The backward half of the intersection is real, not decorative: a sync that
    only reached 7 days back must not have the window claim the configured 45."""
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400, "calendar_past_days": 45,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        today = appmod._today()
        appmod.fdb.kv_set(appmod._db(), "calendar_covered",
                          {"from": (today - dt.timedelta(days=7)).isoformat(),
                           "to": (today + dt.timedelta(days=400)).isoformat()})
        win = c.get("/api/calendar").json()["window"]
    assert win["from"] == (today - dt.timedelta(days=7)).isoformat()
    assert win["to"] == (today + dt.timedelta(days=400)).isoformat()


@pytest.mark.parametrize("junk", [
    "garbage-string",                       # not a dict -> stopped by the isinstance guard
    ["2026-01-01", "2027-01-01"],           # not a dict -> stopped by the isinstance guard
    {"from": 20260101, "to": 20270101},     # numbers -> TypeError
    {"from": "not-a-date", "to": "nope"},   # unparseable -> ValueError
    {"to": "2027-01-01"},                   # truncated -> KeyError
])
def test_calendar_survives_a_malformed_coverage_record(tmp_path, monkeypatch, junk):
    """A bad kv row must degrade to the honest empty window, never raise.
    _calendar_block is inlined into /api/hub, so an exception here blanks the
    ENTIRE wall — chores, to-dos, cameras — over one unparseable record."""
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendar_window_days": 400,
        "calendars": [{"id": "cal", "label": "Fam", "color": "#fff"}]})
    with TestClient(appmod.app) as c:
        appmod.fdb.kv_set(appmod._db(), "calendar_covered", junk)
        r = c.get("/api/calendar")
        assert r.status_code == 200
        win = r.json()["window"]
        assert win["to"] < win["from"], "junk must claim nothing, not over-claim"
        assert c.get("/api/hub").status_code == 200, "the whole wall must survive it"


def test_calendar_window_is_not_narrowed_by_an_inactive_source(tmp_path, monkeypatch):
    """A source with nothing configured contributes no rows, so it can't leave a
    hole and must not narrow the window — otherwise an install with no CalDAV
    would hatch its whole (healthy, Google-backed) calendar."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"calendar_window_days": 30, "calendars": []})
    with TestClient(appmod.app) as c:
        win = c.get("/api/calendar").json()["window"]
        today = appmod._today()
    assert win["to"] == (today + dt.timedelta(days=30)).isoformat()


def test_calendar_window_is_intersection_of_fetch_and_sync(tmp_path, monkeypatch):
    """The window must be the INTERSECTION of the fetch range and the sync
    coverage. A config window wider than the fixed fetch (frontend uses
    days=400&past=45) must cap to the fetch, or days the sync caches but this
    request never fetched would render as falsely-empty instead of not-synced."""
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"calendar_window_days": 500, "calendar_past_days": 60})
    with TestClient(appmod.app) as c:
        win = c.get("/api/calendar").json()["window"]              # default fetch 400/45
        win2 = c.get("/api/calendar?days=10&past=5").json()["window"]
    today = appmod._today()
    # config 500/60 capped to the fetch 400/45
    assert win["to"] == (today + dt.timedelta(days=400)).isoformat()
    assert win["from"] == (today - dt.timedelta(days=45)).isoformat()
    # an explicit narrower fetch caps further
    assert win2["to"] == (today + dt.timedelta(days=10)).isoformat()
    assert win2["from"] == (today - dt.timedelta(days=5)).isoformat()


def test_admin_patch_rejects_explicit_null_on_nonnullable_fields(client, app_mod):
    """Regression for issue #35: an explicit JSON null for a field backed by a
    NOT NULL column is a 422, not a 500 from the DB write. fixed_person_id (the
    one nullable chore field) still accepts null."""
    pid = client.post("/api/admin/people",
                      json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
    assert client.patch(f"/api/admin/people/{pid}", json={"sort": None}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}", json={"active": None}).status_code == 422
    cid = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    assert client.patch(f"/api/admin/chores/{cid}", json={"icon": None}).status_code == 422
    assert client.patch(f"/api/admin/chores/{cid}", json={"active": None}).status_code == 422
    # active is a 0/1 flag: any other number is a bad request, not a row the
    # `active = 1` filters then silently treat as inactive
    for bad in (2, -1, 7):
        assert client.patch(f"/api/admin/people/{pid}",
                            json={"active": bad}).status_code == 422
        assert client.patch(f"/api/admin/chores/{cid}",
                            json={"active": bad}).status_code == 422
    assert client.patch(f"/api/admin/people/{pid}",
                        json={"active": 1}).status_code == 200
    # JSON true/false still work (they mean 1/0) and are stored as 1/0, which
    # the `active = 1` filters read correctly
    people = lambda: {p["id"]: p for p in
                      client.get("/api/admin/state").json()["people"]}
    chores = lambda: {c["id"]: c for c in
                      client.get("/api/admin/state").json()["chores"]}
    for flag, stored in ((False, 0), (True, 1)):
        assert client.patch(f"/api/admin/people/{pid}",
                            json={"active": flag}).status_code == 200
        got = people()[pid]["active"]
        assert type(got) is int and got == stored, got   # 1/0, not true/false
        assert client.patch(f"/api/admin/chores/{cid}",
                            json={"active": flag}).status_code == 200
        got = chores()[cid]["active"]
        assert type(got) is int and got == stored, got   # 1/0, not true/false
    # fixed_person_id: null is legitimate (clearing the fixed assignee) -> 200
    assert client.patch(f"/api/admin/chores/{cid}",
                        json={"assign_kind": "rotation", "rotation_order": [pid],
                              "fixed_person_id": None}).status_code == 200


def test_admin_delete_person_hard_removes_and_404s(client, app_mod):
    """The hard-delete endpoint removes the person entirely (distinct from the
    PATCH active=0 deactivate path); an unknown id is a 404."""
    pid = client.post("/api/admin/people",
                      json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
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
    pr = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"})
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
    pr = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"})
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
    pr = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"})
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
                      json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
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
                      json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
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


def test_camera_probe_skips_a_non_dict_entry(tmp_path, monkeypatch):
    """One non-object camera entry (a bare string typo) used to 500 EVERY
    probe (`entry.get` on a str). It is skipped, like _camera_links does."""
    appmod = _reload_with(tmp_path, monkeypatch, {
        "go2rtc_base": "http://cam",
        "cameras": ["Driveway", {"src": "cam", "label": "Driveway"}]})

    async def ok(hclient, cfg, src="cam"):
        return (b"\xff\xd8jpeg", "image/jpeg")
    monkeypatch.setattr("family_hub.tiles.camera_snapshot", ok)
    with TestClient(appmod.app) as c:
        assert c.get("/api/tiles/camera.jpg?src=cam").status_code == 200
        assert c.get("/api/tiles/camera.jpg?src=Driveway").status_code == 404


def test_camera_probe_is_404_when_no_cameras_are_configured(tmp_path,
                                                             monkeypatch):
    """With no cameras, the allowlist used to fall back to {"cam"}, so a hub
    with no cameras still proxied a probe of a go2rtc stream named `cam`."""
    appmod = _reload_with(tmp_path, monkeypatch, {"go2rtc_base": "http://cam"})
    seen = []

    async def ok(hclient, cfg, src="cam"):
        seen.append(src)
        return (b"\xff\xd8jpeg", "image/jpeg")
    monkeypatch.setattr("family_hub.tiles.camera_snapshot", ok)
    with TestClient(appmod.app) as c:
        assert c.get("/api/tiles/camera.jpg").status_code == 404
        assert c.get("/api/tiles/camera.jpg?src=cam").status_code == 404
    assert seen == []


def test_html_is_never_heuristically_cached(client):
    """Phones cached a stale index.html past a deploy (2026-08-13, missing tab
    bar): the HTML must say no-cache so browsers revalidate. API JSON is left
    alone."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert "cache-control" not in {k.lower() for k in client.get("/health").headers}


def test_scripts_and_styles_always_revalidate(client):
    """The ?v= busters only move on a release, so a hub.js or styles.css change
    shipped without a version bump kept the old URL: the deploy reload fetched
    fresh HTML but the browser could serve the old script from its heuristic
    cache (no Cache-Control plus a Last-Modified), leaving the wall on stale JS
    under a new build token. Every script, stylesheet and the manifest must
    revalidate; a 304 costs the wall almost nothing on the LAN."""
    for path in ("/hub.js?v=1.6.0", "/common.js", "/osk.js", "/theme.js",
                 "/styles.css?v=1.6.0", "/manifest.webmanifest"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
    # and a revalidation really is cheap: the ETag round-trips to a 304
    r = client.get("/hub.js")
    etag = r.headers.get("etag")
    assert etag, "static files must carry an ETag so no-cache revalidates to a 304"
    assert client.get("/hub.js", headers={"If-None-Match": etag}).status_code == 304


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
        "tile": "/go2rtc/stream.html?src=cam2&mode=webrtc",
        "full": "/go2rtc/stream.html?src=cam2_hd",
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


def test_todos_complete_lingers_briefly_then_archives(client, app_mod):
    tid = client.post("/api/todos", json={"title": "Water plants"}).json()["id"]
    assert client.post(f"/api/todos/{tid}/complete").json() == {"ok": True}

    # just checked: still visible in buckets, struck via done_at, in recent_done
    data = client.get("/api/todos").json()
    assert [t["id"] for t in data["buckets"]["now"]] == [tid]
    assert data["buckets"]["now"][0]["done_at"] is not None
    assert [t["id"] for t in data["recent_done"]] == [tid]
    assert [t["id"] for t in client.get("/api/hub").json()["todos"]["now"]] == [tid]

    # age the REAL stored stamp past the grace window, same day: gone from
    # buckets and the wall, still restorable (operator report 2026-09-22: a
    # checked item sat on the wall all day, so checking it off looked broken)
    c = app_mod._db()
    aged = (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=tdlogic.DONE_GRACE_MIN, seconds=5)).isoformat()
    c.execute("UPDATE todos SET done_at = ? WHERE id = ?", (aged, tid))
    c.commit()
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
                      json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
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


def test_uncomplete_rejects_bad_and_out_of_range_dates(client, app_mod):
    """DELETE takes the same date the wall sends on POST, so it validates it
    the same way: a garbled or far-off date is a 422, not a silent no-op."""
    pid, cid = _seed_person_chore(client)
    far = (app_mod._today() - dt.timedelta(days=400)).isoformat()
    assert client.delete(
        f"/api/chores/{cid}/complete?date=nope").status_code == 422
    assert client.delete(
        f"/api/chores/{cid}/complete?date={far}").status_code == 422


def test_tap_just_after_midnight_lands_on_the_day_the_wall_showed(
        client, app_mod, monkeypatch):
    """The wall polls every so often, so for a minute after midnight it still
    shows yesterday's chores. A tap then sends the date it is showing, and the
    server must credit THAT day, not its own new date."""
    c = app_mod._db()
    pid, cid = _seed_person_chore(client)
    shown = app_mod._today()
    client.get("/api/hub")                       # the wall served (and froze) it
    monkeypatch.setattr(app_mod, "_today",
                        lambda: shown + dt.timedelta(days=1))
    r = client.post(f"/api/chores/{cid}/complete",
                    json={"date": shown.isoformat()})
    assert r.status_code == 200
    s = shown.isoformat()
    n = (shown + dt.timedelta(days=1)).isoformat()
    assert [x["chore_id"] for x in fdb.completions_between(c, s, s)] == [cid]
    assert fdb.completions_between(c, n, n) == []
    assert client.delete(
        f"/api/chores/{cid}/complete?date={s}").status_code == 200
    assert fdb.completions_between(c, s, s) == []


def test_legacy_db_backfills_occurrence_log_once(app_mod):
    """A pre-log deployment (completions but an empty occurrence_log) gets its
    recent history reconstructed from current definitions on first boot, so
    existing streaks survive the upgrade; the backfill never runs again."""
    from fastapi.testclient import TestClient
    conn = fdb.connect(app_mod.DB_PATH)
    fdb.ensure_schema(conn)
    today = dt.date.fromisoformat(app_mod._today().isoformat())
    epoch = (today - dt.timedelta(days=10)).isoformat()
    pid = fdb.add_person(conn, "Ben", "#5BC9F0")
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


# --- away overlay -----------------------------------------------------------
#
# CONVENTION for every test in this block (T0): freeze _today BEFORE the first
# client call. admin_add_chore anchors rotation_epoch to _today(), and occurs()
# is False before the epoch -- seeding against the real clock makes the test
# date-dependent and it rots at the first midnight after it was written.

def test_away_person_freezes_no_rows_and_streak_continues(client, app_mod, monkeypatch):
    """A person marked away: their fixed daily chore (no backup) pauses --
    no occurrence_log row is written for them on away days -- and their
    streak treats the away span as rest, so it survives the gap and resumes
    counting on return.

    Day layout, relative to RETURN_DAY (D0):
      D0-7..D0-4  pretrip: 4 consecutive completed days (seeded directly into
                  the frozen log + completions, like the other freeze tests)
      D0-3..D0-1  away (3 days), closed at D0-1 ("the day before return")
      D0-2        the day we check mid-trip (away=True, streak still 4)
      D0          return day: chore completed, streak == 4 + 1 == 5
    """
    c = app_mod._db()
    return_day = dt.date(2026, 8, 17)
    # Freeze today BEFORE seeding: admin_add_chore anchors rotation_epoch to
    # _today(), and occurs() is False before the epoch -- seeding at the real
    # clock made this test date-dependent (it broke the first midnight after
    # it was written, when real-today moved past return_day).
    monkeypatch.setattr(app_mod, "_today",
                        lambda: return_day - dt.timedelta(days=7))
    pid, cid = _seed_person_chore(client, title="Dishes")

    pretrip_days = [return_day - dt.timedelta(days=i) for i in (7, 6, 5, 4)]
    for d in pretrip_days:
        ds = d.isoformat()
        _log(c, ds, cid, pid, title="Dishes")
        fdb.set_completion(c, cid, ds, pid)

    away_start = return_day - dt.timedelta(days=3)
    away_last = return_day - dt.timedelta(days=1)   # day before return
    period_id = fdb.add_away_period(c, pid, away_start.isoformat())

    # --- mid-trip: away day, streak unchanged (rest-day skip) --------------
    mid_trip = return_day - dt.timedelta(days=2)
    monkeypatch.setattr(app_mod, "_today", lambda: mid_trip)
    hub = client.get("/api/hub").json()
    assert len(hub["people"]) == 1
    entry = hub["people"][0]
    assert entry["person"]["id"] == pid
    assert entry["away"] is True
    assert entry["streak"] == 4, "away days are skipped as rest; pretrip streak survives"

    # frozen log for the away day has NO row for this person's chore -- the
    # fixed chore paused (no backup), so nothing was covered either
    mid_rows = fdb.day_log(c, mid_trip.isoformat())
    assert not any(r["person_id"] == pid and r["chore_id"] == cid for r in mid_rows)

    # --- close the period the day before return, complete on return -------
    fdb.close_away_period(c, period_id, away_last.isoformat())
    monkeypatch.setattr(app_mod, "_today", lambda: return_day)
    assert client.post(f"/api/chores/{cid}/complete").json() == {"ok": True}
    hub = client.get("/api/hub").json()
    entry = hub["people"][0]
    assert entry["away"] is False
    assert entry["streak"] == 5, "pretrip streak (4) + the completed return day (1)"


def test_away_overlay_build_fails_soft(client, app_mod, monkeypatch, caplog):
    """A broken away_map() (bad row, read failure) must not 500 the whole
    wall -- same fails-soft philosophy as the todos block in hub(). The wall
    renders with no away overlay (nobody marked away) instead of crashing."""
    import logging
    _seed_person_chore(client, title="Dishes")

    def boom(*a, **k):
        raise RuntimeError("simulated away_map failure")
    monkeypatch.setattr(app_mod.fdb, "away_map", boom)
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        r = client.get("/api/hub")
    assert r.status_code == 200
    body = r.json()
    entry = body["people"][0]
    assert entry["away"] is False
    # S1b: the wall gets a degraded-state flag (mirrors todos_ok) so it can show
    # a "away status unavailable" note instead of silently rendering present.
    assert body["away_ok"] is False
    assert any("away overlay" in rec.getMessage()
               for rec in caplog.records if rec.levelno >= logging.ERROR)


def test_failed_away_overlay_never_freezes_todays_log(client, app_mod,
                                                      monkeypatch):
    """Today's plan is frozen into the occurrence log on every serve, and that
    log becomes permanent history. If the away overlay failed, the plan was
    built as if nobody were away, so freezing it would record the wrong owner
    forever. A degraded serve must leave the log alone: no first write, and no
    overwrite of a good record frozen earlier in the day."""
    c = app_mod._db()
    today = app_mod._today().isoformat()
    pid, cid = _seed_person_chore(client, title="Dishes")

    def boom(*a, **k):
        raise RuntimeError("simulated away_map failure")
    monkeypatch.setattr(app_mod.fdb, "away_map", boom)
    assert client.get("/api/hub").json()["away_ok"] is False
    assert fdb.day_log(c, today) == [], "a degraded plan must not be frozen"

    # A good record frozen earlier (here: a backup owned it) survives too.
    other = client.post("/api/admin/people",
                        json={"name": "Kit", "color": "#E0A030"}).json()["id"]
    _log(c, today, cid, other)
    client.get("/api/hub")
    client.get(f"/api/chores/day?date={today}")
    assert [r["person_id"] for r in fdb.day_log(c, today)] == [other]


def test_hub_away_ok_by_default(client, app_mod):
    _seed_person_chore(client, title="Dishes")
    assert client.get("/api/hub").json()["away_ok"] is True


def test_admin_state_and_settings_surface_a_failed_chore_mirror(client, app_mod):
    """M1: a chore-mirror tick that reported an error must be visible — in
    /api/admin/state, and as the 'error' state on the iCloud settings row (the
    mirror runs inside that integration). Before this it failed silently
    forever with every badge still reading ok."""
    c = app_mod._db()
    assert client.get("/api/admin/state").json()["chore_mirror_status"] == {}
    fdb.kv_set(c, "chore_mirror_status",
               {"ok": False, "at": "2026-08-17T12:00:00", "created": 0,
                "moved": 0, "updated": 0, "deleted": 0})
    assert client.get("/api/admin/state").json()["chore_mirror_status"]["ok"] is False
    # the settings row for iCloud reads 'error' (the pure helper, since iCloud
    # needs credentials to appear in the integrations list at all)
    assert app_mod._integ_status("icloud_caldav", {"ok": True}, {},
                                 {"ok": False}) == "error"
    assert app_mod._integ_status("icloud_caldav", {"ok": True}, {},
                                 {"ok": True}) == "ok"
    assert app_mod._integ_status("google_calendar", {}, {"ok": True},
                                 {"ok": False}) == "ok", \
        "a mirror failure only marks the integration it runs inside"


def test_admin_state_includes_away_periods(client, app_mod):
    c = app_mod._db()
    pid, _ = _seed_person_chore(client, title="Dishes")
    period_id = fdb.add_away_period(c, pid, app_mod._today().isoformat())
    state = client.get("/api/admin/state").json()
    assert "away_periods" in state
    assert [p["id"] for p in state["away_periods"]] == [period_id]
    assert state["away_periods"][0]["person_id"] == pid


# --- admin away endpoints ----------------------------------------------------

def test_admin_away_create_close_delete(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
    p2 = client.post("/api/admin/people", json={"name": "Sam", "color": "#F05B5B"}).json()["id"]

    # started a few days back, so the default "I'm back" (end=yesterday) is a
    # valid end >= start (a same-day open + default back is now a 422, see
    # test_away_back_rejects_end_before_start).
    r = client.post("/api/admin/away",
                    json={"person_id": p1, "backup_person_id": p2,
                          "start_date": "2026-08-12"})
    assert r.status_code == 200
    body = r.json()
    pid = body["id"]
    assert body["person_id"] == p1
    assert body["backup_person_id"] == p2
    assert body["person_name"] == "Ben"
    assert body["backup_name"] == "Sam"
    assert body["start_date"] == "2026-08-12"
    assert body["end_date"] is None

    listed = client.get("/api/admin/away").json()["away_periods"]
    assert any(p["id"] == pid for p in listed)

    back = client.post(f"/api/admin/away/{pid}/back")
    assert back.status_code == 200
    listed = client.get("/api/admin/away").json()["away_periods"]
    row = next(p for p in listed if p["id"] == pid)
    assert row["end_date"] == "2026-08-16"  # yesterday of frozen _today

    d = client.delete(f"/api/admin/away/{pid}")
    assert d.status_code == 200
    listed = client.get("/api/admin/away").json()["away_periods"]
    assert not any(p["id"] == pid for p in listed)

    assert client.delete(f"/api/admin/away/{pid}").status_code == 404


def test_admin_away_patch_and_back_with_explicit_date(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
    pid = client.post("/api/admin/away", json={"person_id": p1}).json()["id"]

    r = client.patch(f"/api/admin/away/{pid}", json={"start_date": "2026-08-10"})
    assert r.status_code == 200
    row = next(p for p in client.get("/api/admin/away").json()["away_periods"]
               if p["id"] == pid)
    assert row["start_date"] == "2026-08-10"

    back = client.post(f"/api/admin/away/{pid}/back", json={"end_date": "2026-08-20"})
    assert back.status_code == 200
    row = next(p for p in client.get("/api/admin/away").json()["away_periods"]
               if p["id"] == pid)
    assert row["end_date"] == "2026-08-20"

    # An unknown id is a 404 no-op, not a reassuring 200 (S3: mirrors DELETE).
    assert client.patch("/api/admin/away/9999", json={"start_date": "2026-08-10"}).status_code == 404
    assert client.patch(f"/api/admin/away/{pid}", json={"start_date": "bad-date"}).status_code == 422


def test_admin_away_validation(client, app_mod):
    p1 = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]

    assert client.post("/api/admin/away", json={"person_id": 9999}).status_code == 404
    assert client.post("/api/admin/away",
                        json={"person_id": p1, "backup_person_id": p1}).status_code == 422
    assert client.post("/api/admin/away",
                        json={"person_id": p1, "backup_person_id": 9999}).status_code == 404


def test_admin_away_everyone_opens_for_all_active(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = client.post("/api/admin/people", json={"name": "Ben", "color": "#5BC9F0"}).json()["id"]
    p2 = client.post("/api/admin/people", json={"name": "Sam", "color": "#F05B5B"}).json()["id"]
    # deactivated person must be skipped entirely
    p3 = client.post("/api/admin/people", json={"name": "Nan", "color": "#5BFF5B"}).json()["id"]
    client.patch(f"/api/admin/people/{p3}", json={"active": 0})

    # p1 already has an open away period -> everyone must SKIP them, not double-open
    existing = client.post("/api/admin/away", json={"person_id": p1}).json()["id"]

    r = client.post("/api/admin/away/everyone", json={})
    assert r.status_code == 200
    created = r.json()["created"]
    assert len(created) == 1

    listed = client.get("/api/admin/away").json()["away_periods"]
    p1_periods = [p for p in listed if p["person_id"] == p1]
    assert len(p1_periods) == 1 and p1_periods[0]["id"] == existing
    p2_periods = [p for p in listed if p["person_id"] == p2]
    assert len(p2_periods) == 1
    assert p2_periods[0]["start_date"] == "2026-08-17"
    assert not any(p["person_id"] == p3 for p in listed)


def _make_person(client, name, color="#5BC9F0"):
    return client.post("/api/admin/people",
                       json={"name": name, "color": color}).json()["id"]


def _fixed_chore(client, owner, title="Dishes"):
    return client.post("/api/admin/chores", json={
        "title": title, "icon": "", "schedule_kind": "daily", "days_mask": 0,
        "assign_kind": "fixed", "fixed_person_id": owner,
        "rotation_order": []}).json()["id"]


def test_backup_covering_completion_credits_backup_not_away_person(
        client, app_mod, monkeypatch):
    """C1 (THE regression guard): a backup tapping a covering FIXED chore on the
    wall (client sends NO person_id) must be recorded under the BACKUP, not the
    away owner. Recording it under the away owner shows done today but never
    reaches the backup's per-person completion map, so the backup's streak
    silently breaks once the day ages."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    c = app_mod._db()
    A = _make_person(client, "Away", "#5BC9F0")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    # A goes away today with B as backup
    client.post("/api/admin/away", json={"person_id": A, "backup_person_id": B})

    # the wall resolves the covering chore onto B
    hub = client.get("/api/hub").json()
    bcard = next(p for p in hub["people"] if p["person"]["id"] == B)
    assert any(ch["id"] == cid and ch["covering_for"] == A for ch in bcard["chores"])

    # B taps it -- no person_id in the request, exactly like the wall sends
    assert client.post(f"/api/chores/{cid}/complete").json() == {"ok": True}

    comps = fdb.completions_between(c, today.isoformat(), today.isoformat())
    row = next(r for r in comps if r["chore_id"] == cid)
    assert row["person_id"] == B, \
        "covering completion must credit the backup, not the away owner"
    assert not any(r["person_id"] == A for r in comps), \
        "nothing may be credited to the away owner"

    # age the day: B's streak counts the covering day; A stays away, uncredited
    monkeypatch.setattr(app_mod, "_today", lambda: today + dt.timedelta(days=1))
    hub2 = client.get("/api/hub").json()
    b2 = next(p for p in hub2["people"] if p["person"]["id"] == B)
    a2 = next(p for p in hub2["people"] if p["person"]["id"] == A)
    assert b2["streak"] >= 1, "the backup's covering day counts toward THEIR streak"
    assert a2["away"] is True and a2["streak"] == 0, "away owner is uncredited"


def test_return_mid_day_keeps_the_owners_streak(client, app_mod, monkeypatch):
    """C1 (THE streak-destroyer): the ordinary "I'm back" flow.

    The backup covers and COMPLETES the owner's fixed chore in the morning
    (completion row keyed to the backup). The owner then taps "I'm back", which
    ends the period yesterday -- so today is theirs again and the next serve
    RE-FREEZES today's occurrence_log row onto the owner. The card keeps showing
    the tick (done flags are per chore, not per person), but the owner's streak
    input used to be keyed by completions.person_id, so today read "not all
    done" and the streak broke the moment the day aged.

    Ownership of a day belongs to the FROZEN LOG, not to whoever happened to tap
    the row: a completion means "that chore got done that day". Completions are
    never rewritten (the ledger still records who physically did it)."""
    today = dt.date(2026, 8, 17)
    seed_day = today - dt.timedelta(days=5)
    monkeypatch.setattr(app_mod, "_today", lambda: seed_day)   # T0: freeze first
    c = app_mod._db()
    A = _make_person(client, "Owner", "#5BC9F0")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")

    # A is away from two days ago (so the default "I'm back" -> end=yesterday is
    # a valid end >= start), B covers.
    period_id = fdb.add_away_period(
        c, A, (today - dt.timedelta(days=2)).isoformat(), None, B)

    monkeypatch.setattr(app_mod, "_today", lambda: today)
    hub = client.get("/api/hub").json()                 # freezes today onto B
    bcard = next(p for p in hub["people"] if p["person"]["id"] == B)
    assert any(ch["id"] == cid and ch["covering_for"] == A for ch in bcard["chores"])
    assert client.post(f"/api/chores/{cid}/complete").json() == {"ok": True}

    # ... and mid-morning A walks in the door and taps "I'm back".
    assert client.post(f"/api/admin/away/{period_id}/back").status_code == 200
    hub = client.get("/api/hub").json()                 # re-freeze: today is A's
    acard = next(p for p in hub["people"] if p["person"]["id"] == A)
    assert acard["away"] is False
    assert [ch["done"] for ch in acard["chores"] if ch["id"] == cid] == [True], \
        "the card draws the tick -- the streak must agree with it"
    assert acard["streak"] == 1, \
        "today is the owner's and it is fully done: the streak counts it"
    assert acard["week"][-1] == "done"

    # R1: the completion row itself is NOT rewritten -- B really did the chore.
    row = next(r for r in fdb.completions_between(c, today.isoformat(),
                                                  today.isoformat())
               if r["chore_id"] == cid)
    assert row["person_id"] == B

    # age the day: the frozen record still reads "the owner's day, fully done".
    monkeypatch.setattr(app_mod, "_today", lambda: today + dt.timedelta(days=1))
    hub2 = client.get("/api/hub").json()
    a2 = next(p for p in hub2["people"] if p["person"]["id"] == A)
    b2 = next(p for p in hub2["people"] if p["person"]["id"] == B)
    assert a2["streak"] == 1, "aging must not break the returning person's streak"
    assert a2["week"][-2] == "done"
    assert b2["week"][-2] == "rest", \
        "the backup no longer owns that day, so it is rest for them, not a miss"


def test_open_period_mid_day_keeps_the_backups_day_whole(
        client, app_mod, monkeypatch):
    """C1, mirror image: the owner completes their chore in the morning, THEN
    goes away with a backup. The re-freeze moves today's row onto the backup, so
    the backup's day must read fully done from that same completion -- otherwise
    opening a period mid-day silently breaks the COVERING person's streak."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)      # T0: freeze first
    c = app_mod._db()
    A = _make_person(client, "Owner", "#5BC9F0")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")

    client.get("/api/hub")                                  # freeze today onto A
    assert client.post(f"/api/chores/{cid}/complete").json() == {"ok": True}
    comp = next(r for r in fdb.completions_between(c, today.isoformat(),
                                                   today.isoformat())
                if r["chore_id"] == cid)
    assert comp["person_id"] == A

    # A leaves at lunchtime; B picks the chore up.
    assert client.post("/api/admin/away",
                       json={"person_id": A,
                             "backup_person_id": B}).status_code == 200
    hub = client.get("/api/hub").json()                     # re-freeze onto B
    bcard = next(p for p in hub["people"] if p["person"]["id"] == B)
    acard = next(p for p in hub["people"] if p["person"]["id"] == A)
    assert any(ch["id"] == cid and ch["covering_for"] == A and ch["done"]
               for ch in bcard["chores"])
    assert bcard["streak"] == 1, "the backup owns today now, and it is done"
    assert bcard["week"][-1] == "done"
    assert acard["away"] is True and acard["week"][-1] == "away"

    # and it survives aging
    monkeypatch.setattr(app_mod, "_today", lambda: today + dt.timedelta(days=1))
    b2 = next(p for p in client.get("/api/hub").json()["people"]
              if p["person"]["id"] == B)
    assert b2["week"][-2] == "done" and b2["streak"] == 1


def test_away_back_same_day_cancels_the_period(client, app_mod, monkeypatch):
    """'Going away' (start=today) then 'I'm back' the same day used to 422
    every time: the default end (yesterday) falls before the start. The period
    never took effect, so the plain tap removes it and succeeds. The wall
    treats the person as present again straight away."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    pid = client.post("/api/admin/away", json={"person_id": p1}).json()["id"]
    r = client.post(f"/api/admin/away/{pid}/back")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert fdb.get_away_period(app_mod._db(), pid) is None
    assert client.get("/api/admin/away").json()["away_periods"] == []
    hub = client.get("/api/hub").json()
    assert next(p for p in hub["people"]
                if p["person"]["id"] == p1)["away"] is False


def test_away_back_on_a_planned_future_trip_still_refuses(client, app_mod,
                                                         monkeypatch):
    """Only a period that started TODAY is cancelled by a plain tap. A trip
    planned for later must not be deleted by one tap on the wrong button."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    pid = client.post("/api/admin/away", json={
        "person_id": p1, "start_date": "2026-08-20"}).json()["id"]
    assert client.post(f"/api/admin/away/{pid}/back").status_code == 422
    assert fdb.get_away_period(app_mod._db(), pid) is not None


def test_away_back_same_day_explicit_end_still_closes(client, app_mod,
                                                      monkeypatch):
    """An explicit end_date keeps the old meaning: close the period on that
    day (here: away for today only), and an end before the start is a 422."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    pid = client.post("/api/admin/away", json={"person_id": p1}).json()["id"]
    assert client.post(f"/api/admin/away/{pid}/back",
                       json={"end_date": "2026-08-16"}).status_code == 422
    assert client.post(f"/api/admin/away/{pid}/back",
                       json={"end_date": "2026-08-17"}).status_code == 200
    assert fdb.get_away_period(app_mod._db(), pid)["end_date"] == "2026-08-17"


def test_away_patch_rejects_end_before_start(client, app_mod, monkeypatch):
    """S2: PATCH end_date earlier than the row's start_date is 422."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    pid = client.post("/api/admin/away",
                      json={"person_id": p1, "start_date": "2026-08-15"}).json()["id"]
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"end_date": "2026-08-10"}).status_code == 422
    # moving start in the SAME patch is respected for the comparison
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"start_date": "2026-08-05",
                              "end_date": "2026-08-10"}).status_code == 200


def test_away_patch_rejects_start_after_stored_end(client, app_mod, monkeypatch):
    """Mirror of the end<start guard: PATCHing start_date LATER than the row's
    already-stored end_date, without re-supplying end_date, must 422 -- not
    silently void the whole period via away_map's a>b skip."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    pid = client.post("/api/admin/away",
                      json={"person_id": p1, "start_date": "2026-08-10"}).json()["id"]
    # close the period so it has a stored end_date
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"end_date": "2026-08-12"}).status_code == 200

    # moving start past the stored end, without touching end_date, must 422
    r = client.patch(f"/api/admin/away/{pid}", json={"start_date": "2026-08-20"})
    assert r.status_code == 422

    # a start move that stays within the stored end still works
    ok = client.patch(f"/api/admin/away/{pid}", json={"start_date": "2026-08-11"})
    assert ok.status_code == 200
    row = next(p for p in client.get("/api/admin/away").json()["away_periods"]
               if p["id"] == pid)
    assert row["start_date"] == "2026-08-11"
    assert row["end_date"] == "2026-08-12"


def test_away_patch_and_back_unknown_id_404(client, app_mod, monkeypatch):
    """S3: PATCH/back on an unknown id is a 404, mirroring DELETE."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    assert client.patch("/api/admin/away/9999",
                        json={"start_date": "2026-08-10"}).status_code == 404
    assert client.post("/api/admin/away/9999/back").status_code == 404


def test_away_patch_backup_validation(client, app_mod, monkeypatch):
    """S4: PATCH backup_person_id is validated like open -- 422 on self,
    404 on unknown, 200 on a real other person."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    p2 = _make_person(client, "Sam", "#F05B5B")
    pid = client.post("/api/admin/away", json={"person_id": p1}).json()["id"]
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": p1}).status_code == 422
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": 9999}).status_code == 404
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": p2}).status_code == 200


def test_away_open_twice_conflicts(client, app_mod, monkeypatch):
    """F1b: a second open period for a person 409s, so overlapping rows never
    reach the wall."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    p1 = _make_person(client, "Ben")
    assert client.post("/api/admin/away", json={"person_id": p1}).status_code == 200
    assert client.post("/api/admin/away", json={"person_id": p1}).status_code == 409
    # after they're back, opening a fresh period is allowed again
    pid = client.get("/api/admin/away").json()["away_periods"][0]["id"]
    client.post(f"/api/admin/away/{pid}/back", json={"end_date": "2026-08-17"})
    assert client.post("/api/admin/away", json={"person_id": p1}).status_code == 200


def test_future_dated_away_period_not_active_today(client, app_mod, monkeypatch):
    """F2: a period that starts AFTER today leaves the person present today with
    their normal chores rendering."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    A = _make_person(client, "Away")
    cid = _fixed_chore(client, A, "Dishes")
    client.post("/api/admin/away",
                json={"person_id": A, "start_date": "2026-08-25"})
    card = next(p for p in client.get("/api/hub").json()["people"]
                if p["person"]["id"] == A)
    assert card["away"] is False
    assert any(ch["id"] == cid for ch in card["chores"]), \
        "normal chore renders while the away period is still in the future"


def test_backup_deleted_pauses_covering_chore_end_to_end(
        client, app_mod, monkeypatch):
    """F3: deleting the backup while referenced pauses the away owner's fixed
    chore (no stale covering row to a deleted id), no crash."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    A = _make_person(client, "Away")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    client.post("/api/admin/away", json={"person_id": A, "backup_person_id": B})
    client.delete(f"/api/admin/people/{B}")
    hub = client.get("/api/hub").json()
    a_card = next(p for p in hub["people"] if p["person"]["id"] == A)
    assert a_card["away"] is True
    # chore paused: it appears on nobody's card
    assert all(not any(ch["id"] == cid for ch in p["chores"])
               for p in hub["people"])


def test_frozen_past_day_keeps_the_covering_for_explanation(client, app_mod,
                                                            monkeypatch):
    """I9 end-to-end: the wall freezes a covered day, the away period then ENDS
    (and is even deleted), and the day browser must still explain why the
    backup had that chore — 'covering for <the away person>'. Before this the
    tag was derived at render time and vanished with the period."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)      # T0: freeze first
    c = app_mod._db()
    A = _make_person(client, "Away")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    period = client.post("/api/admin/away",
                         json={"person_id": A,
                               "backup_person_id": B}).json()["id"]
    client.get("/api/hub")                        # freeze today with B covering
    assert fdb.day_log(c, today.isoformat())[0]["covering_for"] == A

    # the trip ends and the record of it is deleted outright
    client.delete(f"/api/admin/away/{period}")
    monkeypatch.setattr(app_mod, "_today", lambda: today + dt.timedelta(days=1))
    day = client.get(f"/api/chores/day?date={today.isoformat()}").json()
    bcard = next(p for p in day["people"] if p["person"]["id"] == B)
    row = next(ch for ch in bcard["chores"] if ch["id"] == cid)
    assert row["covering_for"] == A, \
        "the frozen day still says who the backup was standing in for"


def test_backup_that_goes_inactive_later_pauses_the_covering_chore(
        client, app_mod, monkeypatch):
    """F7 + I6: an unavailable backup is now rejected UP FRONT (see
    test_backup_picker_rejects_inactive_and_away_people), but a backup who goes
    inactive AFTER the period opened is still handled at resolve time — the
    covering chore pauses instead of landing on someone who isn't there."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    A = _make_person(client, "Away")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    assert client.post("/api/admin/away",
                       json={"person_id": A,
                             "backup_person_id": B}).status_code == 200
    client.patch(f"/api/admin/people/{B}", json={"active": 0})   # inactive LATER
    hub = client.get("/api/hub").json()
    assert all(not any(ch["id"] == cid for ch in p["chores"])
               for p in hub["people"]), "inactive backup -> chore pauses"


def test_deactivating_an_away_person_stops_parking_chores_on_the_backup(
        client, app_mod, monkeypatch):
    """I5 end-to-end: a person is marked away with a backup, then deactivated
    (they moved out, the account was retired) while the period is still open.
    Their chores must stop, not sit on the backup's card forever."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)      # T0: freeze first
    A = _make_person(client, "Away")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    client.post("/api/admin/away", json={"person_id": A, "backup_person_id": B})
    bcard = next(p for p in client.get("/api/hub").json()["people"]
                 if p["person"]["id"] == B)
    assert any(ch["id"] == cid for ch in bcard["chores"])      # covered today

    client.patch(f"/api/admin/people/{A}", json={"active": 0})
    hub = client.get("/api/hub").json()
    assert all(not any(ch["id"] == cid for ch in p["chores"])
               for p in hub["people"]), \
        "an inactive owner's chore drops; it is not parked on the backup"
    assert fdb.day_log(app_mod._db(), today.isoformat()) == []


def test_backup_picker_rejects_inactive_and_away_people(client, app_mod,
                                                        monkeypatch):
    """I6: choosing a backup who is inactive, or away themselves, made every
    covered chore vanish at resolve time with no explanation. Both are 422 at
    the source, on open AND on patch."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    A = _make_person(client, "Away")
    inactive = _make_person(client, "Gone", "#F05B5B")
    traveller = _make_person(client, "Also away", "#5BFF5B")
    ok_backup = _make_person(client, "Home", "#C39BEA")
    client.patch(f"/api/admin/people/{inactive}", json={"active": 0})
    client.post("/api/admin/away", json={"person_id": traveller})

    assert client.post("/api/admin/away",
                       json={"person_id": A,
                             "backup_person_id": inactive}).status_code == 422
    assert client.post("/api/admin/away",
                       json={"person_id": A,
                             "backup_person_id": traveller}).status_code == 422
    r = client.post("/api/admin/away",
                    json={"person_id": A, "backup_person_id": ok_backup})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": inactive}).status_code == 422
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": traveller}).status_code == 422
    # clearing the backup, and re-picking an available one, still work
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": None}).status_code == 200
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"backup_person_id": ok_backup}).status_code == 200


def test_away_patch_rejects_null_start_date(client, app_mod, monkeypatch):
    """I7: an explicit JSON null for start_date (a NOT NULL column) was a 500
    from the DB write. It's a bad request — 422, like every sibling patch."""
    monkeypatch.setattr(app_mod, "_today", lambda: dt.date(2026, 8, 17))
    A = _make_person(client, "Away")
    pid = client.post("/api/admin/away", json={"person_id": A}).json()["id"]
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"start_date": None}).status_code == 422
    # the period is untouched, and the nullable fields still accept null
    row = next(p for p in client.get("/api/admin/away").json()["away_periods"]
               if p["id"] == pid)
    assert row["start_date"] == "2026-08-17"
    assert client.patch(f"/api/admin/away/{pid}",
                        json={"end_date": None}).status_code == 200


def test_complete_refuses_to_guess_when_the_away_overlay_is_broken(
        client, app_mod, monkeypatch):
    """I2: reading the wall may degrade when the away overlay build fails (it
    shows a note); WRITING a completion may not — crediting the away owner
    would break the covering person's streak. No person_id + a broken overlay
    is a 503 the tap can retry; an explicit person_id is still honored."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    A = _make_person(client, "Away")
    B = _make_person(client, "Backup", "#F05B5B")
    cid = _fixed_chore(client, A, "Dishes")
    client.post("/api/admin/away", json={"person_id": A, "backup_person_id": B})

    def boom(*a, **k):
        raise RuntimeError("simulated away_map failure")
    monkeypatch.setattr(app_mod.fdb, "away_map", boom)

    r = client.post(f"/api/chores/{cid}/complete")
    assert r.status_code == 503
    assert "away status unavailable" in r.json()["detail"]
    assert fdb.completions_between(app_mod._db(), today.isoformat(),
                                   today.isoformat()) == []
    ok = client.post(f"/api/chores/{cid}/complete", json={"person_id": B})
    assert ok.status_code == 200


def test_complete_422s_when_the_chore_has_no_resolvable_assignee(client, app_mod,
                                                                 monkeypatch):
    """T11: the away owner has no available backup, so the chore is PAUSED for
    the day and appears on nobody's card. A completion for it (no person_id)
    must 422 rather than fall back to the away owner and credit a day the wall
    never asked them for."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    A = _make_person(client, "Away")
    cid = _fixed_chore(client, A, "Dishes")
    client.post("/api/admin/away", json={"person_id": A})      # no backup
    hub = client.get("/api/hub").json()
    assert all(not any(ch["id"] == cid for ch in p["chores"])
               for p in hub["people"]), "the chore is paused for the day"

    r = client.post(f"/api/chores/{cid}/complete")
    assert r.status_code == 422
    assert r.json()["detail"] == "no resolvable assignee"
    assert fdb.completions_between(app_mod._db(), today.isoformat(),
                                   today.isoformat()) == []


def test_chores_day_carries_away_ok(client, app_mod, monkeypatch):
    """M4/I10: the day browser reads the same degraded-state flag the wall
    does, so a failed overlay build shows a note instead of presenting an away
    person as present with a full chore list."""
    today = dt.date(2026, 8, 17)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    _seed_person_chore(client, title="Dishes")
    day = f"/api/chores/day?date={today.isoformat()}"
    assert client.get(day).json()["away_ok"] is True

    def boom(*a, **k):
        raise RuntimeError("simulated away_map failure")
    monkeypatch.setattr(app_mod.fdb, "away_map", boom)
    body = client.get(day).json()
    assert body["away_ok"] is False and body["people"]


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


def test_open_sync_conn_retries_and_flags_failure(app_mod, monkeypatch):
    """Regression for issue #32: a transient ensure_schema failure at sync
    startup must not kill the thread; _open_sync_conn retries until it succeeds
    AND records an unhealthy calendar_status so the wall shows staleness instead
    of a false-healthy badge."""
    # Pre-create the schema so the failure path can write calendar_status (the kv
    # table has to exist for the status write to land, not be swallowed).
    seed = app_mod.fdb.connect(app_mod.DB_PATH)
    app_mod.fdb.ensure_schema(seed)
    seed.close()
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
    # the failure surfaced, not a false-healthy badge
    status = app_mod.fdb.kv_get(conn, "calendar_status")
    assert status["ok"] is False and "sync startup" in status["error"]


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
    pid = fdb.add_person(conn, "Ben", "#5BC9F0")
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

    def flaky(chores, people, d, away=None):
        if d == boom_day and not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated crash mid-backfill")
        return real_plan_rows(chores, people, d, away)
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
    # The ceiling must admit the frontend's own fixed fetch, or the wall's
    # calendar 422s on every load (test_static guards the two staying in step).
    assert client.get("/api/calendar?days=400&past=45").status_code == 200
    assert client.get("/api/calendar?days=401").status_code == 422
    # `past` shares the same ceiling (the constant is documented as covering both
    # directions); pin its bound too rather than leaving it untested.
    assert client.get("/api/calendar?past=400").status_code == 200
    assert client.get("/api/calendar?past=401").status_code == 422


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


def test_integrations_list_toggle_and_hub_block(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {
        "weather_base": "http://w", "go2rtc_base": "http://g",
        "cameras": [{"src": "cam1", "label": "Front"}],
        "calendars": [{"id": "fam", "kind": "google", "label": "Fam"}],
    })
    with TestClient(appmod.app) as c:
        ids = {i["id"]: i for i in c.get("/api/integrations").json()["integrations"]}
        # available ones present, all enabled by default (non-breaking seed)
        assert ids["weather"]["enabled"] is True
        assert ids["cameras"]["enabled"] is True
        assert ids["google_calendar"]["enabled"] is True
        assert "climate" not in ids            # not configured
        assert "icloud_caldav" not in ids      # no creds
        # /api/hub carries the same block
        hub = c.get("/api/hub").json()
        assert {i["id"] for i in hub["integrations"]} == set(ids)
        assert hub["links"]["cameras"][0]["label"] == "Front"
        # toggle cameras off -> camera links blanked, flag flips
        assert c.patch("/api/integrations/cameras", json={"enabled": False}).status_code == 200
        hub2 = c.get("/api/hub").json()
        assert hub2["links"]["cameras"] == []
        assert next(i for i in hub2["integrations"] if i["id"] == "cameras")["enabled"] is False
        # unknown integration -> 404
        assert c.patch("/api/integrations/nope", json={"enabled": False}).status_code == 404


def test_chores_todos_toggle_via_integrations_and_hub_block(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {})  # nothing else configured
    with TestClient(appmod.app) as c:
        ids = {i["id"]: i for i in c.get("/api/integrations").json()["integrations"]}
        # always present, seeded enabled, tagged as features
        assert ids["chores"]["enabled"] is True
        assert ids["todos"]["enabled"] is True
        assert ids["chores"]["group"] == "feature"
        assert ids["todos"]["group"] == "feature"
        # /api/hub carries the same entries incl. group
        hub = c.get("/api/hub").json()
        hids = {i["id"]: i for i in hub["integrations"]}
        assert hids["chores"]["group"] == "feature"
        # toggle chores off -> flag flips (data still served; UI hides it)
        assert c.patch("/api/integrations/chores", json={"enabled": False}).status_code == 200
        hub2 = c.get("/api/hub").json()
        assert next(i for i in hub2["integrations"] if i["id"] == "chores")["enabled"] is False
        # round-trip: toggle it back on -> /api/hub reports it enabled again
        assert c.patch("/api/integrations/chores", json={"enabled": True}).status_code == 200
        hub3 = c.get("/api/hub").json()
        assert next(i for i in hub3["integrations"] if i["id"] == "chores")["enabled"] is True


def test_disabling_calendar_integration_hides_its_events(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {
        "calendars": [{"id": "fam", "kind": "google", "label": "Fam"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        today = appmod._today()
        soon = (today + dt.timedelta(days=3)).isoformat()
        appmod.fdb.replace_events(c, [{"id": "e1", "calendar_id": "fam",
            "title": "Dentist", "start_ts": f"{soon}T10:00:00-07:00",
            "end_ts": f"{soon}T11:00:00-07:00", "all_day": 0}])
        assert any(e["title"] == "Dentist"
                   for e in tc.get("/api/calendar").json()["events"])
        # disable the Google Calendar integration -> its events are hidden (cache kept)
        tc.patch("/api/integrations/google_calendar", json={"enabled": False})
        assert tc.get("/api/calendar").json()["events"] == []
        # re-enable -> visible again immediately, no re-sync
        tc.patch("/api/integrations/google_calendar", json={"enabled": True})
        assert any(e["title"] == "Dentist"
                   for e in tc.get("/api/calendar").json()["events"])


def test_calendar_renders_caldav_events_with_color_and_gating(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})   # no google/ics calendars
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=3)).isoformat()
        appmod.fdb.replace_events_caldav(c, [{"id": "u1", "calendar_id": "caldav:abc",
            "title": "Dentist", "start_ts": f"{soon}T10:00:00",
            "end_ts": f"{soon}T11:00:00", "all_day": 0}])
        appmod.fdb.upsert_caldav_collection(c, "caldav:abc", "VEVENT", "Family",
                                            "#FF0000", "2026-08-17T00:00:00")
        ev = next(e for e in tc.get("/api/calendar").json()["events"]
                  if e["title"] == "Dentist")
        assert ev["color"] == "#FF0000" and ev["label"] == "Family"
        # iCloud CalDAV is available (creds set) -> a toggleable integration
        ids = {i["id"] for i in tc.get("/api/integrations").json()["integrations"]}
        assert "icloud_caldav" in ids
        # disabling it hides the CalDAV events (cache kept)
        tc.patch("/api/integrations/icloud_caldav", json={"enabled": False})
        assert all(e["title"] != "Dentist"
                   for e in tc.get("/api/calendar").json()["events"])


def _seed_reminder_object(appmod, c, list_id, uid, title, due=None, completed=False,
                          list_name="List", seed_collection=True):
    """Seed one iCloud reminder as a cal_objects VTODO row — the render source of
    truth — (and, by default, its collection). `due` is a date or None."""
    from family_hub import reminders as remlogic
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    ics = remlogic.build_vtodo(uid, title, now, due=due)
    if completed:
        ics = remlogic.set_completed(ics, True, now)
    if seed_collection:
        appmod.fdb.upsert_caldav_collection(c, list_id, "VTODO", list_name, None, "t")
    appmod.fdb.upsert_cal_object_synced(c, {
        "id": f"{list_id}/{uid}", "collection_id": list_id, "comp_type": "VTODO",
        "uid": uid, "href": f"h/{uid}", "etag": "e", "summary": title,
        "raw_ics": ics, "sequence": 0, "last_modified": None})


def test_reminders_api_and_hub_block(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        today = appmod._today()
        _seed_reminder_object(appmod, c, "caldav:x", "r1", "Buy milk",
                              due=today + dt.timedelta(days=2))
        _seed_reminder_object(appmod, c, "caldav:x", "r2", "Old thing",
                              due=today - dt.timedelta(days=1))
        _seed_reminder_object(appmod, c, "caldav:x", "r3", "Done",
                              due=today, completed=True)
        data = tc.get("/api/reminders").json()
        assert data["configured"] is True
        assert [r["title"] for r in data["buckets"]["upcoming"]] == ["Buy milk"]
        assert [r["title"] for r in data["buckets"]["overdue"]] == ["Old thing"]
        # completed never appears
        assert all("Done" not in [r["title"] for r in data["buckets"][b]]
                   for b in ["overdue", "today", "upcoming", "no_date"])
        # hub carries the grouped block
        assert [r["title"] for r in tc.get("/api/hub").json()["reminders"]["upcoming"]] \
            == ["Buy milk"]
        # disabling iCloud CalDAV empties reminders everywhere
        tc.patch("/api/integrations/icloud_caldav", json={"enabled": False})
        assert tc.get("/api/reminders").json()["buckets"]["upcoming"] == []
        assert tc.get("/api/hub").json()["reminders"]["upcoming"] == []


def test_reminders_timed_due_buckets_by_the_hubs_local_day(tmp_path,
                                                           monkeypatch):
    """A reminder due at 11:30pm tonight is stored as 06:30Z tomorrow. Both the
    hub block and the full list must file it under today, in the hub's zone."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        today = appmod._today()
        tonight = dt.datetime(today.year, today.month, today.day, 23, 30,
                              tzinfo=appmod.TZ).astimezone(dt.timezone.utc)
        assert tonight.date() != today      # the UTC form names tomorrow
        _seed_reminder_object(appmod, c, "caldav:x", "r1", "Bins out",
                              due=tonight)
        full = tc.get("/api/reminders").json()["buckets"]
        assert [r["title"] for r in full["today"]] == ["Bins out"]
        assert full["upcoming"] == []
        assert full["today"][0]["due"][:10] == today.isoformat()
        hub = tc.get("/api/hub").json()["reminders"]
        assert [r["title"] for r in hub["today"]] == ["Bins out"]


def test_reminders_api_not_configured_without_creds(tmp_path, monkeypatch):
    monkeypatch.delenv("ICLOUD_CALDAV_USER", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_APP_PASSWORD", raising=False)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        data = tc.get("/api/reminders").json()
        assert data["configured"] is False and data["buckets"]["today"] == []


def test_integration_status_surfaces_needs_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        appmod.fdb.kv_set(appmod._db(), "caldav_status",
                          {"ok": False, "needs_auth": True, "error": "401"})
        integ = {i["id"]: i for i in tc.get("/api/integrations").json()["integrations"]}
        assert integ["icloud_caldav"]["status"] == "needs_auth"


def test_calendar_status_agg_surfaces_needs_auth_even_when_a_source_is_ok(tmp_path, monkeypatch):
    # Mixed setup: Google/ICS healthy, iCloud app password revoked. The aggregate
    # is still ok (Google renders), but needs_auth must survive so the wall shows
    # the reconnect banner — otherwise the iCloud half silently drifts stale
    # behind a "connected" wall and nobody ever reconnects it.
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": True})
        appmod.fdb.kv_set(c, "caldav_status", {"ok": False, "needs_auth": True})
        status = tc.get("/api/calendar").json()["status"]
        assert status["ok"] is True
        assert status.get("needs_auth") is True


def test_event_on_two_calendars_renders_once(tmp_path, monkeypatch):
    # Composite events PK (issue #30) stores an event shared by two calendars as
    # two rows with the same id; the wall must render it ONCE, not twice.
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "cal_a", "kind": "google", "label": "A", "color": "#f00"},
        {"id": "cal_b", "kind": "google", "label": "B", "color": "#00f"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=2)).isoformat()
        row = {"id": "shared1", "title": "Dinner",
               "start_ts": f"{soon}T18:00:00", "end_ts": f"{soon}T19:00:00",
               "all_day": 0}
        appmod.fdb.replace_events(c, [{**row, "calendar_id": "cal_a"},
                                      {**row, "calendar_id": "cal_b"}])
        events = tc.get("/api/calendar").json()["events"]
        dinners = [e for e in events if e["title"] == "Dinner"]
        assert len(dinners) == 1
        # the first visible copy wins its calendar's color
        assert dinners[0]["color"] == "#f00"


def test_dedup_a_hidden_first_copy_does_not_claim_the_row(tmp_path, monkeypatch):
    # The dedup runs AFTER the per-calendar visibility gates, so a copy on a
    # HIDDEN calendar must neither win the row nor block the visible copy — the
    # event still renders once, in the VISIBLE calendar's color. (If the dedup
    # were hoisted above the gates, the hidden copy would claim the key and the
    # event would vanish entirely.)
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "hol", "kind": "ics", "label": "Holidays", "url": "http://x/h.ics",
         "color": "#111"},
        {"id": "gcal", "kind": "google", "label": "Family", "color": "#0f0"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=2)).isoformat()
        row = {"id": "shared1", "title": "Parade",
               "start_ts": f"{soon}T09:00:00", "end_ts": f"{soon}T10:00:00",
               "all_day": 0}
        # insert the ICS (soon-to-be-hidden) copy FIRST so it sorts ahead on the
        # tie, then the visible google copy
        appmod.fdb.replace_events(c, [{**row, "calendar_id": "hol"},
                                      {**row, "calendar_id": "gcal"}])
        tc.patch("/api/integrations/ics_calendar", json={"enabled": False})
        events = tc.get("/api/calendar").json()["events"]
        parades = [e for e in events if e["title"] == "Parade"]
        assert len(parades) == 1                 # still rendered once, not dropped
        assert parades[0]["color"] == "#0f0"     # the VISIBLE (google) copy won


def test_calendar_status_agg_surfaces_a_sustained_degraded_source(tmp_path, monkeypatch):
    # Google ok + iCloud stuck on a persistent NON-auth error (already past the
    # sustained threshold). The aggregate stays ok (Google renders) but carries
    # degraded so the wall warns the family that calendar may be behind.
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": True})
        appmod.fdb.kv_set(c, "caldav_status",
                          {"ok": False, "error": "connection reset", "sustained": True})
        status = tc.get("/api/calendar").json()["status"]
        assert status["ok"] is True
        assert status.get("degraded") is True
        assert not status.get("needs_auth")


def test_calendar_status_agg_ignores_a_disabled_sustained_source(tmp_path, monkeypatch):
    # A stale sustained flag on a DISABLED iCloud must not nag the family about a
    # calendar they turned off — disabling drops it from the aggregated sources.
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": True})
        appmod.fdb.kv_set(c, "caldav_status",
                          {"ok": False, "error": "connection reset", "sustained": True})
        tc.patch("/api/integrations/icloud_caldav", json={"enabled": False})
        status = tc.get("/api/calendar").json()["status"]
        assert status["ok"] is True
        assert not status.get("degraded")


def test_calendar_status_agg_needs_auth_wins_over_degraded(tmp_path, monkeypatch):
    # If a source both needs auth AND is sustained, reconnect is the louder,
    # actionable signal — the banner must not downgrade it to a generic "trouble".
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": True})
        appmod.fdb.kv_set(c, "caldav_status",
                          {"ok": False, "needs_auth": True, "sustained": True})
        status = tc.get("/api/calendar").json()["status"]
        assert status.get("needs_auth") is True
        assert not status.get("degraded")


def test_calendar_status_agg_healthy_setup_has_no_reconnect_flag(tmp_path, monkeypatch):
    # A fully-healthy mixed setup must NOT carry needs_auth, or the wall would
    # show a spurious "sign-in expired" banner on a perfectly connected calendar.
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": True})
        appmod.fdb.kv_set(c, "caldav_status", {"ok": True})
        status = tc.get("/api/calendar").json()["status"]
        assert status["ok"] is True
        assert not status.get("needs_auth")
        assert not status.get("degraded")


def test_disabling_ics_calendar_hides_its_events(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "holidays", "kind": "ics", "label": "Holidays", "url": "http://x/h.ics"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=2)).isoformat()
        appmod.fdb.replace_events(c, [{"id": "e1", "calendar_id": "holidays",
            "title": "Holiday", "start_ts": f"{soon}T00:00:00",
            "end_ts": f"{soon}T23:59:00", "all_day": 0}])
        assert any(e["title"] == "Holiday"
                   for e in tc.get("/api/calendar").json()["events"])
        tc.patch("/api/integrations/ics_calendar", json={"enabled": False})
        assert all(e["title"] != "Holiday"
                   for e in tc.get("/api/calendar").json()["events"])


def test_integration_status_error_and_ics_not_shared(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "g", "kind": "google", "label": "G"},
        {"id": "i", "kind": "ics", "label": "I", "url": "http://x"}]})
    with TestClient(appmod.app) as tc:
        appmod.fdb.kv_set(appmod._db(), "calendar_status", {"ok": False, "error": "boom"})
        integ = {i["id"]: i for i in tc.get("/api/integrations").json()["integrations"]}
        assert integ["google_calendar"]["status"] == "error"
        assert integ["ics_calendar"]["status"] is None   # ICS doesn't inherit it


def test_caldav_events_hidden_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("ICLOUD_CALDAV_USER", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_APP_PASSWORD", raising=False)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=2)).isoformat()
        appmod.fdb.replace_events_caldav(c, [{"id": "u1", "calendar_id": "caldav:x",
            "title": "Stale", "start_ts": f"{soon}T10:00:00",
            "end_ts": f"{soon}T11:00:00", "all_day": 0}])
        # no credentials -> cached CalDAV events are hidden, not shown stale
        assert all(e["title"] != "Stale"
                   for e in tc.get("/api/calendar").json()["events"])


def test_caldav_credentials_endpoints_never_leak_the_password(tmp_path, monkeypatch):
    monkeypatch.delenv("ICLOUD_CALDAV_USER", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_APP_PASSWORD", raising=False)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        ids = lambda: {i["id"] for i in tc.get("/api/integrations").json()["integrations"]}
        assert "icloud_caldav" not in ids()                       # not configured yet
        r = tc.post("/api/integrations/icloud_caldav/credentials",
                    json={"user": "bot@icloud.com", "app_password": "abcd-efgh"})
        assert r.status_code == 200 and r.json()["user"] == "bot@icloud.com"
        assert "abcd-efgh" not in r.text                          # password NEVER returned
        # now available, account shown, password still never exposed
        integ = {i["id"]: i for i in tc.get("/api/integrations").json()["integrations"]}
        assert integ["icloud_caldav"]["account"] == "bot@icloud.com"
        assert "abcd-efgh" not in tc.get("/api/integrations").text
        assert tc.post("/api/integrations/icloud_caldav/credentials",
                       json={"user": "x", "app_password": ""}).status_code == 422
        assert tc.delete("/api/integrations/icloud_caldav/credentials").json()["ok"] is True
        assert "icloud_caldav" not in ids()                       # disconnected


def test_caldav_settings_entry_carries_pending_and_parked_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "abcd-efgh")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        entry = lambda: {i["id"]: i for i in
                         tc.get("/api/integrations").json()["integrations"]}["icloud_caldav"]
        assert entry()["parked"] == 0                  # absent status reads as none
        appmod.fdb.kv_set(appmod._db(), "caldav_status",
                          {"ok": False, "pending": 1, "parked": 2})
        e = entry()
        assert (e["pending"], e["parked"]) == (1, 2)


def test_caldav_settings_entry_names_people_whose_list_is_gone(tmp_path, monkeypatch):
    """A person mapped to a Reminders list the sync dropped is named on the
    iCloud settings entry, read live, so picking a new list clears it."""
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "abcd-efgh")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        entry = lambda: {i["id"]: i for i in
                         tc.get("/api/integrations").json()["integrations"]}["icloud_caldav"]
        c = appmod._db()
        assert entry()["lists_gone"] == []
        pid = appmod.fdb.add_person(c, "Sam", "#5BC9F0")
        appmod.fdb.upsert_caldav_collection(c, "caldav:sam", "VTODO", "Sam", None, "t")
        appmod.fdb.upsert_caldav_collection(c, "caldav:new", "VTODO", "New", None, "t")
        appmod.fdb.update_person(c, pid, reminder_list_id="caldav:sam")
        assert entry()["lists_gone"] == []
        appmod.fdb.drop_caldav_collection(c, "caldav:sam")
        assert entry()["lists_gone"] == ["Sam"]
        appmod.fdb.update_person(c, pid, reminder_list_id="caldav:new")
        assert entry()["lists_gone"] == []


def test_admin_state_flags_each_person_whose_list_is_gone(tmp_path, monkeypatch):
    """/api/admin/state says per person whether their mapped list is gone, so
    the wall does not have to guess from the list array. With every list gone
    (an empty array) the person still reads as gone, not as unconnected. An
    inactive person is flagged too; an unmapped one never is."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        flags = lambda: {p["name"]: p["list_gone"] for p in
                         tc.get("/api/admin/state").json()["people"]}
        sam = appmod.fdb.add_person(c, "Sam", "#5BC9F0")
        ada = appmod.fdb.add_person(c, "Ada", "#8AE0AD")
        appmod.fdb.add_person(c, "Bee", "#F0A05B")                 # unmapped
        appmod.fdb.upsert_caldav_collection(c, "caldav:sam", "VTODO", "Sam", None, "t")
        appmod.fdb.upsert_caldav_collection(c, "caldav:ada", "VTODO", "Ada", None, "t")
        appmod.fdb.update_person(c, sam, reminder_list_id="caldav:sam")
        appmod.fdb.update_person(c, ada, reminder_list_id="caldav:ada", active=0)
        assert flags() == {"Sam": False, "Ada": False, "Bee": False}
        appmod.fdb.drop_caldav_collection(c, "caldav:sam")
        assert flags() == {"Sam": True, "Ada": False, "Bee": False}
        appmod.fdb.drop_caldav_collection(c, "caldav:ada")        # every list gone
        assert tc.get("/api/admin/state").json()["reminder_lists"] == []
        assert flags() == {"Sam": True, "Ada": True, "Bee": False}


def test_caldav_test_endpoint_reports_sync_outcome(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        assert tc.post("/api/integrations/icloud_caldav/test").json() \
            == {"ok": False, "error": "no credentials"}

    class _Fake:
        def configured(self):
            return True

        def discover(self):
            return [{"id": "cal", "name": "F", "comp": "VEVENT", "color": None}]

        def fetch_ics(self, col, lo, hi):
            return [{"href": "h", "etag": "e", "ics":
                     "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:u1\r\n"
                     "SUMMARY:E\r\nDTSTART;VALUE=DATE:20260820\r\n"
                     "DTEND;VALUE=DATE:20260821\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"}]

        def fetch_todos(self, col):
            return []
    monkeypatch.setattr(appmod, "_get_caldav_client", lambda: _Fake())
    with TestClient(appmod.app) as tc:
        st = tc.post("/api/integrations/icloud_caldav/test").json()
        assert st["ok"] is True and st["events"] == 1


def test_caldav_test_connection_never_overlaps_the_background_sync(
        tmp_path, monkeypatch):
    """"Test connection" ran a full CalDAV sync in a request thread while the
    background thread could be mid-sync on its own connection: two pulls and
    two pushes of the same outbox at once (double PUTs, stomped rows). A lock
    must keep every CalDAV sync one at a time."""
    import threading
    import time as _time
    appmod = _reload_with(tmp_path, monkeypatch, {})
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    started = threading.Event()

    def fake_sync_once(client, conn, cfg, now):
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        started.set()
        _time.sleep(0.2)
        with guard:
            active["now"] -= 1
        return {"ok": True}
    monkeypatch.setattr(appmod.caldav_sync, "sync_once", fake_sync_once)
    monkeypatch.setattr(appmod, "sync_once", lambda *a, **k: None)   # google
    monkeypatch.setattr(appmod, "_get_caldav_client", lambda: object())
    with TestClient(appmod.app) as tc:
        bg = threading.Thread(
            target=lambda: appmod._sync_tick(None, appmod._db(), appmod.cfg))
        bg.start()
        assert started.wait(5), "the background sync never started"
        assert tc.post("/api/integrations/icloud_caldav/test").json() \
            == {"ok": True}
        bg.join()
    assert active["max"] == 1, "two CalDAV syncs ran at the same time"


def test_caldav_test_connection_reports_a_wedged_sync(tmp_path, monkeypatch):
    """A background sync that never lets go must not hang the settings button
    forever: it waits a bounded time, then says so."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    monkeypatch.setattr(appmod, "CALDAV_TEST_WAIT_S", 0.05)
    monkeypatch.setattr(appmod, "_get_caldav_client", lambda: object())
    ran = []
    monkeypatch.setattr(appmod.caldav_sync, "sync_once",
                        lambda *a: ran.append(1) or {"ok": True})
    with TestClient(appmod.app) as tc:
        with appmod._caldav_sync_lock:
            st = tc.post("/api/integrations/icloud_caldav/test").json()
    assert st["ok"] is False and "already running" in st["error"]
    assert ran == []


def test_caldav_readonly_mode_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        get = lambda: {i["id"]: i for i in tc.get("/api/integrations").json()["integrations"]}
        assert get()["icloud_caldav"]["readonly"] is True         # defaults to 1-way
        assert tc.patch("/api/integrations/icloud_caldav",
                        json={"readonly": False}).json()["readonly"] is False
        assert get()["icloud_caldav"]["readonly"] is False         # 2-way persisted
        # a plain enable toggle doesn't reset the mode
        tc.patch("/api/integrations/icloud_caldav", json={"enabled": True})
        assert get()["icloud_caldav"]["readonly"] is False


def test_caldav_collection_picker_hides_calendar_and_reminders(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        soon = (appmod._today() + dt.timedelta(days=2)).isoformat()
        appmod.fdb.upsert_caldav_collection(c, "caldav:fam", "VEVENT", "Family", "#FF0000", "t")
        appmod.fdb.upsert_caldav_collection(c, "caldav:groc", "VTODO", "Groceries", None, "t")
        appmod.fdb.replace_events_caldav(c, [{"id": "u1", "calendar_id": "caldav:fam",
            "title": "Dentist", "start_ts": f"{soon}T10:00:00",
            "end_ts": f"{soon}T11:00:00", "all_day": 0}])
        _seed_reminder_object(appmod, c, "caldav:groc", "r1", "Buy milk",
                              seed_collection=False)   # groc collection seeded above
        # picker lists both, enabled; event + reminder show
        cols = {x["id"]: x for x in tc.get(
            "/api/integrations/icloud_caldav/collections").json()["collections"]}
        assert cols["caldav:fam"]["name"] == "Family" and cols["caldav:fam"]["enabled"] is True
        assert cols["caldav:groc"]["comp_type"] == "VTODO"
        assert any(e["title"] == "Dentist" for e in tc.get("/api/calendar").json()["events"])
        assert [r["title"] for r in tc.get("/api/reminders").json()["buckets"]["no_date"]] == ["Buy milk"]
        # uncheck the calendar -> its events hide (cache kept); uncheck the list -> reminders hide
        assert tc.patch("/api/integrations/icloud_caldav/collections/caldav:fam",
                        json={"enabled": False}).json()["enabled"] is False
        tc.patch("/api/integrations/icloud_caldav/collections/caldav:groc", json={"enabled": False})
        assert all(e["title"] != "Dentist" for e in tc.get("/api/calendar").json()["events"])
        assert tc.get("/api/reminders").json()["buckets"]["no_date"] == []
        # unknown collection -> 404
        assert tc.patch("/api/integrations/icloud_caldav/collections/caldav:nope",
                        json={"enabled": False}).status_code == 404


def test_calendar_status_clears_when_icloud_connected_even_if_google_isnt(tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "x")
    appmod = _reload_with(tmp_path, monkeypatch,
                          {"calendars": [{"id": "g", "kind": "google", "label": "G"}]})
    with TestClient(appmod.app) as tc:
        # Google unconfigured (no token), but iCloud synced ok -> no "not connected"
        appmod.fdb.kv_set(appmod._db(), "caldav_status", {"ok": True})
        assert tc.get("/api/hub").json()["calendar"]["status"]["ok"] is True


def test_calendar_status_not_configured_when_nothing_connected(tmp_path, monkeypatch):
    monkeypatch.delenv("ICLOUD_CALDAV_USER", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_APP_PASSWORD", raising=False)
    appmod = _reload_with(tmp_path, monkeypatch, {})   # no google/ics, no caldav
    with TestClient(appmod.app) as tc:
        st = tc.get("/api/hub").json()["calendar"]["status"]
        assert st["ok"] is False and "not configured" in st.get("error", "")


def test_todo_source_setting(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        assert tc.get("/api/hub").json()["todo_source"] == "local"        # default
        assert tc.patch("/api/todo-source", json={"source": "icloud"}).json()["source"] == "icloud"
        assert tc.get("/api/hub").json()["todo_source"] == "icloud"
        assert tc.patch("/api/todo-source", json={"source": "nope"}).status_code == 422
        assert tc.patch("/api/todo-source", json={"source": "local"}).json()["source"] == "local"


# --- two-way iCloud reminder writes (toggle / add / delete) ---------------

_RVTODO = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\n"
           "SUMMARY:Buy milk\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\n"
           "END:VCALENDAR\r\n")


def _caldav_env(monkeypatch):
    monkeypatch.setenv("ICLOUD_CALDAV_USER", "bot@icloud.com")
    monkeypatch.setenv("ICLOUD_CALDAV_APP_PASSWORD", "abcd-efgh")


def _seed_reminder(tmp_path, readonly):
    """Seed the same DB file the app uses: a writable/read-only CalDAV integration,
    one reminder list, and one open reminder (pulled + cached)."""
    from family_hub import reminders as remlogic
    c = fdb.connect(str(tmp_path / "hub.db"))
    fdb.ensure_schema(c)
    fdb.seed_integration(c, "icloud_caldav", "caldav")
    fdb.set_integration_config(c, "icloud_caldav", {"readonly": readonly})
    fdb.upsert_caldav_collection(c, "caldav:rem", "VTODO", "Groceries", None,
                                 "2026-08-17T00:00:00")
    fdb.upsert_cal_object_synced(c, {
        "id": "caldav:rem/t1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": "h/rem/0", "etag": "e0", "summary": "Buy milk",
        "raw_ics": _RVTODO, "sequence": 0, "last_modified": None})
    fdb.kv_set(c, "caldav_reminders",
               remlogic.parse_vtodo(_RVTODO, "caldav:rem", "Groceries"))
    c.close()


def _titles(buckets):
    return [x["title"] for b in buckets.values() for x in b]


def test_reminder_toggle_completes_via_overlay(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        full = tc.get("/api/reminders").json()
        assert full["writable"] is True and "Buy milk" in _titles(full["buckets"])
        r = tc.post("/api/reminders/toggle",
                    json={"id": "caldav:rem/t1", "completed": True})
        assert r.status_code == 200 and r.json()["completed"] is True
        # overlay: a completed reminder drops out of the open buckets at once
        assert "Buy milk" not in _titles(tc.get("/api/reminders").json()["buckets"])
        # ...and the object is queued for the next push
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, "caldav:rem/t1")["sync_state"] == "PENDING_UPDATE"
        c.close()


def test_reminder_add_appears_in_overlay_and_queues_create(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        r = tc.post("/api/reminders/add",
                    json={"list_id": "caldav:rem", "title": "Eggs"})
        assert r.status_code == 200
        oid = r.json()["id"]
        assert "Eggs" in _titles(tc.get("/api/reminders").json()["buckets"])
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, oid)["sync_state"] == "PENDING_CREATE"
        c.close()


def test_reminder_delete_removes_from_overlay(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        r = tc.post("/api/reminders/delete", json={"id": "caldav:rem/t1"})
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert "Buy milk" not in _titles(tc.get("/api/reminders").json()["buckets"])
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, "caldav:rem/t1")["sync_state"] == "PENDING_DELETE"
        c.close()


def test_reminder_write_refused_when_readonly(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=True)      # 1-way (default)
        assert tc.get("/api/reminders").json()["writable"] is False
        r = tc.post("/api/reminders/toggle",
                    json={"id": "caldav:rem/t1", "completed": True})
        assert r.status_code == 409                  # loud refusal, not a no-op
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, "caldav:rem/t1")["sync_state"] == "SYNCED"
        c.close()


def test_reminder_add_bad_due_is_422(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        r = tc.post("/api/reminders/add",
                    json={"list_id": "caldav:rem", "title": "X", "due": "not-a-date"})
        assert r.status_code == 422


def test_reminder_toggle_of_a_deleted_reminder_is_404_and_stays_deleted(
        tmp_path, monkeypatch):
    """A stale second screen checks off a reminder that was just deleted. The
    toggle must not bring it back (it used to turn the queued delete into an
    update), and must not answer as if it worked."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        assert tc.post("/api/reminders/delete",
                       json={"id": "caldav:rem/t1"}).status_code == 200
        r = tc.post("/api/reminders/toggle",
                    json={"id": "caldav:rem/t1", "completed": True})
        assert r.status_code == 404
        # the substring the wall's toast keys on (common.js reminderFailMessage)
        assert "unknown reminder" in r.json()["detail"]
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, "caldav:rem/t1")["sync_state"] == "PENDING_DELETE"
        c.close()


def test_reminder_toggle_unknown_id_is_404(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        r = tc.post("/api/reminders/toggle",
                    json={"id": "caldav:rem/nope", "completed": True})
        assert r.status_code == 404


def test_hub_exposes_reminder_lists_and_writable(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        body = tc.get("/api/hub").json()
        assert body["reminders_writable"] is True
        assert {"id": "caldav:rem", "name": "Groceries"} in body["reminder_lists"]
        # a disabled list drops out of the add targets
        c = fdb.connect(str(tmp_path / "hub.db"))
        fdb.set_caldav_collection_enabled(c, "caldav:rem", False)
        c.close()
        assert tc.get("/api/hub").json()["reminder_lists"] == []


def test_disabled_list_hides_pulled_and_pending_reminders(tmp_path, monkeypatch):
    """A reminder list unchecked in the picker hides BOTH its synced reminders and
    any un-pushed wall edit queued for it (the render filters by collection before
    parsing, so a PENDING create for a disabled list can't leak into the view)."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)                 # caldav:rem, "Buy milk"
        tc.post("/api/reminders/add", json={"list_id": "caldav:rem", "title": "Eggs"})
        before = _titles(tc.get("/api/reminders").json()["buckets"])
        assert "Buy milk" in before and "Eggs" in before         # pulled + PENDING create
        c = fdb.connect(str(tmp_path / "hub.db"))
        fdb.set_caldav_collection_enabled(c, "caldav:rem", False)
        c.close()
        after = _titles(tc.get("/api/reminders").json()["buckets"])
        assert "Buy milk" not in after and "Eggs" not in after   # both hidden


def test_disconnect_resets_icloud_todo_source(tmp_path, monkeypatch):
    """Clearing iCloud creds while the To-Do surface points at iCloud resets it to
    local — otherwise the surface strands on an empty iCloud card with the source
    picker (its only escape) hidden."""
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        appmod.fdb.kv_set(appmod._db(), "todo_source", "icloud")
        assert tc.delete("/api/integrations/icloud_caldav/credentials").status_code == 200
        assert appmod.fdb.kv_get(appmod._db(), "todo_source") == "local"


def test_reminder_delete_refuses_a_calendar_event(tmp_path, monkeypatch):
    """The delete endpoint takes any cal_objects id, and events live in the same
    store. Queuing an EVENT's delete would remove it from the family's iCloud
    calendar, so only a reminder (VTODO) may be deleted here, like toggle."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        c = fdb.connect(str(tmp_path / "hub.db"))
        fdb.upsert_cal_object_synced(c, {
            "id": "caldav:cal/ev1", "collection_id": "caldav:cal",
            "comp_type": "VEVENT", "uid": "ev1", "href": "h/cal/ev1",
            "etag": "e1", "summary": "Dentist", "raw_ics": "X", "sequence": 0,
            "last_modified": None})
        c.close()
        r = tc.post("/api/reminders/delete", json={"id": "caldav:cal/ev1"})
        assert r.status_code == 404
        c = fdb.connect(str(tmp_path / "hub.db"))
        assert fdb.get_cal_object(c, "caldav:cal/ev1")["sync_state"] == "SYNCED"
        c.close()


def test_reminder_delete_unknown_id_is_404(tmp_path, monkeypatch):
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        _seed_reminder(tmp_path, readonly=False)
        r = tc.post("/api/reminders/delete", json={"id": "caldav:rem/nope"})
        assert r.status_code == 404


def test_env_icloud_credentials_refuse_a_settings_save(tmp_path, monkeypatch):
    """Env credentials win over the settings file, so saving new ones in settings
    used to answer ok while the env account stayed in use. Say so instead."""
    _caldav_env(monkeypatch)
    monkeypatch.setenv("CALDAV_CREDS_PATH", str(tmp_path / "caldav.json"))
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        r = tc.post("/api/integrations/icloud_caldav/credentials",
                    json={"user": "partner@icloud.com", "app_password": "zzzz"})
        assert r.status_code == 409
        assert "environment" in r.json()["detail"]
        assert not (tmp_path / "caldav.json").exists()      # nothing written
        integ = {i["id"]: i for i in tc.get("/api/integrations").json()["integrations"]}
        assert integ["icloud_caldav"]["account"] == "bot@icloud.com"


def test_env_icloud_credentials_refuse_a_disconnect(tmp_path, monkeypatch):
    """Disconnect only removes the settings file; with env credentials the hub
    stayed connected while the reply said ok. Refuse it with the reason."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app) as tc:
        appmod.fdb.kv_set(appmod._db(), "todo_source", "icloud")
        r = tc.delete("/api/integrations/icloud_caldav/credentials")
        assert r.status_code == 409
        assert "environment" in r.json()["detail"]
        # still connected, so the To-Do surface is left alone
        assert appmod.fdb.kv_get(appmod._db(), "todo_source") == "icloud"
        ids = {i["id"] for i in tc.get("/api/integrations").json()["integrations"]}
        assert "icloud_caldav" in ids


def test_visible_reminders_skip_completed_rows_without_parsing(tmp_path, monkeypatch):
    """Completed reminders pile up (the chore mirror leaves one per chore per
    day). The wall never shows them, so they must be dropped before the costly
    icalendar parse, not after it."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app):
        c = appmod._db()
        _seed_reminder_object(appmod, c, "caldav:x", "open1", "Open one")
        for i in range(3):
            _seed_reminder_object(appmod, c, "caldav:x", f"done{i}", f"Done {i}",
                                  completed=True)
        parsed = []
        real = appmod.remlogic.parse_vtodo

        def spy(ics, *a, **kw):
            parsed.append(ics)
            return real(ics, *a, **kw)

        monkeypatch.setattr(appmod.remlogic, "parse_vtodo", spy)
        titles = [r["title"] for r in appmod._visible_reminders(c)]
        assert titles == ["Open one"]
        assert len(parsed) == 1, "completed rows must not reach the parser"


def test_visible_reminders_hide_rows_of_a_list_no_longer_known(tmp_path, monkeypatch):
    """A reminder list dropped from iCloud (deleted or unshared) leaves its
    parked, unsent wall edits in the store. They must not render under a
    nameless list on the wall."""
    _caldav_env(monkeypatch)
    appmod = _reload_with(tmp_path, monkeypatch, {})
    with TestClient(appmod.app):
        c = appmod._db()
        _seed_reminder_object(appmod, c, "caldav:x", "r1", "Kept")
        _seed_reminder_object(appmod, c, "caldav:gone", "r2", "Orphan",
                              seed_collection=False)
        assert [r["title"] for r in appmod._visible_reminders(c)] == ["Kept"]


def test_tiles_laundry_route_stamps_and_serves_completion(client, monkeypatch):
    # The laundry route's completion memory: a machine reading "done" stamps
    # its finish moment (status_since) into the kv store, and every response
    # carries the remembered stamp as last_done — so "finished at 2:14"
    # survives the machine being opened/powered off and server restarts.
    done_at = "2026-08-17T21:02:00+00:00"

    def fake(machines):
        async def tile(hclient, cfg, token):
            return {"available": True, "machines": machines}
        return tile

    # 1) dryer finishes: phase done -> stamped + echoed back
    monkeypatch.setattr("family_hub.tiles.laundry_tile", fake([
        {"id": "washer", "label": "Washer", "kind": "washer", "phase": "idle",
         "status": "initial", "finishes_at": None, "status_since": None},
        {"id": "dryer", "label": "Dryer", "kind": "dryer", "phase": "done",
         "status": "end", "finishes_at": None, "status_since": done_at}]))
    t = client.get("/api/tiles/laundry").json()
    w, d = t["machines"]
    assert d["last_done"] == done_at
    assert w["last_done"] is None      # washer has never finished

    # 2) dryer later opened/powered off (idle): the stamp survives
    monkeypatch.setattr("family_hub.tiles.laundry_tile", fake([
        {"id": "washer", "label": "Washer", "kind": "washer", "phase": "idle",
         "status": "initial", "finishes_at": None, "status_since": None},
        {"id": "dryer", "label": "Dryer", "kind": "dryer", "phase": "idle",
         "status": "power_off", "finishes_at": None, "status_since": None}]))
    t = client.get("/api/tiles/laundry").json()
    assert t["machines"][1]["last_done"] == done_at

    # 3) an unavailable tile passes through untouched (no machines key)
    async def down(hclient, cfg, token):
        return {"available": False}
    monkeypatch.setattr("family_hub.tiles.laundry_tile", down)
    assert client.get("/api/tiles/laundry").json() == {"available": False}


def test_integrations_laundry_toggle_roundtrip(client, monkeypatch):
    # With laundry configured+tokened, the integration lists, toggles off via
    # PATCH, and /api/hub reflects it — the generic toggle machinery, proven
    # for the new id.
    import family_hub.app as appmod
    from family_hub.config import _clean_laundry
    monkeypatch.setattr(appmod.cfg, "laundry", _clean_laundry({
        "ha_base": "http://ha:8123", "machines": [
            {"id": "washer", "status_entity": "s.a",
             "remaining_entity": "s.b"}]})[0])
    monkeypatch.setenv("HA_TOKEN", "tok")
    ids = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert "laundry" in ids and ids["laundry"]["group"] == "integration"
    r = client.patch("/api/integrations/laundry", json={"enabled": False})
    assert r.status_code == 200
    hub = {i["id"]: i for i in client.get("/api/hub").json()["integrations"]}
    assert hub["laundry"]["enabled"] is False
    client.patch("/api/integrations/laundry", json={"enabled": True})


def test_tiles_laundry_route_restamps_only_across_a_real_new_cycle(client, monkeypatch):
    # The stamp moves ONLY across an observed transition into done (a real new
    # cycle passes through running first). done -> done with a shifted
    # status_since is what an HA restart / cloud blip looks like (last_changed
    # resets) — re-stamping there would overwrite the true 9:02pm finish with
    # the 3am restart time.
    t1 = "2026-08-17T15:00:00+00:00"
    t_restart = "2026-08-18T03:07:00+00:00"
    t2 = "2026-08-18T20:30:00+00:00"

    def tile_with(phase, status, ts):
        async def tile(hclient, cfg, token):
            return {"available": True, "machines": [
                {"id": "dryer", "label": "Dryer", "kind": "dryer",
                 "phase": phase, "status": status, "finishes_at": None,
                 "status_since": ts}]}
        return tile

    last_done = lambda: client.get("/api/tiles/laundry").json()["machines"][0]["last_done"]
    # first finish observed -> stamped
    monkeypatch.setattr("family_hub.tiles.laundry_tile", tile_with("done", "end", t1))
    assert last_done() == t1
    # HA restart: still done, but last_changed moved -> stamp MUST NOT move
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_restart))
    assert last_done() == t1
    # done -> offline -> done (cloud blip) must not move it either
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    assert last_done() == t1
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_restart))
    assert last_done() == t1
    # a REAL new cycle: running, then done -> the stamp moves to the new finish
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", None))
    assert last_done() == t1   # still the old finish while running
    monkeypatch.setattr("family_hub.tiles.laundry_tile", tile_with("done", "end", t2))
    assert last_done() == t2


def _laundry_tile_with(phase, status, since, finishes=None):
    async def tile(hclient, cfg, token):
        return {"available": True, "machines": [
            {"id": "washer", "label": "Washer", "kind": "washer",
             "phase": phase, "status": status, "finishes_at": finishes,
             "status_since": since}]}
    return tile


def test_laundry_steady_state_writes_nothing(client, monkeypatch):
    """The watcher annotates every 5s. A machine sitting in the same phase
    used to rewrite its last-phase key on every tick: ~35k SQLite commits a
    day on the event loop for no change at all. A repeat of the same state
    must not write."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    writes = []
    real = fdb.kv_set
    monkeypatch.setattr(fdb, "kv_set",
                        lambda c, k, v: (writes.append(k), real(c, k, v)))
    for phase, status in (("running", "running"), ("idle", "power_off"),
                          ("done", "end")):
        monkeypatch.setattr("family_hub.tiles.laundry_tile",
                            _laundry_tile_with(phase, status, now))
        client.get("/api/tiles/laundry")            # the transition writes
        writes.clear()
        for _ in range(3):
            assert client.get("/api/tiles/laundry").status_code == 200
        assert writes == [], f"steady {phase} rewrote {writes}"


def test_laundry_watch_stamp_is_refreshed_once_it_ages(client, monkeypatch):
    """The "was the hub watching" stamp is rewritten only once it is
    LAUNDRY_TICK_PERSIST_S old, so a steady watcher does not commit every
    tick, yet the stamp never trails far enough to fake a gap."""
    import family_hub.app as appmod
    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_tile_with("idle", "power_off", now.isoformat()))
    c = appmod._db()
    fresh = (now - dt.timedelta(seconds=5)).isoformat()
    fdb.kv_set(c, "laundry_last_tick", fresh)
    client.get("/api/tiles/laundry")
    assert fdb.kv_get(c, "laundry_last_tick") == fresh, "rewritten too soon"
    aged = (now - dt.timedelta(
        seconds=appmod.LAUNDRY_TICK_PERSIST_S + 1)).isoformat()
    fdb.kv_set(c, "laundry_last_tick", aged)
    client.get("/api/tiles/laundry")
    assert fdb.kv_get(c, "laundry_last_tick") > aged
    assert appmod.LAUNDRY_TICK_PERSIST_S < appmod.LAUNDRY_START_EXACT_MIN * 60


def test_laundry_annotations_never_run_side_by_side(app_mod, monkeypatch):
    """The watcher and the route's inline fallback both annotate in worker
    threads now. Run together, both could read the same previous phase and
    log one finished load twice. The lock keeps them one at a time."""
    import threading
    import time as _time
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    def slow(t):
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        _time.sleep(0.1)
        with guard:
            active["now"] -= 1
        return t
    monkeypatch.setattr(app_mod, "_laundry_annotate", slow)
    threads = [threading.Thread(target=app_mod._laundry_annotate_serial,
                                args=({"machines": []},)) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert active["max"] == 1


def test_laundry_annotate_runs_off_the_event_loop(app_mod, monkeypatch):
    """_laundry_annotate does blocking SQLite work. Called straight from the
    async watcher and route it stalled the event loop (every stream, every
    request) on each tick. It must run in a worker thread."""
    import asyncio
    import threading
    seen = []

    def spy(t):
        seen.append(threading.get_ident())
        return t
    monkeypatch.setattr(app_mod, "_laundry_annotate", spy)

    async def fake_tile(*a, **kw):
        return {"available": True, "machines": []}
    monkeypatch.setattr(app_mod.tiles, "laundry_tile", fake_tile)
    monkeypatch.setattr(app_mod, "_laundry_snapshot", None)

    async def run():
        loop_thread = threading.get_ident()
        await app_mod._laundry_watch_tick()
        monkeypatch.setattr(app_mod, "_laundry_snapshot", None)
        await app_mod._laundry_payload()                  # inline fallback
        return loop_thread
    loop_thread = asyncio.run(run())
    assert len(seen) == 2 and loop_thread not in seen


def test_tiles_laundry_observed_finish_holds_done_through_auto_power_off(
        client, monkeypatch):
    # LG machines turn THEMSELVES off 30-90s after "end" with the load still
    # inside (measured live: 29s and 79s). An observed done -> idle(power_off)
    # exit is the machine's own act, not a person collecting the load — the
    # wall must keep presenting Done for the same hold window a MISSED finish
    # gets. Without this, perfect observation makes Done vanish in a minute
    # while a missed finish holds 30 (operator report, 2026-08-18: "washer
    # shows idle instead of done"). An exit to "initial" (a human powering
    # the machine on) still decays immediately — that's the real seen-it
    # signal this status enum offers.
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    # cycle runs, then the observed end
    t_end = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-30), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    assert machine()["phase"] == "done"
    # ...the machine powers ITSELF off: Done must stand, at the observed end
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = machine()
    assert m["phase"] == "done", "auto power-off must not eat an observed Done"
    assert m["status_since"] == t_end
    assert m["last_done"] == t_end
    # steady state keeps holding (idle -> idle is not a transition)
    assert machine()["phase"] == "done"
    # the hold is diagnosable in the cycle log (raw transition + note)
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["phase"] == "idle" and rows[0]["note"] == "auto_off_hold"
    # a new cycle retires the hold like any other machine activity
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "detecting", iso(0)))
    assert machine()["phase"] == "running"
    # cancelled cycle: no Done faked — the earlier hold was already retired
    # by the new cycle above, so nothing is left to present
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0), finishes=iso(5)))
    assert machine()["phase"] == "idle"


def test_tiles_laundry_power_on_mid_hold_clears_done(client, monkeypatch):
    # Once the hold has engaged the machine sits in phase "idle", so a
    # person powering it on arrives as a STATUS change (power_off ->
    # initial), never a phase transition — the clear must key on current
    # status or the green Done keeps glowing for the rest of its window
    # while someone stands at the machine emptying it (caught in review).
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    t_end = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-30), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    assert machine()["phase"] == "done"          # hold engaged
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "initial", iso(0)))
    m = machine()                                # a person powered it on
    assert m["phase"] == "idle", "power-on must clear the hold immediately"
    assert m["last_done"] == t_end               # the quiet line still serves
    # the collection moment is LOGGED (the one status-keyed row in the
    # phase-keyed log) — a hold killed at minute 3 and one that ran its
    # window must be tellable apart when tuning the hold from log data
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["note"] == "hold_cleared_by_power_on"
    assert rows[0]["prev_phase"] == "idle" and rows[0]["phase"] == "idle"
    # ...and the clear is durable: powering back off must not resurrect it
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    assert machine()["phase"] == "idle"
    # same contact signal clears a MISSED-finish hold (shared mechanism)
    t_fin = iso(-2)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "rinsing", iso(-40), finishes=t_fin))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0), finishes=t_fin))
    assert machine()["phase"] == "done"          # missed finish presents
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "initial", iso(0), finishes=t_fin))
    assert machine()["phase"] == "idle"


def test_tiles_laundry_auto_off_hold_bridges_an_ha_blip(client, monkeypatch):
    # done -> offline -> idle(power_off): the auto power-off landing during
    # an HA blip must still engage the hold via the non-offline provenance
    # (came_from), with the bridge recorded — the missed-finish twin has
    # this guard, the observed-finish path needs it too (a simplification
    # of came_from back to prev would silently lose exactly this case).
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    t_end = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-30), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = machine()
    assert m["phase"] == "done" and m["status_since"] == t_end
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["note"] == "auto_off_hold+offline_bridge"


def test_tiles_laundry_frozen_prevent_exit_keeps_the_hold(client, monkeypatch):
    # frozen_prevent_initial is the machine's own freeze-prevention standby
    # (winter firmware), not a person at the machine — a cold-day finish
    # must hold Done the same as a plain auto power-off
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    t_end = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-30), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "frozen_prevent_initial", iso(0)))
    assert machine()["phase"] == "done"


def test_tiles_laundry_stale_observed_finish_does_not_hold(client, monkeypatch):
    # The hold refuses a stale end stamp (server down between "end" and the
    # auto power-off for longer than the hold window): presenting a
    # 40-minute-old finish as a fresh green Done would be wrong-but-
    # reassuring — decay straight to idle + the "last load" line.
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    t_end = iso(-40)   # past LAUNDRY_MISSED_DONE_HOLD_MIN
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-70), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    assert machine()["phase"] == "done"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = machine()
    # not a fresh green Done; a WASHER load nobody has moved presents as
    # "waiting" instead (see test_laundry_washer_load_waits_until_...)
    assert m["phase"] == "waiting" and m["status_since"] == t_end
    assert m["last_done"] == t_end   # the accurate stamp still serves the line
    # the refusal is LOUD in the cycle log — a bare NULL row would be
    # indistinguishable from an ordinary transition, invisible to the
    # tune-from-log analysis this log exists for
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["phase"] == "idle" and rows[0]["note"] == "auto_off_refused"
    # ...and a refusal resolved ACROSS an HA blip carries the bridge suffix
    # like every other outcome (the suffix line serves both arms)
    t_end2 = iso(-40)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "spinning", iso(-70), finishes=t_end2))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end2))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    assert machine()["phase"] == "idle"
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["note"] == "auto_off_refused+offline_bridge"


def test_tiles_laundry_route_stamps_and_presents_a_missed_finish(client, monkeypatch):
    # LG machines auto-power-off a minute or two after "end", so a 60s poll
    # can watch running -> power_off and never observe the done phase at all —
    # the live board sat on a bare "Idle" with no last-load line
    # (2026-08-17). An observed running -> idle transition with the projection
    # passed IS a finished load: it stamps completion memory and is presented
    # as the Done it was (until the hold window lapses — separate test).
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    # cycle runs, then the machine powers itself off past the poll window:
    # the idle poll still carries the (now past) projected finish -> that
    # exact moment is the stamp AND the wall shows Done at that moment
    t_fin = iso(-3)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-40), finishes=t_fin))
    assert machine()["last_done"] is None
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(-1), finishes=t_fin))
    m = machine()
    assert m["last_done"] == t_fin
    assert m["phase"] == "done" and m["status_since"] == t_fin
    # sitting idle: neither the stamp nor the held Done moves
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = machine()
    assert m["last_done"] == t_fin and m["phase"] == "done"
    # an HA blip DURING the hold must not erase the held Done — offline is
    # deliberately absent from the missed-memory clearing set
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    assert machine()["phase"] == "offline"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = machine()
    assert m["last_done"] == t_fin and m["phase"] == "done"

    # next cycle CANCELED mid-run: projection still in the future -> the
    # stamp falls back to the moment the machine left the cycle, and no
    # Done is faked for a load that never finished
    t_cancel = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-10), finishes=iso(30)))
    m = machine()
    assert m["last_done"] == t_fin      # still the old finish while running
    assert m["phase"] == "running"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", t_cancel, finishes=iso(30)))
    m = machine()
    # idle presented (not a lingering Done): the new cycle retired the held
    # missed-done, and a canceled cycle never creates one
    assert m["last_done"] == t_cancel and m["phase"] == "idle"

    # exit from PAUSED: pause freezes the drum while the projection keeps
    # aging, so a "past" projection there is fiction — the stamp is the exit
    # moment, and no Done is faked
    t_stop = iso(-1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("paused", "pause", iso(-20), finishes=iso(-15)))
    assert machine()["phase"] == "paused"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", t_stop, finishes=iso(-15)))
    m = machine()
    assert m["last_done"] == t_stop and m["phase"] == "idle"
    # the paused exit's cycle-log note labels it honestly — a mislabeled
    # note is silently wrong tuning evidence
    assert client.get("/api/laundry/log").json()["entries"][0]["note"] \
        == "cycle_exit"

    # running -> OFFLINE alone stamps nothing (HA blind mid-cycle is not a
    # finish; the drum may still be turning) ...
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-5)))
    assert machine()["last_done"] == t_stop   # observed: prev is now running
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    assert machine()["last_done"] == t_stop
    # ... but the machine REAPPEARING idle closes the cycle across the blip:
    # with no usable projection the stamp is the reappear moment, and no
    # Done is faked (the projection-backed blip case has its own test)
    t_back = iso(0)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", t_back))
    m = machine()
    assert m["last_done"] == t_back and m["phase"] == "idle"

    # a REAL observed finish, then the door opens (done -> idle with a fresh
    # status_since): the accurate end stamp survives — the door-open moment
    # must not overwrite it — and Done does not linger past the door
    t_end = iso(-2)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-30), finishes=t_end))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("done", "end", t_end))
    assert machine()["phase"] == "done"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "initial", iso(0)))
    m = machine()
    assert m["last_done"] == t_end and m["phase"] == "idle"


def _laundry_pair(w, d):
    """A two-machine tile stand-in: w and d are (phase, status, since,
    finishes, total) tuples for the washer and dryer."""
    def mk(mid, kind, spec):
        phase, status, since, finishes, total = spec
        return {"id": mid, "label": mid.title(), "kind": kind, "phase": phase,
                "status": status, "finishes_at": finishes,
                "status_since": since, "total_min": total, "starts_at": None,
                "error": None}

    async def tile(hclient, cfg, token):
        return {"available": True, "machines": [mk("washer", "washer", w),
                                                mk("dryer", "dryer", d)]}
    return tile


def test_config_keeps_valid_optional_laundry_entities_only(caplog):
    from family_hub.config import _clean_laundry
    got, err = _clean_laundry({"ha_base": "http://ha", "machines": [{
        "id": "washer", "status_entity": "sensor.s", "remaining_entity": "sensor.r",
        "total_entity": " sensor.t ", "start_entity": 7, "error_entity": ""}]})
    assert err is None
    m = got["machines"][0]
    assert m["total_entity"] == "sensor.t"
    assert "start_entity" not in m and "error_entity" not in m
    assert "ignoring start_entity" in caplog.text


def test_laundry_washer_load_waits_until_the_dryer_starts(client, monkeypatch):
    # Real data (34 washes, 2026-08-18..09-21): the dryer started a median
    # ~100 min after the washer finished, only 5 of 34 inside the 30-min
    # Done hold. The wall said "Idle" for hours over a drum of wet clothes.
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-45)     # finished 45 min ago: past the 30-min Done hold
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("running", "spinning", iso(-80), t_end, 35), off))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("done", "end", t_end, None, None), off))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-44), None, None), off))
    w = get()["washer"]
    assert w["phase"] == "waiting", "a finished wash nobody moved is waiting"
    assert w["status_since"] == t_end and w["last_done"] == t_end
    assert get()["washer"]["phase"] == "waiting"      # steady
    # the dryer starts: the load went in it
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-44), None, None),
        ("running", "running", iso(0), iso(54), 54)))
    ms = get()
    assert ms["washer"]["phase"] == "idle" and ms["washer"]["last_done"] == t_end
    notes = [r["note"] for r in client.get("/api/laundry/log").json()["entries"]]
    assert "wait_cleared_by_dryer" in notes
    # and it stays cleared after the dryer finishes
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-44), None, None), off))
    assert get()["washer"]["phase"] == "idle"


def test_laundry_dryer_that_started_BEFORE_the_finish_does_not_clear_it(
        client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    dry = ("running", "running", iso(-50), iso(10), 60)   # started long ago
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-300), None, None), dry))
    get()
    t_end = iso(-40)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("running", "spinning", iso(-70), t_end, 30), dry))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("done", "end", t_end, None, None), dry))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-39), None, None), dry))
    assert get()["washer"]["phase"] == "waiting", \
        "a dryer already running when the wash ended holds a different load"


def test_laundry_dryer_start_ends_a_fresh_done_hold_too(client, monkeypatch):
    # 5 of 34 real washes moved inside the hold: the Done glow should stop
    # the moment the load is in the dryer, not glow on for the full 30 min
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-3)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("running", "spinning", iso(-40), t_end, 35), off))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("done", "end", t_end, None, None), off))
    get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-2), None, None), off))
    assert get()["washer"]["phase"] == "done"          # the hold, as before
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-2), None, None),
        ("running", "running", iso(0), iso(54), 54)))
    assert get()["washer"]["phase"] == "idle"


def test_laundry_waiting_cleared_by_washer_power_on_and_logged(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-90)
    for w in (("running", "spinning", iso(-120), t_end, 30),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-89), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    assert get()["washer"]["phase"] == "waiting"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "initial", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "idle"
    rows = client.get("/api/laundry/log").json()["entries"]
    assert rows[0]["note"] == "wait_cleared_by_power_on"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "idle", "powering back off must not resurrect it"


def test_laundry_waiting_expires_after_the_cap(client, monkeypatch):
    # a load hung up to dry leaves no signal at all; the claim must end
    from family_hub import tiles as t_mod
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-3000), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-(t_mod.LAUNDRY_WAIT_MAX_H * 60 + 5))
    for w in (("running", "spinning", iso(-900), t_end, 30),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-700), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    w = get()["washer"]
    assert w["phase"] == "idle" and w["last_done"] == t_end
    notes = [r["note"] for r in client.get("/api/laundry/log").json()["entries"]]
    assert "wait_expired" in notes


def test_laundry_dryer_never_waits(client, monkeypatch):
    # dry clothes don't sour; only a washer load presents as waiting
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-60)
    for d in (("running", "running", iso(-110), t_end, 50),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-59), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, d))
        get()
    assert get()["dryer"]["phase"] == "idle"


def test_laundry_start_of_cycle_placeholder_finish_is_replaced(client, monkeypatch):
    # Live history, every dryer load: right after it starts, remaining time
    # reads "1 min" (once "6 min") for a minute or more before the real
    # projection lands, e.g. 09-12 16:31:38 start, total 54, finish 16:32:35.
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, off))
    get()                                          # the dryer at rest
    start = now - dt.timedelta(minutes=1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "running", start.isoformat(), iso(1), 54)))
    d = get()["dryer"]
    fixed = dt.datetime.fromisoformat(d["finishes_at"])
    assert abs((fixed - (start + dt.timedelta(minutes=54))).total_seconds()) < 1
    # the raw value stays in the cycle log
    row = client.get("/api/laundry/log").json()["entries"][0]
    assert row["machine"] == "dryer" and row["finishes_at"] == iso(1)
    # the real projection lands: consistent, left alone
    real = iso(52)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "running", start.isoformat(), real, 54)))
    assert get()["dryer"]["finishes_at"] == real


def _watched_start(client, monkeypatch, off, spec):
    """Rest, then the running spec, so the hub watches the start."""
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, off))
    client.get("/api/tiles/laundry")
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, spec))
    return {m["id"]: m for m in client.get("/api/tiles/laundry").json()["machines"]}


def test_laundry_placeholder_fix_leaves_real_estimates_alone(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    # a hub that was DOWN when the cycle began first sees it mid-cycle: the
    # status changed 2 min ago (a sub-status), finish in 5, length 60. Not a
    # start we watched, so no claim (review, 2026-09-22: this used to push
    # the finish out ~38 min for the rest of the cycle)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, off))
    client.get("/api/tiles/laundry")
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "cooling", iso(-5), iso(5), 60)))
    assert client.get("/api/tiles/laundry").json()["machines"][1]["finishes_at"] == iso(5)


def test_laundry_cycle_begun_during_an_ha_outage_is_not_a_watched_start(
        client, monkeypatch):
    # the dryer was idle, HA went blind (the lg_thinq MQTT freeze), the load
    # started unseen; on reconnect last_changed is FRESH. Not a start we saw
    # (review wave 2): no placeholder rewrite of its real 5-min finish.
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    for d in (off, ("offline", None, None, None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, d))
        client.get("/api/tiles/laundry")
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "cooling", iso(-0.2), iso(5), 60)))
    assert client.get("/api/tiles/laundry").json()["machines"][1]["finishes_at"] == iso(5)


def test_laundry_start_after_a_hub_gap_is_not_a_watched_start(client, monkeypatch):
    # the hub was down; it comes back 1 min after a sub-status change of a
    # cycle it never saw begin (review wave 2)
    import family_hub.app as appmod
    from family_hub import db as fdb_mod
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, off))
    client.get("/api/tiles/laundry")
    fdb_mod.kv_set(appmod._db(), "laundry_last_tick", iso(-40))   # the gap
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "cooling", iso(-1), iso(5), 60)))
    assert client.get("/api/tiles/laundry").json()["machines"][1]["finishes_at"] == iso(5)


def test_laundry_error_then_resume_keeps_the_watched_start(client, monkeypatch):
    import family_hub.app as appmod
    from family_hub import db as fdb_mod
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    start = iso(-0.5)
    _watched_start(client, monkeypatch, off, ("running", "running", start, iso(50), 54))
    assert fdb_mod.kv_get(appmod._db(), "laundry_start_dryer") == start
    for d in (("error", "error", iso(0), None, None),
              ("running", "running", iso(0), iso(49), 54)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, d))
        client.get("/api/tiles/laundry")
    assert fdb_mod.kv_get(appmod._db(), "laundry_start_dryer") == start


def test_laundry_placeholder_fix_ignores_a_large_time_left(client, monkeypatch):
    # washer load sensing cuts a default 48-min course to 26; if total_time
    # lags a poll, the large, real time left must not be pushed back out
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    d = _watched_start(client, monkeypatch, off,
                       ("running", "running", iso(-1), iso(26), 60))
    assert d["dryer"]["finishes_at"] == iso(26)


def test_laundry_wrinkle_care_after_a_finish_is_not_a_dryer_start(client, monkeypatch):
    # wrinkle care tumbles a FINISHED dryer load now and then; it must not
    # tell a waiting wash that it was moved into the dryer
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-45)
    dry_done = ("done", "end", iso(-50), None, None)
    for w in (("running", "spinning", iso(-80), t_end, 35),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-44), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, dry_done))
        get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-44), None, None),
        ("running", "wrinkle_care", iso(0), None, None)))
    assert get()["washer"]["phase"] == "waiting"


def test_laundry_dryer_first_seen_mid_cycle_does_not_clear_a_wait(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-45)
    for w in (("running", "spinning", iso(-80), t_end, 35),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-44), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    # the dryer is seen running, but its status changed 30 min ago: we did
    # not watch it start, so it can't prove the wash went in it
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(-44), None, None),
        ("running", "running", iso(-30), iso(20), 60)))
    assert get()["washer"]["phase"] == "waiting"


def test_laundry_power_on_inside_the_hold_clears_the_wait_too(client, monkeypatch):
    # the real LG sequence: end, then the machine turns itself off within
    # 90 s (auto_off_hold), then someone powers it on inside the half hour
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-5)
    for w in (("running", "spinning", iso(-40), t_end, 35),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-4), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    assert get()["washer"]["phase"] == "done"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "initial", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "idle"
    assert client.get("/api/laundry/log").json()["entries"][0]["note"] \
        == "hold_cleared_by_power_on"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "idle", "no wait survives the power-on"


def test_laundry_power_on_after_the_hold_logs_the_wait_not_the_hold(
        client, monkeypatch):
    # the REAL sequence: auto_off_hold sets the missed key, which outlives
    # its window; a power-on hours later ended the WAIT (review, 2026-09-22)
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    from family_hub import db as fdb_mod
    t_end = iso(-5)
    for w in (("running", "spinning", iso(-40), t_end, 35),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-4), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    # age both stamps two hours back, as if the family came back later
    import family_hub.app as appmod
    c = appmod._db()
    old = iso(-120)
    for k in ("laundry_missed_washer", "laundry_wait_washer", "laundry_done_washer"):
        fdb_mod.kv_set(c, k, old)
    assert get()["washer"]["phase"] == "waiting"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "initial", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "idle"
    assert client.get("/api/laundry/log").json()["entries"][0]["note"] \
        == "wait_cleared_by_power_on"


def test_laundry_waiting_survives_an_ha_blip(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-45)
    for w in (("running", "spinning", iso(-80), t_end, 35),
              ("done", "end", t_end, None, None),
              ("idle", "power_off", iso(-44), None, None)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("offline", None, None, None, None), off))
    assert get()["washer"]["phase"] == "offline"
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        ("idle", "power_off", iso(0), None, None), off))
    assert get()["washer"]["phase"] == "waiting"


def test_laundry_new_wash_straight_from_done_clears_the_old_wait(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    t_end = iso(-45)
    for w in (("running", "spinning", iso(-80), t_end, 35),
              ("done", "end", t_end, None, None),
              ("running", "detecting", iso(0), iso(48), 48)):
        monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(w, off))
        get()
    import family_hub.app as appmod
    from family_hub import db as fdb_mod
    assert fdb_mod.kv_get(appmod._db(), "laundry_wait_washer") is None


def test_laundry_reserved_then_running_is_a_watched_start(client, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("reserved", "reserved", iso(-60), None, None)))
    client.get("/api/tiles/laundry")
    start = now - dt.timedelta(minutes=1)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "running", start.isoformat(), iso(1), 54)))
    d = client.get("/api/tiles/laundry").json()["machines"][1]
    fixed = dt.datetime.fromisoformat(d["finishes_at"])
    assert abs((fixed - (start + dt.timedelta(minutes=54))).total_seconds()) < 1


def test_laundry_unusable_wait_stamp_is_dropped(client, monkeypatch, caplog):
    import family_hub.app as appmod
    from family_hub import db as fdb_mod
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(off, off))
    client.get("/api/tiles/laundry")
    fdb_mod.kv_set(appmod._db(), "laundry_wait_washer", "2026-01-01T00:00:00")
    assert client.get("/api/tiles/laundry").json()["machines"][0]["phase"] == "idle"
    assert fdb_mod.kv_get(appmod._db(), "laundry_wait_washer") is None
    assert "unusable wait stamp" in caplog.text


def test_laundry_placeholder_fix_leaves_a_long_paused_cycle_alone(client, monkeypatch):
    # after a pause the machine is BEHIND schedule (elapsed > implied): the
    # rule only ever fires the other way
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda m: (now + dt.timedelta(minutes=m)).isoformat()
    off = ("idle", "power_off", iso(-300), None, None)
    get = lambda: {m["id"]: m for m in
                   client.get("/api/tiles/laundry").json()["machines"]}
    start = iso(-50)
    monkeypatch.setattr("family_hub.tiles.laundry_tile", _laundry_pair(
        off, ("running", "running", start, iso(30), 54)))
    assert get()["dryer"]["finishes_at"] == iso(30)


def _laundry_stub_tile(machines, calls=None):
    """A tiles.laundry_tile stand-in; `calls` (a list) counts invocations so
    tests can prove the route served the watcher's snapshot WITHOUT re-fetching."""
    async def tile(hclient, cfg, token):
        if calls is not None:
            calls.append(1)
        return {"available": True, "machines": [dict(m) for m in machines]}
    return tile


_LN_IDLE = {"id": "washer", "label": "Washer", "kind": "washer",
            "phase": "idle", "status": "initial", "finishes_at": None,
            "status_since": None}
_LN_RUN = {"id": "washer", "label": "Washer", "kind": "washer",
           "phase": "running", "status": "spinning", "finishes_at": None,
           "status_since": None}


def test_laundry_watcher_snapshot_serves_without_refetch(client, monkeypatch):
    # The background watcher's tick fetches + annotates once and stores a
    # snapshot; the route then serves that snapshot while it's fresh instead
    # of re-fetching per request — and falls back to its own inline fetch
    # once the snapshot goes stale (watcher wedged/disabled).
    import asyncio
    import time as _time
    import family_hub.app as appmod
    calls = []
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_IDLE], calls))
    asyncio.run(appmod._laundry_watch_tick())
    assert len(calls) == 1
    t = client.get("/api/tiles/laundry").json()
    assert t["available"] is True
    assert t["machines"][0]["id"] == "washer"
    assert "last_done" in t["machines"][0]     # snapshot is the ANNOTATED tile
    assert len(calls) == 1                     # served from the snapshot
    # stale snapshot -> the route's inline fallback fetches for itself
    appmod._laundry_snapshot_ts = (
        _time.monotonic() - appmod.LAUNDRY_SNAPSHOT_FRESH_S - 1)
    client.get("/api/tiles/laundry").json()
    assert len(calls) == 2


def test_laundry_watcher_wakes_stream_waiters_only_on_change(client, monkeypatch):
    # Each tick that CHANGES the payload swaps in a fresh waiter event and
    # sets the old one (SSE pushes); an unchanged tick wakes nobody (idle
    # machines must not spray heartbeat traffic as data events).
    import asyncio
    import family_hub.app as appmod
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_IDLE]))
    asyncio.run(appmod._laundry_watch_tick())   # None -> idle: a change
    ev = appmod._laundry_change
    asyncio.run(appmod._laundry_watch_tick())   # steady state
    assert appmod._laundry_change is ev and not ev.is_set()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))
    asyncio.run(appmod._laundry_watch_tick())   # idle -> running: a change
    assert ev.is_set() and appmod._laundry_change is not ev


def test_laundry_watcher_tick_failure_is_soft_and_recovers(client, monkeypatch):
    # A tick that blows up (HA down mid-request, DB hiccup) must neither
    # raise out of the loop nor clobber the standing snapshot; the next
    # good tick recovers.
    import asyncio
    import family_hub.app as appmod
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_IDLE]))
    asyncio.run(appmod._laundry_watch_tick())
    good = appmod._laundry_snapshot

    async def boom(hclient, cfg, token):
        raise RuntimeError("HA fell over")
    monkeypatch.setattr("family_hub.tiles.laundry_tile", boom)
    asyncio.run(appmod._laundry_watch_tick())   # must not raise
    assert appmod._laundry_snapshot == good
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))
    asyncio.run(appmod._laundry_watch_tick())
    assert appmod._laundry_snapshot["machines"][0]["phase"] == "running"


def test_laundry_watcher_holds_last_good_through_a_brief_blip(client, monkeypatch):
    # At a 5s cadence a transient HA blip is ~15x more likely to be observed
    # than under the old 60s poll — so a brief available:false tick keeps
    # the last good card standing (still served fresh) rather than
    # flickering "Laundry unavailable" across the kitchen. A PERSISTENT
    # outage still goes honestly unavailable once the hold expires.
    import asyncio
    import time as _time
    import family_hub.app as appmod
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))
    asyncio.run(appmod._laundry_watch_tick())

    async def down(hclient, cfg, token):
        return {"available": False}
    monkeypatch.setattr("family_hub.tiles.laundry_tile", down)
    asyncio.run(appmod._laundry_watch_tick())   # blip: last good stands
    t = client.get("/api/tiles/laundry").json()
    assert t["available"] is True and t["machines"][0]["phase"] == "running"
    # outage persists past the hold -> honest unavailable
    appmod._laundry_unavail_since = (
        _time.monotonic() - appmod.LAUNDRY_UNAVAIL_HOLD_S - 1)
    asyncio.run(appmod._laundry_watch_tick())
    assert client.get("/api/tiles/laundry").json() == {"available": False}
    # recovery mid-outage clears the hold clock: a LATER blip gets a fresh
    # 30s hold, not the dregs of this one
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))
    asyncio.run(appmod._laundry_watch_tick())
    assert appmod._laundry_unavail_since is None
    monkeypatch.setattr("family_hub.tiles.laundry_tile", down)
    asyncio.run(appmod._laundry_watch_tick())
    t = client.get("/api/tiles/laundry").json()
    assert t["available"] is True, "a fresh blip must get a fresh hold"


def test_laundry_watcher_startup_with_ha_down_is_honestly_unavailable(
        client, monkeypatch):
    # No prior good snapshot means there is nothing honest to hold — a hub
    # that BOOTS against a down HA must show unavailable immediately, not
    # nothing-at-all for the length of the hold window.
    import asyncio
    import family_hub.app as appmod

    async def down(hclient, cfg, token):
        return {"available": False}
    monkeypatch.setattr("family_hub.tiles.laundry_tile", down)
    asyncio.run(appmod._laundry_watch_tick())
    assert appmod._laundry_snapshot == {"available": False}
    assert client.get("/api/tiles/laundry").json() == {"available": False}


def test_laundry_watch_gate_opens_in_production_shape(app_mod, monkeypatch):
    # THE headline gate: with a real-shaped deployment (sync enabled +
    # laundry configured) the watcher must arm — a gate regressed to
    # always-False loses the entire real-time feature INVISIBLY (the tile
    # route's inline fallback keeps the card looking fine at 60s latency,
    # and the cycle log quietly stops observing). Each leg is then flipped
    # individually to prove every OFF condition still gates.
    from family_hub.config import _clean_laundry
    appmod = app_mod
    assert appmod.LAUNDRY_WATCH_S <= 10.0
    laundry = _clean_laundry({
        "ha_base": "http://ha:8123", "machines": [
            {"id": "washer", "status_entity": "s.a",
             "remaining_entity": "s.b"}]})[0]
    monkeypatch.setattr(appmod.cfg, "laundry", laundry)
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    assert appmod._laundry_watch_enabled() is True    # the production shape
    monkeypatch.setenv("DISABLE_SYNC", "1")
    assert appmod._laundry_watch_enabled() is False   # tests / opt-out
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    monkeypatch.setattr(appmod, "DEMO", True)
    assert appmod._laundry_watch_enabled() is False   # demo: canned data
    monkeypatch.setattr(appmod, "DEMO", False)
    monkeypatch.setattr(appmod.cfg, "laundry", None)
    assert appmod._laundry_watch_enabled() is False   # unconfigured hub
    # PAIRED: the tile cache must expire under the watcher cadence, or every
    # tick re-reads a still-warm cache and the "watcher" silently observes
    # nothing new (the same drift trap the old endgame fast lane had)
    assert ftiles.LAUNDRY_TTL < appmod.LAUNDRY_WATCH_S


def test_laundry_lifespan_arms_and_disarms_the_watcher(app_mod, monkeypatch):
    # The lifespan actually STARTS the loop when the gate is open and
    # cancels it on shutdown — the gate test above means nothing if the
    # task creation itself is dropped from _lifespan.
    import asyncio
    appmod = app_mod
    ran = asyncio.Event()

    async def loop_stub():
        ran.set()
        await asyncio.sleep(3600)   # parks until cancelled by the lifespan

    monkeypatch.setattr(appmod, "_laundry_watch_enabled", lambda: True)
    monkeypatch.setattr(appmod, "laundry_watch_loop", loop_stub)
    with TestClient(appmod.app):
        pass   # enter starts the lifespan; exit must cancel cleanly
    assert ran.is_set(), "lifespan never started the watch loop"


# A laundry block as config.load actually produces one: _clean_laundry drops a
# machine-less block to None, so a fixture with "machines": [] tests a state
# that cannot exist on a real hub.
_LAUNDRY_CFG = {"ha_base": "http://ha:8123", "machines": [
    {"id": "washer", "label": "Washer", "kind": "washer",
     "status_entity": "sensor.washer_current_status",
     "remaining_entity": "sensor.washer_remaining_time"}]}


def _errors(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.ERROR and r.name.startswith("family_hub")]


def test_configured_laundry_without_a_token_shouts_at_startup(app_mod, monkeypatch, caplog):
    # The 2026-09-17 bug: the deploy box lost its .env, compose recreated the
    # container with an empty HA_TOKEN, and the wall's laundry card simply
    # disappeared, with no error anywhere and /health still 200. A configured-
    # tokenless laundry must be LOUD at startup.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "   ")   # whitespace is still no token
    monkeypatch.setattr(appmod, "_laundry_watch_enabled", lambda: False)
    assert appmod._laundry_env_broken() is True
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        with TestClient(appmod.app):
            pass
    hits = [r.getMessage() for r in _errors(caplog) if "HA_TOKEN" in r.getMessage()]
    assert len(hits) == 1, hits          # once, not once per worker/route
    assert "laundry" in hits[0]


def test_a_healthy_laundry_setup_says_nothing_at_startup(app_mod, monkeypatch, caplog):
    # The paired half: the loud line must not cry wolf on a working hub, or it
    # gets tuned out. Both halves run through the real lifespan; asserting the
    # predicate alone would miss a guard that shouts for every hub.
    appmod = app_mod
    monkeypatch.setattr(appmod, "_laundry_watch_enabled", lambda: False)
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "tok")
    assert appmod._laundry_env_broken() is False
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        with TestClient(appmod.app):
            pass
    assert not _errors(caplog), "a working laundry must start silently"

    monkeypatch.setattr(appmod.cfg, "laundry", None)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert appmod._laundry_env_broken() is False   # no laundry, no complaint
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        with TestClient(appmod.app):
            pass
    assert not _errors(caplog)


def test_demo_laundry_is_never_called_broken(app_mod, monkeypatch, caplog):
    # DEMO serves canned laundry with no HA at all, and the compose quickstart
    # tells people to run DEMO=1 against a real config.json. Shouting about a
    # missing token there is an error line that is wrong in a documented mode,
    # which is how error lines get tuned out.
    appmod = app_mod
    monkeypatch.setattr(appmod, "DEMO", True)
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr(appmod, "_laundry_watch_enabled", lambda: False)
    assert appmod._laundry_env_broken() is False
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        with TestClient(appmod.app):
            pass
    assert not _errors(caplog)


def test_a_tokenless_laundry_stays_in_the_settings_menu_as_needs_auth(
        app_mod, client, monkeypatch):
    # The other half of the disappearing act: with no token the integration
    # used to drop out of the registry, so the ONE surface an operator checks
    # showed a hub with no laundry instead of a laundry that needs a token.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert "laundry" in rows, "a tokenless laundry vanished from settings again"
    assert rows["laundry"]["status"] == "needs_auth"

    monkeypatch.setenv("HA_TOKEN", "tok")
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert rows["laundry"]["status"] is None, "a working laundry must not nag"


def test_the_laundry_status_never_leaks_onto_another_integration(
        app_mod, client, monkeypatch):
    # _integ_status takes the laundry health as a parameter and the call site
    # passes it for EVERY row. Applying it to any id but laundry would paint a
    # "reconnect" badge across the whole settings menu.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert rows["laundry"]["status"] == "needs_auth"
    for iid, row in rows.items():
        if iid not in ("laundry", "icloud_caldav", "google_calendar"):
            assert row["status"] is None, f"{iid} inherited laundry's status"


def test_a_laundry_block_that_survives_nothing_is_an_error_not_an_absence(
        app_mod, client, monkeypatch, caplog):
    # The same disappearing act through a different door: a typo'd entity key
    # drops every machine, _clean_laundry returns None, and the integration
    # used to leave the registry entirely -- blank slot on the wall, no
    # settings row, /health 200.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", None)
    monkeypatch.setattr(appmod.cfg, "laundry_config_error",
                        "no valid machines in the laundry block")
    monkeypatch.setenv("HA_TOKEN", "tok")
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert "laundry" in rows, "a broken laundry config vanished the integration"
    assert rows["laundry"]["status"] == "error"
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        with TestClient(appmod.app):
            pass
    assert any("laundry is configured" in r.getMessage() for r in _errors(caplog))


def test_a_rejected_token_also_asks_for_a_reconnect(app_mod, client, monkeypatch):
    # A revoked token is the likelier failure than a missing one (they are
    # revoked from HA's own UI), and it looks identical on the wall. The row
    # has to say so, or "the settings row says reconnect" is only true for the
    # case nobody hits.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "a-token-that-ha-no-longer-accepts")
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert rows["laundry"]["status"] is None          # nothing rejected yet
    monkeypatch.setattr(appmod.tiles, "laundry_auth_rejected", lambda: True)
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert rows["laundry"]["status"] == "needs_auth"


def test_a_machine_stuck_offline_is_escalated_and_shown(app_mod, monkeypatch, caplog):
    # `available` is an OR across machines, so a washer whose entity was
    # renamed reads offline forever while the dryer keeps the tile healthy.
    # Nothing used to notice: no whole-feed alert, no status, just a dash.
    appmod = app_mod
    clock = {"t": 5000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "tok")     # the token is fine; the entity is not
    monkeypatch.setattr(appmod, "_laundry_machine_offline_since", {})
    monkeypatch.setattr(appmod, "_laundry_machines_stuck", set())
    with caplog.at_level(logging.WARNING, logger="family_hub"):
        snap = {"available": True, "machines": [
            {"id": "washer", "phase": "offline"},
            {"id": "dryer", "phase": "running"}]}
        appmod._laundry_watch_machines(snap, clock["t"])
        assert appmod._laundry_row_status() is None      # inside the grace
        clock["t"] += appmod.LAUNDRY_UNAVAIL_ALERT_S + 1
        appmod._laundry_watch_machines(snap, clock["t"])
        stuck = [r for r in _errors(caplog) if "washer" in r.getMessage()]
        assert len(stuck) == 1, [r.getMessage() for r in stuck]
        assert appmod._laundry_row_status() == "error"
        clock["t"] += 600
        appmod._laundry_watch_machines(snap, clock["t"])
        assert len([r for r in _errors(caplog) if "washer" in r.getMessage()]) == 1

        snap["machines"][0]["phase"] = "running"
        appmod._laundry_watch_machines(snap, clock["t"])
        assert appmod._laundry_row_status() is None
        assert any("washer is reporting again" in r.getMessage()
                   for r in caplog.records)


def test_a_whole_feed_outage_reaches_the_settings_row_too(app_mod, monkeypatch):
    # The loudest lane in the log used to be the quietest one in the UI: HA
    # down, or every entity renamed at once, left the row reading healthy
    # beside a wall that said "Laundry unavailable". That is the original
    # incident in miniature -- the one surface an operator checks says nothing
    # is wrong.
    appmod = app_mod
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "tok")
    monkeypatch.setattr(appmod, "_laundry_machines_stuck", set())
    monkeypatch.setattr(appmod, "_laundry_unavail_alerted", False)
    assert appmod._laundry_row_status() is None
    monkeypatch.setattr(appmod, "_laundry_unavail_alerted", True)
    assert appmod._laundry_row_status() == "error"


def test_a_machines_outage_clock_survives_a_feed_outage(app_mod, monkeypatch, caplog):
    # A machine offline for hours never escalated if the whole feed dropped
    # often enough to keep resetting its clock: the same starvation the
    # two-clock split fixed on the feed lane.
    appmod = app_mod
    monkeypatch.setattr(appmod, "_laundry_machine_offline_since", {})
    monkeypatch.setattr(appmod, "_laundry_machines_stuck", set())
    monkeypatch.setattr(appmod.cfg, "laundry", _LAUNDRY_CFG)
    monkeypatch.setenv("HA_TOKEN", "tok")
    t = 100.0
    offline = {"available": True, "machines": [
        {"id": "washer", "phase": "offline"}, {"id": "dryer", "phase": "idle"}]}
    appmod._laundry_watch_machines(offline, t)
    started = dict(appmod._laundry_machine_offline_since)
    assert "washer" in started
    # the feed drops and comes back, repeatedly, while the washer stays gone
    for _ in range(5):
        t += 10
        appmod._laundry_watch_machines({"available": False}, t)
        t += 10
        appmod._laundry_watch_machines(offline, t)
    assert appmod._laundry_machine_offline_since == started, \
        "the feed outage restarted the machine's clock"
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        appmod._laundry_watch_machines(offline, t + appmod.LAUNDRY_UNAVAIL_ALERT_S)
    assert [r for r in _errors(caplog) if "washer" in r.getMessage()]


def test_the_escalation_threshold_cannot_undercut_the_display_hold():
    # The ERROR says the card is "stuck on unavailable". If the threshold ever
    # dropped below the hold, it would say that about a card still showing
    # live machines, and the two surfaces would contradict each other.
    import family_hub.app as appmod
    assert appmod.LAUNDRY_UNAVAIL_ALERT_S > appmod.LAUNDRY_UNAVAIL_HOLD_S
    assert appmod.LAUNDRY_UNAVAIL_ALERT_S >= 120, \
        "shorter than a Home Assistant restart: this would cry wolf"


def test_a_long_outage_escalates_even_while_a_good_card_is_held(
        app_mod, monkeypatch, caplog):
    # The hold's early return sits BELOW the escalation for a reason: with a
    # last-good card standing, an outage that never ends would otherwise skip
    # the alert entirely. Drive a real outage from an available snapshot.
    import asyncio
    appmod = app_mod
    clock = {"t": 9000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(appmod, "_laundry_unavail_since", None)
    monkeypatch.setattr(appmod, "_laundry_alert_since", None)
    monkeypatch.setattr(appmod, "_laundry_unavail_alerted", False)
    monkeypatch.setattr(appmod, "_laundry_ok_streak", 0)
    monkeypatch.setattr(appmod, "_laundry_snapshot",
                        {"available": True, "machines": []})
    state = {"available": False}

    async def fake_tile(*a, **kw):
        return dict(state)
    monkeypatch.setattr(appmod.tiles, "laundry_tile", fake_tile)

    with caplog.at_level(logging.ERROR, logger="family_hub"):
        asyncio.run(appmod._laundry_watch_tick())     # inside the hold
        assert appmod._laundry_snapshot["available"] is True, "hold broke"
        assert not _errors(caplog)
        clock["t"] += appmod.LAUNDRY_UNAVAIL_ALERT_S + 1
        asyncio.run(appmod._laundry_watch_tick())
        assert appmod._laundry_snapshot == {"available": False}
        assert len([r for r in _errors(caplog)
                    if "has been failing for" in r.getMessage()]) == 1


def test_the_completion_history_failing_is_latched_not_flooded(
        app_mod, monkeypatch, caplog):
    # At a 5s cadence an un-latched warning here is ~17,000 lines a day, each
    # with a traceback, burying the one ERROR this branch exists to make
    # findable. And it is not cosmetic: without the synthesis a finished load
    # renders as a bare "Idle".
    import asyncio
    appmod = app_mod
    monkeypatch.setattr(appmod, "_laundry_annotate_failures", 0)

    async def fake_tile(*a, **kw):
        return {"available": True, "machines": []}
    monkeypatch.setattr(appmod.tiles, "laundry_tile", fake_tile)

    def boom(_t):
        raise RuntimeError("kv layer down")
    monkeypatch.setattr(appmod, "_laundry_annotate", boom)

    with caplog.at_level(logging.WARNING, logger="family_hub"):
        for _ in range(appmod.LAUNDRY_ANNOTATE_STRIKES * 3):
            asyncio.run(appmod._laundry_watch_tick())
    errs = [r for r in _errors(caplog) if "completion history" in r.getMessage()]
    assert len(errs) == 1, [r.getMessage() for r in errs]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) < appmod.LAUNDRY_ANNOTATE_STRIKES, "flooded the log"
    # the card itself still renders: a missing history must not blank the wall
    assert appmod._laundry_snapshot == {"available": True, "machines": []}


def test_a_long_outage_is_escalated_once_and_closed_on_recovery(
        app_mod, monkeypatch, caplog):
    # tiles.py logs one WARNING per entity and goes quiet "until it recovers",
    # which reads the same whether HA rebooted for 20s or the token was revoked
    # last Tuesday. Minutes of unavailability earn exactly one ERROR, and the
    # recovery closes it, so the next outage is loud again.
    import asyncio
    appmod = app_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(appmod, "_laundry_snapshot", None)
    monkeypatch.setattr(appmod, "_laundry_unavail_since", None)
    monkeypatch.setattr(appmod, "_laundry_alert_since", None)
    monkeypatch.setattr(appmod, "_laundry_unavail_alerted", False)
    monkeypatch.setattr(appmod, "_laundry_ok_streak", 0)
    state = {"available": False}

    async def fake_tile(*a, **kw):
        return dict(state)
    monkeypatch.setattr(appmod.tiles, "laundry_tile", fake_tile)

    def tick():
        asyncio.run(appmod._laundry_watch_tick())

    with caplog.at_level(logging.INFO, logger="family_hub"):
        tick()                                    # outage starts
        clock["t"] += appmod.LAUNDRY_UNAVAIL_ALERT_S - 1
        tick()                                    # still inside the grace
        assert not _errors(caplog), "escalated before the threshold"
        clock["t"] += 2
        tick()                                    # past it: one ERROR
        clock["t"] += 600
        tick()                                    # ...and not a second one
        stuck = [r for r in _errors(caplog) if "has been failing for" in r.getMessage()]
        assert len(stuck) == 1, [r.getMessage() for r in stuck]

        state["available"] = True
        state["machines"] = []
        # Recovery must be SUSTAINED to close the incident: one good tick
        # clears the card's hold, not the alert.
        clock["t"] += 10
        tick()
        assert not any("feed recovered" in r.getMessage() for r in caplog.records), \
            "closed the incident on a single good tick"
        for _ in range(appmod.LAUNDRY_RECOVERY_TICKS - 1):
            clock["t"] += 10
            tick()
        assert any("feed recovered" in r.getMessage() for r in caplog.records)

        # a SECOND outage must shout again; a latch that never re-arms is the
        # same silence in slower motion. (state.clear() first: the payload has
        # to differ from the last one or the tick treats it as unchanged.)
        state.clear()
        state["available"] = False
        clock["t"] += appmod.LAUNDRY_UNAVAIL_ALERT_S + 1
        tick()
        clock["t"] += appmod.LAUNDRY_UNAVAIL_ALERT_S + 1
        tick()
        stuck = [r for r in _errors(caplog) if "has been failing for" in r.getMessage()]
        assert len(stuck) == 2, [r.getMessage() for r in stuck]


def test_laundry_watch_loop_survives_a_tick_that_raises(app_mod, monkeypatch):
    # The loop's own armor: _laundry_watch_tick guards fetch+annotate, but
    # an exception escaping the tick (a future edit past the try, an
    # unexpected error path) must not kill the task permanently — the death
    # would be SILENT (the route's fallback keeps the card healthy-looking
    # while the log stops observing).
    import asyncio
    appmod = app_mod
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("escaped the tick")
        raise asyncio.CancelledError()   # ends the loop for the test

    async def run():
        monkeypatch.setattr(appmod, "_laundry_watch_tick", tick)
        monkeypatch.setattr(appmod, "LAUNDRY_WATCH_S", 0.001)
        with pytest.raises(asyncio.CancelledError):
            await appmod.laundry_watch_loop()
    asyncio.run(run())
    assert len(calls) == 2, "the loop must outlive a crashing tick"


# The SSE stream is tested by driving its generator directly: this
# starlette's TestClient BUFFERS the whole response before returning
# (verified by stack dump — client.stream() never comes back from an
# unbounded body), so the HTTP layer physically can't exercise an infinite
# stream. Route registration is asserted separately; live behavior is part
# of the post-deploy verification.

def _sse_data(chunk):
    """Parse one `data: {...}` SSE chunk to JSON; None for a keepalive."""
    s = chunk.decode() if isinstance(chunk, bytes) else chunk
    if not s.startswith("data: "):
        return None
    return json.loads(s[len("data: "):s.index("\n")])


def test_laundry_stream_greets_pings_and_pushes(client, monkeypatch):
    # The stream leads with the CURRENT annotated tile (the wall paints
    # without waiting for a change), keeps the pipe warm with `: ping`
    # while nothing happens, and pushes a fresh event the moment a watcher
    # tick observes a change — same payload shape as /api/tiles/laundry.
    import asyncio
    import family_hub.app as appmod
    monkeypatch.setattr(appmod, "LAUNDRY_STREAM_PING_S", 0.05)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))
    assert any(getattr(r, "path", None) == "/api/laundry/stream"
               for r in appmod.app.routes)

    async def scenario():
        await appmod._laundry_watch_tick()
        resp = await appmod.laundry_stream()
        assert resp.media_type == "text/event-stream"
        gen = resp.body_iterator
        chunks = [await gen.__anext__(),    # greeting: the current tile
                  await gen.__anext__()]    # steady state: keepalive
        monkeypatch.setattr("family_hub.tiles.laundry_tile",
                            _laundry_stub_tile([_LN_IDLE]))
        await appmod._laundry_watch_tick()  # a change lands...
        chunks.append(await gen.__anext__())   # ...and is pushed
        await gen.aclose()
        return chunks

    greet, ping, push = asyncio.run(scenario())
    ev = _sse_data(greet)
    assert ev and ev["available"] is True
    assert ev["machines"][0]["phase"] == "running"
    assert "last_done" in ev["machines"][0]
    assert _sse_data(ping) is None          # comment frame, not a data event
    ev2 = _sse_data(push)
    assert ev2 and ev2["machines"][0]["phase"] == "idle"


def test_laundry_stream_pushes_without_waiting_out_the_ping(client, monkeypatch):
    # The lost-wakeup contract, pinned: the generator arms its waiter
    # BEFORE reading the payload, so a change is pushed IMMEDIATELY. With
    # the ping timer left at its real 20s, a reordered arm-after-read (or a
    # dropped waiter) can only deliver on the next ping cycle — this
    # wait_for would time out.
    import asyncio
    import family_hub.app as appmod
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        _laundry_stub_tile([_LN_RUN]))

    async def scenario():
        await appmod._laundry_watch_tick()
        gen = (await appmod.laundry_stream()).body_iterator
        await gen.__anext__()                      # greeting
        push = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)                  # parked on the waiter
        monkeypatch.setattr("family_hub.tiles.laundry_tile",
                            _laundry_stub_tile([_LN_IDLE]))
        await appmod._laundry_watch_tick()
        try:
            return await asyncio.wait_for(push, timeout=1.0)
        finally:
            await gen.aclose()
    chunk = asyncio.run(scenario())
    ev = _sse_data(chunk)
    assert ev and ev["machines"][0]["phase"] == "idle"


def test_laundry_stream_survives_a_failing_payload_read(client, monkeypatch):
    # A payload read that raises mid-stream (HA down AND the snapshot gone
    # stale) must heartbeat, not escape the generator — an escaping
    # exception would put every open wall's EventSource into a reconnect
    # storm against a server that's already struggling.
    import asyncio
    import family_hub.app as appmod
    monkeypatch.setattr(appmod, "LAUNDRY_STREAM_PING_S", 0.05)

    state = {"fail": True}

    async def payload():
        if state["fail"]:
            raise RuntimeError("payload read failed")
        return {"available": True, "machines": []}

    async def scenario():
        monkeypatch.setattr(appmod, "_laundry_payload", payload)
        gen = (await appmod.laundry_stream()).body_iterator
        first = await gen.__anext__()          # held open: keepalive
        state["fail"] = False                  # payload heals...
        second = await gen.__anext__()         # ...and the stream recovers
        await gen.aclose()
        return first, second
    first, second = asyncio.run(scenario())
    assert _sse_data(first) is None, "a failing read must yield a keepalive"
    ev = _sse_data(second)
    assert ev and ev["available"] is True


def test_laundry_stream_demo_serves_canned_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    monkeypatch.setenv("DEMO", "1")
    import asyncio
    import family_hub.app as appmod
    importlib.reload(appmod)
    try:
        monkeypatch.setattr(appmod, "LAUNDRY_STREAM_PING_S", 0.01)

        async def first_two():
            resp = await appmod.laundry_stream()
            gen = resp.body_iterator
            chunks = [await gen.__anext__(), await gen.__anext__()]
            await gen.aclose()
            return chunks
        greet, then = asyncio.run(first_two())
        ev = _sse_data(greet)
        assert ev and ev["available"] is True and len(ev["machines"]) == 2
        # demo machines never transition, but demo_laundry() re-times every
        # payload per call — the stream must NOT push those as "changes"
        # (each push is an innerHTML rebuild restarting the tumble mid-spin
        # on the demo wall). Greet once, then keepalives only.
        assert _sse_data(then) is None, \
            "DEMO stream must not churn data events"
    finally:
        monkeypatch.delenv("DEMO")
        importlib.reload(appmod)


def test_laundry_cycle_log_records_transitions_only(client, app_mod, monkeypatch):
    # every observed RAW phase transition lands in the cycle log with its
    # diagnostic note; steady-state polls add nothing; the endpoint serves
    # newest-first and the synthesized done presentation is never logged
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    poll = lambda: client.get("/api/tiles/laundry").json()
    entries = lambda: client.get("/api/laundry/log").json()["entries"]

    t_fin = iso(-3)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "rinsing", iso(-40), finishes=iso(20)))
    poll()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(-1), finishes=t_fin))
    poll()   # missed finish: presented done, logged as the RAW idle it was
    poll()   # steady state: no new row
    rows = entries()
    assert [r["phase"] for r in rows] == ["idle", "running"]   # newest first
    assert rows[0]["prev_phase"] == "running"
    assert rows[0]["note"] == "missed_finish"
    assert rows[0]["finishes_at"] == t_fin
    assert rows[1]["prev_phase"] is None and rows[1]["note"] is None
    # ?machine= filters, ?limit= clamps
    assert entries() == client.get("/api/laundry/log?machine=washer").json()["entries"]
    assert client.get("/api/laundry/log?machine=dryer").json()["entries"] == []
    assert len(client.get("/api/laundry/log?limit=1").json()["entries"]) == 1
    # out-of-range limits 422 loudly (calendar-route precedent) — silent
    # truncation is wrong for a log someone pages through by hand
    assert client.get("/api/laundry/log?limit=0").status_code == 422
    assert client.get("/api/laundry/log?limit=5000").status_code == 422
    # fail-soft: a broken log read serves an empty list, never a 500
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("family_hub.db.laundry_log_recent", boom)
    r = client.get("/api/laundry/log")
    assert r.status_code == 200 and r.json() == {"entries": []}


def test_laundry_cycle_log_write_failure_retries_the_transition(client, monkeypatch):
    # the log write runs BEFORE phase_key consumes the transition: a flaky
    # DB at exactly the wrong moment must lose NOTHING — the same
    # transition (kv stamps and log row) retries whole on the next poll
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    tile_with = _laundry_tile_with
    poll = lambda: client.get("/api/tiles/laundry").json()["machines"][0]
    entries = lambda: client.get("/api/laundry/log").json()["entries"]

    t_fin = iso(-2)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "rinsing", iso(-30), finishes=iso(20)))
    poll()
    assert len(entries()) == 1
    # the DB goes flaky for exactly the finish transition
    import sqlite3
    real_add = fdb.laundry_log_add
    def flaky(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("family_hub.db.laundry_log_add", flaky)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0), finishes=t_fin))
    m = poll()   # fail-soft response, transition NOT consumed
    assert m["last_done"] is None
    assert len(entries()) == 1
    # DB heals: the SAME poll payload now lands the whole transition —
    # stamp, synthesized Done, and the log row with its note
    monkeypatch.setattr("family_hub.db.laundry_log_add", real_add)
    m = poll()
    assert m["last_done"] == t_fin and m["phase"] == "done"
    rows = entries()
    assert len(rows) == 2 and rows[0]["note"] == "missed_finish"


def test_tiles_laundry_route_missed_done_bounds(client, app_mod, monkeypatch, caplog):
    # two bounds keep the synthetic Done honest. STAMPING: a projection
    # staler than the hold window at the exit is likely a LATCHED
    # previous-cycle value from a flaky remaining-time sensor — refused
    # (with a warning), the exit moment stamps instead, no Done is faked.
    # PRESENTATION: a held stamp older than the window decays to idle (what
    # a restart finding an old stamp in kv must show), keeping the
    # last-load line.
    from family_hub import tiles as ftiles
    tile_with = _laundry_tile_with
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    stale = iso(-(ftiles.LAUNDRY_MISSED_DONE_HOLD_MIN + 10))
    t_off = iso(0)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-90), finishes=stale))
    client.get("/api/tiles/laundry")
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", t_off, finishes=stale))
    with caplog.at_level("WARNING"):
        m = client.get("/api/tiles/laundry").json()["machines"][0]
    assert m["last_done"] == t_off and m["phase"] == "idle"
    assert any("stale finish projection" in r.message for r in caplog.records), \
        "the refused stale projection must be visible in the logs"
    assert client.get("/api/laundry/log").json()["entries"][0]["note"] \
        == "stale_projection"

    # presentation decay: seed kv exactly as a restart would find it — a
    # missed stamp just past the hold — and poll idle
    old = iso(-(ftiles.LAUNDRY_MISSED_DONE_HOLD_MIN + 5))
    c = app_mod._db()
    app_mod.fdb.kv_set(c, "laundry_missed_washer", old)
    app_mod.fdb.kv_set(c, "laundry_done_washer", old)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0)))
    m = client.get("/api/tiles/laundry").json()["machines"][0]
    assert m["last_done"] == old and m["phase"] == "idle"


def test_tiles_laundry_route_blip_straddled_finish_presents_done(client, monkeypatch):
    # a finish landing exactly inside an HA blip (running -> offline ->
    # idle) is still a finish: the last non-offline phase is tracked
    # separately, so the recent past projection stamps and presents Done —
    # while a blip on a machine that never ran stays silent
    tile_with = _laundry_tile_with
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    machine = lambda: client.get("/api/tiles/laundry").json()["machines"][0]

    t_fin = iso(-2)
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("running", "running", iso(-30), finishes=iso(3)))
    assert machine()["phase"] == "running"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    assert machine()["phase"] == "offline"
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0), finishes=t_fin))
    m = machine()
    assert m["last_done"] == t_fin
    assert m["phase"] == "done" and m["status_since"] == t_fin
    # the log row carries both facts: a missed finish, resolved across a blip
    assert client.get("/api/laundry/log").json()["entries"][0]["note"] \
        == "missed_finish+offline_bridge"
    # a FURTHER blip on the now-idle machine changes nothing: provenance is
    # idle, so no re-stamp — and the held Done survives the blip
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("offline", None, None))
    machine()
    monkeypatch.setattr("family_hub.tiles.laundry_tile",
                        tile_with("idle", "power_off", iso(0), finishes=t_fin))
    m = machine()
    assert m["last_done"] == t_fin and m["phase"] == "done"


def test_tiles_laundry_route_end_to_end_real_tile(client, monkeypatch):
    # The real wiring, unmocked: route -> tiles.laundry_tile -> (mock HTTP
    # transport) -> HA-shaped states. Catches an env-var typo or argument
    # swap that the monkeypatched-tile tests can't see (fail-soft would mask
    # it as a permanently unavailable card). Also proves the in-process tile
    # cache serves the SECOND request and that the route's last_done
    # annotation never leaks into the cached dict.
    import httpx
    import family_hub.app as appmod
    import family_hub.tiles as ftiles
    from family_hub.config import _clean_laundry

    ftiles.reset_caches()
    monkeypatch.setattr(appmod.cfg, "laundry", _clean_laundry({
        "ha_base": "http://ha:8123", "machines": [
            {"id": "washer", "label": "Washer", "kind": "washer",
             "status_entity": "sensor.w_status",
             "remaining_entity": "sensor.w_rem"}]})[0])
    monkeypatch.setenv("HA_TOKEN", "tok")
    done_at = "2026-08-17T21:02:00+00:00"

    def handler(req):
        assert req.headers.get("Authorization") == "Bearer tok"
        entity = req.url.path.rsplit("/", 1)[-1]
        state = {"sensor.w_status": {"state": "end", "last_changed": done_at},
                 "sensor.w_rem": {"state": "unknown", "last_changed": done_at}}
        return httpx.Response(200, json=state[entity])

    monkeypatch.setattr(
        appmod, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    t = client.get("/api/tiles/laundry").json()
    assert t["available"] is True
    m = t["machines"][0]
    assert m["phase"] == "done" and m["last_done"] == done_at
    # second request: served from the tile cache, same annotated shape
    assert client.get("/api/tiles/laundry").json()["machines"][0]["last_done"] == done_at
    # the cached dict itself stays un-annotated (the route copies)
    cached = ftiles._laundry_cache["http://ha:8123"][1]
    assert "last_done" not in cached["machines"][0]
    ftiles.reset_caches()


# --- richer chore routines: interval / biweekly / due-times (P1) ----------

def _mk_person(client, name="Ana"):
    return client.post("/api/admin/people",
                       json={"name": name, "color": "#5BC9F0"}).json()["id"]


def test_chore_interval_biweekly_and_due_times(client, app_mod):
    pid = _mk_person(client)
    r = client.post("/api/admin/chores", json={
        "title": "Counters", "schedule_kind": "interval", "interval_days": 3,
        "assign_kind": "fixed", "fixed_person_id": pid})
    assert r.status_code == 200
    ch = r.json()
    assert ch["schedule_kind"] == "interval" and ch["interval_days"] == 3
    assert ch["due_times"] == []
    r = client.post("/api/admin/chores", json={
        "title": "Trash", "schedule_kind": "days", "days_mask": 1,
        "week_interval": 2, "due_times": ["07:00", "18:30"],
        "assign_kind": "fixed", "fixed_person_id": pid})
    assert r.status_code == 200
    ch2 = r.json()
    assert ch2["week_interval"] == 2 and ch2["due_times"] == ["07:00", "18:30"]


def test_chore_recurrence_validation(client, app_mod):
    pid = _mk_person(client)
    base = {"title": "x", "assign_kind": "fixed", "fixed_person_id": pid}
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "interval"}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "interval", "interval_days": 0}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "interval", "interval_days": 400}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "days", "days_mask": 1, "week_interval": 3}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "daily", "due_times": ["25:00"]}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "schedule_kind": "daily", "due_times": ["7am"]}).status_code == 422


def test_chore_kind_change_clears_dependent_fields(client, app_mod):
    pid = _mk_person(client)
    cid = client.post("/api/admin/chores", json={
        "title": "x", "schedule_kind": "interval", "interval_days": 5,
        "assign_kind": "fixed", "fixed_person_id": pid}).json()["id"]
    r = client.patch(f"/api/admin/chores/{cid}", json={"schedule_kind": "daily"})
    assert r.status_code == 200
    ch = r.json()
    assert ch["schedule_kind"] == "daily" and ch["interval_days"] is None
    assert ch["week_interval"] == 1
    # converting TO interval requires interval_days
    assert client.patch(f"/api/admin/chores/{cid}", json={"schedule_kind": "interval"}).status_code == 422


# --- P2: person -> iCloud reminder-list mapping ---------------------------

def test_person_reminder_list_mapping(client, app_mod):
    c = app_mod._db()
    app_mod.fdb.upsert_caldav_collection(c, "caldav:emma", "VTODO", "Emma", None, "t")
    pid = _mk_person(client, "Emma")
    lists = client.get("/api/admin/state").json()["reminder_lists"]
    assert {"id": "caldav:emma", "name": "Emma"} in lists
    r = client.patch(f"/api/admin/people/{pid}", json={"reminder_list_id": "caldav:emma"})
    assert r.status_code == 200 and r.json()["reminder_list_id"] == "caldav:emma"
    assert client.patch(f"/api/admin/people/{pid}",
                        json={"reminder_list_id": "caldav:nope"}).status_code == 422
    r = client.patch(f"/api/admin/people/{pid}", json={"reminder_list_id": None})
    assert r.status_code == 200 and r.json()["reminder_list_id"] is None
# --- backup-health badge: pure _backup_status + /api/hub `backup` block ---

_BT0 = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)


def test_backup_status_unknown(app_mod):
    assert app_mod._backup_status(None, _BT0, 129600) == {
        "known": False, "last_success": None, "age_s": None,
        "stale": False, "threshold_s": 129600, "remote": None}


def _kv_backup(app_mod, tmp_path, rec):
    conn = fdb.connect(str(tmp_path / "rb.db"))
    fdb.ensure_schema(conn)
    fdb.kv_set(conn, "backup_status", rec)
    return app_mod._build_backup(conn, now=_BT0, stale_s=129600)


def test_build_backup_without_remote_has_no_remote_block(app_mod, tmp_path):
    s = _kv_backup(app_mod, tmp_path,
                   {"at": (_BT0 - dt.timedelta(hours=1)).isoformat()})
    assert s["remote"] is None and s["stale"] is False


def test_build_backup_good_remote(app_mod, tmp_path):
    ok_at = (_BT0 - dt.timedelta(hours=1)).isoformat()
    s = _kv_backup(app_mod, tmp_path,
                   {"at": ok_at, "remote_ok": True, "remote_at": ok_at,
                    "remote_ok_at": ok_at})
    assert s["remote"] == {"ok": True, "last_ok": ok_at, "age_s": 3600,
                           "stale": False, "failing": False}


def test_build_backup_failing_remote_is_not_healthy(app_mod, tmp_path):
    """The local snapshot is fresh but the NAS copy failed: that used to read
    as a healthy backup. It must now surface as failing."""
    fresh = (_BT0 - dt.timedelta(hours=1)).isoformat()
    s = _kv_backup(app_mod, tmp_path,
                   {"at": fresh, "remote_ok": False, "remote_at": fresh,
                    "remote_ok_at": (_BT0 - dt.timedelta(hours=2)).isoformat()})
    assert s["stale"] is False                       # local is fine
    assert s["remote"]["failing"] is True and s["remote"]["ok"] is False


def test_build_backup_remote_never_succeeded_or_old_is_stale(app_mod, tmp_path):
    fresh = (_BT0 - dt.timedelta(hours=1)).isoformat()
    never = _kv_backup(app_mod, tmp_path,
                       {"at": fresh, "remote_ok": False, "remote_at": fresh})
    assert never["remote"]["stale"] is True and never["remote"]["last_ok"] is None
    old = (_BT0 - dt.timedelta(hours=40)).isoformat()
    s = _kv_backup(app_mod, tmp_path,
                   {"at": fresh, "remote_ok": True, "remote_at": old,
                    "remote_ok_at": old})
    assert s["remote"]["stale"] is True and s["remote"]["failing"] is False


def test_backup_status_fresh(app_mod):
    s = app_mod._backup_status(_BT0 - dt.timedelta(hours=1), _BT0, 129600)
    assert s["known"] is True and s["age_s"] == 3600 and s["stale"] is False


def test_backup_status_stale(app_mod):
    s = app_mod._backup_status(_BT0 - dt.timedelta(hours=40), _BT0, 129600)  # 144000 > 129600
    assert s["stale"] is True and s["age_s"] == 144000


def test_backup_status_boundary(app_mod):
    assert app_mod._backup_status(_BT0 - dt.timedelta(seconds=129600), _BT0, 129600)["stale"] is False
    assert app_mod._backup_status(_BT0 - dt.timedelta(seconds=129601), _BT0, 129600)["stale"] is True


def test_hub_carries_backup_block(client):
    b = client.get("/api/hub").json()["backup"]
    assert set(b) >= {"known", "stale", "age_s", "last_success", "threshold_s"}
    assert b["known"] is False   # no heartbeat yet on a fresh db -> muted, not a false alarm


def test_build_backup_reads_kv_heartbeat(app_mod, tmp_path):
    conn = fdb.connect(str(tmp_path / "hb.db"))
    fdb.ensure_schema(conn)
    at = _BT0 - dt.timedelta(hours=2)
    fdb.kv_set(conn, "backup_status",
               {"at": at.isoformat(), "snapshot": "hub-x.db", "bytes": 9000})
    s = app_mod._build_backup(conn, now=_BT0, stale_s=129600)
    assert s["known"] is True and s["age_s"] == 7200 and s["stale"] is False


def test_build_backup_unknown_on_empty_kv(app_mod, tmp_path):
    conn = fdb.connect(str(tmp_path / "empty.db"))
    fdb.ensure_schema(conn)
    s = app_mod._build_backup(conn, now=_BT0, stale_s=129600)
    assert s["known"] is False and s["stale"] is False


def test_build_backup_malformed_timestamp_is_unknown(app_mod, tmp_path):
    # A garbage/format-drifted "at" must fail SAFE to unknown, never crash or
    # report a false-fresh timestamp.
    conn = fdb.connect(str(tmp_path / "bad.db"))
    fdb.ensure_schema(conn)
    fdb.kv_set(conn, "backup_status", {"at": "not-a-date"})
    s = app_mod._build_backup(conn, now=_BT0, stale_s=129600)
    assert s["known"] is False and s["stale"] is False


def test_build_backup_naive_timestamp_coerced_to_utc(app_mod, tmp_path):
    # The script writes tz-aware ISO, but a naive "at" must still be read as UTC
    # rather than crashing on naive-vs-aware subtraction.
    conn = fdb.connect(str(tmp_path / "naive.db"))
    fdb.ensure_schema(conn)
    naive = (_BT0 - dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
    fdb.kv_set(conn, "backup_status", {"at": naive})
    s = app_mod._build_backup(conn, now=_BT0, stale_s=129600)
    assert s["known"] is True and s["age_s"] == 3600


def test_hub_survives_backup_read_error(client, monkeypatch):
    # A backup-status read that throws must NOT 500 the whole wall: /api/hub
    # stays 200 and the block degrades to unknown (fails-soft, like todos).
    import family_hub.db as fdbmod
    orig = fdbmod.kv_get

    def boom(conn, key):
        if key == "backup_status":
            raise RuntimeError("kv read blew up")
        return orig(conn, key)

    monkeypatch.setattr(fdbmod, "kv_get", boom)
    r = client.get("/api/hub")
    assert r.status_code == 200
    assert r.json()["backup"]["known"] is False


def test_a_brief_google_hiccup_does_not_flash_the_snag_banner(tmp_path, monkeypatch):
    # review 2026-09-22: one failed fetch showed "sync hit a snag" at once.
    # Within the grace window, with a last good sync on screen, the wall says
    # nothing; past it, the error shows; with no prior sync it shows at once.
    appmod = _reload_with(tmp_path, monkeypatch, {"calendars": [
        {"id": "fam", "kind": "google", "label": "Family"}]})
    with TestClient(appmod.app) as tc:
        c = appmod._db()
        now = appmod._now_local()
        fresh = (now - dt.timedelta(minutes=5)).isoformat()
        old = (now - dt.timedelta(minutes=appmod.CALENDAR_ERROR_GRACE_MIN + 5)).isoformat()
        appmod.fdb.kv_set(c, "calendar_status", {"ok": False, "error": "503", "last_sync": old, "error_since": fresh})
        assert tc.get("/api/calendar").json()["status"]["ok"] is True
        appmod.fdb.kv_set(c, "calendar_status", {"ok": False, "error": "503", "last_sync": old, "error_since": old})
        assert tc.get("/api/calendar").json()["status"]["ok"] is False
        appmod.fdb.kv_set(c, "calendar_status", {"ok": False, "error": "503", "last_sync": None, "error_since": fresh})
        assert tc.get("/api/calendar").json()["status"]["ok"] is False
        # an expired sign-in is never held back
        appmod.fdb.kv_set(c, "calendar_status", {"ok": False, "needs_auth": True, "last_sync": old, "error_since": fresh})
        assert tc.get("/api/calendar").json()["status"].get("needs_auth") is True



# ---- review 2026-09-22: chores keep finished work, validate people, no future ticks ----

def _today_chores(client, pid):
    people = client.get("/api/hub").json()["people"]
    return next(p for p in people if p["person"]["id"] == pid)["chores"]


def test_pause_everyone_keeps_chores_already_done_today(client, app_mod):
    # 2026-08-26: thirteen morning check-offs vanished from that day when
    # "Pause everyone" started the same day.
    pid = _mk_person(client, "Ana")
    done_id = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    open_id = client.post("/api/admin/chores", json={
        "title": "Trash", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")                                   # today's plan is served
    assert client.post(f"/api/chores/{done_id}/complete").status_code == 200
    assert client.post("/api/admin/away/everyone", json={}).status_code == 200
    rows = {c["id"]: c for c in _today_chores(client, pid)}
    assert rows[done_id]["done"] is True                     # the finished work stays
    assert open_id not in rows                               # the rest pauses as before
    today = app_mod._today().isoformat()
    assert {r["chore_id"] for r in app_mod.fdb.day_log(app_mod._db(), today)} == {done_id}


def test_editing_a_chore_off_today_keeps_todays_check_off(client, app_mod):
    # 2026-09-12: a Saturday check-off was dropped when the chore was edited
    # to Fridays only.
    pid = _mk_person(client, "Ben")
    cid = client.post("/api/admin/chores", json={
        "title": "Clean Bedroom", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    other_day = 1 << ((app_mod._today().weekday() + 3) % 7)
    assert client.patch(f"/api/admin/chores/{cid}", json={
        "schedule_kind": "days", "days_mask": other_day}).status_code == 200
    rows = {c["id"]: c for c in _today_chores(client, pid)}
    assert rows[cid]["done"] is True
    # the kept row is locked: undo is refused and it stays done for the day
    assert client.delete(f"/api/chores/{cid}/complete").status_code == 409
    assert rows[cid]["locked"] is True


def test_a_chore_cannot_be_assigned_to_someone_who_does_not_exist(client, app_mod):
    pid = _mk_person(client)
    base = {"title": "x", "schedule_kind": "daily"}
    assert client.post("/api/admin/chores", json={**base, "assign_kind": "fixed",
                       "fixed_person_id": 999}).status_code == 422
    assert client.post("/api/admin/chores", json={**base, "assign_kind": "rotation",
                       "rotation_order": [pid, 999]}).status_code == 422
    # someone listed twice is allowed on purpose (two turns in the cycle)
    assert client.post("/api/admin/chores", json={**base, "assign_kind": "rotation",
                       "rotation_order": [pid, pid]}).status_code == 200


def test_a_future_day_cannot_be_checked_off(client, app_mod):
    pid = _mk_person(client)
    cid = client.post("/api/admin/chores", json={
        "title": "x", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    tomorrow = (app_mod._today() + dt.timedelta(days=1)).isoformat()
    r = client.post(f"/api/chores/{cid}/complete", json={"date": tomorrow})
    assert r.status_code == 422


def test_deleting_a_person_keeps_their_check_off_on_someone_elses_chore(client, app_mod):
    a = _mk_person(client, "Owner")
    b = _mk_person(client, "Helper")
    cid = client.post("/api/admin/chores", json={
        "title": "Feed dog", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": a}).json()["id"]
    client.get("/api/hub")                                   # freezes today: owned by a
    today = app_mod._today().isoformat()
    assert client.post(f"/api/chores/{cid}/complete", json={"person_id": b}).status_code == 200
    assert client.delete(f"/api/admin/people/{b}").status_code == 200
    comps = app_mod.fdb.completions_between(app_mod._db(), today, today)
    assert [(r["chore_id"], r["person_id"]) for r in comps] == [(cid, a)]


def test_a_kept_chore_is_locked_and_cannot_be_unticked(client, app_mod):
    # unticking a chore kept after a pause would drop it with no way back
    pid = _mk_person(client, "Ana")
    cid = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    # before the pause it is an ordinary done row
    assert _today_chores(client, pid)[0]["locked"] is False
    client.post("/api/admin/away/everyone", json={})
    row = next(c for c in _today_chores(client, pid) if c["id"] == cid)
    assert row["done"] is True and row["locked"] is True
    r = client.delete(f"/api/chores/{cid}/complete")
    assert r.status_code == 409
    assert next(c for c in _today_chores(client, pid) if c["id"] == cid)["done"] is True


def test_deleting_a_chore_done_today_takes_it_off_today(client, app_mod):
    pid = _mk_person(client, "Ana")
    cid = client.post("/api/admin/chores", json={
        "title": "Dishes", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    assert client.delete(f"/api/admin/chores/{cid}").status_code == 200
    assert cid not in {c["id"] for c in _today_chores(client, pid)}


def test_editing_a_chore_that_names_a_turned_off_person_still_works(client, app_mod):
    a = _mk_person(client, "Ana")
    b = _mk_person(client, "Ben")
    cid = client.post("/api/admin/chores", json={
        "title": "Trash", "schedule_kind": "daily", "assign_kind": "rotation",
        "rotation_order": [a, b]}).json()["id"]
    assert client.patch(f"/api/admin/people/{b}", json={"active": False}).status_code == 200
    assert client.patch(f"/api/admin/chores/{cid}", json={"title": "Bins"}).status_code == 200


def test_deleting_the_owner_then_the_helper_never_orphans_a_check_off(client, app_mod):
    # the hand-off must only go to an owner who still exists
    a = _mk_person(client, "Owner")
    b = _mk_person(client, "Helper")
    cid = client.post("/api/admin/chores", json={
        "title": "Feed dog", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": a}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete", json={"person_id": b})
    assert client.delete(f"/api/admin/people/{a}").status_code == 200
    assert client.delete(f"/api/admin/people/{b}").status_code == 200
    c = app_mod._db()
    orphans = c.execute("SELECT COUNT(*) FROM completions WHERE person_id NOT IN "
                        "(SELECT id FROM people)").fetchone()[0]
    assert orphans == 0


def test_a_helpers_past_check_off_keeps_the_owners_streak_when_the_helper_is_deleted(
        client, app_mod, monkeypatch):
    today = dt.date(2026, 8, 17)
    yesterday = today - dt.timedelta(days=1)
    monkeypatch.setattr(app_mod, "_today", lambda: yesterday)
    a = _mk_person(client, "Owner")
    b = _mk_person(client, "Helper")
    cid = client.post("/api/admin/chores", json={
        "title": "Feed dog", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": a, "date": None}).json()["id"]
    client.get("/api/hub")                                   # freeze yesterday onto the owner
    client.post(f"/api/chores/{cid}/complete", json={"person_id": b})
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    assert client.delete(f"/api/admin/people/{b}").status_code == 200
    comps = app_mod.fdb.completions_between(app_mod._db(), yesterday.isoformat(), yesterday.isoformat())
    assert [(r["chore_id"], r["person_id"]) for r in comps] == [(cid, a)]
    owner = next(p for p in client.get("/api/hub").json()["people"] if p["person"]["id"] == a)
    assert owner["week"][-2] == "done"


def test_undo_on_a_chore_done_today_reopens_the_mirror_under_the_logged_owner(client, app_mod, monkeypatch):
    a = _mk_person(client, "Ana")
    b = _mk_person(client, "Ben")
    cid = client.post("/api/admin/chores", json={
        "title": "Trash", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": a}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    # the plan now names someone else, but the log still says a did it
    client.patch(f"/api/admin/chores/{cid}", json={"fixed_person_id": b})
    seen = []
    monkeypatch.setattr(app_mod.chore_mirror, "push_completion",
                        lambda c, ch, d, done, expected_person_id=None: seen.append(expected_person_id))
    today = app_mod._today().isoformat()
    assert app_mod._resolved_owner(app_mod._db(), cid, today) == a
    client.delete(f"/api/chores/{cid}/complete")
    assert seen == [a]


import pytest as _pytest_sync  # noqa: E402


@_pytest_sync.mark.parametrize("prior_since, expect_since", [
    ("2026-08-11T09:00:00-07:00", "2026-08-11T09:00:00-07:00"),   # a running clock is kept
    (None, "2026-08-10T09:00:00-07:00"),                          # else dated from the last good sync
])
def test_open_sync_conn_keeps_the_error_clock_and_an_expired_sign_in(app_mod, monkeypatch, prior_since, expect_since):
    seed = app_mod.fdb.connect(app_mod.DB_PATH)
    app_mod.fdb.ensure_schema(seed)
    prior = {"ok": False, "needs_auth": True, "last_sync": "2026-08-10T09:00:00-07:00"}
    if prior_since:
        prior["error_since"] = prior_since
    app_mod.fdb.kv_set(seed, "calendar_status", prior)
    seed.close()
    real_ensure = app_mod.fdb.ensure_schema
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_ensure(conn)

    monkeypatch.setattr(app_mod.fdb, "ensure_schema", flaky)
    monkeypatch.setattr(app_mod.time, "sleep", lambda *_: None)
    conn = app_mod._open_sync_conn()
    st = app_mod.fdb.kv_get(conn, "calendar_status")
    assert st["needs_auth"] is True
    assert st["last_sync"] == "2026-08-10T09:00:00-07:00"
    assert st["error_since"] == expect_since


def test_turning_off_a_chore_done_today_takes_it_off_today(client, app_mod):
    pid = _mk_person(client, "Ana")
    cid = client.post("/api/admin/chores", json={"title": "Dishes", "schedule_kind": "daily",
        "assign_kind": "fixed", "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    assert client.patch(f"/api/admin/chores/{cid}", json={"active": False}).status_code == 200
    assert cid not in {c["id"] for c in _today_chores(client, pid)}


def test_untick_is_refused_while_the_away_list_cannot_be_read(client, app_mod, monkeypatch):
    # without the away list the server can't tell a locked row from a normal
    # one; refuse and let the tap be retried (complete() does the same)
    pid = _mk_person(client, "Ana")
    cid = client.post("/api/admin/chores", json={"title": "Dishes", "schedule_kind": "daily",
        "assign_kind": "fixed", "fixed_person_id": pid}).json()["id"]
    client.get("/api/hub")
    client.post(f"/api/chores/{cid}/complete")
    monkeypatch.setattr(app_mod, "_away_view", lambda c, d: ({}, {"ids": set(), "backup": {}}, False))
    assert client.delete(f"/api/chores/{cid}/complete").status_code == 503
    today = app_mod._today().isoformat()
    assert app_mod.fdb.completion_exists(app_mod._db(), cid, today)



def test_a_deleted_persons_fixed_chore_stays_active_to_be_reassigned(client, app_mod):
    # it shows under "No one to do these" in edit mode (JS tests cover the list);
    # here: the chore itself is kept, active and unassigned, not lost
    a = _mk_person(client, "Gone")
    cid = client.post("/api/admin/chores", json={
        "title": "Feed dog", "schedule_kind": "daily", "assign_kind": "fixed",
        "fixed_person_id": a}).json()["id"]
    client.delete(f"/api/admin/people/{a}")
    ch = next(c for c in client.get("/api/admin/state").json()["chores"] if c["id"] == cid)
    assert ch["active"] in (1, True) and ch["fixed_person_id"] is None


def test_db_rolls_back_a_transaction_left_open_on_the_thread(app_mod):
    """If a write on this thread's connection failed without rolling back,
    the next _db() must not hand out that half-open transaction: reads would
    see a frozen snapshot and the WAL could not checkpoint."""
    conn = app_mod._db()
    conn.execute("INSERT INTO kv(key, value) VALUES('stray', '1')")
    assert conn.in_transaction
    again = app_mod._db()
    assert again is conn
    assert not again.in_transaction
    assert fdb.kv_get(again, "stray") is None   # the stray write was undone


def test_async_routes_never_touch_sqlite_directly(app_mod):
    """sqlite calls block. Inside an `async def` route they stall the whole
    event loop (every other request, the laundry watcher) for as long as the
    query or a busy lock takes. A route that reads the DB is a plain `def`
    (FastAPI runs it in the thread pool) or hands the work to
    asyncio.to_thread, like /health/full."""
    import inspect
    offenders = []
    for r in app_mod.app.routes:
        ep = getattr(r, "endpoint", None)
        if ep is None or not inspect.iscoroutinefunction(ep):
            continue
        body = inspect.getsource(ep)
        if "_db()" in body or "fdb." in body:
            offenders.append(r.path)
    assert offenders == []


def test_hub_reads_integration_availability_once_per_request(client, app_mod,
                                                              monkeypatch):
    """Availability includes the iCloud credential state, which reads and
    parses a file on disk. /api/hub asked for it about six times per poll
    (every _integration_on call); it is computed once per request now."""
    calls = []
    real = app_mod.caldav_service.configured

    def counting(env=None):
        calls.append(1)
        return real(env)
    monkeypatch.setattr(app_mod.caldav_service, "configured", counting)
    assert client.get("/api/hub").status_code == 200
    assert len(calls) == 1
    # and the memo does not leak into the next request
    calls.clear()
    assert client.get("/api/hub").status_code == 200
    assert len(calls) == 1
