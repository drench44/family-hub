# Native per-person chores — design

**Goal.** Make chores work well per person, support richer routines (weekly /
biweekly / every-N-days / timed), and back them **natively in iOS** so each
person sees and checks off their chores in the iPhone Reminders app, gets
notifications, and can ask Siri — while the wall keeps its rotation, streaks,
grid, and frozen history.

## Decisions (locked with the operator)

1. **The wall is the source of truth.** Rotation, streaks, the grid, and history
   stay local (they have no equivalent in iCloud). The wall *mirrors* each
   person's chores into iCloud so they're native on iPhone; check-off syncs both
   ways.
2. **One iCloud Reminders list per person** (e.g. "Emma", "Jack").
3. **New routine types:** weekly / biweekly, every-N-days, and a **due time**
   (for notifications). Monthly is out of scope.

## Why mirror (not "iCloud is the source")

iCloud Reminders is a flat "per-list repeating tasks" model — no rotation, no
streaks. A **rotating** chore hands off between people each cycle, i.e. it moves
between *different people's lists*; that can't be one native repeating reminder.
So the wall computes each occurrence (existing `chores.occurs` / `assignee_id`)
and **materializes** it as a concrete reminder in the current assignee's list.
Fixed-person routines could in principle be one native `RRULE` reminder, but we
materialize uniformly (per occurrence, over a rolling horizon) so rotation and
fixed chores share one code path and streaks stay authoritative on the wall.

## Architecture

```
  Wall chores (SOURCE OF TRUTH)                  iCloud (bot account garynbot)
  ┌───────────────────────────┐                  ┌──────────────────────────┐
  │ chores + rotation + times  │                  │  "Emma"  (VTODO list)     │
  │ streaks + occurrence_log   │   materialize    │  "Jack"  (VTODO list)     │
  │ plan_rows(today..+H days)  │ ───────────────► │   • Feed dog  due 7am ⏰   │
  │                            │                  │   • Trash     due today   │
  │ completions (per person)   │ ◄─────────────── │  (shared to each iPhone)  │
  └───────────────────────────┘  reconcile done  └──────────────────────────┘
        ▲   check off on wall            check off in iOS / Siri   │
        └──────────────── both directions update both ────────────┘
```

Built on the two-way CalDAV outbox that already works (create/If-None-Match,
update/If-Match, delete/If-Match — all verified live against real iCloud).

## Data model

Extend `chores` (new columns, all defaulted so existing rows are valid):

| column | meaning |
|---|---|
| `schedule_kind` | add `'interval'` to the existing `daily`/`days`/`once` |
| `week_interval INTEGER DEFAULT 1` | for `days`: 1 = weekly, 2 = biweekly (weeks counted from `rotation_epoch`) |
| `interval_days INTEGER` | for `interval`: occurs every N days from `rotation_epoch` |
| `due_times TEXT DEFAULT '[]'` | JSON list of `"HH:MM"` local times → iOS notifications (empty = all-day, no notification) |

Link people to their iCloud list:

| column | meaning |
|---|---|
| `people.reminder_list_id TEXT` | the `caldav:<slug>` VTODO collection this person's chores mirror into (null = not mirrored) |

New table — the mirror ledger (chore occurrence ↔ iCloud object):

```
CREATE TABLE chore_mirror(
  chore_id INTEGER NOT NULL,
  date TEXT NOT NULL,            -- occurrence date 'YYYY-MM-DD'
  person_id INTEGER NOT NULL,
  cal_object_id TEXT NOT NULL,   -- the cal_objects row we created for it
  uid TEXT NOT NULL,             -- stable VTODO UID (below)
  PRIMARY KEY(chore_id, date));
```

**Stable UID scheme:** `familyhub-chore-<chore_id>-<date>` so a reminder is
idempotent to (chore, date) and reconciliation can map an iCloud completion back
to the right occurrence + person regardless of which list it lives in.

## Recurrence logic (`chores.occurs`)

- `daily` — every day (unchanged).
- `days` — masked weekday **and** `((weeks_since_epoch) % week_interval) == 0`
  (weekly when `week_interval=1`, biweekly when `2`).
- `interval` — `((d - epoch).days % interval_days) == 0`.
- `once` — `d == epoch` (unchanged).

`assignee_id` (rotation) is unchanged for `daily`; extend its closed-form week
math to respect `week_interval`, and add an `interval` branch (rotate per
occurrence index).

## The mirror engine (runs in the CalDAV sync tick)

Over a rolling horizon **today .. today + H** (H≈7, config):

1. **Desired set** = for each day in the horizon, `plan_rows()` → one desired
   reminder per (chore, date, assignee) whose person has a `reminder_list_id`.
   VTODO carries: SUMMARY (icon + title), DUE (date, or date+first due-time),
   `VALARM`s for each due-time (native notifications), and the stable UID.
2. **Reconcile** against `chore_mirror` + `cal_objects`:
   - missing → queue a create in the assignee's list (existing outbox).
   - assignee changed (rotation) → delete from the old list, create in the new.
   - past the horizon / chore deleted / deactivated → delete (only future,
     un-completed ones; never touch a completed reminder — it's history).
3. All writes go through the existing outbox (`queue_cal_object_*` → `flush_pending`),
   so If-Match/server-wins and the pending-backlog surfacing come for free.

Materializing only a short horizon keeps each person's Reminders list to "this
week," not a wall of future tasks.

## Two-way completion

- **On the wall** (`POST /api/chores/{id}/complete`): record the local completion
  (streaks update) **and** mark the mirrored VTODO completed via the outbox.
- **In iOS** (person checks it off / Siri): the sync pull sees the VTODO
  `STATUS:COMPLETED`; map its UID → (chore, date, person) via `chore_mirror` and
  record the local completion (idempotent). Streaks update on the wall.
- Conflict/if-match handling is already server-wins in the outbox; a completion is
  effectively idempotent so there's no lost-edit risk here.

## Notifications (native)

A `due_time` puts a real time on the reminder and adds a `VALARM` at that time →
iOS fires a native notification. Multiple times/day (e.g. "feed dog 7am & 6pm") =
multiple `VALARM`s on the one occurrence, or one reminder per time (decide in
build; `VALARM`s are cleaner and keep one checkbox per chore/day).

## The one setup caveat (important, one-time, operator-only)

The wall writes to the **bot** account (`garynbot`). For a person to see *their*
list natively on *their own* iPhone, the bot must **share that list** with the
person's Apple ID — done once in the iCloud Reminders app (Share List → add
person), per person. CalDAV can create the lists but can't initiate iCloud
sharing (it's Apple-proprietary). So the flow is:

1. Wall (or operator) creates the per-person lists on the bot account.
2. **Operator shares each list** from `garynbot` to each family member's Apple ID
   (one time).
3. From then on the wall just writes into those already-shared lists; everyone
   sees + is notified + checks off natively.

If a family shares one Apple ID, step 2 is unnecessary.

## Phased build (each phase: tests + the repo's review gate before merge)

- **P1 — Recurrence + times (local only).** Schema + `chores.occurs`/`assignee_id`
  + the chore editor UI (weekly/biweekly, every-N-days, due-times). Ships value on
  the wall immediately, no iCloud yet. Fully unit-testable.
- **P2 — Person→list mapping + list bootstrap.** `people.reminder_list_id`,
  settings UI to map each person to a VTODO list (reuse the collections picker),
  optional "create list" via CalDAV `make_calendar`.
- **P3 — Mirror engine (wall → iCloud).** `chore_mirror`, the reconcile pass in
  the sync tick, VALARM/DUE rendering. Verified against the bot account with
  throwaway people, same as the reminder round-trips today.
- **P4 — Two-way completion.** Wall-complete → mark reminder done; iOS-complete →
  record local completion (streaks). The reconciliation from `chore_mirror`.
- **P5 — Polish.** Notifications tuning, horizon config, the settings copy for the
  sharing setup, and a "chores are mirrored to iCloud" status/health line.

## Risks / open questions

- **Reminder clutter**: a short horizon + prune keeps it tidy; confirm H (default 7).
- **Rotation reassignment mid-week**: deleting the old-assignee reminder must not
  fire a "completed elsewhere" false positive — only delete un-completed future ones.
- **iCloud list sharing** is manual (above) — the only non-automatable step.
- **Timezone**: due-times are local; store the wall's TZ on the VTODO so a phone in
  another TZ notifies at the intended local time (or accept device-local).
