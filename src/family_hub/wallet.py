"""The chore wallet's money: the glue between the database and rewards.py.

- A child's run (streak, shields) is worked out from the frozen log, the
  completions and the served days since launch, never stored.
- Chore earnings are summed from the log's frozen ``cents`` on house rows
  that were done. Streak bonuses and payouts are ledger rows.
- Wallet = all chore earnings + all bonuses - all payouts. An old-day edit
  after a payout (PIN-guarded) moves the wallet, even below zero.
- A bonus is paid once per (child, run, streak length) and never clawed back.
"""
from __future__ import annotations

import datetime as dt

from . import db as fdb
from . import rewards as rw

LAUNCH_KEY = "rewards.launch"
SHIELD_RULES_KEY = "rewards.shield_rules"
MS_FLOOR_KEY = "rewards.ms_floor"


def shield_rules(conn, settings: dict, launch: dict) -> list:
    """The shield settings in force on each day since launch, oldest first:
    [[from_day, on, every, max], ...]. A wallet launched before rules were
    recorded uses today's settings from launch day."""
    rules = fdb.kv_get(conn, SHIELD_RULES_KEY)
    if rules:
        return [tuple(r) for r in rules]
    return [(launch["day"], bool(settings.get("shields_on", True)),
             int(settings.get("shield_every", 20)),
             int(settings.get("shield_max", 2)))]


def record_shield_rule(conn, settings: dict, launch: dict, today: dt.date) -> None:
    """New shield settings apply from today on; earlier days keep theirs."""
    rules = [list(r) for r in shield_rules(conn, settings, launch)
             if r[0] < today.isoformat()]
    rules.append([today.isoformat(), bool(settings["shields_on"]),
                  int(settings["shield_every"]), int(settings["shield_max"])])
    fdb.kv_set(conn, SHIELD_RULES_KEY, rules)


def record_milestone_floor(conn, pid: int, run) -> None:
    """Milestones changed while this run was going: whatever the run already
    passed is behind it, so only milestones still ahead can pay."""
    floors = fdb.kv_get(conn, MS_FLOOR_KEY) or {}
    if run.run_key:
        floors[str(pid)] = [run.run_key, int(run.streak)]
    else:
        floors.pop(str(pid), None)
    fdb.kv_set(conn, MS_FLOOR_KEY, floors)
PACE_DAYS = 56


def _seed(launch: dict, pid: int) -> tuple[int, str]:
    return (int((launch.get("seeds") or {}).get(str(pid), 0)),
            "seed:" + launch["day"])


def person_run(conn, pid: int, today: dt.date, settings: dict, launch: dict,
               away: set) -> rw.Run:
    start = launch["day"]
    rows: dict[str, list] = {}
    for r in fdb.logs_between(conn, start, today.isoformat()):
        if r["person_id"] == pid:
            rows.setdefault(r["date"], []).append(r)
    done = {(r["chore_id"], r["date"])
            for r in fdb.completions_between(conn, start, today.isoformat())}
    seed, seed_key = _seed(launch, pid)
    return rw.run_state(
        rows, done, start=dt.date.fromisoformat(start), today=today,
        away=away, served=fdb.served_between(conn, start, today.isoformat()),
        seed=seed, seed_key=seed_key,
        shield_rules=shield_rules(conn, settings, launch))


def _run_start(run_key: str, launch_day: str) -> str:
    return launch_day if run_key.startswith("seed:") else run_key


def award(conn, pid: int, run: rw.Run, settings: dict, today: dt.date) -> list:
    """Pay every milestone this run has reached and not been paid for.

    A run's floor is what is already paid on it (for the seeded run, never
    below the seed: launch() paid whatever the seed had passed). A milestone
    already paid at the same streak length on or after this run's first day
    is skipped too, so a late fix that re-keys a run cannot pay it twice."""
    if not settings.get("bonus_on") or not run.run_key or run.streak <= 0:
        return []
    launch = fdb.kv_get(conn, LAUNCH_KEY)
    if not launch:
        return []
    ledger = [r for r in fdb.ledger_for(conn, pid) if r["kind"] == "bonus"]
    floor = max([r["at_streak"] for r in ledger if r["run_key"] == run.run_key],
                default=0)
    seed, seed_key = _seed(launch, pid)
    if run.run_key == seed_key:
        floor = max(floor, seed)
    changed = (fdb.kv_get(conn, MS_FLOOR_KEY) or {}).get(str(pid))
    if changed and changed[0] == run.run_key:
        floor = max(floor, int(changed[1]))
    start = _run_start(run.run_key, launch["day"])
    taken = {r["at_streak"] for r in ledger if r["day"] >= start}
    paid = []
    for at, cents in rw.milestones_crossed(floor, run.streak,
                                           settings.get("milestones") or []):
        if at in taken:
            continue
        rid = fdb.add_ledger(conn, person_id=pid, kind="bonus", cents=cents,
                             day=today.isoformat(), run_key=run.run_key,
                             at_streak=at, note=f"{at}-day streak")
        if rid:
            paid.append({"id": rid, "at_streak": at, "cents": cents})
    return paid


def earned_cents(conn, pid: int, launch_day: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(o.cents), 0) FROM occurrence_log o "
        "JOIN completions c ON c.chore_id = o.chore_id AND c.date = o.date "
        "WHERE o.person_id = ? AND o.kind = 'house' AND o.date >= ?",
        (pid, launch_day)).fetchone()
    return int(row[0])


def balance_cents(conn, pid: int, launch_day: str) -> int:
    bonus = payout = 0
    for r in fdb.ledger_for(conn, pid):
        if r["kind"] == "bonus":
            bonus += r["cents"]
        else:
            payout += r["cents"]
    return earned_cents(conn, pid, launch_day) + bonus - payout


def pay_out(conn, pid: int, today: dt.date) -> dict | None:
    """Pay out the whole balance. The read and the write are one IMMEDIATE
    transaction, so two taps at once (two requests, two connections) see
    one balance: the second waits, then finds $0 and pays nothing."""
    launch = fdb.kv_get(conn, LAUNCH_KEY)
    if not launch:
        return None
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        bal = balance_cents(conn, pid, launch["day"])
        if bal <= 0:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO ledger(person_id, kind, cents, run_key, at_streak, day, "
            "created_at, note) VALUES(?, 'payout', ?, NULL, NULL, ?, ?, 'Paid')",
            (pid, bal, today.isoformat(), fdb._now_iso()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": int(cur.lastrowid), "kind": "payout", "cents": bal,
            "day": today.isoformat()}


def launch(conn, today: dt.date, settings: dict, seeds: dict) -> dict:
    """Turn the wallet on, once. ``seeds`` maps person id -> the streak the
    wall showed that day under the old rules; each run carries on from it.
    Milestones a seed has already passed are paid now, on that run, so the
    total by each milestone is what was promised. A second call returns the
    existing record and changes nothing."""
    rec = fdb.kv_get(conn, LAUNCH_KEY)
    if rec:
        return rec
    rec = {"day": today.isoformat(),
           "seeds": {str(pid): int(n) for pid, n in seeds.items()}}
    fdb.kv_set(conn, LAUNCH_KEY, rec)
    fdb.kv_set(conn, SHIELD_RULES_KEY, [[rec["day"],
                                         bool(settings.get("shields_on", True)),
                                         int(settings.get("shield_every", 20)),
                                         int(settings.get("shield_max", 2))]])
    if settings.get("bonus_on"):
        for pid, n in seeds.items():
            for at, cents in rw.milestones_crossed(
                    0, int(n), settings.get("milestones") or []):
                fdb.add_ledger(conn, person_id=int(pid), kind="bonus",
                               cents=cents, day=rec["day"],
                               run_key="seed:" + rec["day"], at_streak=at,
                               note=f"{at}-day streak (before the wallet)")
    return rec


def cents_for_today(conn, pid: int, today: dt.date, settings: dict,
                    launch: dict, away: set) -> int:
    """What one house chore pays today: the rate for the streak as it stood
    at the end of yesterday, so today's own ticks never change today's rate."""
    run = person_run(conn, pid, today, settings, launch, away)
    streak = run.streak - (1 if run.outcomes.get(today.isoformat()) == rw.KEPT
                           else 0)
    return rw.rate_for(streak, settings)


def pace(conn, pid: int, today: dt.date) -> dict:
    """House chores a week and the share done, over the last 8 weeks
    (today not included). No chores on record: 0 a week, a perfect share."""
    lo = (today - dt.timedelta(days=PACE_DAYS)).isoformat()
    hi = (today - dt.timedelta(days=1)).isoformat()
    n, done = conn.execute(
        "SELECT COUNT(*), COUNT(c.chore_id) FROM occurrence_log o "
        "LEFT JOIN completions c ON c.chore_id = o.chore_id AND c.date = o.date "
        "WHERE o.person_id = ? AND o.kind = 'house' AND o.date BETWEEN ? AND ?",
        (pid, lo, hi)).fetchone()
    return {"chores_per_week": round(n * 7 / PACE_DAYS, 2),
            "done_share": round(done / n, 4) if n else 1.0}


def history(conn, pid: int, launch_day: str) -> dict:
    """Earnings by week (Monday start) and every ledger row, newest first."""
    weeks: dict[str, int] = {}
    for ds, cents in conn.execute(
            "SELECT o.date, o.cents FROM occurrence_log o "
            "JOIN completions c ON c.chore_id = o.chore_id AND c.date = o.date "
            "WHERE o.person_id = ? AND o.kind = 'house' AND o.date >= ?",
            (pid, launch_day)):
        d = dt.date.fromisoformat(ds)
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        weeks[wk] = weeks.get(wk, 0) + int(cents)
    return {"weeks": [{"week_start": k, "earned_cents": v}
                      for k, v in sorted(weeks.items(), reverse=True)],
            "ledger": list(reversed(fdb.ledger_for(conn, pid)))}
