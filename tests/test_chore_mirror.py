import datetime as dt
from types import SimpleNamespace

from family_hub import chore_mirror
from family_hub import db as fdb

_CFG = SimpleNamespace(chore_mirror_horizon_days=7)
_NOW = dt.datetime(2026, 8, 17, 12, 0, 0)   # naive -> all-day fallback


def _person(conn, name, list_id=None):
    pid = fdb.add_person(conn, name, "#5BC9F0")
    if list_id:
        fdb.upsert_caldav_collection(conn, list_id, "VTODO", name, None, "t")
        fdb.update_person(conn, pid, reminder_list_id=list_id)
    return pid


def _daily(conn, title, pid, **kw):
    return fdb.add_chore(conn, title=title, icon=kw.get("icon", ""),
                         schedule_kind="daily", days_mask=0, assign_kind="fixed",
                         fixed_person_id=pid, rotation_order=[],
                         rotation_epoch="2026-08-01")


def test_reconcile_creates_for_mapped_person(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    _daily(conn, "Dishes", pid, icon="🍽")
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["created"] == 8                       # today-1 .. today+7
    pend = [o for o in fdb.caldav_pending(conn) if o["comp_type"] == "VTODO"]
    assert len(pend) == 8
    assert all(o["collection_id"] == "caldav:emma" for o in pend)
    assert all("🍽 Dishes" in o["raw_ics"] for o in pend)
    assert len(fdb.list_chore_mirror(conn)) == 8


def test_reconcile_skips_unmapped_person(conn):
    pid = _person(conn, "Jack")                      # no list -> not mirrored
    _daily(conn, "Trash", pid)
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["created"] == 0 and fdb.list_chore_mirror(conn) == []


def test_reconcile_is_idempotent(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    n = len(fdb.list_chore_mirror(conn))
    res2 = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res2["created"] == 0 and len(fdb.list_chore_mirror(conn)) == n


def test_reconcile_rotation_handoff_targets_each_persons_list(conn):
    a = _person(conn, "A", "caldav:a")
    b = _person(conn, "B", "caldav:b")
    fdb.add_chore(conn, title="Trash", icon="", schedule_kind="daily", days_mask=0,
                  assign_kind="rotation", fixed_person_id=None, rotation_order=[a, b],
                  rotation_epoch=_NOW.date().isoformat())   # epoch = today
    chore_mirror.reconcile(conn, _CFG, _NOW)
    ledger = {m["date"]: m["person_id"] for m in fdb.list_chore_mirror(conn)}
    today = _NOW.date().isoformat()
    tomorrow = (_NOW.date() + dt.timedelta(days=1)).isoformat()
    assert ledger[today] == a and ledger[tomorrow] == b


def test_reconcile_prunes_deactivated_chore(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    assert fdb.list_chore_mirror(conn)
    fdb.update_chore(conn, cid, active=0)
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["deleted"] == 8 and fdb.list_chore_mirror(conn) == []


# --- P4: two-way completion -----------------------------------------------

def _mirrored(conn):
    """A mapped person + daily chore + one reconcile; returns (pid, cid)."""
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    return pid, cid


def test_push_completion_marks_and_reopens_mirrored_reminder(conn):
    pid, cid = _mirrored(conn)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    assert chore_mirror.push_completion(conn, cid, today, True) is True
    obj = fdb.get_cal_object(conn, m["cal_object_id"])
    assert "STATUS:COMPLETED" in obj["raw_ics"]
    chore_mirror.push_completion(conn, cid, today, False)
    obj = fdb.get_cal_object(conn, m["cal_object_id"])
    assert "STATUS:NEEDS-ACTION" in obj["raw_ics"]


def test_push_completion_noop_when_not_mirrored(conn):
    pid = _person(conn, "Jack")                 # unmapped -> no mirror row
    cid = _daily(conn, "Trash", pid)
    assert chore_mirror.push_completion(conn, cid, _NOW.date().isoformat(), True) is False


def test_reconcile_completions_records_ios_checkoff_add_only(conn):
    from family_hub import reminders as rem
    pid, cid = _mirrored(conn)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    oid = m["cal_object_id"]
    coll = oid.split("/", 1)[0]
    utcnow = _NOW.replace(tzinfo=dt.timezone.utc)
    # simulate: reminder pushed, then completed in iOS, stored SYNCED by the pull
    fdb.mark_cal_object_pushed(conn, oid, "https://x/" + m["uid"] + ".ics", "e1")
    done = rem.set_completed(fdb.get_cal_object(conn, oid)["raw_ics"], True, utcnow)
    fdb.upsert_cal_object_synced(conn, {
        "id": oid, "collection_id": coll, "comp_type": "VTODO", "uid": m["uid"],
        "href": "https://x", "etag": "e2", "summary": "Dishes", "raw_ics": done,
        "sequence": 1, "last_modified": None}, force=True)
    assert not fdb.completion_exists(conn, cid, today)
    assert chore_mirror.reconcile_completions(conn, _NOW) == 1
    assert fdb.completion_exists(conn, cid, today)
    assert chore_mirror.reconcile_completions(conn, _NOW) == 0   # idempotent
    # add-only: reopening in iOS must NOT remove the wall completion
    reopened = rem.set_completed(done, False, utcnow)
    fdb.upsert_cal_object_synced(conn, {
        "id": oid, "collection_id": coll, "comp_type": "VTODO", "uid": m["uid"],
        "href": "https://x", "etag": "e3", "summary": "Dishes", "raw_ics": reopened,
        "sequence": 2, "last_modified": None}, force=True)
    chore_mirror.reconcile_completions(conn, _NOW)
    assert fdb.completion_exists(conn, cid, today)               # still done on wall


def test_reconcile_refreshes_mirrored_reminder_on_edit(conn):
    pid, cid = _mirrored(conn)
    m = fdb.get_chore_mirror(conn, cid, _NOW.date().isoformat())
    fdb.update_chore(conn, cid, title="Wash dishes")     # content edit
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["updated"] >= 1
    assert "Wash dishes" in fdb.get_cal_object(conn, m["cal_object_id"])["raw_ics"]
    assert chore_mirror.reconcile(conn, _CFG, _NOW)["updated"] == 0   # idempotent


# --- review fixes: handoff / completed guards / orphan / prune-skip --------

def _complete_in_ios(conn, m, title="Dishes"):
    """Simulate the person completing the mirrored reminder in iOS: the object is
    pushed then stored SYNCED with STATUS:COMPLETED (as the next pull would)."""
    from family_hub import reminders as rem
    oid = m["cal_object_id"]
    coll = oid.split("/", 1)[0]
    fdb.mark_cal_object_pushed(conn, oid, "https://x/" + m["uid"] + ".ics", "e1")
    done = rem.set_completed(fdb.get_cal_object(conn, oid)["raw_ics"], True,
                             _NOW.replace(tzinfo=dt.timezone.utc))
    fdb.upsert_cal_object_synced(conn, {
        "id": oid, "collection_id": coll, "comp_type": "VTODO", "uid": m["uid"],
        "href": "https://x", "etag": "e2", "summary": title, "raw_ics": done,
        "sequence": 1, "last_modified": None}, force=True)


def _rotation(conn, a, b, epoch):
    return fdb.add_chore(conn, title="Trash", icon="", schedule_kind="daily",
                         days_mask=0, assign_kind="rotation", fixed_person_id=None,
                         rotation_order=[a, b], rotation_epoch=epoch)


def test_reconcile_handoff_moves_to_new_persons_list(conn):
    a = _person(conn, "A", "caldav:a")
    b = _person(conn, "B", "caldav:b")
    cid = _rotation(conn, a, b, _NOW.date().isoformat())
    chore_mirror.reconcile(conn, _CFG, _NOW)
    today = _NOW.date().isoformat()
    m_a = fdb.get_chore_mirror(conn, cid, today)
    assert m_a["person_id"] == a and m_a["cal_object_id"].startswith("caldav:a/")
    fdb.update_chore(conn, cid, rotation_order=[b, a])       # today (occ0) now -> B
    res = chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:a", "caldav:b"})
    assert res["moved"] >= 1
    m_b = fdb.get_chore_mirror(conn, cid, today)
    assert m_b["person_id"] == b and m_b["cal_object_id"].startswith("caldav:b/")
    assert fdb.get_cal_object(conn, m_a["cal_object_id"]) is None   # old dropped


def test_reconcile_handoff_keeps_completed_old_reminder(conn):
    a = _person(conn, "A", "caldav:a")
    b = _person(conn, "B", "caldav:b")
    cid = _rotation(conn, a, b, _NOW.date().isoformat())
    chore_mirror.reconcile(conn, _CFG, _NOW)
    today = _NOW.date().isoformat()
    m_a = fdb.get_chore_mirror(conn, cid, today)
    _complete_in_ios(conn, m_a, title="Trash")             # A finished it
    fdb.update_chore(conn, cid, rotation_order=[b, a])       # reassign to B
    chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:a", "caldav:b"})
    old = fdb.get_cal_object(conn, m_a["cal_object_id"])
    assert old is not None and old["sync_state"] == "SYNCED"  # kept as A's history


def test_reconcile_prune_keeps_completed_reminder(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    _complete_in_ios(conn, m)
    fdb.update_chore(conn, cid, active=0)                    # all fall out of desired
    chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:emma"})
    assert fdb.get_cal_object(conn, m["cal_object_id"]) is not None  # history kept
    assert fdb.get_chore_mirror(conn, cid, today) is None           # ledger dropped


def test_reconcile_prune_skips_unsynced_list(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    fdb.update_chore(conn, cid, active=0)
    # the emma list didn't sync this tick -> prune must NOT touch it
    res = chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections=set())
    assert res["deleted"] == 0 and fdb.list_chore_mirror(conn)


def test_reconcile_drift_skips_completed_occurrence(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    _complete_in_ios(conn, m)
    fdb.update_chore(conn, cid, title="Wash dishes")        # content edit
    chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:emma"})
    obj = fdb.get_cal_object(conn, m["cal_object_id"])
    assert "Dishes" in obj["raw_ics"] and "Wash dishes" not in obj["raw_ics"]
    assert "STATUS:COMPLETED" in obj["raw_ics"]             # left as history


def test_reconcile_recreates_orphaned_ledger_row(conn):
    pid = _person(conn, "Emma", "caldav:emma")
    cid = _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    fdb.delete_cal_object_row(conn, m["cal_object_id"])     # object vanished (iOS delete)
    res = chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:emma"})
    assert res["created"] >= 1
    assert fdb.get_cal_object(conn, m["cal_object_id"]) is not None   # re-mirrored


def test_push_completion_on_synced_object_bumps_to_pending_update(conn):
    pid, cid = _mirrored(conn)
    today = _NOW.date().isoformat()
    m = fdb.get_chore_mirror(conn, cid, today)
    fdb.mark_cal_object_pushed(conn, m["cal_object_id"], "https://x", "e1")
    assert chore_mirror.push_completion(conn, cid, today, True) is True
    obj = fdb.get_cal_object(conn, m["cal_object_id"])
    assert obj["sync_state"] == "PENDING_UPDATE" and "STATUS:COMPLETED" in obj["raw_ics"]
    assert obj["base_etag"] == "e1"                          # preserved for If-Match


def test_reconcile_prunes_orphans_after_mapped_person_deleted(conn):
    """Deleting the last mapped person must still prune their mirrored reminders
    (reconcile used to early-return on 'no mapped people' and orphan them)."""
    pid = _person(conn, "Emma", "caldav:emma")
    _daily(conn, "Dishes", pid)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    assert len(fdb.list_chore_mirror(conn)) == 8
    fdb.delete_person(conn, pid)                    # nothing mapped now; rows orphaned
    res = chore_mirror.reconcile(conn, _CFG, _NOW, synced_collections={"caldav:emma"})
    assert res["deleted"] == 8 and fdb.list_chore_mirror(conn) == []


# --- away/pause overlay: the mirror mirrors what the WALL shows ---------------

def test_reconcile_away_person_paused_chore_not_mirrored(conn):
    """An away person's fixed chore with NO backup pauses on the wall, so the
    mirror must not push it to their iCloud list as due either -- otherwise a
    kid at camp keeps getting daily reminder notifications for chores the wall
    isn't asking anyone to do."""
    pid = _person(conn, "Milo", "caldav:milo")
    _daily(conn, "Fish", pid)
    fdb.add_away_period(conn, pid, _NOW.date().isoformat())   # open-ended, today
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["created"] == 0
    assert fdb.list_chore_mirror(conn) == []


def test_reconcile_covering_chore_lands_on_backups_list_and_credits_backup(conn):
    """Milo away with Ava as backup: the wall shows the fixed chore on Ava's
    card, so the mirror puts the reminder on AVA's iCloud list with the ledger
    row under Ava -- and an iOS check-off then credits Ava's streak (the same
    C1 crediting rule the wall's /complete endpoint enforces)."""
    milo = _person(conn, "Milo", "caldav:milo")
    ava = _person(conn, "Ava", "caldav:ava")
    cid = _daily(conn, "Fish", milo)
    fdb.add_away_period(conn, milo, _NOW.date().isoformat(),
                        backup_person_id=ava)
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["created"] == 8                    # today .. today+7, all covered
    pend = [o for o in fdb.caldav_pending(conn) if o["comp_type"] == "VTODO"]
    assert pend and all(o["collection_id"] == "caldav:ava" for o in pend)
    rows = fdb.list_chore_mirror(conn)
    assert rows and all(m["person_id"] == ava for m in rows)

    # iOS check-off of today's covering reminder credits the BACKUP
    today_iso = _NOW.date().isoformat()
    m = next(r for r in rows if r["date"] == today_iso)
    _complete_in_ios(conn, m, title="Fish")
    assert chore_mirror.reconcile_completions(
        conn, _NOW.replace(tzinfo=dt.timezone.utc)) == 1
    comp = conn.execute("SELECT person_id FROM completions WHERE chore_id=? AND date=?",
                        (cid, today_iso)).fetchone()
    assert comp["person_id"] == ava


def test_reconcile_return_moves_reminder_back_to_owner(conn):
    """Closing the away period hands the future occurrences back: the next
    reconcile's 'moved' branch relocates uncompleted covering reminders from the
    backup's list back to the owner's list, ledger rows re-keyed to the owner."""
    milo = _person(conn, "Milo", "caldav:milo")
    ava = _person(conn, "Ava", "caldav:ava")
    _daily(conn, "Fish", milo)
    period = fdb.add_away_period(conn, milo, _NOW.date().isoformat(),
                                 backup_person_id=ava)
    chore_mirror.reconcile(conn, _CFG, _NOW)
    assert all(m["person_id"] == ava for m in fdb.list_chore_mirror(conn))

    # back home: close the period as of yesterday; the whole horizon is his again
    fdb.close_away_period(conn, period,
                          (_NOW.date() - dt.timedelta(days=1)).isoformat())
    res = chore_mirror.reconcile(conn, _CFG, _NOW)
    assert res["moved"] == 8
    rows = fdb.list_chore_mirror(conn)
    assert rows and all(m["person_id"] == milo for m in rows)
