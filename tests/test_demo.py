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
    # Ava has her four chores today, three of them done (Brush the dog not).
    ava = next(r for r in hub["people"] if r["person"]["name"] == "Ava")
    titles = {c["title"] for c in ava["chores"]}
    assert {"Laundry", "Clean rabbit cage", "Workout", "Brush the dog"} <= titles
    assert ava["done_count"] == 3 and ava["total"] == 4
    assert ava["streak"] >= 1 and len(ava["week"]) == 7
    # Milo's chores are scheduled off today, so his card is empty today.
    milo = next(r for r in hub["people"] if r["person"]["name"] == "Milo")
    assert milo["chores"] == []


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


def test_demo_weather_and_climate_tiles_are_canned(demo_client):
    wj = demo_client.get("/api/tiles/weather").json()
    assert wj["available"] is True and wj["temp"] == 74.8 and wj["uv"] == 6
    assert wj["spark"] and wj["spark_now"] == 8
    cj = demo_client.get("/api/tiles/climate").json()
    assert cj["available"] is True
    assert [r["name"] for r in cj["rooms"]] == \
        ["Upstairs", "Downstairs", "Garage", "Crawl Space"]


def test_demo_seed_is_idempotent_across_reopen(demo_app):
    """Re-opening the connection must not double-seed (guarded on no people)."""
    with TestClient(demo_app.app) as c:
        first = len(c.get("/api/hub").json()["people"])
    demo_app._conn = None   # force a fresh connect on the next open
    with TestClient(demo_app.app) as c:
        second = len(c.get("/api/hub").json()["people"])
    assert first == 3 and second == 3


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
