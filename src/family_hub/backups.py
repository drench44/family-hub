"""The hub's own backups: an hourly job inside the app, writing to one folder.

Why: hub.db is the only state that starts in the hub (chores, streaks,
to-dos, wallets, history), and the settings files beside it (config.json,
.env, data/pin.key, data/wyze.env, data/token.json) hold everything a rebuilt
box needs. backup/family-hub-backup.sh covers hub.db on a box that installs
its systemd timer; a hub installed from GitHub got nothing. This module is
that script ported into the app (same names, stamps, checks and keep counts)
plus an encrypted copy of the settings files.

Where: one folder, `/backups` in the container (BACKUPS_PATH moves it).
docker-compose.yml mounts the host's BACKUP_DIR there (default ./backups), so
a NAS is just a folder the host has mounted. Layout:

    <dir>/db/<hourly|daily|weekly|monthly>/hub-<stamp>.db
    <dir>/settings/settings-<stamp>.fhb

The database snapshot, exactly as the box script makes it: SQLite's online
backup (safe while the app writes), PRAGMA integrity_check, out of WAL mode
so it restores as one plain file, a size check, a temp file and an atomic
rename, then promoted (temp copy and rename again) into the coarser tiers,
each keeping the latest copy for its period. Pruning goes by the stamp in the
name, newest first, and a keep count below 1 is refused, so a tier is never
emptied. Leftover .partial-* files from a killed run are swept first.

The settings bundle: a tar of whichever settings files exist, encrypted with
AES-256-GCM under a key made from the backup password with scrypt. The file is
`FHB1` + 16-byte salt + 12-byte nonce + ciphertext and tag, and those first
32 bytes are the GCM associated data, so a changed header is refused too. The
password is never stored: data/backup.key holds the salt and the derived key
(0600), so the job can encrypt with no one typing anything, while a copy of
the backup folder alone cannot be opened. A new bundle is written only when
the files changed (or the password did); the last 14 are kept.

Restore: `python -m family_hub.backups restore-settings <file> <out dir>` asks
for the password and writes the files; `--list` shows the newest backups.
See docs/backup-and-restore.md.
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("family_hub")

TIERS = ("hourly", "daily", "weekly", "monthly")
KEEP = {"hourly": 48, "daily": 14, "weekly": 8, "monthly": 12}
SETTINGS_KEEP = 14
MIN_BYTES = 8192            # the box script's floor: smaller is not a real hub.db
STATE_KEY = "backups.in_app"   # this job's own record (the card reads it)
MARKER_KEY = "backups.marker"  # the id written into the folder by the first good run
# A file the first good run leaves in the backup folder, holding a random id
# the hub also keeps. A bind mount always looks mounted from inside the
# container, so when a NAS share drops (and the box's empty mount folder is
# what the hub sees), this file being gone is the only sign.
MARKER_NAME = ".family-hub-backups"
MARKER_MISSING = ("The backup folder isn't the one backups went to before. "
                  "Is the NAS mounted?")
HEARTBEAT_KEY = "backup_status"  # shared with the box script (the wall's badge)

MAGIC = b"FHB1"
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
KEY_LEN = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
# scrypt needs 128 * r * n bytes (32 MiB here), which is exactly OpenSSL's
# default cap, so the cap is raised or every call fails.
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# The settings files, by the short name each has inside a bundle, and the
# name it gets back on restore.
SETTINGS_FILES = ("config.json", "env", "pin.key", "wyze.env", "token.json")
RESTORE_NAMES = {"config.json": "config.json", "env": ".env", "pin.key": "pin.key",
                 "wyze.env": "wyze.env", "token.json": "token.json"}
RESTORE_HOME = {"config.json": "the project folder", "env": "the project folder",
                "pin.key": "data/", "wyze.env": "data/", "token.json": "data/"}


class BundleError(Exception):
    """A settings bundle that cannot be opened: not a bundle, a wrong
    password, or a damaged file."""


# --- stamps and pruning (pure) ----------------------------------------------

def stamps(now: dt.datetime) -> dict:
    """The period stamps the box script uses. They sort in time order as plain
    strings, which pruning relies on (the ISO year keeps W52, W53, W01 in
    order across New Year)."""
    iso = now.isocalendar()
    return {"hourly": now.strftime("%Y%m%d-%H%M"), "daily": now.strftime("%Y%m%d"),
            "weekly": "%04d-W%02d" % (iso[0], iso[1]), "monthly": now.strftime("%Y%m")}


def prune_names(names, keep, prefix="hub-", suffix=".db") -> list:
    """The names to delete so `keep` remain: only `<prefix><stamp><suffix>`
    files count, newest first by the stamp in the name. A keep below 1 (or
    not a whole number) is refused, so a tier is never emptied."""
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError(f"keep counts must be whole numbers of at least 1 (got {keep!r})")
    pat = re.compile(re.escape(prefix) + r"[0-9W-]+" + re.escape(suffix) + r"\Z")
    mine = sorted((n for n in names if pat.match(n)), reverse=True)
    return mine[keep:]


def _prune_dir(folder: Path, keep: int, prefix="hub-", suffix=".db") -> None:
    for name in prune_names(os.listdir(folder), keep, prefix, suffix):
        (folder / name).unlink(missing_ok=True)


def _sweep_partials(folder: Path) -> None:
    for p in folder.glob(".partial-*"):
        p.unlink(missing_ok=True)


# --- the folder ------------------------------------------------------------------

def folder_problem(path: str, *, must_be_mount: bool, marker: str | None = None) -> str | None:
    """Why backups cannot go to `path`, or None. `must_be_mount` is for the
    default /backups: in the container it must be something mounted in, or
    the copies would sit on the container's own disk and vanish with it.
    `marker` is the id the first good run left in the folder: once there is
    one, a folder without it (a dropped NAS share) is refused."""
    if not os.path.isdir(path):
        return "no_folder"
    if must_be_mount and not os.path.ismount(path):
        return "not_mounted"
    if not os.access(path, os.W_OK | os.X_OK):
        return "not_writable"
    if marker and read_marker(path) != marker:
        return "marker_missing"
    return None


def read_marker(path: str) -> str | None:
    try:
        with open(os.path.join(path, MARKER_NAME), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def adopt_folder(path: str) -> str:
    """Write a fresh id into `path` (temp file, rename) and return it: the
    first good run does this, and so does Use this folder after a deliberate
    move."""
    new = secrets.token_hex(16)
    final = Path(path) / MARKER_NAME
    tmp = Path(path) / f".partial-marker-{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return new


def counts(out_dir: str) -> dict:
    """How many copies each tier holds now, and how many settings bundles."""
    out = {}
    base = Path(out_dir)
    for tier in TIERS:
        d = base / "db" / tier
        out[tier] = len([n for n in os.listdir(d) if re.fullmatch(r"hub-[0-9W-]+\.db", n)]) \
            if d.is_dir() else 0
    s = base / "settings"
    out["settings"] = len([n for n in os.listdir(s)
                           if re.fullmatch(r"settings-[0-9-]+\.fhb", n)]) if s.is_dir() else 0
    return out


def newest(out_dir: str, n: int = 5) -> dict:
    """The newest few names in each tier and in settings, newest first."""
    base = Path(out_dir)
    found = {}
    for tier in TIERS:
        d = base / "db" / tier
        names = os.listdir(d) if d.is_dir() else []
        found[tier] = sorted((x for x in names if re.fullmatch(r"hub-[0-9W-]+\.db", x)),
                             reverse=True)[:n]
    s = base / "settings"
    names = os.listdir(s) if s.is_dir() else []
    found["settings"] = sorted((x for x in names if re.fullmatch(r"settings-[0-9-]+\.fhb", x)),
                               reverse=True)[:n]
    return found


# --- the database snapshot ---------------------------------------------------------

def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _drop(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _snapshot(db_path: str, hourly: Path, stamp: str) -> tuple[Path, int]:
    """One verified snapshot at hourly/hub-<stamp>.db. Raises with a plain
    reason, leaving nothing behind."""
    if not os.path.isfile(db_path):
        raise RuntimeError(f"The database file is missing ({db_path}).")
    tmp = hourly / f".partial-{stamp}.db"
    _drop(tmp)
    try:
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                row = dst.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise RuntimeError(f"The copy failed its integrity check ({row!r}).")
                mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
                if not mode or str(mode[0]).lower() != "delete":
                    raise RuntimeError("The copy could not be taken out of WAL mode.")
            finally:
                dst.close()
        finally:
            src.close()
        # Some SQLite builds keep an empty -wal or a -shm after close. With
        # the copy out of WAL they hold nothing; a -wal with content would
        # mean frames were not folded in, and that copy is not trusted.
        wal = Path(str(tmp) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise RuntimeError("The copy left a WAL file with data behind.")
        Path(str(tmp) + "-wal").unlink(missing_ok=True)
        Path(str(tmp) + "-shm").unlink(missing_ok=True)
        nbytes = tmp.stat().st_size
        if nbytes < MIN_BYTES:
            raise RuntimeError(f"The copy came out too small ({nbytes} bytes), so it was not kept.")
        _fsync_file(tmp)
        final = hourly / f"hub-{stamp}.db"
        os.replace(tmp, final)
        return final, nbytes
    except sqlite3.Error as e:
        _drop(tmp)
        raise RuntimeError(f"Couldn't copy the database ({e}).") from e
    except BaseException:
        _drop(tmp)
        raise


def _promote(snap: Path, tier_dir: Path, stamp: str) -> None:
    """Copy to a temp file, then rename over the period's slot, so a crash
    or a full disk never truncates the previous good copy."""
    tmp = tier_dir / f".partial-{stamp}.db"
    try:
        shutil.copyfile(snap, tmp)
        _fsync_file(tmp)
        os.replace(tmp, tier_dir / f"hub-{stamp}.db")
    finally:
        tmp.unlink(missing_ok=True)


# --- the settings bundle ------------------------------------------------------------

def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                          p=SCRYPT_P, dklen=KEY_LEN, maxmem=_SCRYPT_MAXMEM)


def encrypt_bundle(plaintext: bytes, key: bytes, salt: bytes, nonce: bytes | None = None) -> bytes:
    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_LEN)
    header = MAGIC + salt + nonce
    return header + AESGCM(key).encrypt(nonce, plaintext, header)


def decrypt_bundle(blob: bytes, *, password: str | None = None, key: bytes | None = None) -> bytes:
    head = len(MAGIC) + SALT_LEN + NONCE_LEN
    if len(blob) < head + TAG_LEN or blob[:len(MAGIC)] != MAGIC:
        raise BundleError("This is not a family hub settings backup.")
    salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
    nonce = blob[len(MAGIC) + SALT_LEN:head]
    if key is None:
        key = derive_key(password or "", salt)
    try:
        return AESGCM(key).decrypt(nonce, blob[head:], blob[:head])
    except InvalidTag:
        raise BundleError("The password is wrong, or the file is damaged.") from None


def pack_settings(files: dict) -> bytes:
    """A tar of the settings files under their short names, the same bytes
    for the same files (sorted, no times, no owners)."""
    bad = set(files) - set(SETTINGS_FILES)
    if bad:
        raise ValueError(f"not a settings file: {sorted(bad)}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack_settings(data: bytes) -> dict:
    out = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
            for m in t.getmembers():
                if not m.isfile() or m.name not in SETTINGS_FILES or m.name in out:
                    raise BundleError(f"The backup holds an unexpected entry ({m.name!r}).")
                out[m.name] = t.extractfile(m).read()
    except tarfile.TarError as e:
        raise BundleError(f"The backup is damaged ({e}).") from e
    return out


def _display(short: str) -> str:
    return RESTORE_NAMES.get(short, short)


def _read_settings(sources: dict) -> dict:
    files = {}
    for short in SETTINGS_FILES:
        path = sources.get(short)
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                files[short] = f.read()
        except OSError as e:
            raise RuntimeError(f"Couldn't read {_display(short)} ({e}).") from e
    return files


# --- backup.key (the salt and the key made from the password) ------------------

_KEY_LOCK = threading.Lock()
_warned: set = set()


def _warn_once(what: str, msg: str, *args) -> None:
    if what not in _warned:
        _warned.add(what)
        log.error(msg, *args)


def _key_raw(path: str):
    """(bytes or None, state) for the key file."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
            mode = os.fstat(f.fileno()).st_mode
    except FileNotFoundError:
        return None, "unset"
    except OSError as e:
        _warn_once("unreadable", "backups: cannot read the key file %s (%s)", path, e)
        return None, "unreadable"
    if len(raw) != SALT_LEN + KEY_LEN:
        _warn_once("damaged", "backups: the key file %s is damaged (%d bytes, not %d); "
                   "set the backup password again in Settings", path, len(raw),
                   SALT_LEN + KEY_LEN)
        return None, "damaged"
    if mode & 0o077:
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            _warn_once("chmod", "backups: could not make %s owner-only (%s)", path, e)
    return raw, "ok"


def read_key(path: str):
    """(salt, key), or None when there is none or it can't be used (logged once)."""
    raw, _ = _key_raw(path)
    return (raw[:SALT_LEN], raw[SALT_LEN:]) if raw else None


def key_state(path: str) -> str:
    """'unset', 'ok', 'damaged' or 'unreadable'."""
    return _key_raw(path)[1]


def check_password(password) -> None:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ValueError(f"the backup password must be {PASSWORD_MIN} to "
                         f"{PASSWORD_MAX} characters")


def set_password(path: str, password) -> None:
    """Make a new salt and key from `password` and store them (temp file,
    rename, 0600), replacing any old key. Bundles made before still need the
    old password."""
    check_password(password)
    salt = secrets.token_bytes(SALT_LEN)
    key = derive_key(password, salt)
    with _KEY_LOCK:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(salt + key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        _warned.clear()


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _save_settings(sources: dict, key_path: str, folder: Path, stamp: str,
                   keep: int, password_expected: bool
                   ) -> tuple[str | None, str | None, list | None]:
    """(bundle name or None, problem or None, the short names the bundle
    holds or None). The restore test checks a bundle against those names."""
    state = key_state(key_path)
    if state == "unset" and not password_expected:
        return None, None, None         # no password yet: nothing to encrypt with
    pair = read_key(key_path)
    if pair is None and state == "unset":
        _warn_once("key-gone", "backups: a backup password was set but %s is missing; "
                   "set the password again in Settings", key_path)
    if pair is None:
        return None, ("The settings files were not saved. Set the backup password "
                      "again in Settings: its key file on the hub can't be used."), None
    salt, key = pair
    files = _read_settings(sources)
    if not files:
        return None, "No settings files were found to save.", None
    folder.mkdir(parents=True, exist_ok=True)
    _sweep_partials(folder)
    have = sorted((n for n in os.listdir(folder)
                   if re.fullmatch(r"settings-[0-9-]+\.fhb", n)), reverse=True)
    if have:
        try:
            last = unpack_settings(decrypt_bundle((folder / have[0]).read_bytes(), key=key))
        except (BundleError, OSError):
            last = None                 # an older password, or damaged: write a new one
        if last is not None:
            gone = sorted(set(last) - set(files))
            if gone:
                # A file that was saved before and is gone now (a deleted .env,
                # a lost pin.key) is a problem to see, not a reason to quietly
                # write a smaller bundle over the good one.
                names = ", ".join(_display(n) for n in gone)
                _warn_once(f"gone:{names}", "backups: %s was in the last settings "
                           "backup and is missing now; no new settings backup is "
                           "written until it is back", names)
                return None, (f"{names} was in the last settings backup and is missing "
                              "from the hub now, so no new copy was saved. Put it back, "
                              "or set the backup password again to start fresh."), None
            if last == files:
                return have[0], None, sorted(files)   # nothing changed since the newest one
    blob = encrypt_bundle(pack_settings(files), key, salt)
    tmp = folder / f".partial-{stamp}.fhb"
    try:
        _write_private(tmp, blob)
        name = f"settings-{stamp}.fhb"
        os.replace(tmp, folder / name)
    finally:
        tmp.unlink(missing_ok=True)
    _prune_dir(folder, keep, "settings-", ".fhb")
    return name, None, sorted(files)


# --- one run ----------------------------------------------------------------------

def run_once(now: dt.datetime, *, db_path: str, out_dir: str, sources: dict,
             key_path: str, keep: dict | None = None, settings_keep: int = SETTINGS_KEEP,
             password_expected: bool = False, marker: str | None = None) -> dict:
    """One backup: the database snapshot into every tier, then the settings
    bundle. Never raises; each half reports its own failure:
    {ok, snapshot, bytes, error, settings_file, settings_error,
    settings_names, marker}.
    With no `marker` yet, the folder gets one and `marker` is its new id."""
    keep = keep or KEEP
    st = stamps(now)
    out = {"ok": False, "snapshot": None, "bytes": None, "error": None,
           "settings_file": None, "settings_error": None, "settings_names": None,
           "marker": None}
    base = Path(out_dir)
    try:
        for tier in TIERS:
            prune_names([], keep[tier])          # refuse a bad keep before any write
        tiers = {t: base / "db" / t for t in TIERS}
        for d in tiers.values():
            d.mkdir(parents=True, exist_ok=True)
            _sweep_partials(d)
        if (base / "settings").is_dir():
            _sweep_partials(base / "settings")
        _sweep_partials(base)
        snap, nbytes = _snapshot(db_path, tiers["hourly"], st["hourly"])
        for tier in ("daily", "weekly", "monthly"):
            _promote(snap, tiers[tier], st[tier])
        for tier in TIERS:
            _prune_dir(tiers[tier], keep[tier])
        if not marker:                 # the first good run marks the folder
            out["marker"] = adopt_folder(str(base))
        out.update(ok=True, snapshot=snap.name, bytes=nbytes)
    except Exception as e:      # noqa: BLE001 - reported, never raised
        msg = str(e) if isinstance(e, RuntimeError) else f"Couldn't write the backup ({e})."
        out["error"] = msg
        log.error("backups: the database backup failed: %s", msg)
    try:
        out["settings_file"], out["settings_error"], out["settings_names"] = _save_settings(
            sources, key_path, base / "settings", st["hourly"], settings_keep,
            password_expected)
    except Exception as e:      # noqa: BLE001 - reported, never raised
        out["settings_error"] = (str(e) if isinstance(e, RuntimeError)
                                 else f"Couldn't save the settings files ({e}).")
        log.error("backups: %s", out["settings_error"])
    return out


def record(conn, result: dict, at: dt.datetime, dir_label: str) -> None:
    """Write this run into the job's own record and into the shared
    backup_status heartbeat the wall's stale badge reads. `at` (the last good
    copy) only moves on success, so a failing job goes stale honestly. The box
    script's off-box fields are left as they are."""
    def get(key):
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        try:
            v = json.loads(row[0]) if row else None
        except ValueError:
            v = None
        return v if isinstance(v, dict) else {}

    when = at.isoformat()
    state, beat = get(STATE_KEY), get(HEARTBEAT_KEY)
    if result.get("ok"):
        state["last_ok"] = {"at": when, "snapshot": result["snapshot"], "bytes": result["bytes"]}
        state["last_error"] = None
        # The restore test's clock: the first one runs a day after this.
        if _parse_at(state.get("first_ok_at")) is None:
            state["first_ok_at"] = when
        beat.update(at=when, snapshot=result["snapshot"], bytes=result["bytes"],
                    source="in-app", in_app_error=None)
    else:
        state["last_error"] = {"at": when, "message": result.get("error") or "unknown"}
        beat["in_app_error"] = state["last_error"]["message"]
    if result.get("settings_file"):
        state["settings"] = {"at": when, "file": result["settings_file"]}
        if result.get("settings_names"):
            state["settings"]["files"] = list(result["settings_names"])
    if "settings_error" in result:
        state["settings_error"] = result.get("settings_error")
    beat["dir"] = dir_label
    beat["settings_at"] = (state.get("settings") or {}).get("at")
    rows = [(STATE_KEY, state), (HEARTBEAT_KEY, beat)]
    if result.get("marker"):
        rows.append((MARKER_KEY, result["marker"]))
    with conn:
        for key, value in rows:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                         (key, json.dumps(value)))


# --- the restore test ---------------------------------------------------------------
#
# Once a month the hub proves its newest backup really restores (and the
# owner sees it on the card). It works on a private copy in a temp folder,
# never on hub.db or the files in the backup folder, and a settings bundle is
# only ever opened in memory.

RESTORE_TEST_KEY = "backups.restore_test"
# The tables a restored hub cannot do without.
RESTORE_TABLES = ("kv", "people", "chores", "completions", "occurrence_log", "ledger", "todos")
# The tables whose row counts are held against the live database, and what
# the card calls their rows.
RESTORE_COUNTED = {"people": "people", "chores": "chores",
                   "occurrence_log": "chore history rows", "ledger": "wallet ledger rows"}
# A copy holding less than this share of a live table's rows looks stale or
# cut short. A table may also be one row short with no failure, so a person
# added since the last hourly copy is not a failed test.
RESTORE_MIN_SHARE = 0.9
RESTORE_SLACK_ROWS = 1
RESTORE_EVERY = dt.timedelta(days=30)        # after a passed test
RESTORE_RETRY = dt.timedelta(days=1)         # after a failed one, until it passes
RESTORE_FIRST_AFTER = dt.timedelta(days=1)   # the first, after the first good backup


def _parse_at(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def restore_test_due(state: dict, last: dict | None, now: dt.datetime) -> bool:
    """Whether the scheduled restore test should run now. `state` is the
    job's record (its first_ok_at starts the clock) and `last` the previous
    test's record. A passed test waits 30 days, a failed one a day."""
    if isinstance(last, dict):
        at = _parse_at(last.get("at"))
        if at is None:
            return True                 # a record no one can read: test again
        return now - at >= (RESTORE_EVERY if last.get("ok") else RESTORE_RETRY)
    first = _parse_at((state or {}).get("first_ok_at"))
    return first is not None and now - first >= RESTORE_FIRST_AFTER


def newest_snapshot(out_dir: str) -> Path | None:
    """The newest database copy: the newest hourly one, or the newest in the
    next tier down when the hourly folder is empty."""
    base = Path(out_dir) / "db"
    for tier in TIERS:
        d = base / tier
        names = sorted((n for n in os.listdir(d) if re.fullmatch(r"hub-[0-9W-]+\.db", n)),
                       reverse=True) if d.is_dir() else []
        if names:
            return d / names[0]
    return None


def live_counts(conn) -> dict:
    """Row counts of the counted tables in the live database (None for a
    table it does not have)."""
    out = {}
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in RESTORE_COUNTED:
        out[table] = (conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                      if table in have else None)
    return out


def _check_snapshot(snap: Path, live: dict, tmp_dir: str | None) -> tuple[str | None, dict]:
    """(problem or None, counts) for one database copy, tested on a private
    copy that is removed afterwards."""
    label = f"The newest backup ({snap.name})"
    counts = {}
    work = tempfile.mkdtemp(prefix="fh-restore-test-", dir=tmp_dir)
    try:
        copy = Path(work) / "hub.db"
        try:
            shutil.copyfile(snap, copy)
            os.chmod(copy, 0o600)
        except OSError as e:
            return f"{label} can't be copied for the test ({e}).", counts
        try:
            conn = sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)
        except sqlite3.Error as e:
            return f"{label} can't be opened as a database ({e}).", counts
        try:
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
            except sqlite3.DatabaseError as e:
                if "not a database" in str(e):
                    return f"{label} can't be opened as a database ({e}).", counts
                return f"{label} can't be read ({e}).", counts
            if [tuple(r) for r in rows] != [("ok",)]:
                first = rows[0][0] if rows else "no answer"
                return f"{label} failed its integrity check ({first}).", counts
            try:
                have = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                missing = [t for t in RESTORE_TABLES if t not in have]
                if missing:
                    return (f"{label} is missing tables the hub needs "
                            f"({', '.join(missing)})."), counts
                for table in RESTORE_COUNTED:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    counts[table] = {"backup": n, "live": live.get(table)}
            except sqlite3.Error as e:
                return f"{label} can't be read ({e}).", counts
        finally:
            conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for table, what in RESTORE_COUNTED.items():
        got, want = counts[table]["backup"], counts[table]["live"]
        if not want:
            continue
        if got == 0:
            return (f"{label} has no {what}, but the hub has {want}. "
                    "It looks old or cut short."), counts
        if got < want * RESTORE_MIN_SHARE and want - got > RESTORE_SLACK_ROWS:
            return (f"{label} has {got} {what}, but the hub has {want}. "
                    "It looks old or cut short."), counts
    return None, counts


def _check_settings(out_dir: str, key_path: str, recorded) -> tuple[str | None, str | None, bool]:
    """(problem or None, bundle name or None, checked) for the newest
    settings bundle, opened in memory with the stored key. With no usable key
    there is nothing to open it with, and the card already says so."""
    folder = Path(out_dir) / "settings"
    names = sorted((n for n in os.listdir(folder) if re.fullmatch(r"settings-[0-9-]+\.fhb", n)),
                   reverse=True) if folder.is_dir() else []
    pair = read_key(key_path)
    if pair is None:
        return None, None, False
    if not names:
        if recorded:
            return "No settings backup was found, but the last backup saved one.", None, True
        return None, None, False
    name = names[0]
    label = f"The newest settings backup ({name})"
    try:
        blob = (folder / name).read_bytes()
    except OSError as e:
        return f"{label} can't be read ({e}).", name, True
    salt, key = pair
    if blob[:len(MAGIC)] == MAGIC and blob[len(MAGIC):len(MAGIC) + SALT_LEN] != salt:
        return (f"{label} was made with an older backup password, so the hub's key "
                "can't open it. The next backup saves a new one; test again after it."), name, True
    try:
        plain = decrypt_bundle(blob, key=key)
    except BundleError:
        return (f"{label} can't be opened with the hub's backup key. The key file may be "
                "damaged. Set the backup password again."), name, True
    try:
        files = unpack_settings(plain)
    except BundleError as e:
        return f"{label} is damaged ({e}).", name, True
    if not files:
        return f"{label} holds no files.", name, True
    gone = sorted(set(recorded or ()) - set(files))
    if gone:
        return (f"{label} is missing {', '.join(_display(n) for n in gone)}, "
                "which the last backup saved."), name, True
    return None, name, True


def restore_test(out_dir: str, *, live: dict, key_path: str, settings_files=None,
                 tmp_dir: str | None = None) -> dict:
    """Prove the newest backup restores. Never raises:
    {ok, snapshot, reason, counts, settings_file, settings_checked}.
    `live` is live_counts() of hub.db; `settings_files` the short names the
    last backup recorded in its bundle (None when it recorded none)."""
    out = {"ok": False, "snapshot": None, "reason": None, "counts": {},
           "settings_file": None, "settings_checked": False}
    try:
        snap = newest_snapshot(out_dir)
        if snap is None:
            out["reason"] = "No database backup was found in the backup folder."
            return out
        out["snapshot"] = snap.name
        problem, out["counts"] = _check_snapshot(snap, live, tmp_dir)
        if problem:
            out["reason"] = problem
            return out
        problem, out["settings_file"], out["settings_checked"] = _check_settings(
            out_dir, key_path, settings_files)
        if problem:
            out["reason"] = problem
            return out
        out["ok"] = True
    except Exception as e:      # noqa: BLE001 - reported, never raised
        out["ok"] = False
        out["reason"] = f"The restore test couldn't finish ({e})."
    return out


def record_restore_test(conn, result: dict, at: dt.datetime) -> None:
    """Keep the test's result for the card. Only its own kv row is written:
    the backup heartbeat and the job's record are left alone. A failure is
    logged once for each new reason."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (RESTORE_TEST_KEY,)).fetchone()
    try:
        prev = json.loads(row[0]) if row else None
    except ValueError:
        prev = None
    prev = prev if isinstance(prev, dict) else None
    value = {"at": at.isoformat(), "ok": bool(result.get("ok")),
             "snapshot": result.get("snapshot"), "reason": result.get("reason"),
             "settings_file": result.get("settings_file")}
    if not value["ok"]:
        if prev is None or prev.get("ok") or prev.get("reason") != value["reason"]:
            log.error("backups: the restore test failed: %s", value["reason"])
    else:
        log.info("backups: the restore test passed (%s)", value["snapshot"])
    with conn:
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                     (RESTORE_TEST_KEY, json.dumps(value)))


# --- the command line ------------------------------------------------------------------

USAGE = """usage:
  python -m family_hub.backups restore-settings <file.fhb> <out dir>
      asks for the backup password and writes the settings files into <out dir>
  python -m family_hub.backups --list [backup folder]
      shows the newest backups (the folder defaults to BACKUPS_PATH or /backups)"""


def _restore(path: str, out_dir: str, ask_password) -> int:
    try:
        blob = Path(path).read_bytes()
    except OSError as e:
        print(f"Can't read {path}: {e}", file=sys.stderr)
        return 1
    try:
        files = unpack_settings(decrypt_bundle(blob, password=ask_password("Backup password: ")))
    except BundleError as e:
        print(str(e), file=sys.stderr)
        return 1
    out = Path(out_dir)
    clash = [RESTORE_NAMES[n] for n in files if (out / RESTORE_NAMES[n]).exists()]
    if clash:
        print(f"Not restoring: {', '.join(clash)} already exist in {out_dir}. "
              "Pick an empty folder.", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for short in sorted(files):
            target = out / RESTORE_NAMES[short]
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            written.append(target)
            with os.fdopen(fd, "wb") as f:
                f.write(files[short])
    except OSError as e:
        # all or nothing, so a rerun into the same folder works
        for t in written:
            t.unlink(missing_ok=True)
        print(f"Couldn't write the restored files ({e}). Nothing was kept.", file=sys.stderr)
        return 1
    print(f"Restored {len(files)} file(s) into {out_dir}:")
    for short in sorted(files):
        print(f"  {RESTORE_NAMES[short]:12} goes in {RESTORE_HOME[short]}")
    return 0


def main(argv=None, ask_password=getpass.getpass) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) == 3 and args[0] == "restore-settings":
        return _restore(args[1], args[2], ask_password)
    if args[:1] == ["--list"] and len(args) <= 2:
        folder = args[1] if len(args) == 2 else os.environ.get("BACKUPS_PATH", "/backups")
        found = newest(folder)
        if not any(found.values()):
            print(f"No backups found in {folder}.")
            return 0
        for group, names in found.items():
            print(f"{group}:")
            for n in names:
                print(f"  {n}")
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
