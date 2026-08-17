# Toggleable Chores & To-Dos — Feature Toggles Design Spec

Date: 2026-08-17
Status: **APPROVED** — design confirmed with operator in chat (2026-08-17).
Owner: (this branch) — builds directly on Gary's #40 "Extensible
integrations: settings menu + iCloud CalDAV".

## Summary

Make the family-hub wall **fully customizable**: any household can turn each
feature on or off and keep only what they need. Today the external integrations
(cameras, weather, climate, iCloud CalDAV) are already toggleable server-side;
the two core surfaces — **Chores** and **To-Dos** — are always-on. This change
adds Chores and To-Dos as two more entries in the existing integrations
registry Gary built in #40, so they get the same server-side on/off switch, and
hardens the wall + mobile shell so toggling anything off degrades gracefully
instead of leaving dead columns and empty tabs.

The change is **additive and non-breaking**. Seeding is `INSERT OR IGNORE` and
an un-seeded integration reads as *enabled*, so existing deployments upgrade
with both features ON and the wall is byte-identical until the operator flips a
switch.

### Decisions locked with the operator (2026-08-17)

- **Persistence = server-side (the box), not per-browser.** On/off lives in the
  SQLite `integrations` table and is delivered to every screen via `/api/hub`,
  exactly like cameras/weather/iCloud. One switch flips it for the whole house
  (wall, phones, tablets). Per-browser `localStorage` stays reserved for
  cosmetics only (theme, accent, columns, Auto/Desktop layout).
- **Settings UI = a labeled "Features" group.** Chores and To-Dos render under a
  "Features" sub-header, above the existing "Integrations" list of external
  services. Same `.integ-row` switch rows, same Settings card.
- **All-off = allowed, degrade gracefully.** The operator may turn everything
  off. Off features hide their wall column/tile and their phone tab; the mobile
  tab bar falls back to the first enabled tab; if *everything* is off, a
  friendly "All features are turned off — open Settings" panel shows instead of
  a broken blank.
- **Mobile tab fix is general.** The tab-visibility + active-tab-fallback logic
  is data-driven for *all* toggleable tabs, which also fixes an existing latent
  bug: turning off cameras or weather today leaves their phone tabs present
  showing an empty surface.

## Non-goals (out of scope)

- No new persistence pattern, table, or migration. Reuses the `integrations`
  table.
- No `config.json` changes; nothing new to rsync-exclude on the deploy box.
- No change to the *contents* of Chores or To-Dos, their data model, or their
  admin/edit flows. This is purely about show/hide.
- No per-browser feature overrides (deliberately rejected — see locked
  decisions).
- No persistence of "last active mobile tab" across reloads (the HTML default
  `chores` tab is reconciled to a visible tab on first render; that's enough).

## Architecture

### 1. Registry (backend) — `src/family_hub/integrations.py`

`available_integrations()` gains two descriptors, `chores` and `todos`, marked
**always available** (`available=True`) and tagged `group:"feature"`. Every
existing descriptor gains `group:"integration"`. The `group` field is the only
new descriptor attribute; it flows through unchanged everywhere the registry is
read.

Rationale: this is exactly the extension point the module docstring anticipates
("adding an integration is adding a descriptor here — the settings menu,
seeding, and gating all read this list").

### 2. Seeding & gating (backend) — `src/family_hub/app.py`, `db.py`

- Startup seeding (`_init_db_once`) already seeds a row (enabled) for each
  available integration; chores/todos are picked up automatically. **Non-breaking:
  both come up enabled on upgrade.**
- The existing gating predicate `_integration_on(c, iid)` works unchanged for
  chores/todos (available + enabled).
- `_integrations_state` (the `/api/hub` `integrations` payload) carries the new
  `group` field per integration so the frontend can split the Settings list.
- **No server-side blanking of chores/todos data** (unlike cameras, whose URLs
  are secret). Chores/todos data is not sensitive; it keeps flowing so
  toggling back on is instant with no re-fetch gap. Visibility is handled in
  the frontend (CSS + tab logic), matching the weather/climate slot pattern.

### 3. Settings UI — `hub.js` `renderIntegrations`

Split the flat switch list by `group`: a **Features** sub-header (chores,
todos) then an **Integrations** sub-header (external services). Same
`.integ-row` role="switch" rows; same failed-PATCH guard (a rejected toggle
must not silently revert the switch). The existing per-integration
`body.integ-off-<id>` stamping is generic and covers chores/todos with no
change.

### 4. Hiding surfaces — CSS (`styles.css`)

Belt-and-suspenders via the already-stamped body classes:

- Wall: `body.integ-off-chores .people-col { display:none }`,
  `body.integ-off-todos #todo-slot { display:none }`. **Verify the 1920px wall
  reflows cleanly** on a real full-width screenshot (per CLAUDE.md) — no dead
  column gap.
- Mobile surfaces are hidden by the same tab logic below.

### 5. Mobile tab bar — data-driven (the robustness core) — `hub.js`

Today the tab bar is static HTML (5 hardcoded buttons), `<body data-tab="chores">`
is a hardcoded startup tab, and `setTab` has no hide/fallback logic. Replace the
implicit "all tabs always present" assumption with a table mapping each tab to
its backing feature(s); a tab is visible iff **any** backing feature is enabled:

```
chores  -> [chores]
todos   -> [todos]
cal     -> [google_calendar, ics_calendar, icloud_caldav]
cams    -> [cameras]
weather -> [weather, climate]
```

On every poll/render, from the enabled set in `data.integrations`:

1. Hide the tab button (and its surface) for any tab with no enabled feature.
2. If the currently-active tab just became hidden, switch to the first visible
   tab (in bar order).
3. Reconcile the hardcoded `chores` startup default the same way (if chores is
   off at load, the first render moves to a visible tab).

This closes the existing cameras/weather empty-tab latent bug in the same
stroke. iOS safety: buttons hide with `display:none` only — no changes to
`position:fixed`, `transform`, or `backdrop-filter`, so the tab-bar tappability
guards still hold.

### 6. Everything-off empty state — `hub.js` + `styles.css`

When the enabled feature set is empty, stamp `body.hub-empty` and show a
centered `.empty-hub` panel — "All features are turned off — open Settings to
turn some on" with a button that opens the Settings overlay — on both wall and
mobile. Turning anything back on removes the class; instant, no re-fetch.

### 7. Defensive edge handling

- `openOverlay('chores')` / `openOverlay('todos')` no-op when that feature is
  off (the section head that launches them is hidden, but guard the entry point
  too).
- To-Do **source picker** in Settings stays visible even when To-Dos is off (the
  operator may configure the source before enabling). It's a sub-setting of the
  todos feature, not gated by it.

## Data flow

```
integrations.available_integrations()   # + chores, todos (group=feature); others group=integration
        │  (startup seed: INSERT OR IGNORE, enabled)
        ▼
SQLite integrations table  ──PATCH /api/integrations/{id}──►  set enabled
        │
        ▼
/api/hub  _integrations_state  ──►  data.integrations[] { id, enabled, group, ... }
        │
        ▼
hub.js renderIntegrations(data):
  • split Settings list into Features / Integrations groups
  • stamp body.integ-off-<id> for each disabled feature   (CSS hides wall column/slot)
  • compute visible tabs from enabled set; hide buttons; reconcile active tab
  • stamp body.hub-empty when no feature is enabled        (CSS shows .empty-hub)
```

## Error handling

- Failed `PATCH /api/integrations/{id}` → the switch does not silently flip
  (existing `.ok` check retained). Operator sees the real state on next poll.
- Toggling an unknown id → existing 404 path unchanged.
- Poll returning no `integrations` (older/partial payload) → treat as "all
  visible" (fail-open to today's behavior), never blank the wall on a transient
  gap.

## Testing

- `tests/test_integrations.py` — chores/todos present in the registry, always
  available, `group:"feature"`; existing entries `group:"integration"`.
- `tests/test_api.py` — `PATCH /api/integrations/chores` (and `todos`) toggles
  enabled; `/api/hub` reflects `enabled` and `group`.
- `tests/js/hub-dom.test.mjs` (+ `test_js.py`) — feature off ⇒ tab hidden;
  active tab off ⇒ falls back to first visible; all off ⇒ `body.hub-empty` +
  `.empty-hub`; `renderIntegrations` splits into two groups; toggling back on
  restores.
- `tests/test_static.py` — update `MOBILE_TABS` / `TAB_SURFACE` /
  `WALL_SURFACES` and the tab-coverage / per-tab-visibility guards to the new
  model; assert the new CSS classes (`integ-off-chores`, `integ-off-todos`,
  `.empty-hub`, `hub-empty`) are styled; assert the Features sub-header exists.
- Manual gates (CLAUDE.md): full 1920px wall screenshot with Chores and/or
  To-Dos off (clean reflow), phone width ≤400px on every tab with features off,
  and night mode. Operator confirms on a real iPhone before "done".
- Review gate (CLAUDE.md): `silent-failure-hunter`, `code-reviewer`,
  `pr-test-analyzer` on the branch diff before PR.

## Rollout / migration

None required. Additive registry entries + `INSERT OR IGNORE` seeding mean the
deploy box (`gary`/garage) upgrades with both features enabled, no config.json
change, no schema migration, no rsync-exclude change.
