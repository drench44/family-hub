"""The in-app backup job wired into the app: the Backups switch, the
/api/backups routes, the background task, and the wall's backup badge."""
import asyncio
import datetime as dt
import importlib
import json
import os
import sqlite3

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
    """Give the database enough rows to clear the snapshot's size floor."""
    c = app_mod._db()
    fdb.kv_set(c, "filler", "x" * 20000)


def test_backups_is_a_feature_in_the_registry(client):
    rows = {i["id"]: i for i in client.get("/api/integrations").json()["integrations"]}
    assert rows["backups"]["group"] == "feature" and rows["backups"]["enabled"] is True
    assert rows["backups"]["name"] == "Backups"


def test_state_with_a_folder_and_no_runs_yet(client):
    st = client.get("/api/backups").json()
    assert st["enabled"] is True and st["why_off"] is None
    assert st["dir"] == "/mnt/nas/family-hub"
    assert st["scheduled"] is False            # DISABLE_SYNC: no hourly runs here
    assert st["last_ok"] is None and st["last_error"] is None
    assert st["counts"] == {"hourly": 0, "daily": 0, "weekly": 0, "monthly": 0, "settings": 0}
    assert st["keep"] == {"hourly": 48, "daily": 14, "weekly": 8, "monthly": 12, "settings": 14}
    assert st["password_set"] is False and st["password_problem"] is False
    assert st["running"] is False


def test_back_up_now_writes_a_snapshot_and_the_wall_badge_sees_it(client, app_mod, tmp_path):
    _bulk(app_mod)
    assert client.get("/api/hub").json()["backup"]["known"] is False
    r = client.post("/api/backups/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["ok"] is True
    snap = tmp_path / "nas" / "db" / "hourly" / body["result"]["snapshot"]
    c = sqlite3.connect(snap)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    assert body["state"]["last_ok"]["snapshot"] == snap.name
    assert body["state"]["counts"]["hourly"] == 1
    badge = client.get("/api/hub").json()["backup"]
    assert badge["known"] is True and badge["stale"] is False
    beat = fdb.kv_get(app_mod._db(), "backup_status")
    assert beat["source"] == "in-app" and beat["dir"] == "/mnt/nas/family-hub"


def test_the_snapshot_is_named_by_house_time(client, app_mod, monkeypatch):
    _bulk(app_mod)
    fixed = dt.datetime(2026, 8, 18, 9, 30, tzinfo=app_mod.TZ)
    monkeypatch.setattr(app_mod, "_now_local", lambda: fixed)
    assert client.post("/api/backups/run").json()["result"]["snapshot"] == "hub-20260818-0930.db"


def test_a_failed_run_says_why_and_the_badge_keeps_the_last_good_time(client, app_mod,
                                                                       monkeypatch):
    _bulk(app_mod)
    assert client.post("/api/backups/run").json()["result"]["ok"]
    good_at = fdb.kv_get(app_mod._db(), "backup_status")["at"]
    monkeypatch.setattr(fb, "MIN_BYTES", 10 ** 9)
    body = client.post("/api/backups/run").json()
    assert body["result"]["ok"] is False
    assert "too small" in body["state"]["last_error"]["message"]
    beat = fdb.kv_get(app_mod._db(), "backup_status")
    assert beat["at"] == good_at and "too small" in beat["in_app_error"]


def test_back_up_now_while_one_runs_is_refused(client, app_mod):
    assert app_mod._backup_lock.acquire(blocking=False)
    try:
        r = client.post("/api/backups/run")
        assert r.status_code == 409
        assert "already" in r.json()["detail"]
        assert client.get("/api/backups").json()["running"] is True
    finally:
        app_mod._backup_lock.release()


def test_switched_off_means_no_runs_and_says_so(client, app_mod):
    assert client.patch("/api/integrations/backups", json={"enabled": False}).status_code == 200
    st = client.get("/api/backups").json()
    assert st["enabled"] is False and st["why_off"] == "switched_off"
    r = client.post("/api/backups/run")
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert app_mod._backup_tick() is None


def test_no_folder_means_off(app_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "BACKUPS_PATH", str(tmp_path / "gone"))
    with TestClient(app_mod.app) as client:
        st = client.get("/api/backups").json()
        assert st["enabled"] is False and st["why_off"] == "no_folder"
        assert st["counts"] is None
        assert client.post("/api/backups/run").status_code == 409


def test_the_default_folder_must_be_mounted_in(app_mod, monkeypatch, tmp_path):
    """/backups that is just a folder in the image would lose every copy
    with the container, so the default path only counts once mounted."""
    monkeypatch.setattr(app_mod, "_BACKUPS_PATH_SET", False)
    with TestClient(app_mod.app) as client:
        assert client.get("/api/backups").json()["why_off"] == "not_mounted"


def test_password_needs_eight_characters(client):
    r = client.post("/api/backups/password", json={"password": "short"})
    assert r.status_code == 422
    assert client.get("/api/backups").json()["password_set"] is False


def test_password_then_back_up_now_saves_the_settings_files(client, app_mod, tmp_path):
    _bulk(app_mod)
    from family_hub import parent_pin
    open(parent_pin.KEY_PATH, "wb").write(b"k" * 32)
    r = client.post("/api/backups/password", json={"password": "family hub 2026"})
    assert r.status_code == 200 and r.json()["password_set"] is True
    key = tmp_path / "data" / "backup.key"
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    body = client.post("/api/backups/run").json()
    name = body["result"]["settings_file"]
    assert name and body["state"]["settings"]["file"] == name
    blob = (tmp_path / "nas" / "settings" / name).read_bytes()
    files = fb.unpack_settings(fb.decrypt_bundle(blob, password="family hub 2026"))
    assert files["env"] == b"HA_TOKEN=abc123\n"
    assert files["config.json"] == (tmp_path / "config.json").read_bytes()
    assert files["pin.key"] == b"k" * 32
    assert fdb.kv_get(app_mod._db(), "backup_status")["settings_at"]


def test_the_password_never_reaches_the_database_or_the_log(client, app_mod, caplog):
    client.post("/api/backups/password", json={"password": "family hub 2026"})
    dump = "\n".join(str(v) for _, v in app_mod._db().execute("SELECT key, value FROM kv"))
    assert "family hub 2026" not in dump
    assert "family hub 2026" not in caplog.text


def test_password_needs_the_parent_pin_when_one_is_set(client):
    assert client.post("/api/rewards/pin", json={"pin": "2468"}).status_code == 204
    r = client.post("/api/backups/password", json={"password": "family hub 2026"})
    assert r.status_code == 403 and r.json()["detail"] == {"pin": "required"}
    r = client.post("/api/backups/password", json={"password": "family hub 2026"},
                    headers={"X-Parent-Pin": "2468"})
    assert r.status_code == 200 and r.json()["password_set"] is True


def test_a_lost_key_file_is_a_problem_the_card_shows(client, app_mod, tmp_path):
    _bulk(app_mod)
    client.post("/api/backups/password", json={"password": "family hub 2026"})
    os.remove(tmp_path / "data" / "backup.key")
    st = client.get("/api/backups").json()
    assert st["password_set"] is False and st["password_problem"] is True
    body = client.post("/api/backups/run").json()
    assert body["result"]["ok"] is True             # the database copy still happens
    assert "password" in body["state"]["settings_error"]


def test_the_background_task_runs_only_where_sync_runs(app_mod, monkeypatch):
    assert app_mod._backup_loop_enabled() is False      # DISABLE_SYNC
    monkeypatch.delenv("DISABLE_SYNC")
    assert app_mod._backup_loop_enabled() is True
    monkeypatch.setattr(app_mod, "DEMO", True)
    assert app_mod._backup_loop_enabled() is False


def test_the_tick_runs_a_backup_when_on(client, app_mod):
    _bulk(app_mod)
    r = app_mod._backup_tick()
    assert r and r["ok"] is True


def test_the_tick_skips_while_a_manual_run_holds_the_lock(client, app_mod):
    assert app_mod._backup_lock.acquire(blocking=False)
    try:
        assert app_mod._backup_tick() is None
    finally:
        app_mod._backup_lock.release()


def test_the_loop_survives_a_crashing_tick(app_mod, monkeypatch):
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(app_mod, "_backup_tick", tick)
    monkeypatch.setattr(app_mod, "BACKUP_FIRST_S", 0)
    monkeypatch.setattr(app_mod, "BACKUP_EVERY_S", 0.01)

    async def go():
        task = asyncio.create_task(app_mod.backup_loop())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 3:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert len(calls) >= 3


def test_lifespan_starts_and_stops_the_task(app_mod, monkeypatch):
    started = []

    async def fake_loop():
        started.append(1)
        await asyncio.sleep(3600)
    monkeypatch.setattr(app_mod, "backup_loop", fake_loop)
    monkeypatch.setattr(app_mod, "_backup_loop_enabled", lambda: True)
    with TestClient(app_mod.app):
        pass
    assert started == [1]


def test_demo_serves_a_live_shaped_card(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "DEMO", True)
    with TestClient(app_mod.app) as client:
        st = client.get("/api/backups").json()
        assert st["enabled"] is True and st["last_ok"]["snapshot"].startswith("hub-")
        at = dt.datetime.fromisoformat(st["last_ok"]["at"])
        assert dt.timedelta(0) < dt.datetime.now(dt.timezone.utc) - at < dt.timedelta(hours=1)
        assert client.post("/api/backups/run").status_code == 409
        assert client.post("/api/backups/password",
                           json={"password": "family hub 2026"}).status_code == 409
