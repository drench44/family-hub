# Changelog

All notable changes to family-hub are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Added
- On-screen keyboard: a ✕ Cancel key beside Done that closes the keyboard
  without saving and clears what you typed — for when you change your mind.
- On-screen keyboard: two-page symbol layer (`?123` / `#+=`) and a categorized
  emoji picker — a tab strip (smileys, people, animals, nature, food, activity,
  travel, objects, symbols) over a scrollable grid, plus a 🕐 Recently-used tab
  that remembers your go-to emojis. Backspace is grapheme-aware, so one tap
  deletes a whole emoji. Every emoji is color-verified against the wall's font.

### Fixed
- On-screen keyboard can be re-summoned after Cancel/Done: the keyboard now
  blurs the field when it hides, so tapping the same box again brings it back
  (before, the still-focused field fired no focusin and a later Done no-oped).
  Added `scripts/wall-smoke-test.py` — a Marionette smoke test that drives the
  real wall Firefox, which is how this was caught.
- On-screen keyboard is now **wall-only** and works on a wall whose browser
  reports itself mouse-only (Firefox under Wayland delivers the touchscreen as a
  mouse, so the old touch gate never fired there and the OS keyboard took over).
  Open the hub on the wall once with `?kiosk=1` to turn it on (remembered
  thereafter; `?kiosk=0` clears it); the served fields are then marked read-only
  so the OS keyboard stays out of the way. It no longer appears on phones or
  laptops, which keep their own keyboard. See `docs/on-screen-keyboard.md`.
- On-screen keyboard no longer needs a second tap to appear: a background to-do
  refresh was rebuilding the focused input and dismissing the keyboard the
  instant it opened.

## [1.3.0] — 2026-08-18

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

### Changed
- Laundry is now real-time. A server-side watcher polls Home Assistant every
  5 seconds for the whole cycle (not just near a projected finish) and pushes
  every change to open walls over a live stream (`GET /api/laundry/stream`),
  so the card reflects a machine's actual status within seconds instead of up
  to ~1.5 minutes. Finish detection and the cycle log no longer depend on a
  browser being open — the server observes every transition itself. The old
  endgame fast lane (chained 10s re-polls + a two-speed server cache) is
  retired; the 60s poll remains as a fallback.

### Fixed
- An observed washer/dryer finish now keeps showing **Done** through the
  machine's own auto power-off (LG machines turn themselves off 30–90s
  after the end-of-cycle chime with the load still inside), for the same
  30-minute hold a missed finish gets. Previously a perfectly observed
  finish showed Done for barely a minute — and the real-time watcher
  observes every finish, so every finish took that short path. A person
  powering the machine on clears Done immediately — including mid-hold —
  and a stale end stamp is never re-presented as a fresh Done (the refusal
  is recorded in the cycle log).
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

## [1.2.1] — 2026-08-18

### Fixed
- Weather sky clouds now drift smoothly at any width. The drift animation exited
  at a fixed offset tuned to the narrow desktop column, so on the wider
  mobile/full-screen weather view a cloud was still mid-sky when it snapped back
  to the left — a visible "reset". The exit is now relative to the sky's own
  width, so the wrap-around always happens off-screen.
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
