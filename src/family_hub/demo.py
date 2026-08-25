"""Sample-family fixtures for DEMO mode (env DEMO=1).

Everything here is fake and self-contained: a fully populated wall a stranger
can spin up for a README screenshot or a "try it" run with no calendars,
cameras, feeds, or config secrets. Dates are computed RELATIVE to a passed-in
``today`` so the demo always looks current, and the seeding runs against an
EMPTY db (guarded by app.py checking there are no people yet).

The weather/climate payloads mirror the exact dict shapes ``tiles.weather_tile``
and ``tiles.climate_tile`` return, so the frontend renders a demo wall
identically to a live one. The camera entries mirror ``_links()`` cameras but
carry ``demo: True`` (no go2rtc URLs) so the frontend paints a static
placeholder instead of a live stream.
"""
from __future__ import annotations

import datetime as dt

from . import chores as chlogic
from . import db as fdb

# One demo calendar; its rail color is seeded into the kv `calendar_colors`
# map so events pick up a color exactly the way a synced Google calendar would.
DEMO_CALENDAR_ID = "demo-home"
DEMO_CALENDAR_COLOR = "#5BC9F0"

# The sample family: nicknames + their one expressive hue on the wall.
_PEOPLE = [
    ("Ava", "#E86A9E"),
    ("Milo", "#3E9BE8"),
    ("Ruby", "#E39A2A"),
]

# Weekdays Mon..Fri as a days_mask (bit 0 = Monday .. bit 6 = Sunday); Milo's
# chores ride this minus today's bit, so his card always reads "nothing this
# day" while still having a real record on the other days.
_WEEKDAYS_MASK = 0b0011111


def _add_daily_fixed(conn, title, icon, person_id, epoch):
    return fdb.add_chore(
        conn, title=title, icon=icon, schedule_kind="daily", days_mask=0,
        assign_kind="fixed", fixed_person_id=person_id, rotation_order=[],
        rotation_epoch=epoch)


# Every table seed_demo writes into; children (FK holders) before parents.
# away_periods holds a NOT NULL FK to people, so it's deleted before people.
_SEEDED_TABLES = ("completions", "occurrence_log", "todos", "events", "chores",
                  "away_periods", "people")


def is_unseeded(conn) -> bool:
    """True only if EVERY table seed_demo writes into is empty. The seed (and the
    clear_demo it triggers on a partial failure) must run only against a truly
    empty db: a real family's db can be people-less while still holding todos,
    events, or chore history (todos need no people; or every person was deleted),
    and seeding would then wipe real events and mix demo rows into real ones
    (issue #36). Guard on all seeded tables, not just people."""
    for tbl in _SEEDED_TABLES:
        if conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone() is not None:
            return False
    return True


def clear_demo(conn) -> None:
    """Undo everything seed_demo writes, back to an empty db. app.py calls this
    when a seed raises partway: the fdb helpers each self-commit, so a failed
    seed can leave people/chores already written, and the "no people yet" guard
    would then treat that half-written db as already seeded. Wiping lets the
    guard fire again and the next open re-seed cleanly. Only ever runs against a
    db app.py already found empty, so it never deletes a real family's data."""
    with conn:
        for tbl in _SEEDED_TABLES:
            conn.execute(f"DELETE FROM {tbl}")
        conn.execute(
            "DELETE FROM kv WHERE key IN ('calendar_status', 'calendar_colors')")


def seed_demo(conn, today: dt.date) -> None:
    """Seed the whole sample wall (people, chores, ~6 days of history, todos and
    calendar events) into an EMPTY db. Intended to run once, on first open."""
    epoch = (today - dt.timedelta(days=30)).isoformat()   # every chore predates the window

    ava, milo, ruby = (fdb.add_person(conn, name, color) for name, color in _PEOPLE)

    # Ava: four daily chores. Three are done today, "Brush the dog" is not, so
    # her card reads 3/4 today and her streak (which forgives an unfinished
    # today) counts back through the completed past days -> ~3.
    ava_laundry = _add_daily_fixed(conn, "Laundry", "🧺", ava, epoch)
    ava_cage = _add_daily_fixed(conn, "Clean rabbit cage", "🐰", ava, epoch)
    ava_workout = _add_daily_fixed(conn, "Workout", "🏃", ava, epoch)
    ava_dog = _add_daily_fixed(conn, "Brush the dog", "🐶", ava, epoch)

    # Milo: two chores on weekdays that are NOT today (mask excludes today's
    # bit), so today's card shows "nothing this day".
    milo_mask = _WEEKDAYS_MASK & ~(1 << today.weekday())
    fdb.add_chore(conn, title="Set the table", icon="🍴", schedule_kind="days",
                  days_mask=milo_mask, assign_kind="fixed", fixed_person_id=milo,
                  rotation_order=[], rotation_epoch=epoch)
    fdb.add_chore(conn, title="Sort recycling", icon="♻️", schedule_kind="days",
                  days_mask=milo_mask, assign_kind="fixed", fixed_person_id=milo,
                  rotation_order=[], rotation_epoch=epoch)
    # Milo's one DAILY fixed chore -- unlike the two above (scheduled off
    # today), this one occurs every day, including today and any away day.
    # That's what makes his away period below always have a chore for Ava to
    # cover, whatever day of the week the demo happens to be seeded on.
    _add_daily_fixed(conn, "Feed the fish", "🐟", milo, epoch)

    # Ruby: two daily chores. "Water plants" is done today, "Trash out" is not,
    # so her streak counts back through her completed past days -> ~2.
    ruby_trash = _add_daily_fixed(conn, "Trash out", "🗑️", ruby, epoch)
    ruby_plants = _add_daily_fixed(conn, "Water plants", "🌱", ruby, epoch)

    people = fdb.list_people(conn)
    chores = fdb.list_chores(conn)

    # Milo is away the last 2 days, still ongoing (open-ended): Ava backs him
    # up. Seeded here, before the history freeze below, so both the frozen
    # past days AND the live 'today' render (app.py resolves 'today' itself,
    # straight from this same away_periods row) agree: Milo's away badge, his
    # paused-not-broken streak, and Ava's "covering for Milo" tag on Feed the
    # fish are all visible the moment someone loads the demo wall.
    away_start = (today - dt.timedelta(days=2)).isoformat()
    fdb.add_away_period(conn, milo, away_start, None, backup_person_id=ava)
    amap = fdb.away_map(conn, (today - dt.timedelta(days=6)).isoformat(),
                        today.isoformat())

    def away_view(d: dt.date) -> dict:
        """The plan_rows() away overlay for day ``d`` -- via the SAME shared
        helper the wall and the iCloud mirror use, so the demo can never drift
        from what the real render produces. A day nobody is away yields an
        empty overlay, which resolves exactly as it did before this feature."""
        return chlogic.away_view_on(amap, d.isoformat())

    # Which chore_ids count as "completed" on a day offset i days before today.
    # Ava fully completes the last 3 days then slips one earlier (capping her
    # streak at 3); Ruby fully completes the last 2 then slips (streak 2); Milo
    # completes everything that occurs. Anything not listed is left undone so a
    # day reads "partial" and the week strip looks like a real household's.
    def completed_for(i: int) -> set[int]:
        done: set[int] = set()
        # Ava: all four on days 1..3, three of four on days 4..6 (dog skipped).
        if i <= 3:
            done |= {ava_laundry, ava_cage, ava_workout, ava_dog}
        else:
            done |= {ava_laundry, ava_cage, ava_workout}
        # Ruby: both on days 1..2, only plants from day 3 back.
        if i <= 2:
            done |= {ruby_trash, ruby_plants}
        else:
            done |= {ruby_plants}
        return done

    # Freeze the past ~6 days into the occurrence log (the same rows the wall
    # would have written live on each of those days) and record completions, so
    # streaks and the 7-day week strip render from real history.
    for i in range(6, 0, -1):
        d = today - dt.timedelta(days=i)
        # Pass the away view for any day inside Milo's away window so the
        # FROZEN log itself already reflects away: his own rows are absent
        # and his fixed chore's row belongs to Ava, tagged covering_for. If
        # this were plain plan_rows(chores, people, d), a past day drilled
        # into from the day browser would show Milo doing chores on a day he
        # was recorded away -- exactly the contradiction this task exists to
        # avoid.
        rows = chlogic.plan_rows(chores, people, d, away_view(d))
        fdb.replace_day_log(conn, d.isoformat(), rows)
        done = completed_for(i)
        for r in rows:
            # Milo has no explicit entry above, so complete his occurring rows
            # here (he's the reliable one); Ava/Ruby follow the `done` set;
            # and whoever is covering for Milo while he's away completes that
            # row too, so the covering day still reads fully "done" -- an
            # uncompleted covering row would otherwise turn one of Ava's
            # already-scripted "fully done" streak days into "partial".
            if r["person_id"] == milo or r["chore_id"] in done \
                    or r["covering_for"] is not None:
                fdb.set_completion(conn, r["chore_id"], d.isoformat(), r["person_id"])

    # Today's completions: three of Ava's four, and Ruby's plants (today's
    # occurrence log itself is written live by the hub on the first serve).
    today_str = today.isoformat()
    for chore_id, person_id in ((ava_laundry, ava), (ava_cage, ava),
                                (ava_workout, ava), (ruby_plants, ruby)):
        fdb.set_completion(conn, chore_id, today_str, person_id)

    _seed_todos(conn)
    _seed_calendar(conn, today)


def _seed_todos(conn) -> None:
    """A shared household list that reads roughly 3 now / 2 soon / 5 later."""
    for title in ("Clean the recycling bin", "Return the bottles",
                  "Order more dog food"):
        fdb.add_todo(conn, title, "now")
    for title in ("Book the dentist checkup", "Replace the porch light bulb"):
        fdb.add_todo(conn, title, "soon")
    for title in ("Plan Ava's birthday party", "Deep-clean the garage",
                  "Sort the winter clothes", "Research summer camps",
                  "Fix the squeaky back gate"):
        fdb.add_todo(conn, title, "later")


def _seed_calendar(conn, today: dt.date) -> None:
    """A handful of demo events on one calendar. Timestamps carry no timezone
    offset on purpose: fmtTime reads the wall-clock digits and the "ended" strike
    compares against the viewer's local clock, so the demo renders sensibly in
    any timezone without knowing the box's TZ here."""
    today_str = today.isoformat()
    plus3 = (today + dt.timedelta(days=3)).isoformat()

    def ev(eid, day, start, end, title):
        return {"id": eid, "calendar_id": DEMO_CALENDAR_ID, "title": title,
                "start_ts": f"{day}T{start}:00", "end_ts": f"{day}T{end}:00",
                "all_day": 0}

    def allday(eid, day, ndays, title):
        # All-day ends are exclusive (Google's convention).
        end = (day + dt.timedelta(days=ndays)).isoformat()
        return {"id": eid, "calendar_id": DEMO_CALENDAR_ID, "title": title,
                "start_ts": day.isoformat(), "end_ts": end, "all_day": 1}

    events = [
        # All-day events: the month grid draws each as a bar spanning the
        # days it covers, and the agenda tags a multi-day run "day N of M".
        # The 6-day fair usually crosses a week boundary (the clipped-arrow
        # bar); the birthday is a plain one-day all-day event.
        allday("demo-fair", today + dt.timedelta(days=2), 6, "State fair"),
        allday("demo-grandma", today + dt.timedelta(days=3), 3, "Grandma visiting"),
        allday("demo-bday", today + dt.timedelta(days=1), 1, "Aunt Jo's birthday"),
        # Ended earlier today -> renders struck through on the wall.
        ev("demo-eye", today_str, "11:30", "12:15", "Eye appointment"),
        ev("demo-pest", today_str, "15:00", "15:45",
           "Pest control: crawl space quote"),
        ev("demo-crawl", plus3, "09:00", "11:00", "Crawl space service"),
        ev("demo-guitar", plus3, "11:30", "12:15", "Guitar lesson"),
    ]
    fdb.replace_events(conn, events)
    # Mark the calendar healthy and give it a rail color, mirroring a real sync.
    fdb.kv_set(conn, "calendar_status", {"ok": True})
    fdb.kv_set(conn, "calendar_colors", {DEMO_CALENDAR_ID: DEMO_CALENDAR_COLOR})


def demo_weather() -> dict:
    """A live-shaped weather tile (matches tiles.weather_tile) for a clear,
    mild summer afternoon. The spark is a plausible 24h curve, oldest->newest,
    with ``spark_now`` marking the current hour."""
    # ~12h of "observations" behind and ~12h of "forecast" ahead; the values
    # sweep the seeded low (59) to high (81).
    spark = [62.0, 60.0, 59.0, 59.0, 61.0, 64.0, 68.0, 72.0, 74.8, 77.0,
             79.0, 80.0, 81.0, 80.0, 78.0, 75.0, 72.0, 69.0, 66.0, 64.0,
             62.0, 61.0, 60.0, 59.0]
    return {
        "available": True,
        "temp": 74.8,
        "unit": "F",
        "conditions": "Clear & sunny",
        "feels": 78,
        "feels_desc": "Warm",
        "low": 59,
        "high": 81,
        "uv": 6,
        "uv_desc": "High",
        "aqi": 42,
        "aqi_cat": "Good",
        "humidity": 57,
        "dew_point": 58.5,
        "spark": spark,
        "spark_now": 8,
        # 5-day daily forecast strip (weather card foot); first entry is today,
        # so its hi/lo echo the "high"/"low" above. Shapes tiles.weather_tile's
        # 'forecast' list: [{day, hi, lo, cond}, ...].
        "forecast": [
            {"day": "Mon", "hi": 81, "lo": 59, "cond": "Clear & sunny"},
            {"day": "Tue", "hi": 84, "lo": 62, "cond": "Partly cloudy"},
            {"day": "Wed", "hi": 78, "lo": 61, "cond": "Scattered showers"},
            {"day": "Thu", "hi": 73, "lo": 57, "cond": "Thunderstorms"},
            {"day": "Fri", "hi": 79, "lo": 58, "cond": "Mostly sunny"},
        ],
        "stale": False,
        "sunrise": "06:15",
        "sunset": "20:15",
        "moon_phase": "Waxing Gibbous",
        "moon_illum": 68,
    }


def demo_climate() -> dict:
    """A live-shaped climate tile (matches tiles.climate_tile): four rooms, all
    fresh, plus indoor RH / dew point."""
    return {
        "available": True,
        "rooms": [
            {"name": "Upstairs", "channel": 1, "temp_f": 75.0,
             "humidity": 48, "stale": False},
            {"name": "Downstairs", "channel": 2, "temp_f": 75.0,
             "humidity": 51, "stale": False},
            {"name": "Garage", "channel": 3, "temp_f": 77.0,
             "humidity": 55, "stale": False},
            {"name": "Crawl Space", "channel": 4, "temp_f": 68.0,
             "humidity": 62, "stale": False},
        ],
        "indoor_rh": 51,
        "indoor_dp": 55.5,
    }


def demo_cameras() -> list[dict]:
    """Camera link entries shaped like ``_links()`` cameras but each flagged
    ``demo: True`` (no go2rtc URLs). The frontend renders a static gradient
    placeholder tile for these and skips the liveness probe. ``tone`` steers the
    placeholder's gradient (cool for the front door, warm for the back yard)."""
    return [
        {"src": "demo-front", "label": "Front Door", "demo": True, "tone": "cool"},
        {"src": "demo-yard", "label": "Back Yard", "demo": True, "tone": "warm"},
    ]


def demo_camera_page() -> list[dict]:
    """Four placeholder cameras for the Cameras-tab 2x2 grid (row-major:
    top-left, top-right, bottom-left, bottom-right). Same shape as
    ``demo_cameras()`` so DEMO renders a full grid for a README screenshot."""
    return [
        {"src": "demo-drive", "label": "Driveway", "demo": True, "tone": "cool"},
        {"src": "demo-mail", "label": "Mailbox", "demo": True, "tone": "cool"},
        {"src": "demo-yard", "label": "Back Yard", "demo": True, "tone": "warm"},
        {"src": "demo-side", "label": "Side Gate", "demo": True, "tone": "warm"},
    ]


def demo_laundry() -> dict:
    """A live-shaped laundry tile (matches tiles.laundry_tile plus the route's
    ``last_done`` annotation): the washer mid-cycle finishing soon and the
    dryer freshly done, so a demo screenshot shows both signature states.
    Times are computed relative to now so the countdown always looks live."""
    now = dt.datetime.now(dt.timezone.utc)

    def iso(minutes: float) -> str:
        return (now + dt.timedelta(minutes=minutes)).isoformat()

    return {
        "available": True,
        "machines": [
            {"id": "washer", "label": "Washer", "kind": "washer",
             "phase": "running", "status": "rinsing",
             "finishes_at": iso(23), "status_since": iso(-8),
             "last_done": iso(-26 * 60)},
            {"id": "dryer", "label": "Dryer", "kind": "dryer",
             "phase": "done", "status": "end",
             "finishes_at": None, "status_since": iso(-47),
             "last_done": iso(-47)},
        ],
    }


def demo_fleet() -> dict:
    """A live-shaped fleet tile (matches tiles.fleet_tile's trimmed contract):
    a fully healthy fleet and a printer mid-print, so a demo screenshot shows
    the card's best state. ``remainingMinutes`` is a fixed count (not relative
    to ``now`` the way laundry's timestamps are — the printer contract carries
    a duration, not an ISO instant, so there is nothing to re-anchor)."""
    return {
        "available": True,
        "fleet": {
            "health": "ok",
            "hostsUp": 3,
            "hostsTotal": 3,
            "worstProblem": None,
        },
        "printer": {
            "health": "ok",
            "state": "printing",
            "job": "Bracket_v3.gcode",
            "progressPercent": 42,
            "remainingMinutes": 96,
            "nozzleF": 410.0,
            "bedF": 140.0,
            "online": True,
        },
    }


def demo_laundry_log() -> dict:
    """A live-shaped cycle log (matches /api/laundry/log): the dryer's
    observed finish plus a washer missed-finish — the two row shapes the
    real endpoint produces. Times relative to now, like demo_laundry."""
    now = dt.datetime.now(dt.timezone.utc)

    def iso(minutes: float) -> str:
        return (now + dt.timedelta(minutes=minutes)).isoformat()

    return {"entries": [
        {"ts": iso(-47), "machine": "dryer", "prev_phase": "running",
         "phase": "done", "status": "end", "finishes_at": None,
         "status_since": iso(-47), "note": None},
        {"ts": iso(-26 * 60), "machine": "washer", "prev_phase": "running",
         "phase": "idle", "status": "power_off",
         "finishes_at": iso(-26 * 60 - 2), "status_since": iso(-26 * 60),
         "note": "missed_finish"},
    ]}
