import datetime as dt

from family_hub import chores as ch


def C(**kw):  # chore factory with defaults
    d = dict(id=1, title="t", icon="", schedule_kind="daily", days_mask=0,
             assign_kind="fixed", fixed_person_id=1, rotation_order=[],
             rotation_epoch="2026-08-03", sort=0, active=1)
    d.update(kw)
    return d


def P(pid, name="p", color="#5BC9F0"):
    return {"id": pid, "name": name, "color": color, "sort": 0, "active": 1}


def test_occurs_daily_and_days_mask():
    mon, sun = dt.date(2026, 8, 10), dt.date(2026, 8, 16)
    assert ch.occurs(C(), mon)
    weekly = C(schedule_kind="days", days_mask=0b1000001)  # Mon+Sun
    assert ch.occurs(weekly, mon) and ch.occurs(weekly, sun)
    assert not ch.occurs(weekly, dt.date(2026, 8, 11))     # Tuesday
    assert not ch.occurs(C(), dt.date(2026, 8, 1))         # before epoch
    assert not ch.occurs(C(active=0), mon)


def test_occurs_once_only_on_its_date():
    due = dt.date(2026, 8, 20)
    once = C(schedule_kind="once", rotation_epoch="2026-08-20")
    assert ch.occurs(once, due)
    assert not ch.occurs(once, due - dt.timedelta(days=1))   # before: shared epoch guard
    # the day AFTER is what pins the new once-branch (d == epoch), since the
    # before-date case is already caught by the generic d < epoch guard
    assert not ch.occurs(once, due + dt.timedelta(days=1))
    assert not ch.occurs(C(schedule_kind="once", rotation_epoch="2026-08-20",
                           active=0), due)                    # inactive never occurs


def test_occurrences_before_daily_closed_form():
    daily = C(rotation_epoch="2026-08-03")
    assert ch.occurrences_before(daily, dt.date(2026, 8, 3)) == 0
    assert ch.occurrences_before(daily, dt.date(2026, 8, 4)) == 1
    assert ch.occurrences_before(daily, dt.date(2026, 8, 13)) == 10
    # dates at/before the epoch never see occurrences
    assert ch.occurrences_before(daily, dt.date(2026, 8, 1)) == 0


def test_occurrences_before_days_mask_matches_brute_force():
    # Mon+Wed+Sat over a multi-year span, epoch mid-week
    weekly = C(schedule_kind="days", days_mask=0b0100101,
               rotation_epoch="2024-08-15")
    for probe in (dt.date(2024, 8, 15), dt.date(2024, 8, 16),
                  dt.date(2024, 9, 1), dt.date(2025, 3, 7),
                  dt.date(2026, 8, 15)):
        brute = 0
        d = dt.date(2024, 8, 15)
        while d < probe:
            if ch.occurs(weekly, d):
                brute += 1
            d += dt.timedelta(days=1)
        assert ch.occurrences_before(weekly, probe) == brute


def test_rotation_is_deterministic_and_skips_nonoccurring_days():
    rot = C(assign_kind="rotation", rotation_order=[7, 8, 9],
            schedule_kind="days", days_mask=0b0000001)     # Mondays only
    # epoch Mon 2026-08-03: 8/3->7, 8/10->8, 8/17->9, 8/24->7
    assert ch.assignee_id(rot, dt.date(2026, 8, 3)) == 7
    assert ch.assignee_id(rot, dt.date(2026, 8, 10)) == 8
    assert ch.assignee_id(rot, dt.date(2026, 8, 24)) == 7
    assert ch.assignee_id(C(assign_kind="rotation", rotation_order=[]),
                          dt.date(2026, 8, 10)) is None


def test_rotation_skips_inactive_members():
    rot = C(assign_kind="rotation", rotation_order=[7, 8, 9],
            rotation_epoch="2026-08-03")  # daily
    # with 8 inactive the effective order is [7, 9]
    active = {7, 9}
    assert ch.assignee_id(rot, dt.date(2026, 8, 3), active) == 7
    assert ch.assignee_id(rot, dt.date(2026, 8, 4), active) == 9
    assert ch.assignee_id(rot, dt.date(2026, 8, 5), active) == 7
    # nobody in the rotation is active -> unresolvable
    assert ch.assignee_id(rot, dt.date(2026, 8, 3), {1, 2}) is None
    # no filter provided -> full order (pure callers/tests)
    assert ch.assignee_id(rot, dt.date(2026, 8, 4)) == 8


def test_fixed_assignee_returns_person():
    assert ch.assignee_id(C(fixed_person_id=5), dt.date(2026, 8, 10)) == 5


def test_plan_rows_flat_shape_and_omissions():
    people = [P(1, "A"), P(2, "B")]
    fixed_a = C(id=10, title="Dishes", icon="🍽️", fixed_person_id=1,
                rotation_epoch="2026-08-01")
    fixed_gone = C(id=11, fixed_person_id=99, rotation_epoch="2026-08-01")
    rot = C(id=12, title="Feed cat", assign_kind="rotation",
            rotation_order=[1, 2], rotation_epoch="2026-08-12")
    inactive = C(id=13, active=0, fixed_person_id=1, rotation_epoch="2026-08-01")
    rows = ch.plan_rows([fixed_a, fixed_gone, rot, inactive], people,
                        dt.date(2026, 8, 12))
    assert rows == [
        {"chore_id": 10, "person_id": 1, "title": "Dishes", "icon": "🍽️",
         "rot": 0},
        {"chore_id": 12, "person_id": 1, "title": "Feed cat", "icon": "",
         "rot": 1},
    ]


def test_plan_rows_rotation_skips_inactive_person():
    # person 2 is not in the active people list -> their turns go to person 1
    people = [P(1, "A")]
    rot = C(id=12, assign_kind="rotation", rotation_order=[1, 2],
            rotation_epoch="2026-08-12")
    for d in (dt.date(2026, 8, 12), dt.date(2026, 8, 13)):
        rows = ch.plan_rows([rot], people, d)
        assert [r["person_id"] for r in rows] == [1]


def test_day_plan_shape_done_flags_and_rotation():
    people = [P(1, "A", "#111111"), P(2, "B", "#222222")]
    rows = [
        {"chore_id": 10, "person_id": 1, "title": "Dishes", "icon": "🍽️",
         "rot": 0},
        {"chore_id": 12, "person_id": 1, "title": "Feed cat", "icon": "",
         "rot": 1},
        {"chore_id": 11, "person_id": 2, "title": "Trash", "icon": "", "rot": 0},
    ]
    plan = ch.day_plan(rows, people, {10})

    p1 = plan[0]
    assert p1["person"] == {"id": 1, "name": "A", "color": "#111111"}
    assert [(c["id"], c["done"], c["rot"]) for c in p1["chores"]] == \
        [(10, True, False), (12, False, True)]
    assert (p1["done_count"], p1["total"]) == (1, 2)

    p2 = plan[1]
    assert [(c["id"], c["done"]) for c in p2["chores"]] == [(11, False)]
    assert (p2["done_count"], p2["total"]) == (0, 1)

    # rows for people not in the list are dropped; no people -> empty plan
    assert ch.day_plan(rows, [], set()) == []


def test_streak_skips_rest_days_and_forgives_unfinished_today():
    today = dt.date(2026, 8, 13)                           # Thursday
    # occurred Mon+Wed, both completed -> 2
    occ = {"2026-08-10": {1}, "2026-08-12": {1}}
    assert ch.streak(occ, {"2026-08-10": {1}, "2026-08-12": {1}}, today) == 2
    # Mon missed -> stops at 1
    assert ch.streak(occ, {"2026-08-12": {1}}, today) == 1
    # unfinished today is forgiven, doesn't break or count
    occ_daily = {d: {2} for d in ("2026-08-10", "2026-08-11", "2026-08-12",
                                  "2026-08-13")}
    done = {d: {2} for d in ("2026-08-10", "2026-08-11", "2026-08-12")}
    assert ch.streak(occ_daily, done, today) == 3


def test_streak_zero_when_no_history_and_counts_today_when_done():
    today = dt.date(2026, 8, 13)
    assert ch.streak({today.isoformat(): {1}}, {}, today) == 0
    assert ch.streak({today.isoformat(): {3}}, {today.isoformat(): {3}},
                     today) == 1


def test_week_strip_composition():
    today = dt.date(2026, 8, 13)   # Thursday; window 8/7 Fri .. 8/13 Thu
    occ = {
        "2026-08-07": {1, 2},   # Fri
        "2026-08-09": {1, 2},   # Sun
        "2026-08-12": {1, 2},   # Wed
        "2026-08-13": {1, 2},   # Thu
    }
    cbd = {
        "2026-08-07": {1, 2},   # done
        "2026-08-09": {1},      # partial
        # 2026-08-12 absent -> none
        "2026-08-13": {1, 2},   # done
    }
    assert ch.week_strip(occ, cbd, today) == \
        ["done", "rest", "partial", "rest", "rest", "none", "done"]
