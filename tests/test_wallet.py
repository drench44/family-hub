"""The chore wallet's money: balances, streak bonuses, payouts, launch."""
import datetime as dt

from family_hub import db as fdb
from family_hub import wallet

LAUNCH = dt.date(2026, 12, 1)
SET = {"cents_per_chore": 30, "bonus_on": True, "milestones": [[100, 10000]],
       "levels_on": False, "levels": [[25, 35], [50, 40], [100, 45]],
       "shields_on": True, "shield_every": 20, "shield_max": 2}


def _day(n):
    return (LAUNCH + dt.timedelta(days=n)).isoformat()


def _serve(conn, pid, n, *, done=True, cents=30, kind="house", cid=1):
    ds = _day(n)
    fdb.replace_day_log(conn, ds, [{"chore_id": cid, "person_id": pid,
                                    "title": "Dishes", "icon": "", "rot": 0,
                                    "cents": cents, "kind": kind}])
    fdb.mark_served(conn, ds)
    if done:
        fdb.set_completion(conn, cid, ds, pid)


def _launch(conn, seeds, settings=SET):
    return wallet.launch(conn, LAUNCH, settings, seeds)


def _run(conn, pid, n, settings=SET):
    launch = fdb.kv_get(conn, "rewards.launch")
    return wallet.person_run(conn, pid, LAUNCH + dt.timedelta(days=n),
                             settings, launch, set())


def test_launch_pays_passed_milestones_once(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    custom = {**SET, "milestones": [[25, 1000], [50, 2500], [100, 10000]]}
    rec = _launch(conn, {pid: 71}, custom)
    assert rec == {"day": LAUNCH.isoformat(), "seeds": {str(pid): 71}}
    assert [(r["at_streak"], r["cents"]) for r in fdb.ledger_for(conn, pid)] == \
        [(25, 1000), (50, 2500)]
    _launch(conn, {pid: 90}, custom)          # second call: no change
    assert len(fdb.ledger_for(conn, pid)) == 2
    assert fdb.kv_get(conn, "rewards.launch")["seeds"] == {str(pid): 71}


def test_launch_with_default_milestone_pays_nothing(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 71})
    assert fdb.ledger_for(conn, pid) == []


def test_seeded_run_pays_100_when_it_gets_there(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 71})
    for n in range(29):
        _serve(conn, pid, n)
    run = _run(conn, pid, 28)
    assert run.streak == 100 and run.run_key == "seed:" + LAUNCH.isoformat()
    paid = wallet.award(conn, pid, run, SET, LAUNCH + dt.timedelta(days=28))
    assert [(r["at_streak"], r["cents"]) for r in paid] == [(100, 10000)]
    assert wallet.award(conn, pid, run, SET, LAUNCH + dt.timedelta(days=28)) == []


def test_award_pays_on_crossing_and_never_twice(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    custom = {**SET, "milestones": [[25, 1000], [100, 10000]]}
    _launch(conn, {pid: 0}, custom)
    for n in range(25):
        _serve(conn, pid, n)
    run = _run(conn, pid, 24, custom)
    assert run.streak == 25
    assert len(wallet.award(conn, pid, run, custom, LAUNCH + dt.timedelta(days=24))) == 1
    assert wallet.award(conn, pid, run, custom, LAUNCH + dt.timedelta(days=24)) == []


def test_untick_keeps_paid_bonus(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    custom = {**SET, "milestones": [[25, 1000], [100, 10000]]}
    _launch(conn, {pid: 0}, custom)
    for n in range(25):
        _serve(conn, pid, n)
    today = LAUNCH + dt.timedelta(days=24)
    wallet.award(conn, pid, _run(conn, pid, 24, custom), custom, today)
    fdb.clear_completion(conn, 1, _day(24))
    run = _run(conn, pid, 24, custom)
    assert run.streak == 24                          # today is open again
    assert wallet.award(conn, pid, run, custom, today) == []
    fdb.set_completion(conn, 1, _day(24), pid)
    assert wallet.award(conn, pid, _run(conn, pid, 24, custom), custom, today) == []
    assert [r["kind"] for r in fdb.ledger_for(conn, pid)] == ["bonus"]


def test_balance_counts_only_house_non_once_after_launch(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    # a row from before launch never pays
    fdb.replace_day_log(conn, "2026-11-30", [{"chore_id": 9, "person_id": pid,
                        "title": "x", "icon": "", "rot": 0, "cents": 30}])
    fdb.set_completion(conn, 9, "2026-11-30", pid)
    _serve(conn, pid, 0, cid=1)
    _serve(conn, pid, 1, cid=2, kind="personal", cents=0)
    _serve(conn, pid, 2, cid=3, kind="once", cents=0)
    _serve(conn, pid, 3, cid=4, done=False)
    assert wallet.balance_cents(conn, pid, LAUNCH.isoformat()) == 30


def test_rate_change_does_not_rewrite_past(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0, cents=30)
    _serve(conn, pid, 1, cents=50, cid=2)
    assert wallet.balance_cents(conn, pid, LAUNCH.isoformat()) == 80


def test_levels_freeze_rate_per_day(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    on = {**SET, "levels_on": True}
    _launch(conn, {pid: 23}, on)
    launch = fdb.kv_get(conn, "rewards.launch")
    # streak 23 at the end of yesterday's... day 0 is the first served day
    assert wallet.cents_for_today(conn, pid, LAUNCH, on, launch, set()) == 30
    _serve(conn, pid, 0)
    _serve(conn, pid, 1)              # streak 25 by the end of day 1
    # day 1's own tick does not raise day 1's rate; day 2 pays the new level
    assert wallet.cents_for_today(conn, pid, LAUNCH + dt.timedelta(days=1),
                                  on, launch, set()) == 30
    assert wallet.cents_for_today(conn, pid, LAUNCH + dt.timedelta(days=2),
                                  on, launch, set()) == 35


def test_payout_zeroes_and_records(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    assert wallet.pay_out(conn, pid, LAUNCH) is None
    _serve(conn, pid, 0)
    _serve(conn, pid, 1, cid=2)
    row = wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=1))
    assert row["kind"] == "payout" and row["cents"] == 60
    assert wallet.balance_cents(conn, pid, LAUNCH.isoformat()) == 0


def test_balance_can_go_negative_after_edit(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    wallet.pay_out(conn, pid, LAUNCH)
    fdb.clear_completion(conn, 1, _day(0))
    assert wallet.balance_cents(conn, pid, LAUNCH.isoformat()) == -30


def test_pace_from_last_8_weeks(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    # 56 days, two chores a day, the second one done every other day
    for n in range(56):
        ds = _day(n)
        fdb.replace_day_log(conn, ds, [
            {"chore_id": 1, "person_id": pid, "title": "a", "icon": "", "rot": 0,
             "cents": 30},
            {"chore_id": 2, "person_id": pid, "title": "b", "icon": "", "rot": 0,
             "cents": 30},
            {"chore_id": 3, "person_id": pid, "title": "goal", "icon": "",
             "rot": 0, "kind": "personal"}])
        fdb.set_completion(conn, 1, ds, pid)
        if n % 2 == 0:
            fdb.set_completion(conn, 2, ds, pid)
    p = wallet.pace(conn, pid, LAUNCH + dt.timedelta(days=56))
    assert p == {"chores_per_week": 14.0, "done_share": 0.75}
    # nothing on record yet: no chores, a perfect share
    other = fdb.add_person(conn, "Ben", "#5BC9F0")
    assert wallet.pace(conn, other, LAUNCH) == {"chores_per_week": 0.0,
                                                 "done_share": 1.0}


def test_history_weeks_and_ledger(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)                  # Tue 2026-12-01
    _serve(conn, pid, 6, cid=2)           # Mon 2026-12-07
    wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=6))
    h = wallet.history(conn, pid, LAUNCH.isoformat())
    assert h["weeks"] == [{"week_start": "2026-12-07", "earned_cents": 30},
                          {"week_start": "2026-11-30", "earned_cents": 30}]
    assert [r["kind"] for r in h["ledger"]] == ["payout"]
