# Changelog

All notable changes to family-hub are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Added
- Away / pause mode for chores: mark a family member away (open-ended — set it
  when they leave, tap "I'm back" when they return, back-date it if you forgot)
  and their away days read as rest, so a trip never breaks a streak. Rotation
  turns fall to whoever's home; a fixed chore can pass to an optional backup,
  who is shown "covering for" it and gets the streak credit — on the wall AND
  through the iCloud mirror (the reminder moves to the backup's list and an iOS
  check-off credits them). "Pause everyone" covers whole-family trips. Away is
  a pure overlay over the frozen history: nothing recorded is ever rewritten,
  and deleting an away period restores exactly what was there before.

### Fixed
- Native chores: deleting (or unmapping) the last person whose chores mirror to
  iCloud no longer orphans their reminders — the mirror reconcile now still prunes
  existing rows when nothing is mapped, instead of early-returning. (Caught in the
  live deploy verification.)
- A chore mirror error left latched from an earlier two-way tick no longer haunts
  the settings row forever: switching iCloud back to read-only now clears the
  stale error on the next tick.
- Coming home no longer costs you your streak. If someone covered your chore
  while you were away and you tap "I'm back" the same day, the day now counts
  as finished for you, exactly as the tick on your card already showed. The
  same fix keeps the covering person's day whole when someone leaves mid-day.
- A one-time chore that falls inside an away stretch with nobody to cover it
  no longer disappears for good — it stays on the away person's card (and
  their phone) instead of pausing into a day that never comes back.
- Deactivating someone who is still marked away no longer leaves their chores
  parked on their fill-in forever.
- Chores on your phone follow the away overlay properly: checking one off in
  Reminders now credits whoever the wall says owns it today, a chore you
  finish on the wall is no longer marked done on the away person's phone, and
  a reminder you already completed never reopens itself.
- A chore mirror that fails now says so in settings instead of failing quietly
  behind a green badge.
- The fill-in picker only offers people who can actually cover — nobody who
  has left the household or is away themselves — so a covered chore can't
  quietly disappear.
- "Pause everyone" no longer locks the chore editor: you can still add and
  edit chores for someone while they're away.
- Browsing back to a past day still shows who the fill-in was covering for,
  long after the trip has ended.
- The day browser now carries the same "away status unavailable" note the
  main screen shows, and tapping a chore off while that's broken asks you to
  try again instead of crediting the wrong person.
- `chore_mirror_horizon_days` (how far ahead chores are pushed to each phone)
  is now a real setting in `config.json`, documented in the example file.
- `changelog-guard` no longer fails a release PR: a diff that bumps
  `VERSION` (a `scripts/release.py` release, which rolls `[Unreleased]`
  rather than adding a bullet) is now exempt. Releases can PR on their own.

## [1.2.0] — 2026-08-17

### Added
- Native chores: chore routines now mirror into each person's iCloud Reminders
  list, two-way. New routine types — every-N-days, biweekly, and due-time
  notifications — in the chore editor; a per-person iCloud-list mapping in the
  Chores admin. The wall stays the source of truth (rotation, streaks, frozen
  history) while chores appear natively on each iPhone (Reminders app, Siri,
  notifications): check one off on the wall or in iOS and both stay in sync, edit
  a chore and its reminders update, and rotation hands each occurrence to the
  next person's list automatically. Read-only by default; two-way is opt-in.
  Requires a one-time per-person list share from the hub's iCloud account.

### Fixed
- `scripts/release.py` no longer prints "restored files … nothing committed" when
  the post-commit-failure `git checkout` restore *also* fails — that false
  clean-tree claim could lead to re-running release on a partially-written tree.
  It now reports the double failure and points at `git status`.

## [1.1.0] — 2026-08-17

### Added
- Backup-health badge: the wall header shows an amber "Backup stale" pill once
  the last successful `hub.db` backup is older than `BACKUP_STALE_HOURS`
  (default 36h) — hidden while healthy. A successful backup records a heartbeat
  in `hub.db`, `/api/hub` carries the status (fails-soft), and a stale heartbeat
  also catches a backup that stopped running entirely.
- Versioning system: a single `VERSION` source of truth, this changelog, git
  tags, and a `scripts/release.py` bump-roll-tag ceremony.
- Enforced changelog: a CI `changelog-guard` job and a local pre-commit hook
  block a `src/**` change that adds no `[Unreleased]` entry.
- Auto-published GitHub Releases: pushing a `vX.Y.Z` tag publishes a Release
  whose notes are that version's changelog section.
- A debug/ops version readout: `GET /api/version` returns `{version, build}`,
  and a quiet `family-hub v<version>` line shows at the foot of the Settings
  overlay. No changelog on the wall — releases live on GitHub.
- README CI / latest-release / license badges.

### Changed
- Static asset cache-busting is unified to the app version (`?v=<version>`), so
  the css/js cache-busts can no longer drift apart or lag a branch.

### Fixed
- The release-tooling dry-run test no longer pins itself to the live repo's
  `[Unreleased]` state, so the first real `scripts/release.py` cut (which empties
  `[Unreleased]`) doesn't break the test suite.

## [1.0.0] — 2026-08-17

The baseline: the family wall-dashboard as it runs in production.

### Added
- Chores wall with per-person cards, rotations, streaks, and a week strip.
- To-Dos with Now/Soon/Later tiers and a bounded wall digest.
- Calendar (Google + iCloud/CalDAV) with a full-screen overlay.
- Live camera tiles (go2rtc / Wyze bridge) and a full-screen camera grid.
- Weather, climate, and laundry tiles, each fail-soft and demo-shaped.
- A registry-driven Settings surface: every feature and integration toggles off.
- Mobile app-shell reflow with a fixed tab bar, plus a DEMO mode for screenshots.
