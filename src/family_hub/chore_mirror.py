"""Chore mirror (P3): project the wall's chore plan into each mapped person's
iCloud reminder list, so chores show up natively on every iPhone — the Reminders
app, Siri, and (via due-times) notifications. The wall stays the source of truth;
this only pushes a rolling window of it.

Runs inside the CalDAV sync tick, gated on two-way (readonly=False). It queues
creates/deletes into the existing cal_objects outbox (flush_pending pushes them
with If-Match/server-wins) and records each occurrence in the chore_mirror ledger,
so the next pass reconciles rotation hand-offs and prunes days that fell out of
the window. Never raises — a mirror hiccup must not disrupt the calendar sync.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import chores as chlogic
from . import db as fdb
from . import reminders as remlogic

log = logging.getLogger("family_hub.caldav")

DEFAULT_HORIZON_DAYS = 7
# Keep one past day in the window so a chore completed late (yesterday) is not
# pruned before the completion sync records it (P4). Completed reminders are kept
# regardless; this only affects un-completed ones.
_PAST_DAYS = 1


def _title(chore: dict) -> str:
    icon = (chore.get("icon") or "").strip()
    return f"{icon} {chore['title']}".strip() if icon else chore["title"]


def _queue_create(conn, chore, diso, person_id, list_id, tz, now, now_iso) -> None:
    d = dt.date.fromisoformat(diso)
    uid = f"familyhub-chore-{chore['id']}-{diso}"
    oid = f"{list_id}/{uid}"
    title = _title(chore)
    ics = remlogic.build_chore_vtodo(uid, title, d,
                                     chore.get("due_times") or [], now, tz=tz)
    fdb.queue_cal_object_create(conn, {
        "id": oid, "collection_id": list_id, "comp_type": "VTODO",
        "uid": uid, "summary": title, "raw_ics": ics}, now_iso)
    fdb.upsert_chore_mirror(conn, chore["id"], diso, person_id, oid, uid)


def reconcile(conn, cfg, now: dt.datetime) -> dict:
    """Bring each mapped person's iCloud list in line with the wall's chore plan
    over [today-1, today+H]. Returns {created, moved, deleted}. Never raises."""
    try:
        mapped = {p["id"]: p["reminder_list_id"]
                  for p in fdb.list_people(conn) if p.get("reminder_list_id")}
        if not mapped:
            return {"created": 0, "moved": 0, "deleted": 0}
        tz = now.tzinfo             # wall zone (None in tests -> all-day fallback)
        now_iso = now.isoformat()
        today = now.date()
        H = getattr(cfg, "chore_mirror_horizon_days", DEFAULT_HORIZON_DAYS)
        all_people = fdb.list_people(conn)       # ALL active -> correct rotation
        chores = {c["id"]: c for c in fdb.list_chores(conn)}
        chores_list = list(chores.values())

        desired: dict = {}
        for i in range(-_PAST_DAYS, H + 1):
            d = today + dt.timedelta(days=i)
            for row in chlogic.plan_rows(chores_list, all_people, d):
                lid = mapped.get(row["person_id"])
                if lid:
                    desired[(row["chore_id"], d.isoformat())] = (row["person_id"], lid)

        existing = {(m["chore_id"], m["date"]): m
                    for m in fdb.list_chore_mirror(conn)}
        created = moved = deleted = 0

        for (cid, diso), (pid, lid) in desired.items():
            chore = chores.get(cid)
            if chore is None:
                continue
            cur = existing.get((cid, diso))
            if cur is None:
                _queue_create(conn, chore, diso, pid, lid, tz, now, now_iso)
                created += 1
            elif cur["person_id"] != pid:     # rotation handed off to another person
                fdb.queue_cal_object_delete(conn, cur["cal_object_id"], now_iso)
                fdb.delete_chore_mirror(conn, cid, diso)
                _queue_create(conn, chore, diso, pid, lid, tz, now, now_iso)
                moved += 1

        # prune occurrences that fell out of the window (past, or the chore was
        # deleted/deactivated). Never delete a COMPLETED reminder — that's history
        # in iOS; just forget the ledger row so it isn't reconciled again.
        for (cid, diso), m in existing.items():
            if (cid, diso) in desired:
                continue
            obj = fdb.get_cal_object(conn, m["cal_object_id"])
            if not (obj and "STATUS:COMPLETED" in (obj.get("raw_ics") or "")):
                fdb.queue_cal_object_delete(conn, m["cal_object_id"], now_iso)
            fdb.delete_chore_mirror(conn, cid, diso)
            deleted += 1

        return {"created": created, "moved": moved, "deleted": deleted}
    except Exception:
        log.exception("chore mirror reconcile failed (non-fatal)")
        return {"created": 0, "moved": 0, "deleted": 0, "error": True}
