# FamView — Technical Architecture & Design Spec

**Status:** Draft v0.1 · **Owner:** Gary · **Date:** 2026-08-12
**Companion to:** `PRD: Family Dashboard` (native Android + iCloud CalDAV)

This document resolves the PRD's §9 Open Questions and fixes the module
architecture *before* implementation. It is deliberately protocol- and
data-model-first; pixel-level visual design is a separate follow-up pass and is
explicitly out of scope here (see §12).

> **Protocol research (2026-08-12) is now folded in.** Two of the three gated
> items are resolved from evidence; one is a **premise correction** that needs a
> product decision:
> - ✅ **Change detection (§5.3):** iCloud *does* support `sync-collection` /
>   `sync-token`; use it as primary with a CTag+ETag fallback.
> - ✅ **Push (§5.5):** confirmed unavailable to third-party clients → polling.
> - ⚠️ **Color (§6.3) — PRD premise is wrong.** Stock Apple Calendar has **no
>   per-event color**; it colors strictly *by calendar*. A per-event `COLOR` will
>   not render on family members' Apple devices. This changes the product's data
>   model and needs a decision (single calendar + dashboard-only color, **vs.**
>   multiple category calendars + true cross-device color). See §6.3 + §13.
> - 🔬 One empirical item remains for **M0**: does iCloud round-trip a standard
>   `COLOR` written by a *non-owning editor sharee* on a *shared* calendar (no
>   citable source settles this).

---

## 1. Context, Constraints, and Design Principles

### 1.1 What we are building
A single-purpose, always-on **native Android** kiosk app running in the
Apolosign 21.5" display's **System Mode**, presenting a set of shared family
iCloud **category Calendars** (VEVENT; one calendar per category — see §6.3) and
a shared **Reminders** list (VTODO), plus local-only tiles
(chores, message board, weather webview). It authenticates to iCloud CalDAV as a
dedicated **bot iCloud account** that has been added as an *editor-level sharee*.

### 1.2 Non-negotiable constraints (from PRD)
- **C1 — Native property fidelity.** The app may only write properties that
  Apple's own Calendar/Reminders apps already understand and render. No custom
  fields, no app-private metadata on the CalDAV objects.
- **C2 — iCloud is the source of truth.** The dashboard is a lens + control
  surface, not a data owner. On unresolvable conflict, the server's data model
  wins; the dashboard never invents structure the server can't represent.
- **C3 — Instant touch response.** Calendar and chores must feel native — no
  network on the render path, no webview scroll jank.
- **C4 — Extensible slot system.** New tile types must be addable without
  rearchitecting (webview tiles = zero code; native tiles = register one class).

### 1.3 Design principles that fall out of the constraints
| # | Principle | Driven by |
|---|---|---|
| P1 | **Local-first.** Room (SQLite) is the UI's single source of truth. The UI never awaits the network. | C3 |
| P2 | **Sync is a background reconciler**, not a request/response layer. Room ↔ iCloud is eventually consistent. | C2, C3 |
| P3 | **Every persisted CalDAV field maps to a standard iCalendar property.** The Room row is a *cache of ICS*, plus the raw ICS blob for round-trip safety. | C1 |
| P4 | **Native-vs-web is decided per tile, declaratively.** Interactive/touched → native Compose; glanceable/read-only → webview. | C4 |
| P5 | **Graceful degradation everywhere.** Auth revoked, network down, color unsupported, conflict detected — each has a defined, visible fallback state. | Global reliability rules |

---

## 2. Architecture Overview

### 2.1 Layered view

```
┌──────────────────────────────────────────────────────────────────┐
│  PRESENTATION  (Jetpack Compose, one ViewModel per screen region)  │
│  ├─ CalendarRegion (month/week/day/agenda)   ← bottom half         │
│  ├─ WeatherTile (WebView)                     ← top right          │
│  └─ SlotHost → TileProvider registry          ← top left "+"       │
└───────────────▲───────────────────────────────▲──────────────────┘
                │ observes Flow<…>               │ user intents
┌───────────────┴───────────────────────────────┴──────────────────┐
│  DOMAIN / REPOSITORY                                               │
│  ├─ CalendarRepository / RemindersRepository  (Room-backed)       │
│  ├─ ChoresRepository (local-only)                                 │
│  └─ SlotRepository / MessageBoardRepository                       │
└───────────────▲───────────────────────────────────────────────────┘
                │ reads/writes                                       
┌───────────────┴───────────────────────────────────────────────────┐
│  LOCAL STORE   Room (SQLite) — SINGLE SOURCE OF TRUTH for the UI    │
│  events · attendees · alarms · todos · collections · sync_state     │
│  chores · message_board · widget_slots · sync_log                   │
└───────────────▲───────────────────────────────────────────────────┘
                │ upsert / read outbox                                
┌───────────────┴───────────────────────────────────────────────────┐
│  SYNC ENGINE  (foreground service + coroutine reconciler)          │
│  ├─ Discovery (principal → home-set → collections)                │
│  ├─ Pull  (change-detect → fetch changed → parse ICS → upsert)    │
│  ├─ Push  (outbox → If-Match write → reconcile ETag)              │
│  └─ Conflict resolver (SEQUENCE / LAST-MODIFIED aware)            │
└───────────────▲───────────────────────────────────────────────────┘
                │ CalDAV over HTTPS (Basic auth, app-specific pw)    
┌───────────────┴───────────────────────────────────────────────────┐
│  TRANSPORT  dav4jvm (WebDAV/CalDAV) + ical4j (iCalendar parse/gen) │
│             OkHttp · EncryptedSharedPreferences (credential)      │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 The one rule that keeps it fast
**No layer above the Sync Engine ever touches the network.** A user tap that
edits an event writes to Room *immediately* (optimistic), the UI re-renders from
Room *immediately*, and the Sync Engine later flushes the change to iCloud and
reconciles the returned ETag back into Room. This is what satisfies C3 without
violating C2.

---

## 3. Technology Choices

| Concern | Choice | Why / alternatives rejected |
|---|---|---|
| Language | **Kotlin** | Standard for modern Android; coroutines for the sync loop. |
| UI toolkit | **Jetpack Compose** | Declarative, good touch perf, custom calendar grid via `Canvas`/custom `Layout`. Rejected XML views (more boilerplate for the slot system). |
| Local store | **Room** over SQLite | Type-safe DAO, `Flow` observation = reactive UI straight off the DB. |
| Async | **Coroutines + Flow** | Structured concurrency for the reconciler; `Flow` for DB→UI. |
| DI | **Hilt** | Standard, testable wiring for repositories/engine. |
| WebDAV/CalDAV | **dav4jvm** (bitfireAT, the engine behind DAVx5) | Battle-tested *against iCloud specifically*; handles PROPFIND/REPORT/`sync-collection`/ETag. Rejected hand-rolling WebDAV XML (error-prone). **License caveat below.** |
| iCalendar parse/gen | **ical4j** (or `ical4android`) | Robust VEVENT/VTODO/RRULE/VALARM/VTIMEZONE handling incl. recurrence expansion. Rejected hand-rolling ICS (timezone + RRULE edge cases are brutal). |
| Background exec | **Foreground Service** (long-lived) + WorkManager for boot/backoff | See §5.5 — WorkManager's 15-min floor is too slow for a live dashboard; the device is mains-powered and always-on, so a foreground service reconciler is the right primitive. |
| Credential storage | **EncryptedSharedPreferences** (Keystore-backed) | App-specific password is the crown jewel (full R/W to the family calendar); never in source, never plaintext. |
| Crash reporting | **TBD (self-hosted or Firebase Crashlytics)** | Global rule: crash reporting from day one. Decide at M6; wire the seam now. |

> **⚠️ License caveat (dav4jvm / ical4android):** these bitfireAT libraries are
> copyleft (dav4jvm is MPL-2.0; `ical4android` and DAVx5 are GPLv3). MPL-2.0 is
> file-level copyleft and fine to link. If we pull in `ical4android` (GPLv3) the
> whole app inherits GPLv3 obligations. For a private family device that is
> irrelevant, but if this is ever distributed, prefer **dav4jvm (MPL) + ical4j
> (BSD-3)** and avoid `ical4android`. Decision recorded so it isn't a surprise.

---

## 4. CalDAV Service Identity, Auth & Discovery

### 4.1 Service identity
- One dedicated **bot iCloud account**, already an *editor* sharee on the family
  calendar and the family reminders list.
- Auth: **HTTP Basic** over TLS to iCloud, username = the bot Apple ID, password
  = an **app-specific password** minted at appleid.apple.com.
- The credential lives only in EncryptedSharedPreferences. The app must handle
  **auth-failure as a first-class state** (password revoked/expired → on-screen
  "Reconnect iCloud" banner, sync paused, no crash).

### 4.2 Discovery sequence (standard CalDAV, RFC 4791 + iCloud specifics)
Runs once on setup, and re-runs on `412`/`404` drift or manual "rediscover":

1. **Principal** — `PROPFIND` `Depth: 0` on the well-known base
   (`https://caldav.icloud.com/` or the RFC 6764 `/.well-known/caldav`),
   requesting `DAV:current-user-principal`. ✅ *Confirmed:* the `calendar-home-set`
   href lands on a **per-account partition host** (`pNN-caldav.icloud.com` — p22,
   p34, p42, p67… seen in the wild). **The partition number is per-account —
   never hardcode it; resolve it via discovery on every login and follow
   cross-host redirects.**
2. **Calendar home** — `PROPFIND` `Depth: 0` on the principal URL requesting
   `CALDAV:calendar-home-set`. Returns the collection that contains all the
   account's (and accepted shared) calendars.
3. **Enumerate collections** — `PROPFIND` `Depth: 1` on the calendar-home,
   requesting per child:
   - `DAV:resourcetype` (is it a `calendar`?)
   - `DAV:displayname`
   - `CALDAV:supported-calendar-component-set` → **`VEVENT` vs `VTODO`**
   - `apple:calendar-color` (`{http://apple.com/ns/ical/}calendar-color`)
   - `CS:getctag` (`{http://calendarserver.org/ns/}getctag`)
   - `DAV:sync-token` (if advertised)
   - `DAV:current-user-privilege-set` (confirm write privilege as sharee)
4. **Classify & persist.** Under Strategy B (§6.3) the home-set contains **several
   shared category calendars** — every collection whose component-set contains
   `VEVENT` is a category (its `apple:calendar-color` is its color); the shared
   **reminders list** = the collection whose component-set contains `VTODO`. On
   iCloud these are *separate single-component collections*. Persist each
   collection's URL, ctag/sync-token, `apple:calendar-color`, and privilege set
   into the `collections` table; default `is_visible=true` for calendars the bot
   can edit.

> **Resolves PRD §9 "Reminders list discovery":** a Reminders list is simply a
> calendar collection advertising `VTODO` in its supported-component-set,
> discovered by the same `Depth:1` enumeration — no special API.

### 4.3 Confirmed iCloud quirks (code defensively — none are Apple-documented)
iCloud CalDAV is an undocumented, forked/older CalendarServer; treat everything
as reverse-engineered. The following are confirmed and have design consequences:

- **Auth** = HTTP **Basic**, `base64(appleid : app-specific-password)`; the
  account must have 2FA. A `401` with known-good credentials almost always means a
  *primary* password was used where an app-specific one is required. **Resetting
  the primary Apple ID password revokes all app-specific passwords** → design the
  auth-failure state (§11) for exactly this.
- **⚠️ Third-party calendar creation (`MKCALENDAR`) is unreliable on iCloud.** New
  calendars generally must be created from an Apple client or iCloud web. **This
  directly constrains the per-calendar-color strategy (§6.3):** if we categorize
  via separate calendars, the *family creates and shares those calendars from an
  Apple device*; the dashboard/bot does **not** try to `MKCALENDAR` them.
- **No `VJOURNAL`, no free/busy** on iCloud; and **UID uniqueness is enforced** —
  one UID cannot exist in two calendars. Task enumeration on iCloud is historically
  finicky (list objects generically rather than issuing a VTODO-only query).
- **Rate limits are undocumented** — no published numbers. Third-party clients hit
  throttling / `503`s under aggressive polling. → mandatory exponential backoff on
  `429/503` and a conservative idle interval (§5.5).

---

## 5. Sync Engine

### 5.1 Model: local-first outbox reconciler
Room holds every event/todo with a `sync_state`:
`SYNCED · PENDING_CREATE · PENDING_UPDATE · PENDING_DELETE · PENDING_MOVE`
(`MOVE` = cross-calendar re-categorization, §6.6), plus `base_etag`,
`sequence`, `last_modified`, and the `raw_ics` blob. The engine runs two
independent passes each cycle: **Pull** (remote→local) and **Push**
(local→remote via the outbox).

### 5.2 Pull pass — change detection then fetch
1. **Detect collection change cheaply.** Compare stored `getctag`/`sync-token`
   against a fresh `PROPFIND`. If unchanged → skip the collection entirely (this
   is what makes frequent polling cheap).
2. **Compute the delta** (mechanism per §5.3).
3. **Fetch changed resources** with a `calendar-multiget` REPORT (batch by href),
   pulling `getetag` + `calendar-data`.
4. **Parse ICS → upsert Room**, but **never clobber a row that has un-pushed
   local changes** (`PENDING_*`) — instead mark it `CONFLICT` and hand to §5.6.
5. **Handle remote deletes** — hrefs gone from the listing are removed locally
   (unless locally `PENDING_UPDATE`, → conflict).

### 5.3 ✅ Change-detection mechanism (resolved — both tiers implemented)
Research confirms iCloud supports WebDAV-Sync today (captured `sync-collection`
responses with `sync-token` from `pNN-caldav.icloud.com`), but also that iCloud's
implementation varies by account/partition. So: **primary = WebDAV-Sync, with a
mandatory CTag fallback** (this is DAVx5's own strategy).

- **Primary — WebDAV-Sync (RFC 6578).** `sync-collection` REPORT returns an
  **incremental delta** (changed + removed hrefs, with ETags) in one round-trip
  against a stored `sync-token`. Cheapest, cleanest. First sync has no token → do
  a full listing, then store the returned token.
- **Fallback — CTag + ETag diff.** If `sync-collection` errors or the account's
  partition misbehaves, use `CS:getctag` to detect *that* the collection changed,
  then `Depth:1 PROPFIND` for all hrefs+ETags, diffed against Room. More
  bandwidth, identical correctness. The engine auto-falls-back on primary error.

### 5.4 Push pass — outbox flush with optimistic concurrency
For each `PENDING_*` row, oldest first:
- **Create:** `PUT` to a new href (`<collection>/<uid>.ics`) with header
  `If-None-Match: *`. On `201/204` store returned `ETag` → `SYNCED`.
- **Update:** `PUT` full regenerated ICS with `If-Match: <base_etag>`. On success
  store new ETag → `SYNCED`.
- **Delete:** `DELETE` with `If-Match: <base_etag>`.
- **`412 Precondition Failed`** on any of these = the server copy moved under us →
  §5.6 conflict resolution.
- **Bump `SEQUENCE`** on every semantic update we originate (native iCalendar
  change-signaling — keeps Apple clients' own conflict logic happy). C1-safe.

### 5.5 Cadence & scheduling
- **Foreground Service** owns a coroutine reconcile loop. The Apolosign is
  mains-powered and always-on, so a persistent service is appropriate and
  reliable (survives Doze better than WorkManager for a wall display).
- **Adaptive polling interval:**
  - Idle baseline: poll ctag/sync-token every **~60–120 s** (cheap — one PROPFIND
    per collection when nothing changed).
  - **Burst after a local write:** immediately push, then poll fast (~10 s) for
    ~1 min to catch the echo/any server-side normalization, then decay to
    baseline.
  - Back off exponentially on errors (network/throttle) up to a cap; surface a
    "last synced HH:MM" + error state on screen.
- **Push notifications:** ✅ **confirmed unavailable** to third-party clients.
  Apple's CalendarServer push (APNs `push-transports`) is gated to Apple's own
  clients; the newer WebDAV-Push draft is implemented only by Nextcloud, not
  iCloud (BusyCal disables push for iCloud for this exact reason). iCloud's
  email change-notifications only fire for *other people's* edits, so they can't
  serve as a change log either. **Polling is the only path.** *(Resolves the
  polling-vs-push half of PRD §9.)*
- WorkManager is still used for **boot-time start** and **watchdog restart** of
  the service, not for the sync cadence itself.

### 5.6 Conflict resolution (resolves PRD §9 "conflict resolution UX")
Family-scale conflicts are rare (bot vs. one phone editing the same event in the
same minute), so the policy optimizes for *never silently losing an explicit
dashboard edit* while honoring C2:

1. On `412` or a pull that finds a `PENDING_*` row changed remotely, **re-fetch
   the remote resource**.
2. **Field-level merge when disjoint.** If the local pending change and the remote
   change touch *different* properties (e.g. dashboard changed the color, phone
   changed the time), auto-merge: re-apply the local delta on top of the remote
   base, bump SEQUENCE, re-push. No user involvement.
3. **Same-field conflict → newest wins, by native signal.** Compare
   `SEQUENCE` then `LAST-MODIFIED`; the higher/newer wins. This uses only native
   iCalendar properties (C1-safe) and matches how Apple clients themselves
   arbitrate.
4. **Always log the resolution** to `sync_log` (visible in a diagnostics view) so
   a "wrong" auto-resolution is recoverable, not invisible.

Because C2 makes the server authoritative, the *only* case a human sees is a
persistent, repeated same-field fight — logged and surfaced, not popped as a
modal on a wall display nobody is standing at.

---

## 6. Native Property Mapping (ICS ⇄ Room)

Every column below is a **standard iCalendar property** (C1). The `raw_ics` blob
is retained so we never lose properties we don't model on round-trip.

### 6.1 Events (VEVENT)
| iCalendar | Room field | Notes |
|---|---|---|
| `UID` | `uid` (PK within collection) | Stable identity. |
| `SUMMARY` | `summary` | Title. |
| `DTSTART` / `DTEND` | `dt_start`,`dt_end`,`all_day`,`tzid` | `VALUE=DATE` ⇒ all-day; else store TZID + UTC instant. **Store UTC, render local** (global rule; matters for DST + all-day boundaries). |
| `LOCATION` | `location` | |
| `DESCRIPTION` | `description` | |
| `ATTENDEE` (×n) | `attendees` table | `mailto:`, `CN`, `ROLE`, `PARTSTAT`, `RSVP`. Powers the picker (§6.4). |
| `RRULE` / `RDATE` / `EXDATE` | `rrule`,`is_master`,`recurrence_id` | Recurrence — see §6.5. |
| `VALARM` (×n) | `alarms` table | `ACTION`, `TRIGGER` (relative offset or absolute). |
| `TRANSP` / `STATUS` | `transp`,`status` | Free/busy + tentative/confirmed/cancelled. |
| `URL` | `url` | |
| `COLOR` *(RFC 7986; see 6.3)* | `color_name`,`color_argb` | CSS3 named color on the VEVENT. **Renders in the dashboard's own view + `COLOR`-aware clients only — NOT in stock Apple Calendar.** |
| `SEQUENCE`,`LAST-MODIFIED` | `sequence`,`last_modified` | Conflict signaling (§5.6). |
| — (HTTP `ETag`) | `etag`/`base_etag` | Optimistic concurrency; not an ICS field. |

### 6.2 Reminders (VTODO)
| iCalendar | Room field | Notes |
|---|---|---|
| `SUMMARY` | `summary` | |
| `DUE` (or `DTSTART`) | `due`,`all_day`,`tzid` | |
| `PRIORITY` | `priority` | 1–9 (Apple maps to none/low/med/high). |
| `STATUS` + `COMPLETED` | `status`,`completed_at` | `NEEDS-ACTION`↔`COMPLETED`; toggling completion writes both. |
| `DESCRIPTION` | `description` | |
| `VALARM` | `todo_alarms` table | |
| (collection) | `collection_id` | The shared list *is* the family to-do list. |

### 6.3 ⚠️ Color / categorization — PRD premise corrected, decision pending
The PRD (§4.1, §8.3) treats **per-event color** as the reliable native lever that
"appears correctly colored in Apple Calendar on all devices." **Research shows
that is not achievable**, because:

1. **Stock Apple Calendar has no per-event color.** iOS 17 / macOS Sonoma did not
   add it (that's a Fantastical/BusyCal feature, not stock). Apple Calendar colors
   **strictly by calendar** and ignores any per-event `COLOR`.
2. So a per-event `COLOR:tomato` we write will render in the **FamView dashboard's
   own view and other `COLOR`-aware clients** (DAVx5-backed apps, Fantastical) —
   but **stock Apple Calendar on the family's iPhones will paint the calendar's
   color regardless.** The cross-device promise fails.
3. `COLOR` is also lossy (CSS3 named colors only, ~147 values; not full ARGB).
4. iCloud very likely *stores/round-trips* the raw bytes (BusyCal's `X-` tags sync
   between BusyCal instances via iCloud) — so the *data* survives; it just isn't
   *rendered* by Apple's app.

**Two viable strategies — this is a product/data-model decision (see §13, and the
question posed to the owner):**

| | **A · Per-event `COLOR` (single calendar)** | **B · Per-calendar color (calendar-per-category)** |
|---|---|---|
| Data model | One shared family calendar (matches PRD §1) | Several shared calendars: "Gavin", "School", "Family"… |
| Renders colored **in the dashboard** | ✅ | ✅ |
| Renders colored **in stock Apple Calendar on phones** | ❌ (ignored) | ✅ (this is the *only* thing Apple colors by) |
| Native / C1-clean | ✅ standard `COLOR` prop | ✅ calendar color is the most native signal there is |
| Cost | Cheap; lossy palette | Family creates+shares N calendars from an Apple device (**iCloud `MKCALENDAR` by the bot is unreliable — §4.3**); events must live in the right calendar; attendee/color = which calendar |
| Delivers PRD §8.3 thesis | ✗ partially | ✅ genuinely |

### ✅ DECISION: Strategy B — calendar-per-category (chosen 2026-08-12)
Colors must render on the family's iPhones, so categorization = **one shared
calendar per category**. Concrete model:

- **The shared calendars ARE the category taxonomy.** The family creates each
  category calendar (e.g. "Gavin", "Ella", "School", "Health", "Family") on an
  Apple device, sets its color there, and shares each to the bot as *editor*. The
  dashboard does not invent categories and does not `MKCALENDAR` (unreliable on
  iCloud — §4.3); it **discovers** them via the §4.2 home-set enumeration.
- **Color is read-only and native.** The dashboard reads each collection's
  `apple:calendar-color` (`{http://apple.com/ns/ical/}calendar-color`) at
  discovery and renders events in that color. This is the exact color Apple
  Calendar paints, so the wall display and every iPhone agree by construction.
  We **never write** calendar color (sidesteps the PRD §4.1 "partially synced"
  concern — it only bites on *writes*, which we don't do; the family sets color
  once on an Apple device).
- **An event's category = the calendar it lives in.** Creation is trivial: the
  editor shows a **calendar picker** (swatches from each collection's
  `apple:calendar-color`); the chosen calendar is the `PUT` target. No per-event
  `COLOR` needed.
- **Per-event `COLOR` (Strategy A) is deferred, not used in v1.** Categorization
  is fully carried by calendar membership; layering a second color system (option
  C) would only add incoherence. The `ColorStrategy` seam remains so C is a later
  additive option, not a rewrite.
- **Reminders stay a single shared VTODO list** in v1 (categorize to-dos by
  priority/filter, not by list) — multi-list is a later, symmetric extension.

New complexity this introduces → **§6.6 cross-calendar move semantics**. The M0
empirical `COLOR` probe is **dropped** (we're not using per-event color); M0 now
verifies the bot sharee can *read* each calendar's color and *write* events into
each shared calendar, and that a cross-calendar move round-trips cleanly.

### 6.4 Attendee picker (PRD FR#5)
Populated purely from `ATTENDEE` values already seen on synced events — a
`SELECT DISTINCT email, common_name FROM attendees` view (`known_attendees`). No
contacts API, no invented data. New attendees typed by the user are added as
plain `ATTENDEE;CN=…:mailto:…` — Apple-native.

### 6.5 Recurrence — the biggest hidden complexity
Called out explicitly because it is routinely underestimated:
- Store the **master VEVENT** (with `RRULE`) plus any **override instances**
  (each a VEVENT with the same `UID` + a `RECURRENCE-ID`). `EXDATE` records
  deleted instances.
- **Expand occurrences on demand** for the visible window only (ical4j's
  recurrence iterator), never materialize an infinite series into Room.
- **Editing offers "This event" vs "All events"** (matching Apple UX):
  - *This event* → write/modify an override VEVENT with `RECURRENCE-ID` (+ maybe
    `EXDATE` on master).
  - *All events* → edit the master.
- This is a **known complexity hotspot** — budget for it; it is the most likely
  place to leak non-native structure if done carelessly.

### 6.6 Cross-calendar move — re-categorizing an event (Strategy-B consequence)
Because category = calendar membership, changing an event's category means moving
its resource between collections. iCloud constraints force a specific, careful
dance:

- **UID uniqueness is enforced account-wide (§4.3)** — the same `UID` cannot exist
  in two calendars at once, so we **cannot** "copy then delete." The move is
  ordered **`DELETE` old → then `PUT` new** (same `UID`, new href under the target
  collection). WebDAV `MOVE` across collections is *not* relied on (unreliable on
  iCloud).
- **Durability against a half-done move.** The danger window is "old deleted, new
  not yet written" → the event is briefly absent on other devices, and a crash
  there could lose it. Mitigation: the move is a single **outbox operation**
  (`PENDING_MOVE`) carrying `{old_href, old_etag, target_collection, full_ics}`.
  Room (source of truth) keeps the event visible on the wall the entire time. The
  engine executes `DELETE (If-Match old_etag)` → `PUT (If-None-Match *)`, and
  **only clears the op after the PUT's ETag is stored**. A failed/interrupted PUT
  simply retries from the outbox — the ICS payload is never only in flight.
- **Conflict on move.** `DELETE` returning `412` = the source changed remotely →
  re-fetch and re-resolve (§5.6) before moving. `PUT` returning `412`/`409` on the
  target = href/UID collision → regenerate href and retry.
- **Hot path stays trivial.** *Creating* an event in the right category is just a
  `PUT` to the chosen calendar — no move machinery. Moves are the rarer edit path;
  they get the durable-outbox treatment precisely because they're the only
  multi-request, data-loss-capable operation in the app.

---

## 7. Local Data Model (Room schema sketch)

```
-- Under Strategy B, each shared VEVENT collection IS a category; its
-- color_argb comes from apple:calendar-color (read-only) and is the color
-- the dashboard renders. is_visible lets the owner hide calendars the bot
-- can see but shouldn't display.
collections(id PK, url, type[VEVENT|VTODO], display_name, color_argb,
            is_visible, ctag, sync_token, privileges, last_sync_at, last_error)

-- An event's rendered color = its collection's color (Strategy B), so the
-- color_* fields are reserved/nullable for a future Strategy-C layer only.
-- move_target_collection_id is set while sync_state = PENDING_MOVE (§6.6).
events(uid, collection_id FK, href, etag, base_etag,
       summary, dt_start_utc, dt_end_utc, all_day, tzid,
       location, description, color_name?, color_argb?,
       transp, status, url, rrule, is_master, recurrence_id,
       sequence, last_modified, raw_ics,
       sync_state, move_target_collection_id?,
       local_modified_at, sync_attempts, last_sync_error,
       PRIMARY KEY(collection_id, uid, recurrence_id))

attendees(id PK, event_uid FK, email, common_name, role, partstat, rsvp)
alarms(id PK, event_uid FK, action, trigger_rel_minutes, trigger_abs, description)

todos(uid, collection_id FK, href, etag, base_etag, summary,
      due_utc, all_day, tzid, priority, status, completed_at,
      description, sequence, last_modified, raw_ics,
      sync_state, local_modified_at, sync_attempts, last_sync_error,
      PRIMARY KEY(collection_id, uid))
todo_alarms(id PK, todo_uid FK, action, trigger_rel_minutes, trigger_abs)

-- local-only (never synced to CalDAV):
chore_people(id PK, name, avatar, color_argb)
chore_tasks(id PK, person_id FK, title, recurrence_spec, points, active)
chore_completions(id PK, task_id FK, date, completed_at, points_awarded)
message_board(id PK, author, body, created_at, pinned)
widget_slots(id PK, position[TOP_LEFT|…], tile_type, config_json, enabled, sort)
sync_log(id PK, at, collection_id, level, code, message)   -- observability
```

Notes:
- `raw_ics` is the safety net for **round-trip fidelity** — regenerate writes by
  patching the parsed model back over the original ICS, preserving unmodeled
  properties (C1).
- Schema changes go through **Room migrations** (global rule: version schema
  changes properly, no destructive `fallbackToDestructiveMigration` in release).

---

## 8. Chores / Routines Subsystem (local, CalDAV-independent)
Fully local (PRD FR#6): `chore_people` / `chore_tasks` / `chore_completions`.
Supports per-day assignment, recurrence, point values, completion timestamps, and
weekly payout tallies (`SUM(points_awarded)` per person per ISO week). Native
Compose tile for instant touch. **No CalDAV involvement** — deliberately, so the
family's existing chore/reward habit is served without polluting the calendar
with non-native structure. Coexists with the Apolosign's own Calendar-Mode chore
feature (PRD §7.10) rather than replacing it.

---

## 9. Widget Slot / Plugin Architecture (PRD §6, FR#8)

The slot system is a **registry of `TileProvider`s** + a `widget_slots` config
table that maps a screen position to a tile type + JSON config.

```kotlin
interface TileProvider {
    val type: String                    // stable id, e.g. "filtered_todos"
    val displayName: String
    val renderKind: RenderKind          // NATIVE | WEBVIEW
    fun defaultConfig(): JsonObject
    // NATIVE tiles:
    @Composable fun Render(config: JsonObject, modifier: Modifier)
    // WEBVIEW tiles:
    fun webUrl(config: JsonObject): String?   // non-null iff WEBVIEW
}
```

- **Adding a webview tile = zero code:** insert a `widget_slots` row with a URL in
  `config_json` (weather, Google Photos shared album, Wyze snapshot). Satisfies
  FR#8 "without core app changes" literally.
- **Adding a native tile = register one `TileProvider`** in the Hilt multibinding
  set (chores, filtered to-dos, message board). No touching the `SlotHost`.
- `SlotHost` reads `widget_slots`, resolves each to a provider, and renders
  NATIVE via `Render(...)` or WEBVIEW via a locked-down `WebView` (JS off unless
  required, no navigation chrome, cache-friendly).

v1 tiles: **Weather** (webview, already built), **Chores** (native), **Filtered
to-dos** (native, over the VTODO cache), **Message board** (native). Deferred per
PRD: Spotify (would be a Web Playback SDK webview tile if ever revisited),
Photos/camera (webview tiles).

---

## 10. Kiosk / System Mode / Device Integration

The Apolosign runs stock Android in **System Mode**, so standard kiosk techniques
apply. Requirements for a wall display and the chosen mechanism:

| Need | Mechanism | Fallback |
|---|---|---|
| Launch on boot | `BOOT_COMPLETED` receiver → start activity + foreground service | manual launch |
| Stay foregrounded, no accidental exit | **Lock Task Mode** (screen pinning). True lockdown needs **device-owner** via `adb shell dpm set-device-owner` (no MDM enrollment on this hardware) | **HOME launcher replacement** if device-owner isn't grantable on the Apolosign |
| Survive crashes | foreground service + WorkManager watchdog restart | — |
| Screen always on | `FLAG_KEEP_SCREEN_ON` / sustained; optional scheduled sleep hours | — |
| **Burn-in mitigation** (always-on panel) | periodic 1–2px content shift + scheduled dimming overnight | — |
| Immersive (no status/nav bars) | `WindowInsetsController` immersive-sticky | — |

> **M0 spike also verifies device-owner feasibility on the actual Apolosign**
> (can we reach it over `adb` in System Mode, and does `dpm set-device-owner`
> succeed on an unprovisioned account?). If not, we take the launcher-replacement
> path. This is a real hardware unknown, not a formality.

---

## 11. Observability, Resilience & Security (global rules)

- **Crash reporting from day one** — seam wired now (§3), backend chosen at M6.
- **Persistent logging** — `sync_log` table (ring-buffered) + rotating file log,
  not just logcat; surfaced in an on-device **Diagnostics** view.
- **On-screen health** — since there's no server/`/health`, the dashboard shows a
  discreet "last synced HH:MM" + colored dot; red on auth failure / repeated sync
  error. This *is* the health endpoint for a device with no operator watching
  logs.
- **Auth-failure UX** — app-specific passwords can be revoked; app must show a
  clear "Reconnect iCloud" state and keep serving the cached view read-only.
- **Time** — store UTC, render local (global rule); careful all-day vs timed +
  DST via VTIMEZONE.
- **Security** — credential in Keystore-backed EncryptedSharedPreferences; TLS
  (consider cert pinning to iCloud); **debounce/rate-limit writes** so rapid touch
  edits don't hammer CalDAV (global rule: rate-limit write ops); the bot account
  is scoped to exactly the shared calendar + list (least privilege by sharing, not
  by full account access).

---

## 12. Resolved PRD §9 Open Questions — summary

| PRD §9 question | Resolution | Section |
|---|---|---|
| Sync-loop: CTag/ETag polling vs push invalidation | ✅ **Resolved:** `sync-collection`/`sync-token` primary + CTag/ETag fallback; **push confirmed unavailable** to third parties → adaptive polling via foreground service with `429/503` backoff | §5.3, §5.5 |
| Reminders list discovery | Calendar collection advertising `VTODO` in supported-component-set; same `Depth:1` enumeration | §4.2 |
| Conflict resolution UX | Optimistic concurrency (`If-Match`/`412`) → disjoint-field auto-merge → same-field newest-wins by SEQUENCE/LAST-MODIFIED → logged | §5.6 |
| Pixel-level UX/visual design | **Deferred to a design pass** (out of scope here) | — |
| Music control via Spotify | **Deferred**; if revisited, a Web Playback SDK **webview tile**, not native embed | §9 |

---

## 13. Risks & Milestones

### 13.1 Top risks
| # | Risk | Mitigation |
|---|---|---|
| R1 | ✅ **RESOLVED → Strategy B (calendar-per-category).** Residual: (a) the PRD's per-event-color premise was false — **PRD §1/§4.1/§8.3 wording needs updating** to "category calendars"; (b) B relies on the family **provisioning + sharing** each category calendar from an Apple device (bot can't `MKCALENDAR` reliably) | §6.3 model; onboarding checklist for the family to create/share calendars; discovery auto-reflects them |
| R6 | **Cross-calendar move (re-categorization) is the only data-loss-capable op** (DELETE→PUT, §6.6) | Durable `PENDING_MOVE` outbox op carrying full ICS; Room stays source of truth; verified in M0 |
| R2 | Device-owner kiosk not grantable on the Apolosign | Launcher-replacement fallback (§10); verified in M0 |
| R3 | Recurrence editing leaks non-native structure | Strict master+RECURRENCE-ID model, ical4j expansion, "this/all" UX (§6.5) |
| R4 | iCloud throttles/blocks a polling bot account | Adaptive backoff, cheap ctag checks, generous idle interval (§5.5) |
| R5 | App-specific password revoked silently | First-class auth-failure state + read-only cached view (§11) |

### 13.2 Milestones (tracer-bullet, vertical slices)
- **M0 — Spikes (do first).** Color strategy is **decided (B)**. Remaining probes:
  (a) bot sharee can **read `apple:calendar-color`** on each shared category
  calendar and **`PUT` an event** into each; (b) a **cross-calendar move**
  (DELETE→PUT, §6.6) round-trips cleanly for a sharee; (c) verify each account's
  `sync-collection` support + partition host; (d) **device-owner / adb kiosk**
  feasibility on the Apolosign. *Gates M2 + §10.*
- **M1 — Read path.** Discovery → pull → Room → read-only **agenda + month view**
  rendering *real* family data. Proves the local-first pipeline end to end.
- **M2 — Write path.** Create/edit event with `If-Match`/conflict handling;
  per-event color *iff* M0 passed.
- **M3 — Reminders.** VTODO read/write + complete-toggle + filtered to-do tile.
- **M4 — Chores.** Local subsystem + weekly payout.
- **M5 — Slot framework.** `TileProvider` registry + weather webview tile +
  message board.
- **M6 — Kiosk hardening.** Boot/crash recovery, immersive, burn-in mitigation,
  crash reporting + diagnostics view.

---

---

## 14. Protocol evidence (load-bearing citations)

Decisions above trace to these (research pass 2026-08-12); items marked *test*
have no citable source and are empirical M0 checks:

- **Stock Apple Calendar colors by calendar, not by event** — Macworld "color-code
  events" (per-calendar only); BusyCal iCloud docs: BusyCal tags/colors "do not
  appear in Calendar or other clients." *(HIGH)*
- **`COLOR` = RFC 7986 property on VEVENT, CSS3 named value** — RFC 7986 §5.9;
  DAVx5 maps event color to/from it since DAVdroid 1.7. *(HIGH)*
- **iCloud preserves unknown/`X-`/`COLOR` bytes on round-trip** — BusyCal `X-` tags
  sync between BusyCal via iCloud. *(MEDIUM-HIGH; sharee-on-shared case = test)*
- **iCloud supports `sync-collection`/`sync-token`** — captured `p34-caldav.icloud.com`
  responses (Aurinko); iCalDAV Kotlin client; DAVx5 uses collection-sync. *(MED-HIGH;
  ship CTag fallback for partition variance)*
- **No third-party push** — BusyCal ("iCloud does not allow third-party apps to
  subscribe to push"); WebDAV-Push is Nextcloud-only. *(HIGH)*
- **Per-account partition hosts `pNN-caldav.icloud.com`; app-specific Basic auth;
  VTODO vs VEVENT via supported-component-set; `MKCALENDAR` unreliable; no VJOURNAL/
  freebusy; UID uniqueness** — Aurinko, Nylas, vdirsyncer, python-caldav#3. *(HIGH,
  except rate limits = undocumented → backoff)*

---

*Status: protocol findings folded in; color **Strategy B (calendar-per-category)**
chosen 2026-08-12. The doc is **build-ready for M1**. Open follow-ups: (1) update
the PRD's single-calendar language (§1/§4.1/§8.3) to the category-calendar model;
(2) write the family onboarding checklist for creating + sharing category
calendars to the bot.*
