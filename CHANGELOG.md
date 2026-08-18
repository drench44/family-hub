# Changelog

All notable changes to family-hub are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Fixed
- An observed washer/dryer finish now keeps showing **Done** through the
  machine's own auto power-off (LG machines turn themselves off 30–90s
  after the end-of-cycle chime with the load still inside), for the same
  30-minute hold a missed finish gets. Previously a perfectly observed
  finish showed Done for barely a minute — and the real-time watcher below
  observes every finish, so every finish took that short path. A person
  powering the machine on still clears Done immediately, and a stale end
  stamp is never re-presented as a fresh Done.

### Changed
- Laundry is now real-time. A server-side watcher polls Home Assistant every
  5 seconds for the whole cycle (not just near a projected finish) and pushes
  every change to open walls over a live stream (`GET /api/laundry/stream`),
  so the card reflects a machine's actual status within seconds instead of up
  to ~1.5 minutes. Finish detection and the cycle log no longer depend on a
  browser being open — the server observes every transition itself. The old
  endgame fast lane (chained 10s re-polls + a two-speed server cache) is
  retired; the 60s poll remains as a fallback.

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
- Native chores: deleting (or unmapping) the last person whose chores mirror to
  iCloud no longer orphans their reminders — the mirror reconcile now still prunes
  existing rows when nothing is mapped, instead of early-returning. (Caught in the
  live deploy verification.)

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
