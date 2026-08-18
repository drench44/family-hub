# Versioning & "What's New" — design

**Date:** 2026-08-17
**Status:** approved, then **trimmed on implementation**.

> **Trimmed on implementation.** The in-app "What's New" surface described below
> (version badge, dismissible release-notes panel, "new" dot, and `/api/version`
> returning changelog `entries`) was **cut**. The operator's direction was that
> versioning is GitHub documentation, not a family-wall feature. What shipped:
> `CHANGELOG.md` + git tags/Releases as the "what's new," an auto-publish release
> workflow, the enforced changelog guard, and — the only site-facing piece — a
> bare debug readout (`GET /api/version` → `{version, build}` and a quiet
> `family-hub v<version>` line in Settings). The panel sections below are kept as
> the original design record, not as shipped behavior.

## Goal

Make family-hub carry, enforce, and *show* a real version, the way a
professionally maintained product does. One SemVer number is the source of
truth; a Keep-a-Changelog `CHANGELOG.md` is the "what's new"; a release script
performs the bump ceremony; CI + a local hook enforce that changes update the
changelog; and the app itself surfaces its version plus a dismissible
"What's New" panel fed from that same changelog.

Constraints (from CLAUDE.md): no build step, stdlib-only Python, vanilla JS,
public repo (no house data), fail-soft on the wall (a tile/route never 500s).

## Single source of truth

- **`VERSION`** — repo-root file containing one SemVer string, e.g. `1.0.0`.
- **`family_hub.__version__`** — `__init__.py` reads `VERSION` at import
  (falling back to `"0.0.0+unknown"` if the file is missing, never raising).
- Everything downstream (API, in-app badge, cache-bust, git tag) derives from
  this. Nothing else stores a version number of its own.

Seed at **`1.0.0`**: the hub runs on the wall daily, so 1.0.0 is the honest
baseline. The first changelog entry summarizes today's real feature set
(chores, to-dos, calendar, cameras, weather, climate, laundry) — factual, no
invented history.

## CHANGELOG.md

Keep a Changelog format. Top section is `## [Unreleased]`, which every
code-changing PR adds to under `### Added` / `### Changed` / `### Fixed` /
`### Removed`. On release it rolls to `## [x.y.z] — YYYY-MM-DD` and a fresh
empty `[Unreleased]` opens above it. This file is the *single* data source for
the in-app panel — there is no second copy to drift.

## Release script — `scripts/release.py` (stdlib only)

`python scripts/release.py {major|minor|patch} [--dry-run]` does the whole
ceremony atomically, and refuses to do a partial job:

1. Precondition checks — clean working tree; `[Unreleased]` is non-empty
   (nothing to release otherwise); on a release branch. Abort loudly on any.
2. Bump `VERSION` per the SemVer part given.
3. Roll `## [Unreleased]` → `## [x.y.z] — <today>`; open a fresh empty
   `[Unreleased]`. (`today` passed in / read from the system at run time — the
   script is a CLI, not workflow code, so real dates are fine here.)
4. **Stamp every `?v=<n>` in `index.html` to the new version** — one writer,
   one number, so the css/js/theme/common/osk/hub cache-busts can never drift
   apart or lag a branch again (the bug behind commit `f712ac0`).
5. `git commit -am "release: vX.Y.Z"` then `git tag vX.Y.Z`.
6. Print the tag and the `git push --follow-tags` reminder. The script does
   **not** push (respects the pre-push privacy guard + operator control).

`--dry-run` prints the diff it would make and exits 0 without writing.

## Cache-bust unification

Today each asset has an independent hand-bumped `?v=` (`styles.css?v=80`,
`hub.js?v=79`, `theme.js?v=7`, `common.js?v=43`, `osk.js?v=1`). These collapse
to the single app version: after this change every asset is `?v=<VERSION>`.
The release script (step 4) is the only writer. `index.html` stays a static
file served by the existing `StaticFiles(html=True)` mount — **no dynamic
route, no templating, no build.** The number changes only at release time,
which is exactly when caches should bust.

A `test_static.py` guard asserts every `?v=` in `index.html` equals `VERSION`,
so a stray hand-edit that reintroduces drift fails CI.

## In-app surface (runs the full feature gauntlet)

### API — `GET /api/version`

Returns `{ "version": "1.0.0", "entries": [ {version, date, groups:{Added:[...],
Fixed:[...]}} ... ] }`. Parses `CHANGELOG.md` server-side (stdlib regex/line
parse), result cached in-process. **Fail-soft:** any parse/read error returns
`{ "version": <__version__> }` with no `entries` — the panel then simply
offers no notes; the route never raises (consistent with the tile contract).
Only released sections are exposed; `[Unreleased]` is omitted from the API.

### Version badge

- Quiet `family-hub v1.0.0` line in the **Settings overlay** (always visible).
- Small `v1.0.0` near the topbar wordmark on the wall.

### "What's New" panel

- A modal reusing the existing overlay/modal pattern (no new primitive beyond
  what the Chores/Calendar overlays already use), listing recent released
  entries from `/api/version`.
- **Auto-surfaces once per new version:** client keeps `lastSeenVersion` in
  `localStorage`. When `version` (from the API) is newer, a subtle indicator
  dot appears on the badge/gear; opening the panel and dismissing writes the
  current version as seen. No nag on repeat loads of the same version.
- Motion respects `prefers-reduced-motion` (no slide/pulse when reduced).

### Gauntlet coverage

- **Registry/toggle:** "What's New" is a core always-on affordance, not a
  togglable integration/tile — it adds no `integrations.py` descriptor and no
  wall column, so no `applyWallLayout` reflow work. (Documented as an explicit
  exception in the plan.)
- **DEMO:** `demo.py` provides a small canned changelog payload (or the real
  `CHANGELOG.md` is readable in demo) so the README screenshot / "try it" run
  shows the badge + panel populated.
- **Mobile:** badge lives in Settings + a modal, not a new tab — **no tab-bar
  re-fit needed** (tab count unchanged). Verify modal spacing at ≤400px and
  tap target ≥44px; confirm on a real iPhone before calling mobile done.
- **Tests + guards** (below), README + `docs/hub.png` regenerated in this PR.

## Enforcement (belt-and-suspenders)

### CI — `changelog-guard` job (`.github/workflows/ci.yml`)

On `pull_request`: if the diff touches `src/**` but `CHANGELOG.md`'s
`[Unreleased]` gained no new line, fail with the exact remedy printed
(`add an entry under ## [Unreleased]`). Exemptions mirror the review rule:
docs-only (`*.md`, comments) and test-only diffs don't require an entry.
Implemented as a small stdlib Python script (`scripts/check_changelog.py`) the
job runs, so the same check is reusable locally.

### Local pre-commit hook (`.githooks/pre-commit`)

Runs `scripts/check_changelog.py` against staged changes; warns + blocks with
the same message. Wired by the existing `scripts/install-hooks.sh`
(`core.hooksPath=.githooks` is already set there). Bypassable with
`--no-verify` for genuine exceptions, per git norms. Inert-safe for anyone.

## Testing

- `test_release.py` — bump math (major/minor/patch), `[Unreleased]`→dated roll
  + fresh `[Unreleased]`, `index.html` `?v=` stamping, refusal on dirty tree /
  empty unreleased. Runs the script's pure functions (no real git needed;
  git steps behind a `--dry-run`/injected-runner seam).
- `test_check_changelog.py` — src change without entry fails; docs-only passes;
  test-only passes; entry present passes.
- `test_version_api.py` — `/api/version` shape; parses a fixture changelog;
  fail-soft returns `{version}` only on a malformed changelog (no 500).
- `test_static.py` guards — every `index.html` `?v=` equals `VERSION`;
  `__version__` equals `VERSION` contents.
- `tests/js/` — What's New: shows dot when version newer than `lastSeenVersion`,
  hides after dismiss, no dot on equal version.

## Files touched

- **New:** `VERSION`, `CHANGELOG.md`, `scripts/release.py`,
  `scripts/check_changelog.py`, `.githooks/pre-commit`,
  `tests/test_release.py`, `tests/test_check_changelog.py`,
  `tests/test_version_api.py`, JS test additions.
- **Edited:** `src/family_hub/__init__.py` (`__version__`),
  `src/family_hub/app.py` (`/api/version`), `index.html` (badge + `?v=`
  unification + What's New markup), `hub.js` (panel + seen-state),
  `styles.css` (badge/panel styles), `demo.py` (canned changelog),
  `.github/workflows/ci.yml` (`changelog-guard` job), `test_static.py`
  (guards), `README.md` + `docs/hub.png`, `docs/adding-a-feature.md`
  (new checklist line: "added a CHANGELOG `[Unreleased]` entry").

## Rejected alternatives

- **Dynamic route serving a templated `index.html`** to inject the version —
  breaks the pure-static mount for no benefit; caches only need busting at
  release, which the script already handles.
- **Generated `whatsnew.json` at release time** — a second artifact that can
  drift from `CHANGELOG.md`; runtime parse of the one file is simpler and
  always in sync.
- **Auto-deriving the SemVer bump from conventional commits** — more tooling
  to trust for little gain; the operator/Claude decides the bump explicitly.
