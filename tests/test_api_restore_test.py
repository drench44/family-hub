"""The restore test wired into the app: Test restore now, the hourly loop's
monthly run, the card's state, and the /health/full note."""
import datetime as dt
import importlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from family_hub import backups as fb
from family_hub import db as fdb


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"calendars": []}))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("BACKUPS_PATH", str(tmp_path / "nas"))
    monkeypatch.setenv("BACKUP_DIR", "/mnt/nas/family-hub")
    monkeypatch.setenv("SETTINGS_ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("BACKUP_KEY_PATH", raising=False)
    (tmp_path / "nas").mkdir()
    (tmp_path / ".env").write_text("HA_TOKEN=abc123\n")
    import family_hub.app as appmod
    importlib.reload(appmod)
    return appmod


@pytest.fixture
def client(app_mod):
    with TestClient(app_mod.app) as c:
        yield c


def _bulk(app_mod):
    c = app_mod._db()
    fdb.kv_set(c, "filler", "x" * 20000)
    c.execute("INSERT INTO people(name, color) VALUES('A', '#fff')")
    c.commit()


def _backed_up(client, app_mod):
    _bulk(app_mod)
    r = client.post("/api/backups/run").json()["result"]
    assert r["ok"], r
    return r


def _snap(tmp_path, name):
    return tmp_path / "nas" / "db" / "hourly" / name


def test_test_restore_now_passes_and_the_card_shows_it(client, app_mod):
    r = _backed_up(client, app_mod)
    assert client.get("/api/backups").json()["restore_test"] is None
    res = client.post("/api/backups/restore-test")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["result"]["ok"] is True and body["result"]["snapshot"] == r["snapshot"]
    rt = body["state"]["restore_test"]
    assert rt["ok"] is True and rt["snapshot"] == r["snapshot"] and rt["reason"] is None
    assert client.get("/api/backups").json()["restore_test"] == rt


def test_the_settings_bundle_is_checked_with_the_files_the_backup_recorded(client, app_mod):
    _bulk(app_mod)
    client.post("/api/backups/password", json={"password": "family hub 2026"})
    r = client.post("/api/backups/run").json()["result"]
    assert r["settings_file"]
    st = fdb.kv_get(app_mod._db(), fb.STATE_KEY)
    assert "config.json" in st["settings"]["files"] and "env" in st["settings"]["files"]
    body = client.post("/api/backups/restore-test").json()
    assert body["result"]["ok"] is True and body["result"]["settings_checked"] is True
    # a settings file the last backup recorded and the newest bundle lacks
    st["settings"]["files"].append("token.json")
    fdb.kv_set(app_mod._db(), fb.STATE_KEY, st)
    body = client.post("/api/backups/restore-test").json()
    assert body["result"]["ok"] is False and "token.json" in body["result"]["reason"]


def test_a_failed_test_shows_why_and_never_moves_the_heartbeat(client, app_mod, tmp_path):
    r = _backed_up(client, app_mod)
    c = app_mod._db()
    beat, state = fdb.kv_get(c, fb.HEARTBEAT_KEY), fdb.kv_get(c, fb.STATE_KEY)
    _snap(tmp_path, r["snapshot"]).write_bytes(b"junk" * 5000)
    body = client.post("/api/backups/restore-test").json()
    assert body["result"]["ok"] is False
    rt = body["state"]["restore_test"]
    assert rt["ok"] is False and "can't be opened as a database" in rt["reason"]
    assert fdb.kv_get(c, fb.HEARTBEAT_KEY) == beat
    assert fdb.kv_get(c, fb.STATE_KEY) == state
    assert client.get("/api/hub").json()["backup"]["stale"] is False


def test_test_restore_now_with_no_backup_yet_is_refused(client):
    r = client.post("/api/backups/restore-test")
    assert r.status_code == 409 and r.json()["detail"] == (
        "There is no backup to test yet. Back up now first.")


def test_test_restore_now_when_off_is_refused(client, app_mod):
    _backed_up(client, app_mod)
    client.patch("/api/integrations/backups", json={"enabled": False})
    r = client.post("/api/backups/restore-test")
    assert r.status_code == 409 and "switched off" in r.json()["detail"]


def test_test_restore_now_shares_the_backup_lock(client, app_mod):
    _backed_up(client, app_mod)
    assert app_mod._backup_lock.acquire(blocking=False)
    try:
        r = client.post("/api/backups/restore-test")
        assert r.status_code == 409 and "already running" in r.json()["detail"]
    finally:
        app_mod._backup_lock.release()


def test_demo_refuses_and_shows_a_passed_test(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "DEMO", True)
    with TestClient(app_mod.app) as client:
        st = client.get("/api/backups").json()
        rt = st["restore_test"]
        assert rt["ok"] is True and rt["snapshot"].startswith("hub-") and rt["reason"] is None
        at = dt.datetime.fromisoformat(rt["at"])
        assert dt.timedelta(0) < dt.datetime.now(dt.timezone.utc) - at < dt.timedelta(days=30)
        assert client.post("/api/backups/restore-test").status_code == 409


# --- the hourly loop's monthly run -----------------------------------------------------

def _age_first_ok(app_mod, days):
    c = app_mod._db()
    st = fdb.kv_get(c, fb.STATE_KEY)
    st["first_ok_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    fdb.kv_set(c, fb.STATE_KEY, st)


def test_the_tick_waits_a_day_after_the_first_good_backup(client, app_mod):
    _bulk(app_mod)
    assert app_mod._backup_tick()["ok"] is True
    assert fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY) is None     # not a day yet
    _age_first_ok(app_mod, 2)
    assert app_mod._backup_tick()["ok"] is True
    rt = fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY)
    assert rt and rt["ok"] is True
    # tested the copy this tick just made
    assert rt["snapshot"] == fdb.kv_get(app_mod._db(), fb.STATE_KEY)["last_ok"]["snapshot"]
    app_mod._backup_tick()
    assert fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY)["at"] == rt["at"], "not again for a month"


def test_the_tick_tests_again_after_thirty_days(client, app_mod):
    _bulk(app_mod)
    app_mod._backup_tick()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat()
    fdb.kv_set(app_mod._db(), fb.RESTORE_TEST_KEY,
               {"at": old, "ok": True, "snapshot": "hub-x.db", "reason": None})
    app_mod._backup_tick()
    rt = fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY)
    assert rt["at"] != old and rt["ok"] is True


def test_the_tick_does_not_test_when_backups_are_off(client, app_mod):
    _bulk(app_mod)
    app_mod._backup_tick()
    _age_first_ok(app_mod, 2)
    client.patch("/api/integrations/backups", json={"enabled": False})
    assert app_mod._backup_tick() is None
    assert fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY) is None


def test_a_crashing_restore_test_never_breaks_the_backup(client, app_mod, monkeypatch, caplog):
    _bulk(app_mod)
    app_mod._backup_tick()
    _age_first_ok(app_mod, 2)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(fb, "restore_test", boom)
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        r = app_mod._backup_tick()
    assert r and r["ok"] is True
    assert any("restore test" in m for m in caplog.messages)


def test_a_failed_scheduled_test_logs_once(client, app_mod, monkeypatch, caplog):
    _bulk(app_mod)
    app_mod._backup_tick()
    _age_first_ok(app_mod, 2)
    real = fb.restore_test

    def stale(out_dir, **kw):              # the live hub has far more people than the copy
        return real(out_dir, **{**kw, "live": {**kw["live"], "people": 50}})
    monkeypatch.setattr(fb, "restore_test", stale)
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        app_mod._backup_tick()
        rt = fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY)
        assert rt["ok"] is False and "but the hub has 50" in rt["reason"]
        # two days on it retries, still failing for the same reason
        rt["at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        fdb.kv_set(app_mod._db(), fb.RESTORE_TEST_KEY, rt)
        app_mod._backup_tick()
    assert sum("restore test failed" in m for m in caplog.messages) == 1
    assert fdb.kv_get(app_mod._db(), fb.RESTORE_TEST_KEY)["at"] != rt["at"]


# --- /health/full ----------------------------------------------------------------------

def test_health_full_notes_the_restore_test_without_failing(client, app_mod, tmp_path):
    r = _backed_up(client, app_mod)
    body = client.get("/health/full").json()
    assert body["notes"]["backups_restore_test"] is None
    _snap(tmp_path, r["snapshot"]).write_bytes(b"junk" * 5000)
    client.post("/api/backups/restore-test")
    body = client.get("/health/full").json()
    note = body["notes"]["backups_restore_test"]
    assert note["ok"] is False and "can't be opened" in note["reason"]
    assert note["at"] and note["snapshot"] == r["snapshot"]
    assert not any("restore" in p for p in body.get("problems", []))
