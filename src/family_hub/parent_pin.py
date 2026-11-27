"""The parent PIN: guards the chore wallet's money actions (old-day edits,
Paid, reward settings, house/personal on a chore).

The hub's trust model stays LAN-only with no logins; the PIN only keeps a
child from quietly changing money on the shared wall. It is 4 to 6 digits,
stored as a salted PBKDF2 hash in kv ``rewards.pin``, and never logged. Five
wrong tries inside five minutes pause PIN checks for 30 seconds; after that,
each wrong try pauses them again until the right PIN goes in. Guessing runs
at one try per 30 seconds at best (days for 4 digits). Pauses are kept
per screen (the request's address): guessing on the kitchen wall pauses the
wall, never a parent's own phone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets

from . import db as fdb

KEY = "rewards.pin"
LOCK_KEY = "rewards.pin_lock"
ROUNDS = 200_000
MAX_TRIES = 5
WINDOW_S = 300
LOCK_S = 30
_FORMAT = re.compile(r"[0-9]{4,6}")


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ROUNDS).hex()


def is_set(conn) -> bool:
    return bool(fdb.kv_get(conn, KEY))


def _matches(conn, pin) -> bool:
    stored = fdb.kv_get(conn, KEY)
    if not stored or not isinstance(pin, str):
        return False
    return hmac.compare_digest(_hash(pin, bytes.fromhex(stored["salt"])),
                               stored["hash"])


def set_pin(conn, pin, *, current) -> None:
    """Set the PIN, or change it (``current`` must then match). Raises
    ValueError on a bad format or a wrong current PIN."""
    if not isinstance(pin, str) or not _FORMAT.fullmatch(pin):
        raise ValueError("the PIN must be 4 to 6 digits")
    if is_set(conn) and not _matches(conn, current):
        raise ValueError("the current PIN is wrong")
    salt = secrets.token_bytes(16)
    fdb.kv_set(conn, KEY, {"salt": salt.hex(), "hash": _hash(pin, salt)})
    fdb.kv_set(conn, LOCK_KEY, None)


def _locks(conn, now: dt.datetime) -> dict:
    """Per-screen pause records, dropping any that no longer matter (no
    pause running and no wrong try inside the window)."""
    raw = fdb.kv_get(conn, LOCK_KEY) or {}
    if not isinstance(raw, dict) or "n" in raw or "until" in raw:
        raw = {}                       # the old single-record shape: start fresh
    keep = {}
    for src, lk in raw.items():
        stamp = lk.get("until") or lk.get("first")
        if stamp and (now - dt.datetime.fromisoformat(stamp)).total_seconds() <= WINDOW_S:
            keep[src] = lk
    return keep


def check(conn, pin, now: dt.datetime, source: str = "") -> tuple[bool, int]:
    """(ok, seconds still paused) for a try from ``source`` (the screen's
    address). A paused screen refuses even the right digits; other screens
    are not affected."""
    locks = _locks(conn, now)
    lock = locks.get(source, {})
    until = lock.get("until")
    if until:
        left = (dt.datetime.fromisoformat(until) - now).total_seconds()
        if left > 0:
            return False, int(left) + 1
        # the pause is over, but the guessing isn't forgotten: one more
        # wrong try inside the window pauses again
        lock = {"n": MAX_TRIES - 1, "first": now.isoformat()}
    if not is_set(conn):
        return False, 0
    if _matches(conn, pin):
        if source in locks:
            locks.pop(source)
            fdb.kv_set(conn, LOCK_KEY, locks)
        return True, 0
    first = lock.get("first")
    if first and (now - dt.datetime.fromisoformat(first)).total_seconds() <= WINDOW_S:
        lock = {"n": lock.get("n", 0) + 1, "first": first}
    else:
        lock = {"n": 1, "first": now.isoformat()}
    if lock["n"] >= MAX_TRIES:
        lock = {"until": (now + dt.timedelta(seconds=LOCK_S)).isoformat(),
                "first": lock["first"]}
    locks[source] = lock
    fdb.kv_set(conn, LOCK_KEY, locks)
    return False, 0
