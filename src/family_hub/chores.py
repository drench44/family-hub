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


def _week_index(d: dt.date, epoch: dt.date) -> int:
    """Monday-anchored week number of ``d`` relative to the epoch's week (0 = the
    epoch's own week). Drives biweekly ('days' + week_interval) cycling so 'every
    other week' means every other calendar week, independent of the weekday."""
    d_mon = d - dt.timedelta(days=d.weekday())
    e_mon = epoch - dt.timedelta(days=epoch.weekday())
    return (d_mon - e_mon).days // 7


def occurs(chore: dict, d: dt.date) -> bool:
    if not chore.get("active", 1):
        return False
    epoch = _epoch(chore)
    if d < epoch:
        return False
    kind = chore["schedule_kind"]
    if kind == "daily":
        return True
    # A one-time chore reuses rotation_epoch as its single due date: it occurs
    # on exactly that day and never again. Past days still render from the
    # frozen occurrence_log, so it stays on the record after it drops off live.
    if kind == "once":
        return d == epoch
    # Every-N-days from the epoch, ignoring weekday.
    if kind == "interval":
        n = chore.get("interval_days") or 1
        return (d - epoch).days % n == 0
    # 'days': masked weekday, optionally only every week_interval-th week
    # (1 = weekly, the default and existing behavior; 2 = biweekly).
    if not ((chore["days_mask"] >> d.weekday()) & 1):
        return False
    w = chore.get("week_interval") or 1
    return _week_index(d, epoch) % w == 0


def occurrences_before(chore: dict, d: dt.date) -> int:
    """Number of occurrences strictly before ``d``, counting from the epoch.
    Closed form (no day walk) so rotation lookups stay O(1) however old the
    chore's epoch is."""
    epoch = _epoch(chore)
    if d <= epoch:
        return 0
    total_days = (d - epoch).days
    kind = chore["schedule_kind"]
    if kind == "daily":
        return total_days
    # Every-N-days: occurrences at offsets 0, N, 2N, ...; count those < total_days.
    if kind == "interval":
        n = chore.get("interval_days") or 1
        return (total_days - 1) // n + 1   # total_days > 0 here
    # No 'once' branch: one-time chores are always assign_kind='fixed', and this
    # function only drives rotation assignment (assignee_id), so it's never
    # reached for them. A hypothetical rotating one-time chore would fall through
    # to the weekly math below (days_mask=0 -> 0) — add an explicit branch first
    # if that ever becomes a real kind.
    mask = chore["days_mask"]
    w = chore.get("week_interval") or 1
    if w == 1:
        # weekly (the default): existing closed form, byte-for-byte unchanged.
        full_weeks, rem = divmod(total_days, 7)
        n = full_weeks * mask.bit_count()
        wd = epoch.weekday()
        for i in range(rem):
            n += (mask >> ((wd + i) % 7)) & 1
        return n
    # week_interval > 1 (biweekly): only every w-th Mon-anchored week is on-cycle.
    # Count masked days in on-cycle weeks over [epoch, d) via f(hi) - f(lo), where
    # j = epoch.weekday() + offset walks day columns and full-week groups g = j//7
    # are on-cycle iff g % w == 0.
    a = epoch.weekday()
    pc = mask.bit_count()

    def _f(m: int) -> int:
        if m <= 0:
            return 0
        full, rem = divmod(m, 7)
        # multiples of w in [0, full): 0, w, 2w, ... — that many on-cycle full weeks
        on_cycle_full = 0 if full <= 0 else (full - 1) // w + 1
        total = on_cycle_full * pc
        if full % w == 0:                       # the partial trailing week is on-cycle
            total += sum((mask >> k) & 1 for k in range(rem))
        return total

    return _f(a + total_days) - _f(a)


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


def away_view_on(amap: dict, d_iso: str) -> dict:
    """Reshape one date out of a db.away_map() window into the ``{"ids",
    "backup"}`` view ``plan_rows`` consumes. Pure — shared by the wall render
    (app._away_view) and the iCloud chore mirror so both resolve the SAME
    assignees for a day; a divergence there would mirror a reminder to a
    different person than the wall shows (and credit the wrong streak on an
    iOS check-off)."""
    ids = {pid for pid, info in amap.items() if d_iso in info["dates"]}
    return {"ids": ids,
            "backup": {pid: amap[pid]["backup_on"].get(d_iso) for pid in ids}}


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
    for the day (row omitted) rather than crashing -- except a one-time
    ('once') chore, which stays with its away owner because pausing its single
    dated occurrence would destroy it. An owner who is no longer active is
    dropped before any of that: inactive beats away-cover."""
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
        # An owner who is no longer in the household (deactivated or deleted)
        # drops out BEFORE the away branch: inactive beats away-cover. With the
        # order reversed, deactivating someone who still had an open away period
        # parked their chores on the backup forever.
        if aid not in active_ids:
            continue
        covering_for = None
        if aid in away_ids:                      # only fixed chores reach here
            b = backup.get(aid)
            if b is not None and b in present_ids:
                covering_for = aid
                aid = b
            elif chore["schedule_kind"] != "once":
                continue                         # no available backup -> pause
            # A 'once' chore is a DATED COMMITMENT: it occurs on exactly one day
            # and never again, so pausing it destroys it outright. With nobody
            # available to cover, it stays on the away owner's card (the wall
            # renders it alongside their Away badge) instead of vanishing.
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
