"""iCloud Reminders (VTODO): parse + group, kept separate from the local To-Dos.

`parse_vtodo` lazily imports icalendar (so this module imports without it);
`group`/`open_count` are pure stdlib. A reminder dict is
{id, title, due (ISO str or None), completed (bool), priority (int|None),
list_id, list_name}. This slice is read-only; completing a reminder back to
iCloud (two-way) is a later slice.
"""
from __future__ import annotations

import datetime as dt

BUCKETS = ("overdue", "today", "upcoming", "no_date")


def parse_vtodo(ics_data, list_id: str, list_name: str = "") -> list[dict]:
    """Flatten every VTODO in one ICS document to reminder dicts."""
    import icalendar
    cal = icalendar.Calendar.from_ical(ics_data)
    out = []
    for comp in cal.walk("VTODO"):
        uid = str(comp.get("UID") or "")
        status = str(comp.get("STATUS") or "")
        completed = status == "COMPLETED" or comp.get("COMPLETED") is not None
        due = comp.decoded("DUE", None)
        if due is None:
            due = comp.decoded("DTSTART", None)
        due_iso = due.isoformat() if due is not None else None
        pr = comp.get("PRIORITY")
        try:
            priority = int(pr) if pr is not None else None
        except Exception:
            priority = None
        out.append({
            "id": f"{list_id}/{uid}",
            "title": str(comp.get("SUMMARY") or "") or "(untitled)",
            "due": due_iso, "completed": completed, "priority": priority,
            "list_id": list_id, "list_name": list_name,
        })
    return out


def group(reminders: list[dict], today: dt.date) -> dict:
    """Incomplete reminders bucketed overdue / today / upcoming / no-date, each
    sorted by due date then title. Completed ones are dropped.

    NOTE: all-day (VALUE=DATE) DUEs — the common iCloud case — bucket exactly.
    A timed DUE carrying an offset/Z is bucketed by the date prefix of its own
    encoding, which can differ from the local day near midnight; revisit with a
    timezone-normalizing compare if timed reminders ever misbucket."""
    out: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    ti = today.isoformat()
    for r in reminders:
        if r.get("completed"):
            continue
        due = r.get("due")
        if not due:
            out["no_date"].append(r)
        elif due[:10] < ti:
            out["overdue"].append(r)
        elif due[:10] == ti:
            out["today"].append(r)
        else:
            out["upcoming"].append(r)
    for b in BUCKETS:
        out[b].sort(key=lambda r: (r.get("due") or "9999-12-31",
                                   r.get("title", "")))
    return out


def open_count(reminders: list[dict]) -> int:
    return sum(1 for r in reminders if not r.get("completed"))
