"""The chore wallet's rules (rewards.py): calendar-day streaks, shields,
milestone bonuses, pay levels and cost projections. Pure, no I/O."""
import datetime as dt

from family_hub import rewards as rw

D0 = dt.date(2026, 12, 1)


def _days(n, start=D0):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _hist(spec, start=D0, kind="house"):
    """spec: a string, one char per day from ``start``:
    'k' a chore that was done, 'm' a chore left undone, 'r' nothing assigned,
    'a' away, 'u' a day the wall never served.
    Returns (rows_by_date, done, away, served, today)."""
    rows, done, away, served = {}, set(), set(), set()
    for i, ch in enumerate(spec):
        ds = (start + dt.timedelta(days=i)).isoformat()
        if ch == "u":
            continue
        served.add(ds)
        if ch == "a":
            away.add(ds)
            continue
        if ch in "km":
            rows[ds] = [{"chore_id": 1, "kind": kind}]
            if ch == "k":
                done.add((1, ds))
    today = start + dt.timedelta(days=len(spec) - 1)
    return rows, done, away, served, today


def _run(spec, **kw):
    rows, done, away, served, today = _hist(spec)
    return rw.run_state(rows, done, start=D0, today=today, away=away,
                        served=served, **kw)


def test_rest_days_count_as_kept():
    # a Friday-and-Sunday child: chores two days a week, all done
    spec = "".join("k" if (D0 + dt.timedelta(days=i)).weekday() in (4, 6) else "r"
                   for i in range(14))
    assert _run(spec).streak == 14


def test_today_unfinished_is_open_not_missed():
    r = _run("kkkm")
    assert r.streak == 3
    assert r.outcomes[(D0 + dt.timedelta(days=3)).isoformat()] == "open"


def test_rest_day_today_counts_now():
    assert _run("kkkr").streak == 4


def test_one_miss_spends_a_shield():
    r = _run("k" * 20 + "m" + "k" * 5)
    assert r.streak == 25 and r.shields == 0
    assert r.outcomes[(D0 + dt.timedelta(days=20)).isoformat()] == "saved"


def test_shields_cap_at_two():
    assert _run("k" * 60).shields == 2


def test_shields_off_means_a_miss_resets():
    r = _run("k" * 20 + "mk", shields_on=False)
    assert r.streak == 1 and r.shields == 0


def test_miss_without_shield_resets():
    r = _run("kkkkkmr")
    # the miss ends the run; the rest day after it starts a new one
    assert r.streak == 1
    r2 = _run("kkkkkm" + "k")
    assert r2.streak == 1 and r2.run_key == (D0 + dt.timedelta(days=6)).isoformat()


def test_away_and_unserved_are_neutral():
    r = _run("kaaauk")
    assert r.streak == 2
    assert r.outcomes[(D0 + dt.timedelta(days=1)).isoformat()] == "away"
    assert r.outcomes[(D0 + dt.timedelta(days=4)).isoformat()] == "unserved"


def test_personal_and_once_rows_never_count():
    ds = D0.isoformat()
    rows = {ds: [{"chore_id": 1, "kind": "house"},
                 {"chore_id": 2, "kind": "personal"},
                 {"chore_id": 3, "kind": "once"}]}
    r = rw.run_state(rows, {(1, ds)}, start=D0, today=D0, away=set(),
                     served={ds})
    assert r.streak == 1


def test_seed_carries_and_keys_the_run():
    r = _run("kkkk", seed=71, seed_key="seed:2026-12-01")
    assert r.streak == 75 and r.run_key == "seed:2026-12-01"
    # a miss with no shield ends the seeded run for good
    r = _run("km" + "k", seed=71, seed_key="seed:2026-12-01")
    assert r.streak == 1 and r.run_key != "seed:2026-12-01"


def test_no_lookback_cap():
    assert _run("k" * 400).streak == 400


def test_run_key_is_the_first_kept_day():
    r = _run("kkk")
    assert r.run_key == D0.isoformat()


HUNDRED = [[100, 10000]]


def test_milestones_default_hundred_repeats():
    assert rw.milestones_crossed(99, 100, HUNDRED) == [(100, 10000)]
    assert rw.milestones_crossed(150, 201, HUNDRED) == [(200, 10000)]
    assert rw.milestones_crossed(0, 0, HUNDRED) == []
    assert rw.milestones_crossed(10, 99, HUNDRED) == []
    assert rw.milestones_crossed(10, 500, []) == []


def test_milestones_custom_list_repeats():
    ms = [[25, 1000], [100, 6500]]
    assert rw.milestones_crossed(99, 126, ms) == [(100, 6500), (125, 1000)]
    assert rw.milestones_crossed(0, 25, ms) == [(25, 1000)]


SET = {"cents_per_chore": 30, "bonus_on": True, "milestones": HUNDRED,
       "levels_on": False, "levels": [[25, 35], [50, 40], [100, 45]]}


def test_rate_for_levels():
    assert [rw.rate_for(s, SET) for s in (0, 24, 25, 71, 100, 300)] == [30] * 6
    on = {**SET, "levels_on": True}
    assert [rw.rate_for(s, on) for s in (0, 24, 25, 71, 100, 300)] == \
        [30, 30, 35, 40, 45, 45]


def test_next_level():
    on = {**SET, "levels_on": True}
    assert rw.next_level(10, on) == (25, 35)
    assert rw.next_level(71, on) == (100, 45)
    assert rw.next_level(100, on) is None
    assert rw.next_level(10, SET) is None


def test_next_milestone_and_goal():
    assert rw.next_milestone(71, HUNDRED) == (100, 10000)
    assert rw.next_milestone(100, HUNDRED) == (200, 10000)
    assert rw.next_milestone(5, []) is None
    assert rw.goal_for(71, SET) == 100
    assert rw.goal_for(100, SET) == 200
    levels_only = {**SET, "bonus_on": False, "levels_on": True}
    assert rw.goal_for(47, levels_only) == 50
    assert rw.goal_for(120, levels_only) == 200      # past the top level
    neither = {**SET, "bonus_on": False}
    assert rw.goal_for(47, neither) == 100


def test_project_perfect_and_usual():
    kids = [{"id": 1, "chores_per_week": 36, "done_share": 1.0, "streak": 10},
            {"id": 2, "chores_per_week": 3, "done_share": 0.9, "streak": 10}]
    p = rw.project(kids, SET)
    a, b = p["children"]
    # perfect: every chore at the base rate plus $100 every 100 days
    assert a["perfect"]["week"] == 36 * 30 + 10000 * 7 // 100      # 1780
    assert a["perfect"]["month"] == round(1780 * 52 / 12)
    assert a["perfect"]["year"] == 1780 * 52
    # usual: the share actually done, and the bonus scaled by a clean week
    assert b["usual"]["week"] == round(3 * 0.9 * 30) + round(700 * 0.9 ** 7)
    assert p["total"]["perfect"]["week"] == a["perfect"]["week"] + b["perfect"]["week"]
    assert p["total"]["usual"]["year"] == a["usual"]["year"] + b["usual"]["year"]


def test_project_uses_top_level_when_levels_on():
    on = {**SET, "bonus_on": False, "levels_on": True}
    p = rw.project([{"id": 1, "chores_per_week": 10, "done_share": 1.0,
                     "streak": 30}], on)
    assert p["children"][0]["perfect"]["week"] == 10 * 45
    assert p["children"][0]["usual"]["week"] == 10 * 35


def test_format_cents():
    assert rw.format_cents(1850) == "$18.50"
    assert rw.format_cents(30) == "30¢"
    assert rw.format_cents(100) == "$1.00"
    assert rw.format_cents(0) == "$0.00"
    assert rw.format_cents(-250) == "-$2.50"
