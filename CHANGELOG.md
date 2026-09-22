# Changelog

All notable changes to family-hub are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Fixed
- When the away list can't be read, the wall no longer saves that day's
  chore plan into history. A plan built without it could record the wrong
  person as the owner for good.
- Checking off a chore just after midnight now counts for the day the wall
  is showing. Before, it landed on the new day and the tick disappeared.
- If a chore tap is for a day that can no longer be changed, the wall says
  the day has ended instead of asking you to tap again.
- Tapping "I'm back" on the same day as "Going away" now works. The away
  time never started, so it is simply removed. Before, it failed every time.
- An iCloud reminder due at a set time in the evening now shows under the
  right day. Times are read in the hub's own time zone, so a 7pm reminder no
  longer shows up as tomorrow's.
- The backup badge now warns when the copy to the NAS fails or goes stale.
  Before, it only looked at the local snapshot, so it read healthy even when
  every NAS copy was failing.
- The backup now checks that the NAS is actually mounted before copying to
  it. If the mount had dropped, it used to copy onto the box's own disk and
  report success. Now that counts as a failed NAS copy.
- The washer and dryer watcher no longer writes to the database every five
  seconds when nothing changed (about 35,000 needless writes a day), and its
  database work no longer holds up the rest of the hub while it runs.
- The laundry watcher's "still watching" time stamp is saved every 30
  seconds instead of every 5, which cuts another ~17,000 writes a day.
- An iCloud reminder edited on the wall at the same moment a sync pulls it
  can no longer be quietly thrown away. The edit now always waits to be sent.
- "Test connection" for iCloud no longer runs a second sync on top of the
  one already running in the background. It waits its turn, or says a sync
  is already running.
- The Google Calendar sign-in file is now saved all at once, readable only by
  its owner. A crash or full disk while saving can no longer leave half a
  file that looks like the calendar was never connected.
- The health check now makes sure the database can be read. Before, it said
  "ok" even when the database was missing or broken.
- The hub's log is much smaller. It no longer writes a line for every check
  on the washer and dryer, which was about two thirds of it.
- Every container's log is now capped (5 files of 10 MB), and the camera
  relay gets 256 MB of memory instead of 128 MB, which it kept running out of.
- A config change (a new camera, a moved panel) now reaches a wall that is
  already open: the wall reloads itself once it is idle, the same way it does
  after a deploy. Before, cameras and panels kept their old setup until
  someone refreshed by hand.
- After a deploy the wall always loads the new scripts and styles, even when
  the release number did not change. Before, the browser could keep running
  the old script from its cache.

## [1.7.0] — 2026-09-22

### Added
- Laundry: a finished wash now reads **Waiting** (amber) after its half hour
  as Done, until the load is moved: the dryer starting or the washer being
  turned on ends it, and it gives up after 12 hours. In this house's own
  cycle log the wet load sat a median of about 100 minutes, and the wall used
  to call the washer "Idle" the whole time. A dryer starting also ends the
  washer's green Done early.
- Laundry: errors name the fault when the machine reports one ("won't
  drain", "load is unbalanced", "door is open"), and a delayed start says
  when it will start. Both read optional Home Assistant entities
  (`error_entity`, `start_entity`); leave them out and nothing changes.

### Changed
- Laundry: the ring shows the share of the cycle left, against the machine's
  own cycle length (optional `total_entity`), instead of a 60-minute dial
  that sat full and frozen for the first 50 minutes of a long wash.
- Laundry: plain words for what the machine is doing ("Sensing load",
  "Draining", "Cooling down"). Load sensing shows "Starting" instead of a
  time that is 15 to 20 minutes too long, and a dryer holding "1 min" while
  it cools reads "Cooling, almost done".

### Fixed
- Laundry: the dryer no longer shows "1 min" (once, "6 min") for the first
  minutes of every load. That is LG's placeholder; the card now uses the
  cycle's real length until the machine reports a real finish time.
- Laundry: the placeholder fix only acts on a cycle start the hub actually
  saw, in the cycle's first minutes, so a hub restarted mid-cycle never moves
  a real finish time, and a dryer's wrinkle-care tumble never counts as a
  new load.
- Laundry: a cycle that began while Home Assistant was unreachable, or
  while the hub was restarting, is not mistaken for one the hub saw start.

## [1.6.1] — 2026-09-22

### Changed
- Fall leaves drift sideways and tip in 3D as they fall, instead of sliding
  down straight lanes flat to the glass.
- Halloween's spider on a thread now hangs head-down from its abdomen, the
  way real ones do, with its legs still while the silk pays out and working
  on the climb back. One bat's wingbeat was slowed so it no longer stutters.

### Fixed
- At night some far bats hung frozen mid-sky with their wings still beating.
  They now go at night, like the rest of the moving layer.
- A checked-off to-do now leaves the list five minutes after it's checked
  (tap it again inside that window to undo) and moves to "recently done",
  instead of staying on the wall, struck through, until midnight.
- A phone (or the wall's full list) left open on the To-Dos view now picks up
  check-offs made elsewhere on the next refresh, instead of showing them as
  open until it is closed and reopened.
- Turning iCloud off in Settings no longer leaves the To-Do card showing an
  empty iCloud list; it shows the local list until iCloud is switched back on.

## [1.6.0] — 2026-09-19

### Added
- Halloween seasonal looks, October 1 to 31 (inside fall, which keeps the
  rest of its window). Five photographs — **Lantern Night** (the default),
  **Witching Hour**, **Haunted Pines**, **Moonrise** and **Bare Branches** —
  each with its own accent (pumpkin orange, violet, neon green, amber) and a
  purple-and-green haze over the photo. Bats cross the sky on a real drawn
  wingbeat, one spider lets itself down on its thread and climbs back, another
  lives on the glass and wanders it all day, and fine webs hang in two
  corners. Nothing moves at night or for reduced motion, nothing can take a
  tap, and everything shrinks on a phone.

### Fixed
- A seasonal creature driven from JavaScript can no longer be left frozen
  on the wall with its legs still moving: every move now stops cleanly
  however it ends, and the spiders stand down the moment their layer goes
  (no look painted, night, reduced motion, or a background tab).
- The bats' wingbeat is drawn by the graphics chip rather than repainted
  every frame, which matters on the wall's small integrated graphics.
- On a phone in October, the Settings preview tiles showed no cobwebs: the
  wall's own placement was reaching inside the tiles.

### Changed
- Seasonal glass is more transparent in every theme (Light 80 to 66% opacity,
  Soft 82 to 68, Blue 70 to 56, Grey 68 to 54, Black 76 to 62), with a little
  more blur (22px to 26px) so small text stays clean, and a touch more
  contrast in the card edges to hold the empty checkbox rings: more of the
  photograph shows through. Affects fall as well as Halloween. With **Season** off the
  wall is unchanged.

## [1.5.0] — 2026-09-19

### Added
- Seasonal looks. Turn **Season** on in the gear menu and the wall follows the
  calendar: a real photograph fills the screen, the dashboard floats on it as
  frosted glass (cards, section titles and the top bar), leaves fall at two
  depths (big, sharp ones drifting over the cards and smaller ones behind the
  glass, softened by it), and a small mark sits beside the wordmark. Fall
  (Sep 1 to Nov 30) ships **Aspen Grove** (the default), **Misty Road** and
  **Maple Sky**, picked per device from live preview tiles under All settings,
  each crediting its photographer. A house can turn seasons on for every
  fresh device with `theme.season` in config.json.
- Every theme keeps its own character with a season on: Light is white glass,
  Soft warm cream, Blue navy, Grey neutral grey and Black near-black over a
  deeper dusk, with each theme's own text and border colours. The look brings
  the photo, the leaves and an accent matched to the photo.
- Quiet by design: the leaves never block a tap and stay under the top bar,
  every menu and the phone's tab bar. At night the near leaves go away, the
  far ones pause and the glass turns nearly solid. With reduced motion the
  near leaves are hidden and the far ones rest behind the glass. With Season
  on but nothing in season, the menu says when the next season starts. With
  seasons off, the wall is exactly as before.
- The photos ship sharp, 2560px wide and never softened: public domain
  (National Park Service) and CC0, with the leaf shapes from Twemoji
  (CC-BY 4.0) and Phosphor Icons (MIT), all credited in
  `static/seasons/CREDITS.md`. `/seasons/` is served `no-cache`, so a
  replaced photo reaches phones after a release.
- `scripts/prep-season-photo.py` turns a downloaded photo or artwork into a
  look's image: converted to sRGB, flattened, 2560px WebP, metadata stripped,
  and it never silently overwrites a shipped photo. Pillow joins CI's
  test-only installs for its tests.
- `docs/seasonal-looks.md`, the design standards for seasonal and holiday
  looks: the bar, what failed and why, what comparable products do, the glass
  and readability rules, allowed image sources and licences, a step-by-step
  for a new look, and the lessons from the fall build. New visual gates in
  `docs/adding-a-feature.md` (all five themes, pixel-diff the off state,
  widest phone content, menus over new layers). CLAUDE.md points to both.

### Changed
- Settings is regrouped. **Display** keeps Theme, Accent and Columns, the new
  **Seasonal looks** card holds the season switch and the look picker, and
  Layout and Auto-return move to their own **This screen** card, since they
  are about how one device behaves rather than how the hub looks. The gear
  popover gains a Season row and a thin divider between the same two groups.
- While a seasonal look shows, the Accent swatches dim and ignore taps, with a
  short note saying the look sets the color. Your accent comes back as it was
  when the look ends.

## [1.4.1] — 2026-09-17

### Fixed
- The weather card keeps its real dawn and dusk when the weather feed is set
  to a 12-hour clock. The feed can now send sunrise and sunset as "6:58 AM"
  instead of "06:58"; the card's sky phase only understood the second shape and
  would have quietly fallen back to fixed dawn/dusk hours. The weather tile now
  turns either shape into "HH:MM" before the card sees it, and anything
  unreadable still falls back as before.
- A sunrise or sunset the weather tile cannot read is now logged, once per new
  shape, so the next change to the feed's clock format shows up in the logs
  instead of quietly on the wall.
- Documented why `icloud_caldav` is allowed to leave the registry when its
  credentials are missing while laundry may not: the Settings overlay draws the
  CalDAV connect panel either way, so an absent row still has a visible way to
  fix it. No behavior change.
- Laundry no longer disappears when something about it breaks. A hub with
  laundry configured but unusable now stays in the integration registry with a
  `needs_auth` or `error` status, so the wall renders an honest "Laundry
  unavailable" card and the settings row says which. Previously the integration
  dropped out of the registry entirely, taking the card and its own settings row
  with it, while `/health` kept answering 200. That covers every way it can be
  configured and not work: no `HA_TOKEN`, a token Home Assistant rejects, and a
  `laundry` config block that nothing valid survived (a typo'd entity key used
  to delete the integration outright, logged once at warning level and nowhere
  else). Startup logs the misconfiguration once, except under `DEMO`, which
  serves canned laundry and is not broken.
- A whole-feed outage now marks the settings row too, not just the log. Home
  Assistant being down, or every configured entity being renamed at once, left
  the row reading healthy beside a wall that said "Laundry unavailable", which
  is the original incident in miniature: the one surface an operator checks says
  nothing is wrong.
- A machine stuck offline while its sibling reports is no longer invisible.
  `available` is an OR across machines, so a renamed washer entity left the tile
  healthy, the card showing a dash, and nothing logged past the first warning.
  It now escalates once per machine, marks the settings row, and clears when the
  machine reports again.
- A Home Assistant token that is rejected rather than missing is an error, not a
  quiet outage. `401`/`403` used to be logged once at warning level and then
  suppressed "until it recovers", which is indistinguishable from HA restarting
  except that a revoked token never recovers. Credential rejections now need
  three in a row before they are called revoked (a reverse proxy or HA's own
  ip_ban answers 403 too, and those heal), then get their own error and latch,
  with a recovery line at a level that is visible wherever the error was.
- The watcher escalates once when the feed has been unavailable for five
  minutes, and requires sustained recovery before closing the incident: a feed
  that worked one tick in twenty used to reset the clock forever and never
  escalate while the wall flickered. The card's own hold is unchanged, so a
  brief blip still does not flicker.
- A failing completion history is latched and named. It cannot blank the card,
  but without it a load that finished and powered itself off renders as a bare
  "Idle", and at one tick every five seconds it was writing a warning with a
  traceback about 17,000 times a day, burying the errors above.
- A whitespace-only `HA_TOKEN` is treated as no token everywhere. It used to be
  "present" to the registry and "empty" to the startup check, so the hub could
  report an empty token for a card it was busy rendering, and still send a
  malformed `Authorization` header to Home Assistant.

## [1.4.0] — 2026-09-16

### Changed
- The calendar can now sync 400 days ahead instead of 90, so paging the month
  view into next year no longer hatches every day as "not synced". The default,
  the example config, the frontend's fixed fetch and the endpoint's ceiling all
  move together, and a guard test pins the chain (config window >= frontend
  fetch <= API ceiling). **An existing install keeps its own window until
  `calendar_window_days` is raised in its private `config.json`** — that file is
  operator-owned and bind-mounted over the baked copy, so upgrading alone
  changes nothing.

### Fixed
- The calendar no longer claims to have synced days it never cached. The
  reported window came from config alone, so widening the config advertised ten
  extra months the moment the app restarted, and a calendar source that kept
  failing kept its old rows while the window still promised the full range. In
  both cases the uncovered days rendered as "nothing scheduled" instead of
  hatched. The window is now the range the last SUCCESSFUL sync actually
  covered, intersected with config and the fetch.
- A calendar fetch that fails with nothing cached no longer renders every day as
  confidently free. It reported no window at all, and the out-of-window check
  fails open, so a year of days read "nothing scheduled" under a banner claiming
  it was showing the last events it saw.
- A failed calendar fetch now preserves the real reason (a 422 from a
  misconfigured window, a 5xx) in the payload and the browser console, instead
  of relabelling every failure "unreachable" and discarding it. The wall's own
  copy stays deliberately non-technical; this is for whoever diagnoses it.
- Opening the calendar no longer shows a whole month as free before it has any
  data. The first paint happens before the fetch resolves, and with no window
  yet it rendered every day as "nothing scheduled" under no banner at all —
  indefinitely, if that fetch hung rather than failed. It now paints from the
  window the wall already has, falls back to claiming nothing, and says the full
  calendar is still loading so the part it hasn't filled in doesn't read as
  final either.
- Running in demo mode no longer writes calendar coverage into a real database.
  Setting `DEMO=1` against a real install (which the README describes for
  compose) stamped a record saying days had been synced when nothing had fetched
  them, and it outlived turning demo mode back off. A demo wall that was already
  set up also keeps working after an upgrade, instead of marking every day "not
  synced", and its window now tracks today instead of the day it was first set
  up.
- A demo wall that was already set up keeps working after an upgrade. The demo
  records which days it can vouch for, and that record was only written when the
  sample family was first created, so an existing demo marked every day "not
  synced" instead. It is now repaired whenever the wall is opened, and follows
  today rather than the day it was first set up.
- An enabled iCloud *reminders* list can no longer mark a healthy calendar as
  not-synced. Reminder lists carry no events, but counted toward the check for
  whether iCloud had anything to say about the window.
- The startup warning covers `calendar_past_days` too, not just the forward
  window. An install's own config.json is the one place this misconfiguration
  can live, and half of it was going unreported.

## [1.3.7] — 2026-09-15

### Fixed
- Weather-card clouds now drift smoothly and continuously. The card redraws
  every minute, and each redraw restarted the clouds from a fixed spot, so they
  jumped back once a minute. Every sky animation (clouds, sun glow, stars,
  rain, snow, fog) now picks up from the wall clock, so a redraw lands exactly
  where it already was.

## [1.3.6] — 2026-08-31

### Added
- Family Hub can be installed to the iPhone home screen (web-app manifest +
  Apple standalone meta + app icons). Added from Safari's Share → Add to Home
  Screen it launches with no browser toolbar at all — the surest cure for the
  tab-bar gap below, because there is no toolbar whose stale inset could strand
  the tab bar.

### Fixed
- Phone tab bar stuck partway up the screen over a black gap when a backgrounded
  iOS Chrome tab comes back (the recurring case #45/#53/#80 kept missing): the
  hub now detects the stuck-short viewport on wake and reloads once to clear it.
  The wrong height lives in Chrome's per-tab toolbar-inset bookkeeping, not the
  page, so every measurement agrees on the wrong number and no CSS/height fix
  could reach it — only a reload resets that state. The reload can never loop:
  three independent guards bound it (per-height one-shot, a 20s minimum gap, and
  a hard per-session cap), it never fires while a text field holds unsaved text,
  and if a reload does not cure the height it accepts that height as the new
  normal instead of fighting it. Kill switch `localStorage['fh-selfheal-off']=
  '1'`. Every notable wake posts its viewport numbers to `/api/diag/viewport`
  (with a live readout in Settings), so the next occurrence is read off the box
  instead of guessed.

### Changed
- The full-screen calendar now opens on the text-rich Week (agenda) list on a
  phone, and on the month grid on the wall. The phone month grid hides event
  titles (7 columns are too narrow), so it opened to a screen of unlabeled
  color strips; the agenda list shows every event's time and title. The wall is
  unchanged. The choice mirrors the CSS phone split exactly (`data-layout=
  "desktop"` forces the wall, else the `max-width:1000px` query decides).

### Fixed
- The calendar's "today" fallback no longer used UTC (`toISOString`) when the
  server date was unavailable — on a US evening that rolled to tomorrow and
  could open the wrong month / mark the wrong day. It now uses local-midnight
  math like the rest of the calendar.
- Drilling into an adjacent-month day cell (a leading/trailing padding day) now
  titles the day view with that day's own month instead of the grid's month.

## [1.3.5] — 2026-08-29

### Fixed
- Phone tab bar no longer sticks partway up the screen over a black gap when
  a stale iOS Chrome / Safari tab comes back (third and structural fix). The
  phone shell body is now a `position: fixed; inset: 0` box, so the browser
  itself sizes it to the viewport on every layout. The JS-measured `--app-h`
  height var (innerHeight + visualViewport + settle timers, #53) and the
  `100dvh` fallback before it (#45) both froze a wrong height when iOS
  settled the viewport without firing any event; there is no measurement
  left to go stale. The measurement code, its listeners, and its tests are
  removed.

## [1.3.4] — 2026-08-25

### Added
- Fleet Console: the card now surfaces the fleet-admin vitals the rollup
  grew: a CPU / RAM / storage / hottest-temp grid, an internet line (link
  capacity when up, a red "Internet DOWN" when not), an open-alerts badge,
  and the worst problem promoted to a prominent severity-coloured line when
  the fleet is not nominal. The tile proxy passes the new nullable fields
  through (`fleet.alerts/cpuPercent/memPercent/storageUsedBytes/
  storageTotalBytes/hottestTempF`, `internet.up/downMbps/upMbps`); a
  missing/wrong-typed `internet` block is muted, not fatal. Every null vital
  renders as "n/a", never a fabricated 0, and the DEMO payload gained
  representative vitals so the richer card shows in demo/offline runs.

## [1.3.3] — 2026-08-25

### Added
- Fleet Console: registry descriptor + fail-soft tile proxy for a separate
  fleet-dashboard app's compact rollup (host status + 3D-printer status),
  gated behind a `fleet` config block (`{base, label?}`, no config = the
  integration doesn't appear). Server side only so far — no card, no DEMO
  payload, no mobile surface yet.
- Fleet Console: DEMO payload (a healthy fleet + a printer mid-print) so the
  card shows in demo/offline runs, plus the native wall/phone card itself —
  a system-health line ("N of M hosts up", the worst problem in words when
  not nominal) over the printer's state, job, progress bar, ETA, and F
  temps. Rides the panels column under Laundry on the wall and the Weather
  tab on the phone (no tab of its own). A "Console" button opens the full
  dashboard full-screen when a `fleet` panels entry is configured; the
  integration toggle switches the card off with the same registry hook
  every other tile uses.

### Fixed
- Fleet Console (three-agent review): a null/missing/unrecognized
  `fleet.health` or `printer.health` no longer defaults to a healthy green
  dot — it's now a neutral grey "unknown" tier, since a rollup that failed
  to report health is not a confirmed-healthy one. `fleet_tile`'s
  fail-soft `except` is narrowed to the specific fetch/decode/validation
  errors it actually expects, and the trimmed result dict is now built
  after that guard, so a real bug there surfaces instead of being read as
  "unavailable". The `fleet.label` config (already accepted and
  documented) now actually reaches the card header instead of being
  dropped. A last-good card riding out a fleet-proxy failure is now
  visibly dimmed with a "stale" badge from the very first missed poll,
  not only once the card gives up after three. A missing `online` field
  and an out-of-range `progressPercent` (e.g. negative or over 100) no
  longer render as a fabricated "online"/"finished" reading.
- Calendar agenda: on the wall's desktop width, a later media-query rule
  (`.cal-ev { padding: 6px 0; }`) was overriding `.cal-ev-allday`'s own
  horizontal padding, so the all-day/multi-day event bar's title and
  "day X of Y" tag sat flush against both edges of the bar. Scoped the
  media-query rule to `.cal-ev:not(.cal-ev-allday)` and widened the
  all-day bar's own inset (6px 12px -> 6px 14px) so both sides read with
  a comfortable gap again.

## [1.3.2] — 2026-08-24

### Changed
- Calendar agenda (home feed + week / day view): all-day events are now a calm
  tinted row — a soft wash of the event color with a solid colored left edge
  and normal-weight text — instead of a saturated solid bar. A top-to-bottom
  list has no span to draw and a multi-day event repeats one row per day, so
  the solid fill read as noise; the solid spanning bar stays in the month grid,
  where it actually spans days.

## [1.3.1] — 2026-08-24

### Changed
- Calendar redesign. The full-screen month is one hairline-ruled sheet: all-day
  and multi-day events draw as ONE bar spanning the days they cover (clipped to
  an arrow tip where they carry into the next week), timed events sit under
  them as dot + time + title, and past four rows a per-day "+N more" opens the
  day. Today is a filled accent circle on the date. The week view and the home
  card lead each day with a date badge (today's header band tints with the
  accent), all-day events render as the same colored bar tagged "day 2 of 4"
  when they're one day of a run, the outlined ALL DAY chip is gone, and the
  overlay's day cards sit with air between them instead of stacked flush. Demo
  data gains three multi-day events so the README screenshot shows the bars.
- Full-screen week / day view: each day card now has a 16px gap below it so
  the days read as separate cards, not one endless list.

### Added
- Weather card: a 5-day forecast strip at the foot of the card — each day shows a
  drawn condition glyph (not emoji, so the wall's Firefox renders it cleanly) with
  the high over the low, "Today" anchored in the accent color. It draws from a
  `dailyForecast` array on the weather feed and stays hidden until the feed
  provides one (fewer than 2 days → nothing shows). The card's height is bought
  back from a slightly shorter sky plus small trims to the climate/laundry cards,
  so the panels column stays on the 1080-tall wall. See `docs/weather-feed.md` for
  the feed contract.
- `docs/weather-feed.md`: the full `wx.json` feed contract — every field, how to
  read the live feed, and the fact that it carries no multi-day forecast (only
  today's high/low, a 24h temp curve, and a 12h AQI curve) — so the daily-forecast
  data question never has to be rediscovered.
- On-screen keyboard: a ✕ Cancel key beside Done that closes the keyboard
  without saving and clears what you typed — for when you change your mind.
- On-screen keyboard: two-page symbol layer (`?123` / `#+=`) and a categorized
  emoji picker — a tab strip (smileys, people, animals, nature, food, activity,
  travel, objects, symbols) over a scrollable grid, plus a 🕐 Recently-used tab
  that remembers your go-to emojis. Backspace is grapheme-aware, so one tap
  deletes a whole emoji. Every emoji is color-verified against the wall's font.

### Changed
- The three list columns — chores, calendar, and to-dos — are a little denser on
  the wall (mouse-only, so below the phone touch targets): tighter rows, card
  padding, and inter-card gaps fit another item or two before scrolling. Phones
  and the mobile layout are unchanged.

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
