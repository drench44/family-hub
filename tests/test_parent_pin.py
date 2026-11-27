"""The parent PIN that guards the chore wallet's money actions."""
import datetime as dt

import pytest

from family_hub import db as fdb
from family_hub import parent_pin as pp

T0 = dt.datetime(2026, 12, 1, 18, 0, tzinfo=dt.timezone.utc)


def test_pin_rejects_bad_format(conn):
    for bad in ("12", "1234567", "12a4", "", " 1234", None):
        with pytest.raises(ValueError):
            pp.set_pin(conn, bad, current=None)
    assert not pp.is_set(conn)
    pp.set_pin(conn, "1234", current=None)
    pp.set_pin(conn, "654321", current="1234")
    assert pp.is_set(conn)


def test_pin_hash_not_plaintext(conn):
    pp.set_pin(conn, "4826", current=None)
    raw = conn.execute("SELECT value FROM kv WHERE key = 'rewards.pin'").fetchone()[0]
    assert "4826" not in raw
    stored = fdb.kv_get(conn, "rewards.pin")
    assert set(stored) == {"v", "salt", "hash"} and stored["v"] == 2


def test_a_stolen_database_alone_cannot_check_a_pin(conn, tmp_path, monkeypatch):
    """The PIN hash mixes in a secret key kept in a file beside the database,
    not in it: with only a copy of hub.db, trying all 4-6 digit PINs gets
    nowhere (the known limit in the wallet audit, closed 2026-11-27)."""
    import hashlib
    import hmac
    pp.set_pin(conn, "4826", current=None)
    key = tmp_path / "pin.key"
    assert key.exists() and len(key.read_bytes()) == 32
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    stored = fdb.kv_get(conn, "rewards.pin")
    salt = bytes.fromhex(stored["salt"])
    # an attacker's guess without the key, under every hash this module uses
    for guess in ("4826",):
        assert hashlib.pbkdf2_hmac("sha256", guess.encode(), salt, 200_000).hex() != stored["hash"]
        assert hashlib.scrypt(guess.encode(), salt=salt, n=2 ** 14, r=8, p=1,
                              dklen=32).hex() != stored["hash"]
        assert hmac.new(b"", guess.encode(), "sha256").hexdigest() != stored["hash"]
    # a different key file (the db restored onto another box) cannot check it
    monkeypatch.setattr(pp, "KEY_PATH", str(tmp_path / "other.key"))
    (tmp_path / "other.key").write_bytes(b"x" * 32)
    assert pp.check(conn, "4826", T0) == (False, 0)


def test_an_old_pin_hash_still_works_and_is_upgraded_on_first_use(conn, tmp_path):
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    fdb.kv_set(conn, "rewards.pin", {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac(
        "sha256", b"2468", salt, 200_000).hex()})          # what v1.21 stored
    assert pp.check(conn, "1111", T0) == (False, 0)
    assert fdb.kv_get(conn, "rewards.pin").get("v") is None   # a wrong try changes nothing
    assert pp.check(conn, "2468", T0) == (True, 0)
    assert fdb.kv_get(conn, "rewards.pin")["v"] == 2         # upgraded with the right PIN
    assert pp.check(conn, "2468", T0) == (True, 0)


def test_a_missing_key_file_fails_closed_loudly_and_can_be_cleared(conn, tmp_path, caplog):
    pp.set_pin(conn, "4826", current=None)
    (tmp_path / "pin.key").unlink()
    with caplog.at_level("ERROR"):
        assert pp.check(conn, "4826", T0) == (False, 0)
    assert "pin.key" in caplog.text
    assert pp.key_missing(conn)
    assert not (tmp_path / "pin.key").exists()               # never re-made under a set PIN
    pp.clear(conn)                                           # the box-side reset
    assert not pp.is_set(conn) and not pp.key_missing(conn)
    pp.set_pin(conn, "1357", current=None)
    assert pp.check(conn, "1357", T0) == (True, 0)


def test_the_box_command_clears_the_pin(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "hub.db")
    c = fdb.connect(db)
    fdb.ensure_schema(c)
    pp.set_pin(c, "4826", current=None)
    c.close()
    monkeypatch.setenv("DB_PATH", db)
    assert pp.main(["--clear"]) == 0
    assert "cleared" in capsys.readouterr().out
    c = fdb.connect(db)
    assert not pp.is_set(c)
    c.close()
    assert pp.main([]) == 2                        # no flag: usage, nothing changed


def test_change_needs_current(conn):
    pp.set_pin(conn, "1234", current=None)
    with pytest.raises(ValueError):
        pp.set_pin(conn, "9999", current=None)
    with pytest.raises(ValueError):
        pp.set_pin(conn, "9999", current="0000")
    assert pp.check(conn, "1234", T0) == (True, 0)


def test_check_right_and_wrong(conn):
    assert pp.check(conn, "1234", T0) == (False, 0)      # no PIN set yet
    pp.set_pin(conn, "1234", current=None)
    assert pp.check(conn, "1234", T0) == (True, 0)
    assert pp.check(conn, "1235", T0) == (False, 0)
    assert pp.check(conn, None, T0) == (False, 0)


def test_lockout_after_five(conn):
    pp.set_pin(conn, "1234", current=None)
    for i in range(5):
        ok, _ = pp.check(conn, "0000", T0 + dt.timedelta(seconds=i))
        assert not ok
    ok, wait = pp.check(conn, "1234", T0 + dt.timedelta(seconds=10))
    assert not ok and 0 < wait <= 30
    assert pp.check(conn, "1234", T0 + dt.timedelta(minutes=6)) == (True, 0)


def test_wrong_tries_spread_out_never_lock(conn):
    pp.set_pin(conn, "1234", current=None)
    for i in range(8):
        pp.check(conn, "0000", T0 + dt.timedelta(minutes=6 * i))
    assert pp.check(conn, "1234", T0 + dt.timedelta(minutes=49)) == (True, 0)


def test_right_pin_clears_the_count(conn):
    pp.set_pin(conn, "1234", current=None)
    for i in range(4):
        pp.check(conn, "0000", T0)
    assert pp.check(conn, "1234", T0)[0]
    for i in range(4):
        pp.check(conn, "0000", T0)
    assert pp.check(conn, "1234", T0)[0]


def test_a_lockout_lasts_seconds_not_minutes(conn):
    """Guessing can't keep a parent out for long: after five wrong tries the
    PIN pauses for 30 seconds, then the right PIN works again."""
    pp.set_pin(conn, "1234", current=None)
    for i in range(5):
        pp.check(conn, "0000", T0 + dt.timedelta(seconds=i))
    ok, wait = pp.check(conn, "1234", T0 + dt.timedelta(seconds=10))
    assert not ok and 0 < wait <= 30
    assert pp.check(conn, "1234", T0 + dt.timedelta(seconds=40)) == (True, 0)


def test_guessing_stays_slow_after_a_lockout(conn):
    """After a lockout ends, ONE more wrong try locks again (not five), so
    guessing runs at one try per 30 seconds at best."""
    pp.set_pin(conn, "1234", current=None)
    for i in range(5):
        pp.check(conn, "0000", T0)
    assert pp.check(conn, "1111", T0 + dt.timedelta(seconds=35)) == (False, 0)
    ok, wait = pp.check(conn, "1234", T0 + dt.timedelta(seconds=36))
    assert not ok and 0 < wait <= 30
    # once the pause passes, the right PIN works and clears the count
    assert pp.check(conn, "1234", T0 + dt.timedelta(seconds=70)) == (True, 0)
    assert pp.check(conn, "0000", T0 + dt.timedelta(seconds=71)) == (False, 0)
    assert pp.check(conn, "1234", T0 + dt.timedelta(seconds=72)) == (True, 0)


def test_a_pause_on_one_screen_does_not_lock_another(conn):
    """Guessing on the kitchen wall pauses the wall only: a parent on their
    own phone (another address) can still enter the PIN."""
    pp.set_pin(conn, "1234", current=None)
    for i in range(5):
        pp.check(conn, "0000", T0, source="wall")
    ok, wait = pp.check(conn, "1234", T0 + dt.timedelta(seconds=1), source="wall")
    assert not ok and wait > 0
    assert pp.check(conn, "1234", T0 + dt.timedelta(seconds=1), source="phone") == (True, 0)
    # and the phone's success does not lift the wall's pause
    ok, wait = pp.check(conn, "1234", T0 + dt.timedelta(seconds=2), source="wall")
    assert not ok and wait > 0


def test_pause_records_are_pruned(conn):
    """Old per-screen records don't pile up in the database."""
    pp.set_pin(conn, "1234", current=None)
    for i in range(30):
        pp.check(conn, "0000", T0 + dt.timedelta(hours=i), source=f"10.0.0.{i}")
    assert len(fdb.kv_get(conn, "rewards.pin_lock") or {}) <= 3


def test_changing_a_pin_still_stored_the_old_way(conn, tmp_path):
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    old = {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", b"2468", salt, 200_000).hex()}
    fdb.kv_set(conn, "rewards.pin", old)
    with pytest.raises(ValueError):
        pp.set_pin(conn, "1357", current="0000")
    assert fdb.kv_get(conn, "rewards.pin") == old             # a wrong current changes nothing
    assert not (tmp_path / "pin.key").exists()
    pp.set_pin(conn, "1357", current="2468")
    assert fdb.kv_get(conn, "rewards.pin")["v"] == 2
    assert pp.check(conn, "1357", T0) == (True, 0)
    assert pp.check(conn, "2468", T0) == (False, 0)


@pytest.mark.parametrize("bad", [b"", b"x" * 31, b"x" * 33])
def test_a_damaged_key_fails_closed_and_is_never_remade_under_a_pin(conn, tmp_path, bad, caplog):
    pp.set_pin(conn, "4826", current=None)
    (tmp_path / "pin.key").write_bytes(bad)
    with caplog.at_level("ERROR"):
        assert pp.key_missing(conn)
        assert pp.check(conn, "4826", T0) == (False, 0)
    assert "pin.key" in caplog.text
    assert (tmp_path / "pin.key").read_bytes() == bad
    pp.clear(conn)
    pp.set_pin(conn, "1357", current=None)                     # a damaged key is replaced, with no PIN set
    assert len((tmp_path / "pin.key").read_bytes()) == 32
    assert pp.check(conn, "1357", T0) == (True, 0)


def test_an_unreadable_key_reads_as_missing_not_a_crash(conn, tmp_path, monkeypatch):
    pp.set_pin(conn, "4826", current=None)
    monkeypatch.setattr(pp, "KEY_PATH", str(tmp_path))         # a directory: IsADirectoryError
    assert pp.key_missing(conn)
    assert pp.check(conn, "4826", T0) == (False, 0)


def test_an_existing_key_is_reused_and_tightened(conn, tmp_path):
    import os
    key = tmp_path / "pin.key"
    key.write_bytes(b"k" * 32)
    os.chmod(key, 0o644)
    pp.set_pin(conn, "4826", current=None)
    assert key.read_bytes() == b"k" * 32
    assert key.stat().st_mode & 0o777 == 0o600


def test_two_first_pins_at_once_never_orphan_the_pin(tmp_path):
    """Review 2026-11-27: two requests making the key at once could leave a
    hash made with a key that the file no longer holds."""
    import threading
    db = str(tmp_path / "hub.db")
    c0 = fdb.connect(db)
    fdb.ensure_schema(c0)
    c0.close()
    import time
    real_make = pp._new_key_bytes

    def slow():
        time.sleep(0.2)              # widen the window two requests can race in
        return real_make()
    pp._new_key_bytes = slow
    try:
        def go():
            c = fdb.connect(db)
            pp._store(c, "4826")
            c.close()
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(timeout=10) for t in ts]
    finally:
        pp._new_key_bytes = real_make
    c = fdb.connect(db)
    assert pp.check(c, "4826", T0) == (True, 0)
    c.close()


def test_an_upgrade_that_cannot_save_still_lets_the_right_pin_in(conn, monkeypatch, caplog):
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    fdb.kv_set(conn, "rewards.pin", {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac(
        "sha256", b"2468", salt, 200_000).hex()})

    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(pp, "_store", boom)
    with caplog.at_level("ERROR"):
        assert pp.check(conn, "2468", T0) == (True, 0)
    assert "upgrade" in caplog.text


def test_clear_also_lifts_any_pause(conn):
    pp.set_pin(conn, "1234", current=None)
    for _ in range(5):
        pp.check(conn, "0000", T0)
    pp.clear(conn)
    assert fdb.kv_get(conn, "rewards.pin_lock") is None
    pp.set_pin(conn, "1357", current=None)
    assert pp.check(conn, "1357", T0 + dt.timedelta(seconds=1)) == (True, 0)
