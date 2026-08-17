"""SQLite storage for family-hub.

WAL mode, Row factory, thread-safe connection (the background sync thread and
request handlers share one process). Dates are stored as 'YYYY-MM-DD' TEXT and
timestamps as ISO-8601 TEXT. `rotation_order` is JSON-encoded in storage and
decoded on read; `kv` values are JSON-encoded blobs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS chores(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '',
  schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days','once')),
  days_mask INTEGER NOT NULL DEFAULT 0,
  assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
  fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
  rotation_epoch TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS completions(
  chore_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  person_id INTEGER NOT NULL REFERENCES people(id),
  done_at TEXT NOT NULL, PRIMARY KEY(chore_id, date));
CREATE TABLE IF NOT EXISTS occurrence_log(
  date TEXT NOT NULL,
  chore_id INTEGER NOT NULL,
  person_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  icon TEXT NOT NULL DEFAULT '',
  rot INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(date, chore_id));
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL, title TEXT NOT NULL,
  start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, all_day INTEGER NOT NULL,
  updated TEXT,
  location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  color_id TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  bucket TEXT NOT NULL CHECK(bucket IN ('now','soon','later')),
  created_at TEXT NOT NULL,
  done_at TEXT,
  done_date TEXT);
"""

# Columns a caller may set through update_person / add_chore validation.
_PERSON_FIELDS = {"name", "color", "sort", "active"}
_CHORE_COLUMNS = {
    "title", "icon", "schedule_kind", "days_mask", "assign_kind",
    "fixed_person_id", "rotation_order", "rotation_epoch", "sort", "active",
}
_TODO_FIELDS = {"title", "bucket"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # With per-thread connections (app._db) and the background sync thread, more
    # than one connection writes the same file. Give SQLite a busy timeout so a
    # writer waits for the lock instead of failing "database is locked" at once.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # in-place migration for DBs created before the event-detail columns
    # (2026-08-12: location/description/color_id feed the wall's event cards)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    for col, ddl in (("location", "TEXT NOT NULL DEFAULT ''"),
                     ("description", "TEXT NOT NULL DEFAULT ''"),
                     ("color_id", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
    conn.commit()
    # 2026-08-15: completions used to FK-reference chores(id), which made
    # deleting a chore impossible without also purging its history. Frozen
    # history keeps those rows, so rebuild the table without that FK.
    fk_parents = {r["table"]
                  for r in conn.execute("PRAGMA foreign_key_list(completions)")}
    if "chores" in fk_parents:
        _drop_completions_chore_fk(conn)
    # 2026-08-15: one-time chores added 'once' to the schedule_kind CHECK. A DB
    # created before that keeps its old CHECK (CREATE TABLE IF NOT EXISTS never
    # rewrites it), so a 'once' insert would fail the constraint. Rebuild the
    # table with the widened CHECK, preserving every chore row.
    chores_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chores'"
    ).fetchone()
    if chores_sql and "'once'" not in chores_sql["sql"]:
        _widen_chore_schedule_check(conn)


def _drop_completions_chore_fk(conn: sqlite3.Connection) -> None:
    """Rebuild `completions` without its legacy FK to chores(id), preserving
    every row, as ONE atomic transaction. An interrupted rebuild (crash, power
    loss, disk-full) rolls back whole, so the family's completion history is
    never half-migrated or lost and the next boot retries from a clean slate.

    Not `executescript` (which force-commits each statement, defeating the
    transaction) and not `with conn:` alone — FK enforcement can't toggle
    inside a transaction, so it is set around an explicit BEGIN/COMMIT with the
    connection in autocommit mode. The leading DROP IF EXISTS clears any
    completions_new stranded by a pre-atomic crash, so a retry can't wedge on a
    duplicate CREATE."""
    prior_iso = conn.isolation_level
    try:
        conn.isolation_level = None        # explicit BEGIN/COMMIT/ROLLBACK are ours
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS completions_new")
        conn.execute("""CREATE TABLE completions_new(
                          chore_id INTEGER NOT NULL,
                          date TEXT NOT NULL,
                          person_id INTEGER NOT NULL REFERENCES people(id),
                          done_at TEXT NOT NULL, PRIMARY KEY(chore_id, date))""")
        conn.execute("INSERT INTO completions_new SELECT * FROM completions")
        conn.execute("DROP TABLE completions")
        conn.execute("ALTER TABLE completions_new RENAME TO completions")
        conn.execute("COMMIT")
    except Exception:
        # roll back only if BEGIN actually opened a transaction, so a failure
        # BEFORE it (e.g. the PRAGMA) doesn't mask itself with "no transaction"
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        # always restore FK enforcement and the connection's transaction mode,
        # even if the rebuild raised before/inside the transaction
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = prior_iso


def _widen_chore_schedule_check(conn: sqlite3.Connection) -> None:
    """Rebuild `chores` so its schedule_kind CHECK also allows 'once', keeping
    every existing row, as ONE atomic transaction. SQLite can't ALTER a CHECK
    constraint in place, so the table is recreated. Same crash-safety contract
    as _drop_completions_chore_fk: an interrupted rebuild (crash, power loss)
    rolls back whole and the next boot retries from a clean slate, and the
    leading DROP IF EXISTS clears any chores_new stranded by a prior crash.

    FK enforcement is turned OFF around the swap: completions/occurrence_log no
    longer FK-reference chores(id) (see _drop_completions_chore_fk), but a DB
    old enough to predate the 'once' CHECK may still carry that legacy FK, and
    dropping the parent under enforcement would fail."""
    prior_iso = conn.isolation_level
    try:
        conn.isolation_level = None        # explicit BEGIN/COMMIT/ROLLBACK are ours
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS chores_new")
        conn.execute("""CREATE TABLE chores_new(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          icon TEXT NOT NULL DEFAULT '',
          schedule_kind TEXT NOT NULL
            CHECK(schedule_kind IN ('daily','days','once')),
          days_mask INTEGER NOT NULL DEFAULT 0,
          assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
          fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
          rotation_epoch TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)""")
        conn.execute("""INSERT INTO chores_new(
          id, title, icon, schedule_kind, days_mask, assign_kind,
          fixed_person_id, rotation_order, rotation_epoch, sort, active)
          SELECT id, title, icon, schedule_kind, days_mask, assign_kind,
          fixed_person_id, rotation_order, rotation_epoch, sort, active
          FROM chores""")
        conn.execute("DROP TABLE chores")
        conn.execute("ALTER TABLE chores_new RENAME TO chores")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = prior_iso


# --- people ---------------------------------------------------------------

def add_person(conn, name: str, color: str) -> int:
    cur = conn.execute(
        "INSERT INTO people(name, color) VALUES(?, ?)", (name, color))
    conn.commit()
    return int(cur.lastrowid)


def list_people(conn, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM people"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort, id"
    return [dict(r) for r in conn.execute(sql)]


def update_person(conn, pid: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _PERSON_FIELDS}
    if not cols:
        return
    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE people SET {assignments} WHERE id = ?",
                 (*cols.values(), pid))
    conn.commit()


def delete_person(conn, pid: int) -> bool:
    """Hard-delete a person, history-safe, as ONE transaction. Their live
    check-offs (completions) are removed — required anyway to clear the
    completions -> people(id) FK — and they're stripped from every chore's
    assignment: dropped from any rotation_order, and a fixed assignment to them
    nulled (that chore then simply goes unassigned until reassigned, exactly as
    plan_rows already treats an unresolvable assignee). The frozen
    occurrence_log is deliberately left untouched — past days keep their
    snapshot — but those rows won't render once the person is gone, since the
    day plan only builds cards for current people. Returns True if the person
    existed. All-or-nothing: an interrupted delete rolls back whole rather than
    stranding a person half-removed from their rotations."""
    with conn:
        if conn.execute("SELECT 1 FROM people WHERE id = ?",
                        (pid,)).fetchone() is None:
            return False
        for ch in conn.execute(
                "SELECT id, rotation_order FROM chores").fetchall():
            order = json.loads(ch["rotation_order"])
            if pid in order:
                conn.execute(
                    "UPDATE chores SET rotation_order = ? WHERE id = ?",
                    (json.dumps([x for x in order if x != pid]), ch["id"]))
        conn.execute("UPDATE chores SET fixed_person_id = NULL "
                     "WHERE fixed_person_id = ?", (pid,))
        conn.execute("DELETE FROM completions WHERE person_id = ?", (pid,))
        conn.execute("DELETE FROM people WHERE id = ?", (pid,))
        return True


# --- chores ---------------------------------------------------------------

def add_chore(conn, *, title, icon, schedule_kind, days_mask, assign_kind,
              fixed_person_id, rotation_order, rotation_epoch) -> int:
    cur = conn.execute(
        """INSERT INTO chores(title, icon, schedule_kind, days_mask, assign_kind,
                              fixed_person_id, rotation_order, rotation_epoch)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, icon, schedule_kind, days_mask, assign_kind, fixed_person_id,
         json.dumps(rotation_order), rotation_epoch))
    conn.commit()
    return int(cur.lastrowid)


def _chore_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["rotation_order"] = json.loads(d["rotation_order"])
    return d


def list_chores(conn, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM chores"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort, id"
    return [_chore_row(r) for r in conn.execute(sql)]


def update_chore(conn, cid: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _CHORE_COLUMNS}
    if not cols:
        return
    if "rotation_order" in cols:
        cols["rotation_order"] = json.dumps(cols["rotation_order"])
    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE chores SET {assignments} WHERE id = ?",
                 (*cols.values(), cid))
    conn.commit()


def delete_chore(conn, cid: int) -> bool:
    """Delete a chore definition. Its completion and occurrence-log rows are
    KEPT — history is frozen; deletion only removes the chore from today
    onward (today's log rows drop out on the next day-plan write). Returns
    True if it existed."""
    with conn:
        cur = conn.execute("DELETE FROM chores WHERE id = ?", (cid,))
        return cur.rowcount > 0


# --- occurrence log (frozen chore history) --------------------------------
#
# One row per (date, chore) the wall actually served: who it was assigned to
# and the display snapshot (title/icon/rot). Past days render from these rows,
# never from live chore definitions, so edits and deletions can't rewrite
# history. No FKs on purpose — rows must outlive their chore.

def _log_row(r: sqlite3.Row) -> dict:
    return dict(r)


def replace_day_log(conn, date: str, rows: list[dict]) -> None:
    """Freeze ``date``'s plan: fully replace that day's log rows."""
    with conn:
        conn.execute("DELETE FROM occurrence_log WHERE date = ?", (date,))
        conn.executemany(
            "INSERT INTO occurrence_log(date, chore_id, person_id, title, "
            "icon, rot) VALUES(?, ?, ?, ?, ?, ?)",
            [(date, r["chore_id"], r["person_id"], r["title"], r["icon"],
              r["rot"]) for r in rows])


def day_log(conn, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM occurrence_log WHERE date = ? ORDER BY chore_id",
        (date,))
    return [_log_row(r) for r in rows]


def logs_between(conn, date_from: str, date_to: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM occurrence_log WHERE date >= ? AND date <= ? "
        "ORDER BY date, chore_id", (date_from, date_to))
    return [_log_row(r) for r in rows]


def log_row(conn, chore_id: int, date: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM occurrence_log WHERE chore_id = ? AND date = ?",
        (chore_id, date)).fetchone()
    return _log_row(r) if r is not None else None


def backfill_occurrence_log(conn, day_rows: list, done_flag_key: str) -> None:
    """Insert reconstructed occurrence-log rows for many days AND set the
    backfill-done kv flag in ONE transaction — all-or-nothing. An interrupted
    legacy-upgrade backfill therefore rolls back whole and re-runs from scratch
    on the next boot, instead of freezing a partial history (whose missing days
    would read as rest days and silently inflate streaks). ``day_rows`` is a
    list of (date, rows) where each row has the plan_rows shape."""
    with conn:   # single transaction: every day + the flag, or nothing
        for date, rows in day_rows:
            conn.executemany(
                "INSERT INTO occurrence_log(date, chore_id, person_id, title, "
                "icon, rot) VALUES(?, ?, ?, ?, ?, ?)",
                [(date, r["chore_id"], r["person_id"], r["title"], r["icon"],
                  r["rot"]) for r in rows])
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                     (done_flag_key, json.dumps(True)))


# --- completions ----------------------------------------------------------

def set_completion(conn, chore_id: int, date: str, person_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO completions(chore_id, date, person_id, done_at) "
        "VALUES(?, ?, ?, ?)",
        (chore_id, date, person_id, _now_iso()))
    conn.commit()


def clear_completion(conn, chore_id: int, date: str) -> None:
    conn.execute("DELETE FROM completions WHERE chore_id = ? AND date = ?",
                 (chore_id, date))
    conn.commit()


def completions_between(conn, date_from: str, date_to: str) -> list[dict]:
    rows = conn.execute(
        "SELECT chore_id, date, person_id FROM completions "
        "WHERE date >= ? AND date <= ? ORDER BY date, chore_id",
        (date_from, date_to))
    return [dict(r) for r in rows]


# --- todos ----------------------------------------------------------------

def add_todo(conn, title: str, bucket: str) -> int:
    cur = conn.execute(
        "INSERT INTO todos(title, bucket, created_at) VALUES(?, ?, ?)",
        (title, bucket, _now_iso()))
    conn.commit()
    return int(cur.lastrowid)


def list_todos(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM todos ORDER BY created_at, id")
    return [dict(r) for r in rows]


def update_todo(conn, tid: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _TODO_FIELDS}
    if not cols:
        return
    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE todos SET {assignments} WHERE id = ?",
                 (*cols.values(), tid))
    conn.commit()


def set_todo_done(conn, tid: int, done_date: str) -> None:
    conn.execute("UPDATE todos SET done_at = ?, done_date = ? WHERE id = ?",
                 (_now_iso(), done_date, tid))
    conn.commit()


def clear_todo_done(conn, tid: int) -> None:
    conn.execute("UPDATE todos SET done_at = NULL, done_date = NULL WHERE id = ?",
                 (tid,))
    conn.commit()


def delete_todo(conn, tid: int) -> None:
    conn.execute("DELETE FROM todos WHERE id = ?", (tid,))
    conn.commit()


# --- events ---------------------------------------------------------------

def replace_events(conn, events: list[dict], keep_ids: tuple = ()) -> None:
    """Replace the cached window. Sources whose fetch failed this round pass
    their calendar_ids in keep_ids so their last-good events survive; every
    other row (including calendars removed from config) is replaced."""
    with conn:  # one transaction
        if keep_ids:
            q = ",".join("?" * len(keep_ids))
            conn.execute(
                f"DELETE FROM events WHERE calendar_id NOT IN ({q})",
                tuple(keep_ids))
        else:
            conn.execute("DELETE FROM events")
        conn.executemany(
            "INSERT INTO events(id, calendar_id, title, start_ts, end_ts, "
            "all_day, updated, location, description, color_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e["id"], e["calendar_id"], e["title"], e["start_ts"],
              e["end_ts"], e["all_day"], e.get("updated"),
              e.get("location", ""), e.get("description", ""),
              e.get("color_id")) for e in events])


def list_events(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM events ORDER BY start_ts")
    return [dict(r) for r in rows]


def event_calendar_ids(conn) -> set:
    """The set of calendar_ids that currently have at least one cached event.
    Used by the sync to tell a source that just went (suspiciously) empty from
    one that was always empty."""
    rows = conn.execute("SELECT DISTINCT calendar_id FROM events")
    return {r["calendar_id"] for r in rows}


# --- kv -------------------------------------------------------------------

def kv_get(conn, key: str) -> Any | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row is not None else None


def kv_set(conn, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                 (key, json.dumps(value)))
    conn.commit()
