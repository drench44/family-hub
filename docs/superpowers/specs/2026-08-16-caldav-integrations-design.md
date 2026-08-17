# iCloud CalDAV + Extensible Integrations — Design Spec

Date: 2026-08-16
Status: **DRAFT — pending operator sign-off** on the open decisions in §14.
Owner: Gary

## Summary

Add authenticated, **two-way iCloud CalDAV** to family-hub so the wall can both
read and write the family's iCloud **calendars** (VEVENT) and **Reminders**
(VTODO). Wrap it — and every existing data source — in a new **Integrations**
subsystem: a runtime registry with one common surface to turn each integration
on and off, configure it, and see its health. The hub becomes "fully
extensible": adding an integration is registering one provider class; enabling
or disabling one is a row update, not a redeploy.

The whole addition is **additive and feature-flagged**. With no iCloud
credential in `.env`, the CalDAV subsystem no-ops and the app behaves
byte-identically to today. Every existing source (Google, ICS, cameras,
weather, climate) seeds into the registry as **enabled**, so a current
deployment's Integrations panel shows everything ON and the wall is unchanged.

### Decisions locked with the operator (2026-08-16)

- **Reminders = a separate new surface.** The existing local To-Dos tab is
  untouched. iCloud Reminders get their own card + surface, clearly distinct.
- **CalDAV client = the `caldav` PyPI library** (battle-tested against iCloud),
  not a hand-rolled client. This is a deliberate, accepted exception to the
  repo's minimal-dependency ethos; rationale in §3.
- **Two-way** (read + write) for both calendars and reminders.
- **A common Integrations control center** — turn integrations on/off, configure
  them, and read their status in one place ("fully extensible family hub").

### Decisions still needed → see §14 before implementation starts.

## Non-goals (explicitly out of scope for v1)

- **Cross-calendar move / re-categorization** (the design doc's Strategy B §6.6).
  v1 writes events into whichever calendar they already belong to or one the user
  picks at create time; it does not move an existing event between calendars.
- **Creating iCloud calendars from the hub** (`MKCALENDAR` is unreliable on
  iCloud — TECHNICAL_DESIGN §4.3). The family creates/shares calendars from an
  Apple device; the hub discovers them.
- **Per-event `COLOR` writes.** Color is read from each calendar's
  `apple:calendar-color` and is read-only (TECHNICAL_DESIGN §6.3, Strategy B).
- **Recurrence *editing*** in v1 (this/all-events split, override instances).
  v1 reads and expands recurring events (already supported for ICS) and writes
  only **single, non-recurring** events + reminders. Recurrence editing is a
  later slice; it's the known complexity hotspot (design doc §6.5).
- **Replacing or migrating the local To-Dos tab.** It stays as-is (operator
  decision above).
- **Auth on the hub itself.** It remains LAN-only, no-auth — but see the security
  note in §11 about the hub now being a *write* proxy to iCloud.

## 1. Architecture overview

Three new layers, each additive:

```
┌────────────────────────────────────────────────────────────────┐
│  UI                                                             │
│  ├─ Calendar (existing) — now unions CalDAV events             │
│  ├─ Reminders surface (NEW) — iCloud VTODO, two-way            │
│  └─ Integrations settings (NEW) — toggle/configure/status      │
└───────────────▲────────────────────────────────────────────────┘
                │ /api/hub, /api/reminders, /api/integrations
┌───────────────┴────────────────────────────────────────────────┐
│  INTEGRATION REGISTRY (NEW)                                     │
│  IntegrationProvider registry + `integrations` DB state overlay │
│  kinds: google_calendar · icloud (caldav) · cameras · weather  │
│         · climate · (todos/chores are local, always-on)        │
└───────────────▲────────────────────────────────────────────────┘
                │ enabled? config? → drives sync + rendering
┌───────────────┴────────────────────────────────────────────────┐
│  CALDAV SUBSYSTEM (NEW)                                         │
│  discovery · pull (change-detect→fetch→upsert) · push (outbox) │
│  · conflict resolver — the design doc §5 engine, in Python      │
└───────────────▲────────────────────────────────────────────────┘
                │ caldav lib (PROPFIND/REPORT/PUT/DELETE) over TLS
┌───────────────┴────────────────────────────────────────────────┐
│  iCloud CalDAV — bot Apple ID + app-specific password (.env)   │
└────────────────────────────────────────────────────────────────┘
```

The **one rule that keeps the wall fast** (same as the design doc §2.2 and the
existing Google/ICS path): no request handler waits on iCloud. A wall write is
optimistic — it writes local DB state immediately, the wall re-renders from the
DB immediately, and the CalDAV outbox flushes to iCloud in the background and
reconciles the returned ETag.

## 2. Feature flag = credential presence

The gate is the bot credential in `.env` (git-ignored; the repo is public):

```
# .env (NOT committed)
ICLOUD_CALDAV_USER=bot-appleid@icloud.com
ICLOUD_CALDAV_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx      # app-specific password
# optional:
ICLOUD_CALDAV_READONLY=0                            # 1 = never write (see §11)
```

`caldav_configured()` returns False when either is absent → the `icloud`
integration renders as "not connected," discovery/sync/push all short-circuit,
zero rows are written, and **no existing behavior changes**. This mirrors the
established `GoogleCalendarClient.configured()` gate (`calendar_sync.py:143`).

The credential lives **only** in `.env`. `config.json` (public-safe) never holds
it; neither does the DB.

## 3. Technology: the `caldav` library

Chosen over a hand-rolled client (operator decision). Rationale:

- CalDAV discovery, XML namespaces, `sync-collection`/CTag fallback, and iCloud's
  undocumented quirks (per-account partition hosts `pNN-caldav.icloud.com`,
  cross-host redirects, VTODO enumeration finickiness) are exactly the
  error-prone surface the design doc warned against hand-rolling
  (TECHNICAL_DESIGN §3: "Rejected hand-rolling WebDAV XML (error-prone)").
- The `caldav` package is the mature Python client, tracks iCloud behavior, and
  reuses `icalendar` — **already a dependency** — for parse/generate.
- Accepted cost: new deps (`caldav`, transitively `lxml`, `vobject`). Recorded
  as a deliberate exception to the lean-repo ethos. `requirements.txt` gains
  `caldav>=1.3`.

All CalDAV access is wrapped in a **service layer** (`caldav_service.py`) so the
library is swappable and the rest of the app never imports `caldav` directly
(global rule: wrap external API calls in a clean service layer).

## 4. Integration registry (the "extensible" core)

### 4.1 Provider interface (new module `integrations.py`)

```python
class IntegrationProvider:
    kind: str                 # stable id: "icloud", "google_calendar", ...
    display_name: str
    configurable: bool        # shows a config affordance in settings
    def is_available(self, env, cfg) -> bool     # creds/config present?
    def discover(self, conn) -> None              # optional: enumerate children
    def sync(self, conn, cfg, now) -> dict        # pull/push; returns status
    def status(self, conn) -> dict                # {ok, error, needs_auth, ...}
```

Providers are registered in a module-level list (the registry). Adding an
integration = write one provider + register it. v1 providers:

| kind | wraps | local? | two-way? |
|---|---|---|---|
| `google_calendar` | existing `GoogleCalendarClient` path | no | read-only |
| `icloud` | NEW CalDAV (calendars + reminders) | no | **yes** |
| `cameras` | go2rtc config | no | n/a (display) |
| `weather` | weather tile proxy | no | n/a |
| `climate` | climate tile proxy | no | n/a |

`todos` and `chores` are local, always-on, and not togglable (they can't
"disconnect"); they may still appear in the panel as informational "built-in".

### 4.2 State overlay (`integrations` table)

Static definitions live in `config.json` as today; **runtime enable/disable and
per-integration config** live in the DB so they're togglable without editing a
file or redeploying:

```sql
CREATE TABLE IF NOT EXISTS integrations(
  id         TEXT PRIMARY KEY,     -- = kind for singletons; kind+':'+child id otherwise
  kind       TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  sort       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

On startup, `seed_integrations(conn, cfg)` inserts a row (`enabled=1`) for every
provider available from the current `config.json`/`.env` that has no row yet.
**This is the non-breaking hinge:** an existing deployment gets every current
source seeded ON, so nothing disappears; new toggles only ever *add* capability.

### 4.3 How enable/disable takes effect

- **Sync loop** (`sync_loop`, `app.py`): each tick iterates enabled providers
  only; a disabled provider is skipped (its cached data may be cleared or frozen
  per §7).
- **Rendering**: `/api/hub` and the Reminders/calendar blocks read only enabled
  integrations' data; a disabled camera/weather/panel tile hides.
- Toggling is a `PATCH /api/integrations/{id}` → row update → effective next poll
  (≤ the loop interval), no restart.

## 5. CalDAV subsystem

### 5.1 Discovery (`caldav_service.discover`)

Once on first sync and on `401/404/412` drift or manual "rediscover":
principal → `calendar-home-set` → enumerate collections (the `caldav` lib does
the PROPFIND dance). For each collection persist url, component type
(VEVENT/VTODO), `displayname`, `apple:calendar-color`, ctag/sync-token, and the
privilege set. Classify: VEVENT collections = calendars; the VTODO collection =
the reminders list (TECHNICAL_DESIGN §4.2).

```sql
CREATE TABLE IF NOT EXISTS caldav_collections(
  id          TEXT PRIMARY KEY,     -- stable hash of the collection URL
  comp_type   TEXT NOT NULL,        -- 'VEVENT' | 'VTODO'
  url         TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  color       TEXT,                 -- apple:calendar-color, read-only
  writable    INTEGER NOT NULL DEFAULT 0,
  enabled     INTEGER NOT NULL DEFAULT 1,   -- per-collection visibility toggle
  ctag        TEXT,
  sync_token  TEXT,
  last_sync_at TEXT,
  last_error  TEXT
);
```

The Integrations panel shows discovered collections as children of the `icloud`
integration, each with its own visibility toggle + color swatch.

### 5.2 Two-way object store

CalDAV objects need durable per-row sync state that the read-only
`replace_events` path must **never** touch. To keep that isolation unambiguous,
CalDAV data lives in its **own tables**, not the existing `events` table:

```sql
CREATE TABLE IF NOT EXISTS cal_objects(
  id           TEXT PRIMARY KEY,    -- collection_id + '/' + uid (+ '/' + recurrence_id)
  collection_id TEXT NOT NULL,
  comp_type    TEXT NOT NULL,       -- 'VEVENT' | 'VTODO'
  uid          TEXT NOT NULL,
  href         TEXT,                -- resource URL on the server
  etag         TEXT,                -- last known server ETag
  base_etag    TEXT,                -- ETag the local edit was based on (If-Match)
  -- rendered fields (cache of the ICS):
  summary TEXT NOT NULL DEFAULT '', start_utc TEXT, end_utc TEXT,
  all_day INTEGER NOT NULL DEFAULT 0, tzid TEXT, due_utc TEXT,
  location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  status TEXT, priority INTEGER, completed_at TEXT,
  color TEXT,                       -- from the collection (read-only)
  -- conflict signaling (native iCalendar):
  sequence INTEGER NOT NULL DEFAULT 0, last_modified TEXT,
  raw_ics TEXT,                     -- round-trip fidelity (design doc P3/§7)
  -- outbox:
  sync_state TEXT NOT NULL DEFAULT 'SYNCED',  -- SYNCED|PENDING_CREATE|PENDING_UPDATE|PENDING_DELETE|CONFLICT
  local_modified_at TEXT, sync_attempts INTEGER NOT NULL DEFAULT 0,
  last_sync_error TEXT
);
```

Rationale for a separate table over adding a `source` column to `events`: the
existing `replace_events` deletes-and-reinserts whole calendars each pull. Sharing
one table with two-way outbox rows invites a clobber bug (the exact "silently
wrong-but-reassuring" failure the repo's review gate hunts for). Separate tables
make the ownership boundary a hard wall. The wall's calendar block reads a
**union** of `events` (Google/ICS, read-only) and `cal_objects` VEVENT rows.

### 5.3 Pull pass

Per **enabled** collection: cheap change-detect (sync-token/ctag), then fetch
changed objects, parse ICS → upsert `cal_objects`. Reuse the existing hardening
philosophy from `sync_once`: per-collection failure isolation, and the
valid-but-empty guard (don't let a maintenance-window empty response wipe a
calendar the family relies on — `calendar_sync.py:275-324`). **Never clobber a
`PENDING_*` row** — mark it `CONFLICT` for §5.5.

### 5.4 Push pass (outbox)

Rows with `sync_state != SYNCED`, oldest first, via the `caldav` lib:
- **Create:** PUT with `If-None-Match: *` → store ETag → `SYNCED`.
- **Update:** regenerate ICS (patch the parsed model back over `raw_ics` to
  preserve unmodeled props — C1 fidelity), PUT with `If-Match: base_etag`.
- **Delete:** DELETE with `If-Match: base_etag`.
- **Complete a reminder** = an update (STATUS `COMPLETED` + `COMPLETED` stamp).
- Bump `SEQUENCE` on every semantic update (native change signaling).
- `412 Precondition Failed` → §5.5.
- **Rate-limit / debounce writes** and back off on `429/503` (global rule;
  TECHNICAL_DESIGN §4.3, §5.5).

**Burst after a local write** so the wall feels live and phones see the change
fast: an API write sets the outbox row + wakes a push pass promptly (a threading
Event the sync loop waits on, instead of always sleeping the full interval),
then decays to the idle cadence.

### 5.5 Conflict resolution (design doc §5.6, in Python)

Re-fetch on `412`/pull-collision → **disjoint-field auto-merge** (local + remote
touched different props) → else **same-field newest-wins** by SEQUENCE then
LAST-MODIFIED (native signals, C1-safe) → **always log to `sync_log`** (already
exists as a concept; add a diagnostics view). No modal on the wall.

## 6. Reminders surface (NEW, separate from local To-Dos)

- **Data:** `cal_objects` rows with `comp_type='VTODO'` from enabled VTODO
  collections. Two-way: complete/uncomplete, add, edit title/due, delete.
- **Grouping (pure module `reminders.py`, stdlib only, testable):** Overdue /
  Today / Upcoming / No date / (done-today lingering, like the To-Dos rule).
  Maps DUE, PRIORITY, STATUS, COMPLETED (design doc §6.2).
- **Wall + phone:** a distinct **Reminders** card and full view/tab, visually
  separate from To-Dos so the two lists are never confused (operator decision).
  Reuses the existing card/overlay/optimistic-toggle patterns and the fails-soft
  rules (a Reminders problem never blanks the wall).
- **Offline honesty:** a completed reminder shows done immediately (optimistic),
  with a subtle "syncing" state until the outbox confirms; a persistent push
  failure surfaces (not a silent swallow — the repo's non-negotiable).

## 7. `/api/hub` and new endpoints

- `/api/hub` gains a `reminders` block (visible items, grouped) and an
  `integrations` health summary (status dots), so neither new surface needs
  extra polling. Existing keys unchanged.
- `GET/POST/PATCH/DELETE /api/reminders...` — mirror the todos routes' shape and
  validation conventions (pydantic models, 422/404, `_row` helpers).
- `GET /api/integrations` → list with status; `PATCH /api/integrations/{id}`
  (enable/disable, config); `PATCH /api/integrations/collections/{id}`
  (per-collection visibility); `POST /api/integrations/icloud/rediscover`.

Disabled-integration semantics: on disable, its rows are **frozen** (kept but not
synced) rather than deleted, so re-enabling is instant and non-destructive;
the panel labels them "paused." A dedicated "remove/forget" action is the only
path that deletes.

## 8. Integrations settings UI (NEW)

One common surface (operator's "common location"): a **Settings/Integrations
overlay** reachable from a discreet gear on the wall (reusing the overlay
pattern, no iframe) and a phone Settings section. Each integration row:
name · status dot (ok / error / needs-auth / paused / not-connected) · enable
toggle · configure affordance. The `icloud` row expands to its discovered
collections, each with a visibility toggle + color swatch, plus a **"Reconnect
iCloud"** state when the app-specific password is revoked (design doc §11
auth-failure: keep serving the cached view read-only, show the banner, never
crash).

## 9. Non-breaking guarantees (the explicit contract)

1. No iCloud creds in `.env` → CalDAV subsystem is fully inert; app is identical
   to today.
2. Every existing source seeds into `integrations` as **enabled**; a current
   deployment's panel is all-ON and the wall is unchanged.
3. All new tables via `CREATE TABLE IF NOT EXISTS`; no changes to the `events`,
   `todos`, or chores tables (CalDAV data is in its own tables). `ensure_schema`
   needs no destructive migration.
4. The local To-Dos tab, Google path, and ICS path are untouched.
5. A guard test asserts that with CalDAV unconfigured, `/api/hub` output is
   unchanged from the pre-feature baseline.

## 10. Data model summary (new tables only)

`integrations`, `caldav_collections`, `cal_objects` (all above), plus reuse of
the existing `kv` table for `caldav_status` / sync bookkeeping and `sync_log`
for the diagnostics trail. No existing table is altered.

## 11. Security & resilience

- **Secrets:** iCloud app-specific password in `.env` only (git-ignored). Never
  in `config.json` (public repo), never in the DB, never logged.
- **The hub is now a write proxy to iCloud — a real escalation.** Today the hub
  is read-only toward external services; two-way CalDAV means anyone on the LAN
  can mutate the family's iCloud calendar/reminders through the no-auth hub.
  Mitigations: keep the LAN-only bind (existing `HUB_BIND`), scope the bot to a
  least-privilege sharee, and honor `ICLOUD_CALDAV_READONLY=1` as a hard
  kill-switch on all writes. **Flag for operator awareness (§14).**
- **Rate-limit/debounce writes**; exponential backoff on `429/503`; conservative
  idle poll interval (TECHNICAL_DESIGN §4.3).
- **Auth-failure is a first-class state** (revoked app password → banner +
  read-only cached view, no crash).
- **Observability:** `sync_log` diagnostics view; per-integration "last synced /
  error" on the settings surface = the health signal for a device nobody watches.
- **UTC storage, local render** throughout (global rule); careful all-day vs
  timed + DST (the ICS path already does this — reuse it).

## 12. Testing

Following the repo's TDD + fails-soft conventions, and encoding mobile/iOS fixes
as static guards (per CLAUDE.md):

- **Pure logic unit tests:** `reminders.py` grouping/visibility (mirrors
  `test_todos.py`); ICS↔model normalization for VTODO; conflict-resolution
  decision function (disjoint-merge vs newest-wins) as a pure function with
  injected SEQUENCE/LAST-MODIFIED.
- **CalDAV service tests with the network mocked** (no live iCloud in CI): the
  `caldav` client is injected/faked exactly as `GoogleCalendarClient` and
  `ics_fetch` are today, so `sync`/`push`/`conflict` are testable offline.
- **Outbox/state-machine tests:** create→PUT→SYNCED; 412→conflict→resolve;
  interrupted push retries from the outbox (no data loss).
- **Non-breaking guard:** CalDAV-unconfigured `/api/hub` equals baseline; a
  disabled integration hides its tile; seeding is idempotent on an existing DB.
- **API tests:** reminders CRUD + complete lifecycle; integrations
  enable/disable; per-collection visibility; validation 422s / 404s.
- **JS helper tests** per the `tests/js` pattern for any new pure render helpers.
- **Verify tests genuinely RUN** (the JS suite skips loudly without node/docker —
  a skip is not a pass).
- **Review gate (required):** the three agents
  (`pr-review-toolkit:silent-failure-hunter`, `code-reviewer`,
  `pr-test-analyzer`) run on each slice's branch diff before merge, plus a
  whole-branch review across the milestones.
- **Real-device check** for any phone-facing Reminders/Settings UI (CLAUDE.md
  mobile/iOS gate): render at ≤400px and confirm on a real iPhone.

## 13. Milestones (tracer-bullet vertical slices)

Each is an independently-reviewable, mergeable slice; each keeps the app shippable.

- **M0 — Spike (small).** With the bot creds in a local `.env`, prove the
  `caldav` lib discovers the shared collections, reads `apple:calendar-color`,
  reads VEVENT + VTODO, and can PUT/DELETE a throwaway object as the sharee.
  Confirms the account works before building on it.
- **M1 — Registry + read calendars.** `integrations` table + provider registry +
  seeding (existing sources seed ON, non-breaking). CalDAV discovery + read-only
  pull of iCloud VEVENT into `cal_objects`; wall calendar unions them. Flagged
  off without creds.
- **M2 — Reminders surface (read-only).** VTODO pull + `reminders.py` grouping +
  the new Reminders card/tab. Read-only first.
- **M3 — Integrations settings UI.** The common on/off surface: enable/disable,
  per-collection visibility, status dots, "Reconnect iCloud" state.
- **M4 — Two-way Reminders.** Outbox + If-Match/412 + conflict resolver + burst
  push, wired to complete/add/edit/delete on the Reminders surface. First write
  path (lower blast radius than calendar).
- **M5 — Two-way Calendar.** Create/edit/delete single (non-recurring) events;
  calendar picker for the target collection at create.
- **M6 — Hardening.** Rate-limit/backoff, auth-failure UX, `sync_log`
  diagnostics view, read-only kill-switch, docs + onboarding checklist.

Recurrence editing and cross-calendar moves are explicit follow-ons after M6.

## 14. Open decisions needed before implementation

1. **Bot iCloud account.** Confirm you have (or will create) a dedicated bot
   Apple ID with 2FA + an app-specific password, added as an *editor* sharee on
   the calendars and the reminders list. (A personal Apple ID works but is
   higher-blast-radius; a dedicated bot is the least-privilege choice.)
2. **LAN write-proxy risk (§11).** Acknowledge that two-way CalDAV lets any
   LAN device mutate the family's iCloud via the no-auth hub. OK as-is (LAN-only
   bind), or do you want the `ICLOUD_CALDAV_READONLY` kill-switch defaulted ON
   until you say otherwise, and/or a lightweight write-PIN later?
3. **Reminders surface placement.** A new phone **tab** (making six tabs — tab
   bar space is tight per CLAUDE.md), or a card + overlay only (no new tab), or
   folded into the calendar area? Recommendation: card + full-screen overlay on
   the wall, and reachable from the calendar/settings on phone, to avoid a sixth
   tab.
4. **Disabled-integration data:** freeze (keep, paused) vs clear. Recommendation:
   **freeze** (non-destructive, instant re-enable); explicit "forget" to delete.
5. **Scope confirmation:** v1 writes = single events + reminders only (no
   recurrence editing, no cross-calendar moves). Confirm that's acceptable for a
   first cut.

---

*Next step after sign-off: turn this into a task-by-task TDD plan under
`docs/superpowers/plans/`, mirroring the to-dos plan, and build M0→M1 first.*

---

## 15. Architecture review follow-ups (Fable, 2026-08-17)

A Fable architecture review of the shipped read-only feature validated the
load-bearing seams (service boundary, credential-as-flag, source-isolation
policy, fails-soft posture, the declarative-registry-plus-DB-overlay shape — the
`TileProvider` class registry from §9 is deliberately NOT adopted; it was for a
Compose per-tile app, and this server-rendered wall doesn't need render-owning
provider classes). It flagged real divergences from this spec. Recording them as
DECISIONS with timing so they are not accidental:

- **APPLIED now (rec 4):** one `_integration_on(conn, id)` gate (availability ∩
  toggle) used by every render/sync path so "is it on" can't drift; the unused
  `calendar_kind_enabled` seam was deleted.
- **`cal_objects` store (rec 1) — deferred to the two-way slice (M4/M5), by
  design.** The read path stores recurrence-EXPANDED occurrence rows in the
  shared `events` table and drops UID/ETag/`raw_ics` at parse. That is a
  read-only cul-de-sac: none of §5.2's outbox prerequisites (per-object identity,
  `base_etag`, `raw_ics` for C1 round-trip, per-row `sync_state`) survive it.
  **Decision:** the read-only card MAY ship on the current storage; NOTHING
  writable may be built on it. M4 lands `cal_objects` FIRST (reminders as the
  first tenant — the kv `caldav_reminders` blob also has a read-modify-write race
  under completion toggling), persists `raw_ics`/`uid`/`href`/`etag` from the
  first pull, and moves the VEVENT read path onto it (keeping `events` as an
  optional render projection). Planned build-out, not a rewrite.
- **`caldav_collections` table + change detection (rec 2) — deferred to before
  two-way / before shortening the poll interval.** Today every 300s tick
  re-`discover()`s and full-range `search()`es every collection (the §4.3/§5.2
  throttling risk). **Decision:** acceptable at the idle read-only cadence;
  land the `caldav_collections` table (ctag/sync-token, `writable`,
  per-collection `enabled`, `last_error`) with `cal_objects`. It is also the home
  for the per-collection visibility toggle (§8) and the ctag/sync-token change
  detection (§5.3).
- **Registry `sync`/`status_key` descriptors (rec 3) + uniform
  disabled-integration semantic (rec 5) — deferred to before a 4th synced
  source.** Note the granularity mismatch: `calendar_sync.sync_once` syncs Google
  AND ICS together, so a clean per-integration `sync` callable wants the
  `cal_objects` refactor first. Until then `_sync_tick` stays hardcoded.
  **Decision on the semantic:** disabling an integration should SKIP its sync
  (freeze cache) + hide render — the CalDAV behavior; Google/ICS currently only
  hide at render (still polling). Apply uniformly when rec 3 lands.
- **Shared valid-but-empty TTL guard (rec 6) — deferred (safe refactor).** The
  ~40-line empty-keep state machine now lives in both syncs; extract one pure
  function, reconciling the divergent edge handling (calendar_sync's "all sources
  down → keep whole cache" vs caldav_sync's "empty discover → keep all") with
  care, guarded by the existing tests.
- **`_cal` handle through the service boundary + `LIKE 'caldav:%'` scoping (rec
  7) — fold into recs 1–2.** Both are contained; no standalone migration for
  purity.
- **Frontend seams (rec 8) — with the reminders card slice.** The `/api/hub`
  reminders block needs the `configured`/`ok` distinction the todos block models
  with `todos_ok` (the `/api/reminders` endpoint already has it); per-collection
  children need the collections in the payload (rec 2 provides them). The
  `body.integ-off-<id>` gating pattern is good — keep it.
