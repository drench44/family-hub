"""iCloud Reminders (VTODO): parse + group + the write-side ICS mutators, kept
separate from the local To-Dos.

`parse_vtodo` lazily imports icalendar (so this module imports without it);
`group`/`open_count` are pure stdlib. A reminder dict is
{id, title, due (ISO str or None), completed (bool), priority (int|None),
list_id, list_name}.

Two-way (the wall writes back to iCloud) is built on three PURE ICS transforms
here — no network, no DB — so the whole edit is unit-testable: `set_completed`
flips a VTODO's status, `build_vtodo` mints a new one. caldav_sync flushes the
resulting object to the server; these functions only shape the bytes.
"""
from __future__ import annotations

import datetime as dt

BUCKETS = ("overdue", "today", "upcoming", "no_date")

# The properties completion state owns; cleared before we re-set them so a second
# toggle can't leave a stale COMPLETED next to STATUS:NEEDS-ACTION (iCloud reads
# the pair inconsistently otherwise).
_COMPLETION_PROPS = ("STATUS", "COMPLETED", "PERCENT-COMPLETE", "LAST-MODIFIED",
                     "SEQUENCE")


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


# --- write side (two-way): pure ICS transforms ----------------------------

def _utc(now: dt.datetime) -> dt.datetime:
    """A tz-aware UTC datetime, so icalendar emits '...Z' (global rule: store
    UTC). A naive `now` is assumed already-UTC (the server calls it that way)."""
    return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)


def set_completed(ics_data, completed: bool, now: dt.datetime) -> str:
    """Return `ics_data` with its (first) VTODO flipped to done / not-done.

    Done → STATUS:COMPLETED + PERCENT-COMPLETE:100 + COMPLETED:<now>; reopened →
    STATUS:NEEDS-ACTION + PERCENT-COMPLETE:0, COMPLETED dropped. SEQUENCE is bumped
    and LAST-MODIFIED stamped either way so iCloud accepts it as a newer revision.
    Raises ValueError if there's no VTODO (never silently no-op a write)."""
    import icalendar
    cal = icalendar.Calendar.from_ical(ics_data)
    todo = next((c for c in cal.walk("VTODO")), None)
    if todo is None:
        raise ValueError("no VTODO in ICS")
    seq = 0
    try:
        seq = int(todo.get("SEQUENCE") or 0)
    except Exception:
        seq = 0
    for k in _COMPLETION_PROPS:
        if k in todo:
            del todo[k]
    now = _utc(now)
    if completed:
        todo.add("STATUS", "COMPLETED")
        todo.add("PERCENT-COMPLETE", 100)
        todo.add("COMPLETED", now)
    else:
        todo.add("STATUS", "NEEDS-ACTION")
        todo.add("PERCENT-COMPLETE", 0)
    todo.add("LAST-MODIFIED", now)
    todo.add("SEQUENCE", seq + 1)
    return cal.to_ical().decode("utf-8")


def build_vtodo(uid: str, title: str, now: dt.datetime, due=None) -> str:
    """A fresh single-VTODO VCALENDAR for a reminder added on the wall. `due` is a
    date (all-day, VALUE=DATE — the common iCloud case) or None; STATUS starts
    NEEDS-ACTION. UID is caller-supplied so the local row id and the server object
    agree before the first push."""
    import icalendar
    cal = icalendar.Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "-//family-hub//caldav//EN")
    todo = icalendar.Todo()
    now = _utc(now)
    todo.add("UID", uid)
    todo.add("SUMMARY", title)
    todo.add("DTSTAMP", now)
    todo.add("CREATED", now)
    todo.add("LAST-MODIFIED", now)
    todo.add("SEQUENCE", 0)
    todo.add("STATUS", "NEEDS-ACTION")
    if due is not None:
        todo.add("DUE", due)   # a date -> VALUE=DATE; a datetime -> timed
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")
