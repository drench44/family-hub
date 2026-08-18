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
    assert res["created"] == 9                       # today-1 .. today+7
    pend = [o for o in fdb.caldav_pending(conn) if o["comp_type"] == "VTODO"]
    assert len(pend) == 9
    assert all(o["collection_id"] == "caldav:emma" for o in pend)
    assert all("🍽 Dishes" in o["raw_ics"] for o in pend)
    assert len(fdb.list_chore_mirror(conn)) == 9


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
    assert res["deleted"] == 9 and fdb.list_chore_mirror(conn) == []
