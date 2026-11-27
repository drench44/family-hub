"""The in-app backup job (src/family_hub/backups.py).

It ports backup/family-hub-backup.sh into the app so a hub installed from
GitHub backs itself up with no host timer: the same snapshot names and
stamps, the same verify step, the same tiers and keep counts, a tier never
emptied, temp file then atomic rename, and leftovers from a killed run swept.
On top of that it saves the settings files, encrypted with a backup password.
"""
import datetime as dt
import io
import json
import logging
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from family_hub import backups as fb
from family_hub import db as fdb

NOW = dt.datetime(2026, 8, 18, 9, 30)


def _make_db(path: Path, rows=3, wal=False):
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("create table todos (id integer primary key, title text)")
    c.execute("create table kv (key text primary key, value text)")
    c.executemany("insert into todos (title) values (?)",
                  [(f"t{i}",) for i in range(rows)])
    c.commit()
    return c


def _sources(tmp_path, **present):
    """Settings sources in a temp folder; the named ones get content."""
    names = {"config.json": "config.json", "env": ".env", "pin.key": "pin.key",
             "wyze.env": "wyze.env", "token.json": "token.json"}
    out = {}
    for short, fname in names.items():
        p = tmp_path / "src" / fname
        p.parent.mkdir(exist_ok=True)
        if short in present:
            p.write_bytes(present[short])
        out[short] = str(p)
    return out


def _run(tmp_path, db, now=NOW, sources=None, **kw):
    return fb.run_once(now, db_path=str(db), out_dir=str(tmp_path / "out"),
                       sources=sources or {}, key_path=str(tmp_path / "backup.key"),
                       **kw)


# --- stamps and pruning (pure) ----------------------------------------------

def test_stamps_match_the_box_script():
    assert fb.stamps(NOW) == {"hourly": "20260818-0930", "daily": "20260818",
                              "weekly": "2026-W34", "monthly": "202608"}


def test_weekly_stamp_uses_the_iso_year_across_new_year():
    # 2027-01-01 is a Friday in ISO week 53 of 2026
    assert fb.stamps(dt.datetime(2027, 1, 1, 0, 5))["weekly"] == "2026-W53"


def test_keep_counts_match_the_box_script():
    assert fb.KEEP == {"hourly": 48, "daily": 14, "weekly": 8, "monthly": 12}
    assert fb.SETTINGS_KEEP == 14


def test_prune_keeps_the_newest_by_the_stamp_in_the_name():
    names = ["hub-20260818-0930.db", "hub-20260818-0730.db", "hub-20260818-0830.db",
             "notes.txt", ".partial-20260818-0930.db"]
    assert fb.prune_names(names, 2) == ["hub-20260818-0730.db"]


def test_prune_orders_weeks_across_the_new_year():
    names = ["hub-2026-W52.db", "hub-2026-W53.db", "hub-2027-W01.db"]
    assert fb.prune_names(names, 2) == ["hub-2026-W52.db"]


@pytest.mark.parametrize("keep", [0, -1, "3", None, True])
def test_prune_refuses_a_keep_that_could_empty_a_tier(keep):
    with pytest.raises(ValueError):
        fb.prune_names(["hub-20260818.db"], keep)


def test_prune_matches_settings_bundles_too():
    names = [f"settings-20260818-{h:02d}00.fhb" for h in range(5)]
    assert fb.prune_names(names, 3, prefix="settings-", suffix=".fhb") == \
        ["settings-20260818-0100.fhb", "settings-20260818-0000.fhb"]


# --- the database snapshot ----------------------------------------------------

def test_one_run_writes_a_verified_snapshot_into_every_tier(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    r = _run(tmp_path, db)
    assert r["ok"] is True, r
    assert r["snapshot"] == "hub-20260818-0930.db"
    out = tmp_path / "out" / "db"
    for tier, stamp in fb.stamps(NOW).items():
        snap = out / tier / f"hub-{stamp}.db"
        assert snap.exists(), tier
        c = sqlite3.connect(snap)
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("select count(*) from todos").fetchone()[0] == 3
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        c.close()
    assert r["bytes"] == (out / "hourly" / "hub-20260818-0930.db").stat().st_size


def test_snapshot_of_a_live_wal_db_holds_uncheckpointed_rows_and_no_sidecars(tmp_path):
    db = tmp_path / "hub.db"
    live = _make_db(db, wal=True)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("insert into todos (title) values ('only in the wal')")
    live.commit()
    r = _run(tmp_path, db)
    live.close()
    assert r["ok"], r
    hourly = tmp_path / "out" / "db" / "hourly"
    assert sorted(p.name for p in hourly.iterdir()) == ["hub-20260818-0930.db"]
    c = sqlite3.connect(hourly / "hub-20260818-0930.db")
    assert c.execute("select count(*) from todos").fetchone()[0] == 4
    c.close()


def test_hourly_tier_prunes_to_its_keep_count(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    for h in range(5):
        assert _run(tmp_path, db, now=NOW.replace(hour=h),
                    keep={**fb.KEEP, "hourly": 3})["ok"]
    names = sorted(p.name for p in (tmp_path / "out/db/hourly").glob("hub-*.db"))
    assert names == ["hub-20260818-0230.db", "hub-20260818-0330.db", "hub-20260818-0430.db"]
    # one day, so the daily tier still holds its one latest copy
    assert [p.name for p in (tmp_path / "out/db/daily").glob("hub-*.db")] == ["hub-20260818.db"]


def test_coarser_tier_holds_the_latest_copy_for_its_period(tmp_path):
    db = tmp_path / "hub.db"
    c = _make_db(db)
    _run(tmp_path, db, now=NOW.replace(hour=1))
    c.execute("insert into todos (title) values ('later')")
    c.commit()
    c.close()
    _run(tmp_path, db, now=NOW.replace(hour=2))
    daily = sqlite3.connect(tmp_path / "out/db/daily/hub-20260818.db")
    assert daily.execute("select count(*) from todos").fetchone()[0] == 4
    daily.close()


def test_missing_source_fails_and_writes_nothing(tmp_path):
    r = _run(tmp_path, tmp_path / "nope.db")
    assert r["ok"] is False
    assert "database" in r["error"].lower()
    assert not list((tmp_path / "out").rglob("hub-*.db"))


def test_corrupt_source_fails_and_leaves_no_partial(tmp_path):
    db = tmp_path / "hub.db"
    db.write_bytes(b"this is not a database" * 1000)
    r = _run(tmp_path, db)
    assert r["ok"] is False
    assert not [p for p in (tmp_path / "out").rglob("*") if p.is_file()]


def test_undersized_snapshot_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    monkeypatch.setattr(fb, "MIN_BYTES", 10 ** 9)
    r = _run(tmp_path, db)
    assert r["ok"] is False and "small" in r["error"]
    assert not [p for p in (tmp_path / "out").rglob("*") if p.is_file()]


def test_leftovers_from_a_killed_run_are_swept(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    for tier in fb.TIERS:
        d = tmp_path / "out" / "db" / tier
        d.mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            (d / f".partial-old.db{suffix}").write_bytes(b"x")
    s = tmp_path / "out" / "settings"
    s.mkdir(parents=True)
    (s / ".partial-old.fhb").write_bytes(b"x")
    assert _run(tmp_path, db)["ok"]
    assert not list((tmp_path / "out").rglob(".partial-*"))


def test_a_failed_promote_never_truncates_the_old_copy(tmp_path, monkeypatch):
    """Promotion copies to a temp file and renames, so a crash mid-copy
    leaves the previous good copy for that period standing."""
    db = tmp_path / "hub.db"
    _make_db(db).close()
    assert _run(tmp_path, db, now=NOW.replace(hour=1))["ok"]
    daily = tmp_path / "out/db/daily/hub-20260818.db"
    before = daily.read_bytes()

    def boom(src, dst, *a, **k):
        Path(dst).write_bytes(b"half")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(fb.shutil, "copyfile", boom)
    r = _run(tmp_path, db, now=NOW.replace(hour=2))
    assert r["ok"] is False and "No space left" in r["error"]
    assert daily.read_bytes() == before
    assert not list((tmp_path / "out").rglob(".partial-*"))


def test_other_files_in_the_folder_are_never_touched(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    hourly = tmp_path / "out/db/hourly"
    hourly.mkdir(parents=True)
    (hourly / "keep-me.txt").write_text("mine")
    for h in range(4):
        _run(tmp_path, db, now=NOW.replace(hour=h), keep={**fb.KEEP, "hourly": 1})
    assert (hourly / "keep-me.txt").read_text() == "mine"


# --- the settings bundle ------------------------------------------------------

def test_encrypt_then_decrypt_round_trips():
    salt = b"s" * 16
    key = fb.derive_key("correct horse", salt)
    blob = fb.encrypt_bundle(b"secret stuff", key, salt)
    assert blob[:4] == b"FHB1" and blob[4:20] == salt
    assert b"secret stuff" not in blob
    assert fb.decrypt_bundle(blob, password="correct horse") == b"secret stuff"
    assert fb.decrypt_bundle(blob, key=key) == b"secret stuff"


def test_derive_key_uses_scrypt_with_the_designed_cost():
    import hashlib
    salt = bytes(range(16))
    want = hashlib.scrypt(b"pw", salt=salt, n=2 ** 15, r=8, p=1, dklen=32,
                          maxmem=64 * 1024 * 1024)
    assert fb.derive_key("pw", salt) == want


def test_a_wrong_password_or_a_changed_byte_is_refused():
    salt = b"t" * 16
    key = fb.derive_key("right password", salt)
    blob = fb.encrypt_bundle(b"data", key, salt)
    with pytest.raises(fb.BundleError, match="password"):
        fb.decrypt_bundle(blob, password="wrong password")
    for i in (2, 10, 25, len(blob) - 1):           # magic, salt, nonce, tag
        bad = bytearray(blob)
        bad[i] ^= 1
        with pytest.raises(fb.BundleError):
            fb.decrypt_bundle(bytes(bad), password="right password")
    with pytest.raises(fb.BundleError, match="not a"):
        fb.decrypt_bundle(b"nope", password="right password")


def test_every_bundle_gets_a_fresh_nonce():
    salt = b"u" * 16
    key = fb.derive_key("pw-pw-pw", salt)
    a, b = fb.encrypt_bundle(b"same", key, salt), fb.encrypt_bundle(b"same", key, salt)
    assert a[20:32] != b[20:32]


def test_pack_and_unpack_settings_keep_only_known_names():
    packed = fb.pack_settings({"config.json": b"{}", "env": b"A=1\n"})
    assert fb.unpack_settings(packed) == {"config.json": b"{}", "env": b"A=1\n"}
    with pytest.raises(ValueError):
        fb.pack_settings({"../etc/passwd": b"x"})
    # a tar with a path outside the known names is refused on the way in too
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("../../evil")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(fb.BundleError):
        fb.unpack_settings(buf.getvalue())


def test_pack_is_deterministic():
    files = {"config.json": b"{}", "pin.key": b"k" * 32}
    assert fb.pack_settings(files) == fb.pack_settings(dict(reversed(files.items())))


def test_settings_are_skipped_without_a_password(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    r = _run(tmp_path, db, sources=_sources(tmp_path, **{"config.json": b"{}"}))
    assert r["ok"] and r["settings_file"] is None and r["settings_error"] is None
    assert not (tmp_path / "out" / "settings").exists() or \
        not list((tmp_path / "out" / "settings").iterdir())


def test_with_a_password_the_settings_files_are_saved_encrypted(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    src = _sources(tmp_path, **{"config.json": b'{"calendars": []}',
                                "env": b"HA_TOKEN=abc123\n",
                                "pin.key": b"p" * 32})
    r = _run(tmp_path, db, sources=src)
    assert r["ok"] and r["settings_error"] is None, r
    assert r["settings_file"] == "settings-20260818-0930.fhb"
    blob = (tmp_path / "out" / "settings" / r["settings_file"]).read_bytes()
    assert b"abc123" not in blob
    files = fb.unpack_settings(fb.decrypt_bundle(blob, password="family hub 2026"))
    assert files == {"config.json": b'{"calendars": []}', "env": b"HA_TOKEN=abc123\n",
                     "pin.key": b"p" * 32}


def test_an_unchanged_bundle_is_not_written_again(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    src = _sources(tmp_path, **{"config.json": b"{}"})
    a = _run(tmp_path, db, sources=src, now=NOW.replace(hour=1))
    b = _run(tmp_path, db, sources=src, now=NOW.replace(hour=2))
    assert a["settings_file"] == b["settings_file"] == "settings-20260818-0130.fhb"
    Path(src["config.json"]).write_bytes(b'{"new": 1}')
    c = _run(tmp_path, db, sources=src, now=NOW.replace(hour=3))
    assert c["settings_file"] == "settings-20260818-0330.fhb"
    assert len(list((tmp_path / "out" / "settings").glob("settings-*.fhb"))) == 2


def test_a_new_password_writes_a_new_bundle(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    key = str(tmp_path / "backup.key")
    fb.set_password(key, "first password")
    src = _sources(tmp_path, **{"config.json": b"{}"})
    _run(tmp_path, db, sources=src, now=NOW.replace(hour=1))
    fb.set_password(key, "second password")
    r = _run(tmp_path, db, sources=src, now=NOW.replace(hour=2))
    assert r["settings_file"] == "settings-20260818-0230.fhb"
    blob = (tmp_path / "out/settings" / r["settings_file"]).read_bytes()
    assert fb.decrypt_bundle(blob, password="second password")


def test_settings_bundles_prune_to_their_keep_count(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    src = _sources(tmp_path, **{"config.json": b"{}"})
    for h in range(5):
        Path(src["config.json"]).write_bytes(json.dumps({"n": h}).encode())
        _run(tmp_path, db, sources=src, now=NOW.replace(hour=h), settings_keep=2)
    names = sorted(p.name for p in (tmp_path / "out/settings").glob("settings-*.fhb"))
    assert names == ["settings-20260818-0330.fhb", "settings-20260818-0430.fhb"]


def test_an_unreadable_settings_file_fails_the_bundle_not_the_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    src = _sources(tmp_path, **{"config.json": b"{}", "env": b"A=1"})
    real_open = open

    def picky(path, *a, **k):
        if str(path) == src["env"]:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", picky)
    r = _run(tmp_path, db, sources=src)
    assert r["ok"] is True and r["snapshot"]
    assert r["settings_file"] is None
    assert ".env" in r["settings_error"] and "Permission denied" in r["settings_error"]


# --- backup.key -------------------------------------------------------------

def test_set_password_writes_a_private_key_file_without_the_password(tmp_path):
    key = tmp_path / "data" / "backup.key"
    fb.set_password(str(key), "family hub 2026")
    raw = key.read_bytes()
    assert len(raw) == 48 and b"family hub" not in raw
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    salt, derived = fb.read_key(str(key))
    assert derived == fb.derive_key("family hub 2026", salt)
    assert not [p for p in key.parent.iterdir() if p.name != "backup.key"]


@pytest.mark.parametrize("pw", ["short", "", None, 12345678, "x" * 1025])
def test_set_password_refuses_a_weak_or_odd_password(tmp_path, pw):
    with pytest.raises(ValueError):
        fb.set_password(str(tmp_path / "backup.key"), pw)
    assert not (tmp_path / "backup.key").exists()


def test_a_damaged_key_is_reported_once_and_saves_no_settings(tmp_path, caplog):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    (tmp_path / "backup.key").write_bytes(b"short")
    fb._warned.clear()
    assert fb.read_key(str(tmp_path / "backup.key")) is None
    assert fb.key_state(str(tmp_path / "backup.key")) == "damaged"
    with caplog.at_level(logging.ERROR, logger="family_hub"):
        for _ in range(3):
            r = _run(tmp_path, db, sources=_sources(tmp_path, **{"config.json": b"{}"}))
    assert r["ok"] and r["settings_file"] is None
    assert "password" in r["settings_error"]
    assert sum("damaged" in m for m in caplog.messages) == 1


def test_key_state_names_each_case(tmp_path):
    p = str(tmp_path / "backup.key")
    assert fb.key_state(p) == "unset"
    fb.set_password(p, "family hub 2026")
    assert fb.key_state(p) == "ok"
    os.chmod(p, 0o644)
    assert fb.read_key(p) is not None
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"   # tightened on read


# --- the heartbeat --------------------------------------------------------------

def test_a_good_run_writes_the_heartbeat_the_wall_reads(tmp_path):
    db = tmp_path / "hub.db"
    c = _make_db(db)
    c.execute("insert into kv values ('backup_status', ?)",
              (json.dumps({"at": "old", "remote_ok": True, "remote_ok_at": "x",
                           "snapshot": "box.db"}),))
    c.commit()
    c.close()
    r = _run(tmp_path, db)
    conn = fdb.connect(str(db))
    at = dt.datetime(2026, 8, 18, 16, 30, tzinfo=dt.timezone.utc)
    fb.record(conn, r, at, "/mnt/nas/family-hub")
    rec = fdb.kv_get(conn, "backup_status")
    assert rec["at"] == at.isoformat()
    assert rec["snapshot"] == "hub-20260818-0930.db" and rec["bytes"] == r["bytes"]
    assert rec["source"] == "in-app" and rec["dir"] == "/mnt/nas/family-hub"
    assert rec["in_app_error"] is None and rec["settings_at"] is None
    # the box script's off-box result stays for the wall's remote badge
    assert rec["remote_ok"] is True and rec["remote_ok_at"] == "x"
    mine = fdb.kv_get(conn, fb.STATE_KEY)
    assert mine["last_ok"] == {"at": at.isoformat(), "snapshot": r["snapshot"],
                               "bytes": r["bytes"]}
    assert mine["last_error"] is None
    conn.close()


def test_a_failed_run_keeps_the_last_good_time_and_records_why(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    conn = fdb.connect(str(db))
    t1 = dt.datetime(2026, 8, 18, 16, 30, tzinfo=dt.timezone.utc)
    fb.record(conn, _run(tmp_path, db), t1, "./backups")
    t2 = t1 + dt.timedelta(hours=1)
    fb.record(conn, {"ok": False, "error": "The backup folder is full."}, t2, "./backups")
    rec = fdb.kv_get(conn, "backup_status")
    assert rec["at"] == t1.isoformat()              # the wall's badge goes stale honestly
    assert rec["in_app_error"] == "The backup folder is full."
    mine = fdb.kv_get(conn, fb.STATE_KEY)
    assert mine["last_ok"]["at"] == t1.isoformat()
    assert mine["last_error"] == {"at": t2.isoformat(), "message": "The backup folder is full."}
    conn.close()


def test_the_settings_time_is_recorded(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    r = _run(tmp_path, db, sources=_sources(tmp_path, **{"config.json": b"{}"}))
    conn = fdb.connect(str(db))
    t = dt.datetime(2026, 8, 18, 16, 30, tzinfo=dt.timezone.utc)
    fb.record(conn, r, t, "./backups")
    assert fdb.kv_get(conn, "backup_status")["settings_at"] == t.isoformat()
    mine = fdb.kv_get(conn, fb.STATE_KEY)
    assert mine["settings"] == {"at": t.isoformat(), "file": r["settings_file"]}
    conn.close()


# --- the folder check -----------------------------------------------------------

def test_folder_check(tmp_path, monkeypatch):
    assert fb.folder_problem(str(tmp_path / "missing"), must_be_mount=False) == "no_folder"
    assert fb.folder_problem(str(tmp_path), must_be_mount=False) is None
    # the default /backups must be something mounted in, not the image's own disk
    assert fb.folder_problem(str(tmp_path), must_be_mount=True) == "not_mounted"
    monkeypatch.setattr(fb.os.path, "ismount", lambda p: True)
    assert fb.folder_problem(str(tmp_path), must_be_mount=True) is None
    monkeypatch.setattr(fb.os, "access", lambda p, m: False)
    assert fb.folder_problem(str(tmp_path), must_be_mount=True) == "not_writable"


def test_counts_per_tier(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    for h in range(3):
        _run(tmp_path, db, now=NOW.replace(hour=h))
    assert fb.counts(str(tmp_path / "out")) == {
        "hourly": 3, "daily": 1, "weekly": 1, "monthly": 1, "settings": 0}
    assert fb.counts(str(tmp_path / "nowhere")) == {
        "hourly": 0, "daily": 0, "weekly": 0, "monthly": 0, "settings": 0}


# --- the restore command ----------------------------------------------------------

def _bundle(tmp_path, files, password="family hub 2026"):
    salt = os.urandom(16)
    blob = fb.encrypt_bundle(fb.pack_settings(files), fb.derive_key(password, salt), salt)
    p = tmp_path / "settings-20260818-0930.fhb"
    p.write_bytes(blob)
    return p


def test_restore_settings_unpacks_under_the_real_names(tmp_path, capsys):
    p = _bundle(tmp_path, {"config.json": b"{}", "env": b"A=1\n", "pin.key": b"k" * 32})
    out = tmp_path / "restored"
    rc = fb.main(["restore-settings", str(p), str(out)],
                 ask_password=lambda prompt: "family hub 2026")
    assert rc == 0
    assert (out / "config.json").read_bytes() == b"{}"
    assert (out / ".env").read_bytes() == b"A=1\n"
    assert (out / "pin.key").read_bytes() == b"k" * 32
    for f in out.iterdir():
        assert oct(f.stat().st_mode & 0o777) == "0o600"
    said = capsys.readouterr().out
    assert ".env" in said and "data/" in said


def test_restore_settings_with_a_wrong_password_writes_nothing(tmp_path, capsys):
    p = _bundle(tmp_path, {"config.json": b"{}"})
    out = tmp_path / "restored"
    rc = fb.main(["restore-settings", str(p), str(out)], ask_password=lambda _: "nope nope")
    assert rc == 1
    assert not out.exists() or not list(out.iterdir())
    assert "password" in capsys.readouterr().err.lower()


def test_restore_settings_never_overwrites_a_file(tmp_path):
    p = _bundle(tmp_path, {"config.json": b"{}"})
    out = tmp_path / "restored"
    out.mkdir()
    (out / "config.json").write_bytes(b"mine")
    rc = fb.main(["restore-settings", str(p), str(out)],
                 ask_password=lambda _: "family hub 2026")
    assert rc == 1
    assert (out / "config.json").read_bytes() == b"mine"


def test_list_shows_the_newest_backups(tmp_path, capsys):
    db = tmp_path / "hub.db"
    _make_db(db).close()
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    for h in range(2):
        _run(tmp_path, db, now=NOW.replace(hour=h),
             sources=_sources(tmp_path, **{"config.json": str(h).encode()}))
    assert fb.main(["--list", str(tmp_path / "out")]) == 0
    said = capsys.readouterr().out
    assert said.index("hub-20260818-0130.db") < said.index("hub-20260818-0030.db")
    assert "settings-20260818-0130.fhb" in said


def test_usage_on_bad_arguments(capsys):
    assert fb.main([]) == 2
    assert "restore-settings" in capsys.readouterr().out
