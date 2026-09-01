# iOS Chrome tab-bar gap: root cause and fix plan

> **Status (2026-08-31): BUILT** on branch `fix/ios-tabbar-gap-selfheal`. All
> three parts shipped — the self-heal reload + diagnostics in `hub.js`, the
> `/api/diag/viewport` endpoint in `app.py`, and the home-screen manifest +
> Apple meta + icons. Guards: `tests/js/hub-dom.test.mjs` (self-heal decision
> table), `tests/test_diag_viewport.py`, and `test_static.py`
> (`test_home_screen_manifest_and_apple_meta`, `test_tabbar_gap_self_heal_is_wired`).
> Still needs the one thing code can't do: on-device confirmation on the
> operator's iPhone.

## The bug
On iPhone (iOS 26.x, Chrome, address bar at top) a backgrounded or stale hub tab
comes back with the phone tab bar stuck ~80px up the screen over a black gap.
Only a manual reload fixes it. Rotation does not. Three fixes failed:
#45 (100dvh + --app-h), #53 (visualViewport + settle timers), #80 (position:fixed shell, v1.3.5).

## Root cause (high confidence)
Rotation forcing a full re-layout does NOT cure it, so the page layout is not stale.
The wrong height lives in Chrome's per-tab state: its bottom-toolbar inset
bookkeeping (78pt = 44pt toolbar + 34pt home indicator) stays applied after the
tab is restored, so every layout Chrome hands the page is consistently ~80px
short. innerHeight, visualViewport.height and clientHeight all agree on the wrong
number, so nothing the page can measure reveals the true glass. Reload resets
Chrome's tab state, which is why only a manual reload works and why no CSS
change can. Known WebKit/iOS 26 bug class (Apple forum 799216, bug 158055568,
"bottom gap ... as if toolbar still occupying space when hidden").

## Fix plan (one PR, phone-shell review band, 3 review agents)
1. Self-heal reload on wake. On load / pageshow / visibilitychange->visible:
   compare innerHeight to the largest steady portrait height this device has
   reported (localStorage, per orientation, capped at screen.height). If it is
   60-100px short and stays short for 1.5s, with no text field focused and no
   unsaved todo text, call location.reload() once. One-shot guard in
   sessionStorage: if still short after our reload, accept it as the new normal
   (lower the stored max) and never reload again for that value. This never
   sizes the shell from a measurement, so it respects the #80 CLAUDE.md rule.
2. Web app manifest (display: standalone, icons, apple-mobile-web-app meta) so
   the hub can be added to the home screen from Chrome. No Chrome toolbars at
   all, so the state that goes wrong does not exist. Structural fix; #1 covers
   opening it in a Chrome tab anyway.
3. Diagnostics: a viewport line in Settings (innerHeight, visualViewport,
   screen, tabbar rect bottom, stored last-good, reason for last self-reload)
   and the same numbers POSTed to a small /api/diag endpoint on every wake, so
   the next occurrence is read off the box instead of guessed.

## Open point
Unverified on device: that a JS location.reload() clears Chrome's tab state the
same way the reload button does. Both are a normal WKWebView reload (unlike the
discarded-tab restore that caused #53), so expected yes; the diag log proves it
on the first occurrence.

## Process
Git worktree, docs/adding-a-feature.md gauntlet end to end, PR, review agents,
stop before merge (no merge without go-ahead).
