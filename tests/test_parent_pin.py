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
    assert set(stored) == {"salt", "hash"}


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
    assert not ok and wait > 0
