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
    """A UTC datetime, so icalendar emits '...Z' (global rule: store UTC). A
    tz-aware `now` is CONVERTED to UTC (the mirror passes a LOCAL-zone now); a
    naive one is assumed already-UTC. Converting — not just attaching — is what
    keeps DTSTAMP/CREATED/LAST-MODIFIED RFC-5545-valid; a bare local TZID with no
    VTIMEZONE would be rejected by iCloud."""
    return now.astimezone(dt.timezone.utc) if now.tzinfo \
        else now.replace(tzinfo=dt.timezone.utc)


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
    for k in (*_COMPLETION_PROPS, "DTSTAMP"):
        if k in todo:
            del todo[k]
    now = _utc(now)
    todo.add("DTSTAMP", now)   # RFC 5545: bump on every revision we send
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


def _local_to_utc(d: dt.date, hhmm: str, tz) -> dt.datetime:
    """date + 'HH:MM' interpreted in the wall's zone `tz`, converted to UTC —
    RFC 5545 requires absolute DUE/alarm times in UTC; the device shows them back
    in its own local zone (the family shares one)."""
    h, m = hhmm.split(":")
    local = dt.datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=tz)
    return local.astimezone(dt.timezone.utc)


def build_chore_vtodo(uid: str, title: str, due_date: dt.date,
                      due_times, now: dt.datetime, tz=None) -> str:
    """A VTODO for one chore occurrence mirrored into a person's iCloud list.
    No `due_times` (or no `tz`) -> an all-day reminder due that date. With times
    -> DUE at the first time and one VALARM per time (absolute UTC triggers, the
    EKAlarm equivalent) so iOS fires a native notification at each. `tz` is the
    wall's zone; the local 'HH:MM' times are converted to UTC per RFC 5545."""
    import icalendar
    cal = icalendar.Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "-//family-hub//caldav//EN")
    todo = icalendar.Todo()
    nowu = _utc(now)
    todo.add("UID", uid)
    todo.add("SUMMARY", title)
    todo.add("DTSTAMP", nowu)
    todo.add("CREATED", nowu)
    todo.add("LAST-MODIFIED", nowu)
    todo.add("SEQUENCE", 0)
    todo.add("STATUS", "NEEDS-ACTION")
    times = sorted({t for t in (due_times or [])})
    if times and tz is not None:
        todo.add("DUE", _local_to_utc(due_date, times[0], tz))
        for t in times:
            alarm = icalendar.Alarm()
            alarm.add("ACTION", "DISPLAY")
            alarm.add("DESCRIPTION", title)
            # absolute trigger MUST be typed DATE-TIME (untyped defaults to DURATION)
            alarm.add("TRIGGER", _local_to_utc(due_date, t, tz),
                      parameters={"VALUE": "DATE-TIME"})
            todo.add_component(alarm)
    else:
        todo.add("DUE", due_date)                              # all-day VALUE=DATE
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")
