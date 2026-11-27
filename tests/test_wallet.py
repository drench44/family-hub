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


def _serve(conn, pid, n, *, done=True, cents=30, kind="house", cid=1,
           earning=1):
    ds = _day(n)
    fdb.replace_day_log(conn, ds, [{"chore_id": cid, "person_id": pid,
                                    "title": "Dishes", "icon": "", "rot": 0,
                                    "cents": cents, "kind": kind,
                                    "earning": earning}])
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


def test_a_done_row_pays_its_earner_not_its_streak_owner(conn):
    a = fdb.add_person(conn, "Ava", "#5BC9F0")
    b = fdb.add_person(conn, "Ben", "#F05B5B")
    _launch(conn, {a: 0, b: 0})
    ds = _day(0)
    fdb.replace_day_log(conn, ds, [{"chore_id": 1, "person_id": a, "title": "Dishes",
                                    "icon": "", "rot": 0, "cents": 30, "earner": b}])
    fdb.set_completion(conn, 1, ds, b)
    assert wallet.balance_cents(conn, b, LAUNCH.isoformat()) == 30
    assert wallet.balance_cents(conn, a, LAUNCH.isoformat()) == 0
    weeks = wallet.history(conn, b, LAUNCH.isoformat())["weeks"]
    assert sum(w["earned_cents"] for w in weeks) == 30


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


def test_two_payouts_at_once_pay_once(conn, tmp_path):
    """A double tap on Paid sends two requests on two connections; only one
    may record the payout."""
    import threading
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    results, start = [], threading.Barrier(2)

    def tap():
        c = fdb.connect(path)
        start.wait()
        results.append(wallet.pay_out(c, pid, LAUNCH))
        c.close()
    threads = [threading.Thread(target=tap) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(r is None for r in results) == [False, True]
    assert [r["cents"] for r in fdb.ledger_for(conn, pid) if r["kind"] == "payout"] == [30]


# --- Audit 2026-11-26: nobody earns a streak without house chores ---------

def _served_only(conn, n):
    fdb.mark_served(conn, _day(n))


def test_no_house_chores_ever_earns_no_streak(conn):
    kid = fdb.add_person(conn, "Ava", "#5BC9F0")
    parent = fdb.add_person(conn, "Mom", "#F0A05B")
    _launch(conn, {kid: 5})
    for n in range(120):
        _serve(conn, kid, n)
    run = _run(conn, parent, 119)
    assert (run.streak, run.shields, run.run_key) == (0, 0, None)
    assert wallet.award(conn, parent, run, SET,
                        LAUNCH + dt.timedelta(days=119)) == []


def test_personal_goals_only_earn_no_streak(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {})
    for n in range(120):
        _serve(conn, pid, n, kind="personal", cents=0)
    run = _run(conn, pid, 119)
    assert (run.streak, run.run_key) == (0, None)


def test_person_added_after_launch_starts_on_first_house_day(conn):
    kid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {kid: 5})
    for n in range(100):
        _served_only(conn, n)
    late = fdb.add_person(conn, "Bo", "#5BF07A")
    for n in range(100, 103):
        _serve(conn, late, n, cid=2)
    run = _run(conn, late, 102)
    assert run.streak == 3 and run.run_key == _day(100)


def test_switched_off_days_count_as_away(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 10})
    for n in range(5):
        _serve(conn, pid, n)
    wallet.record_active_change(conn, pid, False, LAUNCH + dt.timedelta(days=5))
    for n in range(5, 15):
        _served_only(conn, n)       # off: no rows on the wall
    wallet.record_active_change(conn, pid, True, LAUNCH + dt.timedelta(days=15))
    _serve(conn, pid, 15)
    run = _run(conn, pid, 15)
    assert run.streak == 16
    assert all(run.outcomes[_day(n)] == "away" for n in range(5, 15))


def test_switched_off_still_off_counts_as_away(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 10})
    _serve(conn, pid, 0)
    wallet.record_active_change(conn, pid, False, LAUNCH + dt.timedelta(days=1))
    for n in range(1, 40):
        _served_only(conn, n)
    assert _run(conn, pid, 39).streak == 11


def test_off_and_on_same_day_leaves_no_gap(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 10})
    day = LAUNCH + dt.timedelta(days=2)
    wallet.record_active_change(conn, pid, False, day)
    wallet.record_active_change(conn, pid, True, day)
    for n in range(4):
        _serve(conn, pid, n)
    assert _run(conn, pid, 3).streak == 14


def test_launch_starts_a_pause_for_people_already_switched_off(conn):
    on = fdb.add_person(conn, "Ava", "#5BC9F0")
    off = fdb.add_person(conn, "Bo", "#5BF07A")
    fdb.update_person(conn, off, active=0)
    _launch(conn, {on: 3, off: 8})
    for n in range(10):
        _served_only(conn, n)
    wallet.record_active_change(conn, off, True, LAUNCH + dt.timedelta(days=10))
    _serve(conn, off, 10, cid=2)
    assert _run(conn, off, 10).streak == 9


def test_edit_floor_only_with_the_wallet_on_and_a_pin(conn):
    from family_hub import parent_pin
    now = dt.datetime(2026, 12, 10, 8, 0)
    assert wallet.edit_floor(conn, now) is None            # never launched
    _launch(conn, {})
    assert wallet.edit_floor(conn, now) is None            # no PIN: open, as on the wall
    parent_pin.set_pin(conn, "2468", current=None)
    assert wallet.edit_floor(conn, now) == dt.date(2026, 12, 9)   # before 9:00
    assert wallet.edit_floor(conn, now.replace(hour=9, minute=5)) == \
        dt.date(2026, 12, 10)
    fdb.kv_set(conn, "rewards.settings", {"fix_cutoff": "10:00"})
    assert wallet.edit_floor(conn, now.replace(hour=9, minute=5)) == \
        dt.date(2026, 12, 9)
    fdb.seed_integration(conn, "rewards", "feature")
    fdb.set_integration_enabled(conn, "rewards", False)
    assert wallet.edit_floor(conn, now) is None            # switched off


# --- the wallet summary: since the last payout (owner, 2026-11-27) -----------

def _summary(conn, pid, streak=None):
    """The summary, checked against the balance the card shows."""
    s = wallet.summary(conn, pid, LAUNCH.isoformat(), streak)
    assert s["total_cents"] == wallet.balance_cents(conn, pid, LAUNCH.isoformat())
    assert s["chore_cents"] + s["bonus_cents"] == s["total_cents"]
    return s


def test_summary_never_paid_counts_from_launch(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    for n in range(3):
        _serve(conn, pid, n)
    _serve(conn, pid, 3, done=False)
    _serve(conn, pid, 4, kind="personal", cents=0, cid=5)   # a goal never counts
    s = _summary(conn, pid, streak=7)
    assert (s["since"], s["paid_before"]) == (LAUNCH.isoformat(), False)
    assert (s["chores"], s["chore_cents"], s["bonus_cents"]) == (3, 90, 0)
    assert s["streak"] == 7


def test_summary_paid_yesterday_starts_from_the_payout(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _serve(conn, pid, 1, cid=2)
    wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=1))
    _serve(conn, pid, 2, cid=3)
    s = _summary(conn, pid)
    assert (s["since"], s["paid_before"]) == (_day(1), True)
    assert (s["chores"], s["chore_cents"], s["total_cents"]) == (1, 30, 30)


def test_summary_payout_today_then_more_chores_today(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    ds = _day(0)
    fdb.replace_day_log(conn, ds, [
        {"chore_id": c, "person_id": pid, "title": "x", "icon": "", "rot": 0,
         "cents": 30, "kind": "house"} for c in (1, 2)])
    fdb.mark_served(conn, ds)
    fdb.set_completion(conn, 1, ds, pid)
    wallet.pay_out(conn, pid, LAUNCH)
    s = _summary(conn, pid)
    assert (s["since"], s["chores"], s["total_cents"]) == (ds, 0, 0)
    fdb.set_completion(conn, 2, ds, pid)      # done after the tap on Paid
    s = _summary(conn, pid)
    assert (s["since"], s["chores"], s["total_cents"]) == (ds, 1, 30)


def test_summary_counts_a_bonus_inside_the_span_only(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    custom = {**SET, "milestones": [[3, 500]]}
    _launch(conn, {pid: 0}, custom)
    for n in range(3):
        _serve(conn, pid, n, cid=n + 1)
    wallet.award(conn, pid, _run(conn, pid, 2, custom), custom,
                 LAUNCH + dt.timedelta(days=2))
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"], s["bonus_cents"], s["total_cents"]) == \
        (3, 90, 500, 590)
    wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=2))
    _serve(conn, pid, 3, cid=4)
    s = _summary(conn, pid)               # the bonus was paid with the rest
    assert (s["chores"], s["chore_cents"], s["bonus_cents"]) == (1, 30, 0)


def test_summary_old_day_fix_after_a_payout(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _serve(conn, pid, 1, done=False, cid=2)
    wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=2))
    fdb.set_completion(conn, 2, _day(1), pid)      # a parent fixes an old day
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"]) == (1, 30)
    fdb.clear_completion(conn, 1, _day(0))          # and takes a paid one back
    fdb.clear_completion(conn, 2, _day(1))
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"], s["total_cents"]) == (-1, -30, -30)


def test_summary_pays_the_backup_who_did_it(conn):
    a = fdb.add_person(conn, "Ava", "#5BC9F0")
    b = fdb.add_person(conn, "Ben", "#F05B5B")
    _launch(conn, {a: 0, b: 0})
    ds = _day(0)
    fdb.replace_day_log(conn, ds, [{"chore_id": 1, "person_id": a, "title": "Dishes",
                                    "icon": "", "rot": 0, "cents": 30, "earner": b}])
    fdb.set_completion(conn, 1, ds, b)
    assert _summary(conn, b)["chores"] == 1 and _summary(conn, b)["chore_cents"] == 30
    assert _summary(conn, a)["chores"] == 0 and _summary(conn, a)["total_cents"] == 0


def test_payout_says_what_it_paid_for(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _serve(conn, pid, 1, cid=2)
    row = wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=1))
    assert row["summary"]["since"] == LAUNCH.isoformat()
    assert (row["summary"]["chores"], row["summary"]["total_cents"]) == (2, 60)
    assert row["summary"]["total_cents"] == row["cents"]
    assert [r["chores_n"] for r in fdb.ledger_for(conn, pid)] == [2]


def test_summary_after_an_old_style_payout_counts_by_time(conn):
    """A payout written before payouts kept their chore count."""
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    conn.execute("UPDATE completions SET done_at = '2020-01-01T10:00:00+00:00'")
    fdb.add_ledger(conn, person_id=pid, kind="payout", cents=30, day=_day(0))
    conn.execute("UPDATE ledger SET created_at = '2020-01-01T12:00:00+00:00'")
    conn.commit()
    _serve(conn, pid, 1, cid=2)                     # done now, after the payout
    s = _summary(conn, pid)
    assert (s["chores"], s["total_cents"]) == (1, 30)


def _old_style_payout(conn, pid, day, at):
    """A payout as written before payouts kept their chore count."""
    fdb.add_ledger(conn, person_id=pid, kind="payout",
                   cents=wallet.balance_cents(conn, pid, LAUNCH.isoformat()), day=day)
    conn.execute("UPDATE ledger SET created_at = ? WHERE kind = 'payout'", (at,))
    conn.commit()


def _done_at(conn, cid, ds, at):
    conn.execute("UPDATE completions SET done_at = ? WHERE chore_id = ? AND date = ?",
                 (at, cid, ds))
    conn.commit()


def test_old_style_payout_time_compare_is_by_instant_not_text(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _done_at(conn, 1, _day(0), "2020-01-01T12:00:00+00:00")
    # isoformat drops the fraction on a whole second: "12:00:00+00:00"
    _old_style_payout(conn, pid, _day(0), "2020-01-01T12:00:00+00:00")
    _serve(conn, pid, 1, cid=2)
    _done_at(conn, 2, _day(1), "2020-01-01T12:00:00+00:00")   # the same instant
    _serve(conn, pid, 2, cid=3)
    _done_at(conn, 3, _day(2), "2020-01-01T12:00:00.000001+00:00")
    _serve(conn, pid, 3, cid=4)
    _done_at(conn, 4, _day(3), "2020-01-01T13:00:00+01:00")   # same instant, other zone
    # by instant: only the one a microsecond later is after the payout
    assert wallet._ticked_after(conn, pid, LAUNCH.isoformat(),
                                "2020-01-01T12:00:00+00:00") == (1, 30)
    s = _summary(conn, pid)
    # money: three ticks after the payout's balance was read (90); the same
    # instant ones are not after it, but the money says they were not paid
    # either, so the count follows the money
    assert (s["chore_cents"], s["chores"]) == (90, 3)


def test_old_style_payout_untick_counts_as_taken_back(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _done_at(conn, 1, _day(0), "2020-01-01T10:00:00+00:00")
    _old_style_payout(conn, pid, _day(0), "2020-01-01T12:00:00+00:00")
    fdb.clear_completion(conn, 1, _day(0))
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"]) == (-1, -30)      # never "0 chores, -30¢"


def test_old_style_payout_reticked_paid_chore_does_not_count_again(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _done_at(conn, 1, _day(0), "2020-01-01T10:00:00+00:00")
    _old_style_payout(conn, pid, _day(0), "2020-01-01T12:00:00+00:00")
    fdb.clear_completion(conn, 1, _day(0))
    fdb.set_completion(conn, 1, _day(0), pid)                 # ticked again: done_at is now
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"]) == (0, 0)          # never "1 chore, 0¢"


def test_old_style_payout_logs_once_when_count_and_money_disagree(conn, caplog):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0)
    _done_at(conn, 1, _day(0), "2020-01-01T10:00:00+00:00")
    _old_style_payout(conn, pid, _day(0), "2020-01-01T12:00:00+00:00")
    # money that no whole number of chores explains: a bonus row booked as
    # neither (a hand edit) leaves 7 cents the count can't follow
    conn.execute("UPDATE ledger SET cents = cents - 7 WHERE kind = 'payout'")
    conn.commit()
    wallet._logged_mismatch.clear()
    with caplog.at_level("WARNING"):
        _summary(conn, pid)
        _summary(conn, pid)
    hits = [r for r in caplog.records if "disagree" in r.getMessage()]
    assert len(hits) == 1


def test_two_payouts_on_the_same_day(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    ds = _day(0)
    fdb.replace_day_log(conn, ds, [
        {"chore_id": c, "person_id": pid, "title": "x", "icon": "", "rot": 0,
         "cents": 30, "kind": "house"} for c in (1, 2, 3, 4)])
    fdb.mark_served(conn, ds)
    fdb.set_completion(conn, 1, ds, pid)
    fdb.set_completion(conn, 2, ds, pid)
    first = wallet.pay_out(conn, pid, LAUNCH)
    assert (first["summary"]["chores"], first["cents"]) == (2, 60)
    fdb.set_completion(conn, 3, ds, pid)
    second = wallet.pay_out(conn, pid, LAUNCH)
    assert (second["summary"]["chores"], second["cents"]) == (1, 30)
    assert second["summary"]["since"] == ds and second["summary"]["paid_before"]
    fdb.set_completion(conn, 4, ds, pid)
    s = _summary(conn, pid)
    assert (s["since"], s["chores"], s["chore_cents"], s["bonus_cents"]) == (ds, 1, 30, 0)


def test_summary_bonus_on_payout_day_before_the_payout_is_left_out(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    custom = {**SET, "milestones": [[1, 500]]}
    _launch(conn, {pid: 0}, custom)
    _serve(conn, pid, 0)
    wallet.award(conn, pid, _run(conn, pid, 0, custom), custom, LAUNCH)
    wallet.pay_out(conn, pid, LAUNCH)        # same day, after the bonus
    s = _summary(conn, pid)
    assert (s["since"], s["bonus_cents"], s["total_cents"]) == (_day(0), 0, 0)


def test_summary_person_added_after_launch_counts_from_launch(conn):
    _launch(conn, {})
    pid = fdb.add_person(conn, "Cal", "#5BF08A")
    _serve(conn, pid, 5)
    s = _summary(conn, pid)
    assert (s["since"], s["paid_before"], s["chores"]) == (LAUNCH.isoformat(), False, 1)


# --- "Earns money" off: streak only -----------------------------------------

def test_a_streak_only_row_counts_for_the_streak_but_not_the_wallet(conn):
    """A row frozen while its person earned nothing (``earning`` 0) is a
    house chore for the streak, and nothing at all for the money: not in the
    balance, the chore count, the weekly history or the summary."""
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0, cid=1)                          # earned 30
    _serve(conn, pid, 1, cid=2, cents=0, earning=0)      # streak only
    _serve(conn, pid, 2, cid=3, cents=0, earning=0)
    assert _run(conn, pid, 2).streak == 3
    assert wallet.chores_earned(conn, pid, LAUNCH.isoformat()) == (1, 30)
    assert wallet.balance_cents(conn, pid, LAUNCH.isoformat()) == 30
    weeks = wallet.history(conn, pid, LAUNCH.isoformat())["weeks"]
    assert sum(w["earned_cents"] for w in weeks) == 30
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"]) == (1, 30)


def test_a_payout_after_streak_only_days_starts_the_count_right(conn):
    """chores_n on a payout uses the same rule, so chores done while not
    earning never show up later as extra chores or chores taken back."""
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 0})
    _serve(conn, pid, 0, cid=1)
    _serve(conn, pid, 1, cid=2, cents=0, earning=0)
    paid = wallet.pay_out(conn, pid, LAUNCH + dt.timedelta(days=1))
    assert paid["cents"] == 30 and paid["summary"]["chores"] == 1
    _serve(conn, pid, 2, cid=3)
    s = _summary(conn, pid)
    assert (s["chores"], s["chore_cents"]) == (1, 30)


def test_award_pays_nothing_to_someone_not_earning(conn):
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 99})
    _serve(conn, pid, 0, cents=0, earning=0)
    run = _run(conn, pid, 0)
    assert run.streak == 100
    fdb.set_person_earns(conn, pid, False)
    assert wallet.award(conn, pid, run, SET, LAUNCH) == []
    assert fdb.ledger_for(conn, pid) == []


def test_turning_earning_back_on_never_pays_a_milestone_passed_while_off(conn):
    """The run reached 100 while the person earned nothing. Turning earning
    back on records the run as already past it, so only milestones still
    ahead pay (the next one at 200)."""
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 99})
    fdb.set_person_earns(conn, pid, False)
    _serve(conn, pid, 0, cents=0, earning=0)
    run = _run(conn, pid, 0)
    assert run.streak == 100
    fdb.set_person_earns(conn, pid, True)
    wallet.record_earning_on(conn, pid, run)
    assert wallet.award(conn, pid, run, SET, LAUNCH) == []
    # a floor already further along the same run is never lowered
    fdb.kv_set(conn, wallet.MS_FLOOR_KEY, {str(pid): [run.run_key, 150]})
    wallet.record_earning_on(conn, pid, run)
    assert fdb.kv_get(conn, wallet.MS_FLOOR_KEY)[str(pid)] == [run.run_key, 150]


def test_launch_pays_no_seed_bonus_to_someone_not_earning(conn):
    a = fdb.add_person(conn, "Ava", "#5BC9F0")
    b = fdb.add_person(conn, "Ben", "#F05B5B")
    fdb.set_person_earns(conn, b, False)
    custom = {**SET, "milestones": [[25, 1000], [100, 10000]]}
    _launch(conn, {a: 30, b: 30}, custom)
    assert [r["at_streak"] for r in fdb.ledger_for(conn, a)] == [25]
    assert fdb.ledger_for(conn, b) == []


def test_a_break_and_rebuild_to_100_pays_despite_an_old_floor(conn):
    """The floor from switching back on belongs to that run. After a break
    the new run starts clean, and its 100 pays."""
    pid = fdb.add_person(conn, "Ava", "#5BC9F0")
    _launch(conn, {pid: 99})
    _serve(conn, pid, 0)
    run = _run(conn, pid, 0)
    assert run.streak == 100
    wallet.record_earning_on(conn, pid, run)          # floor at 100 on the seed run
    _serve(conn, pid, 1, done=False)                  # missed: the run ends
    for n in range(2, 102):
        _serve(conn, pid, n)
    run = _run(conn, pid, 101)
    assert run.streak == 100 and run.run_key == _day(2)
    paid = wallet.award(conn, pid, run, SET, LAUNCH + dt.timedelta(days=101))
    assert [(r["at_streak"], r["cents"]) for r in paid] == [(100, 10000)]
