# Chores Away / Pause Mode — design

Date: 2026-08-17
Status: approved, pre-implementation
Branch: `chores-away-mode`

## Problem

When a family member is away (camp, travel, vacation) their daily and weekly
chores keep occurring and go uncompleted. On return, the streak walk-back in
`streak()` hits those uncompleted frozen days and the streak snaps to zero. The
family wants a way to pause a person (or everyone) so an absence does not break
a streak, and so chores that still need doing are covered — then resume on
whatever day they actually return, without knowing the return date up front.

## Non-negotiable constraint: never rewrite frozen history

The occurrence log is frozen by design — editing or deleting a chore only ever
changes today and the future, and a day with no logged rows for a person reads
as a streak-neutral rest day (`app._people_day` docstring; `chores.streak`).
A large amount of migration code protects this invariant. This feature MUST NOT
rewrite, delete, or backfill frozen occurrence-log rows. Instead, away state is
a pure additive overlay that is *consulted* during resolution and streak math.
Deleting an away period returns every behavior to exactly what it was before —
that reversibility is the data-safety guarantee.

## Behavioral summary (decisions locked during brainstorming)

- **Scope:** per-person is the primitive; a "Pause everyone" admin action
  applies a period to every active person at once.
- **Rotation chores** whose turn lands on an away person fall through to whoever
  is home, reusing the existing inactive-person `active_ids` reassignment.
- **Fixed chores** of an away person reassign to an optional named **backup**
  person; with no backup they simply pause. An inherited fixed chore counts
  toward the **backup's own** streak (it is genuinely their responsibility while
  covering).
- **Timing:** open-ended. "Going away" starts a period (start date defaults to
  today, may be back-dated); "I'm back" closes it at yesterday, so the return
  day counts as active again. Because it is an overlay, back-dating is safe and
  needs no history rewrite.
- **Control surface:** admin panel only. The wall *reflects* away state but has
  no away controls (this is a rare event; minimal surface by intent).
- **Streak effect:** away days read as rest days, so the walk-back skips them and
  the pre-trip streak is preserved across the gap; the first completed day after
  return continues it.

## Data model

One new overlay table. No change to `people`, `chores`, `completions`, or
`occurrence_log`.

```sql
CREATE TABLE IF NOT EXISTS away_periods(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id),
  start_date TEXT NOT NULL,          -- inclusive, 'YYYY-MM-DD'
  end_date TEXT,                     -- inclusive; NULL = still away
  backup_person_id INTEGER,          -- NULL = fixed chores pause; else inherit
  created_at TEXT NOT NULL);
```

- A person is **away on date `d`** iff a period exists with
  `start_date <= d <= (end_date or +infinity)`.
- Created via `CREATE TABLE IF NOT EXISTS` in the schema block — idempotent, no
  data migration, inert on existing installs until a row is inserted.
- `backup_person_id` is a soft reference (no FK, matching how the frozen tables
  avoid hard chore FKs); a backup who is deleted/inactive/also-away is treated as
  "no available backup" at resolution time, so the fixed chore pauses rather than
  crashing.
- Overlapping periods for one person are allowed and simply union (membership is
  "any period covers `d`"); the admin UI will avoid creating them but the math
  does not depend on non-overlap.

### New DB helpers (`db.py`)

- `add_away_period(conn, person_id, start_date, end_date=None, backup_person_id=None) -> int`
- `close_away_period(conn, period_id, end_date) -> None`
- `update_away_period(conn, period_id, **fields) -> None`
- `delete_away_period(conn, period_id) -> bool`
- `list_away_periods(conn, include_closed=True) -> list[dict]`
- `away_map(conn, from_date, to_date) -> dict` — returns, per person id, the set
  of away date strings in the window, plus the effective backup id per away date.
  Shape: `{person_id: {"dates": set[str], "backup_on": {date: backup_id}}}`.
  This is the single source the resolution and streak layers consult; keeps the
  interval-expansion logic in one tested place.

## Behavior — two independent consult points

### 1. Resolution + freeze (today and future) — `chores.py` / `app.py`

Today and future days are resolved live from current definitions and (for today)
frozen. The away overlay is applied here so an away person's rows are never
produced, and therefore never frozen.

- `plan_rows(chores, people, d, away=None)` gains an optional `away` argument —
  the per-date away view for `d` (set of away person ids, and the backup map).
  Within `plan_rows`:
  - Build `present_ids = active_ids - away_ids` (people not away on `d`).
  - Rotation resolution passes `present_ids` to `assignee_id` (existing
    parameter), so away people's turns fall to whoever is home.
  - For a **fixed** chore assigned to an away person: if that person has an
    available backup on `d` (backup exists, is active, not away on `d`), emit the
    row assigned to the backup, tagged `covering_for = <away person id>`; else
    omit the row (chore pauses).
  - An away person's own rows are otherwise omitted → their served/frozen day has
    no rows for them → natural rest day.
- `_freeze_day` is unchanged: it freezes whatever `plan_rows` produced, which now
  already excludes away people. No frozen row is ever rewritten for the away
  state; the day is simply frozen without those rows in the first place.

### 2. Streak / week math (any day, including already-frozen past) — `chores.py`

- `streak(occ_by_date, completions_by_date, today, away_dates=None)` — before the
  walk, treat any date in `away_dates` as a rest day (skip it exactly like a
  no-occurrence day), regardless of what the frozen log holds for it. This is
  what makes a **back-dated** period repair an already-broken streak with zero
  history rewriting.
- `week_strip(occ_by_date, completions_by_date, today, away_dates=None)` — a date
  in `away_dates` yields a new `"away"` state (5th value alongside
  `done|partial|none|rest`), so an absence is visually distinct from both a rest
  day and a missed day.
- `app._people_day` builds `away_dates` for each person from `away_map` over its
  existing 370-day window and passes it into both calls. It also stamps each
  plan entry with `away: bool` (is the person away on `d`) and, when covering,
  the `covering_for` tag on inherited chores.

## Presentation (wall reflects only)

- **Away person card (Chores tab, wall + mobile):** renders "Away ✈️" with the
  preserved streak number still shown; no chore rows, no "0/0 done" scolding.
- **Week strip:** away days render as a distinct dim marker (new `"away"` state),
  clearly not a red missed day.
- **Backup's board:** the inherited fixed chore appears in the backup's list with
  a small "covering for <name>" tag; it counts toward the backup's done/total and
  streak like any of their own chores.
- **Motion:** the away badge is static — no animation, reduced-motion safe.
- No new wall controls. All setting/clearing happens in the admin panel.

## Admin panel (only control surface) — `app.py` + admin UI

New endpoints (mirroring the existing `/api/admin/*` shape and validation):

- `GET /api/admin/away` — list periods (open + recently closed) with person and
  backup names resolved, for the "who's away" view.
- `POST /api/admin/away` — body `{person_id, start_date?, backup_person_id?}`;
  start defaults to today. Creates an open period. Validates person exists and
  is active, backup (if given) exists and is not the same person.
- `POST /api/admin/away/everyone` — body `{start_date?}`; opens a period for
  every active person with no backup (a household trip — nobody is home to
  cover). Idempotent-ish: skips a person who already has an open period.
- `PATCH /api/admin/away/{id}` — edit start/end/backup (admin correction path).
- `POST /api/admin/away/{id}/back` — closes the period at yesterday (the
  "I'm back" action). Body optional `{end_date}` to override.
- `DELETE /api/admin/away/{id}` — remove a period entirely (full undo).

Admin UI: a per-person "Going away" / "I'm back" control on each person row, a
backup dropdown, a start-date field (defaulting today, back-dateable), a "Pause
everyone" button, and a small current-away list. Kept lean deliberately — this is
a rarely used surface.

`admin_state` payload gains `away_periods` (with resolved names) so the admin
screen renders current state in one round trip.

## Fail-soft

- A malformed or orphaned away period (e.g. backup pointing at a deleted person)
  must never 500 the wall. `away_map` skips unresolvable rows and logs at ERROR,
  the same fails-soft philosophy as `_links()` / the todos block in `hub()`. A
  broken away row degrades to "person not away / no backup", never a blank wall.
- Resolution treats an unavailable backup as "no backup" (chore pauses) rather
  than raising.

## Testing

Pure engine (`tests/test_chores.py`, extend existing streak/week tests):

- `streak` with `away_dates`: away day skipped; streak preserved across a
  multi-day gap; back-dated away date repairs an otherwise-broken streak; away
  interleaved with a partial day.
- `week_strip` with `away_dates`: away state emitted, distinct from rest/none.
- `plan_rows` with `away`: rotation turn on an away person falls to whoever is
  home; fixed chore of an away person reassigns to an available backup with the
  `covering_for` tag; no backup → chore omitted; backup who is also away →
  chore omitted (paused), no crash.

DB (`tests/test_db.py`):

- away-period CRUD; `away_map` interval expansion (open-ended, closed, multi-day,
  overlapping, out-of-window clipping).
- `CREATE TABLE IF NOT EXISTS` is idempotent on an existing DB; no effect on
  frozen tables.

API (`tests/test_api.py` / `test_chores` integration):

- Serving today for an away person freezes the log WITHOUT their rows; the return
  day continues the streak; a back-dated period drops already-frozen days from
  the streak without altering `occurrence_log` (assert the log rows are
  byte-identical before/after).
- Admin endpoints: create/close/back/delete, "pause everyone", validation
  failures (unknown person, self-backup).

DEMO (`demo.py`): one demo person is away with a backup covering a fixed chore,
so the away card, the `"away"` week state, and the covering tag are all visible
in the DEMO payload without real data.

Structural / static guards (`tests/test_static.py`): away badge is static (no
animation on the away marker), away card meets the mobile tap/spacing rules, week
strip renders the 5th state.

## Rollout / feature-gauntlet gates (`docs/adding-a-feature.md`)

Run the gauntlet end to end: fail-soft server side (above), DEMO payload,
mobile Chores tab + tab-bar re-fit for the away card, motion/reduced-motion
(static badge), tests + structural guards, all visual gates, README +
`docs/hub.png` regenerated in the same PR, the three-agent review
(silent-failure-hunter, code-reviewer, pr-test-analyzer) re-run if the branch
grows, and live post-deploy verification on the wall.

This is core chores behavior, not a togglable data source, so no
integrations-registry entry is needed.

## Explicitly out of scope

- Automatic history rewriting / backfill of frozen rows (forbidden by the core
  invariant; back-dating via the overlay covers the real need).
- On-wall away controls (admin-only by decision).
- Per-chore "reassign vs skip" configuration (rotations always fall through;
  fixed chores use the single backup).
- Calendar-integration auto-detection of trips (future idea, not this feature).
