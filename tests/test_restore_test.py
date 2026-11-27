"""The monthly restore test (backups.restore_test and friends).

Once a month the hub proves its newest backup really restores: the newest
database snapshot is copied to a private temp folder, opened read-only,
checked for integrity, for the tables a hub needs, and for row counts close
to the live database's (a stale or cut-short copy fails). With a backup
password set, the newest settings bundle is opened in memory with the stored
key and must hold the files the last backup recorded. Nothing in the live
database or the backup folder is touched, and nothing decrypted is written.
"""
import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from family_hub import backups as fb
from family_hub import db as fdb

NOW = dt.datetime(2026, 8, 18, 9, 30)
UTC = dt.timezone.utc


def _hub(path: Path, people=3, chores=5, occ=100, ledger=2):
    """A live hub.db with the real schema and some rows in the key tables."""
    c = fdb.connect(str(path))
    fdb.ensure_schema(c)
    _grow(c, people=people, chores=chores, occ=occ, ledger=ledger)
    fdb.kv_set(c, "filler", "x" * 20000)       # clear the snapshot's size floor
    return c


def _grow(c, people=0, chores=0, occ=0, ledger=0):
    for i in range(people):
        c.execute("INSERT INTO people(name, color) VALUES(?, '#fff')", (f"p{i}",))
    for i in range(chores):
        c.execute("INSERT INTO chores(title, schedule_kind, assign_kind, rotation_epoch) "
                  "VALUES(?, 'daily', 'fixed', '2026-01-01')", (f"c{i}",))
    start = c.execute("SELECT COUNT(*) FROM occurrence_log").fetchone()[0]
    for i in range(start, start + occ):
        c.execute("INSERT INTO occurrence_log(date, chore_id, person_id, title) "
                  "VALUES(?, ?, 1, 't')", (f"d{i:06d}", i))
    for i in range(ledger):
        c.execute("INSERT INTO ledger(person_id, kind, cents, day, created_at) "
                  "VALUES(1, 'payout', 100, '2026-08-01', '2026-08-01T00:00:00')")
    c.commit()


def _backup(tmp_path, db, now=NOW, sources=None):
    r = fb.run_once(now, db_path=str(db), out_dir=str(tmp_path / "out"),
                    sources=sources or {}, key_path=str(tmp_path / "backup.key"))
    assert r["ok"], r
    return r


def _test(tmp_path, conn, files=None, **kw):
    kw.setdefault("tmp_dir", str(_scratch(tmp_path)))
    return fb.restore_test(str(tmp_path / "out"), live=fb.live_counts(conn),
                           key_path=str(tmp_path / "backup.key"),
                           settings_files=files, **kw)


def _scratch(tmp_path):
    p = tmp_path / "scratch"
    p.mkdir(exist_ok=True)
    return p


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


# --- the database snapshot ------------------------------------------------------------

def test_a_good_snapshot_passes(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    r = _backup(tmp_path, db)
    t = _test(tmp_path, conn)
    assert t["ok"] is True and t["reason"] is None
    assert t["snapshot"] == r["snapshot"]
    assert t["counts"]["occurrence_log"] == {"backup": 100, "live": 100}
    assert t["counts"]["people"] == {"backup": 3, "live": 3}
    assert t["settings_checked"] is False


def test_it_never_touches_the_live_db_or_the_backup_folder_and_cleans_up(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    _backup(tmp_path, db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before_db = db.read_bytes()
    before_out = _tree(tmp_path / "out")
    mtimes = {p: p.stat().st_mtime_ns for p in (tmp_path / "out").rglob("*")}
    assert _test(tmp_path, conn)["ok"] is True
    assert db.read_bytes() == before_db
    assert _tree(tmp_path / "out") == before_out
    assert {p: p.stat().st_mtime_ns for p in (tmp_path / "out").rglob("*")} == mtimes
    assert list(_scratch(tmp_path).iterdir()) == [], "the temp copy is removed"


def test_no_backups_yet_fails_in_plain_words(tmp_path):
    conn = _hub(tmp_path / "hub.db")
    t = _test(tmp_path, conn)
    assert t["ok"] is False and t["snapshot"] is None
    assert t["reason"] == "No database backup was found in the backup folder."


def test_a_truncated_snapshot_fails(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, occ=2000)
    r = _backup(tmp_path, db)
    snap = tmp_path / "out" / "db" / "hourly" / r["snapshot"]
    data = snap.read_bytes()
    snap.write_bytes(data[: len(data) // 2])
    t = _test(tmp_path, conn)
    assert t["ok"] is False and t["snapshot"] == r["snapshot"]
    assert t["reason"].startswith(f"The newest backup ({r['snapshot']}) ")
    assert list(_scratch(tmp_path).iterdir()) == []


def test_a_corrupt_snapshot_fails(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    r = _backup(tmp_path, db)
    snap = tmp_path / "out" / "db" / "hourly" / r["snapshot"]
    snap.write_bytes(b"not a database at all " * 1000)
    t = _test(tmp_path, conn)
    assert t["ok"] is False
    assert "can't be opened as a database" in t["reason"]


def test_damaged_pages_fail_the_integrity_check(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, occ=3000)
    r = _backup(tmp_path, db)
    snap = tmp_path / "out" / "db" / "hourly" / r["snapshot"]
    data = bytearray(snap.read_bytes())
    page = 4096
    for off in range(page * 3, min(len(data), page * 12)):   # scribble over a few pages
        data[off] = (data[off] * 7 + 13) % 256
    snap.write_bytes(bytes(data))
    t = _test(tmp_path, conn)
    assert t["ok"] is False
    assert ("integrity check" in t["reason"] or "can't be opened" in t["reason"]
            or "can't be read" in t["reason"])


def test_a_stale_snapshot_far_below_live_fails(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, occ=100)
    r = _backup(tmp_path, db)
    _grow(conn, occ=200)                  # the hub moved on; the copy did not
    t = _test(tmp_path, conn)
    assert t["ok"] is False and t["snapshot"] == r["snapshot"]
    assert t["reason"] == (f"The newest backup ({r['snapshot']}) has 100 chore history "
                           "rows, but the hub has 300. It looks old or cut short.")


def test_an_empty_table_where_live_has_rows_fails(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, ledger=0)
    _backup(tmp_path, db)
    _grow(conn, ledger=1)
    t = _test(tmp_path, conn)
    assert t["ok"] is False
    assert "has no wallet ledger rows, but the hub has 1" in t["reason"]


def test_a_little_growth_since_the_last_copy_still_passes(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, people=3, occ=100)
    _backup(tmp_path, db)
    _grow(conn, people=1, occ=8)          # a new person and a few chores done since
    t = _test(tmp_path, conn)
    assert t["ok"] is True, t["reason"]


def test_empty_tables_on_both_sides_pass(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db, ledger=0)
    _backup(tmp_path, db)
    assert _test(tmp_path, conn)["ok"] is True


def test_a_snapshot_missing_a_table_fails(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    _backup(tmp_path, db)
    # a newer file in the hourly folder that is a database without the ledger
    odd = tmp_path / "out" / "db" / "hourly" / "hub-20260818-1030.db"
    c = sqlite3.connect(odd)
    c.execute("CREATE TABLE people(id INTEGER)")
    c.execute("CREATE TABLE filler(x TEXT)")
    c.execute("INSERT INTO filler VALUES(?)", ("x" * 20000,))
    c.commit()
    c.close()
    t = _test(tmp_path, conn)
    assert t["ok"] is False and t["snapshot"] == "hub-20260818-1030.db"
    assert "is missing tables the hub needs" in t["reason"] and "ledger" in t["reason"]


def test_the_newest_snapshot_is_the_one_tested(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    _backup(tmp_path, db, now=NOW.replace(hour=7))
    r = _backup(tmp_path, db, now=NOW.replace(hour=8))
    assert _test(tmp_path, conn)["snapshot"] == r["snapshot"]


def test_with_no_hourly_copies_a_coarser_tier_is_used(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    _backup(tmp_path, db)
    for p in (tmp_path / "out" / "db" / "hourly").iterdir():
        p.unlink()
    t = _test(tmp_path, conn)
    assert t["ok"] is True and t["snapshot"] == "hub-20260818.db"


# --- the settings bundle ------------------------------------------------------------

def _with_settings(tmp_path, files=None):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    fb.set_password(str(tmp_path / "backup.key"), "family hub 2026")
    names = {"config.json": "config.json", "env": ".env", "pin.key": "pin.key"}
    src = {}
    for short, fname in names.items():
        p = tmp_path / "src" / fname
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"secret-" + short.encode())
        src[short] = str(p)
    r = _backup(tmp_path, db, sources=src)
    assert r["settings_file"] and r["settings_names"] == ["config.json", "env", "pin.key"]
    return conn, r


def test_the_settings_bundle_opens_with_the_stored_key_and_holds_the_recorded_files(tmp_path):
    conn, r = _with_settings(tmp_path)
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is True, t["reason"]
    assert t["settings_checked"] is True and t["settings_file"] == r["settings_file"]
    assert list(_scratch(tmp_path).iterdir()) == []


def test_nothing_decrypted_is_written_anywhere(tmp_path):
    conn, r = _with_settings(tmp_path)
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is True
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.parent.name != "src":
            assert b"secret-" not in p.read_bytes(), p


def test_a_wrong_key_fails(tmp_path):
    conn, r = _with_settings(tmp_path)
    key = tmp_path / "backup.key"
    raw = key.read_bytes()
    key.write_bytes(raw[:16] + bytes(32))          # same salt, a different key
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is False
    assert t["reason"] == (f"The newest settings backup ({r['settings_file']}) can't be "
                           "opened with the hub's backup key. The key file may be damaged. "
                           "Set the backup password again.")


def test_a_bundle_from_an_older_password_fails_and_says_so(tmp_path):
    conn, r = _with_settings(tmp_path)
    fb.set_password(str(tmp_path / "backup.key"), "a newer password")
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is False
    assert "was made with an older backup password" in t["reason"]


def test_a_bundle_missing_a_recorded_file_fails(tmp_path):
    conn, r = _with_settings(tmp_path)
    t = _test(tmp_path, conn, files=r["settings_names"] + ["token.json"])
    assert t["ok"] is False
    assert t["reason"] == (f"The newest settings backup ({r['settings_file']}) is missing "
                           "token.json, which the last backup saved.")


def test_a_damaged_bundle_fails(tmp_path):
    conn, r = _with_settings(tmp_path)
    p = tmp_path / "out" / "settings" / r["settings_file"]
    data = bytearray(p.read_bytes())
    data[-1] ^= 0xFF
    p.write_bytes(bytes(data))
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is False and "can't be opened" in t["reason"]


def test_a_recorded_bundle_that_is_gone_fails(tmp_path):
    conn, r = _with_settings(tmp_path)
    (tmp_path / "out" / "settings" / r["settings_file"]).unlink()
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is False
    assert t["reason"] == "No settings backup was found, but the last backup saved one."


def test_without_a_key_the_settings_check_is_skipped(tmp_path):
    conn, r = _with_settings(tmp_path)
    os.remove(tmp_path / "backup.key")
    t = _test(tmp_path, conn, files=None)
    assert t["ok"] is True and t["settings_checked"] is False


def test_a_bad_database_is_reported_before_the_settings(tmp_path):
    conn, r = _with_settings(tmp_path)
    snap = tmp_path / "out" / "db" / "hourly" / r["snapshot"]
    snap.write_bytes(b"junk" * 5000)
    t = _test(tmp_path, conn, files=r["settings_names"])
    assert t["ok"] is False and "can't be opened as a database" in t["reason"]


def test_the_backup_records_which_settings_files_it_saved(tmp_path):
    conn, r = _with_settings(tmp_path)
    t = dt.datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
    fb.record(conn, r, t, "./backups")
    mine = fdb.kv_get(conn, fb.STATE_KEY)
    assert mine["settings"] == {"at": t.isoformat(), "file": r["settings_file"],
                                "files": ["config.json", "env", "pin.key"]}


def test_an_unchanged_bundle_still_reports_its_files(tmp_path):
    conn, r = _with_settings(tmp_path)
    again = fb.run_once(NOW.replace(hour=10), db_path=str(tmp_path / "hub.db"),
                        out_dir=str(tmp_path / "out"),
                        sources={k: str(tmp_path / "src" / v) for k, v in
                                 {"config.json": "config.json", "env": ".env",
                                  "pin.key": "pin.key"}.items()},
                        key_path=str(tmp_path / "backup.key"))
    assert again["settings_file"] == r["settings_file"]
    assert again["settings_names"] == r["settings_names"]


# --- the schedule ---------------------------------------------------------------------

T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _iso(d):
    return d.isoformat()


@pytest.mark.parametrize("state,last,now,due", [
    ({}, None, T0, False),                                                   # no backup yet
    ({"first_ok_at": _iso(T0)}, None, T0 + dt.timedelta(hours=23), False),   # under a day
    ({"first_ok_at": _iso(T0)}, None, T0 + dt.timedelta(hours=25), True),    # a day after
    ({"first_ok_at": _iso(T0)}, {"at": _iso(T0), "ok": True},
     T0 + dt.timedelta(days=29), False),
    ({"first_ok_at": _iso(T0)}, {"at": _iso(T0), "ok": True},
     T0 + dt.timedelta(days=30, minutes=1), True),
    ({"first_ok_at": _iso(T0)}, {"at": _iso(T0), "ok": False},
     T0 + dt.timedelta(hours=23), False),                                    # failed: daily retry
    ({"first_ok_at": _iso(T0)}, {"at": _iso(T0), "ok": False},
     T0 + dt.timedelta(hours=25), True),
    ({"first_ok_at": _iso(T0)}, {"at": "garbage", "ok": True}, T0, True),     # unreadable: test
    ({"first_ok_at": "garbage"}, None, T0, False),
])
def test_the_schedule(state, last, now, due):
    assert fb.restore_test_due(state, last, now) is due


def test_record_sets_the_first_good_backup_time_once(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    r = _backup(tmp_path, db)
    t1 = dt.datetime(2026, 8, 18, 9, 31, tzinfo=UTC)
    fb.record(conn, {"ok": False, "error": "x"}, t1 - dt.timedelta(hours=1), "d")
    assert "first_ok_at" not in fdb.kv_get(conn, fb.STATE_KEY)
    fb.record(conn, r, t1, "d")
    fb.record(conn, r, t1 + dt.timedelta(hours=1), "d")
    assert fdb.kv_get(conn, fb.STATE_KEY)["first_ok_at"] == t1.isoformat()


def test_an_older_record_with_a_good_copy_gets_its_first_time_on_the_next_run(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    fdb.kv_set(conn, fb.STATE_KEY, {"last_ok": {"at": "2026-08-01T00:00:00+00:00"}})
    t = dt.datetime(2026, 8, 18, 9, 31, tzinfo=UTC)
    fb.record(conn, _backup(tmp_path, db), t, "d")
    assert fdb.kv_get(conn, fb.STATE_KEY)["first_ok_at"] == t.isoformat()


# --- the record -----------------------------------------------------------------------

def test_the_result_is_recorded_without_moving_the_heartbeat(tmp_path):
    db = tmp_path / "hub.db"
    conn = _hub(db)
    r = _backup(tmp_path, db)
    t = dt.datetime(2026, 8, 18, 9, 31, tzinfo=UTC)
    fb.record(conn, r, t, "d")
    beat = fdb.kv_get(conn, fb.HEARTBEAT_KEY)
    state = fdb.kv_get(conn, fb.STATE_KEY)
    later = t + dt.timedelta(days=2)
    fb.record_restore_test(conn, {"ok": False, "snapshot": r["snapshot"],
                                  "reason": "It looks old."}, later)
    assert fdb.kv_get(conn, fb.HEARTBEAT_KEY) == beat
    assert fdb.kv_get(conn, fb.STATE_KEY) == state
    assert fdb.kv_get(conn, fb.RESTORE_TEST_KEY) == {
        "at": later.isoformat(), "ok": False, "snapshot": r["snapshot"],
        "reason": "It looks old.", "settings_file": None}


def test_a_failure_logs_once_per_reason_and_a_pass_after_it_says_so(tmp_path, caplog):
    conn = _hub(tmp_path / "hub.db")
    bad = {"ok": False, "snapshot": "hub-1.db", "reason": "It looks old."}
    with caplog.at_level(logging.INFO, logger="family_hub"):
        fb.record_restore_test(conn, bad, T0)
        fb.record_restore_test(conn, bad, T0 + dt.timedelta(days=1))
        assert sum("restore test failed" in m for m in caplog.messages) == 1
        fb.record_restore_test(conn, {**bad, "reason": "Something else."},
                               T0 + dt.timedelta(days=2))
        assert sum("restore test failed" in m for m in caplog.messages) == 2
        fb.record_restore_test(conn, {"ok": True, "snapshot": "hub-2.db", "reason": None},
                               T0 + dt.timedelta(days=3))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2 and "It looks old." in errors[0].getMessage()
    assert any("restore test passed" in m for m in caplog.messages)
