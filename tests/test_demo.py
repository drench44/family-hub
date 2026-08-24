"""DEMO=1 sample-wall tests.

Reloads the app with DEMO=1 against a temp DB + a demo config (weather/climate
panels present so the native cards have a slot) and proves the wall comes up
fully populated with the fake family, and that an unset DEMO seeds nothing.
"""
import datetime as dt
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from family_hub import chores as chlogic
from family_hub import db
from family_hub import demo


def _write_cfg(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138,
        "calendars": [{"id": "demo-home", "label": "Home", "color": "#5BC9F0"}],
        "cameras": [],
        "panels": [
            {"id": "weather", "label": "Weather", "url": "http://localhost/",
             "vw": 1024, "vh": 600, "full": "fit"},
            {"id": "climate", "label": "House Climate", "url": "http://localhost/",
             "vw": 1024, "vh": 600, "full": "fit"},
        ],
    }))
    return str(p)


@pytest.fixture
def demo_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    import family_hub.app as appmod
    importlib.reload(appmod)
    return appmod


@pytest.fixture
def demo_client(demo_app):
    with TestClient(demo_app.app) as c:
        yield c


def test_demo_hub_seeds_the_sample_family(demo_client):
    hub = demo_client.get("/api/hub").json()
    names = [row["person"]["name"] for row in hub["people"]]
    assert names == ["Ava", "Milo", "Ruby"], names
    milo = next(r for r in hub["people"] if r["person"]["name"] == "Milo")
    # Ava has her four chores today, three of them done (Brush the dog not),
    # plus a fifth row: Milo's daily "Feed the fish", which she's covering
    # since he's seeded away (see test_demo_seeds_an_away_person_with_backup).
    ava = next(r for r in hub["people"] if r["person"]["name"] == "Ava")
    titles = {c["title"] for c in ava["chores"]}
    assert {"Laundry", "Clean rabbit cage", "Workout", "Brush the dog",
            "Feed the fish"} <= titles
    assert ava["done_count"] == 3 and ava["total"] == 5
    assert ava["streak"] >= 1 and len(ava["week"]) == 7
    covering = next(c for c in ava["chores"] if c["title"] == "Feed the fish")
    assert covering["covering_for"] == milo["person"]["id"]
    # Milo's own two chores are scheduled off today AND he's away, so his card
    # shows the away badge (never his own chore rows) regardless.
    assert milo["chores"] == []
    assert milo["away"] is True


def test_demo_hub_has_calendar_and_todos(demo_client):
    hub = demo_client.get("/api/hub").json()
    assert hub["calendar"]["status"]["ok"] is True
    titles = {e["title"] for e in hub["calendar"]["events"]}
    assert "Guitar lesson" in titles
    # events carry the seeded rail color from calendar_colors
    guitar = next(e for e in hub["calendar"]["events"] if e["title"] == "Guitar lesson")
    assert guitar["color"] == "#5BC9F0"
    todos = hub["todos"]
    assert [t["title"] for t in todos["now"]] and len(todos["later"]) == 5
    assert hub["todos_ok"] is True


def test_demo_links_are_placeholder_cameras(demo_client):
    links = demo_client.get("/api/hub").json()["links"]
    cams = links["cameras"]
    assert [c["label"] for c in cams] == ["Front Door", "Back Yard"]
    assert all(c["demo"] is True for c in cams)
    assert all("tile" not in c for c in cams)   # no go2rtc URLs in demo


def test_demo_camera_page_is_a_four_tile_grid(demo_client):
    """The Cameras-tab 2x2 grid renders four placeholder cameras in DEMO so a
    screenshot shows a full grid; all four are placeholders with no go2rtc URLs."""
    grid = demo_client.get("/api/hub").json()["links"]["camera_page"]
    assert [c["label"] for c in grid] == ["Driveway", "Mailbox", "Back Yard", "Side Gate"]
    assert all(c["demo"] is True and "tile" not in c for c in grid)


def test_demo_weather_and_climate_tiles_are_canned(demo_client):
    wj = demo_client.get("/api/tiles/weather").json()
    assert wj["available"] is True and wj["temp"] == 74.8 and wj["uv"] == 6
    assert wj["spark"] and wj["spark_now"] == 8
    # the 5-day strip's demo data rides along too (a demo-side rename/drop would
    # silently render a strip-less card in docs/hub.png — same trap as the sky
    # fields below)
    assert len(wj["forecast"]) == 5
    assert wj["forecast"][0] == {"day": "Mon", "hi": 81, "lo": 59, "cond": "Clear & sunny"}
    # sky-scene fields ride along (a demo-side rename would silently render
    # the fallback full moon / fixed phase boundaries in demo screenshots)
    assert wj["sunrise"] == "06:15" and wj["sunset"] == "20:15"
    assert wj["moon_phase"] == "Waxing Gibbous" and wj["moon_illum"] == 68
    cj = demo_client.get("/api/tiles/climate").json()
    assert cj["available"] is True
    assert [r["name"] for r in cj["rooms"]] == \
        ["Upstairs", "Downstairs", "Garage", "Crawl Space"]


def test_demo_seed_is_idempotent_across_reopen(demo_app):
    """Re-opening the connection must not double-seed (guarded on no people)."""
    with TestClient(demo_app.app) as c:
        first = len(c.get("/api/hub").json()["people"])
    demo_app._db_initialized = False   # force the one-time seed to re-run
    with TestClient(demo_app.app) as c:
        second = len(c.get("/api/hub").json()["people"])
    assert first == 3 and second == 3


def test_demo_seed_skips_when_any_seeded_table_nonempty(demo_app, tmp_path):
    """Regression for issue #36: a real db that is people-less but holds todos or
    events must NOT be seeded or wiped by DEMO mode (the old guard checked only
    people)."""
    from family_hub import db as fdb, demo as fdemo
    c = fdb.connect(str(tmp_path / "real.db"))
    fdb.ensure_schema(c)
    fdb.add_todo(c, "Real user todo", "now")
    fdb.replace_events(c, [{"id": "real", "calendar_id": "real-cal",
                            "title": "Real event", "start_ts": "2026-08-20",
                            "end_ts": "2026-08-21", "all_day": 1}])
    assert fdemo.is_unseeded(c) is False
    demo_app._ensure_demo_seed(c)   # must be a no-op, not a wipe
    assert [t["title"] for t in fdb.list_todos(c)] == ["Real user todo"]
    assert fdb.list_events(c)[0]["title"] == "Real event"
    assert fdb.list_people(c) == []   # nothing seeded over the real data
    c.close()


def test_no_demo_env_seeds_nothing(tmp_path, monkeypatch):
    """The whole feature is gated: with DEMO unset the db stays empty."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    import family_hub.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        hub = c.get("/api/hub").json()
        # the weather/climate demo payloads are gated too: with DEMO unset and no
        # real feed configured, the tiles report unavailable, never the canned
        # 74.8 / Upstairs demo values leaking into a real deployment.
        wj = c.get("/api/tiles/weather").json()
        cj = c.get("/api/tiles/climate").json()
    assert hub["people"] == [] and hub["links"]["cameras"] == []
    assert wj.get("temp") != 74.8 and wj.get("available") is not True
    assert cj.get("available") is not True


def test_demo_integrations_list_includes_cameras_weather_climate(demo_client):
    """DEMO serves placeholder cameras and canned weather/climate (see
    test_demo_links_are_placeholder_cameras and
    test_demo_weather_and_climate_tiles_are_canned) even though the demo config
    leaves go2rtc_base/weather_base/climate_base unset (_write_cfg above sets
    none of them). The registry must reflect that reality: /api/hub's
    integrations list needs cameras/weather/climate present and enabled, or the
    layout engine and tab bar (which key off this list) hide the demo's own
    columns/tabs."""
    ids = {i["id"]: i for i in demo_client.get("/api/hub").json()["integrations"]}
    for iid in ("cameras", "weather", "climate"):
        assert iid in ids, f"{iid} missing from demo /api/hub integrations"
        assert ids[iid]["enabled"] is True, f"{iid} not enabled in demo"


def test_demo_calendar_dates_are_relative_to_today(demo_client):
    """The demo must always look current: events are dated off today, not a
    hardcoded/stale date that would fall out of the calendar window over time."""
    hub = demo_client.get("/api/hub").json()
    today = dt.date.fromisoformat(hub["date"])
    guitar = next(e for e in hub["calendar"]["events"] if e["title"] == "Guitar lesson")
    assert guitar["start_ts"][:10] == (today + dt.timedelta(days=3)).isoformat()


def test_demo_never_clobbers_an_existing_real_db(tmp_path, monkeypatch):
    """DEMO=1 accidentally pointed at a real family's db must not seed into it
    or wipe it: the empty-db guard protects the real people already there."""
    db = tmp_path / "hub.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    # pre-populate one real person straight into the db
    import family_hub.db as fdb
    conn = fdb.connect(str(db))
    fdb.ensure_schema(conn)
    fdb.add_person(conn, "RealKid", "#123456")
    conn.close()
    # now open the SAME db under DEMO=1
    monkeypatch.setenv("DEMO", "1")
    import family_hub.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        names = [r["person"]["name"] for r in c.get("/api/hub").json()["people"]]
    assert names == ["RealKid"]   # untouched; no Ava/Milo/Ruby seeded in


def test_partial_demo_seed_is_wiped_so_the_next_open_retries(tmp_path, monkeypatch):
    """A seed that raises partway must leave the db EMPTY (not half-populated),
    so the empty-db guard fires again and the next open re-seeds cleanly."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    import family_hub.app as appmod
    importlib.reload(appmod)
    import family_hub.db as fdb

    def flaky_seed(conn, today):
        fdb.add_person(conn, "Ava", "#E86A9E")   # commits a partial row, then dies
        raise RuntimeError("boom")
    monkeypatch.setattr(appmod.fdemo, "seed_demo", flaky_seed)

    # first open: the seed fails, so the request 500s...
    with TestClient(appmod.app, raise_server_exceptions=False) as c:
        assert c.get("/api/hub").status_code == 500
    # ...but the half-written person was wiped back out, leaving an empty db.
    probe = fdb.connect(str(tmp_path / "hub.db"))
    assert probe.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
    probe.close()


def test_demo_seeds_an_away_person_with_backup(tmp_path):
    """Task 8: the DEMO seed marks one demo person away with a real backup, so
    the away card and 'covering for' tag are visible with no real data."""
    c = db.connect(str(tmp_path / "t.db"))
    db.ensure_schema(c)
    demo.seed_demo(c, dt.date(2026, 8, 17))
    periods = db.list_away_periods(c)
    assert periods, "demo should seed at least one away period"
    assert any(p["backup_person_id"] is not None for p in periods)
    c.close()


def test_demo_away_state_is_coherent_for_today(tmp_path):
    """Row-count-of-away_periods is not enough: prove the seed is actually
    coherent by resolving today's live plan (the same call app.py's
    _people_day makes for d == today) and checking the away person has no rows
    of their own while the backup carries a row tagged covering_for back to
    them."""
    c = db.connect(str(tmp_path / "t.db"))
    db.ensure_schema(c)
    today = dt.date(2026, 8, 17)
    demo.seed_demo(c, today)

    people = db.list_people(c)
    by_name = {p["name"]: p["id"] for p in people}
    milo_id, ava_id = by_name["Milo"], by_name["Ava"]

    periods = db.list_away_periods(c)
    away_period = next(p for p in periods if p["person_id"] == milo_id)
    assert away_period["backup_person_id"] == ava_id
    assert away_period["end_date"] is None    # still away today and onward

    today_str = today.isoformat()
    amap = db.away_map(c, today_str, today_str)
    # via the shared helper, exactly as app._away_view and the mirror do -- an
    # inline reshape here could pass while the real render diverged
    away_view = chlogic.away_view_on(amap, today_str)
    assert away_view["ids"] == {milo_id}
    rows = chlogic.plan_rows(db.list_chores(c), people, today, away_view)

    milo_rows = [r for r in rows if r["person_id"] == milo_id]
    assert milo_rows == [], "away person must have no own rows on an away day"
    covering = [r for r in rows if r["covering_for"] == milo_id]
    assert covering, "the backup must carry at least one covering row"
    assert all(r["person_id"] == ava_id for r in covering)
    c.close()


def test_demo_laundry_tile_is_canned_and_live_shaped(demo_client):
    """DEMO serves a canned laundry tile with no HA hit: the washer mid-cycle
    (finishing in the future) and the dryer freshly done — both signature
    card states for a screenshot. The integration is also forced available so
    the panel and settings row show."""
    import datetime as dt
    t = demo_client.get("/api/tiles/laundry").json()
    assert t["available"] is True
    w, d = t["machines"]
    assert (w["id"], w["kind"], w["phase"]) == ("washer", "washer", "running")
    assert dt.datetime.fromisoformat(w["finishes_at"]) \
        > dt.datetime.now(dt.timezone.utc)
    assert (d["id"], d["kind"], d["phase"]) == ("dryer", "dryer", "done")
    assert d["last_done"] == d["status_since"]
    ids = {i["id"]: i for i in
           demo_client.get("/api/hub").json()["integrations"]}
    assert "laundry" in ids and ids["laundry"]["enabled"] is True


def test_demo_laundry_log_is_canned_and_live_shaped(demo_client):
    """DEMO serves a canned cycle log matching the live endpoint's row shape
    (both signature rows: an observed finish and a missed_finish), no DB.
    The expected keys come from a REAL row, not a hardcoded copy — so a
    live-schema change that forgets the demo fails here in either
    direction."""
    from family_hub import db as fdb
    c = fdb.connect(":memory:")
    fdb.ensure_schema(c)
    fdb.laundry_log_add(c, "washer", "running", "done", "end", None, None)
    real_keys = set(fdb.laundry_log_recent(c)[0])
    c.close()
    entries = demo_client.get("/api/laundry/log").json()["entries"]
    assert entries, "demo cycle log must not be empty"
    assert all(set(e) == real_keys for e in entries)
    notes = {e["note"] for e in entries}
    assert None in notes and "missed_finish" in notes
