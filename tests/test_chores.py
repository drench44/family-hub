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


def test_rotation_is_deterministic_and_skips_nonoccurring_days():
    rot = C(assign_kind="rotation", rotation_order=[7, 8, 9],
            schedule_kind="days", days_mask=0b0000001)     # Mondays only
    # epoch Mon 2026-08-03: 8/3->7, 8/10->8, 8/17->9, 8/24->7
    assert ch.assignee_id(rot, dt.date(2026, 8, 3)) == 7
    assert ch.assignee_id(rot, dt.date(2026, 8, 10)) == 8
    assert ch.assignee_id(rot, dt.date(2026, 8, 24)) == 7
    assert ch.assignee_id(C(assign_kind="rotation", rotation_order=[]),
                          dt.date(2026, 8, 10)) is None


def test_fixed_assignee_returns_person():
    assert ch.assignee_id(C(fixed_person_id=5), dt.date(2026, 8, 10)) == 5


def test_streak_skips_rest_days_and_forgives_unfinished_today():
    weekly = C(schedule_kind="days", days_mask=0b0000101)  # Mon+Wed
    today = dt.date(2026, 8, 13)                           # Thursday
    # completions_by_date maps 'YYYY-MM-DD' -> set of chore_ids completed
    # by the queried person (the API layer builds it per person)
    assert ch.streak(1, [weekly], {"2026-08-10": {1}, "2026-08-12": {1}}, today) == 2
    assert ch.streak(1, [weekly], {"2026-08-12": {1}}, today) == 1   # Mon missed -> stops
    # unfinished today: daily chore occurs Thursday, not done yet — forgiven
    daily = C(id=2)
    assert ch.streak(1, [daily], {"2026-08-12": {2}, "2026-08-11": {2},
                                  "2026-08-10": {2}}, dt.date(2026, 8, 13)) == 3


def test_streak_zero_when_no_history():
    assert ch.streak(1, [C()], {}, dt.date(2026, 8, 13)) == 0
    # completed today (all done) counts as 1
    assert ch.streak(1, [C(id=3)], {"2026-08-13": {3}}, dt.date(2026, 8, 13)) == 1


def test_day_plan_shape_done_flags_and_rotation():
    people = [P(1, "A", "#111111"), P(2, "B", "#222222")]
    fixed_a = C(id=10, title="Dishes", icon="🍽️", fixed_person_id=1,
                rotation_epoch="2026-08-01")
    fixed_b = C(id=11, title="Trash", fixed_person_id=2, rotation_epoch="2026-08-01")
    rot = C(id=12, title="Feed cat", assign_kind="rotation",
            rotation_order=[1, 2], rotation_epoch="2026-08-12")
    d = dt.date(2026, 8, 12)   # epoch day -> rotation n=0 -> person 1
    plan = ch.day_plan([fixed_a, fixed_b, rot], people, d, {10})

    p1 = plan[0]
    assert p1["person"] == {"id": 1, "name": "A", "color": "#111111"}
    assert [(c["id"], c["done"]) for c in p1["chores"]] == [(10, True), (12, False)]
    assert (p1["done_count"], p1["total"]) == (1, 2)

    p2 = plan[1]
    assert [(c["id"], c["done"]) for c in p2["chores"]] == [(11, False)]
    assert (p2["done_count"], p2["total"]) == (0, 1)


def test_day_plan_omits_unresolvable_and_empty_people():
    # fixed_b's person (2) is inactive -> not in the people list -> chore omitted
    people = [P(1, "A")]
    fixed_a = C(id=10, fixed_person_id=1, rotation_epoch="2026-08-01")
    fixed_b = C(id=11, fixed_person_id=2, rotation_epoch="2026-08-01")
    empty_rot = C(id=13, assign_kind="rotation", rotation_order=[],
                  rotation_epoch="2026-08-01")
    d = dt.date(2026, 8, 12)
    plan = ch.day_plan([fixed_a, fixed_b, empty_rot], people, d, set())
    assert len(plan) == 1
    assert [c["id"] for c in plan[0]["chores"]] == [10]   # 11 and 13 omitted

    assert ch.day_plan([fixed_a], [], d, set()) == []      # no people


def test_week_strip_composition():
    # occurs Wed(4)+Thu(8)+Fri(16)+Sun(64) = mask 92; Sat/Mon/Tue are rest
    a = C(id=1, schedule_kind="days", days_mask=92, rotation_epoch="2026-08-01")
    b = C(id=2, schedule_kind="days", days_mask=92, rotation_epoch="2026-08-01")
    today = dt.date(2026, 8, 13)   # Thursday; window 8/7 Fri .. 8/13 Thu
    cbd = {
        "2026-08-07": {1, 2},   # Fri  -> done
        "2026-08-09": {1},      # Sun  -> partial
        # 2026-08-12 Wed absent -> none
        "2026-08-13": {1, 2},   # Thu  -> done
    }
    assert ch.week_strip(1, [a, b], cbd, today) == \
        ["done", "rest", "partial", "rest", "rest", "none", "done"]
