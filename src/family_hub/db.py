"""SQLite storage for family-hub.

WAL mode, Row factory, thread-safe connection (the background sync thread and
request handlers share one process). Dates are stored as 'YYYY-MM-DD' TEXT and
timestamps as ISO-8601 TEXT. `rotation_order` is JSON-encoded in storage and
decoded on read; `kv` values are JSON-encoded blobs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, color TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  reminder_list_id TEXT);   -- caldav:<slug> of this person's iCloud chore list (P2)
CREATE TABLE IF NOT EXISTS chores(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '',
  schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days','once','interval')),
  days_mask INTEGER NOT NULL DEFAULT 0,
  week_interval INTEGER NOT NULL DEFAULT 1,   -- 'days': 1=weekly, 2=biweekly
  interval_days INTEGER,                       -- 'interval': every N days from epoch
  due_times TEXT NOT NULL DEFAULT '[]',        -- JSON ["HH:MM",...] -> iOS notifications
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
  id TEXT NOT NULL, calendar_id TEXT NOT NULL, title TEXT NOT NULL,
  start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, all_day INTEGER NOT NULL,
  updated TEXT,
  location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  color_id TEXT,
  PRIMARY KEY(calendar_id, id));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS laundry_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  machine TEXT NOT NULL,
  prev_phase TEXT,
  phase TEXT NOT NULL,
  status TEXT,
  finishes_at TEXT,
  status_since TEXT,
  note TEXT);
CREATE INDEX IF NOT EXISTS laundry_log_machine_ts
  ON laundry_log(machine, ts);
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  bucket TEXT NOT NULL CHECK(bucket IN ('now','soon','later')),
  created_at TEXT NOT NULL,
  done_at TEXT,
  done_date TEXT);
-- Integrations registry: the runtime on/off state for each togglable data
-- source / tile ("extension"). The set of AVAILABLE integrations is computed
-- from config/env (integrations.available_integrations); this table only holds
-- the operator's enable/disable overlay, seeded enabled so an existing install
-- is unchanged. config_json is reserved for per-integration settings.
CREATE TABLE IF NOT EXISTS integrations(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  sort INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL);
-- CalDAV object store (the two-way foundation, TECHNICAL_DESIGN §5.1 / spec §5.2
-- / Fable rec 1). One row per iCloud VEVENT/VTODO OBJECT (not per expanded
-- occurrence), carrying the round-trip essentials writes need: uid + href +
-- etag/base_etag (optimistic concurrency), raw_ics (C1 fidelity), sequence, and
-- a sync_state outbox column. The read path still renders from `events`/kv for
-- now; this store is written from the first pull so a later write slice lands on
-- data that can already round-trip. sync_state: SYNCED | PENDING_CREATE |
-- PENDING_UPDATE | PENDING_DELETE.
CREATE TABLE IF NOT EXISTS cal_objects(
  id TEXT PRIMARY KEY,               -- collection_id + '/' + uid
  collection_id TEXT NOT NULL,       -- 'caldav:<slug>'
  comp_type TEXT NOT NULL,           -- 'VEVENT' | 'VTODO'
  uid TEXT NOT NULL,
  href TEXT,                         -- server resource URL (PUT/DELETE target)
  etag TEXT,                         -- last known server ETag
  base_etag TEXT,                    -- ETag a local edit was based on (If-Match)
  summary TEXT NOT NULL DEFAULT '',
  raw_ics TEXT,                      -- round-trip fidelity (C1)
  sequence INTEGER NOT NULL DEFAULT 0,
  last_modified TEXT,
  sync_state TEXT NOT NULL DEFAULT 'SYNCED',
  local_modified_at TEXT,
  sync_attempts INTEGER NOT NULL DEFAULT 0,
  last_sync_error TEXT);
-- Discovered CalDAV collections (Fable rec 2). One row per iCloud calendar /
-- reminders list, so the settings "calendar picker" has a persistent per-
-- collection visibility toggle (`enabled`) that survives sync, plus the metadata
-- the wall renders (display_name/color). ctag/sync_token are reserved for future
-- change detection.
CREATE TABLE IF NOT EXISTS caldav_collections(
  id TEXT PRIMARY KEY,               -- 'caldav:<slug>'
  comp_type TEXT NOT NULL,           -- 'VEVENT' | 'VTODO'
  display_name TEXT NOT NULL DEFAULT '',
  color TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  ctag TEXT,
  sync_token TEXT,
  last_seen_at TEXT);
-- Chore mirror ledger (P3): one row per (chore occurrence) mirrored into a
-- person's iCloud list, mapping it to the cal_objects row + stable UID so the
-- reconcile can create/move/prune and two-way completion can map an iCloud
-- check-off back to (chore, date, person).
CREATE TABLE IF NOT EXISTS chore_mirror(
  chore_id INTEGER NOT NULL,
  date TEXT NOT NULL,                 -- occurrence date 'YYYY-MM-DD'
  person_id INTEGER NOT NULL,
  cal_object_id TEXT NOT NULL,        -- the cal_objects row we created
  uid TEXT NOT NULL,                  -- 'familyhub-chore-<id>-<date>'
  sig TEXT,                           -- content signature; reconcile re-pushes on drift
  PRIMARY KEY(chore_id, date));
"""

# Columns a caller may set through update_person / add_chore validation.
_PERSON_FIELDS = {"name", "color", "sort", "active", "reminder_list_id"}
_CHORE_COLUMNS = {
    "title", "icon", "schedule_kind", "days_mask", "week_interval",
    "interval_days", "due_times", "assign_kind",
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
    # 2026-08-16: events used a bare `id` PRIMARY KEY, but Google reuses one
    # event's id across every calendar it appears on, so the same event on two
    # configured family calendars produced a duplicate-id INSERT that aborted
    # the whole sync (issue #30). Rebuild with PRIMARY KEY(calendar_id, id) so
    # cross-calendar copies coexist. A DB created before this keeps its old PK
    # (CREATE TABLE IF NOT EXISTS never rewrites it) -> detect and rebuild.
    ev_pk = {r["name"]: r["pk"]
             for r in conn.execute("PRAGMA table_info(events)")}
    if ev_pk and ev_pk.get("id", 0) > 0 and ev_pk.get("calendar_id", 0) == 0:
        _rebuild_events_composite_pk(conn)
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
    # 2026-08-16: people/chores used INTEGER PRIMARY KEY without AUTOINCREMENT,
    # so SQLite reused a deleted row's id. completions/occurrence_log deliberately
    # keep frozen-history rows keyed by that id, so a new person/chore silently
    # inherited a deleted one's history (issue #31). Rebuild with AUTOINCREMENT so
    # ids are never reused. A DB created before this keeps its old id column, and
    # so does one the chores-widen rebuild just produced -> detect and rebuild.
    _people_ai = """CREATE TABLE people_new(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, color TEXT NOT NULL,
      sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)"""
    _chores_ai = """CREATE TABLE chores_new(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      icon TEXT NOT NULL DEFAULT '',
      schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days','once')),
      days_mask INTEGER NOT NULL DEFAULT 0,
      assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
      fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
      rotation_epoch TEXT NOT NULL,
      sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)"""
    for tbl, ddl, cols in (
            ("people", _people_ai, "id, name, color, sort, active"),
            ("chores", _chores_ai,
             "id, title, icon, schedule_kind, days_mask, assign_kind, "
             "fixed_person_id, rotation_order, rotation_epoch, sort, active")):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (tbl,)).fetchone()
        if row and "AUTOINCREMENT" not in row["sql"].upper():
            _rebuild_with_autoincrement(conn, tbl, ddl, cols)
    # 2026-08-18: richer routines — every-N-days ('interval' kind), biweekly
    # (week_interval), and due-time notifications (due_times). Adds three columns
    # and widens schedule_kind to allow 'interval'. A DB that already carries
    # AUTOINCREMENT + the 'once' CHECK went through neither rebuild above, so
    # migrate it here. Idempotent: skips once 'interval' is in the CHECK.
    chores_sql2 = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chores'"
    ).fetchone()
    if chores_sql2 and "'interval'" not in chores_sql2["sql"]:
        _migrate_chores_routines(conn)
    # 2026-08-18: people gained reminder_list_id (their iCloud chore list, P2).
    # Nullable, so a plain additive ALTER is enough — no rebuild.
    ppl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
    if "reminder_list_id" not in ppl_cols:
        conn.execute("ALTER TABLE people ADD COLUMN reminder_list_id TEXT")
        conn.commit()


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


def _migrate_chores_routines(conn: sqlite3.Connection) -> None:
    """Rebuild `chores` with the richer-routine columns (week_interval,
    interval_days, due_times) and the widened schedule_kind CHECK ('interval'),
    preserving every row (new columns take their defaults), as ONE atomic
    transaction — same crash-safety contract as _widen_chore_schedule_check. Keeps
    AUTOINCREMENT so a DB that already earned it never regresses to id reuse."""
    prior_iso = conn.isolation_level
    try:
        conn.isolation_level = None        # explicit BEGIN/COMMIT/ROLLBACK are ours
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS chores_new")
        conn.execute("""CREATE TABLE chores_new(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          icon TEXT NOT NULL DEFAULT '',
          schedule_kind TEXT NOT NULL
            CHECK(schedule_kind IN ('daily','days','once','interval')),
          days_mask INTEGER NOT NULL DEFAULT 0,
          week_interval INTEGER NOT NULL DEFAULT 1,
          interval_days INTEGER,
          due_times TEXT NOT NULL DEFAULT '[]',
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


def _rebuild_events_composite_pk(conn: sqlite3.Connection) -> None:
    """Rebuild `events` with PRIMARY KEY(calendar_id, id) instead of a bare `id`
    PK, so the same event id on two configured calendars no longer collides
    (issue #30). Same atomic-rebuild contract as _drop_completions_chore_fk: an
    interrupted rebuild rolls back whole and the next boot retries from a clean
    slate, and the leading DROP IF EXISTS clears any events_new stranded by a
    prior crash. Old rows carried a globally-unique id, so no dedup is needed."""
    prior_iso = conn.isolation_level
    try:
        conn.isolation_level = None        # explicit BEGIN/COMMIT/ROLLBACK are ours
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS events_new")
        conn.execute("""CREATE TABLE events_new(
          id TEXT NOT NULL, calendar_id TEXT NOT NULL, title TEXT NOT NULL,
          start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, all_day INTEGER NOT NULL,
          updated TEXT,
          location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
          color_id TEXT,
          PRIMARY KEY(calendar_id, id))""")
        conn.execute("""INSERT INTO events_new(
          id, calendar_id, title, start_ts, end_ts, all_day, updated,
          location, description, color_id)
          SELECT id, calendar_id, title, start_ts, end_ts, all_day, updated,
          location, description, color_id FROM events""")
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_new RENAME TO events")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = prior_iso


def _rebuild_with_autoincrement(conn: sqlite3.Connection, table: str,
                                create_ddl: str, cols: str) -> None:
    """Rebuild `table` (people/chores) so its INTEGER PRIMARY KEY is
    AUTOINCREMENT, preserving every row and its id (issue #31). `create_ddl` must
    create `<table>_new`; `cols` is the shared column list. Same atomic-rebuild
    contract as the other helpers. After the swap the AUTOINCREMENT counter
    (sqlite_sequence) is pinned to the max preserved id, so the next insert is
    max+1 and never a reused id — independent of whether this SQLite propagates
    sqlite_sequence across the RENAME."""
    prior_iso = conn.isolation_level
    try:
        conn.isolation_level = None        # explicit BEGIN/COMMIT/ROLLBACK are ours
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS {table}_new")
        conn.execute(create_ddl)                       # creates <table>_new (AUTOINCREMENT)
        conn.execute(f"INSERT INTO {table}_new({cols}) SELECT {cols} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN (?, ?)",
                     (table, f"{table}_new"))
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) "
            f"SELECT ?, COALESCE(MAX(id), 0) FROM {table}", (table,))
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
              fixed_person_id, rotation_order, rotation_epoch,
              week_interval=1, interval_days=None, due_times=None) -> int:
    cur = conn.execute(
        """INSERT INTO chores(title, icon, schedule_kind, days_mask, week_interval,
                              interval_days, due_times, assign_kind,
                              fixed_person_id, rotation_order, rotation_epoch)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, icon, schedule_kind, days_mask, week_interval, interval_days,
         json.dumps(due_times or []), assign_kind, fixed_person_id,
         json.dumps(rotation_order), rotation_epoch))
    conn.commit()
    return int(cur.lastrowid)


def _chore_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["rotation_order"] = json.loads(d["rotation_order"])
    d["due_times"] = json.loads(d.get("due_times") or "[]")
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
    if "due_times" in cols:
        cols["due_times"] = json.dumps(cols["due_times"] or [])
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


def completion_exists(conn, chore_id: int, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM completions WHERE chore_id = ? AND date = ?",
        (chore_id, date)).fetchone() is not None


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

def _replace_events(conn, events: list[dict], scope_sql: str,
                    keep_ids: tuple) -> None:
    """Replace the cached events within one SOURCE scope (a WHERE clause on
    calendar_id) so the Google/ICS sync and the CalDAV sync never delete each
    other's rows in the shared table. keep_ids preserves a source whose fetch
    failed this round. OR REPLACE: the PK is (calendar_id, id), so cross-calendar
    copies coexist and an in-batch duplicate replaces rather than aborting."""
    with conn:  # one transaction
        if keep_ids:
            q = ",".join("?" * len(keep_ids))
            conn.execute(
                f"DELETE FROM events WHERE ({scope_sql}) "
                f"AND calendar_id NOT IN ({q})", tuple(keep_ids))
        else:
            conn.execute(f"DELETE FROM events WHERE {scope_sql}")
        conn.executemany(
            "INSERT OR REPLACE INTO events(id, calendar_id, title, start_ts, "
            "end_ts, all_day, updated, location, description, color_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e["id"], e["calendar_id"], e["title"], e["start_ts"],
              e["end_ts"], e["all_day"], e.get("updated"),
              e.get("location", ""), e.get("description", ""),
              e.get("color_id")) for e in events])


def replace_events(conn, events: list[dict], keep_ids: tuple = ()) -> None:
    """Replace the config-sourced (Google/ICS) cached window. CalDAV rows
    (calendar_id 'caldav:%') are managed separately and left untouched."""
    _replace_events(conn, events, "calendar_id NOT LIKE 'caldav:%'", keep_ids)


def replace_events_caldav(conn, events: list[dict], keep_ids: tuple = ()) -> None:
    """Replace the CalDAV (iCloud) cached window only; Google/ICS rows survive."""
    _replace_events(conn, events, "calendar_id LIKE 'caldav:%'", keep_ids)


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


# --- laundry cycle log ----------------------------------------------------
#
# Append-only history of observed washer/dryer phase TRANSITIONS (never
# per-poll rows — an idle machine writes nothing), plus exactly one
# status-keyed event: a person powering the machine on while a Done hold
# stands (note hold_cleared_by_power_on, prev_phase == phase == "idle") —
# the collection moment, which is the number that sizes the hold window.
# The evidence base for tuning the finish-detection heuristics (watcher
# cadence, missed-done hold, projection trust) from real cycles instead of
# guesses, and for diagnosing any finish the wall got wrong. Lives in
# hub.db, so the standard tiered backups cover it.
#
# `note` vocabulary (written by app._laundry_annotate): missed_finish /
# stale_projection / cycle_exit / auto_off_hold / auto_off_refused (the
# machine powered itself off after an observed end but the end stamp was
# too stale/unusable to re-present as Done) / hold_cleared_by_power_on,
# each optionally carrying a "+offline_bridge" suffix when the transition
# was resolved across an HA blip — consumers must match by PREFIX, not
# equality. NULL = an ordinary transition (start, observed end, blip, …).

LAUNDRY_LOG_KEEP_DAYS = 365


def laundry_log_add(conn, machine: str, prev_phase: str | None, phase: str,
                    status: str | None, finishes_at: str | None,
                    status_since: str | None, note: str | None = None) -> None:
    """One transition row, stamped with the observation time. Prunes rows
    older than the keep window on every write — a few rows per laundry day
    keeps the table tiny, so inline pruning is cheaper than a scheduled job
    that could silently stop running."""
    conn.execute(
        "INSERT INTO laundry_log(ts, machine, prev_phase, phase, status,"
        " finishes_at, status_since, note) VALUES(?,?,?,?,?,?,?,?)",
        (_now_iso(), machine, prev_phase, phase, status, finishes_at,
         status_since, note))
    conn.execute(
        "DELETE FROM laundry_log WHERE ts < ?",
        ((datetime.now(timezone.utc)
          - timedelta(days=LAUNDRY_LOG_KEEP_DAYS)).isoformat(),))
    conn.commit()


def laundry_log_recent(conn, machine: str | None = None,
                       limit: int = 200) -> list[dict]:
    """Newest-first transition rows, optionally for one machine."""
    q = ("SELECT ts, machine, prev_phase, phase, status, finishes_at,"
         " status_since, note FROM laundry_log")
    args: list = []
    if machine is not None:
        q += " WHERE machine = ?"
        args.append(machine)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# --- integrations ---------------------------------------------------------

def seed_integration(conn, iid: str, kind: str, sort: int = 0) -> None:
    """Insert an integration row if absent (default enabled). Idempotent, so a
    re-seed on startup never flips an operator's existing toggle."""
    conn.execute(
        "INSERT OR IGNORE INTO integrations(id, kind, enabled, config_json, "
        "sort, created_at) VALUES(?, ?, 1, '{}', ?, ?)",
        (iid, kind, sort, _now_iso()))
    conn.commit()


def list_integrations(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM integrations ORDER BY sort, id")
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.pop("config_json") or "{}")
        except Exception:
            d["config"] = {}
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def integration_enabled(conn, iid: str, default: bool = True) -> bool:
    """Enabled state for one integration; `default` when it has no row yet (an
    unseeded integration reads as enabled, so gating never hides a source the
    operator has not explicitly turned off)."""
    row = conn.execute(
        "SELECT enabled FROM integrations WHERE id = ?", (iid,)).fetchone()
    return default if row is None else bool(row["enabled"])


def set_integration_enabled(conn, iid: str, enabled: bool) -> bool:
    """Toggle an integration. Returns False if no such row (caller seeds first)."""
    cur = conn.execute("UPDATE integrations SET enabled = ? WHERE id = ?",
                       (1 if enabled else 0, iid))
    conn.commit()
    return cur.rowcount > 0


# --- caldav object store (two-way foundation) -----------------------------

def upsert_cal_object_synced(conn, obj: dict, force: bool = False) -> None:
    """Store an object pulled from the server as SYNCED. By default NEVER
    overwrites a row that has un-pushed local changes (sync_state PENDING_*), so
    a routine pull can't stomp a queued edit. `force=True` is the conflict
    resolver's server-wins path: a 412 means the server changed under our edit, so
    we deliberately adopt the server copy over the losing local edit. base_etag is
    set to the server etag (the base a future edit builds on)."""
    row = conn.execute("SELECT sync_state FROM cal_objects WHERE id = ?",
                       (obj["id"],)).fetchone()
    if row is not None and row["sync_state"] != "SYNCED" and not force:
        return
    conn.execute(
        "INSERT OR REPLACE INTO cal_objects(id, collection_id, comp_type, uid, "
        "href, etag, base_etag, summary, raw_ics, sequence, last_modified, "
        "sync_state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SYNCED')",
        (obj["id"], obj["collection_id"], obj["comp_type"], obj["uid"],
         obj.get("href"), obj.get("etag"), obj.get("etag"),
         obj.get("summary", ""), obj.get("raw_ics"),
         int(obj.get("sequence") or 0), obj.get("last_modified")))
    conn.commit()


def list_cal_objects(conn, comp_type: str | None = None) -> list[dict]:
    if comp_type:
        rows = conn.execute(
            "SELECT * FROM cal_objects WHERE comp_type = ? ORDER BY id",
            (comp_type,))
    else:
        rows = conn.execute("SELECT * FROM cal_objects ORDER BY id")
    return [dict(r) for r in rows]


def prune_cal_objects(conn, collection_id: str, keep_ids) -> None:
    """Drop SYNCED objects in a collection not seen this pull (deleted remotely).
    Keeps PENDING_* rows (un-pushed local work)."""
    keep = tuple(keep_ids)
    with conn:
        if keep:
            q = ",".join("?" * len(keep))
            conn.execute(
                f"DELETE FROM cal_objects WHERE collection_id = ? "
                f"AND sync_state = 'SYNCED' AND id NOT IN ({q})",
                (collection_id, *keep))
        else:
            conn.execute(
                "DELETE FROM cal_objects WHERE collection_id = ? "
                "AND sync_state = 'SYNCED'", (collection_id,))


def caldav_pending(conn) -> list[dict]:
    """The outbox: objects with un-pushed local changes, oldest first (the write
    slice flushes these to iCloud)."""
    rows = conn.execute(
        "SELECT * FROM cal_objects WHERE sync_state != 'SYNCED' "
        "ORDER BY local_modified_at, id")
    return [dict(r) for r in rows]


def get_cal_object(conn, oid: str) -> dict | None:
    row = conn.execute("SELECT * FROM cal_objects WHERE id = ?", (oid,)).fetchone()
    return dict(row) if row is not None else None


def queue_cal_object_update(conn, oid: str, raw_ics: str, summary: str,
                            now_iso: str) -> bool:
    """Mark an existing pulled object as edited-locally (PENDING_UPDATE) so the
    next sync PUTs it. Keeps base_etag (the If-Match the push builds on). No-op
    (returns False) if the row is gone. A row already mid-create (PENDING_CREATE)
    stays a create — we only overwrite its body — so a quick edit after add
    doesn't turn into an update against a server object that doesn't exist yet."""
    row = conn.execute("SELECT sync_state FROM cal_objects WHERE id = ?",
                       (oid,)).fetchone()
    if row is None:
        return False
    state = "PENDING_CREATE" if row["sync_state"] == "PENDING_CREATE" \
        else "PENDING_UPDATE"
    conn.execute(
        "UPDATE cal_objects SET raw_ics = ?, summary = ?, sync_state = ?, "
        "local_modified_at = ?, sync_attempts = 0, last_sync_error = NULL "
        "WHERE id = ?", (raw_ics, summary, state, now_iso, oid))
    conn.commit()
    return True


def queue_cal_object_create(conn, obj: dict, now_iso: str) -> None:
    """Insert a wall-created object as PENDING_CREATE (no href/etag yet — the push
    assigns them)."""
    conn.execute(
        "INSERT OR REPLACE INTO cal_objects(id, collection_id, comp_type, uid, "
        "href, etag, base_etag, summary, raw_ics, sequence, sync_state, "
        "local_modified_at) VALUES(?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, "
        "'PENDING_CREATE', ?)",
        (obj["id"], obj["collection_id"], obj["comp_type"], obj["uid"],
         obj.get("summary", ""), obj.get("raw_ics"), now_iso))
    conn.commit()


def queue_cal_object_delete(conn, oid: str, now_iso: str) -> bool:
    """Queue a delete. If the object was never pushed (PENDING_CREATE), just drop
    the row — there's nothing on the server to DELETE. Otherwise mark
    PENDING_DELETE so the flush removes it server-side then locally. False if the
    row is gone already."""
    row = conn.execute("SELECT sync_state FROM cal_objects WHERE id = ?",
                       (oid,)).fetchone()
    if row is None:
        return False
    if row["sync_state"] == "PENDING_CREATE":
        conn.execute("DELETE FROM cal_objects WHERE id = ?", (oid,))
    else:
        conn.execute(
            "UPDATE cal_objects SET sync_state = 'PENDING_DELETE', "
            "local_modified_at = ?, sync_attempts = 0, last_sync_error = NULL "
            "WHERE id = ?", (now_iso, oid))
    conn.commit()
    return True


def mark_cal_object_pushed(conn, oid: str, href, etag) -> None:
    """After a successful PUT: adopt the server href/etag and go back to SYNCED
    (base_etag = etag, the base the next edit builds on)."""
    conn.execute(
        "UPDATE cal_objects SET href = ?, etag = ?, base_etag = ?, "
        "sync_state = 'SYNCED', sync_attempts = 0, last_sync_error = NULL "
        "WHERE id = ?", (href, etag, etag, oid))
    conn.commit()


def delete_cal_object_row(conn, oid: str) -> None:
    """Remove a row outright (after a successful server DELETE)."""
    conn.execute("DELETE FROM cal_objects WHERE id = ?", (oid,))
    conn.commit()


def record_cal_object_error(conn, oid: str, err: str, now_iso: str) -> None:
    """A push failed: keep the row PENDING, bump the attempt count and record the
    error so it retries next sync and the operator can see why it's stuck."""
    conn.execute(
        "UPDATE cal_objects SET sync_attempts = sync_attempts + 1, "
        "last_sync_error = ?, local_modified_at = COALESCE(local_modified_at, ?) "
        "WHERE id = ?", (err[:500], now_iso, oid))
    conn.commit()


def integration_config(conn, iid: str) -> dict:
    """Per-integration JSON config (e.g. CalDAV's readonly / 1-way vs 2-way)."""
    row = conn.execute("SELECT config_json FROM integrations WHERE id = ?",
                       (iid,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["config_json"] or "{}")
    except Exception:
        return {}


def set_integration_config(conn, iid: str, config: dict) -> None:
    """Persist an integration's JSON config. Raises if the integration has no row
    (seed it first) rather than silently dropping the write — a swallowed config
    update is how a 'two-way' toggle would appear to save yet never take effect."""
    cur = conn.execute("UPDATE integrations SET config_json = ? WHERE id = ?",
                        (json.dumps(config), iid))
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"no integration row {iid!r} (seed it before set config)")


# --- caldav collections (the calendar picker) -----------------------------

def upsert_caldav_collection(conn, cid: str, comp_type: str, name: str,
                             color, now_iso: str) -> None:
    """Record a discovered collection. Inserts new ones ENABLED; on re-discovery
    updates metadata (name/color/last_seen) but NEVER the enabled toggle, so the
    operator's picker choice survives every sync."""
    conn.execute(
        "INSERT OR IGNORE INTO caldav_collections(id, comp_type, display_name, "
        "color, enabled, last_seen_at) VALUES(?, ?, ?, ?, 1, ?)",
        (cid, comp_type, name, color, now_iso))
    conn.execute(
        "UPDATE caldav_collections SET comp_type = ?, display_name = ?, "
        "color = ?, last_seen_at = ? WHERE id = ?",
        (comp_type, name, color, now_iso, cid))
    conn.commit()


def list_caldav_collections(conn, comp_type: str | None = None) -> list[dict]:
    if comp_type:
        rows = conn.execute(
            "SELECT * FROM caldav_collections WHERE comp_type = ? "
            "ORDER BY display_name, id", (comp_type,))
    else:
        rows = conn.execute(
            "SELECT * FROM caldav_collections ORDER BY display_name, id")
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


# --- chore mirror ledger (P3) ---------------------------------------------

def upsert_chore_mirror(conn, chore_id: int, date: str, person_id: int,
                        cal_object_id: str, uid: str, sig: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO chore_mirror(chore_id, date, person_id, "
        "cal_object_id, uid, sig) VALUES(?, ?, ?, ?, ?, ?)",
        (chore_id, date, person_id, cal_object_id, uid, sig))
    conn.commit()


def list_chore_mirror(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM chore_mirror")]


def delete_chore_mirror(conn, chore_id: int, date: str) -> None:
    conn.execute("DELETE FROM chore_mirror WHERE chore_id = ? AND date = ?",
                 (chore_id, date))
    conn.commit()


def get_chore_mirror(conn, chore_id: int, date: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM chore_mirror WHERE chore_id = ? AND date = ?",
        (chore_id, date)).fetchone()
    return dict(row) if row is not None else None


def get_chore_mirror_by_uid(conn, uid: str) -> dict | None:
    """Map an iCloud object's UID back to its (chore, date, person) — the P4
    two-way completion path uses this to record a check-off done in iOS."""
    row = conn.execute("SELECT * FROM chore_mirror WHERE uid = ?",
                       (uid,)).fetchone()
    return dict(row) if row is not None else None


def caldav_collection_enabled(conn, cid: str, default: bool = True) -> bool:
    row = conn.execute(
        "SELECT enabled FROM caldav_collections WHERE id = ?", (cid,)).fetchone()
    return default if row is None else bool(row["enabled"])


def set_caldav_collection_enabled(conn, cid: str, enabled: bool) -> bool:
    cur = conn.execute("UPDATE caldav_collections SET enabled = ? WHERE id = ?",
                       (1 if enabled else 0, cid))
    conn.commit()
    return cur.rowcount > 0
