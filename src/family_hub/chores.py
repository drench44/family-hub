"""Pure chore scheduling / rotation / streak logic. Stdlib only, no I/O.

Conventions:
- days_mask bit 0 = Monday .. bit 6 = Sunday (matches date.weekday()).
- rotation_epoch is a 'YYYY-MM-DD' string; a chore never occurs before it.
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


def assignee_id(chore: dict, d: dt.date) -> int | None:
    if chore["assign_kind"] == "fixed":
        return chore.get("fixed_person_id")
    order = chore["rotation_order"]
    if not order:
        return None
    # n = number of occurrences strictly before d, counting from the epoch.
    n = 0
    cur = _epoch(chore)
    while cur < d:
        if occurs(chore, cur):
            n += 1
        cur += dt.timedelta(days=1)
    return order[n % len(order)]


def _person_chores_on(person_id: int, chores: list[dict], d: dt.date) -> list[dict]:
    return [c for c in chores
            if occurs(c, d) and assignee_id(c, d) == person_id]


def _all_done(chore_list: list[dict], completions_by_date: dict, d: dt.date) -> bool:
    done = completions_by_date.get(d.isoformat(), set())
    return all(c["id"] in done for c in chore_list)


def day_plan(chores: list[dict], people: list[dict], d: dt.date,
             completions) -> list[dict]:
    """Per active person (in the given order): the chores assigned to them on
    ``d`` and their done flags. `completions` is the set of chore_ids completed
    on ``d``. Chores with no resolvable assignee (fixed person inactive/missing,
    empty rotation) are omitted, never crash."""
    completed = set(completions or [])
    plan = []
    for person in people:
        pid = person["id"]
        rows = []
        for chore in chores:
            if not occurs(chore, d):
                continue
            aid = assignee_id(chore, d)
            if aid is None or aid != pid:
                continue
            rows.append({"id": chore["id"], "title": chore["title"],
                         "icon": chore["icon"], "done": chore["id"] in completed})
        done_count = sum(1 for r in rows if r["done"])
        plan.append({
            "person": {"id": pid, "name": person["name"], "color": person["color"]},
            "chores": rows,
            "done_count": done_count,
            "total": len(rows),
        })
    return plan


def streak(person_id: int, chores: list[dict], completions_by_date: dict,
           today: dt.date) -> int:
    """Consecutive days (walking back from today) where the person had >=1
    occurring chore and completed all of them. Rest days (zero occurring chores)
    are skipped. An unfinished today is forgiven — it doesn't break the streak,
    but also doesn't count. Capped at 365 calendar days."""
    d = today
    todays = _person_chores_on(person_id, chores, today)
    if todays and not _all_done(todays, completions_by_date, today):
        d = today - dt.timedelta(days=1)
    count = 0
    for _ in range(365):
        occ = _person_chores_on(person_id, chores, d)
        if not occ:
            d -= dt.timedelta(days=1)
            continue
        if _all_done(occ, completions_by_date, d):
            count += 1
            d -= dt.timedelta(days=1)
        else:
            break
    return count


def week_strip(person_id: int, chores: list[dict], completions_by_date: dict,
               today: dt.date) -> list[str]:
    """7 entries oldest->today, each 'done'|'partial'|'none'|'rest'."""
    out = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        occ = _person_chores_on(person_id, chores, d)
        if not occ:
            out.append("rest")
            continue
        done = completions_by_date.get(d.isoformat(), set())
        n = sum(1 for c in occ if c["id"] in done)
        if n == len(occ):
            out.append("done")
        elif n == 0:
            out.append("none")
        else:
            out.append("partial")
    return out
