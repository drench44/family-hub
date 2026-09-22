# Adding a feature — the complete gauntlet

Every gate below exists because skipping it already shipped a real bug or an
incomplete surface. The laundry feature (PRs #50/#52, 2026-08-17) ran this
entire arc and is the worked example each item cites. **A feature is not done
when it renders — it is done when every section here is checked.**

Work through the sections in order. CLAUDE.md binds Claude sessions to this
document; human contributors should treat it the same way.

---

## 1. Registry + toggle (the feature must be switchable OFF)

- [ ] Add a descriptor in `src/family_hub/integrations.py` —
      `available` computed from config/env (a feature with no config must not
      appear at all), `group: "feature"` for core surfaces or
      `"integration"` for external services.
- [ ] That one descriptor gives you the Settings switch, seeding, and
      `body.integ-off-<id>` stamping for free — do **not** build a parallel
      toggle mechanism.
- [ ] Add the CSS hook that hides the feature's wall surface:
      `body.integ-off-<id> #<your-slot> { display: none; }`.
- [ ] Wire the wall column logic in `applyWallLayout` (hub.js) so a disabled
      feature's column reflows away instead of leaving a hole.
- [ ] Test the toggle round-trip live: PATCH off → surface hides + tab
      hides + wall reflows; PATCH on → instant return, no refetch gap.

*Why: the toggle machinery (#49) is registry-driven; features that bypass it
can never be turned off and don't degrade gracefully.*

## 2. Server side (fail-soft, or nothing)

- [ ] Data flows through a tile proxy in `tiles.py`: trimmed payload, 3s
      timeouts, short in-process cache, **errors never cached**, and the
      function **never raises** — the routes have no global handler; a raise
      is a 500 on the wall.
- [ ] Guard *valid-but-wrong* upstream shapes: non-dict JSON bodies,
      non-string states, unparseable timestamps. (Laundry: a numeric state
      raised `TypeError` out of the "never raises" tile until reviewed.)
- [ ] Unknown upstream vocabulary fails OPEN toward the active state only on
      strong evidence (laundry: a *future* finish time), else quiet — never
      pin a dead device "running" off stale data.
- [ ] Persistent memory (kv) writes only on observed **transitions**, never
      on repeated states — upstream restarts reset `last_changed` and will
      overwrite real history otherwise. (Laundry: an HA restart would have
      replaced the true 9pm finish with the 3am restart time.)
- [ ] Secrets come from env vars, never config.json. **Add the var to the
      `environment:` allowlist in docker-compose.yml AND `.env.example`** —
      a var missing from the allowlist is invisible inside the container and
      the feature silently can't turn on. (Laundry shipped without it; the
      integration could never enable in Docker until review caught it.)
- [ ] An integration whose credential lives in an env var **stays listed when
      that credential is missing**, carrying a `needs_auth` status. It must
      not drop out of the registry. Absent-from-settings is not an error
      state: it reads as "this hub has no laundry" instead of "this laundry
      needs a token", and the card silently vanishes from the wall with it.
      (2026-09-17: a deploy deleted the box's `.env`; the washer/dryer card
      and its settings row both disappeared and `/health` stayed 200 for a
      day.) Pair it with a startup line for "configured but no credential"
      and an escalation for "credential rejected", which never heals itself.
      **The exception is a credential the operator can supply from the wall
      itself**: `icloud_caldav` drops out of the registry with no credentials
      on purpose, because the Settings overlay renders its connect panel
      regardless (`caldavPanelHtml`), so "absent" there means "not set up
      yet" and has a visible way to fix it. Laundry's token can only arrive
      out of band, in the box's `.env`, so its absence has to be reported.
      If a new integration gains an in-app connect flow, it may follow
      CalDAV; until then it follows laundry.
- [ ] `config.example.json` gains a placeholder entry (use the
      `192.168.1.50` example-IP convention — this repo is public; never a
      real LAN IP, never house data).
- [ ] Config cleaning logs every dropped/malformed entry loudly — a typo'd
      key must not silently vanish a machine or the whole integration.

## 3. DEMO mode

- [ ] `demo.py` serves a live-shaped canned payload showing the feature's
      **best states** (laundry: washer mid-cycle + dryer just done), with
      times computed relative to now so screenshots always look current.
- [ ] Add the feature id to the DEMO availability overlay in `app.py`
      (`demo_ids`) or the settings row and surface won't exist in demo runs.
- [ ] `tests/test_demo.py` asserts the demo payload is live-shaped.

*Why: DEMO is the README screenshot, the "try it" run, and every visual
gate below. A feature absent from demo is invisible everywhere that matters.*

## 4. Mobile — a first-class surface, not an afterthought

- [ ] Decide the phone home: its own tab (add to `TAB_FEATURES`, a tab-bar
      button in index.html, visibility rules in the phone-shell CSS) or a
      slot inside an existing tab. Either way the tab logic must hide it
      when the feature is off, with active-tab fallback.
- [ ] **Re-fit the tab bar**: the small-type media query is width-tuned to
      the tab *count*. Adding a sixth tab overflowed `CALENDAR` at every
      non-Max iPhone width because the breakpoint still assumed five.
      Measure the longest label at 390px, adjust the breakpoint, update its
      comment with the new count.
- [ ] Render at a real phone width (≤400px) via the temporary
      widen-the-breakpoint trick in CLAUDE.md — check every tab, then
      REVERT (the brace/breakpoint guards will fail CI if you forget).
- [ ] Tap targets ≥44px; obey every iOS trap listed in CLAUDE.md.
- [ ] **A real iPhone confirms before "done"** — fake-DOM tests cannot see
      mobile layout. Ask the operator.

## 5. Motion & accessibility (if the feature animates)

- [ ] Transform/opacity-only animations (compositor-friendly — the wall is
      modest hardware). No layout-triggering properties on a timer.
- [ ] Every animated class is neutralized under
      `@media (prefers-reduced-motion: reduce)` **and** paused by the
      feature's paused state. The static guard enumerates the classes —
      extend the roster when you add one. (Laundry's rewrite added six
      animated classes; the guard only knew about one.)
- [ ] Any constant shared between the renderer and an in-place updater
      (radii, circumferences, counts) lives in ONE module-scope const.
      (The countdown dial under-read by 7.4% because render and tick each
      had their own ring radius.)

## 6. Tests — and the guards that keep the guards honest

- [ ] Python: registry availability, tile fail-soft (including garbage
      shapes), route behavior, kv transitions, demo shape.
- [ ] JS fake-DOM: render states, toggle/tab visibility + fallback,
      in-place updates (assert element identity, not markup), XSS of every
      config-sourced string through `escapeHtml`.
- [ ] Static guards in `test_static.py` for load-bearing wiring: the
      `integ-off` hook, tab mapping, reduced-motion roster.
- [ ] **String-built SVG/HTML needs structural assertions**: balanced
      `<g>`/`</g>` counts, expected element counts, unique per-instance SVG
      ids. Browsers never validate this for you; a doubled group shipped and
      silently double-animated the heap and mis-parented the water.
- [ ] `test_css_braces_balanced` stays green — regex guards match raw text,
      not parsed CSS, so stray braces from edit splices can disable whole
      blocks while every guard passes. Never delete this test.
- [ ] Run both suites under `TZ=UTC` too. Time-formatting tests anchored to
      an absolute instant fail on CI's UTC runners; anchor fixtures to
      local wall-clock components. (Three laundry tests turned CI red.)
- [ ] Verify the suites genuinely RUN — a skip is not a pass.

## 7. Visual gates (all of them, on the demo)

- [ ] Full-width wall (≥1920px), every feature state forced
      (running/done/idle/paused/error/offline or your equivalents).
- [ ] Phone width, every tab.
- [ ] Light + dark themes (all five modes resolve through tokens; check at
      least grey and light — glass/edge contrast differs wildly).
- [ ] Night mode.
- [ ] If it animates: **watch it move** — or better, sample element
      positions numerically over a cycle. Stills hid a chord-cutting
      trajectory that looked fine frozen and wrong in motion.
- [ ] **Clear the test browser's cache first.** Assets keep the same `?v=`
      between releases, so an automation browser runs old JS/CSS against
      new files. Twice, seasonal looks showed a "bug" that was only the cache
      (Playwright: `Network.clearBrowserCache` over CDP).
- [ ] **All five themes** (Light, Soft, Blue, Grey, Black), not just "a light
      and a dark". Seasonal looks once made Blue, Grey and Black identical,
      and only a five-way check showed it.
- [ ] **Anything the feature can switch off must not move a pixel when
      off.** Screenshot the off state on main and on the branch, all five
      themes, and diff them.
- [ ] **Phone with the widest real content**: 390px and 360px, and the
      clock at "12:59:59pm". A one-digit hour hid an 8px sideways overflow.
- [ ] **Reduced motion and night are states too.** Look at them. Anything
      that animates over the UI must not rest over text when stilled.
      A paused animation is only paused where you named it: check pseudo-
      elements too (a far bat hung mid-sky every night, still flapping).
- [ ] **The wall runs Firefox ESR.** Automation screenshots of Firefox skip
      `backdrop-filter`, so check glass in Chromium, and look at the wall
      itself for anything Firefox-specific.
- [ ] **Open every menu over the new UI.** A new layer (backdrop-filter,
      transform, opacity) makes a stacking context, and the gear popover
      once opened underneath the cards.
- [ ] **Asked "have you looked at all of them?"**, the answer must be yes,
      at full size. A thumbnail grid is not a review.

## 8. Docs & screenshots — updated IN THE SAME PR

- [ ] README: feature bullet in "What it looks like" + a row in the
      config.json reference.
- [ ] **Regenerate `docs/hub.png`** from the demo at 1920px wide,
      full-page, with the feature showing its best state. Procedure: run
      `DEMO=1 DISABLE_SYNC=1 CONFIG_PATH=config.demo.json` on a spare port,
      screenshot at 1920×(content height). A README screenshot of the
      previous design is documentation rot.
- [ ] `docs/phone.png` only if the phone home screen changed (it shows the
      Chores tab).
- [ ] `.env.example` documents any new env var with how to obtain it.

## 9. Review gate (CLAUDE.md's — restated because it caught everything)

- [ ] All three agents on the branch diff **including late commits** — the
      laundry visual rewrites accumulated four real bugs *after* the first
      review wave; the second wave caught them all. Re-review when the
      branch has grown substantially since the last pass.
- [ ] Every finding fixed (or explicitly rejected with reasoning) before
      merge. Each genuine bug also gets the guard that would have caught it.

## 10. Release

- [ ] **Add a `## [Unreleased]` entry in `CHANGELOG.md`** describing the
      change (`### Added/Changed/Fixed`). This is enforced: the CI
      `changelog-guard` job and the local `.githooks/pre-commit` hook both
      BLOCK a PR that touches `src/**` without a new entry (docs-only /
      test-only diffs are exempt). Run `scripts/install-hooks.sh` once so the
      local hook is active.
- [ ] Cut the version with `python scripts/release.py {major|minor|patch}` —
      it bumps `VERSION`, rolls `[Unreleased]` into a dated release, **stamps
      every `?v=` in index.html to the new version**, commits, and tags. Do
      NOT hand-edit the `?v=` numbers: they are unified to the app version now,
      so one command busts every asset at once (a `test_static.py` guard fails
      any drift). This replaced the old per-asset manual bump that let two
      branches mint the same `v78` and ship stale caches.
- [ ] Assets referenced from INSIDE a stylesheet or script (`url(...)` in
      styles.css, an image path built in hub.js) get no `?v=`. If they can
      change under the same name, serve them `no-cache` from the
      `html_no_cache` middleware in app.py and add a test. (The seasonal
      look art under `/seasons/` is the first case: a photo can be swapped
      under the same name.)
- [ ] New third-party art or code: record source, changes and licence in a
      CREDITS file next to it, including the full notice when the licence
      asks for it (MIT does), and run the full Python suite AFTER committing:
      `test_no_house_data` only scans tracked files, and dense SVG path data
      can look like an IP address to it.
- [ ] Scripts, stylesheets and the manifest are served `no-cache` (ETag
      304s keep it cheap), so a deploy reload always runs the new code even
      when `?v=` did not move. Still treat a deploy as a release (at least a
      `patch`) so the version line tells you what is running. The wall
      auto-reloads on the build hash, which folds in the loaded config too:
      a config change (new camera, moved panel) reloads open walls once idle,
      because cameras and panels are built once per page load.
- [ ] Anything that fetches in the background and repaints (a poll, a day
      browser, a stream fallback) must drop a reply that lands after a
      newer one: number the requests and ignore the stale ones. An older
      `/api/hub` reply once repainted a checked chore as undone and fired the
      confetti twice (see `pollSeq`, `choresFullSeq`, `lnEpoch` in hub.js).
- [ ] A new modal or popover goes in the set `surfaceOpen()` and
      `wallBusy()` read, closes on idle and on Escape, is a
      `role="dialog"` with `aria-modal`, and moves focus in on open and back
      on close (`dialogOpened` / `dialogClosed`). One left out of the idle
      return blocks the deploy auto-reload for good.
- [ ] Privacy scan clean, CI green, PR merged.
- [ ] Deploy via the deployment overlay's `deploy.sh` (frontend is BAKED
      into the image — a bare restart ships nothing).
- [ ] Live verification: served asset versions match, the feature's API
      answers with real data, the wall renders with zero console errors.
- [ ] Real-hardware checks the operator must confirm: the physical wall
      panel (its gamma crushes dark tones — "looks fine here" isn't proof)
      and a real iPhone for anything mobile.
- [ ] Clean up: demo servers, temp screenshots, scratch files, merged
      branches.

---

**The meta-rule.** Most of the bugs above were invisible to a green test
suite: text-matching guards can't see parse errors, fake DOMs can't see
layout, stills can't see motion, and your own machine can't see the wall
panel or an iPhone. Whenever a gate catches something, don't just fix it —
add the guard that would have caught it, and add the lesson here.
