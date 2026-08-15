"""Pure chore scheduling / rotation / streak logic. Stdlib only, no I/O.

Conventions:
- days_mask bit 0 = Monday .. bit 6 = Sunday (matches date.weekday()).
- rotation_epoch is a 'YYYY-MM-DD' string; a chore never occurs before it.
- History is FROZEN: the API layer records each served day's plan in the
  occurrence_log table and renders past days from that record, so editing or
  deleting a chore only changes today and the future. The functions here
  therefore split into two groups:
    * live resolution for today/future — occurs / assignee_id / plan_rows
    * history math — day_plan / streak / week_strip, which consume flat
      occurrence rows (live-computed or read back from the log) and per-person
      occurrence maps ('YYYY-MM-DD' -> set of chore_ids assigned that day).
- completions_by_date maps 'YYYY-MM-DD' -> set of chore_ids completed on that
  date *for the queried person* (the API layer builds it per person).
"""
from __future__ import annotations

import datetime as dt


def _epoch(chore: dict) -> dt.date:
    return dt.date.fromisoformat(chore["rotation_epoch"])


def occurs(chore: dict, d: dt.date) -> bool:
    if not chore.get("active", 1):
        return False
    if d < _epoch(chore):
        return False
    if chore["schedule_kind"] == "daily":
        return True
    return bool((chore["days_mask"] >> d.weekday()) & 1)


def occurrences_before(chore: dict, d: dt.date) -> int:
    """Number of occurrences strictly before ``d``, counting from the epoch.
    Closed form (no day walk) so rotation lookups stay O(1) however old the
    chore's epoch is."""
    epoch = _epoch(chore)
    if d <= epoch:
        return 0
    total_days = (d - epoch).days
    if chore["schedule_kind"] == "daily":
        return total_days
    mask = chore["days_mask"]
    full_weeks, rem = divmod(total_days, 7)
    n = full_weeks * mask.bit_count()
    wd = epoch.weekday()
    for i in range(rem):
        n += (mask >> ((wd + i) % 7)) & 1
    return n


def assignee_id(chore: dict, d: dt.date,
                active_ids: set | None = None) -> int | None:
    """Who is assigned on ``d``. ``active_ids`` (when given) drops inactive
    people from a rotation so their turns fall to the remaining members instead
    of producing an unassignable ghost day; None means no filtering."""
    if chore["assign_kind"] == "fixed":
        return chore.get("fixed_person_id")
    order = chore["rotation_order"]
    if active_ids is not None:
        order = [pid for pid in order if pid in active_ids]
    if not order:
        return None
    return order[occurrences_before(chore, d) % len(order)]


def plan_rows(chores: list[dict], people: list[dict], d: dt.date) -> list[dict]:
    """Live-resolve the day's assignments to flat rows — the exact shape the
    occurrence_log stores, so a frozen past day and a live day render the same
    way. Chores with no resolvable assignee among ``people`` are omitted."""
    active_ids = {p["id"] for p in people}
    rows = []
    for chore in chores:
        if not occurs(chore, d):
            continue
        aid = assignee_id(chore, d, active_ids)
        if aid is None or aid not in active_ids:
            continue
        rows.append({"chore_id": chore["id"], "person_id": aid,
                     "title": chore["title"], "icon": chore["icon"],
                     "rot": 1 if chore["assign_kind"] == "rotation" else 0})
    return rows


def day_plan(rows: list[dict], people: list[dict], completions) -> list[dict]:
    """Per active person (in the given order): the chores assigned to them and
    their done flags, assembled from flat occurrence rows. ``completions`` is
    the set of chore_ids completed that day. Rows for people not in the list
    (e.g. since deactivated) are dropped, never crash."""
    completed = set(completions or [])
    plan = []
    for person in people:
        pid = person["id"]
        prows = [{"id": r["chore_id"], "title": r["title"], "icon": r["icon"],
                  "rot": bool(r["rot"]), "done": r["chore_id"] in completed}
                 for r in rows if r["person_id"] == pid]
        plan.append({
            "person": {"id": pid, "name": person["name"], "color": person["color"]},
            "chores": prows,
            "done_count": sum(1 for r in prows if r["done"]),
            "total": len(prows),
        })
    return plan


def _all_done(occ: set, completions_by_date: dict, d: dt.date) -> bool:
    done = completions_by_date.get(d.isoformat(), set())
    return occ <= done


def streak(occ_by_date: dict, completions_by_date: dict, today: dt.date) -> int:
    """Consecutive days (walking back from today) where the person had >=1
    recorded chore and completed all of them. ``occ_by_date`` maps 'YYYY-MM-DD'
    -> set of chore_ids assigned to the person that day. Rest days (no entry)
    are skipped. An unfinished today is forgiven — it doesn't break the streak,
    but also doesn't count. Capped at 365 calendar days."""
    d = today
    todays = occ_by_date.get(today.isoformat())
    if todays and not _all_done(todays, completions_by_date, today):
        d = today - dt.timedelta(days=1)
    count = 0
    for _ in range(365):
        occ = occ_by_date.get(d.isoformat())
        if not occ:
            d -= dt.timedelta(days=1)
            continue
        if _all_done(occ, completions_by_date, d):
            count += 1
            d -= dt.timedelta(days=1)
        else:
            break
    return count


def week_strip(occ_by_date: dict, completions_by_date: dict,
               today: dt.date) -> list[str]:
    """7 entries oldest->today, each 'done'|'partial'|'none'|'rest'."""
    out = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        occ = occ_by_date.get(d.isoformat())
        if not occ:
            out.append("rest")
            continue
        done = completions_by_date.get(d.isoformat(), set())
        n = len(occ & done)
        if n == len(occ):
            out.append("done")
        elif n == 0:
            out.append("none")
        else:
            out.append("partial")
    return out
