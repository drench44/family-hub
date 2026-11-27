"""The chore wallet's rules. Pure, stdlib only, no I/O (like chores.py).

A child's streak counts CALENDAR days, so a child with chores twice a week
moves toward a goal as fast as one with chores every day:

- **kept**: every house chore assigned that day was done, or nothing was
  assigned (a rest day counts);
- **away** / **unserved**: the child was away, or the wall never served the
  day (an outage). Neither counts nor breaks;
- **saved**: a miss that spent a shield. Doesn't count, doesn't break;
- **missed**: a miss with no shield. The run ends;
- **open**: today, not finished yet. Not judged until it ends.

Only rows frozen with kind 'house' count. 'personal' (a personal goal) and
'once' (a one-time item) never earn or touch the streak.

Shields: one for every ``shield_every`` kept days in the run, holding at most
``shield_max``. A miss spends one automatically.

Money is integer cents. The streak reward is one of two styles or both:
milestone bonuses (``milestones``: [[days, cents], ...], repeating every cycle
of the largest day count) and higher pay per chore (``levels``: [[days,
cents per chore], ...], the highest level reached applies).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

KEPT, SAVED, MISSED, AWAY, UNSERVED, OPEN = (
    "kept", "saved", "missed", "away", "unserved", "open")


@dataclass(frozen=True)
class Run:
    streak: int
    shields: int
    run_key: str | None
    outcomes: dict = field(default_factory=dict)


def _counts(row: dict) -> bool:
    return (row.get("kind") or "house") == "house"


def run_state(rows_by_date: dict, done: set, *, start: dt.date, today: dt.date,
              away: set, served: set, seed: int = 0,
              seed_key: str | None = None, shield_every: int = 20,
              shield_max: int = 2, shields_on: bool = True) -> Run:
    """Walk ``start`` .. ``today`` and return the run as of today.

    ``rows_by_date`` maps 'YYYY-MM-DD' -> the child's frozen rows that day
    (each with ``chore_id`` and ``kind``); ``done`` holds (chore_id, date)
    pairs that were completed. ``seed`` is a streak carried in from before
    ``start`` (the launch carry-over); its run is keyed ``seed_key``."""
    streak = seed
    run_key = seed_key if seed else None
    kept_in_run = shields = 0
    outcomes: dict[str, str] = {}
    d = start
    while d <= today:
        ds = d.isoformat()
        d += dt.timedelta(days=1)
        if ds in away:
            outcomes[ds] = AWAY
            continue
        if ds not in served:
            outcomes[ds] = UNSERVED
            continue
        items = [r for r in rows_by_date.get(ds, ()) if _counts(r)]
        kept = all((r["chore_id"], ds) in done for r in items)
        if not kept and ds == today.isoformat():
            outcomes[ds] = OPEN
            break
        if kept:
            outcomes[ds] = KEPT
            streak += 1
            kept_in_run += 1
            run_key = run_key or ds
            if shields_on and kept_in_run % shield_every == 0 \
                    and shields < shield_max:
                shields += 1
        elif shields:
            outcomes[ds] = SAVED
            shields -= 1
        else:
            outcomes[ds] = MISSED
            streak = kept_in_run = shields = 0
            run_key = None
    return Run(streak, shields, run_key, outcomes)


def _cycle(milestones) -> int:
    return max((int(m[0]) for m in milestones), default=0)


def _thresholds(milestones, lo: int, hi: int):
    """(at, cents) for every repeating milestone with lo < at <= hi, sorted."""
    cycle = _cycle(milestones)
    if cycle <= 0 or hi <= lo:
        return []
    out = []
    for n in range(lo // cycle, hi // cycle + 1):
        for days, cents in milestones:
            at = n * cycle + int(days)
            if lo < at <= hi:
                out.append((at, int(cents)))
    return sorted(out)


def milestones_crossed(prev_streak: int, streak: int, milestones) -> list:
    return _thresholds(milestones, prev_streak, streak)


def next_milestone(streak: int, milestones):
    cycle = _cycle(milestones)
    if cycle <= 0:
        return None
    nxt = _thresholds(milestones, streak, streak + cycle)
    return nxt[0] if nxt else None


def _levels(settings: dict) -> list:
    if not settings.get("levels_on"):
        return []
    return sorted((int(d), int(c)) for d, c in settings.get("levels") or [])


def rate_for(streak: int, settings: dict) -> int:
    rate = int(settings["cents_per_chore"])
    for days, cents in _levels(settings):
        if streak >= days:
            rate = cents
    return rate


def next_level(streak: int, settings: dict):
    for days, cents in _levels(settings):
        if days > streak:
            return (days, cents)
    return None


def goal_for(streak: int, settings: dict) -> int:
    if settings.get("bonus_on"):
        nm = next_milestone(streak, settings.get("milestones") or [])
        if nm:
            return nm[0]
    nl = next_level(streak, settings)
    if nl:
        return nl[0]
    return (streak // 100 + 1) * 100


def _bonus_per_week(settings: dict) -> float:
    ms = settings.get("milestones") or []
    if not settings.get("bonus_on") or not ms:
        return 0.0
    return sum(int(c) for _, c in ms) * 7 / _cycle(ms)


def _span(week: int) -> dict:
    return {"week": week, "month": round(week * 52 / 12), "year": week * 52}


def project(per_child: list, settings: dict) -> dict:
    """What the wallet costs, per child and for the family, in cents.

    perfect: every chore done, at the top pay level, every milestone hit on
    its cycle: the most it can cost. usual: each child's own pace (share of
    chores done, current streak's rate); bonuses scaled by the chance of a
    clean week (``done_share ** 7``, shields ignored), so it is labeled
    "about" and errs low on bonus money."""
    top = max([int(settings["cents_per_chore"])]
              + [c for _, c in _levels(settings)])
    bonus_week = _bonus_per_week(settings)
    children = []
    tot = {"perfect": {"week": 0, "month": 0, "year": 0},
           "usual": {"week": 0, "month": 0, "year": 0}}
    for k in per_child:
        n, share = float(k["chores_per_week"]), float(k["done_share"])
        perfect = _span(round(n * top) + round(bonus_week))
        usual = _span(round(n * share * rate_for(int(k["streak"]), settings))
                      + round(bonus_week * share ** 7))
        children.append({"id": k["id"], "perfect": perfect, "usual": usual})
        for row, vals in (("perfect", perfect), ("usual", usual)):
            for span in vals:
                tot[row][span] += vals[span]
    return {"children": children, "total": tot}


def format_cents(c: int) -> str:
    if 0 < c < 100:
        return f"{c}¢"
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}${c // 100}.{c % 100:02d}"
