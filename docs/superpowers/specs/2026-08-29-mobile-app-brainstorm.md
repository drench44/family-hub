# Mobile app brainstorm (2026-08-29)

Status: **brainstorm, not yet approved.** Captures the options and decisions
so far for giving the family a "real app" on their phones. Next step is to
pick an approach and turn this into a design spec + implementation plan.

## What exists today

- The site already has a phone layout (`@media (max-width: 1000px)`: app-shell
  with the bottom tab bar). No PWA manifest or service worker, so it can't be
  installed to a home screen and can't receive push.
- **No auth, LAN-only by design.** Off Wi-Fi, phones reach the hub over VPN.
  Everyone in the family already has the VPN set up.
- **The chore mirror (`chore_mirror.py`) already projects each person's chores
  into their iCloud Reminders list** with due times, so "chore is due now"
  lock-screen alerts, Siri, and the Reminders app already work on iPhone.

## What the family needs from an "app" (decided)

- Home-screen icon, launches full-screen (no Safari chrome).
- Push notifications on the lock screen.
- Per-person identity: each phone knows whose it is, shows "my chores",
  only nags that person.
- Working away from home is nice-to-have; VPN already covers it and is a
  plus on public Wi-Fi.

### Notifications wanted (all four)

1. **Morning digest** — one alert per person: "You have 3 chores today".
2. **Nag if not done** — evening reminder for chores still unchecked.
3. **New / changed assignment** — chore added to you, or the rotation lands
   on you.
4. **Family-wide events** — laundry done, calendar event soon, someone
   finished all their chores.

### Identity (decided)

**Pick your name once.** First open shows the family list; tap yourself; the
phone remembers (switchable in settings). No passwords or PINs — matches the
LAN trust model.

## VPN note (iOS)

iOS does NOT keep a VPN on after reboot by default. Enable on-demand:
- Tailscale: Settings → "VPN On Demand".
- WireGuard app: per-tunnel "On-Demand Activation" (Wi-Fi and/or cellular,
  exclude the home SSID).
One-time setup per phone; then "works away from home" is free.

## Approaches considered

### A. Installable web app (PWA) + Web Push — RECOMMENDED

Add a manifest, a service worker, and Web Push (VAPID) to the existing site.
iPhone: open in Safari once → Share → "Add to Home Screen". Then it launches
full-screen from an icon, and iOS 16.4+ delivers real lock-screen push to it.
Android Chrome does the same with less ceremony, so Android is free.

- No developer license, no App Store, no Xcode, no review. Ships by rsync
  like the site.
- Pushes leave the hub through Apple's / Google's push servers, so **alerts
  arrive even with the VPN off**; VPN is only needed to open the app.
- Reuses the phone layout, tab bar, and APIs. New pieces:
  - `manifest.webmanifest` + icons, `sw.js` service worker
  - "who are you" first-run screen; person id stored on the phone
  - push-subscription table (person ↔ endpoint/keys) + subscribe/unsubscribe
    API; VAPID keys in `data/`
  - server-side notification scheduler for the four alert types
  - "notifications are off, tap to re-enable" banner when the subscription
    is gone
- Gotchas: iOS only allows push from the home-screen install, not Safari
  tabs; the "enable notifications" tap must happen inside the installed app;
  iOS quietly revokes permission if several alerts in a row are ignored;
  deleting the icon drops the subscription; the hub needs outbound internet
  to reach the push services.

### B. Capacitor wrapper → TestFlight / App Store

Native shell around the same web UI with native APNs/FCM push. Server side
(identity, scheduler, subscriptions) is identical to A, so A is a strict
subset: wrapping later is an add, not a redo.

Ship (one-time):
- Apple Developer Program $99/yr (1–2 day enrollment). Google Play $25 once;
  new personal accounts need a 14-day / 12-tester closed test before public
  publish (for a family, stay on internal testing).
- Xcode (~15 GB) on the Mac; Android Studio for Android.
- Capacitor project (`ios/`, `android/`, `node_modules`) — brings a Node
  toolchain into a deliberately no-build, minimal-dependency repo.
- Native push plumbing: APNs key, Firebase project for FCM, a second sender
  path on the server.
- Distribution: TestFlight (no review, ≤100 testers, **builds expire every
  90 days** — must re-upload quarterly or the app stops launching). App Store
  proper means review, and reviewers can't reach a LAN-only app, so a
  reachable DEMO build would be needed.
- Realistic: 2–4 evenings of certs/provisioning/push headache on top of A.

Maintain (ongoing):
- Renew $99/yr or the app is pulled.
- Re-upload to TestFlight every ≤90 days even with no changes.
- Yearly Xcode/iOS major usually breaks the Capacitor build once.
- Signing certs and push keys expire and fail silently.
- Two release pipelines (rsync for the site; build → sign → upload → family
  updates from TestFlight for the app).

What B buys over A: icon badge with chore count; push that isn't silently
revoked and doesn't need the Add-to-Home-Screen step; a TestFlight / App
Store icon.

### C. Native SwiftUI app — NOT recommended

Best feel, but rewrites every screen, iOS only, doubles the surface to
maintain. Not worth it for a handful of phones.

## B in depth (Capacitor wrapper)

### What it is

A native iOS/Android app that is mostly a full-screen web view running the
existing hub pages unchanged. A thin native layer adds what Safari can't:
real push tokens, the icon badge, install via TestFlight instead of
Add-to-Home-Screen.

Two ways to feed it the UI:
- **Remote URL (pick this):** the app loads the hub's LAN URL. Every rsync
  deploy updates the app instantly; the binary only changes when native bits
  change (icon, push plugin, iOS SDK bump). This is what keeps maintenance
  bounded.
- **Bundled:** copy `hub.js`/`styles.css`/`index.html` into the app at build
  time. Shell works offline, but every UI change means rebuild + re-upload.
  Wrong trade here.

### Repo shape

A sibling repo, not this one: `family-hub-app/` with `package.json`,
`capacitor.config.ts`, `ios/`, `android/`, and a `www/` holding one tiny
redirect page. Keeps the Node toolchain, Xcode project, and signing material
out of the public no-build repo. Hub URL, bundle id, team id in a
git-ignored config.

### Push, end to end

1. App starts → asks for permission → gets an APNs (iOS) or FCM (Android)
   token → `POST /api/push/register {person_id, platform, token}` to the hub.
2. Scheduler decides "Alex: 3 chores today" → looks up that person's tokens →
   sends.
3. iOS: hub talks to APNs directly over HTTP/2 with a `.p8` auth key (one key
   per team, never expires unless revoked). Python: `aioapns` / `apns2`. No
   Firebase needed.
4. Android: Capacitor's push plugin rides FCM → free Firebase project +
   service-account JSON on the hub, send via FCM HTTP v1.
5. Stale tokens (reinstall) are reported on send; hub deletes them.

vs A: A is one Web Push path for both platforms with no vendor accounts; B is
two vendor paths. Identity, subscription table, scheduler, and the four alert
types are the same code either way.

### Getting it onto phones

iOS (all need the $99/yr program):
- **TestFlight — best for a family.** Upload → invite by email/public link →
  they install TestFlight, then the app. No review for internal testers
  (≤100). Builds expire after 90 days → re-upload quarterly. Family gets an
  "update available" prompt.
- **App Store (public or unlisted):** review per version (1–3 days). LAN-only
  apps get rejected because the reviewer can't use them → needs a DEMO mode
  reachable off your LAN. Never expires once through.
- Ad-hoc / UDID sideload: yearly profile expiry, fiddly, no better than
  TestFlight.
- Free Apple ID signing: 7-day expiry, 3-app limit. Not viable.

Android: build a signed APK and send it (AirDrop/Drive); one-time "unknown
sources" tap. No Play account, no expiry. Play Store optional.

### Dev workflow

- `npx cap sync ios` → Xcode → Product → Archive → Distribute → TestFlight.
  ~10 min once it works; hours the first time (certs, provisioning profiles,
  push capability, App ID).
- Test on a real iPhone over cable; the simulator can't do push.
- Xcode needs current macOS; each September's Xcode/iOS release usually
  needs a Capacitor bump.
- The `.p8` key doesn't expire, but revoking it stops every phone until the
  hub has the new key.

### Family experience, A vs B

| | A (PWA) | B (Capacitor) |
|---|---|---|
| Install | Safari → Share → Add to Home Screen | TestFlight → install |
| Icon, full-screen | yes | yes |
| Lock-screen push | yes (iOS 16.4+) | yes |
| Badge count on icon | no on iOS | yes |
| Push revoked if alerts ignored | yes (iOS quirk) | no |
| Updates | instant | instant (remote-URL shell) |
| Tap alert → right screen | yes | yes |
| Cameras / iframes | same engine | same engine |
| Cost | $0 | $99/yr + quarterly re-upload |

### Where B genuinely wins

- Kids who dismiss alerts: iOS kills a PWA's push after a few ignored
  notifications; native push is only ever muted by the user.
- Icon badge count is an effective chore nag.
- Install feels legitimate; no "why am I adding a website to my home screen."

### Where B costs more than it looks

- Two push vendors to wire and monitor.
- Second machine dependency (Xcode) and a second repo.
- The 90-day TestFlight clock: miss a quarter and every phone's app refuses
  to open until rebuilt.
- App Store route means maintaining a reachable demo for reviewers.

### Middle path (design for it now)

Do A, and give the subscription table a `kind` column
(`webpush` | `apns` | `fcm`). Adding B later is: write the shell, add the
APNs/FCM senders. Scheduler, identity, and UI don't change.

## Recommendation

Build **A** now. If PWA install or push annoys the family after a few
months, add the Capacitor shell (B) with nothing wasted.

## Open questions before a spec

- Exact send times for digest and nag (per person? per household?).
- Which "family-wide events" to start with (laundry done and calendar
  soon already have server-side signals).
- Whether the "my chores" view is a new tab or a filter on the Chores tab.
- Icon / splash artwork.
