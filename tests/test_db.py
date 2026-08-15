import pytest

from family_hub import db as fdb


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
