# Toggleable Chores & To-Dos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a household turn Chores and To-Dos on/off from Settings like the other integrations, with the wall and mobile shell degrading gracefully when any feature is off.

**Architecture:** Register `chores` and `todos` as two always-available entries in the existing server-side integrations registry (SQLite `integrations` table → `/api/hub`). Add a `group` field so Settings can show a labeled "Features" group. Make the mobile tab bar data-driven: hide a tab when all its backing features are off, fall back to the first visible tab, and show an all-off empty panel. Wall columns/slots hide via the already-stamped `body.integ-off-<id>` classes.

**Tech Stack:** FastAPI + SQLite (Python, stdlib-only registry), vanilla JS (no build, no npm), CSS. Tests: pytest (`test_integrations.py`, `test_api.py`, `test_static.py`) and Node `--test` fake-DOM (`tests/js/hub-dom.test.mjs`).

**Spec:** `docs/superpowers/specs/2026-08-17-feature-toggles-design.md`

## Global Constraints

- **Public repo — no house data or secrets** in code, tests, or fixtures.
- **No new dependencies, no build step, no npm/package.json.** JS is classic `<script>` (function declarations become sandbox globals). CSS is hand-written.
- **Non-breaking:** existing deployments must upgrade with Chores and To-Dos ON and behave identically until a toggle is flipped. Seeding is `INSERT OR IGNORE`; `integration_enabled(..., default=True)`.
- **Persistence is server-side only** (SQLite `integrations`). No `localStorage`, no `config.json` changes, no schema migration.
- **iOS tab-bar rules (CLAUDE.md):** hide tab buttons with the `hidden` attribute / `display:none` only. Do NOT add `position:fixed`, `transform`, or `backdrop-filter` to any tab element. Tap targets stay ≥44px.
- **Commit message footer** (every task commit):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01WouSh3uxxCJQVKj3feimCb
  ```
- **Review gate before PR (CLAUDE.md):** run `pr-review-toolkit:silent-failure-hunter`, `code-reviewer`, `pr-test-analyzer` on the branch diff and address real findings.

---

### Task 1: Register Chores & To-Dos in the integrations registry (backend)

**Files:**
- Modify: `src/family_hub/integrations.py:38-55` (the `add()` helper + descriptor list)
- Modify: `src/family_hub/app.py:526-529` (the `_integrations_state` entry dict)
- Test: `tests/test_integrations.py`, `tests/test_api.py`

**Interfaces:**
- Produces: `available_integrations(cfg, env, caldav_ok)` entries now each carry `"group"` (`"feature"` for `chores`/`todos`, `"integration"` for the rest). `chores` and `todos` are always `available=True` with `kind` equal to their id. `/api/hub` and `/api/integrations` each integration entry now includes `"group"`.

- [ ] **Step 1: Write the failing registry test**

In `tests/test_integrations.py` add:

```python
def test_chores_and_todos_are_always_available_features():
    class Cfg:  # nothing configured: no calendars, cameras, weather, climate
        calendars = []
    ids = {i["id"]: i for i in integrations.available_integrations(Cfg(), {})}
    # core features are available regardless of config
    assert ids["chores"]["available"] is True
    assert ids["todos"]["available"] is True
    assert ids["chores"]["group"] == "feature"
    assert ids["todos"]["group"] == "feature"
    # external services are tagged as integrations
    assert ids["weather"]["group"] == "integration"
    assert ids["cameras"]["group"] == "integration"
```

(Match the module import style already used at the top of `tests/test_integrations.py` — reuse its existing `integrations` import and any `Cfg`-style stub if one is defined; if it uses a real `Config`, mirror that.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_integrations.py::test_chores_and_todos_are_always_available_features -v`
Expected: FAIL — `KeyError: 'chores'` (and no `group` key).

- [ ] **Step 3: Add the `group` field and the two feature descriptors**

In `src/family_hub/integrations.py`, change the `add()` helper and prepend the two features:

```python
    def add(iid, kind, name, available, group="integration"):
        out.append({"id": iid, "kind": kind, "name": name,
                    "available": bool(available), "group": group})

    # Core features — always available; the operator turns them off to slim the
    # wall down. Listed first so they seed with the lowest sort and head the
    # Settings "Features" group.
    add("chores", "chores", "Chores", True, group="feature")
    add("todos", "todos", "To-Dos", True, group="feature")
    add("google_calendar", "calendar", "Google Calendar",
        any(c.get("kind", "google") == "google" for c in cals))
```

(Leave the remaining `add(...)` calls unchanged; they inherit `group="integration"`.)

- [ ] **Step 4: Run the registry test — expect PASS**

Run: `python -m pytest tests/test_integrations.py -v`
Expected: PASS (new test + existing ones).

- [ ] **Step 5: Write the failing API test**

In `tests/test_api.py` add (mirroring `test_integrations_list_toggle_and_hub_block`'s `_reload_with` + `TestClient` pattern):

```python
def test_chores_todos_toggle_via_integrations_and_hub_block(tmp_path, monkeypatch):
    appmod = _reload_with(tmp_path, monkeypatch, {})  # nothing else configured
    with TestClient(appmod.app) as c:
        ids = {i["id"]: i for i in c.get("/api/integrations").json()["integrations"]}
        # always present, seeded enabled, tagged as features
        assert ids["chores"]["enabled"] is True
        assert ids["todos"]["enabled"] is True
        assert ids["chores"]["group"] == "feature"
        assert ids["todos"]["group"] == "feature"
        # /api/hub carries the same entries incl. group
        hub = c.get("/api/hub").json()
        hids = {i["id"]: i for i in hub["integrations"]}
        assert hids["chores"]["group"] == "feature"
        # toggle chores off -> flag flips (data still served; UI hides it)
        assert c.patch("/api/integrations/chores", json={"enabled": False}).status_code == 200
        hub2 = c.get("/api/hub").json()
        assert next(i for i in hub2["integrations"] if i["id"] == "chores")["enabled"] is False
```

- [ ] **Step 6: Run it to confirm it fails**

Run: `python -m pytest tests/test_api.py::test_chores_todos_toggle_via_integrations_and_hub_block -v`
Expected: FAIL — `chores`/`todos` missing or no `group` key.

- [ ] **Step 7: Thread `group` into the `/api/hub` + `/api/integrations` entry**

In `src/family_hub/app.py` `_integrations_state`, add `group` to the `entry` dict (around line 526):

```python
        entry = {"id": integ["id"], "kind": integ["kind"],
                 "name": integ["name"], "enabled": en,
                 "group": integ.get("group", "integration"),
                 "status": _integ_status(integ["id"], caldav_status, cal_status)}
```

- [ ] **Step 8: Run the API test — expect PASS, then the full backend suite**

Run: `python -m pytest tests/test_api.py::test_chores_todos_toggle_via_integrations_and_hub_block tests/test_integrations.py -v`
Expected: PASS.
Then: `python -m pytest tests/test_api.py tests/test_integrations.py tests/test_db.py -q` (nothing else regressed by the new seeded rows).
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/family_hub/integrations.py src/family_hub/app.py tests/test_integrations.py tests/test_api.py
git commit -m "feat: register Chores & To-Dos as toggleable features"
```

---

### Task 2: Settings shows a labeled "Features" group (frontend)

**Files:**
- Modify: `src/family_hub/web/static/hub.js:2274-2297` (`renderIntegrations`)
- Modify: `src/family_hub/web/static/hub.js:2358` (the Integrations card `<h2>` in `renderSettingsFull`)
- Modify: `src/family_hub/web/static/styles.css` (add `.integ-group-title` near the `.integ-*` block ~line 1259)
- Test: `tests/js/hub-dom.test.mjs`, `tests/test_static.py`

**Interfaces:**
- Consumes: `data.integrations[]` entries with `{id, name, enabled, status, group}` from Task 1.
- Produces: `renderIntegrations(data)` renders two labeled sub-groups inside `#integrations-ctl` — a `Features` group (`group === 'feature'`) then an `Integrations` group (everything else) — each a `.integ-group-title` header followed by the existing `.integ-row` switches; an empty group renders nothing.

- [ ] **Step 1: Write the failing DOM test**

In `tests/js/hub-dom.test.mjs` add:

```javascript
test('renderIntegrations: splits into a Features group and an Integrations group', () => {
  const { sandbox, document } = loadHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'chores', name: 'Chores', enabled: true, group: 'feature' },
    { id: 'todos', name: 'To-Dos', enabled: true, group: 'feature' },
    { id: 'weather', name: 'Weather', enabled: true, group: 'integration' },
  ] });
  const host = document.getElementById('integrations-ctl');
  const titles = host.querySelectorAll('.integ-group-title').map((n) => n.textContent);
  assert.deepEqual(titles, ['Features', 'Integrations']);
  // every integration still gets its switch row
  assert.equal(host.querySelectorAll('.integ-row').length, 3);
});
```

(Use whatever the file's existing helper is for building the sandbox — the other `renderIntegrations` tests around line 3268 show the exact call, e.g. `const { sandbox, document } = loadHub();` or the local equivalent; copy that idiom.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `node --test --test-reporter=tap tests/js/hub-dom.test.mjs` (or `python -m pytest tests/test_js.py -q` if Node isn't on PATH — it runs the same file in Docker).
Expected: FAIL — no `.integ-group-title` elements.

- [ ] **Step 3: Split `renderIntegrations` by group**

Replace the `host.innerHTML = ...` assignment in `renderIntegrations` (keep the body-class stamping above it unchanged) with a grouped render:

```javascript
  const host = document.getElementById('integrations-ctl');
  if (!host) return;
  const row = (it) => {
    const warn = it.status === 'needs_auth' ? 'reconnect'
      : (it.status === 'error' ? 'error' : '');
    return `<button class="integ-row" type="button" role="switch"`
      + ` aria-checked="${it.enabled ? 'true' : 'false'}"`
      + ` data-integ-toggle="${escapeHtml(it.id)}">`
      + `<span class="integ-name">${escapeHtml(it.name)}`
      + (warn ? `<span class="integ-warn">${warn}</span>` : '')
      + `</span>`
      + `<span class="integ-switch${it.enabled ? ' on' : ''}" aria-hidden="true"></span>`
      + `</button>`;
  };
  const group = (title, items) => items.length
    ? `<div class="integ-group-title">${title}</div>` + items.map(row).join('')
    : '';
  const features = list.filter((it) => it.group === 'feature');
  const services = list.filter((it) => it.group !== 'feature');
  const html = group('Features', features) + group('Integrations', services);
  host.innerHTML = html || `<div class="integ-empty">none configured</div>`;
```

- [ ] **Step 4: Rename the card heading so it isn't redundant**

In `renderSettingsFull`, change the Integrations card heading (line ~2358) from:

```javascript
    + `<div class="shead"><span class="tick"></span><h2>Integrations</h2></div>`
```
to:
```javascript
    + `<div class="shead"><span class="tick"></span><h2>Features &amp; integrations</h2></div>`
```

- [ ] **Step 5: Style the sub-group header**

In `styles.css`, near the `.integ-row` / `.integ-name` rules (~line 1259), add:

```css
.integ-group-title {
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--faint);
  margin: 14px 2px 6px; }
.integ-group-title:first-child { margin-top: 2px; }
```

- [ ] **Step 6: Run the DOM test — expect PASS**

Run: `node --test --test-reporter=tap tests/js/hub-dom.test.mjs`
Expected: PASS (new test + the existing `renderIntegrations` tests at ~3268, which use single-group data — verify they still pass; they assert `.integ-row` counts and switch state, which are unchanged).

- [ ] **Step 7: Add a static guard**

In `tests/test_static.py` add:

```python
def test_settings_has_a_features_group():
    assert ".integ-group-title" in CSS, "features/integrations sub-headers unstyled"
    hub = (STATIC / "hub.js").read_text()
    assert "'Features'" in hub or '"Features"' in hub, \
        "renderIntegrations must render a Features group"
```

(Reuse the module's existing `CSS` / `STATIC` constants — check the top of `test_static.py` for their exact names and mirror them.)

- [ ] **Step 8: Run static + JS suites**

Run: `python -m pytest tests/test_static.py tests/test_js.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/family_hub/web/static/hub.js src/family_hub/web/static/styles.css tests/js/hub-dom.test.mjs tests/test_static.py
git commit -m "feat: labeled Features group in Settings"
```

---

### Task 3: Hide the wall column/slot when a feature is off (CSS)

**Files:**
- Modify: `src/family_hub/web/static/styles.css:1345-1348` (the `body.integ-off-*` block)
- Test: `tests/test_static.py`

**Interfaces:**
- Consumes: `body.integ-off-chores` / `body.integ-off-todos` (already stamped every poll by `renderIntegrations`).
- Produces: on the wall, `.people-col` hides under `integ-off-chores` and `.todo-slot` hides under `integ-off-todos`.

- [ ] **Step 1: Write the failing static guard**

In `tests/test_static.py` add:

```python
def test_off_features_hide_their_wall_surface():
    assert re.search(r"body\.integ-off-chores[^\{]*\.people-col[^\{]*\{[^}]*display:\s*none",
                     CSS), "chores-off must hide the people column on the wall"
    assert re.search(r"body\.integ-off-todos[^\{]*\.todo-slot[^\{]*\{[^}]*display:\s*none",
                     CSS), "todos-off must hide the to-do slot on the wall"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_static.py::test_off_features_hide_their_wall_surface -v`
Expected: FAIL — rules not present.

- [ ] **Step 3: Add the hide rules**

In `styles.css`, extend the existing gate block (after line 1348):

```css
body.integ-off-chores .people-col { display: none; }
body.integ-off-todos .todo-slot { display: none; }
```

- [ ] **Step 4: Run the guard — expect PASS**

Run: `python -m pytest tests/test_static.py::test_off_features_hide_their_wall_surface -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/family_hub/web/static/styles.css tests/test_static.py
git commit -m "feat: hide wall column/slot for an off feature"
```

---

### Task 4: Data-driven mobile tabs — hide, fall back, and the all-off empty state (frontend)

**Files:**
- Modify: `src/family_hub/web/static/index.html:109-110` (add the empty-state element after `.hub-grid`)
- Modify: `src/family_hub/web/static/hub.js` — add `TAB_FEATURES`, `featureEnabled`, `updateTabVisibility`; call it from `renderIntegrations`; guard `openOverlay`; broaden the `data-open-settings` click branch
- Modify: `src/family_hub/web/static/styles.css` (add `.hub-empty-state` + `body.hub-empty` rules)
- Test: `tests/js/hub-dom.test.mjs`, `tests/test_static.py`

**Interfaces:**
- Consumes: `data.integrations[]` (`{id, enabled, group}`) from Task 1; the tab buttons in `#tabbar` (`.tab-btn[data-tab]`); `setTab(tab)` (existing).
- Produces:
  - `TAB_FEATURES` — a map from tab id to backing feature ids.
  - `featureEnabled(id)` → boolean, fail-open (unknown id ⇒ true).
  - `updateTabVisibility(list)` — hides `.tab-btn`s whose features are all off (`btn.hidden = true`), stamps `body.hub-empty` when no tab is visible, and calls `setTab(firstVisible)` when the active tab became hidden.
  - `#hub-empty-msg` element shown only under `body.hub-empty`.

- [ ] **Step 1: Write the failing DOM tests**

First extend the test's seeded DOM so the tab bar exists (the hand-rolled DOM builds elements from `innerHTML`). In `tests/js/hub-dom.test.mjs`, add a helper near the other builders that injects the real tab bar markup, then the tests:

```javascript
function seedTabbar(document) {
  const bar = document.getElementById('tabbar');
  bar.innerHTML =
    '<button class="tab-btn active" data-tab="chores">Chores</button>' +
    '<button class="tab-btn" data-tab="todos">To-Dos</button>' +
    '<button class="tab-btn" data-tab="cal">Calendar</button>' +
    '<button class="tab-btn" data-tab="cams">Cameras</button>' +
    '<button class="tab-btn" data-tab="weather">Weather</button>';
  return bar;
}

test('updateTabVisibility: an off feature hides its tab', () => {
  const { sandbox, document } = loadHub();
  seedTabbar(document);
  document.body.dataset.tab = 'todos';
  sandbox.renderIntegrations({ integrations: [
    { id: 'chores', enabled: true, group: 'feature' },
    { id: 'todos', enabled: false, group: 'feature' },
    { id: 'weather', enabled: true, group: 'integration' },
  ] });
  const byTab = (t) => document.querySelectorAll('.tab-btn')
    .find((b) => b.dataset.tab === t);
  assert.equal(byTab('todos').hidden, true, 'todos tab hidden when off');
  assert.equal(byTab('chores').hidden, false, 'chores tab stays');
  // active tab (todos) was hidden -> fell back to the first visible tab
  assert.equal(document.body.dataset.tab, 'chores');
  assert.equal(document.body.classList.contains('hub-empty'), false);
});

test('updateTabVisibility: every feature off -> hub-empty, all tabs hidden', () => {
  const { sandbox, document } = loadHub();
  seedTabbar(document);
  sandbox.renderIntegrations({ integrations: [
    { id: 'chores', enabled: false, group: 'feature' },
    { id: 'todos', enabled: false, group: 'feature' },
  ] });
  assert.equal(document.body.classList.contains('hub-empty'), true);
  assert.ok(document.querySelectorAll('.tab-btn').every((b) => b.hidden));
});
```

(If `.find`/`.every` aren't available on the fake `querySelectorAll` return, wrap with `[...document.querySelectorAll('.tab-btn')]`. Confirm `loadHub()` is the correct sandbox builder name used by neighbouring tests and copy it verbatim.)

- [ ] **Step 2: Run to confirm failure**

Run: `node --test --test-reporter=tap tests/js/hub-dom.test.mjs`
Expected: FAIL — `updateTabVisibility` undefined / `hidden` never set.

- [ ] **Step 3: Add the tab-visibility engine to hub.js**

Add near the other tab helpers (just above `setTab`, ~line 1092):

```javascript
// Which integration(s) back each phone tab. A tab shows when ANY backing
// feature is enabled, so the calendar tab survives on any one calendar source
// and the weather tab survives on either weather or climate. Chores/To-Dos are
// always-available features, so their tabs track their single toggle.
const TAB_FEATURES = {
  chores: ['chores'],
  todos: ['todos'],
  cal: ['google_calendar', 'ics_calendar', 'icloud_caldav'],
  cams: ['cameras'],
  weather: ['weather', 'climate'],
};

// True unless the integration is present AND disabled. Fail-open: an id absent
// from the payload (older server, transient gap) is treated as on, never
// blanking a surface on a hiccup.
function featureEnabled(id) {
  const list = (hubData && hubData.integrations) || lastIntegrations || [];
  const e = list.find((x) => x.id === id);
  return e ? !!e.enabled : true;
}

// Reconcile the phone tab bar with the enabled feature set: hide tabs with no
// enabled backing feature, move off a hidden active tab, and flag the all-off
// state so the empty panel can show. No-ops (fail-open) on an empty list.
function updateTabVisibility(list) {
  if (!list || !list.length) return;
  const on = new Set(list.filter((i) => i.enabled).map((i) => i.id));
  const visible = (tab) => (TAB_FEATURES[tab] || []).some((f) => on.has(f));
  const btns = [...document.querySelectorAll('.tab-btn')];
  let any = false;
  btns.forEach((b) => {
    const vis = visible(b.dataset.tab);
    b.hidden = !vis;
    if (vis) any = true;
  });
  document.body.classList.toggle('hub-empty', !any);
  const active = document.body.dataset.tab;
  if (any && (!active || !visible(active))) {
    const first = btns.find((b) => !b.hidden);
    if (first) setTab(first.dataset.tab);
  }
}
```

- [ ] **Step 4: Call it every poll — BEFORE the `#integrations-ctl` early return**

In `renderIntegrations`, the `body.integ-off-*` stamping already runs before the `const host = ...; if (!host) return;` guard. Insert the tab reconcile immediately after the stamping loop and before that guard (so it runs every poll, not only when Settings is open):

```javascript
  list.forEach((it) =>
    document.body.classList.toggle('integ-off-' + it.id, !it.enabled));
  updateTabVisibility(list);            // <-- add this line
  const host = document.getElementById('integrations-ctl');
  if (!host) return;
```

- [ ] **Step 5: Guard the overlay entry points**

In `openOverlay`, make the chores/todos branches no-op when their feature is off:

```javascript
  } else if (view === 'chores') {
    if (!featureEnabled('chores')) return;
    content.innerHTML = `<div class="overlay-panel"><div id="chores-full"></div></div>`;
    ...
  } else if (view === 'todos') {
    if (!featureEnabled('todos')) return;
    content.innerHTML = `<div class="overlay-panel"><div id="todos-full"></div></div>`;
    ...
```

(Insert only the two `if (!featureEnabled(...)) return;` lines; leave the rest of each branch intact.)

- [ ] **Step 6: Broaden the settings click hook**

The empty-state button (added below) uses `data-open-settings`, but the handler is scoped to `#theme-pop`. Widen it (line ~2582) so any `data-open-settings` element opens Settings; `closeThemePop()` is idempotent:

```javascript
  if (e.target.closest('[data-open-settings]')) {
    closeThemePop();
    openOverlay('settings');
    return;
  }
```

- [ ] **Step 7: Add the empty-state element**

In `index.html`, after the `.hub-grid` closing `</div>` (line 109) and before the `.wrap` closing `</div>` (line 110), add:

```html
      <!-- Shown only when every feature is turned off (body.hub-empty): the
           wall/phone would otherwise be blank. Reuses the settings hook. -->
      <div class="hub-empty-state" id="hub-empty-msg">
        <div>All features are turned off.</div>
        <button class="btn" type="button" data-open-settings>Open Settings</button>
      </div>
```

- [ ] **Step 8: Style the empty state**

In `styles.css`, near the existing `.empty-hub` rules (~line 703), add:

```css
.hub-empty-state { display: none; }
body.hub-empty .hub-empty-state {
  display: flex; flex-direction: column; align-items: center; gap: 16px;
  padding: 80px 20px; text-align: center;
  color: var(--dim); font-family: var(--mono); font-size: 15px; }
body.hub-empty .hub-grid,
body.hub-empty .tabbar { display: none; }
```

(`.btn` already exists in the stylesheet; if not, verify a shared button class name and use it. Confirm `--dim`/`--mono` tokens exist — they are used by `.empty-hub` just above.)

- [ ] **Step 9: Run the DOM tests — expect PASS**

Run: `node --test --test-reporter=tap tests/js/hub-dom.test.mjs`
Expected: PASS (both new tests + all existing ones — the `setTab` test at ~383 still passes because `updateTabVisibility` isn't called there).

- [ ] **Step 10: Add static guards**

In `tests/test_static.py` add:

```python
def test_all_off_empty_state_present_and_wired():
    index = (STATIC / "index.html").read_text()
    assert 'id="hub-empty-msg"' in index, "missing all-off empty-state element"
    assert "body.hub-empty" in CSS, "hub-empty visibility rules missing"
    hub = (STATIC / "hub.js").read_text()
    assert "updateTabVisibility" in hub and "TAB_FEATURES" in hub, \
        "data-driven tab visibility missing"
```

- [ ] **Step 11: Full suite**

Run: `python -m pytest -q`
Expected: PASS (all Python + the JS suite via `test_js.py`).

- [ ] **Step 12: Commit**

```bash
git add src/family_hub/web/static/index.html src/family_hub/web/static/hub.js src/family_hub/web/static/styles.css tests/js/hub-dom.test.mjs tests/test_static.py
git commit -m "feat: data-driven mobile tabs + all-off empty state"
```

---

### Task 5: Manual verification + review gate

**Files:** none (verification only).

- [ ] **Step 1: Full-width wall screenshot with features off**

Per CLAUDE.md render at ≥1920px (or Desktop layout zoomed out). With Chores off, confirm the wall reflows with no dead column; with To-Dos off, confirm the slot closes cleanly; with both off but calendar/cameras on, confirm the layout still reads. Capture and share.

- [ ] **Step 2: Phone-width check (≤400px)**

Temporarily widen the breakpoint locally (`@media (max-width: 2000px)`), screenshot each tab, then revert (never commit that). Confirm: an off feature's tab disappears; tapping was on the removed tab → falls back; with everything off the `#hub-empty-msg` panel shows and its "Open Settings" button works; the tab bar isn't a dead empty strip.

- [ ] **Step 3: Night mode**

Confirm the empty state and hidden columns look right under `.is-night` (22:00–06:00) — no filter/containing-block regressions.

- [ ] **Step 4: Operator confirms on a real iPhone**

Ask the operator to toggle Chores/To-Dos off and back on and verify tab hide/fallback and tappability on the actual phone (iOS-only behaviors are invisible to the suite).

- [ ] **Step 5: Review gate**

Run on the branch diff and address real findings:
- `pr-review-toolkit:silent-failure-hunter`
- `pr-review-toolkit:code-reviewer`
- `pr-review-toolkit:pr-test-analyzer`

- [ ] **Step 6: Open the PR** (only when the operator asks), summarizing the feature, the non-breaking upgrade, and the cameras/weather empty-tab fix.

---

## Self-Review

**Spec coverage:**
- §Registry (chores/todos, `group`) → Task 1. ✔
- §Seeding/gating/`_integrations_state` group → Task 1. ✔
- §Settings "Features" group → Task 2. ✔
- §Hide surfaces (wall CSS) → Task 3. ✔
- §Mobile tab bar (hide, fallback, startup reconcile) → Task 4 (Steps 3–4, 9). ✔
- §All-off empty state → Task 4 (Steps 7–8). ✔
- §Defensive edge handling (openOverlay guard) → Task 4 Step 5; To-Do source picker stays visible (unchanged — no gating added). ✔
- §Error handling (failed PATCH no silent revert) → unchanged existing `.ok` guard, not modified. ✔ Fail-open on empty payload → `featureEnabled` + `updateTabVisibility` early return. ✔
- §Testing (all four suites) → Tasks 1–4 tests + Task 5 manual/review. ✔
- §Rollout/migration (none) → guaranteed by INSERT OR IGNORE + default-True; asserted by Task 1 API test. ✔

**Placeholder scan:** No TBD/TODO; every code step has concrete content. Test-harness idiom caveats (`loadHub` name, `[...spread]` for fake `querySelectorAll`) are flagged where the executor must confirm against the live file rather than guess. ✔

**Type consistency:** `group` field name consistent across `integrations.py`, `_integrations_state`, `renderIntegrations`, and tests. `updateTabVisibility`/`featureEnabled`/`TAB_FEATURES` names consistent between Task 4 definition and its call site + tests. `body.hub-empty`, `#hub-empty-msg`, `.integ-group-title`, `body.integ-off-chores`, `body.integ-off-todos` consistent across HTML/CSS/JS/tests. ✔
