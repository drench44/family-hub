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
    kind = chore["schedule_kind"]
    if kind == "daily":
        return True
    # A one-time chore reuses rotation_epoch as its single due date: it occurs
    # on exactly that day and never again. Past days still render from the
    # frozen occurrence_log, so it stays on the record after it drops off live.
    if kind == "once":
        return d == _epoch(chore)
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
    # No 'once' branch: one-time chores are always assign_kind='fixed', and this
    # function only drives rotation assignment (assignee_id), so it's never
    # reached for them. A hypothetical rotating one-time chore would fall through
    # to the weekly math below (days_mask=0 -> 0) — add an explicit branch first
    # if that ever becomes a real kind.
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


def plan_rows(chores: list[dict], people: list[dict], d: dt.date,
             away: dict | None = None) -> list[dict]:
    """Live-resolve the day's assignments to flat rows — the exact shape the
    occurrence_log stores, so a frozen past day and a live day render the same
    way. Chores with no resolvable assignee among ``people`` are omitted.

    ``away`` (optional) is ``{"ids": set[int], "backup": {pid: backup_pid |
    None}}``. Away people fall out of rotations (their turns go to whoever's
    home). A fixed chore assigned to an away person reassigns to their backup
    if the backup is present (active and not away), tagging the row
    ``covering_for=<away pid>``; with no available backup the chore pauses
    for the day (row omitted) rather than crashing."""
    away = away or {"ids": set(), "backup": {}}
    away_ids = away.get("ids", set())
    backup = away.get("backup", {})
    active_ids = {p["id"] for p in people}
    present_ids = active_ids - away_ids          # people home & not away
    rows = []
    for chore in chores:
        if not occurs(chore, d):
            continue
        # rotations skip away people (fall to whoever's home); fixed returns
        # its fixed_person_id regardless.
        aid = assignee_id(chore, d, present_ids)
        if aid is None:
            continue
        covering_for = None
        if aid in away_ids:                      # only fixed chores reach here
            b = backup.get(aid)
            if b is None or b not in present_ids:
                continue                         # no available backup -> pause
            covering_for = aid
            aid = b
        if aid not in present_ids:               # inactive/away, no cover
            continue
        rows.append({"chore_id": chore["id"], "person_id": aid,
                     "title": chore["title"], "icon": chore["icon"],
                     "rot": 1 if chore["assign_kind"] == "rotation" else 0,
                     "covering_for": covering_for})
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
                  "rot": bool(r["rot"]), "done": r["chore_id"] in completed,
                  "covering_for": r.get("covering_for")}
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


def streak(occ_by_date: dict, completions_by_date: dict, today: dt.date,
           away_dates: set | None = None) -> int:
    """Consecutive days (walking back from today) where the person had >=1
    recorded chore and completed all of them. ``occ_by_date`` maps 'YYYY-MM-DD'
    -> set of chore_ids assigned to the person that day. Rest days (no entry)
    are skipped. An unfinished today is forgiven — it doesn't break the streak,
    but also doesn't count. ``away_dates`` (optional set of 'YYYY-MM-DD') are
    treated as rest days regardless of what was logged, so an away stretch
    never breaks the streak. Capped at 365 calendar days."""
    away = away_dates or set()
    d = today
    todays = occ_by_date.get(today.isoformat())
    if today.isoformat() not in away and todays \
            and not _all_done(todays, completions_by_date, today):
        d = today - dt.timedelta(days=1)
    count = 0
    for _ in range(365):
        ds = d.isoformat()
        if ds in away:                 # away day == rest, regardless of log
            d -= dt.timedelta(days=1)
            continue
        occ = occ_by_date.get(ds)
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
               today: dt.date, away_dates: set | None = None) -> list[str]:
    """7 entries oldest->today, each 'done'|'partial'|'none'|'rest'|'away'."""
    away = away_dates or set()
    out = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        ds = d.isoformat()
        if ds in away:
            out.append("away")
            continue
        occ = occ_by_date.get(ds)
        if not occ:
            out.append("rest")
            continue
        done = completions_by_date.get(ds, set())
        n = len(occ & done)
        if n == len(occ):
            out.append("done")
        elif n == 0:
            out.append("none")
        else:
            out.append("partial")
    return out
