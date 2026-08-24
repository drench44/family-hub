# family-hub

Public family wall-dashboard: FastAPI + SQLite + vanilla JS. Keep any
deployment-specific data (real calendar IDs, LAN IPs, camera URLs, secrets) out
of this repo — it's public.

## Adding a feature — run the gauntlet, every gate

Any new feature (or substantial rework of one) works through
[`docs/adding-a-feature.md`](docs/adding-a-feature.md) **as a checklist, in
order, to the last item** — registry toggle, fail-soft server side, DEMO
payload, mobile surface + tab-bar re-fit, motion/reduced-motion, tests + the
structural guards, all visual gates, README + `docs/hub.png` regenerated in
the same PR, the three-agent review (re-run if the branch grew after the
first pass), and live post-deploy verification. Every item there cites the
real shipped bug that created it; "it renders" is not "it's done". When a
gate catches something new, fix it AND add both the guard and the checklist
line that would have caught it.

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

## Weather feed (`wx.json`) — read the contract before touching the card

The weather card is fed by ONE external document, `GET {weather_base}/wx.json`,
produced by a **separate service** (not this app, not this repo — on the deploy
box it's `:8137`; the app is `:8138`). Before reading a new field off it, or
adding anything that needs forecast/history data, read
[`docs/weather-feed.md`](docs/weather-feed.md): it inventories every field the
feed actually provides, says how to pull the live feed off the deploy box, and
records the load-
bearing fact that **the feed carries no multi-day forecast** (only today's
high/low, a 24h temp curve, and a 12h AQI curve). Guessing a feed key already
cost this repo weeks of a silently-blank chart (`hourlyTemps`); the doc exists so
that never repeats. The 5-day forecast strip waits on a `dailyForecast` array the
feed does not yet emit — it renders only once the feed grows one.

## Testing the wall layout visually

The wall is a FIXED-WIDTH desktop layout: `.wrap { width: 1920px }`. It does not
shrink to fit a narrower window; it overflows. So when you debug or screenshot
the wall in a browser (or a headless/automation viewport), render it at **1920px
wide or more**, or keep the viewport in desktop mode (> 1000px) and zoom the page
out so the 1920px content fits. In a narrower window the right-hand columns
(calendar, cameras, weather, climate) scroll off-screen and their measurements
are meaningless. Several "looks fine to me" false negatives have come from
measuring a viewport that never rendered those columns; trust a real full-width
screenshot over a cramped-viewport measurement. Below 1000px the
`@media (max-width: 1000px)` mobile layout takes over (single column + the fixed
bottom tab bar). `.is-night` dims the page from 22:00 to 06:00. Because a CSS
`filter` establishes a containing block for `position: fixed` descendants, that
dim is applied to the body's children, never to `<body>` (a test guards it).
Test all of: full desktop width, the mobile breakpoint, and night mode.

## Mobile & iOS Safari — required checks for anything phone-facing

The phone reflow (`@media (max-width: 1000px)`: an app-shell — the body is a
fixed-height flex column, the content `.wrap` scrolls inside it, and the tab bar
is the in-flow bottom row) and the full-screen overlays are used on a real
iPhone. Static and fake-DOM tests can't see mobile layout OR iOS Safari
behavior, so a batch of spacing and tap bugs shipped unnoticed (2026-08-15). For
ANY change that touches mobile CSS, the tab bar, overlays, modals, or embedded
iframes, do all of:

1. **Render at a real phone width** (≤ 400px) and eyeball spacing on every tab
   (Chores, To-Dos, Calendar, Cameras, Weather) and every modal/overlay. The
   automation window here won't shrink below ~1000px, so to preview the phone
   layout temporarily widen the breakpoint in a local copy
   (`@media (max-width: 2000px)`), screenshot, then revert — never commit that.
2. **Confirm on a real iPhone before calling a mobile change done.** Several
   iOS-only bugs (below) are invisible in Chromium and in the test suite; the
   operator's phone is the real gate. Ask them to verify.

Known iOS Safari traps this repo has already hit (each has a guard test —
don't reintroduce them):

- **Don't float an interactive `position: fixed` bar over a scrolling body.**
  A fixed bottom nav over the scrolling page went untappable on iOS when
  scrolled to the bottom (its hit area misaligns / taps fall through). The fix
  was the app shell above: content scrolls in its own `.wrap` region and the tab
  bar is an in-flow row below it, so it's always tappable at any scroll
  position. Guards: `test_mobile_tabbar_stays_tappable`,
  `test_mobile_app_shell_scrolls_content_not_the_body`.
- **No `backdrop-filter` on a `position: fixed`/`sticky` element.** Taps fall
  through it intermittently. Use a solid background (put any blur on a
  non-interactive `::before`). Guard: `test_no_backdrop_filter_on_fixed_elements`.
- **No `transform` on a `position: fixed` interactive element.** It's often
  suggested as an iOS "compositing layer" fix, but on a fixed element it
  misaligns the touch target when the page is scrolled — it CAUSED the tab-bar
  bug above, don't reach for it. (On an `absolute` element layered over a
  CSS-transform-scaled iframe, `translateZ(0)` IS the right fix — iOS otherwise
  routes taps into the iframe; that's the overlay ⌂ home pill over "fit" panels,
  guarded by `test_overlay_home_pill_stays_tappable_over_scaled_iframes`.)
- **Scroll resets need every target**, not just `window.scrollTo` — zero the
  scrolling element, both document roots, and the `.wrap` app-shell container
  (`scrollPageToTop` in hub.js).
- **Tap targets ≥ ~44px** and clear of the very bottom edge (the iOS home-
  indicator / swipe zone).

Prefer encoding a new mobile/iOS fix as a static guard in `test_static.py` (or a
fake-DOM test) so it can't silently regress — the same pattern as the guards
above. A real WebKit/Playwright suite was considered and deliberately not added:
it fights this project's no-build, minimal-dependency design and its flakiness
would cost more than it catches here.

## After cloning

After cloning, run `scripts/install-hooks.sh` — installs the pre-push
privacy guard (inert without the operator's private scanner).
