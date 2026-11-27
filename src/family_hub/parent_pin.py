"""The parent PIN: guards the chore wallet's money actions (old-day edits,
Paid, reward settings, house/personal on a chore).

The hub's trust model stays LAN-only with no logins; the PIN only keeps a
child from quietly changing money on the shared wall. It is 4 to 6 digits
and never logged. It is stored in kv ``rewards.pin`` as a salted scrypt hash
of an HMAC keyed by a secret in ``pin.key``, a file beside the database (not
in it): a stolen copy of hub.db alone gives nothing to try the 1.1 million
possible PINs against (wallet audit known limit, closed 2026-11-27). A PIN
stored the old way (PBKDF2, no key) still works and is upgraded the first
time the right PIN goes in. If the key file is lost while a PIN is set, PIN
checks fail closed and say so in the log; ``python -m family_hub.parent_pin
--clear`` on the box clears the PIN so a parent can set a new one. Five
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
import logging
import os
import re
import secrets
import sys
import threading

from . import db as fdb

log = logging.getLogger("family_hub")

KEY = "rewards.pin"
# The secret mixed into every PIN hash. Beside hub.db by default (same data
# volume, its own file); PIN_KEY_PATH moves it.
KEY_PATH = os.environ.get("PIN_KEY_PATH", os.path.join(
    os.path.dirname(os.environ.get("DB_PATH", "data/hub.db")) or ".", "pin.key"))
LOCK_KEY = "rewards.pin_lock"
ROUNDS = 200_000
MAX_TRIES = 5
WINDOW_S = 300
LOCK_S = 30
_FORMAT = re.compile(r"[0-9]{4,6}")


def _hash_v1(pin: str, salt: bytes) -> str:
    """The first scheme (no key file). Only read, to upgrade an old PIN."""
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ROUNDS).hex()


def _hash_v2(pin: str, salt: bytes, key: bytes) -> str:
    keyed = hmac.new(key, pin.encode(), "sha256").digest()
    return hashlib.scrypt(keyed, salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


_KEY_LOCK = threading.Lock()     # one key is ever made, even with two requests at once
_warned: set = set()             # problems already logged, so a poll loop logs once


def _warn_once(what: str, msg: str, *args) -> None:
    if what not in _warned:
        _warned.add(what)
        log.error(msg, *args)


def _read_key() -> bytes | None:
    """The 32-byte key, or None when it is missing, unreadable or damaged
    (each logged once). A key readable by others is tightened to 0600."""
    try:
        with open(KEY_PATH, "rb") as f:
            key = f.read()
            mode = os.fstat(f.fileno()).st_mode
    except FileNotFoundError:
        return None
    except OSError as e:
        _warn_once("unreadable", "parent PIN: cannot read the key file %s (%s)", KEY_PATH, e)
        return None
    if len(key) != 32:
        _warn_once("damaged", "parent PIN: the key file %s is damaged (%d bytes, not 32)",
                   KEY_PATH, len(key))
        return None
    if mode & 0o077:
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError as e:
            _warn_once("chmod", "parent PIN: could not make %s owner-only (%s)", KEY_PATH, e)
    return key


def _new_key_bytes() -> bytes:
    return secrets.token_bytes(32)


def _key_for_new_pin() -> bytes:
    """The key to hash a new PIN with: the existing one, or a new one made
    once (under a lock, written to a temp file and renamed into place, so no
    reader ever sees half a key and two requests never make two keys). Only
    reached when no keyed PIN exists, so replacing a missing or damaged key
    cannot orphan a PIN."""
    with _KEY_LOCK:
        key = _read_key()
        if key is not None:
            return key
        key = _new_key_bytes()
        folder = os.path.dirname(KEY_PATH) or "."
        os.makedirs(folder, exist_ok=True)
        tmp = f"{KEY_PATH}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, KEY_PATH)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        _warned.clear()
        return key


def is_set(conn) -> bool:
    return bool(fdb.kv_get(conn, KEY))


def key_missing(conn) -> bool:
    """A PIN stored with the key, but the key file is gone or damaged. Logged
    once with the way out, since the wall only shows the refusal."""
    stored = fdb.kv_get(conn, KEY)
    if not (stored and stored.get("v") == 2 and _read_key() is None):
        return False
    _warn_once("missing", "parent PIN: the key file %s is missing or damaged, so no PIN "
               "can be checked; on the box run `python -m family_hub.parent_pin "
               "--clear` and set a new PIN", KEY_PATH)
    return True


def _matches(conn, pin) -> bool:
    stored = fdb.kv_get(conn, KEY)
    if not stored or not isinstance(pin, str):
        return False
    salt = bytes.fromhex(stored["salt"])
    if stored.get("v") == 2:
        key = _read_key()
        if key is None:
            key_missing(conn)                 # logs the way out, once
            log.error("parent PIN: a PIN was refused because %s is missing or damaged",
                      KEY_PATH)
            return False
        return hmac.compare_digest(_hash_v2(pin, salt, key), stored["hash"])
    if not hmac.compare_digest(_hash_v1(pin, salt), stored["hash"]):
        return False
    try:
        _store(conn, pin)                   # right PIN, old scheme: upgrade it
    except Exception:
        # the old hash is still valid: let the parent in and retry next time
        log.error("parent PIN: the upgrade to the keyed hash could not be saved",
                  exc_info=True)
    return True


def _store(conn, pin: str) -> None:
    key = _key_for_new_pin()
    salt = secrets.token_bytes(16)
    fdb.kv_set(conn, KEY, {"v": 2, "salt": salt.hex(), "hash": _hash_v2(pin, salt, key)})


def set_pin(conn, pin, *, current) -> None:
    """Set the PIN, or change it (``current`` must then match). Raises
    ValueError on a bad format or a wrong current PIN."""
    if not isinstance(pin, str) or not _FORMAT.fullmatch(pin):
        raise ValueError("the PIN must be 4 to 6 digits")
    if is_set(conn) and not _matches(conn, current):
        raise ValueError("the current PIN is wrong")
    _store(conn, pin)
    fdb.kv_set(conn, LOCK_KEY, None)


def clear(conn) -> None:
    """Forget the PIN and any pauses (the box-side reset for a lost key file).
    Money actions stay locked until a parent sets a new PIN."""
    fdb.kv_set(conn, KEY, None)
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


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--clear"]:
        print("usage: python -m family_hub.parent_pin --clear   (forget the parent PIN)")
        return 2
    conn = fdb.connect(os.environ.get("DB_PATH", "data/hub.db"))
    clear(conn)
    conn.close()
    print("The parent PIN is cleared. Set a new one in Settings > Chore wallet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
