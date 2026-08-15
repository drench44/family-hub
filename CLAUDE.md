# family-hub

Public family wall-dashboard: FastAPI + SQLite + vanilla JS. Keep any
deployment-specific data (real calendar IDs, LAN IPs, camera URLs, secrets) out
of this repo — it's public.

## Code review before merge — required

Any substantive change in this repo goes through review BEFORE it merges (or
opens as a PR). Run all three review agents on the branch diff and address real
findings:

- `pr-review-toolkit:silent-failure-hunter` — swallowed errors, weak fallbacks,
  silent wrong-but-reassuring outcomes
- `pr-review-toolkit:code-reviewer` — guideline/style/best-practice adherence,
  dead code, public-repo leaks
- `pr-review-toolkit:pr-test-analyzer` — test-coverage quality; flag tests that
  skip silently in CI or assert nothing

Also do a per-change review, and a whole-branch review for multi-task work.
Verify tests genuinely RUN (not silently skipped). This is the default gate — it
should happen without being asked. Docs-only changes (`*.md`, comments) are
exempt.

## Testing the wall layout visually

The wall is a FIXED-WIDTH desktop layout: `.wrap { width: 1880px }`. It does not
shrink to fit a narrower window; it overflows. So when you debug or screenshot
the wall in a browser (or a headless/automation viewport), render it at **1880px
wide or more**, or keep the viewport in desktop mode (> 1000px) and zoom the page
out so the 1880px content fits. In a narrower window the right-hand columns
(calendar, cameras, weather, climate) scroll off-screen and their measurements
are meaningless. Several "looks fine to me" false negatives have come from
measuring a viewport that never rendered those columns; trust a real full-width
screenshot over a cramped-viewport measurement. Below 1000px the
`@media (max-width: 1000px)` mobile layout takes over (single column + the fixed
bottom tab bar). `.is-night` dims the page from 22:00 to 06:00. Because a CSS
`filter` establishes a containing block for `position: fixed` descendants, that
dim is applied to the body's children, never to `<body>` (a test guards it).
Test all of: full desktop width, the mobile breakpoint, and night mode.

## After cloning

After cloning, run `scripts/install-hooks.sh` — installs the pre-push
privacy guard (inert without the operator's private scanner).
