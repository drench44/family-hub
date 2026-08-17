import sqlite3

import pytest

from family_hub import db as fdb


class _CrashConn(sqlite3.Connection):
    """A connection that raises on the first execute() whose SQL contains
    ``crash_on`` — lets a test interrupt a migration deterministically to
    prove it rolls back whole instead of half-applying."""
    crash_on = None

    def execute(self, sql, *args):
        if self.crash_on and self.crash_on in sql:
            raise sqlite3.OperationalError(f"simulated crash on {self.crash_on}")
        return super().execute(sql, *args)


def _legacy_fk_db(path):
    """A DB whose completions table still FK-references chores(id) (the
    pre-frozen-history shape), with one person/chore/completion seeded."""
    c = sqlite3.connect(path, factory=_CrashConn)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE people(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE chores(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          icon TEXT NOT NULL DEFAULT '',
          schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days')),
          days_mask INTEGER NOT NULL DEFAULT 0,
          assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
          fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
          rotation_epoch TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE completions(
          chore_id INTEGER NOT NULL REFERENCES chores(id),
          date TEXT NOT NULL,
          person_id INTEGER NOT NULL REFERENCES people(id),
          done_at TEXT NOT NULL, PRIMARY KEY(chore_id, date));
    """)
    c.execute("INSERT INTO people(id, name, color) VALUES(7, 'Rem', '#5BC9F0')")
    c.execute("""INSERT INTO chores(id, title, schedule_kind, assign_kind,
                 fixed_person_id, rotation_epoch)
                 VALUES(1, 'Trash', 'daily', 'fixed', 7, '2026-08-01')""")
    c.execute("""INSERT INTO completions VALUES(1, '2026-08-12', 7,
                 '2026-08-12T20:00:00+00:00')""")
    c.commit()
    return c


def test_schema_idempotent(conn):
    fdb.ensure_schema(conn)  # second call must not raise


def test_person_crud(conn):
    pid = fdb.add_person(conn, "Rem", "#5BC9F0")
    people = fdb.list_people(conn)
    assert [p["name"] for p in people] == ["Rem"]
    fdb.update_person(conn, pid, name="Remy", color="#8AE0AD", sort=2)
    p = fdb.list_people(conn)[0]
    assert (p["name"], p["color"], p["sort"]) == ("Remy", "#8AE0AD", 2)
    fdb.update_person(conn, pid, active=0)
    assert fdb.list_people(conn) == []            # default: active only
    assert len(fdb.list_people(conn, include_inactive=True)) == 1


def test_delete_person_is_history_safe_and_cleans_assignments(conn):
    """Hard delete removes the person + their completions and strips them from
    every chore's assignment, but leaves the frozen occurrence_log intact."""
    keep = fdb.add_person(conn, "Sam", "#5BC9F0")
    gone = fdb.add_person(conn, "Alex", "#8AE0AD")
    # a rotation chore including both, and a chore fixed to the doomed person
    rot = fdb.add_chore(conn, title="Dishes", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="rotation",
                        fixed_person_id=None, rotation_order=[keep, gone, keep],
                        rotation_epoch="2026-08-01")
    fixed = fdb.add_chore(conn, title="Trash", icon="", schedule_kind="daily",
                          days_mask=0, assign_kind="fixed", fixed_person_id=gone,
                          rotation_order=[], rotation_epoch="2026-08-01")
    fdb.set_completion(conn, rot, "2026-08-10", gone)
    fdb.replace_day_log(conn, "2026-08-10", [
        {"chore_id": rot, "person_id": gone, "title": "Dishes", "icon": "",
         "rot": 1}])

    assert fdb.delete_person(conn, gone) is True
    # the person and their completions are gone; the other person survives
    assert [p["id"] for p in fdb.list_people(conn, include_inactive=True)] == [keep]
    assert fdb.completions_between(conn, "2026-08-10", "2026-08-10") == []
    # rotation stripped of the deleted id (keep's slots preserved), fixed nulled
    chores = {c["id"]: c for c in fdb.list_chores(conn)}
    assert chores[rot]["rotation_order"] == [keep, keep]
    assert chores[fixed]["fixed_person_id"] is None
    # frozen history row is left intact (it just won't render without the person)
    assert fdb.day_log(conn, "2026-08-10")[0]["person_id"] == gone
    # deleting an unknown person is a no-op returning False
    assert fdb.delete_person(conn, 9999) is False


def test_chore_crud_and_shapes(conn):
    pid = fdb.add_person(conn, "Rem", "#5BC9F0")
    cid = fdb.add_chore(conn, title="Dishes", icon="🍽️", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch="2026-08-12")
    ch = fdb.list_chores(conn)[0]
    assert ch["title"] == "Dishes" and ch["rotation_order"] == []
    fdb.update_chore(conn, cid, schedule_kind="days", days_mask=0b0010101)
    assert fdb.list_chores(conn)[0]["days_mask"] == 0b0010101


def test_completion_toggle(conn):
    pid = fdb.add_person(conn, "Rem", "#5BC9F0")
    cid = fdb.add_chore(conn, title="Trash", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch="2026-08-12")
    fdb.set_completion(conn, cid, "2026-08-12", pid)
    fdb.set_completion(conn, cid, "2026-08-12", pid)   # idempotent upsert
    assert fdb.completions_between(conn, "2026-08-12", "2026-08-12") \
        == [{"chore_id": cid, "date": "2026-08-12", "person_id": pid}]
    fdb.clear_completion(conn, cid, "2026-08-12")
    assert fdb.completions_between(conn, "2026-08-12", "2026-08-12") == []


def test_events_replace_window(conn):
    fdb.replace_events(conn, [
        {"id": "e1", "calendar_id": "cal", "title": "Dentist",
         "start_ts": "2026-08-13T10:00:00-07:00", "end_ts": "2026-08-13T11:00:00-07:00",
         "all_day": 0}])
    assert fdb.list_events(conn)[0]["title"] == "Dentist"
    fdb.replace_events(conn, [])                      # full-window replace
    assert fdb.list_events(conn) == []


def test_events_carry_detail_fields(conn):
    fdb.replace_events(conn, [
        {"id": "e1", "calendar_id": "cal", "title": "Dentist",
         "start_ts": "2026-08-13T10:00:00-07:00", "end_ts": "2026-08-13T11:00:00-07:00",
         "all_day": 0, "location": "123 Main St", "description": "bring card",
         "color_id": "11"}])
    row = fdb.list_events(conn)[0]
    assert (row["location"], row["description"], row["color_id"]) == \
        ("123 Main St", "bring card", "11")
    # absent fields default cleanly (old-shape callers never crash)
    fdb.replace_events(conn, [{"id": "e2", "calendar_id": "cal", "title": "X",
                               "start_ts": "2026-08-14", "end_ts": "2026-08-15",
                               "all_day": 1}])
    row = fdb.list_events(conn)[0]
    assert (row["location"], row["description"], row["color_id"]) == ("", "", None)


def test_schema_migrates_pre_detail_events_table(tmp_path):
    """A DB created before the detail columns gains them on ensure_schema —
    the box's live hub.db upgrades in place, no data loss."""
    c = fdb.connect(str(tmp_path / "old.db"))
    c.execute("""CREATE TABLE events(
        id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL, title TEXT NOT NULL,
        start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, all_day INTEGER NOT NULL,
        updated TEXT)""")
    c.execute("INSERT INTO events VALUES('e1','cal','Old','2026-08-01','2026-08-01',1,NULL)")
    c.commit()
    fdb.ensure_schema(c)
    row = fdb.list_events(c)[0]
    assert row["title"] == "Old" and row["location"] == "" and row["color_id"] is None
    c.close()


def test_events_same_id_on_two_calendars_coexist(conn):
    """Regression for issue #30: Google reuses one event's id across every
    calendar it appears on, so 'Dentist' on both parents' calendars arrives as
    two rows with the SAME id. The composite PK (calendar_id, id) lets them
    coexist instead of aborting the whole sync on a duplicate-id INSERT."""
    fdb.replace_events(conn, [
        {"id": "shared", "calendar_id": "mom", "title": "Dentist",
         "start_ts": "2026-08-20T10:00:00-07:00",
         "end_ts": "2026-08-20T11:00:00-07:00", "all_day": 0},
        {"id": "shared", "calendar_id": "dad", "title": "Dentist",
         "start_ts": "2026-08-20T10:00:00-07:00",
         "end_ts": "2026-08-20T11:00:00-07:00", "all_day": 0}])
    rows = fdb.list_events(conn)
    assert len(rows) == 2 and {r["calendar_id"] for r in rows} == {"mom", "dad"}
    # a same-(calendar_id, id) duplicate within one batch replaces, never crashes
    fdb.replace_events(conn, [
        {"id": "dup", "calendar_id": "mom", "title": "A",
         "start_ts": "2026-08-21", "end_ts": "2026-08-22", "all_day": 1},
        {"id": "dup", "calendar_id": "mom", "title": "B",
         "start_ts": "2026-08-21", "end_ts": "2026-08-22", "all_day": 1}])
    dup = [r for r in fdb.list_events(conn) if r["id"] == "dup"]
    assert len(dup) == 1 and dup[0]["title"] == "B"


def test_schema_migrates_single_id_pk_events_to_composite(tmp_path):
    """A DB whose events table still has the bare `id` PRIMARY KEY is rebuilt to
    PRIMARY KEY(calendar_id, id) on ensure_schema, preserving rows, so the
    cross-calendar duplicate that used to abort sync now stores cleanly."""
    c = fdb.connect(str(tmp_path / "old.db"))
    c.execute("""CREATE TABLE events(
        id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL, title TEXT NOT NULL,
        start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, all_day INTEGER NOT NULL,
        updated TEXT, location TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', color_id TEXT)""")
    c.execute("INSERT INTO events(id, calendar_id, title, start_ts, end_ts, all_day) "
              "VALUES('e1','mom','Kept','2026-08-01','2026-08-01',1)")
    c.commit()
    fdb.ensure_schema(c)
    assert fdb.list_events(c)[0]["title"] == "Kept"            # row preserved
    fdb.replace_events(c, [
        {"id": "e1", "calendar_id": "mom", "title": "Kept",
         "start_ts": "2026-08-01", "end_ts": "2026-08-01", "all_day": 1},
        {"id": "e1", "calendar_id": "dad", "title": "Kept",
         "start_ts": "2026-08-01", "end_ts": "2026-08-01", "all_day": 1}])
    assert len(fdb.list_events(c)) == 2                        # composite PK active
    c.close()


def test_deleted_person_id_is_not_reused(conn):
    """Regression for issue #31: a deleted person's id must not be handed to the
    next person, or the newcomer inherits the deleted one's frozen history."""
    a = fdb.add_person(conn, "Ann", "#111111")
    assert fdb.delete_person(conn, a) is True
    b = fdb.add_person(conn, "Bea", "#222222")
    assert b > a


def test_deleted_chore_id_is_not_reused(conn):
    """Regression for issue #31: same guarantee for chores (occurrence_log keeps
    frozen rows keyed by chore id)."""
    mk = lambda t: fdb.add_chore(
        conn, title=t, icon="", schedule_kind="daily", days_mask=0,
        assign_kind="rotation", fixed_person_id=None, rotation_order=[],
        rotation_epoch="2026-08-01")
    a = mk("A")
    assert fdb.delete_chore(conn, a) is True
    assert mk("B") > a


def test_schema_adds_autoincrement_to_legacy_people(tmp_path):
    """A DB whose people table predates AUTOINCREMENT is rebuilt on ensure_schema
    with the id counter pinned to the max preserved id, so a later delete+add
    gets max+1 and never reuses the freed id (issue #31)."""
    c = fdb.connect(str(tmp_path / "old.db"))
    c.execute("""CREATE TABLE people(
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
        sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)""")
    c.execute("INSERT INTO people(id, name, color) VALUES(5, 'Old', '#abcabc')")
    c.commit()
    fdb.ensure_schema(c)
    assert fdb.list_people(c, include_inactive=True)[0]["id"] == 5   # row preserved
    fdb.delete_person(c, 5)
    assert fdb.add_person(c, "New", "#defdef") == 6                  # max+1, not reused
    c.close()


def test_kv(conn):
    assert fdb.kv_get(conn, "calendar_status") is None
    fdb.kv_set(conn, "calendar_status", {"ok": True})
    assert fdb.kv_get(conn, "calendar_status") == {"ok": True}


# --- todos ----------------------------------------------------------------

def test_todos_crud_roundtrip(conn):
    tid = fdb.add_todo(conn, "Fix gate latch", "now")
    rows = fdb.list_todos(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == tid
    assert row["title"] == "Fix gate latch"
    assert row["bucket"] == "now"
    assert row["created_at"]  # ISO string, set by add_todo
    assert row["done_at"] is None and row["done_date"] is None

    fdb.update_todo(conn, tid, title="Fix side gate latch", bucket="soon")
    row = fdb.list_todos(conn)[0]
    assert row["title"] == "Fix side gate latch"
    assert row["bucket"] == "soon"

    # allowlist: unknown fields are ignored, not written
    fdb.update_todo(conn, tid, nonsense=1)
    assert fdb.list_todos(conn)[0]["title"] == "Fix side gate latch"

    fdb.set_todo_done(conn, tid, "2026-08-14")
    row = fdb.list_todos(conn)[0]
    assert row["done_date"] == "2026-08-14"
    assert row["done_at"] is not None

    fdb.clear_todo_done(conn, tid)
    row = fdb.list_todos(conn)[0]
    assert row["done_at"] is None and row["done_date"] is None

    fdb.delete_todo(conn, tid)
    assert fdb.list_todos(conn) == []


def test_todos_list_order_is_created_then_id(conn):
    a = fdb.add_todo(conn, "first", "now")
    b = fdb.add_todo(conn, "second", "later")
    # rows with distinct created_at sort by created_at, not insertion order
    conn.execute("UPDATE todos SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (b,))
    conn.commit()
    assert [r["id"] for r in fdb.list_todos(conn)] == [b, a]


def test_todos_list_order_ties_fall_back_to_id(conn):
    a = fdb.add_todo(conn, "first", "now")
    b = fdb.add_todo(conn, "second", "later")
    # equal created_at: order must fall back to id, not insertion order
    conn.execute(
        "UPDATE todos SET created_at = '2026-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
        (a, b))
    conn.commit()
    assert [r["id"] for r in fdb.list_todos(conn)] == sorted([a, b])


def test_todos_table_created_on_pre_todos_db(conn):
    # ensure_schema must add the table to a DB created before this feature
    conn.execute("DROP TABLE todos")
    conn.commit()
    fdb.ensure_schema(conn)
    tid = fdb.add_todo(conn, "still works", "later")
    assert fdb.list_todos(conn)[0]["id"] == tid


def test_todos_bucket_check_constraint(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO todos(title, bucket, created_at) VALUES('x', 'someday', 'now')")


def test_todos_bucket_check_constraint_via_public_api(conn):
    # The CHECK constraint is defense-in-depth against a bad bucket string that
    # slips past application-level validation — verify it surfaces through the
    # public add_todo function itself, not just via raw SQL against the table.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        fdb.add_todo(conn, "x", "someday")


# --- occurrence log (frozen chore history) --------------------------------

def _row(cid, pid, title="Dishes", icon="", rot=0):
    return {"chore_id": cid, "person_id": pid, "title": title, "icon": icon,
            "rot": rot}


def test_occurrence_log_replace_and_read(conn):
    fdb.replace_day_log(conn, "2026-08-14", [_row(1, 7), _row(2, 8, "Trash")])
    assert fdb.day_log(conn, "2026-08-14") == [
        {"date": "2026-08-14", **_row(1, 7)},
        {"date": "2026-08-14", **_row(2, 8, "Trash")},
    ]
    # replace fully supersedes the day's rows
    fdb.replace_day_log(conn, "2026-08-14", [_row(3, 7, "Cat")])
    assert [r["chore_id"] for r in fdb.day_log(conn, "2026-08-14")] == [3]
    # an empty replace clears the day
    fdb.replace_day_log(conn, "2026-08-14", [])
    assert fdb.day_log(conn, "2026-08-14") == []


def test_occurrence_log_between(conn):
    fdb.replace_day_log(conn, "2026-08-12", [_row(1, 7)])
    fdb.replace_day_log(conn, "2026-08-13", [_row(1, 8)])
    fdb.replace_day_log(conn, "2026-08-15", [_row(1, 7)])
    rows = fdb.logs_between(conn, "2026-08-12", "2026-08-14")
    assert [(r["date"], r["person_id"]) for r in rows] == \
        [("2026-08-12", 7), ("2026-08-13", 8)]


def test_log_row_lookup(conn):
    fdb.replace_day_log(conn, "2026-08-14", [_row(1, 7)])
    assert fdb.log_row(conn, 1, "2026-08-14")["person_id"] == 7
    assert fdb.log_row(conn, 1, "2026-08-13") is None
    assert fdb.log_row(conn, 2, "2026-08-14") is None


def test_delete_chore_keeps_history(conn):
    """Frozen history: deleting a chore removes the definition but leaves its
    completion rows and occurrence-log rows for past days intact."""
    pid = fdb.add_person(conn, "Rem", "#5BC9F0")
    cid = fdb.add_chore(conn, title="Trash", icon="", schedule_kind="daily",
                        days_mask=0, assign_kind="fixed", fixed_person_id=pid,
                        rotation_order=[], rotation_epoch="2026-08-12")
    fdb.set_completion(conn, cid, "2026-08-12", pid)
    fdb.replace_day_log(conn, "2026-08-12", [_row(cid, pid, "Trash")])
    assert fdb.delete_chore(conn, cid) is True
    assert fdb.list_chores(conn, include_inactive=True) == []
    assert fdb.completions_between(conn, "2026-08-12", "2026-08-12") == \
        [{"chore_id": cid, "date": "2026-08-12", "person_id": pid}]
    assert [r["chore_id"] for r in fdb.day_log(conn, "2026-08-12")] == [cid]
    assert fdb.delete_chore(conn, cid) is False


def test_completions_migration_is_atomic_and_recovers(tmp_path):
    """A crash mid-rebuild must leave the ORIGINAL completions intact (rows +
    data) and no stranded completions_new — then the next boot completes the
    migration cleanly, never a boot-loop 'table already exists'."""
    c = _legacy_fk_db(str(tmp_path / "old.db"))
    c.crash_on = "RENAME TO completions"       # die between DROP and RENAME
    with pytest.raises(sqlite3.OperationalError):
        fdb.ensure_schema(c)
    # the completion row survived the aborted migration, unmigrated but present
    assert fdb.completions_between(c, "2026-08-12", "2026-08-12") == \
        [{"chore_id": 1, "date": "2026-08-12", "person_id": 7}]
    assert not list(c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='completions_new'")), \
        "the half-built table must be rolled back, not stranded"
    # next boot finishes the job with no wedge
    c.crash_on = None
    fdb.ensure_schema(c)
    assert fdb.completions_between(c, "2026-08-12", "2026-08-12") != []
    assert "chores" not in {r["table"]
                            for r in c.execute("PRAGMA foreign_key_list(completions)")}
    assert fdb.delete_chore(c, 1) is True      # FK really gone
    c.close()


def test_completions_migration_survives_stranded_new_table(tmp_path):
    """Defense in depth: even if a prior (pre-atomic) run left a stranded
    completions_new behind, the rebuild drops it first instead of erroring on
    a duplicate CREATE."""
    c = _legacy_fk_db(str(tmp_path / "old.db"))
    c.execute("CREATE TABLE completions_new(x)")   # leftover junk from a crash
    c.commit()
    fdb.ensure_schema(c)
    assert fdb.completions_between(c, "2026-08-12", "2026-08-12") != []
    assert "chores" not in {r["table"]
                            for r in c.execute("PRAGMA foreign_key_list(completions)")}
    c.close()


def test_schema_widens_chore_schedule_check_for_once(tmp_path):
    """A DB created before one-time chores has a schedule_kind CHECK that only
    allows daily/days; ensure_schema rebuilds it to also allow 'once', keeping
    every existing chore row, so the box's live hub.db can store one-time
    chores after the upgrade."""
    c = fdb.connect(str(tmp_path / "old.db"))
    c.executescript("""
        CREATE TABLE people(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE chores(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          icon TEXT NOT NULL DEFAULT '',
          schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days')),
          days_mask INTEGER NOT NULL DEFAULT 0,
          assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
          fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
          rotation_epoch TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
    """)
    c.execute("INSERT INTO people(id, name, color) VALUES(7, 'Rem', '#5BC9F0')")
    c.execute("""INSERT INTO chores(id, title, schedule_kind, assign_kind,
                 fixed_person_id, rotation_epoch)
                 VALUES(1, 'Trash', 'daily', 'fixed', 7, '2026-08-01')""")
    c.commit()
    # a 'once' insert would fail the old CHECK
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("""INSERT INTO chores(id, title, schedule_kind, assign_kind,
                     fixed_person_id, rotation_epoch)
                     VALUES(2, 'Books', 'once', 'fixed', 7, '2026-08-20')""")

    fdb.ensure_schema(c)

    # the pre-existing chore survived the rebuild
    rows = fdb.list_chores(c, include_inactive=True)
    assert [r["id"] for r in rows] == [1]
    assert rows[0]["title"] == "Trash"
    # and a one-time chore now inserts cleanly through the widened CHECK
    cid = fdb.add_chore(c, title="Books", icon="", schedule_kind="once",
                        days_mask=0, assign_kind="fixed", fixed_person_id=7,
                        rotation_order=[], rotation_epoch="2026-08-20")
    assert cid == 2
    # idempotent: a second ensure_schema leaves the widened table alone
    fdb.ensure_schema(c)
    assert len(fdb.list_chores(c, include_inactive=True)) == 2
    c.close()


def test_chore_check_migration_is_atomic_and_recovers(tmp_path):
    """A crash mid-rebuild of the chores table must leave the ORIGINAL chores
    intact and no stranded chores_new — then the next boot completes the
    migration cleanly, never a boot-loop 'table already exists'. Mirrors
    test_completions_migration_is_atomic_and_recovers for the 'once' CHECK
    widen. (_legacy_fk_db carries the pre-'once' chores CHECK and a _CrashConn;
    the completions FK migration runs first and completes — its rename is
    'RENAME TO completions', which the chores crash hook below does not match.)"""
    c = _legacy_fk_db(str(tmp_path / "old.db"))
    c.crash_on = "RENAME TO chores"            # die between DROP and RENAME
    with pytest.raises(sqlite3.OperationalError):
        fdb.ensure_schema(c)
    # the chore row survived the aborted rebuild, and the half-built table is
    # rolled back rather than stranded
    rows = fdb.list_chores(c, include_inactive=True)
    assert [r["id"] for r in rows] == [1] and rows[0]["title"] == "Trash"
    assert not list(c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='chores_new'")), \
        "the half-built table must be rolled back, not stranded"
    # next boot finishes the job: the CHECK is widened and a 'once' chore inserts
    c.crash_on = None
    fdb.ensure_schema(c)
    cid = fdb.add_chore(c, title="Books", icon="", schedule_kind="once",
                        days_mask=0, assign_kind="fixed", fixed_person_id=7,
                        rotation_order=[], rotation_epoch="2026-08-20")
    assert cid == 2
    c.close()


def test_chore_check_migration_survives_stranded_new_table(tmp_path):
    """Defense in depth: even if a prior (crashed) run left a stranded
    chores_new behind, the rebuild drops it first instead of erroring on a
    duplicate CREATE."""
    c = _legacy_fk_db(str(tmp_path / "old.db"))
    c.execute("CREATE TABLE chores_new(x)")    # leftover junk from a crash
    c.commit()
    fdb.ensure_schema(c)
    rows = fdb.list_chores(c, include_inactive=True)
    assert [r["id"] for r in rows] == [1] and rows[0]["title"] == "Trash"
    # the widened CHECK is really in place now
    cid = fdb.add_chore(c, title="Books", icon="", schedule_kind="once",
                        days_mask=0, assign_kind="fixed", fixed_person_id=7,
                        rotation_order=[], rotation_epoch="2026-08-20")
    assert cid == 2
    c.close()


def test_schema_migrates_completions_chore_fk_away(tmp_path):
    """A DB created when completions had a FOREIGN KEY to chores(id) is rebuilt
    without it, keeping its rows — so chore deletion can preserve history on
    the box's live hub.db too."""
    c = fdb.connect(str(tmp_path / "old.db"))
    c.executescript("""
        CREATE TABLE people(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE chores(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          icon TEXT NOT NULL DEFAULT '',
          schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','days')),
          days_mask INTEGER NOT NULL DEFAULT 0,
          assign_kind TEXT NOT NULL CHECK(assign_kind IN ('fixed','rotation')),
          fixed_person_id INTEGER, rotation_order TEXT NOT NULL DEFAULT '[]',
          rotation_epoch TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE completions(
          chore_id INTEGER NOT NULL REFERENCES chores(id),
          date TEXT NOT NULL,
          person_id INTEGER NOT NULL REFERENCES people(id),
          done_at TEXT NOT NULL, PRIMARY KEY(chore_id, date));
    """)
    c.execute("INSERT INTO people(id, name, color) VALUES(7, 'Rem', '#5BC9F0')")
    c.execute("""INSERT INTO chores(id, title, schedule_kind, assign_kind,
                 fixed_person_id, rotation_epoch)
                 VALUES(1, 'Trash', 'daily', 'fixed', 7, '2026-08-01')""")
    c.execute("""INSERT INTO completions VALUES(1, '2026-08-12', 7,
                 '2026-08-12T20:00:00+00:00')""")
    c.commit()
    fdb.ensure_schema(c)
    # rows survived the rebuild and the chore FK is gone
    assert fdb.completions_between(c, "2026-08-12", "2026-08-12") == \
        [{"chore_id": 1, "date": "2026-08-12", "person_id": 7}]
    fks = {r["table"] for r in c.execute("PRAGMA foreign_key_list(completions)")}
    assert "chores" not in fks
    assert fdb.delete_chore(c, 1) is True   # would raise under the old FK
    # idempotent: a second ensure_schema leaves the rebuilt table alone
    fdb.ensure_schema(c)
    assert fdb.completions_between(c, "2026-08-12", "2026-08-12") != []
    c.close()
