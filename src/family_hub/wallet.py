"""The chore wallet's money: the glue between the database and rewards.py.

- A child's run (streak, shields) is worked out from the frozen log, the
  completions and the served days since launch, never stored.
- Chore earnings are summed from the log's frozen ``cents`` on house rows
  that were done. Streak bonuses and payouts are ledger rows.
- Wallet = all chore earnings + all bonuses - all payouts. An old-day edit
  after a payout (PIN-guarded) moves the wallet, even below zero.
- The summary (``summary``) splits that same balance into what came in
  since the last payout. Paid zeroes the wallet, so it starts over by itself.
- A bonus is paid once per (child, run, streak length) and never clawed back.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import db as fdb
from . import parent_pin
from . import rewards as rw

LAUNCH_KEY = "rewards.launch"
SHIELD_RULES_KEY = "rewards.shield_rules"
MS_FLOOR_KEY = "rewards.ms_floor"
SETTINGS_KEY = "rewards.settings"
FIX_CUTOFF_DEFAULT = "09:00"

log = logging.getLogger("family_hub")
_logged_mismatch: set = set()     # (person, payout id) already logged


def edit_floor(conn, now: dt.datetime) -> dt.date | None:
    """The oldest day a tick may still change without the parent PIN, or
    None when the wallet is off or no PIN is set (then any day may change,
    as on the wall). Today,
    or yesterday until the morning cutoff. For callers outside the web app
    (the iPhone reminder sync); the app's own guards use the same rule."""
    if not (fdb.kv_get(conn, LAUNCH_KEY) and parent_pin.is_set(conn)
            and fdb.integration_enabled(conn, "rewards", default=True)):
        return None
    cut = ((fdb.kv_get(conn, SETTINGS_KEY) or {}).get("fix_cutoff")
           or FIX_CUTOFF_DEFAULT)
    hh, mm = (int(x) for x in cut.split(":"))
    y = now.date() - dt.timedelta(days=1)
    return y if (now.hour, now.minute) < (hh, mm) else now.date()


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


PAUSED_KEY = "rewards.paused"


def record_active_change(conn, pid: int, active: bool, today: dt.date) -> None:
    """A person switched off from ``today`` (or back on from ``today``). The
    days they were off count like away days: their chores left the wall, so
    those days must not count as kept rest days."""
    paused = fdb.kv_get(conn, PAUSED_KEY) or {}
    spans = [list(s) for s in paused.get(str(pid), [])]
    ts = today.isoformat()
    if not active:
        if not (spans and spans[-1][1] is None):
            spans.append([ts, None])
    elif spans and spans[-1][1] is None:
        end = (today - dt.timedelta(days=1)).isoformat()
        if end < spans[-1][0]:
            spans.pop()                  # off and back on the same day
        else:
            spans[-1][1] = end
    paused[str(pid)] = spans
    fdb.kv_set(conn, PAUSED_KEY, paused)


def paused_days(conn, pid: int, today: dt.date) -> set:
    out = set()
    for lo, hi in (fdb.kv_get(conn, PAUSED_KEY) or {}).get(str(pid), []):
        d, end = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi) if hi else today
        while d <= min(end, today):
            out.add(d.isoformat())
            d += dt.timedelta(days=1)
    return out


def person_run(conn, pid: int, today: dt.date, settings: dict, launch: dict,
               away: set) -> rw.Run:
    """The run as of today. A run only starts once a person has house chores:
    someone with none yet (added after launch, personal goals only, a parent
    on the wall) has no streak until their first house chore day, so empty
    days before it never count as kept. A seeded run starts on launch day."""
    start = launch["day"]
    rows: dict[str, list] = {}
    for r in fdb.logs_between(conn, start, today.isoformat()):
        if r["person_id"] == pid:
            rows.setdefault(r["date"], []).append(r)
    seed, seed_key = _seed(launch, pid)
    if not seed:
        house = sorted(ds for ds, rs in rows.items()
                       if any((r.get("kind") or "house") == "house" for r in rs))
        if not house:
            return rw.Run(0, 0, None, {})
        start = house[0]
    done = {(r["chore_id"], r["date"])
            for r in fdb.completions_between(conn, start, today.isoformat())}
    return rw.run_state(
        rows, done, start=dt.date.fromisoformat(start), today=today,
        away=set(away) | paused_days(conn, pid, today),
        served=fdb.served_between(conn, start, today.isoformat()),
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


_EARNED = ("FROM occurrence_log o "
           "JOIN completions c ON c.chore_id = o.chore_id AND c.date = o.date "
           "WHERE COALESCE(o.earner, o.person_id) = ? AND o.kind = 'house' "
           "AND o.date >= ?")


def chores_earned(conn, pid: int, launch_day: str) -> tuple[int, int]:
    """(how many, cents) of the done house chores this person earned since
    launch. Done house chores pay their earner: who owned the row when it
    was ticked (``earner``), else its owner (``person_id``)."""
    n, cents = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(o.cents), 0) " + _EARNED,
        (pid, launch_day)).fetchone()
    return int(n), int(cents)


def earned_cents(conn, pid: int, launch_day: str) -> int:
    return chores_earned(conn, pid, launch_day)[1]


def balance_cents(conn, pid: int, launch_day: str) -> int:
    bonus = payout = 0
    for r in fdb.ledger_for(conn, pid):
        if r["kind"] == "bonus":
            bonus += r["cents"]
        else:
            payout += r["cents"]
    return earned_cents(conn, pid, launch_day) + bonus - payout


def _instant(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ticked_after(conn, pid: int, launch_day: str, at: str) -> tuple[int, int]:
    """(how many, cents) of this person's done house chores ticked after the
    instant ``at``. Compared as instants, not text: isoformat drops the
    fraction on a whole second, and a stamp may carry another offset."""
    cut = _instant(at)
    n = cents = 0
    for done_at, c in conn.execute("SELECT c.done_at, o.cents " + _EARNED,
                                   (pid, launch_day)):
        if _instant(done_at) > cut:
            n, cents = n + 1, cents + int(c)
    return n, cents


def _chore_unit(conn, pid: int) -> int:
    """What one of this person's house chores paid most recently (done or
    not: the log keeps the price), for turning money into a count."""
    row = conn.execute(
        "SELECT cents FROM occurrence_log WHERE COALESCE(earner, person_id) = ? "
        "AND kind = 'house' AND cents > 0 ORDER BY date DESC, chore_id DESC "
        "LIMIT 1", (pid,)).fetchone()
    return int(row[0]) if row else 0


def _legacy_count(conn, pid: int, launch_day: str, payout: dict,
                  chore_cents: int) -> int:
    """Chores since a payout written before payouts kept their count.

    The ticks after it are a start, but they cannot see a paid chore that
    was unticked (its row is gone) and they count a paid chore ticked again.
    The money can: what the chores since are worth beyond those ticks is
    turned into chores at the person's chore price. So it never says
    "0 chores, -30¢" or "1 chore, 0¢". Logged once if it still can't agree."""
    n, cents = _ticked_after(conn, pid, launch_day, payout["created_at"])
    gap = chore_cents - cents
    unit = _chore_unit(conn, pid)
    if gap and unit:
        n += round(gap / unit)
    agree = (gap % unit == 0 if unit else gap == 0) and (
        (n > 0) == (chore_cents > 0) and (n < 0) == (chore_cents < 0))
    key = (pid, payout["id"])
    if not agree and key not in _logged_mismatch:
        _logged_mismatch.add(key)
        log.warning("wallet summary: chore count and money disagree for "
                    "person %s since payout %s (%s chores, %s cents)",
                    pid, payout["id"], n, chore_cents)
    return n


def summary(conn, pid: int, launch_day: str, streak) -> dict:
    """What the wallet holds since the last payout (rewards.since_payout).
    The total is ``balance_cents``, the number the card shows."""
    ledger = fdb.ledger_for(conn, pid)
    n, _ = chores_earned(conn, pid, launch_day)
    bal = balance_cents(conn, pid, launch_day)
    out = rw.since_payout(ledger, chores_done=n, balance_cents=bal,
                          launch_day=launch_day, streak=streak)
    payouts = [r for r in ledger if r["kind"] == "payout"]
    if payouts and payouts[-1].get("chores_n") is None:
        out["chores"] = _legacy_count(conn, pid, launch_day, payouts[-1],
                                      out["chore_cents"])
    return out


def pay_out(conn, pid: int, today: dt.date) -> dict | None:
    """Pay out the whole balance. The read and the write are one IMMEDIATE
    transaction, so two taps at once (two requests, two connections) see
    one balance: the second waits, then finds $0 and pays nothing.

    The payout row keeps the chore count it paid for (``chores_n``), so the
    summary starts over from it, and the result carries the summary of what
    was paid (``summary``)."""
    launch = fdb.kv_get(conn, LAUNCH_KEY)
    if not launch:
        return None
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        paid_for = summary(conn, pid, launch["day"], None)
        bal = paid_for["total_cents"]
        if bal <= 0:
            conn.rollback()
            return None
        n, _ = chores_earned(conn, pid, launch["day"])
        cur = conn.execute(
            "INSERT INTO ledger(person_id, kind, cents, run_key, at_streak, day, "
            "created_at, note, chores_n) "
            "VALUES(?, 'payout', ?, NULL, NULL, ?, ?, 'Paid', ?)",
            (pid, bal, today.isoformat(), fdb._now_iso(), n))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": int(cur.lastrowid), "kind": "payout", "cents": bal,
            "day": today.isoformat(), "summary": paid_for}


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
    for p in fdb.list_people(conn, include_inactive=True):
        if not p["active"]:
            record_active_change(conn, p["id"], False, today)
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
            "WHERE COALESCE(o.earner, o.person_id) = ? AND o.kind = 'house' "
            "AND o.date >= ?",
            (pid, launch_day)):
        d = dt.date.fromisoformat(ds)
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        weeks[wk] = weeks.get(wk, 0) + int(cents)
    return {"weeks": [{"week_start": k, "earned_cents": v}
                      for k, v in sorted(weeks.items(), reverse=True)],
            "ledger": list(reversed(fdb.ledger_for(conn, pid)))}
