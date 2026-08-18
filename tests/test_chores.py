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


def _fixed(cid, pid, title="x"):
    return {"id": cid, "title": title, "icon": "", "schedule_kind": "daily",
            "days_mask": 0, "assign_kind": "fixed", "fixed_person_id": pid,
            "rotation_order": [], "rotation_epoch": "2026-01-01", "active": 1}


def _rot(cid, order):
    return {"id": cid, "title": "trash", "icon": "", "schedule_kind": "daily",
            "days_mask": 0, "assign_kind": "rotation", "fixed_person_id": None,
            "rotation_order": order, "rotation_epoch": "2026-01-01", "active": 1}


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
         "rot": 0, "covering_for": None},
        {"chore_id": 12, "person_id": 1, "title": "Feed cat", "icon": "",
         "rot": 1, "covering_for": None},
    ]


def test_plan_rows_rotation_skips_inactive_person():
    # person 2 is not in the active people list -> their turns go to person 1
    people = [P(1, "A")]
    rot = C(id=12, assign_kind="rotation", rotation_order=[1, 2],
            rotation_epoch="2026-08-12")
    for d in (dt.date(2026, 8, 12), dt.date(2026, 8, 13)):
        rows = ch.plan_rows([rot], people, d)
        assert [r["person_id"] for r in rows] == [1]


def test_plan_rows_rotation_falls_to_whoever_is_home():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    ch_rot = _rot(9, [1, 2])
    base = ch.plan_rows([ch_rot], people, d)               # normal assignee
    normal_pid = base[0]["person_id"]
    away_pid = normal_pid
    other = 2 if normal_pid == 1 else 1
    rows = ch.plan_rows([ch_rot], people, d, {"ids": {away_pid}, "backup": {}})
    assert rows and rows[0]["person_id"] == other          # fell to who's home


def test_plan_rows_fixed_reassigns_to_available_backup():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    rows = ch.plan_rows([_fixed(5, 1, "dog")], people, d,
                        {"ids": {1}, "backup": {1: 2}})
    assert len(rows) == 1
    assert rows[0]["person_id"] == 2 and rows[0]["covering_for"] == 1


def test_plan_rows_fixed_pauses_when_no_available_backup():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    # no backup -> paused
    assert ch.plan_rows([_fixed(5, 1)], people, d, {"ids": {1}, "backup": {}}) == []
    # backup who is also away -> paused (not crash)
    assert ch.plan_rows([_fixed(5, 1)], people, d,
                        {"ids": {1, 2}, "backup": {1: 2}}) == []


def test_plan_rows_away_person_own_chore_absent():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"}]
    assert ch.plan_rows([_fixed(5, 1)], people, d, {"ids": {1}, "backup": {}}) == []


def test_covering_for_not_persisted_in_log_shape():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    r = ch.plan_rows([_fixed(5, 1, "dog")], people, d,
                     {"ids": {1}, "backup": {1: 2}})[0]
    assert set(r) >= {"chore_id", "person_id", "title", "icon", "rot",
                      "covering_for"}


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


def test_streak_treats_away_days_as_rest_and_preserves_across_gap():
    today = dt.date(2026, 8, 17)                     # Sunday
    # daily chore id 1; done through 8/12, away 8/13..8/16, back today undone
    occ = {d: {1} for d in ("2026-08-10", "2026-08-11", "2026-08-12",
                            "2026-08-13", "2026-08-14", "2026-08-15",
                            "2026-08-16", "2026-08-17")}
    done = {d: {1} for d in ("2026-08-10", "2026-08-11", "2026-08-12")}
    away = {"2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16"}
    # Without away, 8/13 is an uncompleted day -> streak breaks after today's
    # forgiveness, counting back only to 8/12 = 3. With away, the gap is rest,
    # so it still reaches the 8/10-8/12 run = 3, but crucially does not BREAK.
    assert ch.streak(occ, done, today, away) == 3
    # Prove non-away would break at 8/16 (0 completed) instead:
    assert ch.streak(occ, done, today) == 0  # 8/16 occurred, not done -> break


def test_streak_backdated_away_repairs_without_touching_history():
    today = dt.date(2026, 8, 17)
    occ = {d: {1} for d in ("2026-08-14", "2026-08-15", "2026-08-16",
                            "2026-08-17")}
    done = {"2026-08-14": {1}, "2026-08-17": {1}}     # missed 15 & 16 (trip)
    assert ch.streak(occ, done, today) == 1           # broken by 8/16
    away = {"2026-08-15", "2026-08-16"}
    assert ch.streak(occ, done, today, away) == 2     # 8/17 + 8/14 across gap


def test_week_strip_emits_away_state():
    today = dt.date(2026, 8, 13)                       # Thu; window Fri..Thu
    occ = {d: {1} for d in ("2026-08-07", "2026-08-11", "2026-08-13")}
    cbd = {"2026-08-07": {1}, "2026-08-13": {1}}
    away = {"2026-08-11"}
    assert ch.week_strip(occ, cbd, today, away) == \
        ["done", "rest", "rest", "rest", "away", "rest", "done"]


# --- away coverage the analyzer wants (behavior looks correct; prove it) ------

def test_plan_rows_all_rotation_members_away_returns_empty():
    """F5: a 2-person rotation with BOTH members away resolves to no rows --
    present_ids is empty, assignee_id filters the order to nothing and returns
    None, so the chore is omitted. No ZeroDivision, no crash."""
    d = dt.date(2026, 8, 17)
    people = [P(1, "A"), P(2, "B")]
    rows = ch.plan_rows([_rot(5, [1, 2])], people, d,
                        {"ids": {1, 2}, "backup": {}})
    assert rows == []


def test_backup_who_is_also_away_pauses_fixed_chore():
    """F4: A is away with backup B, but B is also away. The fixed chore has no
    present cover, so plan_rows pauses it (row omitted) rather than assigning to
    an away backup or crashing."""
    d = dt.date(2026, 8, 17)
    people = [P(1, "A"), P(2, "B")]
    rows = ch.plan_rows([_fixed(5, 1)], people, d,
                        {"ids": {1, 2}, "backup": {1: 2}})
    assert rows == []


def test_backup_deleted_pauses_fixed_chore():
    """F3: the away person's backup was deleted (no longer in the people list),
    so the backup id isn't present -> the fixed chore pauses, no stale covering
    row pointing at a deleted person."""
    d = dt.date(2026, 8, 17)
    people = [P(1, "A")]                    # backup id 2 deleted
    rows = ch.plan_rows([_fixed(5, 1)], people, d,
                        {"ids": {1}, "backup": {1: 2}})
    assert rows == []


def test_away_boundary_last_day_away_next_day_normal():
    """F6: the last inclusive away day reads 'away'; the day immediately after
    end_date reads its normal state -- proven through week_strip."""
    today = dt.date(2026, 8, 17)           # Sunday; window 8/11..8/17
    occ = {d: {1} for d in ("2026-08-14", "2026-08-15", "2026-08-16",
                            "2026-08-17")}
    cbd = {"2026-08-14": {1}, "2026-08-17": {1}}
    away = {"2026-08-15", "2026-08-16"}    # end_date inclusive on 8/16
    ws = ch.week_strip(occ, cbd, today, away)
    # window index: [8/11,8/12,8/13,8/14,8/15,8/16,8/17]
    assert ws[5] == "away", "8/16 is the last inclusive away day"
    assert ws[6] == "done", "8/17 (day after end_date) is a normal completed day"
    # and the streak counts the post-away day plus across the gap
    assert ch.streak(occ, cbd, today, away) == 2
