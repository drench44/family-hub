// Executable DOM tests for the glue in hub.js — run with `node --test`.
// Sibling to hub.test.mjs (which covers the PURE helpers in common.js); this
// file exercises the DOM-touching functions that hub.test.mjs can't reach:
//   - showToast:      builds the .hub-toast element (textContent, no innerHTML)
//   - toggleChore:    on a FAILED write, surfaces the "couldn't save" toast
//   - renderCalendar: maps /api/hub event fields into the calendar DOM
//
// Why a hand-rolled DOM and not jsdom/linkedom/happy-dom: this repo ships NO
// package.json and installs NO node_modules — the box runs this suite inside a
// read-only `node:20-alpine` mount (see tests/test_js.py) and CI runs a bare
// `node --test tests/js/*.mjs` with no `npm install`. A devDependency would go
// unresolved in both. So, mirroring the existing hub.test.mjs pattern, hub.js
// (a classic <script>, no exports) is loaded into a vm context alongside a
// minimal fake `document`/`window` seeded with the real ids from index.html.
// hub.js's functions are function *declarations*, so they surface as sandbox
// globals; its load-time side effects (tickClock, the click listener, the
// setInterval poll loop, the initial poll()) run harmlessly against the stub —
// timers are no-ops and `fetch` rejects straight into poll()'s offline path.
// No behaviour-changing refactor of hub.js was needed to make this work.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'family_hub', 'web', 'static');
const commonSrc = readFileSync(join(staticDir, 'common.js'), 'utf8');
const hubSrc = readFileSync(join(staticDir, 'hub.js'), 'utf8');

// The elements that genuinely exist in index.html. getElementById returns null
// for anything else (e.g. 'toast'), so hub.js's create-if-missing branches run
// for real — that's the whole point of the showToast assertions below.
const SEEDED_IDS = [
  'conn-word', 'clock-date', 'clock-time', 'cal', 'people', 'todo-slot', 'tiles',
  'camgrid', 'integrations-ctl',
  'panels', 'tabbar', 'overlay', 'overlay-home', 'overlay-content', 'ev-modal',
  'ev-card',
  'chore-modal', 'chore-card', 'chore-editor',
  'confirm-modal', 'confirm-card', 'confirm-msg', 'confirm-sub',
];

function makeClassList() {
  const s = new Set();
  return {
    add: (...c) => c.forEach((x) => s.add(x)),
    remove: (...c) => c.forEach((x) => s.delete(x)),
    toggle: (c, force) => {
      const on = force === undefined ? !s.has(c) : force;
      if (on) s.add(c); else s.delete(c);
      return on;
    },
    contains: (c) => s.has(c),
  };
}

// A DOM node thin enough to satisfy exactly what hub.js touches: text/markup
// sinks, a classList, a dataset, a style with setProperty, and a tree so an
// appended element becomes findable by id (as it would once in the document).
//
// buildChoreForm (moved to common.js in Task 2, shared with hub.js) renders by
// assigning a full HTML string to `host.innerHTML` and then re-finding its own
// pieces via `host.querySelector('.f-title')` etc. To exercise it for real
// (not just assert it doesn't throw), `innerHTML` below parses the assigned
// markup into a tiny live tree — good enough for the class/attribute selectors
// this codebase actually uses — while the getter still returns the exact
// string that was set, so every existing `assert.match(el.innerHTML, /re/)`
// in this file keeps reading raw HTML, unaffected by the new parsing.
const VOID_TAGS = new Set(['input', 'br', 'img', 'hr', 'meta', 'link']);

function parseTagAttrs(attrStr) {
  const attrs = {};
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(?:=\s*("([^"]*)"|'([^']*)'|[^\s"'=<>`]+))?/g;
  let m;
  while ((m = re.exec(attrStr))) {
    const val = m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : (m[2] || ''));
    attrs[m[1].toLowerCase()] = val;
  }
  return attrs;
}

function datasetFromAttrs(attrs) {
  const ds = {};
  Object.keys(attrs).forEach((k) => {
    if (k.startsWith('data-')) {
      ds[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = attrs[k];
    }
  });
  return ds;
}

// A selector matcher covering the subset this codebase's `$(sel)` helpers
// actually issue: one COMPOUND simple selector — an optional tag name followed
// by any mix of classes and bare/valued attributes ('.f-title',
// '[data-submit]', '[data-repeat="daily"]', '.tile-camera[data-cam="cam1"]').
// A selector OUTSIDE this grammar throws instead of silently matching
// nothing: a silent miss would send tests down the "element not found" branch
// while the real browser takes the found branch. TWO KNOWN GAPS, both
// deliberate: (1) descendant combinators ('#theme-pop [data-theme-set]',
// used by the theme-picker's closest() calls) match NOTHING here — the parsed
// tree has no parent links, so ancestor context can't be evaluated; tests
// that exercise those paths only ever assert the not-inside-the-popover
// branch. (2) attributes reflect only what was in the PARSED MARKUP —
// property writes like hub.js's `recent.open = true` are not mirrored into
// attrs, so `[open]`-style selectors can only match markup-authored
// attributes.
function selectorMatches(node, sel) {
  sel = sel.trim();
  if (/\s/.test(sel)) return false;   // gap (1): combinator selectors never match
  const m = /^([a-zA-Z][a-zA-Z0-9]*)?((?:[.#][a-zA-Z0-9_-]+|\[[a-zA-Z0-9_-]+(?:="[^"]*")?\])*)$/.exec(sel);
  if (!m || (!m[1] && !m[2])) throw new Error(`unsupported selector in fake DOM: ${sel}`);
  if (m[1] && node._tag !== m[1].toLowerCase()) return false;
  const partRe = /([.#])([a-zA-Z0-9_-]+)|\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]/g;
  let p;
  while ((p = partRe.exec(m[2] || ''))) {
    if (p[1] === '.') {
      if (!node.classList.contains(p[2])) return false;
    } else if (p[1] === '#') {
      if (node.attrs.id !== p[2]) return false;
    } else {
      if (!(p[3] in node.attrs)) return false;
      if (p[4] !== undefined && node.attrs[p[3]] !== p[4]) return false;
    }
  }
  return true;
}

function queryFirst(nodes, sel) {
  for (const n of nodes) {
    if (selectorMatches(n, sel)) return n;
    const found = queryFirst(n.children, sel);
    if (found) return found;
  }
  return null;
}

function queryAll(nodes, sel, out = []) {
  for (const n of nodes) {
    if (selectorMatches(n, sel)) out.push(n);
    queryAll(n.children, sel, out);
  }
  return out;
}

// A single parser drives both the outer FakeEl.innerHTML and every inner
// QueryNode.innerHTML: it walks open/close tags with a stack, treating the
// handful of void tags (input, etc.) as self-closing.
function parseFragment(html) {
  const tagRe = /<\/?([a-zA-Z][a-zA-Z0-9]*)((?:\s+[a-zA-Z_:][-a-zA-Z0-9_:.]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*\/?>/g;
  const root = [];
  const stack = [{ tag: null, children: root }];
  let m;
  while ((m = tagRe.exec(html))) {
    const full = m[0];
    const tag = m[1].toLowerCase();
    if (full[1] === '/') {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag === tag) { stack.length = i; break; }
      }
      continue;
    }
    const node = new QueryNode(tag, parseTagAttrs(m[2] || ''));
    stack[stack.length - 1].children.push(node);
    if (!VOID_TAGS.has(tag) && !full.endsWith('/>')) {
      stack.push({ tag, children: node.children });
    }
  }
  return root;
}

// A rendered descendant inside a FakeEl's parsed innerHTML — everything
// buildChoreForm's `$(sel)` results touch: value, classList, dataset, a
// settable innerHTML (re-parsed, so nested re-renders like paintDays() work),
// an inert onclick slot, and scoped querySelector(All) for `$('.f-repeat')
// .querySelectorAll('.seg-btn')`-style lookups.
class QueryNode {
  constructor(tag, attrs) {
    this._tag = tag;
    this.tagName = tag.toUpperCase();
    this.attrs = attrs;
    this.classList = makeClassList();
    (attrs.class || '').split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    this.dataset = datasetFromAttrs(attrs);
    this.value = attrs.value !== undefined ? attrs.value : '';
    this.children = [];
    this._innerHTML = '';
    this.textContent = '';
    this.onclick = null;
    // Parsed nodes are VISIBLE by default — the OPPOSITE of FakeEl's
    // hidden-by-default. Tests that need a hidden tile/panel set
    // `offsetParent = null` explicitly (that's how display:none reads).
    this.offsetParent = {};
  }

  get innerHTML() { return this._innerHTML; }

  set innerHTML(html) {
    this._innerHTML = String(html);
    try { this.children = parseFragment(this._innerHTML); } catch (e) { this.children = []; }
  }

  addEventListener() {}

  querySelector(sel) { return queryFirst(this.children, sel); }

  querySelectorAll(sel) { return queryAll(this.children, sel); }
}

class FakeEl {
  constructor(registry, tag = 'div') {
    this._registry = registry;
    this._tag = String(tag).toLowerCase();
    this.tagName = String(tag).toUpperCase();
    this._id = '';
    this.className = '';
    this.textContent = '';
    this._innerHTML = '';
    this._queryChildren = [];
    this.children = [];
    this.dataset = {};
    this.value = '';
    this.style = { setProperty() {} };
    this.classList = makeClassList();
    this.offsetParent = null;   // hidden by default; guards camera/panel wiring
    this.parentNode = null;
  }

  get id() { return this._id; }

  set id(v) { this._id = v; }   // registration happens on insertion, not naming

  // The getter still returns exactly the string that was set (existing tests
  // regex-match on it); the setter additionally parses it into `_queryChildren`
  // so `querySelector`/`querySelectorAll` — needed by buildChoreForm's `$(sel)`
  // helper — can find real nodes. A parse failure never breaks the plain-string
  // behaviour existing tests depend on.
  get innerHTML() { return this._innerHTML; }

  set innerHTML(html) {
    this._innerHTML = String(html);
    try { this._queryChildren = parseFragment(this._innerHTML); } catch (e) { this._queryChildren = []; }
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    if (child._id) this._registry[child._id] = child;  // now findable by id
    return child;
  }

  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
    }
    if (this._id && this._registry[this._id] === this) delete this._registry[this._id];
  }

  querySelector(sel) { return queryFirst(this._queryChildren, sel); }

  querySelectorAll(sel) { return queryAll(this._queryChildren, sel); }

  addEventListener() {}

  setAttribute() {}
}

// A fresh sandbox per test: hub.js's load-time side effects run once per load,
// and each test gets its own isolated document tree + registry.
function newHub() {
  const registry = {};
  // Captured lifecycle listeners so a test can fire pageshow/visibilitychange/
  // resize/orientationchange and observe the effect (the --app-h gap fix). A
  // plain object of type -> [fn]; fire() below dispatches to both maps.
  const winListeners = {};
  const docListeners = {};
  // document.querySelector(All) searches every registered element's parsed
  // innerHTML — enough for hub.js's document-wide lookups (probeCamera's
  // '.tile-camera[data-cam="..."]', renderPeople's '.person-card[...]').
  const document = {
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new FakeEl(registry, tag),
    addEventListener: (type, fn) => { (docListeners[type] || (docListeners[type] = [])).push(fn); },
    visibilityState: 'visible',
    querySelector: (sel) => {
      for (const el of Object.values(registry)) {
        const hit = queryFirst(el._queryChildren || [], sel);
        if (hit) return hit;
      }
      return null;
    },
    querySelectorAll: (sel) => {
      const out = [];
      Object.values(registry).forEach((el) => queryAll(el._queryChildren || [], sel, out));
      return out;
    },
  };
  SEEDED_IDS.forEach((id) => {
    const el = new FakeEl(registry);
    el._id = id;
    registry[id] = el;
  });
  document.body = new FakeEl(registry, 'body');
  // Scroll targets scrollPageToTop() zeroes on iOS (besides window.scrollTo);
  // seeded non-zero so a test can prove each one gets reset to the top.
  document.scrollingElement = { scrollTop: 0 };
  // documentElement carries a style recorder so the --app-h gap fix
  // (documentElement.style.setProperty('--app-h', ...)) is observable, and a
  // tiny attribute bag so reflectThemeControls() can read the live data-theme/
  // data-accent/data-cols/data-layout the way it does off the real <html>.
  document.documentElement = {
    scrollTop: 0,
    _attrs: {},
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    style: {
      _props: {},
      setProperty(k, v) { this._props[k] = String(v); },
      getPropertyValue(k) { return this._props[k]; },
    },
  };
  // The phone scroll container. scrollPageToTop() resets document.querySelector
  // ('.wrap'); expose a stand-in so the reset is observable in tests.
  const wrapEl = new FakeEl(registry, 'div');
  wrapEl.scrollTop = 0;
  const _qs = document.querySelector;
  document.querySelector = (sel) => (sel === '.wrap' ? wrapEl : _qs(sel));

  // Timers are inert: no callback ever fires, so the poll loop and the toast
  // auto-hide don't run — the toast stays put for us to assert on.
  const scrollCalls = [];
  const sandbox = {
    document,
    window: {
      addEventListener: (type, fn) => { (winListeners[type] || (winListeners[type] = [])).push(fn); },
      innerWidth: 1280,
      innerHeight: 800,
    },
    innerWidth: 1280,
    innerHeight: 800,
    scrollTo: (x, y) => { scrollCalls.push([x, y]); },
    scrollCalls,
    setTimeout: () => 0,
    setInterval: () => 0,
    clearTimeout: () => {},
    clearInterval: () => {},
    // Default: no network. Load-time poll() and any poll() a test triggers take
    // the offline branch deterministically instead of hitting a real server.
    fetch: async () => { throw new Error('offline in test'); },
  };
  vm.createContext(sandbox);
  vm.runInContext(commonSrc, sandbox);
  vm.runInContext(hubSrc, sandbox);
  // Dispatch a captured lifecycle event to both window- and document-level
  // listeners (real code registers pageshow/resize/orientationchange on window,
  // visibilitychange on document).
  const fire = (type, ev = {}) => {
    [...(winListeners[type] || []), ...(docListeners[type] || [])].forEach((fn) => fn(ev));
  };
  return { document, sandbox, fire, winListeners, docListeners };
}

test('showToast builds the .hub-toast element with textContent (no innerHTML/XSS)', () => {
  const { document, sandbox } = newHub();
  // index.html has no #toast, so hub.js must create it on first call.
  assert.equal(document.getElementById('toast'), null);

  const msg = 'Save failed <img src=x onerror=alert(1)>';
  sandbox.showToast(msg);

  const el = document.getElementById('toast');
  assert.ok(el, 'toast element was created and inserted');
  assert.equal(el.parentNode, document.body, 'appended to <body>');
  assert.equal(el.className, 'hub-toast');
  assert.ok(el.classList.contains('hub-toast-visible'), 'shown');
  // The message is written via textContent, verbatim — never through innerHTML,
  // so an event title/description can't inject markup into the toast.
  assert.equal(el.textContent, msg);
  assert.equal(el.innerHTML, '', 'innerHTML never touched');
});

test('setTab scrolls the page back to the top on every tab tap', () => {
  const { document, sandbox } = newHub();
  // Pretend the page is scrolled down on every target scrollPageToTop() resets.
  sandbox.scrollCalls.length = 0;   // ignore any load-time scroll
  document.scrollingElement.scrollTop = 900;
  document.documentElement.scrollTop = 900;
  document.body.scrollTop = 900;
  document.querySelector('.wrap').scrollTop = 900;
  sandbox.setTab('cal');
  assert.deepEqual(sandbox.scrollCalls.at(-1), [0, 0],
    'switching tabs must call window.scrollTo(0,0)');
  // every scroll target lands at the top — window (desktop) AND the .wrap
  // app-shell content region (phone), plus the iOS belt-and-suspenders roots
  assert.equal(document.scrollingElement.scrollTop, 0, 'scrollingElement reset');
  assert.equal(document.documentElement.scrollTop, 0, 'documentElement reset');
  assert.equal(document.body.scrollTop, 0, 'body reset');
  assert.equal(document.querySelector('.wrap').scrollTop, 0, '.wrap content region reset');
  // tapping the tab you are already on scrolls to the top too (iOS pattern)
  sandbox.scrollCalls.length = 0;
  document.scrollingElement.scrollTop = 500;
  sandbox.setTab('cal');
  assert.deepEqual(sandbox.scrollCalls.at(-1), [0, 0],
    're-tapping the active tab must still scroll to the top');
  assert.equal(document.scrollingElement.scrollTop, 0,
    're-tap also resets the scrolling element');
});

// ---- updateTabVisibility: hide/fall-back/all-off empty state (Task 4) ----

// Injects the real tab bar markup into the already-registered #tabbar element
// (document.querySelectorAll('.tab-btn') below finds these through the
// registry-wide search over each element's parsed _queryChildren — the same
// pattern seedThemePopWithLayout uses for '[data-layout-set]').
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
  const { sandbox, document } = newHub();
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
  const { sandbox, document } = newHub();
  seedTabbar(document);
  sandbox.renderIntegrations({ integrations: [
    { id: 'chores', enabled: false, group: 'feature' },
    { id: 'todos', enabled: false, group: 'feature' },
  ] });
  assert.equal(document.body.classList.contains('hub-empty'), true);
  assert.ok(document.querySelectorAll('.tab-btn').every((b) => b.hidden));
});

// ---- --app-h: the phone-shell height var (tab-bar gap fix) ----
// The phone shell body is height: var(--app-h, 100dvh). On iOS a stale 100dvh
// after a bfcache/app-switch restore left the in-flow tab bar floating above a
// gap until a full reload. hub.js measures window.innerHeight into --app-h and
// re-measures it on the lifecycle events iOS doesn't reliably relayout for.

const appH = (document) => document.documentElement.style.getPropertyValue('--app-h');

test('--app-h is set from window.innerHeight at load', () => {
  const { document } = newHub();   // sandbox.window.innerHeight = 800
  assert.equal(appH(document), '800px',
    'the phone shell height var must be measured from innerHeight up front');
});

test('a pageshow (bfcache restore) re-measures --app-h', () => {
  // The reported bug: returning to an already-open iOS tab restores a stale
  // height and the tab bar floats above a gap until reload. pageshow must fix it.
  const { document, sandbox, fire } = newHub();
  sandbox.window.innerHeight = 640;   // Safari restored a different viewport
  fire('pageshow', { persisted: true });
  assert.equal(appH(document), '640px', 'pageshow must resync the shell height');
});

test('a visibilitychange back to visible re-measures --app-h', () => {
  const { document, sandbox, fire } = newHub();
  sandbox.window.innerHeight = 712;
  document.visibilityState = 'visible';
  fire('visibilitychange');
  assert.equal(appH(document), '712px');
});

test('an orientationchange re-measures --app-h', () => {
  const { document, sandbox, fire } = newHub();
  sandbox.window.innerHeight = 500;   // rotated to landscape
  fire('orientationchange');
  assert.equal(appH(document), '500px');
});

test('a resize re-measures --app-h', () => {
  const { document, sandbox, fire } = newHub();
  sandbox.window.innerHeight = 900;
  fire('resize');
  assert.equal(appH(document), '900px');
});

test('a zero innerHeight is ignored, not stamped (would black-screen the shell)', () => {
  // iOS Safari can transiently report innerHeight:0 on exactly these lifecycle
  // events. --app-h drives the whole shell height; a literal 0px collapses it to
  // an empty screen (and the 100dvh fallback does NOT kick in — 0px is "valid").
  const { document, sandbox, fire } = newHub();
  assert.equal(appH(document), '800px');   // measured at load
  sandbox.window.innerHeight = 0;
  fire('pageshow', { persisted: true });
  assert.equal(appH(document), '800px', 'a 0px reading must not overwrite the good height');
});

test('a visibilitychange to HIDDEN does not re-measure --app-h', () => {
  // Only a return to visible should re-measure; a hidden tab can report a
  // collapsed viewport, so re-measuring then would stamp a bad height.
  const { document, sandbox, fire } = newHub();
  assert.equal(appH(document), '800px');
  sandbox.window.innerHeight = 640;
  document.visibilityState = 'hidden';
  fire('visibilitychange');
  assert.equal(appH(document), '800px', 'a hidden tab must be left at its prior height');
});

// ---- Layout control reflection in the display popover ----
// The popover carries an Auto/Desktop segmented control (data-layout-set).
// reflectThemeControls() must mark exactly the button matching the live
// <html data-layout> as .on, the same way it does Theme/Accent/Columns.
// Buttons are wrapped in .theme-ctl, matching the real markup shape both
// surfaces use — reflectThemeControls scopes its query to that container.

function seedThemePopWithLayout(document) {
  const pop = document.getElementById('theme-pop') || (() => {
    // theme-pop isn't in SEEDED_IDS; register a bare host to attach markup to
    const el = document.createElement('div');
    el._id = 'theme-pop';
    return el;
  })();
  pop.innerHTML =
    '<div class="theme-ctl">'
    + '<button type="button" data-layout-set="auto">Auto</button>'
    + '<button type="button" data-layout-set="desktop">Desktop</button>'
    + '</div>';
  return pop;
}

test('reflectThemeControls marks the active layout button .on (desktop)', () => {
  const { document, sandbox } = newHub();
  const pop = seedThemePopWithLayout(document);
  // register so getElementById('theme-pop') finds it inside reflectThemeControls
  document.body.appendChild(pop);
  document.documentElement.setAttribute('data-layout', 'desktop');

  sandbox.reflectThemeControls();

  const btns = pop.querySelectorAll('[data-layout-set]');
  const on = btns.filter((b) => b.classList.contains('on')).map((b) => b.dataset.layoutSet);
  assert.deepEqual(on, ['desktop'], 'only the desktop layout button is marked on');
});

test('reflectThemeControls clears the old .on when the layout choice changes', () => {
  // Genuinely exercise the toggle-OFF branch: pre-mark Desktop as active, then
  // reflect with data-layout=auto and assert Desktop lost .on and Auto gained it.
  const { document, sandbox } = newHub();
  const pop = seedThemePopWithLayout(document);
  document.body.appendChild(pop);
  pop.querySelectorAll('[data-layout-set]')
    .find((b) => b.dataset.layoutSet === 'desktop').classList.add('on');
  document.documentElement.setAttribute('data-layout', 'auto');

  sandbox.reflectThemeControls();

  const btns = pop.querySelectorAll('[data-layout-set]');
  const on = btns.filter((b) => b.classList.contains('on')).map((b) => b.dataset.layoutSet);
  assert.deepEqual(on, ['auto'], 'the desktop button was cleared and auto marked on');
});

test('toggleChore surfaces the "couldn’t save" toast when the write fails', async () => {
  const { document, sandbox } = newHub();
  // Force a persistent write failure (full disk / read-only SD on the kiosk).
  sandbox.attemptToggle = async () => false;

  await sandbox.toggleChore(42, false);   // poll() inside also takes offline path

  const el = document.getElementById('toast');
  assert.ok(el, 'a toast is shown on failed save');
  assert.match(el.textContent, /Couldn.t save/);   // tolerant of the curly apostrophe
  assert.match(el.textContent, /tap again/);
});

test('toggleChore shows NO toast when the write succeeds', async () => {
  const { document, sandbox } = newHub();
  sandbox.attemptToggle = async () => true;

  await sandbox.toggleChore(42, true);

  // Proves the toast is tied to failure, not fired on every check-off.
  assert.equal(document.getElementById('toast'), null, 'no toast on a good save');
});

test('renderCalendar maps event title/time/all-day into the calendar DOM', () => {
  const { document, sandbox } = newHub();
  const data = {
    date: '2026-08-14',
    calendar: {
      status: { ok: true },
      events: [
        {
          id: 'e1', title: 'Dentist', all_day: 0,
          start_ts: '2026-08-14T09:30:00-07:00',
          end_ts: '2026-08-14T10:30:00-07:00',
          color: '#5BC9F0',
        },
        {
          id: 'e2', title: 'Camp <b>all week</b>', all_day: 1,
          start_ts: '2026-08-14', end_ts: '2026-08-15',
        },
      ],
    },
  };

  sandbox.renderCalendar(data);
  const html = document.getElementById('cal').innerHTML;

  // Title read from ev.title, placed in .cal-title (the "wrong key" guard).
  assert.match(html, /class="cal-title">Dentist</);
  // Time read from ev.start_ts, formatted in the household tz into .cal-time.
  assert.match(html, /class="cal-time num">9:30am</);
  // The tap target carries the event id, so a tapped row can be re-found.
  assert.match(html, /data-eid="e1"/);
  // An all-day event renders the "all day" chip, not a clock time.
  assert.match(html, /class="cal-allday[^>]*>all day</);
  // A title containing markup is escaped — not rendered as live HTML.
  assert.match(html, /Camp &lt;b&gt;all week&lt;\/b&gt;/);
  assert.ok(!html.includes('<b>all week</b>'), 'event title markup is inert');
});

test('renderCalendar keeps an in-progress timed event on today, marked with its end time', () => {
  // Started yesterday, still running now: the backend keeps the row (its span
  // overlaps today) and the home feed must actually SHOW it on today — the
  // old start-day-only bucketing silently dropped exactly these events.
  const { document, sandbox } = newHub();
  sandbox.renderCalendar({
    date: '2026-08-14',
    calendar: { status: { ok: true }, events: [
      {
        id: 'trip', title: 'Overnighter', all_day: 0,
        start_ts: '2026-08-13T22:00:00-07:00',
        end_ts: '2026-08-14T06:00:00-07:00',
        color: '#5BC9F0',
      },
    ] },
  });
  const html = document.getElementById('cal').innerHTML;
  assert.match(html, /class="cal-title">Overnighter</, 'the running event is on the feed');
  // On a continuation day the time cell shows when it ENDS, not a stale start
  assert.match(html, /class="cal-time num">→ 6am</);
});

test('eventRow: a multi-day TIMED event shows start / all-day / end across its span', () => {
  const { sandbox } = newHub();
  const ev = { id: 'x', title: 'Conference', all_day: 0,
    start_ts: '2026-08-10T22:00:00-07:00', end_ts: '2026-08-12T06:00:00-07:00' };
  // start day: its start time
  assert.match(sandbox.eventRow(ev, '2026-08-10'), /class="cal-time num">10pm</);
  // middle day: the event owns the whole day — the all-day chip, NOT a stale
  // "→ <final end time>" that reads as if it ended that day
  const mid = sandbox.eventRow(ev, '2026-08-11');
  assert.match(mid, /class="cal-allday[^>]*>all day</);
  assert.doesNotMatch(mid, /6am/, 'a middle day must not show the final end time');
  // end day: the end time, marked as a continuation
  assert.match(sandbox.eventRow(ev, '2026-08-12'), /class="cal-time num">→ 6am</);
});

test('safeColor is applied at the color sinks: a hostile color can NOT inject extra CSS', () => {
  // Guards the WIRING, not just the helper: reverting any sink from safeColor()
  // back to escapeHtml() (which passes ;/: through) would re-open CSS injection
  // and this test would fail — the isolated safeColor unit test would not catch it.
  const { document, sandbox } = newHub();
  const hostile = 'red;background:url(https://evil/x)';
  sandbox.renderCalendar({
    date: '2026-08-14',
    calendar: { status: { ok: true }, events: [
      { id: 'e1', title: 'Picnic', all_day: 1, start_ts: '2026-08-14', end_ts: '2026-08-15', color: hostile },
    ] },
  });
  const cal = document.getElementById('cal').innerHTML;
  assert.doesNotMatch(cal, /background:url/, 'no injected declaration at the event color sink');
  assert.match(cal, /background:transparent/, 'malformed color falls back to transparent');
  // person --pc / name color sink
  const card = sandbox.personCardHtml(
    { person: { id: 1, name: 'Bo', color: hostile }, chores: [], week: [], streak: 0, total: 0, done_count: 0 },
    { readonly: true });
  assert.doesNotMatch(card, /background:url/, 'no injected declaration at the person color sink');
  assert.match(card, /--pc:transparent/);
  // CalDAV Calendars-picker swatch sink: `color` comes straight from the
  // iCloud server's response, an untrusted-input sink like the two above.
  const picker = sandbox.caldavCollectionsHtml(
    [{ id: 'a', name: 'Hostile', color: hostile, comp_type: 'VEVENT', enabled: true }]);
  assert.doesNotMatch(picker, /background:url/, 'no injected declaration at the caldav-cal-dot color sink');
  assert.match(picker, /caldav-cal-dot" style="background:transparent"/);
});

test('scheduledPoll: skips a second tick while a poll is still in flight (no stacked requests)', async () => {
  const { sandbox } = newHub();
  await flush();   // let the load-time poll settle first (it uses poll(), not scheduledPoll)
  let hubCalls = 0;
  sandbox.fetch = (url) => { if (url === '/api/hub') hubCalls++; return new Promise(() => {}); }; // hangs
  sandbox.scheduledPoll();           // starts a poll that never resolves
  const afterFirst = hubCalls;
  assert.ok(afterFirst >= 1, 'the first scheduled tick issued a poll');
  sandbox.scheduledPoll();           // in-flight -> must NOT start another
  assert.equal(hubCalls, afterFirst, 'no second /api/hub request while one is in flight');
});

test('fetchCalWindow: a failed refresh keeps cached events but downgrades ok:true so the stale banner fires', async () => {
  const { sandbox } = newHub();
  await flush();
  sandbox.fetch = async () => okResp({ status: { ok: true },
    events: [{ id: 'x', title: 'Picnic', all_day: 1, start_ts: '2026-08-14', end_ts: '2026-08-15' }] });
  await sandbox.fetchCalWindow();
  assert.equal(vm.runInContext('calWin.status.ok', sandbox), true);
  assert.equal(vm.runInContext('calWin.events.length', sandbox), 1);
  sandbox.fetch = async () => { throw new Error('down'); };
  await sandbox.fetchCalWindow();
  assert.equal(vm.runInContext('calWin.events.length', sandbox), 1, 'cached events retained on failure');
  assert.equal(vm.runInContext('calWin.status.ok', sandbox), false, 'stale ok:true downgraded, banner will fire');
});

test('pruneEvIndex: rebuilds from live sources once the index grows past the cap', () => {
  const { sandbox } = newHub();
  // stuff the index past the 4000-key cap with stale ids from old windows
  const stale = Array.from({ length: 4001 }, (_, i) =>
    ({ id: 'old' + i, all_day: 1, start_ts: '2020-01-01', end_ts: '2020-01-02' }));
  sandbox.indexEvents(stale);
  // the two live sources hold a small current set
  vm.runInContext(
    "hubData = { calendar: { events: [{ id: 'live1', all_day: 1, start_ts: '2026-08-14', end_ts: '2026-08-15' }] } };"
    + " calWin = { events: [{ id: 'win1', all_day: 1, start_ts: '2026-08-14', end_ts: '2026-08-15' }] };",
    sandbox);
  sandbox.pruneEvIndex();
  assert.equal(vm.runInContext('Object.keys(evIndex).length', sandbox), 2, 'rebuilt to just the live events');
  assert.ok(vm.runInContext('!!evIndex.live1 && !!evIndex.win1', sandbox), 'on-screen events stay tappable');
  assert.ok(vm.runInContext('!evIndex.old0', sandbox), 'stale ids evicted');
});

test('renderCalendar reads calendar.status and shows the auth banner', () => {
  const { document, sandbox } = newHub();
  sandbox.renderCalendar({
    date: '2026-08-14',
    calendar: { status: { ok: false, needs_auth: true }, events: [] },
  });

  const html = document.getElementById('cal').innerHTML;
  // status is read under the right key and routed through calStatusMessage.
  assert.match(html, /cal-note/);
  assert.match(html, /sign-in expired/);
});

/* ------------------------------------------ calendar sync-window marking */
// issue #37: the month/agenda views can page past the range the backend
// actually caches (calWin.window); a day out there must read as "not synced",
// not as a confident empty day. isDayOutsideWindow itself (common.js, pure)
// is unit-tested in hub.test.mjs; these exercise the markup it drives.

test('monthHtml marks a day past the sync window as not-synced, not falsely-empty', () => {
  const { sandbox } = newHub();
  const win = { from: '2026-08-01', to: '2026-08-28' };   // Aug 2026 grid tails into Sept
  const html = sandbox.monthHtml(2026, 8, [], '2026-08-14', win);

  const cellHtml = (date) => {
    const m = html.match(new RegExp(`<div class="[^"]*" data-date="${date}"[\\s\\S]*?<\\/div>`));
    assert.ok(m, `cell for ${date} rendered`);
    return m[0];
  };

  const inside = cellHtml('2026-08-14');
  assert.doesNotMatch(inside, /mg-unsynced/, 'a day inside the synced window is not marked');
  assert.doesNotMatch(inside, /not synced/);

  const beyond = cellHtml('2026-08-29');   // one day past win.to, still in-month
  assert.match(beyond, /class="[^"]*\bmg-unsynced\b/, 'a day past the synced window IS marked');
  assert.match(beyond, /not synced/i, 'a visible caption explains the empty cell');
});

test('monthHtml marks nothing when no sync window is known yet (boot / fetch-failure race)', () => {
  const { sandbox } = newHub();
  const html = sandbox.monthHtml(2026, 8, [], '2026-08-14', undefined);
  assert.doesNotMatch(html, /mg-unsynced/, 'fails open with no window data, matching isDayOutsideWindow');
});

test('monthHtml: an event on the day wins over the unsynced marking (defensive, should never co-occur)', () => {
  const { sandbox } = newHub();
  const win = { from: '2026-08-01', to: '2026-08-15' };
  const ev = { id: 'e1', title: 'Somehow cached', all_day: 1,
    start_ts: '2026-08-29', end_ts: '2026-08-30' };
  const html = sandbox.monthHtml(2026, 8, [ev], '2026-08-14', win);
  const cellHtml = html.match(/<div class="[^"]*" data-date="2026-08-29"[\s\S]*?<\/div>/)[0];
  assert.doesNotMatch(cellHtml, /mg-unsynced/, 'a cell with an event to show is never marked unsynced');
  assert.match(cellHtml, /Somehow cached/);
});

test('monthHtml never marks an out-of-month padding cell (.mg-out) as unsynced, even when it falls outside the window', () => {
  // .mg-out is already opacity-dimmed (0.38); stacking .mg-unsynced's hatch +
  // caption under that opacity would render them nearly illegible right on the
  // grid's own filler cells, which the family isn't reading as "this page".
  const { sandbox } = newHub();
  const win = { from: '2026-08-01', to: '2026-08-05' };   // most of the grid falls outside this
  const html = sandbox.monthHtml(2026, 8, [], '2026-08-14', win);

  const cellHtml = (date) => {
    const m = html.match(new RegExp(`<div class="[^"]*" data-date="${date}"[\\s\\S]*?<\\/div>`));
    assert.ok(m, `cell for ${date} rendered`);
    return m[0];
  };

  const julyPadding = cellHtml('2026-07-26');   // grid start: the Sunday before Aug 1 (mg-out)
  assert.match(julyPadding, /\bmg-out\b/, 'sanity: this is an adjacent-month padding cell');
  assert.doesNotMatch(julyPadding, /mg-unsynced/, 'a padding cell is never also marked unsynced');

  const inMonth = cellHtml('2026-08-14');   // in-month, past the tiny window
  assert.match(inMonth, /\bmg-unsynced\b/, 'an in-month day past the window is still marked');
});

test('agendaHtml: a day past the sync window reads "not synced", not "nothing scheduled"', () => {
  const { sandbox } = newHub();
  const win = { from: '2026-08-01', to: '2026-08-15' };
  // A single-day agenda (maxDays=1, as renderCalFull's day-drill view calls it)
  // landing one day past the window.
  const beyond = sandbox.agendaHtml([], '2026-08-16', '2026-08-14', 1, null, win);
  assert.match(beyond, /cal-day-unsynced/);
  assert.match(beyond, /not synced/i);
  assert.doesNotMatch(beyond, /nothing scheduled/);

  const inside = sandbox.agendaHtml([], '2026-08-10', '2026-08-14', 1, null, win);
  assert.doesNotMatch(inside, /cal-day-unsynced/);
  assert.match(inside, /nothing scheduled/);
});

test('agendaHtml: an event on the day wins over the unsynced marking (defensive, should never co-occur)', () => {
  const { sandbox } = newHub();
  const win = { from: '2026-08-01', to: '2026-08-15' };
  const ev = { id: 'e1', title: 'Somehow cached', all_day: 0,
    start_ts: '2026-08-20T10:00:00-07:00', end_ts: '2026-08-20T11:00:00-07:00' };
  const html = sandbox.agendaHtml([ev], '2026-08-20', '2026-08-14', 1, null, win);
  assert.doesNotMatch(html, /cal-day-unsynced/, 'a day with an event to show is never marked unsynced');
  assert.match(html, /Somehow cached/);
});

test('renderCalFull passes calWin.window through to the month grid', () => {
  const { document, sandbox } = newHub();
  // #cal-full is normally created by openOverlay('calendar'); build it
  // directly so renderCalFull can be driven without the overlay/fetch
  // machinery, and without depending on the real wall-clock date (which
  // calGoToday()'s fallback would otherwise pull in via openOverlay).
  const host = document.createElement('div');
  host._id = 'cal-full';
  document.body.appendChild(host);
  vm.runInContext(
    "data_date = '2026-08-14';"
    + "calState.mode = 'month'; calState.y = 2026; calState.m = 8;"
    + "calWin = { status: { ok: true }, events: [], "
    + "window: { from: '2026-08-01', to: '2026-08-05' } };",
    sandbox);

  sandbox.renderCalFull();

  const html = document.getElementById('cal-full').innerHTML;
  assert.match(html, /mg-unsynced/, 'a day beyond the fixture window is marked, via the real render path');
});

test('buildChoreForm is shared via common.js (usable from the hub context)', () => {
  const { document, sandbox } = newHub();
  assert.equal(typeof sandbox.buildChoreForm, 'function');
  assert.equal(typeof sandbox.freshChoreModel, 'function');

  // buildChoreForm took a `people` parameter in the move (Task 2): it used to
  // read admin.js's module-level `people` array as a free variable, which
  // doesn't exist in hub.js's context. Passing an active + an inactive person
  // here doubles as proof the active-only filtering still works post-move.
  const people = [
    { id: 1, name: 'Sam', color: '#5BC9F0', active: 1 },
    { id: 2, name: 'Alex', color: '#8AE0AD', active: 0 },
  ];
  const host = document.createElement('div');
  sandbox.buildChoreForm(host, sandbox.freshChoreModel(), 'Add chore', () => {}, people);

  assert.ok(host.querySelector('.f-title'), 'the title input rendered');
  const personSelect = host.querySelector('.f-person');
  assert.ok(personSelect, 'the person picker rendered');
  assert.match(personSelect.innerHTML, /Sam/, 'active person listed');
  assert.doesNotMatch(personSelect.innerHTML, /Alex/, 'inactive person excluded');
  assert.match(host.innerHTML, /Add chore/, 'submit button carries the given label');
});

test('buildChoreForm: a one-time chore shows the date field and hides rotation', () => {
  const { document, sandbox } = newHub();
  const people = [{ id: 1, name: 'Sam', color: '#5BC9F0', active: 1 }];
  const host = document.createElement('div');
  const model = { ...sandbox.freshChoreModel(), repeat: 'once', date: '2026-08-20' };
  sandbox.buildChoreForm(host, model, 'Add chore', () => {}, people);

  const dateInput = host.querySelector('.f-date');
  assert.ok(dateInput, 'the date input rendered');
  assert.equal(dateInput.value, '2026-08-20', 'seeded with the model date');
  assert.ok(!dateInput.classList.contains('hidden'), 'date shown for a once chore');
  assert.ok(host.querySelector('.f-days').classList.contains('hidden'),
    'weekly day chips hidden for a once chore');
  const rotBtn = host.querySelector('[data-assign="rotation"]');
  assert.ok(rotBtn.classList.contains('hidden'),
    'the Rotation choice is hidden — a one-time chore is one person');
  assert.ok(!host.querySelector('.f-person').classList.contains('hidden'),
    'the single-person picker stays visible');
});

test('buildChoreForm: clicking "Once" from a rotation model coerces to fixed and seeds today', () => {
  const { document, sandbox } = newHub();
  const people = [
    { id: 1, name: 'Sam', color: '#5BC9F0', active: 1 },
    { id: 2, name: 'Alex', color: '#8AE0AD', active: 1 },
  ];
  const host = document.createElement('div');
  // start rotation-capable, no date — the real add-flow starting point
  const model = { ...sandbox.freshChoreModel(), repeat: 'weekly', assign: 'rotation', rot: [1, 2] };
  let captured = null;
  sandbox.buildChoreForm(host, model, 'Add chore', (body) => { captured = body; }, people);

  // click the "Once" repeat button (the reactive handler, not a pre-seeded
  // model): fire the .f-repeat onclick with a target that answers .closest()
  // the way a real tap on that button would.
  const onceBtn = host.querySelector('[data-repeat="once"]');
  onceBtn.closest = (s) => (selectorMatches(onceBtn, s) ? onceBtn : null);
  host.querySelector('.f-repeat').onclick({ target: onceBtn });

  const dateInput = host.querySelector('.f-date');
  assert.ok(!dateInput.classList.contains('hidden'), 'date shown after switching to Once');
  assert.match(dateInput.value, /^\d{4}-\d{2}-\d{2}$/, 'date seeded with a valid YYYY-MM-DD (todayISO)');
  assert.ok(host.querySelector('[data-assign="rotation"]').classList.contains('hidden'),
    'rotation choice hidden after switching to Once');

  // submitting now yields a once/fixed payload carrying the seeded date
  host.querySelector('[data-submit]').onclick();
  assert.equal(captured.schedule_kind, 'once');
  assert.equal(captured.assign_kind, 'fixed');
  assert.match(captured.date, /^\d{4}-\d{2}-\d{2}$/, 'payload carries the seeded date, not empty');
});

test('buildChoreForm: a once model with no date renders the date input seeded to today', () => {
  const { document, sandbox } = newHub();
  const people = [{ id: 1, name: 'Sam', color: '#5BC9F0', active: 1 }];
  const host = document.createElement('div');
  // repeat:'once' but date:'' — the todayISO() fallback branch on first render
  const model = { ...sandbox.freshChoreModel(), repeat: 'once', date: '' };
  sandbox.buildChoreForm(host, model, 'Add chore', () => {}, people);
  assert.match(host.querySelector('.f-date').value, /^\d{4}-\d{2}-\d{2}$/,
    'empty once date falls back to a valid todayISO string');
});

// Build a #chores-full host, seed hub.js's state as if the chores overlay were
// open on today, render it, and return helpers to inspect + tap the result.
//
// hub.js keeps choreState/data_date/hubData as module-lexical bindings (const/
// let), so they are NOT properties of the vm sandbox and can't be poked via
// `sandbox.foo = …`. A follow-up runInContext in the SAME context, however,
// shares that lexical scope — so `seed()` assigns them and `read()` reads them
// straight from the running module, exactly what the real code sees.
function mountChoresFull(people, adminState = SAMPLE_ADMIN) {
  const registry = {};
  const clickHandlers = [];
  const document = {
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new FakeEl(registry, tag),
    addEventListener: (type, fn) => { if (type === 'click') clickHandlers.push(fn); },
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  SEEDED_IDS.forEach((id) => {
    const el = new FakeEl(registry);
    el._id = id;
    registry[id] = el;
  });
  document.body = new FakeEl(registry, 'body');
  // hub.js's load-time syncAppHeight() sets --app-h on documentElement.style;
  // give it a documentElement so the module loads (these tests don't assert on
  // the height var — newHub() covers that).
  document.documentElement = { scrollTop: 0, style: { setProperty() {} } };
  // openOverlay('chores') writes #chores-full via innerHTML; our fake parser
  // doesn't register parsed nodes by id, so pre-register a real host FakeEl.
  const choresFull = new FakeEl(registry);
  choresFull._id = 'chores-full';
  registry['chores-full'] = choresFull;

  const completeCalls = [];
  const adminChoreCalls = [];   // POST/PATCH /api/admin/chores writes from the editor
  const adminPeopleCalls = [];  // POST/PATCH/DELETE /api/admin/people from the people editor
  const okJson = (v) => ({ ok: true, status: 200, json: async () => v });
  const sandbox = {
    document,
    window: { addEventListener: () => {}, innerWidth: 1280, innerHeight: 800 },
    innerWidth: 1280, innerHeight: 800,
    // Generic host (never a real house IP) for any code that reads location.
    location: { host: 'hub.example:8138', protocol: 'http:' },
    scrollTo: () => {},
    setTimeout: () => 0, setInterval: () => 0,
    clearTimeout: () => {}, clearInterval: () => {},
    _people: people,   // handed to the module via seed() below
    // Record chore mutations so tests can assert what the editor sent. The
    // editor pulls /api/admin/state (flat people + full chore records) before
    // opening. A completion POST is recorded separately so an edit-mode test can
    // prove /complete is NEVER hit. Any other fetch (a stray poll) takes offline.
    fetch: async (url, opts) => {
      if (typeof url === 'string') {
        if (/\/api\/chores\/\d+\/complete/.test(url)) {
          completeCalls.push({ url, opts });
          return okJson({});
        }
        if (url === '/api/admin/state') {
          return okJson({ people: adminState.people, chores: adminState.chores });
        }
        if (/\/api\/admin\/chores(\/\d+)?$/.test(url)) {
          adminChoreCalls.push({
            url,
            method: (opts && opts.method) || 'GET',
            body: opts && opts.body ? JSON.parse(opts.body) : null,
          });
          return okJson({ id: 99 });
        }
        if (/\/api\/admin\/people(\/\d+)?$/.test(url)) {
          adminPeopleCalls.push({
            url,
            method: (opts && opts.method) || 'GET',
            body: opts && opts.body ? JSON.parse(opts.body) : null,
          });
          return okJson({ id: 99 });
        }
      }
      throw new Error('offline in test');
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(commonSrc, sandbox);
  vm.runInContext(hubSrc, sandbox);

  // Seed the module's lexical state to "chores overlay open on today", then
  // render the full view exactly as openOverlay('chores') would.
  vm.runInContext(
    'data_date = "2026-08-14"; hubData = { people: _people };'
    + ' openView = "chores"; choreState.day = "2026-08-14"; choreState.editing = false;',
    sandbox);
  sandbox.renderChoresFull(people);

  const fireClick = (target) => clickHandlers.forEach((fn) => fn({ target }));
  // Drive the real delegated click handler: find the node the tap lands on and
  // give it the .closest(sel) the handler calls (self-match by selector — every
  // tap target here carries its own hook attribute/class).
  const tap = (sel) => {
    const node = queryFirst(choresFull._queryChildren, sel);
    assert.ok(node, `a node matching ${sel} exists to tap`);
    node.closest = (s) => (selectorMatches(node, s) ? node : null);
    fireClick(node);
  };
  const read = (expr) => vm.runInContext(expr, sandbox);
  // The delete confirm lives in index.html (#confirm-modal), NOT inside
  // #chores-full, so `tap` (which searches the chores tree) can't reach its
  // buttons. Fire the delegated handler with a synthetic target that answers
  // .closest() for exactly the selectors that button carries — same shape a
  // real tap on Cancel / Delete inside .confirm-card / .confirm-modal presents.
  const tapConfirm = (matches) => {
    const node = { closest: (s) => (matches.includes(s) ? node : null) };
    fireClick(node);
  };
  return {
    sandbox, document, registry, choresFull, completeCalls, adminChoreCalls,
    adminPeopleCalls, tap, tapConfirm, read,
  };
}

const SAMPLE_PEOPLE = [{
  person: { id: 1, name: 'Sam Rivera', color: '#5BC9F0' },
  chores: [{ id: 10, title: 'Feed cat', icon: '🐈', rot: false, done: false }],
  streak: 0,
  week: ['done', 'done', 'miss', 'today'],
  total: 1, done_count: 0,
}];

// What /api/admin/state returns: FLAT people (with active flags) + FULL chore
// records. The editor pulls this because the wall's /api/hub payload lacks both
// (its chore rows are minimal; its people are nested under .person).
const SAMPLE_ADMIN = {
  people: [
    { id: 1, name: 'Sam Rivera', color: '#5BC9F0', active: 1 },
    { id: 2, name: 'Alex Kim', color: '#8AE0AD', active: 1 },
  ],
  chores: [
    {
      id: 10, title: 'Feed cat', icon: '🐈', schedule_kind: 'daily', days_mask: 0,
      assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [],
    },
  ],
};

const flush = () => new Promise((r) => setImmediate(r));

// Fake fetch responses in the exact shape j() reads: a non-2xx `failResp` makes
// j() throw `new Error(detail)` (its `.json().detail` branch), which is what the
// editor/delete flows catch and surface as a toast. `okResp` mirrors the harness
// helper for the one route a failure test still needs to succeed (opening the
// editor pulls /api/admin/state before the write it's about to fail).
const okResp = (v) => ({ ok: true, status: 200, json: async () => v });
const failResp = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });

test('chores overlay: default (view) mode shows check-off rows, no Edit affordances', () => {
  const { choresFull, read } = mountChoresFull(SAMPLE_PEOPLE);
  assert.equal(read('choreState.editing'), false, 'opens in check-off mode');
  const html = choresFull.innerHTML;
  // check-off rows carry data-chore; the toggle offers "Edit"
  assert.match(html, /data-chore="10"/);
  assert.ok(!html.includes('data-edit-chore'), 'no edit-chore rows in view mode');
  assert.ok(!html.includes('data-add-chore'), 'no add rows in view mode');
  assert.match(html, /data-chedit="1"[^>]*>Edit</);
});

test('chores overlay: tapping Edit enters edit mode (state, add rows, edit-chore rows)', () => {
  const { choresFull, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');

  assert.equal(read('choreState.editing'), true, 'choreState.editing flipped on');
  const html = choresFull.innerHTML;
  // a "+ Add chore" row per person, carrying the person id
  assert.match(html, /data-add-chore="1"/);
  assert.match(html, /Add chore/);
  // chore rows now open the editor (data-edit-chore), NOT complete (data-chore)
  assert.match(html, /data-edit-chore="10"/);
  assert.ok(!html.includes('data-chore="10"'), 'no completion rows while editing');
  // the toggle now reads "Done" and is active
  assert.match(html, /data-chedit="1"[^>]*class="[^"]*active[^"]*"|class="[^"]*active[^"]*"[^>]*data-chedit/);
  assert.match(html, />Done</);
});

test('chores overlay: tapping a chore row in edit mode does NOT complete it', async () => {
  const { completeCalls, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');
  assert.equal(read('choreState.editing'), true);

  // Tap the edit-mode chore row: it must open the editor (Task 5), never POST
  // the completion endpoint.
  tap('[data-edit-chore="10"]');
  // let any (erroneous) async completion settle before asserting
  await new Promise((r) => setImmediate(r));
  assert.equal(completeCalls.length, 0, 'the /complete endpoint was never called in edit mode');
});

test('chores overlay: tapping a chore row in VIEW mode DOES complete it (positive control)', async () => {
  // The paired positive control for the edit-mode "does NOT complete" test above:
  // in the default (view) mode a data-chore tap must hit /complete exactly once.
  const { completeCalls, tap } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chore="10"]');            // check-off row (view mode, not editing)
  await flush();                       // toggleChore awaits attemptToggle + poll()

  assert.equal(completeCalls.length, 1, 'the /complete endpoint was called exactly once');
  assert.match(completeCalls[0].url, /\/api\/chores\/10\/complete/, 'targets the tapped chore id');
});

test('chores overlay: tapping Done returns to check-off (data-chore, no add rows)', () => {
  const { choresFull, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');            // -> editing
  assert.equal(read('choreState.editing'), true);
  tap('[data-chedit="1"]');            // -> back to view
  assert.equal(read('choreState.editing'), false, 'choreState.editing flipped off');

  const html = choresFull.innerHTML;
  assert.match(html, /data-chore="10"/, 'check-off rows are back');
  assert.ok(!html.includes('data-edit-chore'), 'edit-chore rows gone');
  assert.ok(!html.includes('data-add-chore'), 'add rows gone');
  assert.match(html, />Edit</, 'toggle reads Edit again');
});

test('chores overlay edit mode: "+ Add chore" opens the shared form with that person preselected', async () => {
  const { document, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');            // enter edit mode
  tap('[data-add-chore="1"]');         // tap Sam's add row (person id 1)
  await flush();                       // openChoreEditor awaits /api/admin/state

  assert.ok(!document.getElementById('chore-modal').classList.contains('hidden'),
    'the chore editor modal is shown above the overlay');
  const host = document.getElementById('chore-editor');
  assert.ok(host.querySelector('.f-title'), 'the shared chore form rendered (one form, reused)');
  // add seeds fixed_person_id -> that person's <option> carries `selected`
  assert.match(host.querySelector('.f-person').innerHTML, /value="1" selected/,
    'the tapped person is preselected');
  assert.equal(read('choreState.editing'), true, 'still in edit mode with the editor open');
});

test('chores overlay edit mode: a FAILED /api/admin/state fetch shows a toast and does NOT open the editor', async () => {
  // openChoreEditor pulls /api/admin/state before it can build the form. If that
  // fetch fails, it must surface a toast and leave the modal shut — never open an
  // empty/half-built editor.
  const { document, sandbox, tap } = mountChoresFull(SAMPLE_PEOPLE);
  document.getElementById('chore-modal').classList.add('hidden');   // index.html's initial state
  sandbox.fetch = async (url) => {
    if (url === '/api/admin/state') return failResp(503, 'hub down (test)');
    throw new Error('offline in test');
  };
  tap('[data-chedit="1"]');
  tap('[data-add-chore="1"]');          // openChoreEditor -> state fetch rejects
  await flush();

  assert.match(document.getElementById('toast').textContent, /Couldn.t open the editor/,
    'a toast explains the editor could not open');
  assert.ok(document.getElementById('chore-modal').classList.contains('hidden'),
    'the editor modal stayed hidden');
  assert.equal(document.getElementById('chore-editor').innerHTML, '',
    'no form was rendered');
});

test('openChoreEditor does not reopen the modal if the wall went home while /api/admin/state was still in flight', async () => {
  // openChoreEditor's fetch can outlive the overlay it was opened from: the
  // idle timer or a #overlay-home tap can run closeAllOverlays() mid-await.
  // When the fetch then resolves, it must not resurrect a modal over a wall
  // the user (or the idle return) already left.
  const { document, sandbox, tap } = mountChoresFull(SAMPLE_PEOPLE);
  document.getElementById('chore-modal').classList.add('hidden');   // index.html's initial state
  let resolveState;
  sandbox.fetch = async (url) => {
    if (url === '/api/admin/state') {
      return new Promise((res) => { resolveState = res; });   // held open until we resolve it below
    }
    throw new Error('offline in test');
  };
  tap('[data-chedit="1"]');
  tap('[data-add-chore="1"]');   // openChoreEditor starts; its /api/admin/state fetch is now pending

  sandbox.closeAllOverlays();    // the wall goes home WHILE that fetch is still in flight

  resolveState({ ok: true, status: 200,
    json: async () => ({ people: SAMPLE_ADMIN.people, chores: SAMPLE_ADMIN.chores }) });
  await flush();

  assert.ok(document.getElementById('chore-modal').classList.contains('hidden'),
    'the editor must NOT reopen after the wall already went home');
  assert.equal(document.getElementById('chore-editor').innerHTML, '',
    'no stale form got built into the editor host');
});

test('chores overlay edit mode: submitting the add form POSTs the right body and re-renders in edit mode', async () => {
  const { document, choresFull, adminChoreCalls, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');
  tap('[data-add-chore="1"]');
  await flush();

  const host = document.getElementById('chore-editor');
  // Simulate the operator: type a title. The fake <select> can't reflect its
  // selected <option>'s value the way a real browser does, so mirror the browser
  // by setting the picker's value to the preselected person.
  host.querySelector('.f-title').value = 'Water plants';
  host.querySelector('.f-person').value = '1';
  host.querySelector('[data-submit]').onclick();
  await flush();

  const posts = adminChoreCalls.filter((c) => c.method === 'POST');
  assert.equal(posts.length, 1, 'exactly one create POST');
  assert.equal(posts[0].url, '/api/admin/chores', 'create hits the admin chores route');
  assert.equal(posts[0].body.title, 'Water plants');
  assert.equal(posts[0].body.assign_kind, 'fixed');
  assert.equal(posts[0].body.fixed_person_id, 1, 'the preselected person rode along in the body');

  assert.ok(document.getElementById('chore-modal').classList.contains('hidden'),
    'the editor closes after a successful save');
  assert.equal(read('choreState.editing'), true, 'stayed in edit mode after the refresh');
  assert.match(choresFull.innerHTML, /data-add-chore="1"/, 'the chores view re-rendered in edit mode');
});

test('chores overlay edit mode: a FAILED add save shows a toast, keeps the editor open, and does not refresh', async () => {
  // detect-don't-swallow: when the POST fails, submitChore surfaces the reason as
  // a toast, leaves the editor OPEN (typed input isn't lost), and never runs the
  // success path (close + refresh). /api/admin/state still succeeds so the editor
  // can open; only the write is failed.
  const { document, sandbox, tap } = mountChoresFull(SAMPLE_PEOPLE);
  sandbox.fetch = async (url) => {
    if (url === '/api/admin/state') return okResp({ people: SAMPLE_ADMIN.people, chores: SAMPLE_ADMIN.chores });
    if (/\/api\/admin\/chores(\/\d+)?$/.test(url)) return failResp(500, 'Disk full (test)');
    throw new Error('offline in test');
  };
  tap('[data-chedit="1"]');
  tap('[data-add-chore="1"]');
  await flush();                       // editor opens (state fetch succeeded)

  const host = document.getElementById('chore-editor');
  host.querySelector('.f-title').value = 'Water plants';
  host.querySelector('.f-person').value = '1';
  host.querySelector('[data-submit]').onclick();   // POST -> rejects
  await flush();

  // (a) the failure reason surfaces as a toast (j()'s detail rides through)
  assert.match(document.getElementById('toast').textContent, /Disk full/,
    'the write failure surfaces as a toast');
  // (b) the editor stays OPEN so the typed input isn't lost
  assert.ok(!document.getElementById('chore-modal').classList.contains('hidden'),
    'the editor modal is still open after a failed save');
  assert.equal(host.querySelector('.f-title').value, 'Water plants',
    'the typed title is still in the form');
  // (c) no success refresh — the editor form was NOT cleared (refreshChoresAfterEdit
  // closes the editor by wiping #chore-editor.innerHTML; a failed save must not)
  assert.ok(host.querySelector('.f-title'), 'the form was not torn down by a refresh');
});

test('chores overlay edit mode: tapping a chore row seeds the form from choreToModel and saves via PATCH', async () => {
  const { document, adminChoreCalls, tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');
  tap('[data-edit-chore="10"]');       // edit "Feed cat" (chore id 10)
  await flush();

  assert.ok(!document.getElementById('chore-modal').classList.contains('hidden'), 'editor shown');
  const host = document.getElementById('chore-editor');
  // seeded from choreToModel(the FULL record fetched via /api/admin/state)
  assert.equal(host.querySelector('.f-title').value, 'Feed cat', 'title seeded from the chore');
  assert.match(host.querySelector('.f-person').innerHTML, /value="1" selected/,
    'the assigned person is seeded');

  // rename and save
  host.querySelector('.f-title').value = 'Feed the cat';
  host.querySelector('.f-person').value = '1';
  host.querySelector('[data-submit]').onclick();
  await flush();

  const patches = adminChoreCalls.filter((c) => c.method === 'PATCH');
  assert.equal(patches.length, 1, 'exactly one PATCH');
  assert.equal(patches[0].url, '/api/admin/chores/10', 'PATCH targets the edited chore id');
  assert.equal(patches[0].body.title, 'Feed the cat', 'the rename is sent');
  assert.equal(read('choreState.editing'), true, 'stayed in edit mode');
});

test('chores overlay edit mode: each chore row grows a delete control; view mode has none', () => {
  const { choresFull, tap } = mountChoresFull(SAMPLE_PEOPLE);
  // view mode: no delete affordance
  assert.ok(!choresFull.innerHTML.includes('data-del-chore'), 'no delete control in view mode');
  tap('[data-chedit="1"]');   // enter edit mode
  // the delete control rides INSIDE the data-edit-chore row, carrying the cid
  assert.match(choresFull.innerHTML, /data-del-chore="10"/, 'edit rows carry a delete control');
});

test('chores overlay edit mode: tapping delete shows a custom confirm naming the chore, fires NO delete yet', () => {
  const { document, choresFull, adminChoreCalls, tap } = mountChoresFull(SAMPLE_PEOPLE);
  const modal = document.getElementById('confirm-modal');
  modal.classList.add('hidden');   // mirror index.html's initial hidden state
  tap('[data-chedit="1"]');
  tap('[data-del-chore="10"]');

  // a CUSTOM confirm appears (not window.confirm), naming the chore by title
  assert.ok(!modal.classList.contains('hidden'), 'the confirm modal is shown above the overlay');
  assert.match(document.getElementById('confirm-msg').textContent, /Delete/);
  assert.match(document.getElementById('confirm-msg').textContent, /Feed cat/, 'names the chore');
  // nothing deleted yet — the write waits for an explicit Delete tap
  assert.equal(adminChoreCalls.filter((c) => c.method === 'DELETE').length, 0,
    'no DELETE fired on the first tap');
  // and it did NOT re-render the chores view away from edit mode
  assert.match(choresFull.innerHTML, /data-edit-chore="10"/, 'still in edit mode');
});

test('chores overlay edit mode: tapping delete does NOT also open the editor (del before edit)', () => {
  const { document, tap } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');
  tap('[data-del-chore="10"]');
  // precedence: the tap opened the confirm, never the shared chore editor.
  assert.equal(document.getElementById('chore-editor').innerHTML, '',
    'the editor form was NOT rendered by a delete tap');
});

test('chores overlay edit mode: Cancel dismisses the confirm with no fetch', () => {
  const { document, adminChoreCalls, tap, tapConfirm } = mountChoresFull(SAMPLE_PEOPLE);
  const modal = document.getElementById('confirm-modal');
  modal.classList.add('hidden');
  tap('[data-chedit="1"]');
  tap('[data-del-chore="10"]');
  assert.ok(!modal.classList.contains('hidden'), 'confirm is up');

  tapConfirm(['[data-confirm-cancel]', '.confirm-modal', '.confirm-card', '.confirm-cancel']);
  assert.ok(modal.classList.contains('hidden'), 'Cancel hides the confirm');
  assert.equal(adminChoreCalls.filter((c) => c.method === 'DELETE').length, 0,
    'Cancel never calls the delete endpoint');
});

test('chores overlay edit mode: Delete calls DELETE with the cid and re-renders in edit mode', async () => {
  const { document, choresFull, adminChoreCalls, tap, tapConfirm, read } =
    mountChoresFull(SAMPLE_PEOPLE);
  const modal = document.getElementById('confirm-modal');
  modal.classList.add('hidden');
  tap('[data-chedit="1"]');
  tap('[data-del-chore="10"]');

  // confirm the delete
  tapConfirm(['[data-confirm-del]', '.confirm-modal', '.confirm-card', '.confirm-del']);
  await flush();   // confirmDelete awaits the DELETE, then refreshChoresAfterEdit

  const dels = adminChoreCalls.filter((c) => c.method === 'DELETE');
  assert.equal(dels.length, 1, 'exactly one DELETE');
  assert.equal(dels[0].url, '/api/admin/chores/10', 'DELETE targets the chore id');
  assert.ok(modal.classList.contains('hidden'), 'the confirm closed after deleting');
  assert.equal(read('choreState.editing'), true, 'stayed in edit mode after the delete');
  assert.match(choresFull.innerHTML, /data-add-chore="1"/, 'the chores view re-rendered in edit mode');
});

test('chores overlay edit mode: a FAILED Delete shows a toast and does NOT run the success refresh', async () => {
  // detect-don't-swallow: when the DELETE fails, confirmDelete surfaces the reason
  // as a toast and RETURNS before refreshChoresAfterEdit — so the chore stays on
  // screen instead of vanishing as if it were removed. The load-bearing assertion
  // is that the refresh's poll() (a GET /api/hub) never fired: a "chore still
  // listed" check alone is a false safety net here, because the harness has no
  // /api/hub route, so poll() no-ops and the markup is unchanged whether or not
  // the refresh ran. Counting /api/hub hits after the failed DELETE is the real
  // signal — mirrors how the submitChore-failure test asserts a post-failure
  // side-effect rather than trusting unchanged markup.
  const { document, sandbox, choresFull, tap, tapConfirm } = mountChoresFull(SAMPLE_PEOPLE);
  const modal = document.getElementById('confirm-modal');
  modal.classList.add('hidden');
  let hubPolls = 0;   // GET /api/hub fires only from poll(), i.e. from a refresh
  sandbox.fetch = async (url, opts) => {
    if (typeof url === 'string' && url.startsWith('/api/hub')) hubPolls += 1;
    if (/\/api\/admin\/chores\/\d+$/.test(url) && opts && opts.method === 'DELETE') {
      return failResp(500, 'Delete failed (test)');
    }
    throw new Error('offline in test');
  };
  tap('[data-chedit="1"]');
  tap('[data-del-chore="10"]');
  tapConfirm(['[data-confirm-del]', '.confirm-modal', '.confirm-card', '.confirm-del']);
  await flush();                       // confirmDelete awaits the DELETE (rejects)

  assert.match(document.getElementById('toast').textContent, /Delete failed/,
    'the delete failure surfaces as a toast');
  // the real contract: the failure path returned BEFORE refreshChoresAfterEdit,
  // so its poll() never ran. If confirmDelete swallowed and fell through to the
  // refresh, this count would be 1 and the test would fail.
  assert.equal(hubPolls, 0, 'no success refresh ran after the failed delete (poll never fired)');
  // and the chore is still on screen (the view was never re-rendered to drop it)
  assert.match(choresFull.innerHTML, /data-edit-chore="10"/, 'the chore is still on screen');
});

test('chores overlay: navigating off today clears edit mode (no silent resume beside save controls)', () => {
  const { tap, read } = mountChoresFull(SAMPLE_PEOPLE);
  tap('[data-chedit="1"]');
  assert.equal(read('choreState.editing'), true);
  tap('[data-chnav="prev"]');          // leave today
  assert.equal(read('choreState.editing'), false, 'edit mode is cleared when leaving today');
});

test('chores overlay: no footer link to the retired admin page (all management is inline)', () => {
  const { choresFull, tap } = mountChoresFull(SAMPLE_PEOPLE);
  // admin.html was retired 2026-08-15 — the "Manage on the admin page" footer is
  // gone from BOTH modes; the Edit button + inline People section replace it.
  assert.ok(!choresFull.querySelector('.manage-admin-link'), 'no admin-page link in view mode');
  assert.ok(!choresFull.innerHTML.includes('admin.html'), 'no /admin.html reference in view mode');
  tap('[data-chedit="1"]');
  assert.ok(!choresFull.querySelector('.manage-admin-link'), 'no admin-page link in edit mode');
  assert.ok(!choresFull.innerHTML.includes('admin.html'), 'no /admin.html reference in edit mode');
});

test('renderPeople: the empty-people state points at All chores → Edit, not the retired admin page', () => {
  const { document, sandbox } = newHub();
  sandbox.renderPeople({ people: [] });
  const html = document.getElementById('people').innerHTML;
  // With no household yet, the wall's only on-screen instruction for adding
  // people must route to the live path (All chores → Edit), never the dead
  // /admin.html the retirement removed.
  assert.match(html, /empty-hub/, 'the empty-people tile rendered');
  assert.match(html, /All chores/, 'directs the user to the All chores overlay');
  assert.match(html, /Edit/, 'and to its Edit mode, where people are added');
  assert.ok(!html.includes('admin.html'), 'no reference to the retired admin page');
});

// --- inline people admin on the Chores page (edit mode) ------------------

// Enter edit mode and let ensurePeopleThenRerender's /api/admin/state fetch
// resolve + repaint, so the people-editor section is present in the DOM.
async function enterEditWithPeople(ctx) {
  ctx.tap('[data-chedit="1"]');
  await flush();               // ensurePeopleThenRerender fetch + re-render
}

// hub.js's showToast creates #toast on <body> and writes the message as
// textContent (see the showToast test above).
const readToast = (ctx) => {
  const el = ctx.document.getElementById('toast');
  return el ? el.textContent : '';
};

test('people admin: edit mode renders a People section with a row + controls per person', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  assert.ok(!ctx.choresFull.innerHTML.includes('padmin'), 'no people editor in view mode');
  await enterEditWithPeople(ctx);
  const html = ctx.choresFull.innerHTML;
  assert.match(html, /class="padmin"/, 'the people section rendered');
  assert.match(html, /Sam Rivera/);
  assert.match(html, /Alex Kim/);
  // per-person controls + the add-person affordance
  assert.ok(ctx.choresFull.querySelector('[data-pedit="1"]'), 'Edit control');
  assert.ok(ctx.choresFull.querySelector('[data-ptoggle="1"]'), 'Deactivate control');
  assert.ok(ctx.choresFull.querySelector('[data-pdel="1"]'), 'Delete control');
  assert.ok(ctx.choresFull.querySelector('[data-padd="1"]'), 'Add person control');
});

test('people admin: tapping "Add person" opens the shared person form; submit POSTs {name,color}', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  ctx.tap('[data-padd="1"]');
  const editor = ctx.registry['chore-editor'];
  assert.ok(editor.querySelector('[data-pname]'), 'the person name field opened in the modal');
  assert.ok(!ctx.registry['chore-modal'].classList.contains('hidden'), 'the modal is shown');
  // fill the name and submit (color defaults to the first swatch)
  editor.querySelector('[data-pname]').value = 'Jordan';
  editor.querySelector('[data-psubmit]').onclick();
  await flush();
  const post = ctx.adminPeopleCalls.find((c) => c.method === 'POST');
  assert.ok(post, 'a POST /api/admin/people fired');
  assert.equal(post.url, '/api/admin/people');
  assert.equal(post.body.name, 'Jordan');
  assert.match(post.body.color, /^#[0-9A-Fa-f]{6}$/, 'a palette color was sent');
});

test('people admin: tapping Edit seeds the form and saves via PATCH', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  ctx.tap('[data-pedit="1"]');
  const editor = ctx.registry['chore-editor'];
  assert.equal(editor.querySelector('[data-pname]').value, 'Sam Rivera', 'seeded with the name');
  editor.querySelector('[data-pname]').value = 'Samuel';
  editor.querySelector('[data-psubmit]').onclick();
  await flush();
  const patch = ctx.adminPeopleCalls.find((c) => c.method === 'PATCH');
  assert.ok(patch, 'a PATCH fired');
  assert.equal(patch.url, '/api/admin/people/1');
  assert.equal(patch.body.name, 'Samuel');
});

test('people admin: Deactivate PATCHes the active flag off', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  ctx.tap('[data-ptoggle="1"]');
  await flush();
  const patch = ctx.adminPeopleCalls.find((c) => c.url === '/api/admin/people/1' && c.method === 'PATCH');
  assert.ok(patch, 'a PATCH fired');
  assert.equal(patch.body.active, 0, 'toggles active off (Sam starts active)');
});

test('people admin: Delete shows a person confirm (not the chore copy) and fires DELETE only on confirm', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  ctx.tap('[data-pdel="1"]');
  const modal = ctx.document.getElementById('confirm-modal');
  assert.ok(!modal.classList.contains('hidden'), 'the confirm is shown');
  assert.match(ctx.document.getElementById('confirm-msg').textContent, /Sam Rivera/, 'names the person');
  assert.match(ctx.document.getElementById('confirm-sub').textContent, /Removed for good/, 'blunt hard-delete warning');
  assert.equal(ctx.adminPeopleCalls.length, 0, 'no DELETE before confirming');
  // confirm
  ctx.tapConfirm(['[data-confirm-del]']);
  await flush();
  const del = ctx.adminPeopleCalls.find((c) => c.method === 'DELETE');
  assert.ok(del, 'a DELETE fired on confirm');
  assert.equal(del.url, '/api/admin/people/1');
});

test('people admin: a FAILED /api/admin/state keeps the cards and shows a visible note (not a silent blank)', async () => {
  // fetch fails for admin/state; the cards still paint and the section explains
  // itself rather than vanishing silently
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  ctx.sandbox.fetch = async (url) => {
    if (url === '/api/admin/state') return failResp(500, 'Disk full (test)');
    throw new Error('offline in test');
  };
  await enterEditWithPeople(ctx);
  assert.match(ctx.choresFull.innerHTML, /Feed cat/, 'chore cards still present');
  assert.ok(!ctx.choresFull.innerHTML.includes('class="padmin"'), 'no people editor on fetch failure');
  assert.match(ctx.choresFull.innerHTML, /couldn.t load people/i, 'a visible note explains the failure');
});

test('people admin: a FAILED add save shows the error inline and keeps the editor open', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  ctx.tap('[data-padd="1"]');
  const editor = ctx.registry['chore-editor'];
  // make the write fail (state still loads so the editor can open)
  ctx.sandbox.fetch = async (url) => {
    if (url === '/api/admin/state') return okResp({ people: SAMPLE_ADMIN.people, chores: SAMPLE_ADMIN.chores });
    if (url === '/api/admin/people') return failResp(422, 'name must be 1–30 characters');
    throw new Error('offline in test');
  };
  editor.querySelector('[data-pname]').value = '';
  editor.querySelector('[data-psubmit]').onclick();
  await flush();
  assert.ok(!ctx.registry['chore-modal'].classList.contains('hidden'), 'editor stays open on failure');
  const err = editor.querySelector('[data-perror]');
  assert.ok(!err.classList.contains('hidden'), 'the inline error is shown');
  assert.match(err.textContent, /name must be/, 'it carries the server detail');
});

test('people admin: a FAILED Deactivate shows a toast and does not refresh', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  let polls = 0;
  ctx.sandbox.fetch = async (url, opts) => {
    if (/\/api\/hub/.test(url)) { polls++; throw new Error('offline'); }
    if (/\/api\/admin\/people\/\d+$/.test(url) && opts && opts.method === 'PATCH') {
      return failResp(500, 'Disk full (test)');
    }
    throw new Error('offline in test');
  };
  ctx.tap('[data-ptoggle="1"]');
  await flush();
  assert.match(readToast(ctx), /disk full/i, 'a toast surfaces the failure reason');
  assert.equal(polls, 0, 'no refresh poll ran after the failed write');
});

test('people admin: a FAILED person Delete shows a toast and runs no refresh', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  await enterEditWithPeople(ctx);
  let polls = 0;
  ctx.sandbox.fetch = async (url, opts) => {
    if (/\/api\/hub/.test(url)) { polls++; throw new Error('offline'); }
    if (/\/api\/admin\/people\/\d+$/.test(url) && opts && opts.method === 'DELETE') {
      return failResp(500, 'Disk full (test)');
    }
    throw new Error('offline in test');
  };
  ctx.tap('[data-pdel="1"]');
  ctx.tapConfirm(['[data-confirm-del]']);
  await flush();
  assert.match(readToast(ctx), /disk full/i, 'a toast surfaces the failure reason');
  assert.equal(polls, 0, 'no refresh poll ran after the failed delete');
});

test('people admin: an inactive person shows Activate + .inactive and reactivates (PATCH active:1)', async () => {
  const adminState = {
    people: [{ id: 1, name: 'Sam Rivera', color: '#5BC9F0', active: 0 }],
    chores: [],
  };
  const ctx = mountChoresFull(SAMPLE_PEOPLE, adminState);
  await enterEditWithPeople(ctx);
  const row = ctx.choresFull.querySelector('[data-padmin="1"]');
  assert.ok(row.classList.contains('inactive'), 'inactive row carries .inactive');
  assert.match(ctx.choresFull.innerHTML, /Activate/, 'shows the Activate label');
  ctx.tap('[data-ptoggle="1"]');
  await flush();
  const patch = ctx.adminPeopleCalls.find((c) => c.method === 'PATCH');
  assert.equal(patch.body.active, 1, 'reactivates (active 0 -> 1)');
});

test('people admin: a rename invalidates the cache so the re-render shows fresh data', async () => {
  const ctx = mountChoresFull(SAMPLE_PEOPLE);
  // install the stub BEFORE entering edit, so call #1 is the edit-mode load and
  // call #2 is the post-mutation re-fetch (proving the cache was invalidated).
  let stateCalls = 0;
  ctx.sandbox.fetch = async (url, opts) => {
    if (url === '/api/admin/state') {
      stateCalls++;
      const name = stateCalls === 1 ? 'Sam Rivera' : 'Samuel';
      return okResp({ people: [{ id: 1, name, color: '#5BC9F0', active: 1 }], chores: [] });
    }
    if (/\/api\/admin\/people\/\d+$/.test(url) && opts && opts.method === 'PATCH') return okResp({ id: 1 });
    if (/\/api\/hub/.test(url)) throw new Error('offline');   // poll takes offline
    throw new Error('offline in test');
  };
  await enterEditWithPeople(ctx);
  assert.equal(stateCalls, 1, 'edit mode loaded the people once');
  ctx.tap('[data-pedit="1"]');
  const editor = ctx.registry['chore-editor'];
  assert.equal(editor.querySelector('[data-pname]').value, 'Sam Rivera', 'seeded from the first load');
  editor.querySelector('[data-pname]').value = 'Samuel';
  editor.querySelector('[data-psubmit]').onclick();
  await flush(); await flush();   // submit -> refreshPeopleAdmin -> re-fetch -> re-render
  assert.ok(stateCalls >= 2, 'the cache was invalidated and re-fetched');
  assert.match(ctx.choresFull.innerHTML, /Samuel/, 'the re-render shows the fresh name');
});

test('sectionHead emits a .shead with tick, label, and an expand button', () => {
  const { sandbox } = newHub();
  const html = sandbox.sectionHead('Chores', { overlay: 'chores', expandLabel: 'All chores' });
  // the shared section-header shell: a tick + the uppercase-styled <h2> label
  assert.match(html, /class="shead"/);
  assert.match(html, /<span class="tick"><\/span>/);
  assert.match(html, /<h2>Chores<\/h2>/);
  // the expand button opens the given overlay and carries the ⛶ glyph + label
  assert.match(html, /class="expand"[^>]*data-overlay="chores"/);
  assert.match(html, /⛶ All chores/);
});

test('sectionHead with no overlay/expandLabel emits no expand button', () => {
  const { sandbox } = newHub();
  const html = sandbox.sectionHead('Plain Section');
  assert.match(html, /<h2>Plain Section<\/h2>/);
  assert.ok(!html.includes('class="expand"'), 'no expand button without an overlay');
  assert.ok(!html.includes('class="act"'), 'no action slot without an overlay');
});

test('sectionHead escapes label, expandLabel, and the overlay attribute', () => {
  const { sandbox } = newHub();
  const html = sandbox.sectionHead('A <b>x</b>', { overlay: 'panel:"y"', expandLabel: 'Z<i>' });
  assert.ok(!html.includes('<b>x</b>'), 'label markup is inert');
  assert.match(html, /A &lt;b&gt;x&lt;\/b&gt;/);
  assert.match(html, /data-overlay="panel:&quot;y&quot;"/, 'overlay attribute value is escaped');
  assert.match(html, /Z&lt;i&gt;/, 'expand label is escaped');
});

test('weatherCardHtml chips: when the category and the number DISAGREE, each OR operand that wins is pinned', () => {
  // Pins the CURRENT implemented behavior so a regression that drops one operand
  // of either OR is caught (this asserts behavior; it does not change hub.js).
  //   aqiGood = /good/i.test(aqi_cat) || (aqi != null && Number(aqi) <= 50)
  //   uvWarn  = Number(uv) >= 6      || /high|extreme|severe|very/.test(uv_desc)
  const { sandbox } = newHub();

  // Case A — the CATEGORY/DESC text disagrees with the number:
  //   AQI category says "Good" while the number (200) is bad -> the category
  //   operand wins, chip is GOOD. UV number (7) is high while the desc
  //   ("Moderate") is mild -> the number operand wins, chip is WARN.
  const a = sandbox.weatherCardHtml({
    temp: 70, unit: 'F', aqi: 200, aqi_cat: 'Good', uv: 7, uv_desc: 'Moderate',
  });
  assert.match(a, /<span class="q good">Good<\/span>/,
    'aqi_cat "Good" wins over the bad number 200 (category operand of the OR)');
  assert.match(a, /<span class="q warn">Moderate<\/span>/,
    'uv number 7 (>=6) wins over the mild desc "Moderate" (number operand of the OR)');

  // Case B — the OTHER operand of each OR wins, so both operands are pinned:
  //   AQI has no category but a good number (40) -> number operand -> GOOD.
  //   UV number (3) is low but the desc ("Extreme") is severe -> desc -> WARN.
  const b = sandbox.weatherCardHtml({
    temp: 70, unit: 'F', aqi: 40, aqi_cat: 'Unhealthy', uv: 3, uv_desc: 'Extreme',
  });
  assert.match(b, /<span class="q good">Unhealthy<\/span>/,
    'aqi number 40 <= 50 wins the GOOD class (number operand); label is the category text');
  assert.match(b, /<span class="q warn">Extreme<\/span>/,
    'uv desc "Extreme" wins over the low number 3 (desc operand of the OR)');
});

test('renderPeople puts an "All chores" expand button in the #people header', () => {
  const { document, sandbox } = newHub();
  const people = [{
    person: { id: 1, name: 'Sam', color: '#5BC9F0' },
    chores: [{ id: 10, title: 'Feed cat', icon: '', rot: false, done: false }],
    streak: 0,
    week: ['done', 'done', 'miss', 'today'],
    total: 1,
    done_count: 0,
  }];
  sandbox.renderPeople({ people });

  const host = document.getElementById('people');
  const btn = host.querySelector('[data-overlay="chores"]');
  assert.ok(btn, 'a data-overlay="chores" button rendered in #people');
  assert.ok(btn.classList.contains('expand'), 'uses the shared .expand button');
  assert.match(host.innerHTML, /class="shead"/, 'the header uses the one section-header system');
  assert.match(host.innerHTML, /All chores/, 'button carries the expected label');
  // The person card itself still rendered alongside the new header.
  assert.match(host.innerHTML, /Sam/);
});

test('renderTodoSlot: header sits OUTSIDE the .card, the list sits INSIDE it (Task 3 re-box)', () => {
  const { document, sandbox } = newHub();
  const data = {
    todos: {
      now: [{ id: 1, title: 'Call dentist', bucket: 'now', done_at: null }],
      soon: [{ id: 2, title: 'Return the bottles', bucket: 'soon', done_at: null }],
      later: [
        { id: 3, title: 'Order dog food', bucket: 'later', done_at: null },
        { id: 4, title: 'Clean gutters', bucket: 'later', done_at: null },
      ],
    },
  };

  sandbox.renderTodoSlot(data);
  const html = document.getElementById('todo-slot').innerHTML;

  // sectionHead (the .shead header) renders BEFORE — i.e. outside — the
  // .card that boxes the rows + count chips, matching .cal-day/.person-card.
  const headIdx = html.indexOf('class="shead"');
  const cardIdx = html.indexOf('class="card todo"');
  assert.ok(headIdx >= 0, 'the shead header renders');
  assert.ok(cardIdx >= 0, 'the list renders inside a "card todo" box');
  assert.ok(headIdx < cardIdx, 'the header comes before (outside) the card');
  assert.match(html, /Call dentist/);

  // The old unboxed wrapper + plain-text counts line are gone.
  assert.doesNotMatch(html, /class="todo-card"/);
  assert.doesNotMatch(html, /class="todo-counts"/);

  // Row markup/data-attributes are BYTE-FOR-BYTE what they were before the
  // re-box — hub.js's click delegate matches `[data-todo]` and the
  // `.todo-row.done, .todo-row-full.done` combinator directly against this
  // markup, so renaming/restructuring the row itself (as opposed to just its
  // container) would silently break add/check/move.
  assert.match(html,
    /<button class="todo-row" type="button" data-todo="1" aria-label="mark done: Call dentist">/);

  // Counts render as three chips (now/soon/later), 'now' carrying the accent
  // chip class — same open-item semantics as before (done items excluded).
  assert.match(html, /<div class="foot"><span class="chip now">1 now<\/span>/);
  assert.match(html, /<span class="chip">1 soon<\/span>/);
  assert.match(html, /<span class="chip">2 later<\/span><\/div>/);
});

test('renderTodoSlot: todos_ok===false shows a "couldn’t load" note, NOT an empty card', () => {
  const { document, sandbox } = newHub();
  // The server fell back to empty buckets on a read error and flagged it. The
  // wall must NOT render this as "nothing on the list" (which reads as "all
  // caught up") — it must say the list couldn't be loaded.
  sandbox.renderTodoSlot({ todos: { now: [], soon: [], later: [] }, todos_ok: false });
  const html = document.getElementById('todo-slot').innerHTML;
  assert.match(html, /couldn.t load the list/);
  assert.doesNotMatch(html, /nothing on the list/);
});

test('renderTodoSlot: a genuinely empty list (todos_ok omitted) still reads "nothing on the list"', () => {
  const { document, sandbox } = newHub();
  sandbox.renderTodoSlot({ todos: { now: [], soon: [], later: [] } });
  const html = document.getElementById('todo-slot').innerHTML;
  assert.match(html, /nothing on the list/);
  assert.doesNotMatch(html, /couldn.t load the list/);
});

test('renderTodoSlot: source=iCloud but iCloud unavailable falls back to the local card', () => {
  const { document, sandbox } = newHub();
  const buckets = { overdue: [], today: [], upcoming: [], no_date: [] };
  // todo_source says iCloud, but no icloud_caldav integration (disconnected
  // out-of-band): must render the LOCAL card, not a reassuring-empty iCloud one.
  sandbox.renderTodoSlot({ todo_source: 'icloud', reminders: buckets,
    todos: { now: [], soon: [], later: [] }, integrations: [] });
  let html = document.getElementById('todo-slot').innerHTML;
  assert.doesNotMatch(html, /shead-chip/);          // no "iCloud" chip -> local card
  assert.doesNotMatch(html, /data-reminder/);

  // with iCloud available it renders the reminder surface (the iCloud chip)
  sandbox.renderTodoSlot({ todo_source: 'icloud', reminders: buckets,
    reminders_writable: true, todos: { now: [], soon: [], later: [] },
    integrations: [{ id: 'icloud_caldav', enabled: true }] });
  html = document.getElementById('todo-slot').innerHTML;
  assert.match(html, /shead-chip">iCloud/);
});

test('todosFullHtml: full-screen buckets are boxed like every other section, rows untouched', () => {
  const { sandbox } = newHub();
  vm.runInContext(
    `todoState.data = { buckets: {
       now: [{ id: 1, title: 'Call dentist', bucket: 'now', done_at: null }],
       soon: [], later: [] },
       recent_done: [] };`,
    sandbox);

  const html = sandbox.todosFullHtml();

  // Each Now/Soon/Later column is boxed (Task 3 consistency pass) via the
  // existing bare-.card padding utility.
  const sectionCount = (html.match(/class="card pad todo-section"/g) || []).length;
  assert.equal(sectionCount, 3, 'three boxed bucket columns render');
  assert.match(html, /<div class="todo-sec-head">Now<\/div>/);

  // Full-row markup (the check/move/delete data-attributes attemptTodo's
  // handlers key off) is unchanged by the boxing.
  assert.match(html,
    /<button class="todo-row-main" type="button" data-todo="1" aria-label="mark done: Call dentist">/);
  assert.match(html, /data-todo-open="1"/);
});

// ---- iCloud reminders as the To-Do source -------------------------------

const REMINDERS = {
  overdue: [{ id: 'caldav:g/1', title: 'Renew tags', due: '2026-08-11', priority: 1 }],
  today: [{ id: 'caldav:g/2', title: 'Call the vet', due: '2026-08-17' }],
  upcoming: [{ id: 'caldav:g/3', title: 'Book flights', due: '2026-09-01' }],
  no_date: [{ id: 'caldav:g/4', title: 'Someday project' }],
};

test('renderTodoSlot: iCloud source renders the reminders card with a source chip, pressing rows, and count chips', () => {
  const { document, sandbox } = newHub();
  vm.runInContext("data_date = '2026-08-17';", sandbox);
  sandbox.renderTodoSlot({ todo_source: 'icloud', reminders_writable: true, reminders: REMINDERS,
    integrations: [{ id: 'icloud_caldav', enabled: true }] });
  const html = document.getElementById('todo-slot').innerHTML;

  // Header still reads "To-Do", now carrying the quiet iCloud source chip.
  assert.match(html, /<h2>To-Do<\/h2>/);
  assert.match(html, /class="shead-chip">iCloud</);
  // Pressing = overdue + today; a writable row is a tap-to-complete button.
  assert.match(html, /data-reminder="caldav:g\/1"/);
  assert.match(html, /Renew tags/);
  assert.match(html, /Call the vet/);
  // Overdue row shows its date; the high-priority one gets the "!" mark.
  assert.match(html, /class="rem-due">Tue 8\/11</);
  assert.match(html, /class="rem-pri"[^>]*>!</);
  // Three counts: overdue leads (accent chip), then today, then upcoming.
  assert.match(html, /<span class="chip now">1 overdue<\/span>/);
  assert.match(html, /<span class="chip">1 today<\/span>/);
  assert.match(html, /<span class="chip">1 upcoming<\/span>/);
});

test('renderTodoSlot: iCloud read-only renders inert rows — same items, no data-reminder to tap', () => {
  const { document, sandbox } = newHub();
  vm.runInContext("data_date = '2026-08-17';", sandbox);
  sandbox.renderTodoSlot({ todo_source: 'icloud', reminders_writable: false, reminders: REMINDERS,
    integrations: [{ id: 'icloud_caldav', enabled: true }] });
  const html = document.getElementById('todo-slot').innerHTML;
  assert.match(html, /Renew tags/);                 // the information is still shown
  assert.doesNotMatch(html, /data-reminder=/);      // but nothing is tappable
});

test('renderTodoSlot: iCloud with no reminders reads "nothing on the list", not a broken card', () => {
  const { document, sandbox } = newHub();
  sandbox.renderTodoSlot({ todo_source: 'icloud', reminders_writable: true,
    reminders: { overdue: [], today: [], upcoming: [], no_date: [] },
    integrations: [{ id: 'icloud_caldav', enabled: true }] });
  const html = document.getElementById('todo-slot').innerHTML;
  assert.match(html, /nothing on the list/);
  assert.match(html, /class="shead-chip">iCloud</);   // the iCloud card, not the local fallback
});

function mountRemindersFull(reminders, { writable = true, lists = [] } = {}) {
  const { document, sandbox } = newHub();
  vm.runInContext("data_date = '2026-08-17';", sandbox);
  vm.runInContext(`todoState.source = 'icloud';
    todoState.reminders = ${JSON.stringify({ buckets: reminders, configured: true, writable })};
    hubData = { reminder_lists: ${JSON.stringify(lists)} };`, sandbox);
  return { document, sandbox, html: sandbox.todosFullHtml() };
}

test('todosFullHtml (iCloud): buckets stack overdue-first, empty buckets are dropped, rows are writable', () => {
  const { html } = mountRemindersFull({
    overdue: REMINDERS.overdue, today: [], upcoming: REMINDERS.upcoming, no_date: [],
  }, { writable: true, lists: [{ id: 'caldav:g', name: 'Groceries' }] });

  // Only the non-empty buckets render, and Overdue comes before Upcoming.
  assert.match(html, /todo-sec-head">Overdue<\/div>/);
  assert.match(html, /todo-sec-head">Upcoming<\/div>/);
  assert.doesNotMatch(html, /todo-sec-head">Today<\/div>/);
  assert.doesNotMatch(html, /todo-sec-head">No date<\/div>/);
  assert.ok(html.indexOf('Overdue') < html.indexOf('Upcoming'), 'overdue leads');
  // Writable => check button + open/delete affordance.
  assert.match(html, /data-reminder="caldav:g\/1"/);
  assert.match(html, /data-reminder-open="caldav:g\/1"/);
  // Single enabled list => a naming placeholder and NO list picker.
  assert.match(html, /placeholder="Add to Groceries…"/);
  assert.doesNotMatch(html, /id="todo-list-select"/);
});

test('todosFullHtml (iCloud): more than one list adds a compact list picker defaulting to the first', () => {
  const { html } = mountRemindersFull(
    { overdue: [], today: REMINDERS.today, upcoming: [], no_date: [] },
    { writable: true, lists: [{ id: 'caldav:g', name: 'Groceries' }, { id: 'caldav:h', name: 'Home' }] });
  assert.match(html, /placeholder="Add a reminder…"/);
  assert.match(html, /id="todo-list-select"/);
  assert.match(html, /<option value="caldav:g" selected>Groceries<\/option>/);
  assert.match(html, /<option value="caldav:h">Home<\/option>/);
});

test('todosFullHtml (iCloud): read-only shows the reminders but no add form and no check controls', () => {
  const { html } = mountRemindersFull(
    { overdue: [], today: REMINDERS.today, upcoming: [], no_date: [] },
    { writable: false, lists: [{ id: 'caldav:g', name: 'Groceries' }] });
  assert.match(html, /Call the vet/);                 // information is present
  assert.doesNotMatch(html, /id="todo-add-form"/);    // no add control
  assert.doesNotMatch(html, /data-reminder=/);        // no tap-to-complete
  assert.doesNotMatch(html, /data-reminder-open=/);   // no delete affordance
});

test('todosFullHtml (iCloud): an unconfigured account points at Settings rather than faking an empty list', () => {
  const { document, sandbox } = newHub();
  vm.runInContext("todoState.source = 'icloud'; todoState.reminders = { configured: false, buckets: {} };", sandbox);
  assert.match(sandbox.todosFullHtml(), /iCloud isn’t connected — add it in Settings/);
});

test('toggleReminder: POSTs the id + completed flag to /api/reminders/toggle', async () => {
  const { sandbox } = newHub();
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, method: opts && opts.method, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (url === '/api/reminders/toggle') return okResp({ id: 'caldav:g/2', completed: true });
    throw new Error('offline in test');   // the refresh's poll/reminders GET — irrelevant here
  };
  await sandbox.toggleReminder('caldav:g/2', true);
  const post = calls.find((c) => c.url === '/api/reminders/toggle');
  assert.equal(post.method, 'POST');
  assert.deepEqual(post.body, { id: 'caldav:g/2', completed: true });
});

test('addReminder: targets the single list directly, and the picked list when there are several', async () => {
  const { document, sandbox } = newHub();
  const input = document.createElement('input'); input._id = 'todo-add-input'; input.value = 'Buy eggs';
  document.body.appendChild(input);
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (url === '/api/reminders/add') return okResp({ id: 'caldav:g/new', title: 'Buy eggs', due: null });
    throw new Error('offline in test');
  };

  // one list, no picker: uses it directly
  vm.runInContext("hubData = { reminder_lists: [{ id: 'caldav:g', name: 'Groceries' }] };", sandbox);
  await sandbox.addReminder();
  assert.deepEqual(calls.find((c) => c.url === '/api/reminders/add').body,
    { list_id: 'caldav:g', title: 'Buy eggs' });
  assert.equal(input.value, '', 'the input clears on success');

  // several lists + a picker: honours the selected list
  calls.length = 0; input.value = 'Fix gate';
  const sel = document.createElement('select'); sel._id = 'todo-list-select'; sel.value = 'caldav:h';
  document.body.appendChild(sel);
  vm.runInContext("hubData = { reminder_lists: [{ id: 'caldav:g', name: 'Groceries' }, { id: 'caldav:h', name: 'Home' }] };", sandbox);
  await sandbox.addReminder();
  assert.deepEqual(calls.find((c) => c.url === '/api/reminders/add').body,
    { list_id: 'caldav:h', title: 'Fix gate' });
});

test('deleteReminder: POSTs the id to /api/reminders/delete', async () => {
  const { sandbox } = newHub();
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (url === '/api/reminders/delete') return okResp({ id: 'caldav:g/2', deleted: true });
    throw new Error('offline in test');
  };
  await sandbox.deleteReminder('caldav:g/2');
  assert.deepEqual(calls.find((c) => c.url === '/api/reminders/delete').body, { id: 'caldav:g/2' });
});

test('a failed reminder write surfaces the right toast: read-only vs already-changed', async () => {
  const { document, sandbox } = newHub();
  sandbox.fetch = async (url) => {
    if (url === '/api/reminders/toggle') return failResp(409, 'iCloud reminders are read-only (enable two-way in settings)');
    throw new Error('offline in test');
  };
  await sandbox.toggleReminder('caldav:g/2', true);
  assert.match(document.getElementById('toast').textContent, /read-only/);

  sandbox.fetch = async (url) => {
    if (url === '/api/reminders/delete') return failResp(404, 'unknown reminder');
    throw new Error('offline in test');
  };
  await sandbox.deleteReminder('caldav:g/2');
  assert.match(document.getElementById('toast').textContent, /already changed on another device/);
});

test('setTodoSource: PATCHes /api/todo-source and no-ops when the source is unchanged', async () => {
  const { sandbox } = newHub();
  vm.runInContext("hubData = { todo_source: 'local' };", sandbox);
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, method: opts && opts.method, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (url === '/api/todo-source') return okResp({ source: 'icloud' });
    throw new Error('offline in test');   // the follow-up poll() — caught, irrelevant here
  };
  await sandbox.setTodoSource('icloud');
  const patch = calls.find((c) => c.url === '/api/todo-source');
  assert.equal(patch.method, 'PATCH');
  assert.deepEqual(patch.body, { source: 'icloud' });

  // Tapping the already-active source writes nothing.
  calls.length = 0;
  await sandbox.setTodoSource('local');
  assert.equal(calls.length, 0, 'no PATCH when the source is unchanged');
});

test('renderTodoSourcePicker: hidden without CalDAV; shown with a read-only hint once iCloud is chosen but still 1-way', () => {
  const { document, sandbox } = newHub();
  const host = document.createElement('div'); host._id = 'todo-source-ctl';
  document.body.appendChild(host);

  // No icloud_caldav integration => nothing to pick, so the picker stays empty.
  vm.runInContext("hubData = { integrations: [], todo_source: 'local' };", sandbox);
  sandbox.renderTodoSourcePicker();
  assert.equal(host.innerHTML, '');

  // CalDAV present, iCloud chosen, still read-only => the picker shows and warns.
  vm.runInContext("hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, readonly: true }], todo_source: 'icloud' };", sandbox);
  sandbox.renderTodoSourcePicker();
  assert.match(host.innerHTML, /data-todo-source="local"/);
  assert.match(host.innerHTML, /data-todo-source="icloud"/);
  assert.match(host.innerHTML, /seg-btn active" type="button" data-todo-source="icloud"/);
  assert.match(host.innerHTML, /read-only until you set Sync direction to 2-way/);

  // Two-way on => no hint.
  vm.runInContext("hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, readonly: false }], todo_source: 'icloud' };", sandbox);
  sandbox.renderTodoSourcePicker();
  assert.doesNotMatch(host.innerHTML, /read-only until/);
});

// ---- reminders driven through the REAL delegated click/submit handlers ----
// The tests above call toggleReminder/addReminder/deleteReminder directly, which
// skips the optimistic .done flip, the revert-on-failure, and the submit
// routing. This harness captures hub.js's delegated listeners (same pattern as
// mountChoresFull) and taps rendered nodes so those paths run for real.
function mountReminders({ surface = 'full', writable = true, lists = [],
  reminders = REMINDERS, fetch } = {}) {
  const registry = {};
  const clickHandlers = [];
  const submitHandlers = [];
  const document = {
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new FakeEl(registry, tag),
    addEventListener: (type, fn) => {
      if (type === 'click') clickHandlers.push(fn);
      if (type === 'submit') submitHandlers.push(fn);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  SEEDED_IDS.forEach((id) => { const el = new FakeEl(registry); el._id = id; registry[id] = el; });
  document.body = new FakeEl(registry, 'body');
  // hub.js's load-time syncAppHeight() sets --app-h on documentElement.style;
  // give it a documentElement so the module loads.
  document.documentElement = { scrollTop: 0, style: { setProperty() {} } };
  // The full view paints into #todos-page (not in SEEDED_IDS); the add flow reads
  // #todo-add-input by id (parsed innerHTML nodes aren't id-registered, so seed a
  // real one). Only the full surface needs these.
  if (surface === 'full') {
    const page = new FakeEl(registry); page._id = 'todos-page'; registry['todos-page'] = page;
    const inp = new FakeEl(registry, 'input'); inp._id = 'todo-add-input'; registry['todo-add-input'] = inp;
  }
  const sandbox = {
    document,
    window: { addEventListener: () => {}, innerWidth: 1280, innerHeight: 800 },
    innerWidth: 1280, innerHeight: 800,
    location: { host: 'hub.example:8138', protocol: 'http:' },
    scrollTo: () => {}, setTimeout: () => 0, setInterval: () => 0,
    clearTimeout: () => {}, clearInterval: () => {},
    fetch: fetch || (async () => { throw new Error('offline in test'); }),
  };
  vm.createContext(sandbox);
  vm.runInContext(commonSrc, sandbox);
  vm.runInContext(hubSrc, sandbox);

  const hub = {
    todo_source: 'icloud', reminders_writable: writable, reminders,
    reminder_lists: lists,
    integrations: [{ id: 'icloud_caldav', enabled: true, readonly: !writable }],
  };
  vm.runInContext(
    "data_date = '2026-08-17';"
    + ` hubData = ${JSON.stringify(hub)};`
    + " todoState.source = 'icloud';"
    + ` todoState.reminders = ${JSON.stringify({ buckets: reminders, configured: true, writable })};`
    + " openView = 'todos'; document.body.dataset.tab = 'todos';",
    sandbox);

  if (surface === 'home') sandbox.renderTodoSlot(hub);
  else sandbox.renderTodosPaint();

  const host = registry[surface === 'home' ? 'todo-slot' : 'todos-page'];
  const fireClick = (target) => clickHandlers.forEach((fn) => fn({ target }));
  const fireSubmit = (target) =>
    submitHandlers.forEach((fn) => fn({ target, preventDefault: () => {} }));
  const tap = (sel) => {
    const node = queryFirst(host._queryChildren, sel);
    assert.ok(node, `a node matching ${sel} exists to tap`);
    node.closest = (s) => (selectorMatches(node, s) ? node : null);
    fireClick(node);
    return node;
  };
  const read = (expr) => vm.runInContext(expr, sandbox);
  return { sandbox, document, registry, host, tap, fireSubmit, read };
}

test('reminder tap (home): the row flips to .done immediately and the write carries completed:true', () => {
  const calls = [];
  const { tap } = mountReminders({ surface: 'home', fetch: async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (url === '/api/reminders/toggle') return okResp({ id: 'caldav:g/1', completed: true });
    throw new Error('offline in test');
  } });
  const row = tap('[data-reminder]');                 // the overdue "Renew car tags" row
  assert.ok(row.classList.contains('done'), 'the tapped row is optimistically marked done');
  const post = calls.find((c) => c.url === '/api/reminders/toggle');
  assert.deepEqual(post.body, { id: 'caldav:g/1', completed: true });
});

test('reminder tap (home): a FAILED toggle reverts the optimistic .done and toasts (no strand offline)', async () => {
  const { tap, host, document } = mountReminders({ surface: 'home', fetch: async (url) => {
    if (url === '/api/reminders/toggle') return failResp(500, 'boom');
    throw new Error('offline in test');   // the follow-up isn't reached — we revert and return
  } });
  const row = tap('[data-reminder]');
  assert.ok(row.classList.contains('done'), 'optimistic flip happens first');
  await flush();                                       // let toggleReminder settle
  // The home card must repaint from the UNCHANGED cache: re-query the LIVE row
  // (the optimistic flip mutated the node's classList, not the innerHTML string,
  // so a string check wouldn't catch a strand). Without the revert, this is the
  // same stranded node still carrying .done; with it, it's a fresh open row.
  const after = queryFirst(host._queryChildren, '[data-reminder]');
  assert.ok(after && !after.classList.contains('done'), 'the stranded .done is reverted');
  assert.match(document.getElementById('toast').textContent, /Couldn.t save/);
});

test('reminder open (full): tapping the body flips openId and reveals the delete affordance', () => {
  const { tap, host, read } = mountReminders({ surface: 'full',
    lists: [{ id: 'caldav:g', name: 'Groceries' }] });
  assert.doesNotMatch(host.innerHTML, /data-reminder-del=/, 'no delete button until opened');
  tap('[data-reminder-open]');
  assert.equal(read('todoState.openId'), 'caldav:g/1');
  assert.match(host.innerHTML, /data-reminder-del="caldav:g\/1"/, 'delete affordance now shown');
});

test('reminder add (full): submitting #todo-add-form in iCloud mode routes to addReminder, not addTodo', async () => {
  const calls = [];
  const { fireSubmit, registry } = mountReminders({ surface: 'full',
    lists: [{ id: 'caldav:g', name: 'Groceries' }],
    fetch: async (url, opts) => {
      calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
      if (url === '/api/reminders/add') return okResp({ id: 'caldav:g/new', title: 'Buy milk', due: null });
      throw new Error('offline in test');
    } });
  registry['todo-add-input'].value = 'Buy milk';
  fireSubmit({ id: 'todo-add-form' });
  await flush();
  assert.ok(calls.some((c) => c.url === '/api/reminders/add'), 'routed to the reminders endpoint');
  assert.ok(!calls.some((c) => c.url === '/api/todos'), 'the local todo endpoint is never hit');
  assert.deepEqual(calls.find((c) => c.url === '/api/reminders/add').body,
    { list_id: 'caldav:g', title: 'Buy milk' });
});

test('renderTodosFull: fetches the endpoint that matches the active source', async () => {
  const { sandbox } = newHub();
  const urls = [];
  sandbox.fetch = async (url) => {
    urls.push(url);
    return okResp(url === '/api/reminders'
      ? { buckets: { overdue: [], today: [], upcoming: [], no_date: [] }, configured: true, writable: true }
      : { now: [], soon: [], later: [] });
  };
  vm.runInContext("hubData = { todo_source: 'icloud' };", sandbox);
  await sandbox.renderTodosFull();
  assert.ok(urls.includes('/api/reminders') && !urls.includes('/api/todos'), 'iCloud -> /api/reminders');
  urls.length = 0;
  vm.runInContext("hubData = { todo_source: 'local' };", sandbox);
  await sandbox.renderTodosFull();
  assert.ok(urls.includes('/api/todos') && !urls.includes('/api/reminders'), 'local -> /api/todos');
});

test('renderTodosFull: a failed refresh toasts only when data was already loaded (not on first load)', async () => {
  const { document, sandbox } = newHub();
  sandbox.fetch = async () => { throw new Error('down'); };
  vm.runInContext("hubData = { todo_source: 'icloud' }; todoState.reminders = null;", sandbox);
  await sandbox.renderTodosFull();
  assert.equal(document.getElementById('toast'), null, 'no toast on the first, empty load');
  vm.runInContext("todoState.reminders = { buckets: { overdue: [], today: [], upcoming: [], no_date: [] }, configured: true, writable: true };", sandbox);
  await sandbox.renderTodosFull();
  assert.match(document.getElementById('toast').textContent, /Couldn.t refresh/, 'a failed REFRESH surfaces');
});

test('setTodoSource: re-polls, repaints the picker to the new source, and refreshes the open view on the new endpoint', async () => {
  const { document, sandbox } = newHub();
  const host = document.createElement('div'); host._id = 'todo-source-ctl';
  document.body.appendChild(host);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, readonly: false }], todo_source: 'local' }; openView = 'todos';",
    sandbox);
  const urls = [];
  sandbox.fetch = async (url) => {
    urls.push(url);
    if (url === '/api/todo-source') return okResp({ source: 'icloud' });
    if (url === '/api/hub') {
      return okResp({
        date: '2026-08-17', people: [], todos: { now: [], soon: [], later: [] }, todos_ok: true,
        reminders: { overdue: [], today: [], upcoming: [], no_date: [] },
        reminders_writable: true, reminder_lists: [], todo_source: 'icloud',
        calendar: { status: { ok: true }, events: [] }, links: {},
        integrations: [{ id: 'icloud_caldav', enabled: true, readonly: false }],
      });
    }
    if (url === '/api/reminders') return okResp({ buckets: { overdue: [], today: [], upcoming: [], no_date: [] }, configured: true, writable: true });
    throw new Error('offline in test');
  };
  await sandbox.setTodoSource('icloud');
  assert.ok(urls.includes('/api/todo-source'), 'the source was PATCHed');
  assert.ok(urls.includes('/api/reminders'), 'the open view refreshed against the iCloud endpoint');
  assert.match(host.innerHTML, /seg-btn active" type="button" data-todo-source="icloud"/, 'picker repainted to iCloud');
});

test('reminders (full): zero enabled lists hides the add form AND addReminder posts nothing', async () => {
  const { host, sandbox, registry } = mountReminders({ surface: 'full', writable: true, lists: [] });
  assert.doesNotMatch(host.innerHTML, /id="todo-add-form"/, 'no add form without a target list');
  const urls = [];
  sandbox.fetch = async (url) => { urls.push(url); return okResp({}); };
  registry['todo-add-input'].value = 'Orphan';
  await sandbox.addReminder();
  assert.equal(urls.length, 0, 'addReminder early-returns with no POST when there are no lists');
});

test('reminders (full): iCloud-supplied title and list name are escaped, never live markup', () => {
  const { host } = mountReminders({ surface: 'full', writable: true,
    lists: [{ id: 'caldav:g', name: 'Home <script>' }],
    reminders: { overdue: [], today: [{ id: 'caldav:g/x', title: 'A <b>x</b>', due: '2026-08-17' }], upcoming: [], no_date: [] } });
  assert.match(host.innerHTML, /A &lt;b&gt;x&lt;\/b&gt;/, 'title markup is escaped');
  assert.ok(!host.innerHTML.includes('<b>x</b>'), 'no live <b> tag');
  assert.match(host.innerHTML, /Add to Home &lt;script&gt;/, 'list name in the placeholder is escaped');
  assert.ok(!host.innerHTML.includes('<script>'), 'no live <script> tag');
});

test('reminderPriHtml / reminderDueHtml: the "!" is high-only, the due date is upcoming/overdue-only', () => {
  const { sandbox } = newHub();
  vm.runInContext("data_date = '2026-08-17';", sandbox);
  assert.match(sandbox.reminderPriHtml(4), /rem-pri/, 'priority 1-4 marks high');
  assert.equal(sandbox.reminderPriHtml(5), '', 'priority 5 (medium) is unmarked');
  assert.equal(sandbox.reminderPriHtml(null), '', 'no priority is unmarked');
  assert.match(sandbox.reminderDueHtml('upcoming', '2026-09-01'), /rem-due/, 'upcoming shows the date');
  assert.match(sandbox.reminderDueHtml('overdue', '2026-08-10'), /rem-due/, 'overdue shows the date');
  assert.equal(sandbox.reminderDueHtml('today', '2026-08-17'), '', 'today suppresses the redundant date');
  assert.equal(sandbox.reminderDueHtml('no_date', null), '', 'no_date has nothing to show');
});

// links is module-level `let` state in hub.js, set for real by poll() from
// /api/hub's response. A plain `sandbox.links = ...` from out here would only
// add an own property to the sandbox object — it wouldn't touch the separate
// lexical binding hub.js's functions close over (vm.createContext semantics),
// so we reassign it the same way poll() does: by running an assignment
// statement inside the context. initTiles itself is a function *declaration*,
// so unlike links it IS a real sandbox property and can be called directly.
test('initTiles prepends a Cameras header only when cameras exist', () => {
  const { document, sandbox } = newHub();
  vm.runInContext(
    "links = { cameras: [{ src: 'cam1', label: 'Front Porch', " +
    "tile: 'http://example.invalid/t', full: 'http://example.invalid/f' }] };",
    sandbox,
  );

  sandbox.initTiles();
  const html = document.getElementById('tiles').innerHTML;

  assert.ok(html.startsWith('<div class="shead">'),
    'the shared .shead section header is prepended ahead of the camera tiles');
  assert.match(html, /<h2>Cameras<\/h2>/, 'the header names the Cameras section');
  // The Cameras header carries a "Camera page" expand button that opens the
  // 2x2 grid full-screen — the wall's only entry point to the camera page.
  assert.match(html, /class="expand"[^>]*data-overlay="cameras-page"/,
    'the Cameras header has a Camera page expand button');
  assert.match(html, /data-cam="cam1"/);
});

test('initTiles renders no orphan header with zero cameras', () => {
  const { document, sandbox } = newHub();
  vm.runInContext('links = { cameras: [] };', sandbox);

  sandbox.initTiles();

  assert.equal(document.getElementById('tiles').innerHTML, '',
    'no cameras configured means no "Cameras" header with nothing under it');
});

/* ------------------------------------------------- Cameras-tab 2x2 grid */

test('initCamGrid renders the camera_page cameras into #camgrid (config order, no header)', () => {
  const { document, sandbox } = newHub();
  vm.runInContext(
    "links = { camera_page: [" +
    "{ src: 'a', label: 'Drive', tile:'/t/a', full:'/f/a' }," +
    "{ src: 'b', label: 'Mail', tile:'/t/b', full:'/f/b' } ] };", sandbox);

  sandbox.initCamGrid();
  const html = document.getElementById('camgrid').innerHTML;
  // The grid IS the cameras page — no .shead section header, unlike the column.
  assert.ok(!html.includes('class="shead"'), 'grid carries no section header');
  assert.match(html, /data-cam="a"/);
  assert.match(html, /data-cam="b"/);
  // Row-major order preserved so the 2x2 fills as configured (TL, TR, BL, BR).
  assert.ok(html.indexOf('data-cam="a"') < html.indexOf('data-cam="b"'),
    'camera_page order preserved');
});

test('initCamGrid renders more than four cameras (all six, row-major)', () => {
  // The camera page supports any count, not just the historical four. Six
  // cameras must all render in order, so the CSS two-wide grid gets six cells
  // (a 2x3). Guards the render path the grid-auto-rows CSS change depends on.
  const { document, sandbox } = newHub();
  const six = ['a', 'b', 'c', 'd', 'e', 'f'].map((s, i) => (
    { src: s, label: 'C' + i, tile: '/t/' + s, full: '/f/' + s }));
  vm.runInContext('links = { camera_page: ' + JSON.stringify(six) + ' };', sandbox);
  sandbox.initCamGrid();
  const html = document.getElementById('camgrid').innerHTML;
  const cams = [...html.matchAll(/data-cam="([a-f])"/g)].map((m) => m[1]);
  assert.deepEqual(cams, ['a', 'b', 'c', 'd', 'e', 'f'],
    'all six cameras render in config (row-major) order');
});

test('initCamGrid is a no-op until links arrive (does not latch built early)', () => {
  const { document, sandbox } = newHub();
  vm.runInContext('links = {};', sandbox);   // links payload not in yet
  sandbox.initCamGrid();
  assert.equal(document.getElementById('camgrid').innerHTML, '',
    'nothing built before links.camera_page exists');
  vm.runInContext(
    "links = { camera_page: [{ src:'a', label:'A', tile:'/t/a', full:'/f/a' }] };", sandbox);
  sandbox.initCamGrid();
  assert.match(document.getElementById('camgrid').innerHTML, /data-cam="a"/,
    'builds once links arrive');
});

function hubWithGrid(wallCams, gridCams) {
  const { document, sandbox } = newHub();
  vm.runInContext(
    `links = { cameras: ${JSON.stringify(wallCams)}, ` +
    `camera_page: ${JSON.stringify(gridCams)} };`, sandbox);
  sandbox.initTiles();
  sandbox.initCamGrid();
  return { document, sandbox };
}

test('probeCamera dedups by src and drives every tile of a shared camera from one probe', async () => {
  const SHARED = { src: 'drive', label: 'Drive', tile: '/wr/drive', full: '/wr/drive',
                   has_hd: false, hd_src: 'drive' };
  const GRID_ONLY = { src: 'mail', label: 'Mail', tile: '/wr/mail', full: '/wr/mail',
                      has_hd: false, hd_src: 'mail' };
  // 'drive' on the wall column AND the grid; 'mail' only in the grid.
  const { document, sandbox } = hubWithGrid([SHARED], [SHARED, GRID_ONLY]);
  // Capture the probes THIS call issues synchronously (before any await drains
  // the load-time poll().then(probeCamera) microtask), the same isolation the
  // concurrent-probe test above relies on.
  const started = [];
  const resolvers = [];
  sandbox.fetch = (url) => new Promise((res) => { started.push(String(url)); resolvers.push(res); });

  const done = sandbox.probeCamera();

  // The probe set is the UNION of both surfaces, deduped by src: 'drive' is
  // probed once despite two tiles, 'mail' once — no double snapshot.
  const srcs = started.map((u) => u.match(/src=([^&]+)/)[1]).sort();
  assert.deepEqual(srcs, ['drive', 'mail'], 'one probe per camera across both surfaces');

  resolvers.forEach((res) => res({ ok: true }));
  await done;

  // The core new path: a camera that lives ONLY in camera_page must still be
  // probed, or the grid tile would sit permanently offline.
  const mailTiles = document.querySelectorAll('.tile-camera[data-cam="mail"]');
  assert.equal(mailTiles.length, 1, 'mailbox renders only in the grid');
  assert.ok(!mailTiles[0].classList.contains('is-offline'), 'grid-only camera probed live');

  // One probe flips BOTH tiles of the shared src (querySelectorAll, not first-match).
  const driveTiles = document.querySelectorAll('.tile-camera[data-cam="drive"]');
  assert.equal(driveTiles.length, 2, 'shared camera renders on the column AND the grid');
  driveTiles.forEach((t) => {
    assert.ok(!t.classList.contains('is-offline'), 'both tiles of the shared src go live');
    assert.ok(!t.querySelector('.tile-live').classList.contains('hidden'), 'LIVE badge on both');
  });
});

test('a down grid-only camera marks its grid tile offline', async () => {
  const GRID_ONLY = { src: 'mail', label: 'Mail', tile: '/wr/mail', full: '/wr/mail',
                      has_hd: false, hd_src: 'mail' };
  const { document, sandbox } = hubWithGrid([], [GRID_ONLY]);
  sandbox.fetch = async () => { throw new Error('down'); };

  await sandbox.probeCamera();

  const tile = document.querySelector('.tile-camera[data-cam="mail"]');
  assert.ok(tile.classList.contains('is-offline'), 'grid-only camera reflects offline');
  assert.ok(tile.querySelector('.cam-frame').classList.contains('hidden'),
    'frame stays hidden while the camera is down');
});

/* --------------------------------------------------------- camera probes */

const CAM1 = { src: 'cam1', label: 'Drive', tile: '/wr/cam1', full: '/wr/cam1',
               has_hd: false, hd_src: 'cam1' };
const CAM2 = { src: 'cam2', label: 'Yard', tile: '/wr/cam2', full: '/wr/cam2',
               has_hd: false, hd_src: 'cam2' };

function hubWithTiles(cams) {
  const { document, sandbox } = newHub();
  vm.runInContext(`links = { cameras: ${JSON.stringify(cams)} };`, sandbox);
  sandbox.initTiles();
  return { document, sandbox };
}

test('scheduledProbeCamera: skips a second tick while a probe run is still in flight (no stacked requests)', async () => {
  const { sandbox } = hubWithTiles([CAM1]);
  let probeCalls = 0;
  sandbox.fetch = (url) => { probeCalls++; return new Promise(() => {}); };   // hangs forever
  sandbox.scheduledProbeCamera();
  const afterFirst = probeCalls;
  assert.ok(afterFirst >= 1, 'the first scheduled tick issued a probe');
  sandbox.scheduledProbeCamera();   // in-flight -> must NOT start another round
  assert.equal(probeCalls, afterFirst, 'no second round of probes while one is still in flight');
});

test('camera SD probe arms fetchTimeout (J_TIMEOUT_MS), not a bare unbounded fetch', () => {
  // probeOneCamera was changed from a bare fetch to fetchTimeout so a wedged
  // producer can't stack never-resolving sockets (issue #33). Prove the SD probe
  // arms an abort timer at the default J_TIMEOUT_MS (12s); a bare fetch arms none.
  const { sandbox } = hubWithTiles([CAM1]);
  const timers = captureTimers(sandbox);   // capture only the timers THIS probe arms
  sandbox.AbortController = AbortController;
  sandbox.fetch = () => new Promise(() => {});   // hang forever; only the armed timer matters
  sandbox.probeCamera();   // issues probeOneCamera synchronously up to its fetchTimeout await
  assert.ok(timers.some((t) => t.ms === 12000 && !t.done),
    'the SD probe arms a J_TIMEOUT_MS (12000ms) abort timer via fetchTimeout, not a bare fetch');
});

test('probeCamera probes every camera concurrently — one slow camera never delays the rest', async () => {
  const { document, sandbox } = hubWithTiles([CAM1, CAM2]);
  const started = [];
  const resolvers = [];
  sandbox.fetch = (url) => new Promise((res) => { started.push(String(url)); resolvers.push(res); });

  const done = sandbox.probeCamera();

  // BEFORE any probe answers, both probes must already be in flight. The old
  // serial for-await loop issued cam2's fetch only after cam1's resolved, so a
  // slow first camera (cold snapshot: seconds) delayed every tile after it.
  assert.equal(started.length, 2, 'both probes issued before either answers');
  assert.match(started[0], /src=cam1/);
  assert.match(started[1], /src=cam2/);

  resolvers.forEach((res) => res({ ok: true }));
  await done;
  const tile = document.querySelector('.tile-camera[data-cam="cam2"]');
  assert.ok(!tile.classList.contains('is-offline'), 'probe results still land on the tiles');
  assert.ok(!tile.querySelector('.tile-live').classList.contains('hidden'), 'LIVE badge shown');
});

test('a visible tile starts its stream when the probe is ISSUED, not after it answers', () => {
  const { document, sandbox } = hubWithTiles([CAM1]);
  sandbox.fetch = () => new Promise(() => {});   // probe stays in flight forever

  sandbox.probeCamera();

  const tile = document.querySelector('.tile-camera[data-cam="cam1"]');
  const frame = tile.querySelector('.cam-frame');
  // The WebRTC connect and the snapshot probe share the same go2rtc producer;
  // starting them together means the tile paints as soon as the stream delivers
  // a keyframe instead of queueing the connect behind the probe's round-trip.
  assert.equal(frame.src, '/wr/cam1',
    'stream connect overlaps the probe instead of waiting on it');
  assert.ok(frame.classList.contains('hidden'), 'frame stays hidden until the probe confirms live');
  assert.ok(tile.classList.contains('is-offline'), 'tile stays offline-styled until confirmed');
});

test('a hidden tile (mobile, behind its tab) never starts a background stream', async () => {
  const { document, sandbox } = hubWithTiles([CAM1]);
  const tile = document.querySelector('.tile-camera[data-cam="cam1"]');
  tile.offsetParent = null;   // display:none — how the tab bar hides the cams section
  sandbox.fetch = async () => ({ ok: true });

  await sandbox.probeCamera();

  const frame = tile.querySelector('.cam-frame');
  assert.ok(!frame.src, 'no stream into a hidden tile even when the camera is live');
  assert.ok(!frame.classList.contains('hidden'), 'still marked live for when the tab opens');
});

test('offline-to-online recovery reloads the frame for a deterministic fresh connect', async () => {
  const { document, sandbox } = hubWithTiles([CAM1]);
  sandbox.fetch = async () => { throw new Error('down'); };
  await sandbox.probeCamera();   // camera down at first probe — src set, frame hidden

  const tile = document.querySelector('.tile-camera[data-cam="cam1"]');
  const frame = tile.querySelector('.cam-frame');
  assert.equal(frame.src, '/wr/cam1', 'optimistic src survives the failed probe');
  assert.ok(frame.classList.contains('hidden'));

  sandbox.fetch = async () => ({ ok: true });
  frame.src = '/wr/cam1#stale-dead-session';   // sentinel: what the dead connect left behind
  await sandbox.probeCamera();   // camera recovered on a later 30s cycle

  // A player that connected against a dead producer may never have established
  // its session — recovery must NOT trust its self-reconnect. The probe's
  // offline->live transition reassigns src, forcing a fresh connect.
  assert.equal(frame.src, '/wr/cam1', 'src reassigned on recovery — fresh connect, not player internals');
  assert.ok(!frame.classList.contains('hidden'), 'frame revealed once the probe confirms live');
  assert.ok(!tile.classList.contains('is-offline'));

  // ...and the reload is transition-only: the NEXT healthy cycle leaves it alone
  frame.src = '/wr/cam1#sentinel-untouched';
  await sandbox.probeCamera();
  assert.equal(frame.src, '/wr/cam1#sentinel-untouched', 'no churn once recovered');
});

test('a live tile\'s stream is started exactly once — later probe cycles never restart it', async () => {
  const { document, sandbox } = hubWithTiles([CAM1]);
  sandbox.fetch = async () => ({ ok: true });
  await sandbox.probeCamera();

  const frame = document.querySelector('.tile-camera[data-cam="cam1"]').querySelector('.cam-frame');
  assert.equal(frame.src, '/wr/cam1');
  // Reassigning src on every ok probe would visibly restart the live stream
  // every CAM_PROBE_MS — pin that the second cycle leaves the iframe alone.
  frame.src = '/wr/cam1#sentinel-untouched';
  await sandbox.probeCamera();
  assert.equal(frame.src, '/wr/cam1#sentinel-untouched', 'no src churn on a healthy tile');
});

test('a failed probe marks the tile offline and keeps the frame hidden', async () => {
  const { document, sandbox } = hubWithTiles([CAM1]);
  sandbox.fetch = async () => { throw new Error('down'); };

  await sandbox.probeCamera();

  const tile = document.querySelector('.tile-camera[data-cam="cam1"]');
  const frame = tile.querySelector('.cam-frame');
  assert.ok(frame.classList.contains('hidden'), 'no black dead frame — the offline badge shows');
  assert.ok(tile.classList.contains('is-offline'));
  assert.ok(!tile.querySelector('.tile-offline').classList.contains('hidden'), 'offline badge visible');
});

// A camera with a distinct HD twin (Protect-style) vs one without (Wyze-style).
// has_hd is the backend's explicit signal — the tile/full URLs always differ by
// query string, so the frontend must not infer "has HD" from them.
const HD_CAM = { src: 'drive', label: 'Driveway', tile: '/wr/drive',
                 full: '/wr/drive_hd', has_hd: true, hd_src: 'drive_hd' };
const NO_HD_CAM = { src: 'yard', label: 'Yard', tile: '/wr/yard',
                    full: '/wr/yard', has_hd: false, hd_src: 'yard' };
// A few more than hub.js's CAM_HD_TRIES (12), so the give-up path is reached.
const CAM_HD_TRIES_FOR_TEST = 14;

// Reassign the sandbox's setTimeout to CAPTURE callbacks (the default stub is a
// no-op), so tests can drive the HD-upgrade timers deterministically.
function captureTimers(sandbox) {
  const timers = [];
  sandbox.setTimeout = (fn, ms) => { timers.push({ fn, ms, done: false }); return timers.length; };
  return timers;
}
const nextTimer = (timers, ms) => timers.find((t) => t.ms === ms && !t.done);

test('camera full-screen shows the warm stream first, HD twin stacked in front (transparent)', () => {
  const { document, sandbox } = newHub();
  captureTimers(sandbox);
  vm.runInContext(`links = { cameras: [${JSON.stringify(HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:drive');

  // No cold 4K black wait: the already-warm tile stream is the visible base, and
  // the HD twin is stacked in front, transparent, NOT yet revealed.
  const content = document.getElementById('overlay-content');
  assert.equal(content.children.length, 2, 'warm base stream + HD upgrade layer');
  assert.equal(content.children[0].src, '/wr/drive', 'the warm tile stream shows first (instant)');
  assert.equal(content.children[1].src, '/wr/drive_hd', 'the HD twin loads in front');
  assert.ok(content.children[1].classList.contains('cam-hd-upgrade'), 'HD is the upgrade layer');
  assert.ok(!content.children[1].classList.contains('ready'),
    'HD is NOT revealed until its stream is probed live');
});

test('openOverlay("cameras-page") renders the 2x2 grid and probes the streams after open', () => {
  const { document, sandbox } = newHub();
  captureTimers(sandbox);
  const started = [];   // probe fetches issued synchronously by this open
  sandbox.fetch = (url) => { started.push(String(url)); return new Promise(() => {}); };
  vm.runInContext(
    "links = { camera_page: [" +
    "{ src:'a', label:'Drive', tile:'/wr/a', full:'/wr/a', has_hd:false, hd_src:'a' }," +
    "{ src:'b', label:'Mail', tile:'/wr/b', full:'/wr/b', has_hd:false, hd_src:'b' } ] };", sandbox);

  sandbox.openOverlay('cameras-page');

  const content = document.getElementById('overlay-content');
  assert.match(content.innerHTML, /class="camera-page"/, 'renders the full-screen grid container');
  assert.match(content.innerHTML, /data-cam="a"/);
  assert.match(content.innerHTML, /data-cam="b"/);
  // each grid tile still opens that single camera full-screen when tapped
  assert.match(content.innerHTML, /data-overlay="camera:a"/);
  assert.ok(document.getElementById('overlay').classList.contains('open'), 'overlay opened');
  // The streams must be probed once the overlay is open — deleting the probe
  // call would leave every grid tile permanently offline in production.
  const srcs = started.map((u) => u.match(/src=([^&]+)/)[1]).sort();
  assert.deepEqual(srcs, ['a', 'b'], 'probeCamera fired for the grid after open');
});

test('opening an overlay locks page scroll (body.overlay-open); closing unlocks it', () => {
  const { document, sandbox } = newHub();
  captureTimers(sandbox);
  sandbox.fetch = () => new Promise(() => {});   // probe hangs; irrelevant here
  vm.runInContext(
    "links = { camera_page: [{ src:'a', label:'A', tile:'/t', full:'/f' }] };", sandbox);

  sandbox.openOverlay('cameras-page');
  assert.ok(document.body.classList.contains('overlay-open'),
    'page scroll is locked while the overlay is open (no wall scrollbar behind it)');

  sandbox.scrollCalls.length = 0;
  document.scrollingElement.scrollTop = 700;
  sandbox.closeOverlay();
  assert.ok(!document.body.classList.contains('overlay-open'),
    'page scroll is restored when the overlay closes');
  // coming home from an overlay also lands at the top of the page
  assert.deepEqual(sandbox.scrollCalls.at(-1), [0, 0], 'closeOverlay scrolls to top');
  assert.equal(document.scrollingElement.scrollTop, 0, 'closeOverlay resets the scroller');
});

test('idle auto-return closes every modal, not just the overlay (a stranded editor must not block the deploy reload)', () => {
  const { document, sandbox } = newHub();
  const timers = captureTimers(sandbox);

  sandbox.openOverlay('chores');   // arms the idle timer for the 'chores' view

  // Simulate an editor / delete-confirm / event-detail modal left open over
  // the overlay: each is a fixed sibling of #overlay, reachable only while
  // an overlay is open (openChoreEditor/openDeleteConfirm/openEventDetail all
  // gate their own armIdle() on `openView`).
  document.getElementById('chore-modal').classList.remove('hidden');
  document.getElementById('confirm-modal').classList.remove('hidden');
  document.getElementById('ev-modal').classList.remove('hidden');

  const idleMs = sandbox.idleReturnMs('chores');
  const idle = timers.find((t) => t.ms === idleMs && !t.done);
  assert.ok(idle, 'idle timer armed while the overlay is open');
  idle.done = true;
  idle.fn();   // idle fires

  assert.ok(!document.getElementById('overlay').classList.contains('open'), 'overlay closes');
  assert.ok(document.getElementById('chore-modal').classList.contains('hidden'), 'chore editor closes too');
  assert.ok(document.getElementById('confirm-modal').classList.contains('hidden'), 'delete confirm closes too');
  assert.ok(document.getElementById('ev-modal').classList.contains('hidden'), 'event detail closes too');
  assert.equal(vm.runInContext('openView', sandbox), null, 'openView cleared');
  assert.ok(!sandbox.wallBusy(),
    'wallBusy() no longer sees a stranded modal, so the deploy auto-reload can proceed');
});

test('openOverlay("cameras-page") with an empty camera_page shows a note, not a black grid', () => {
  const { document, sandbox } = newHub();
  captureTimers(sandbox);
  const started = [];
  sandbox.fetch = (url) => { started.push(String(url)); return new Promise(() => {}); };
  // Wall has a camera (so the button shows) but the grid list is empty.
  vm.runInContext(
    "links = { cameras: [{ src:'x', label:'X', tile:'/t', full:'/f' }], camera_page: [] };", sandbox);

  sandbox.openOverlay('cameras-page');

  const content = document.getElementById('overlay-content');
  assert.match(content.innerHTML, /camera-page-empty/, 'shows an explicit empty-state note');
  assert.ok(!content.innerHTML.includes('data-cam='), 'no camera tiles rendered');
  assert.ok(document.getElementById('overlay').classList.contains('open'), 'overlay still opens');
  assert.equal(started.length, 0, 'no probe fetches for an empty grid');
});

test('camera full-screen with no HD twin shows a single (already warm) stream, no upgrade', () => {
  const { document, sandbox } = newHub();
  captureTimers(sandbox);
  vm.runInContext(`links = { cameras: [${JSON.stringify(NO_HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:yard');

  const content = document.getElementById('overlay-content');
  assert.equal(content.children.length, 1, 'no distinct HD — one stream, no upgrade layer');
  assert.equal(content.children[0].src, '/wr/yard');
});

test('camera HD upgrade reveals only after the HD stream is probed live, then drops the base', async () => {
  const { document, sandbox } = newHub();
  const timers = captureTimers(sandbox);
  let hdProbes = 0;
  sandbox.fetch = async (url) => {
    if (String(url).includes('src=drive_hd')) hdProbes += 1;
    return { ok: true };   // HD snapshot answers -> stream is live
  };
  vm.runInContext(`links = { cameras: [${JSON.stringify(HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:drive');
  const content = document.getElementById('overlay-content');
  const hd = content.children[1];

  // fire the readiness poll — the FIRST check is scheduled immediately (0ms),
  // not a full CAM_HD_POLL_MS out: with a warm producer the HD answers fast,
  // and the old built-in 700ms head start added most of a second to every open
  await nextTimer(timers, 0).fn();
  assert.equal(hdProbes, 1, 'the HD stream (hd_src) was probed for readiness');
  assert.ok(hd.classList.contains('ready'), 'HD revealed once its snapshot answered');
  assert.equal(content.children.length, 2, 'the warm base is still up during the cross-fade');

  // the base is dropped only after the fade completes
  nextTimer(timers, 450).fn();
  assert.equal(content.children.length, 1, 'base removed after the HD faded in');
  assert.equal(content.children[0], hd, 'the HD stream is the one that remains');
});

test('a stale HD-upgrade probe is a no-op if the view changes while the probe is in flight', async () => {
  const { document, sandbox } = newHub();
  const timers = captureTimers(sandbox);
  // The user navigates away DURING the probe — the guard that matters is the
  // re-check AFTER the await, so simulate the switch inside the fetch itself.
  sandbox.fetch = async () => { vm.runInContext(`openView = 'camera:other';`, sandbox); return { ok: true }; };
  vm.runInContext(`links = { cameras: [${JSON.stringify(HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:drive');
  const hd = document.getElementById('overlay-content').children[1];

  await nextTimer(timers, 0).fn();   // probe runs; the view flips mid-await

  assert.ok(!hd.classList.contains('ready'),
    'a probe that resolves after the view changed must not reveal HD over the new view');
});

/* ---------------------------------------------------------- weather card */

// Mount a native weather slot and paint it. renderWeather writes into
// #weather-slot (built by buildPanels via innerHTML); the fake parser doesn't
// register parsed nodes by id, so pre-register a real host and paint into it —
// same trick the chores-full mount uses. Returns the slot's rendered markup.
function renderWeatherHtml(payload) {
  const { document, sandbox } = newHub();
  const slot = document.createElement('div');
  slot._id = 'weather-slot';
  document.body.appendChild(slot);   // appendChild registers it by id
  sandbox.renderWeather(payload);
  return { html: slot.innerHTML, document, sandbox };
}

const WX_GOOD = {
  available: true, temp: 74.8, unit: 'F', conditions: 'Clear & sunny', feels: 78,
  low: 59, high: 81, uv: 3, uv_desc: 'Low', aqi: 42, aqi_cat: 'Good',
  humidity: 57, dew_point: 58.5, spark: [70, 71, 73, 75, 78, 80, 79, 77], spark_now: 4, stale: false,
};
const WX_WARN = {
  available: true, temp: 90, unit: 'F', conditions: 'Hazy', feels: 98,
  low: 75, high: 95, uv: 9, uv_desc: 'Very High', aqi: 151, aqi_cat: 'Unhealthy',
  humidity: 70, dew_point: 74, spark: [], stale: true,
};

test('fetchWeather: keeps the last good card through transient failures, gives up after the limit', async () => {
  const { document, sandbox } = newHub();
  await flush();   // let the load-time fetchWeather() (offline stub) settle first
  const slot = document.createElement('div');
  slot._id = 'weather-slot';
  document.body.appendChild(slot);
  // a good reading paints the card and resets the failure counter
  sandbox.fetch = async () => ({ ok: true, status: 200, json: async () => WX_GOOD });
  await sandbox.fetchWeather();
  assert.match(slot.innerHTML, /class="temp num">74</, 'good card first');
  // the feed now blips: single failures must NOT blank the card
  sandbox.fetch = async () => { throw new Error('down'); };
  await sandbox.fetchWeather();   // fail 1
  assert.match(slot.innerHTML, /class="temp num">74</, 'last-good retained after 1 fail');
  assert.doesNotMatch(slot.innerHTML, /unavailable/i);
  await sandbox.fetchWeather();   // fail 2
  assert.match(slot.innerHTML, /class="temp num">74</, 'last-good retained after 2 fails');
  // only after TILE_FAIL_LIMIT (3) consecutive misses does it fall back
  await sandbox.fetchWeather();   // fail 3
  assert.match(slot.innerHTML, /unavailable/i, 'gives up after the failure limit');
});

test('weather card renders temp, condition/feels, UV, AQI, humidity and dew', () => {
  const { html } = renderWeatherHtml(WX_GOOD);
  // header carries the almanac overlay hook (the "Full forecast" button)
  assert.match(html, /class="expand"[^>]*data-overlay="panel:weather"/);
  assert.match(html, /⛶ Full forecast/);
  // big temp split into whole + ".<frac>°<unit>" (74 | .8°F)
  assert.match(html, /class="temp num">74</);
  assert.match(html, /class="deg num">\.8°F</);
  // condition + feels line; the "&" in the conditions string is escaped
  assert.match(html, /Clear &amp; sunny · feels 78°/);
  // stats
  assert.match(html, /High<\/div><div class="v num">81°/);
  assert.match(html, /Low<\/div><div class="v num">59°/);
  assert.match(html, /UV Index<\/div><div class="v num">3 /);
  assert.match(html, /Air Quality<\/div><div class="v num">42 /);
  assert.match(html, /Humidity<\/div><div class="v num">57%/);
  assert.match(html, /Dew point<\/div><div class="v num">58.5°/);
});

test('wxTempParts renders exactly one degree, even when the unit already carries one (no °°F)', () => {
  const { sandbox } = newHub();
  // the live feed sends the unit WITH the degree ("°F") -> must not double it
  const a = sandbox.wxTempParts(74.8, '°F');
  assert.equal(a.whole + a.deg, '74.8°F', 'no doubled degree when unit is "°F"');
  assert.ok(!a.deg.includes('°°'));
  // a unit without a degree still gets exactly one
  const b = sandbox.wxTempParts(61, 'F');
  assert.equal(b.whole + b.deg, '61.0°F');
  // a bare-degree unit ("°") stays a single degree (strip then re-add)
  assert.equal(sandbox.wxTempParts(61, '°').deg, '.0°');
  // non-finite fallback also stays single-degree
  const c = sandbox.wxTempParts(NaN, '°F');
  assert.ok(!c.deg.includes('°°'), 'no doubled degree in the non-finite fallback');
  assert.equal(c.whole, '--');
});

test('the weather card temp shows a single degree with the real feed unit "°F"', () => {
  const { html } = renderWeatherHtml({ ...WX_GOOD, temp: 72.9, unit: '°F' });
  assert.ok(!html.includes('°°'), 'no doubled degree in the rendered card');
  assert.match(html, /class="deg num">\.9°F</);
});

test('weather card escapes string fields (conditions markup is inert)', () => {
  const { html } = renderWeatherHtml({ ...WX_GOOD, conditions: 'Sun <script>x</script>', spark: [] });
  assert.ok(!html.includes('<script>'), 'a markup-bearing conditions string is not live HTML');
  assert.match(html, /Sun &lt;script&gt;x&lt;\/script&gt;/, 'conditions rendered escaped');
});

test('weather card AQI + UV chips pick .good when air is good / UV is low', () => {
  const { html } = renderWeatherHtml(WX_GOOD);
  // AQI "Good" category -> .q.good; UV "Low"/3 -> .q.good
  assert.match(html, /<span class="q good">Good<\/span>/);
  assert.match(html, /<span class="q good">Low<\/span>/);
});

test('weather card AQI + UV chips pick .warn by category / high UV', () => {
  const { html } = renderWeatherHtml(WX_WARN);
  // AQI "Unhealthy" (aqi 151) -> .q.warn; UV "Very High" (uv 9) -> .q.warn
  assert.match(html, /<span class="q warn">Unhealthy<\/span>/);
  assert.match(html, /<span class="q warn">Very High<\/span>/);
});

test('weather card draws the temp chart with an observed/forecast split and a "now" marker', () => {
  const { html } = renderWeatherHtml(WX_GOOD);   // 8 pts, now at index 4
  assert.ok(html.includes('<svg class="spark"'), 'the temp chart svg is present');
  assert.match(html, /viewBox="0 0 300 46"/, 'normalized to the 0 0 300 46 viewBox');
  assert.match(html, /<path d="M[^"]*Z" fill="url\(#sg\)"/, 'gradient area under the curve');
  // the observed-past line is solid (full opacity, no stroke-opacity attr) —
  // it's the chart's primary content, so pin it against accidental removal
  assert.match(html, /fill="none" stroke="var\(--accent\)" stroke-width="2"\/>/,
    'solid observed-past line');
  // the forecast-ahead segment is drawn fainter than the observed past
  assert.match(html, /fill="none" stroke="var\(--accent\)" stroke-width="2" stroke-opacity="\.38"/,
    'faded forecast segment');
  // now marker: a dot + a faint vertical guide at the current hour (i=4 of 8 -> x=171.4)
  assert.match(html, /<circle cx="171\.4"[^>]*fill="var\(--accent\)"/, 'dot on the current hour');
  assert.match(html, /<line x1="171\.4"[^>]*stroke-opacity="\.2"/, 'faint "now" guide line');
});

test('temp chart falls back to a last-point dot, all solid, when no now index is given', () => {
  const { html } = renderWeatherHtml({ ...WX_GOOD, spark_now: undefined });
  assert.match(html, /<circle cx="300"[^>]*fill="var\(--accent\)"/, 'dot on the last point');
  assert.ok(!html.includes('stroke-opacity=".38"'), 'no separate forecast segment when now is unknown');
});

test('temp chart with an out-of-range integer now index falls back to the last point (no throw)', () => {
  let out;
  assert.doesNotThrow(() => { out = renderWeatherHtml({ ...WX_GOOD, spark_now: 99 }); });
  assert.match(out.html, /<circle cx="300"[^>]*fill="var\(--accent\)"/, 'dot on the last point');
  assert.ok(!out.html.includes('stroke-opacity=".38"'), 'no forecast segment when now is out of range');
});

test('temp chart with now at index 0 renders an all-forecast curve with the dot at the start', () => {
  const { html } = renderWeatherHtml({ ...WX_GOOD, spark_now: 0 });   // 8 pts, now at index 0
  assert.match(html, /fill="none" stroke="var\(--accent\)" stroke-width="2" stroke-opacity="\.38"/,
    'the whole curve is drawn as forecast');
  assert.match(html, /<circle cx="0"[^>]*fill="var\(--accent\)"/, 'dot at the first point (x=0)');
});

test('weather card HIDES the sparkline when spark is empty (fewer than 2 points)', () => {
  const { html } = renderWeatherHtml(WX_WARN);   // spark: []
  assert.ok(!html.includes('<svg class="spark"'), 'no sparkline with an empty series');
  // the rest of the card still renders
  assert.match(html, /class="temp num">90</);
  assert.match(html, /Humidity<\/div><div class="v num">70%/);
});

test('weather card shows a quiet stale mark when stale is truthy', () => {
  assert.match(renderWeatherHtml(WX_WARN).html, /class="wx-stale">stale</);
  assert.ok(!renderWeatherHtml(WX_GOOD).html.includes('wx-stale'), 'no stale mark when fresh');
});

test('weather card: available:false renders no card and does NOT throw', () => {
  let out;
  assert.doesNotThrow(() => { out = renderWeatherHtml({ available: false }); });
  assert.ok(!out.html.includes('class="card wx"'), 'no weather card when unavailable');
  assert.ok(!out.html.includes('<svg class="spark"'), 'no sparkline when unavailable');
  // the column is never blanked: the header (with the almanac hook) still stands
  assert.match(out.html, /data-overlay="panel:weather"/);
  assert.match(out.html, /wx-offline/, 'a slim offline note stands in for the card');
});

test('buildPanels renders native slots for weather + climate, an iframe embed for others', () => {
  const { document, sandbox } = newHub();
  vm.runInContext(
    "links = { panels: ["
    + " { id: 'weather', label: 'Weather', url: 'http://wx.invalid/', vw: 1024, vh: 600, full: 'fit' },"
    + " { id: 'climate', label: 'House Climate', url: 'http://cl.invalid/', vw: 1024, vh: 600, full: 'fit' },"
    + " { id: 'almanac', label: 'Almanac', url: 'http://al.invalid/', vw: 1024, vh: 600 } ] };",
    sandbox);
  sandbox.buildPanels();
  const html = document.getElementById('panels').innerHTML;
  // weather + climate are native card slots, NOT always-on iframe embeds
  assert.match(html, /id="weather-slot"/);
  assert.match(html, /id="climate-slot"/);
  assert.ok(!html.includes('id="frame-weather"'), 'the weather iframe embed is gone');
  assert.ok(!html.includes('id="frame-climate"'), 'the climate iframe embed is gone');
  // any other panel keeps its iframe embed
  assert.match(html, /id="frame-almanac"/);
});

/* ---------------------------------------------------------- climate card */

// Mount a native climate slot and paint it — same pre-register trick the weather
// mount uses (the fake parser doesn't register innerHTML nodes by id).
function renderClimateHtml(payload) {
  const { document, sandbox } = newHub();
  const slot = document.createElement('div');
  slot._id = 'climate-slot';
  document.body.appendChild(slot);   // appendChild registers it by id
  sandbox.renderClimate(payload);
  return { html: slot.innerHTML, document, sandbox };
}

// Generic room names; "Outside" is included (the backend passes it through) and
// MUST be filtered out by the frontend by NAME. Garage is hot (>=80) -> warn;
// Attic is stale -> warn.
const CLIMATE = {
  available: true,
  rooms: [
    { name: 'Living Room', channel: 'ch1', temp_f: 72, humidity: 45, stale: false },
    { name: 'Bedroom', channel: 'ch2', temp_f: 74.4, humidity: 50, stale: false },
    { name: 'Garage', channel: 'ch3', temp_f: 82, humidity: 55, stale: false },
    { name: 'Attic', channel: 'ch4', temp_f: 70, humidity: 60, stale: true },
    { name: 'Outside', channel: 'ch0', temp_f: 91, humidity: 30, stale: false },
  ],
  indoor_rh: 48, indoor_dp: 55,
};

test('fetchClimate: keeps the last good rooms through transient failures, gives up after the limit', async () => {
  const { document, sandbox } = newHub();
  await flush();
  const slot = document.createElement('div');
  slot._id = 'climate-slot';
  document.body.appendChild(slot);
  sandbox.fetch = async () => okResp(CLIMATE);
  await sandbox.fetchClimate();
  assert.match(slot.innerHTML, /Living Room/, 'good rooms first');
  sandbox.fetch = async () => { throw new Error('down'); };
  await sandbox.fetchClimate();   // fail 1
  assert.match(slot.innerHTML, /Living Room/, 'last-good retained after 1 fail');
  await sandbox.fetchClimate();   // fail 2
  assert.match(slot.innerHTML, /Living Room/, 'last-good retained after 2 fails');
  await sandbox.fetchClimate();   // fail 3
  assert.match(slot.innerHTML, /unavailable/i, 'gives up after TILE_FAIL_LIMIT');
});

test('climate card renders one .room per INDOOR room with temp + humidity', () => {
  const { html } = renderClimateHtml(CLIMATE);
  // header carries the full-climate overlay hook (the "Full climate" button)
  assert.match(html, /class="expand"[^>]*data-overlay="panel:climate"/);
  assert.match(html, /⛶ Full climate/);
  // the units header (blank key col + Temp + Humidity)
  assert.match(html, /<span class="u">Temp<\/span><span class="u">Humidity<\/span>/);
  // one row per indoor room (Living Room, Bedroom, Garage, Attic) — NOT Outside
  assert.equal((html.match(/<div class="room/g) || []).length, 4);
  // temp rounded to whole degrees, humidity as a percent
  assert.match(html, /Living Room<\/span><span class="rv num">72°<\/span><span class="rh num">45%<\/span>/);
  assert.match(html, /Bedroom<\/span><span class="rv num">74°<\/span><span class="rh num">50%<\/span>/);
});

test('climate card FILTERS OUT the Outside sensor (its temp lives in Weather)', () => {
  const { html } = renderClimateHtml(CLIMATE);
  assert.ok(!html.includes('Outside'), 'the outdoor sensor never appears in the room list');
  assert.ok(!html.includes('91°'), 'and neither does its outdoor temperature');
});

test('climate card KEEPS a real room whose CHANNEL is outdoor (only NAME filters)', () => {
  // Regression: the live house-climate feed labels a legitimate indoor room (a
  // crawl space) with an "outdoor" channel. It MUST still appear — channel is
  // not a reliable indoor/outdoor signal. Only a room NAMED outside/outdoor is
  // the redundant outdoor-air sensor (its temp lives in Weather) and is filtered.
  const payload = { available: true, rooms: [
    { name: 'Living Room', channel: 'ch1', temp_f: 72, humidity: 45, stale: false },
    { name: 'Crawl Space', channel: 'outdoor', temp_f: 65, humidity: 78, stale: false },
    { name: 'Outdoor', channel: 'ch0', temp_f: 91, humidity: 30, stale: false },
  ] };
  const { html } = renderClimateHtml(payload);
  assert.equal((html.match(/<div class="room/g) || []).length, 2);
  assert.ok(html.includes('Crawl Space'), 'a real room on an outdoor channel is kept');
  assert.ok(!html.includes('Outdoor'), 'a room NAMED outdoor is filtered');
  assert.ok(!html.includes('91°'), 'and its redundant outdoor temperature is gone');
});

test('climate card marks a HOT room warn (temp_f >= HOT_F)', () => {
  const { html } = renderClimateHtml(CLIMATE);
  assert.match(html, /<div class="room warn"><span class="rk">Garage<\/span>/);
  // a comfortable room is NOT warned
  assert.match(html, /<div class="room"><span class="rk">Living Room<\/span>/);
});

test('climate card marks a STALE room warn (regardless of temperature)', () => {
  const { html } = renderClimateHtml(CLIMATE);
  // Attic is only 70° but its sensor is stale -> warn
  assert.match(html, /<div class="room warn"><span class="rk">Attic<\/span>/);
});

test('climate card handles missing temp/humidity gracefully (-- / —), never warns on non-finite temp', () => {
  const payload = { available: true, rooms: [
    { name: 'Nursery', channel: 'ch9', temp_f: null, humidity: null, stale: false },
  ] };
  const { html } = renderClimateHtml(payload);
  assert.match(html, /Nursery<\/span><span class="rv num">--<\/span><span class="rh num">—<\/span>/);
  assert.ok(!html.includes('room warn'), 'a missing (non-finite) temp never triggers the hot warn');
});

test('climate card escapes room names (markup is inert)', () => {
  const payload = { available: true, rooms: [
    { name: 'Den <script>x</script>', channel: 'ch1', temp_f: 72, humidity: 45, stale: false },
  ] };
  const { html } = renderClimateHtml(payload);
  assert.ok(!html.includes('<script>'), 'a markup-bearing room name is not live HTML');
  assert.match(html, /Den &lt;script&gt;x&lt;\/script&gt;/);
});

test('climate card: available:false renders no card and does NOT throw', () => {
  let out;
  assert.doesNotThrow(() => { out = renderClimateHtml({ available: false }); });
  assert.ok(!out.html.includes('class="card rooms"'), 'no climate card when unavailable');
  // the column is never blanked: the header (with the full-climate hook) still stands
  assert.match(out.html, /data-overlay="panel:climate"/);
  assert.match(out.html, /wx-offline/, 'a slim offline note stands in for the card');
});

test('climate card shows the indoor RH + dew aggregate footer (labeled, fail-soft)', () => {
  const { html } = renderClimateHtml(CLIMATE);   // indoor_rh 48, indoor_dp 55
  assert.match(html, /class="rfoot"/, 'the indoor aggregate footer renders');
  assert.match(html, /class="rk">Indoor<\/span>/);
  assert.match(html, /48%<\/b> RH/);
  assert.match(html, /55°<\/b> dew/);
  // fail-soft: no indoor_rh/indoor_dp -> no footer
  const noAgg = renderClimateHtml({ available: true, rooms: [
    { name: 'Bedroom', channel: 'ch1', temp_f: 70, humidity: 50, stale: false }] });
  assert.ok(!noAgg.html.includes('class="rfoot"'), 'no footer when indoor RH/dew are absent');

  // partial: RH present, dew absent -> footer shows ONLY RH (each value guarded
  // independently, so a collapsed `isFinite(rh) && isFinite(dp)` would regress)
  const rhOnly = renderClimateHtml({ available: true, indoor_rh: 48, indoor_dp: null,
    rooms: [{ name: 'Bedroom', channel: 'ch1', temp_f: 70, humidity: 50, stale: false }] });
  assert.match(rhOnly.html, /class="rfoot"/, 'RH-only still renders the footer');
  assert.match(rhOnly.html, /48%<\/b> RH/);
  assert.ok(!rhOnly.html.includes('dew'), 'no dew segment when indoor_dp is absent');

  // partial: dew present, RH absent -> footer shows ONLY dew
  const dpOnly = renderClimateHtml({ available: true, indoor_rh: null, indoor_dp: 55,
    rooms: [{ name: 'Bedroom', channel: 'ch1', temp_f: 70, humidity: 50, stale: false }] });
  assert.match(dpOnly.html, /class="rfoot"/, 'dew-only still renders the footer');
  assert.match(dpOnly.html, /55°<\/b> dew/);
  assert.ok(!dpOnly.html.includes(' RH'), 'no RH segment when indoor_rh is absent');
});

test('the streak chip shows the fire count with an honest tooltip (no misleading "days in a row" claim)', () => {
  const { sandbox } = newHub();
  const html = sandbox.personCardHtml(
    { person: { id: 'p1', name: 'Ava', color: '#3E9BE8' }, streak: 3, chores: [], week: [] });
  // bare "🔥 3" with no visible sub-label
  assert.match(html, /class="chip-streak"[^>]*>🔥 3<\/span>/);
  assert.ok(!html.includes('chip-streak-lbl') && !html.includes('day streak'),
    'no "day streak" sub-label (it overstated non-daily chores)');
  // the tooltip is honest: rest days neither count nor break the streak
  assert.match(html, /title="3 chore days finished in a row \(a day with no chores neither counts nor breaks it\)"/);
  // exactly at the threshold (=== 2): the chip renders (guards >= vs >)
  const two = sandbox.personCardHtml(
    { person: { id: 'p3', name: 'Kai', color: '#7A5AF8' }, streak: 2, chores: [], week: [] });
  assert.match(two, /class="chip-streak"[^>]*>🔥 2<\/span>/,
    'a 2-day streak (the boundary) still shows the chip');
  // below the threshold (<2): no chip at all
  const none = sandbox.personCardHtml(
    { person: { id: 'p2', name: 'Milo', color: '#E39A2A' }, streak: 1, chores: [], week: [] });
  assert.ok(!none.includes('chip-streak'), 'no streak chip below the threshold');
});

test('the wall auto-reloads on a build-token change, but not mid-interaction, and never loops', async () => {
  const { document, sandbox } = newHub();
  // seeded modals -> hidden (production initial state) so the wall reads idle
  ['ev-modal', 'chore-modal', 'confirm-modal'].forEach((id) => {
    const el = document.getElementById(id); if (el) el.classList.add('hidden');
  });
  let reloads = 0;
  sandbox.location = { reload: () => { reloads++; }, host: 'hub.example:8138', protocol: 'http:' };
  let payload = { date: '2026-08-15', people: [], todos: {},
    calendar: { status: 'ok', events: [] }, links: {}, build: 'aaa' };
  sandbox.fetch = async (url) =>
    url === '/api/hub' ? okResp(payload) : { ok: true, status: 200, json: async () => ({}) };

  await sandbox.poll();
  assert.equal(reloads, 0, 'first poll records the build, no reload');
  await sandbox.poll();
  assert.equal(reloads, 0, 'an UNCHANGED build never reloads (no reload loop)');

  payload = { ...payload, build: 'bbb' };
  await sandbox.poll();
  assert.equal(reloads, 1, 'a changed build reloads the idle wall');

  // mid-interaction (overlay open) -> a further change does NOT yank it away
  let ov = document.getElementById('overlay');
  if (!ov) { ov = document.createElement('div'); ov._id = 'overlay'; document.body.appendChild(ov); }
  ov.classList.add('open');
  payload = { ...payload, build: 'ccc' };
  await sandbox.poll();
  assert.equal(reloads, 1, 'a build change does not reload the wall mid-interaction');
});

test('auto-reload defers for EVERY wallBusy condition (each modal + theme-pop), not just the overlay', async () => {
  const conditions = [
    { name: 'ev-modal shown', setup: (doc) => doc.getElementById('ev-modal').classList.remove('hidden') },
    { name: 'chore-modal shown', setup: (doc) => doc.getElementById('chore-modal').classList.remove('hidden') },
    { name: 'confirm-modal shown', setup: (doc) => doc.getElementById('confirm-modal').classList.remove('hidden') },
    { name: 'theme-pop open', setup: (doc) => {
        let p = doc.getElementById('theme-pop');
        if (!p) { p = doc.createElement('div'); p._id = 'theme-pop'; doc.body.appendChild(p); }
        p.classList.add('open');
      } },
  ];
  for (const { name, setup } of conditions) {
    const { document, sandbox } = newHub();
    // idle baseline: hide the three seeded modals and clear any recent interaction
    ['ev-modal', 'chore-modal', 'confirm-modal'].forEach((id) => document.getElementById(id).classList.add('hidden'));
    sandbox.noteInteraction(0);
    let reloads = 0;
    sandbox.location = { reload: () => { reloads++; }, host: 'hub.example:8138', protocol: 'http:' };
    let payload = { date: '2026-08-15', people: [], todos: {},
      calendar: { status: 'ok', events: [] }, links: {}, build: 'aaa' };
    sandbox.fetch = async (url) => url === '/api/hub' ? okResp(payload) : okResp({});
    await sandbox.poll();                     // seed the build token while idle
    setup(document);                          // now busy in exactly one way
    payload = { ...payload, build: 'bbb' };
    await sandbox.poll();
    assert.equal(reloads, 0, `a build change must not reload while busy: ${name}`);
  }
});

test('auto-reload defers briefly after a direct tap (bare-wall chore taps open no overlay), then fires once idle', async () => {
  const { document, sandbox } = newHub();
  ['ev-modal', 'chore-modal', 'confirm-modal'].forEach((id) => document.getElementById(id).classList.add('hidden'));
  let reloads = 0;
  sandbox.location = { reload: () => { reloads++; }, host: 'hub.example:8138', protocol: 'http:' };
  let payload = { date: '2026-08-15', people: [], todos: {},
    calendar: { status: 'ok', events: [] }, links: {}, build: 'aaa' };
  sandbox.fetch = async (url) => url === '/api/hub' ? okResp(payload) : okResp({});
  await sandbox.poll();                        // seed build 'aaa'
  sandbox.noteInteraction();                   // a tap JUST happened, no overlay open
  payload = { ...payload, build: 'bbb' };
  await sandbox.poll();
  assert.equal(reloads, 0, 'a deploy does not reload within the quiet window after a tap');
  sandbox.noteInteraction(Date.now() - 60000); // last tap was a minute ago -> wall is idle
  await sandbox.poll();
  assert.equal(reloads, 1, 'once past the interaction quiet window, the deferred reload fires');
});

test('auto-reload stays dormant when the payload carries no build token (no reload, no throw)', async () => {
  const { document, sandbox } = newHub();
  ['ev-modal', 'chore-modal', 'confirm-modal'].forEach((id) => document.getElementById(id).classList.add('hidden'));
  sandbox.noteInteraction(0);
  let reloads = 0;
  sandbox.location = { reload: () => { reloads++; }, host: 'hub.example:8138', protocol: 'http:' };
  // NOTE: no `build` key at all
  const payload = { date: '2026-08-15', people: [], todos: {},
    calendar: { status: 'ok', events: [] }, links: {} };
  sandbox.fetch = async (url) => url === '/api/hub' ? okResp(payload) : okResp({});
  await sandbox.poll();
  await sandbox.poll();
  assert.equal(reloads, 0, 'a payload with no build token never triggers a reload');
});

test('native cards warn only on genuine misconfig, not the boot race (SF4)', () => {
  // The warn lives in renderWeather/renderClimate's slot-absent branch;
  // #weather-slot / #climate-slot are NOT seeded, so a fresh newHub() exercises
  // the real path. Give the sandbox a console shim so the warn is observable
  // (vm.createContext injects no console).
  const { sandbox } = newHub();
  const warns = [];
  sandbox.console = { warn: (...a) => warns.push(a.join(' ')) };

  // Boot race: links is still the init {} (panels not loaded) -> NO warn, because
  // buildPanels will create the slot once /api/hub returns and render again.
  sandbox.renderWeather({ available: true });
  sandbox.renderClimate({ available: true });
  assert.equal(warns.length, 0, 'no warning during the boot race (links.panels not loaded yet)');

  // Genuinely misconfigured: links loaded, but neither feed has a panel entry.
  vm.runInContext("links = { panels: [{ id: 'chores' }] };", sandbox);
  sandbox.renderWeather({ available: true });
  sandbox.renderClimate({ available: true });
  assert.equal(warns.length, 2, 'each card warns once when its panel is genuinely unconfigured');
  assert.ok(warns.some((w) => /no 'weather' panel is configured/.test(w)), 'weather warns');
  assert.ok(warns.some((w) => /no 'climate' panel is configured/.test(w)), 'climate warns');

  // Latched: further calls stay silent (fires once per feed).
  sandbox.renderWeather({ available: true });
  sandbox.renderClimate({ available: true });
  assert.equal(warns.length, 2, 'each warning is latched');
});

test('camera HD upgrade gives up and keeps the warm stream when the HD never answers', async () => {
  const { document, sandbox } = newHub();
  const timers = captureTimers(sandbox);
  sandbox.fetch = async () => ({ ok: false });   // HD snapshot never answers (dead/slow HD)
  vm.runInContext(`links = { cameras: [${JSON.stringify(HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:drive');
  const content = document.getElementById('overlay-content');
  const hd = content.children[1];

  // drive the poll loop to exhaustion — the first check fires immediately (0ms),
  // each not-live probe reschedules another CAM_HD_POLL_MS out
  for (let i = 0; i < CAM_HD_TRIES_FOR_TEST; i++) {
    const poll = nextTimer(timers, i === 0 ? 0 : 700);
    if (!poll) break;
    poll.done = true;
    await poll.fn();
  }

  assert.ok(!hd.classList.contains('ready'), 'HD never revealed — it never answered');
  assert.equal(content.children.length, 1, 'the dead HD layer was dropped');
  assert.equal(content.children[0].src, '/wr/drive', 'the warm working stream is what stays');
});

test('camera HD reveal probe arms a short bounded timeout (CAM_HD_PROBE_TIMEOUT_MS), not the long default', () => {
  // revealHdWhenLive's own give-up budget is ~8s (CAM_HD_TRIES x CAM_HD_POLL_MS).
  // fetchTimeout defaults to the much longer J_TIMEOUT_MS (12s); a wedged HD
  // producer hanging on the default for all 12 tries would balloon the "~8s,
  // then give up" promise into minutes. Prove the HD probe passes its own
  // shorter, explicit timeout instead of falling back to that default.
  const { sandbox } = newHub();
  const timers = captureTimers(sandbox);   // captures every setTimeout, incl. fetchTimeout's abort timer
  sandbox.AbortController = AbortController;
  sandbox.fetch = () => new Promise(() => {});   // hang forever; only the armed timer matters here
  vm.runInContext(`links = { cameras: [${JSON.stringify(HD_CAM)}] };`, sandbox);

  sandbox.openOverlay('camera:drive');
  const first = nextTimer(timers, 0);   // the first HD reveal check fires immediately
  assert.ok(first, 'first HD reveal check scheduled');
  first.done = true;
  first.fn();   // runs synchronously up to the await, which is where fetchTimeout arms its abort timer

  assert.ok(timers.some((t) => t.ms === 3000 && !t.done),
    'the HD probe arms a 3000ms (CAM_HD_PROBE_TIMEOUT_MS) abort timeout');
  assert.ok(!timers.some((t) => t.ms === 12000),
    'never the long J_TIMEOUT_MS default for this probe');
});

test('renderIntegrations: a switch per integration, and gates disabled tiles', () => {
  const { sandbox } = newHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'weather', kind: 'weather', name: 'Weather', enabled: true },
    { id: 'cameras', kind: 'cameras', name: 'Cameras', enabled: false },
  ] });
  const host = sandbox.document.getElementById('integrations-ctl');
  assert.match(host.innerHTML, /Weather/);
  assert.match(host.innerHTML, /data-integ-toggle="cameras"/);
  assert.match(host.innerHTML, /integ-switch on/);                 // weather is on
  assert.match(host.innerHTML, /aria-checked="false"/);            // cameras is off
  // a disabled integration stamps a body gating class; an enabled one does not
  assert.ok(sandbox.document.body.classList.contains('integ-off-cameras'));
  assert.ok(!sandbox.document.body.classList.contains('integ-off-weather'));
  // toggling flips: re-render with cameras enabled clears the gate
  sandbox.renderIntegrations({ integrations: [
    { id: 'cameras', kind: 'cameras', name: 'Cameras', enabled: true },
  ] });
  assert.ok(!sandbox.document.body.classList.contains('integ-off-cameras'));
});

test('toggleIntegration: PATCHes the opposite of the current state', async () => {
  const { sandbox } = newHub();
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, method: opts.method, body: JSON.parse(opts.body) });
    return { ok: true, status: 200, json: async () => ({}) };
  };
  sandbox.renderIntegrations({ integrations: [
    { id: 'weather', kind: 'weather', name: 'Weather', enabled: true },
  ] });
  await sandbox.toggleIntegration('weather');
  assert.equal(calls[0].url, '/api/integrations/weather');
  assert.equal(calls[0].method, 'PATCH');
  assert.deepEqual(calls[0].body, { enabled: false });   // was on -> turn off
});

test('renderIntegrations: shows a reconnect hint when status is needs_auth', () => {
  const { sandbox } = newHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'icloud_caldav', kind: 'caldav', name: 'iCloud (CalDAV)',
      enabled: true, status: 'needs_auth' },
  ] });
  const host = sandbox.document.getElementById('integrations-ctl');
  assert.match(host.innerHTML, /integ-warn/);
  assert.match(host.innerHTML, /reconnect/);
});

test('renderIntegrations: shows an error hint when status is error', () => {
  const { sandbox } = newHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'google_calendar', kind: 'calendar', name: 'Google Calendar',
      enabled: true, status: 'error' },
  ] });
  const host = sandbox.document.getElementById('integrations-ctl');
  assert.match(host.innerHTML, /integ-warn/);
  assert.match(host.innerHTML, /error/);
});

test('renderIntegrations: splits into a Features group and an Integrations group', () => {
  const { sandbox } = newHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'chores', name: 'Chores', enabled: true, group: 'feature' },
    { id: 'todos', name: 'To-Dos', enabled: true, group: 'feature' },
    { id: 'weather', name: 'Weather', enabled: true, group: 'integration' },
  ] });
  const host = sandbox.document.getElementById('integrations-ctl');
  // NOTE: the fake DOM's parseFragment (top of this file) never populates
  // QueryNode.textContent from parsed markup — every existing textContent
  // assertion in this suite reads a value hub.js set via direct property
  // assignment, not text pulled out of an HTML string. So group titles are
  // asserted via innerHTML (same idiom as the sibling renderIntegrations
  // tests above, e.g. `assert.match(host.innerHTML, /Weather/)`), which
  // verifies the identical thing — both titles present, in order — without
  // depending on that unsupported path.
  assert.equal(host.querySelectorAll('.integ-group-title').length, 2,
    'a header for each non-empty group');
  const featuresAt = host.innerHTML.indexOf('integ-group-title">Features<');
  const integrationsAt = host.innerHTML.indexOf('integ-group-title">Integrations<');
  assert.ok(featuresAt >= 0 && integrationsAt > featuresAt,
    'Features group renders before the Integrations group');
  // every integration still gets its switch row
  assert.equal(host.querySelectorAll('.integ-row').length, 3);
});

test('toggleIntegration: a failed PATCH surfaces a toast (regression: the check used to be a no-op)', async () => {
  // attemptTodo never throws — it resolves to {ok:false, error}, a truthy
  // object — so a bare `if (!ok)` on that result is always false. Pins the
  // fix: toggleIntegration must check `.ok`, or a failed write silently
  // reverts the switch on the next poll() with no explanation at all.
  const { document, sandbox } = newHub();
  sandbox.renderIntegrations({ integrations: [
    { id: 'weather', kind: 'weather', name: 'Weather', enabled: true },
  ] });
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.toggleIntegration('weather');

  const toast = document.getElementById('toast');
  assert.ok(toast && toast.classList.contains('hub-toast-visible'),
    'the failure toast fired');
});

/* ------------------------------------------- Settings overlay + CalDAV panel */
// hub.js keeps caldavUi/hubData as module-lexical `let` bindings (same trick
// used throughout this file for choreState/data_date/etc.): a follow-up
// vm.runInContext in the SAME sandbox context shares that lexical scope, so
// it can seed/read them directly.

test('openOverlay("settings") opens the overlay, builds the settings-full skeleton, and clears a stale form error', () => {
  const { document, sandbox } = newHub();
  vm.runInContext("caldavUi.formError = 'stale error from a previous session';", sandbox);

  sandbox.openOverlay('settings');

  const content = document.getElementById('overlay-content');
  assert.match(content.innerHTML, /id="settings-full"/);
  assert.ok(document.getElementById('overlay').classList.contains('open'));
  assert.equal(vm.runInContext('caldavUi.formError', sandbox), '');
});

test('renderSettingsFull: paints Display + Integrations, with the live theme reflected onto its own controls', () => {
  const { document, sandbox } = newHub();
  // openOverlay('settings') writes #settings-full via innerHTML; the fake
  // parser doesn't register parsed nodes by id (same limitation the cal-full/
  // chores-full tests work around), so pre-register a real host FakeEl.
  const host = document.createElement('div');
  host._id = 'settings-full';
  document.body.appendChild(host);
  // reflectThemeControls reads document.documentElement.getAttribute; newHub's
  // stub documentElement only carries scrollTop (for scrollPageToTop), so give
  // it a working getAttribute for this test.
  document.documentElement.getAttribute = (k) => ({
    'data-theme': 'grey', 'data-accent': 'green', 'data-cols': 'none',
  }[k]);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'weather', kind: 'weather', name: 'Weather', enabled: true }] };",
    sandbox);

  sandbox.renderSettingsFull();

  const html = document.getElementById('settings-full').innerHTML;
  assert.match(html, /<h2>Display<\/h2>/);
  assert.match(html, /<h2>Features &amp; integrations<\/h2>/);
  assert.match(html, /id="integrations-ctl"/, 'the Integrations switch list mounts here');
  assert.match(html, /id="caldav-panel"/, 'the iCloud CalDAV panel mounts here');
  const greyBtn = host.querySelector('[data-theme-set="grey"]');
  assert.ok(greyBtn && greyBtn.classList.contains('on'),
    'reflectThemeControls marks the live theme on the overlay\'s own copy of the controls');
});

test('reflectThemeControls updates every matching control on the page, not just one surface', () => {
  // Pins the generalization from a single #theme-pop-scoped query to a
  // .theme-ctl-scoped one: both the gear popover's controls AND the Settings
  // overlay's own copy (rendered separately, see renderSettingsFull) must
  // stay in sync, since either can be the one currently visible. Each
  // fixture wraps its buttons in .theme-ctl, matching the real markup shape
  // (both surfaces do) that the scoped query now requires.
  const { document, sandbox } = newHub();
  const popHost = document.createElement('div');
  popHost.innerHTML = '<div class="theme-ctl"><button data-theme-set="light">Light</button>'
    + '<button data-theme-set="grey">Grey</button></div>';
  popHost._id = 'theme-pop';
  document.body.appendChild(popHost);
  const overlayHost = document.createElement('div');
  overlayHost.innerHTML = '<div class="theme-ctl"><button data-theme-set="light">Light</button>'
    + '<button data-theme-set="grey">Grey</button></div>';
  overlayHost._id = 'settings-full';
  document.body.appendChild(overlayHost);
  document.documentElement.getAttribute = (k) => (k === 'data-theme' ? 'grey' : null);

  sandbox.reflectThemeControls();

  for (const host of [popHost, overlayHost]) {
    assert.ok(host.querySelector('[data-theme-set="grey"]').classList.contains('on'));
    assert.ok(!host.querySelector('[data-theme-set="light"]').classList.contains('on'));
  }
});

function seedCaldavPanel(document) {
  const host = document.createElement('div');
  host._id = 'caldav-panel';
  document.body.appendChild(host);
  return host;
}

test('renderCaldavPanel: not connected shows the connect form; connected shows the account', () => {
  const { document, sandbox } = newHub();
  const host = seedCaldavPanel(document);

  vm.runInContext("hubData = { integrations: [] };", sandbox);
  sandbox.renderCaldavPanel();
  assert.match(host.innerHTML, /data-caldav-connect/);

  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', kind: 'caldav', name: 'iCloud (CalDAV)', "
    + "enabled: true, status: null, account: 'bot@example.com', readonly: true }] };",
    sandbox);
  sandbox.renderCaldavPanel();
  assert.match(host.innerHTML, /Connected as <strong>bot@example\.com/);
});

test('renderCaldavPanel: falls back to lastIntegrations when hubData has not loaded yet', () => {
  const { document, sandbox } = newHub();
  const host = seedCaldavPanel(document);
  // lastIntegrations is set by renderIntegrations, independent of hubData —
  // e.g. the very first paint before poll()'s first /api/hub response lands.
  sandbox.renderIntegrations({ integrations: [
    { id: 'icloud_caldav', kind: 'caldav', name: 'iCloud (CalDAV)',
      enabled: true, status: null, account: 'first@example.com', readonly: true },
  ] });

  sandbox.renderCaldavPanel();

  assert.match(host.innerHTML, /Connected as <strong>first@example\.com/);
});

test('connectCaldav: empty fields show an inline form error and never call the API', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  const u = document.createElement('input'); u._id = 'caldav-user-input'; u.value = '';
  document.body.appendChild(u);
  const p = document.createElement('input'); p._id = 'caldav-pw-input'; p.value = '';
  document.body.appendChild(p);
  let fetchCalled = false;
  sandbox.fetch = async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; };

  await sandbox.connectCaldav();

  assert.equal(fetchCalled, false, 'no network call when fields are empty');
  assert.match(document.getElementById('caldav-panel').innerHTML,
    /Enter both the Apple ID and the app-specific password/);
});

test('connectCaldav: success clears the password field, re-polls, and auto-runs a test', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  const u = document.createElement('input'); u._id = 'caldav-user-input'; u.value = 'bot@example.com';
  document.body.appendChild(u);
  const p = document.createElement('input'); p._id = 'caldav-pw-input'; p.value = 'app-specific-pw';
  document.body.appendChild(p);

  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts && opts.method,
      body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (String(url).includes('/credentials')) {
      return { ok: true, status: 200, json: async () => ({ ok: true, user: 'bot@example.com' }) };
    }
    if (String(url) === '/api/hub') {
      return { ok: true, status: 200, json: async () => ({ integrations: [
        { id: 'icloud_caldav', kind: 'caldav', name: 'iCloud (CalDAV)',
          enabled: true, status: null, account: 'bot@example.com', readonly: true },
      ] }) };
    }
    if (String(url).includes('/test')) {
      return { ok: true, status: 200, json: async () => ({ ok: true, events: 5, reminders: 1 }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  await sandbox.connectCaldav();

  assert.equal(p.value, '', 'the password input is blanked the instant the POST succeeds');
  const credCall = calls.find((c) => c.url.includes('/credentials'));
  assert.equal(credCall.method, 'POST');
  assert.deepEqual(credCall.body, { user: 'bot@example.com', app_password: 'app-specific-pw' });
  // never leak the password anywhere else, e.g. into a later call's URL/body
  assert.ok(!calls.some((c) => JSON.stringify(c).includes('app-specific-pw') && c !== credCall));
  assert.ok(calls.some((c) => c.url === '/api/hub'), 'poll() re-fetched /api/hub');
  assert.ok(calls.some((c) => c.url.includes('/test')), 'a Test ran automatically after connecting');
  const html = document.getElementById('caldav-panel').innerHTML;
  assert.match(html, /Connected as <strong>bot@example\.com/);
  assert.match(html, /caldav-test-result ok">Connected - 5 events, 1 reminder\./);
});

test('connectCaldav: a rejected POST shows a toast, leaves the password field alone, and stays on the form', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  const u = document.createElement('input'); u._id = 'caldav-user-input'; u.value = 'bot@example.com';
  document.body.appendChild(u);
  const p = document.createElement('input'); p._id = 'caldav-pw-input'; p.value = 'wrong-pw';
  document.body.appendChild(p);
  sandbox.fetch = async () => { throw new Error('422: user and app_password are required'); };

  await sandbox.connectCaldav();

  assert.equal(p.value, 'wrong-pw', 'a failed attempt does not blank what the operator typed');
  const toast = document.getElementById('toast');
  assert.ok(toast && toast.classList.contains('hub-toast-visible'));
  assert.match(document.getElementById('caldav-panel').innerHTML, /data-caldav-connect/,
    'still showing the connect form, not stuck on a permanent "Connecting…"');
});

test('testCaldavConnection: shows a testing state in flight, then the formatted result', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', kind: 'caldav', name: 'iCloud (CalDAV)', "
    + "enabled: true, status: null, account: 'bot@example.com', readonly: true }] };",
    sandbox);
  let sawTesting = false;
  sandbox.fetch = async (url) => {
    // Only the /test call itself proves the in-flight "Testing…" state; the
    // Calendars-picker refresh testCaldavConnection fires afterward (see
    // below) reuses this same mock and must not overwrite the capture.
    if (String(url).includes('/test')) {
      sawTesting = /Testing…/.test(document.getElementById('caldav-panel').innerHTML);
      return { ok: true, status: 200, json: async () => ({ needs_auth: true }) };
    }
    return { ok: true, status: 200, json: async () => ({ collections: [] }) };
  };

  await sandbox.testCaldavConnection();

  assert.ok(sawTesting, 'the panel showed the testing state before the request resolved');
  assert.match(document.getElementById('caldav-panel').innerHTML, /Sign-in rejected/);
});

test('testCaldavConnection: a network failure folds into the same {ok:false,error} shape', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  sandbox.fetch = async () => { throw new Error('network down'); };

  await sandbox.testCaldavConnection();

  assert.match(document.getElementById('caldav-panel').innerHTML, /caldav-test-result err">network down/);
});

test('disconnectCaldav: DELETEs credentials, clears the stale test result + Calendars picker, and re-polls back to the form',
  async () => {
    const { document, sandbox } = newHub();
    seedCaldavPanel(document);
    vm.runInContext("caldavUi.testResult = { ok: true, events: 1, reminders: 0 };", sandbox);
    vm.runInContext(
      "caldavUi.collections = [{ id: 'caldav:ab12', name: 'Family', color: null, "
      + "comp_type: 'VEVENT', enabled: true }]; caldavUi.collectionsError = true;",
      sandbox);
    const calls = [];
    sandbox.fetch = async (url, opts) => {
      calls.push({ url: String(url), method: opts && opts.method });
      if (String(url).includes('/credentials')) return { ok: true, status: 200, json: async () => ({ ok: true }) };
      if (String(url) === '/api/hub') return { ok: true, status: 200, json: async () => ({ integrations: [] }) };
      return { ok: true, status: 200, json: async () => ({}) };
    };

    await sandbox.disconnectCaldav();

    const del = calls.find((c) => c.url.includes('/credentials'));
    assert.equal(del.method, 'DELETE');
    assert.equal(vm.runInContext('caldavUi.testResult', sandbox), null,
      'a stale "connected" result would be misleading now');
    // vm.runInContext values live in a different realm, so a plain deepEqual
    // against a main-realm [] fails on prototype identity alone (same
    // JSON-round-trip workaround hub.test.mjs's monthGrid/panelFit use) -
    // .length is realm-agnostic and just as conclusive here.
    assert.equal(vm.runInContext('caldavUi.collections.length', sandbox), 0,
      'a stale calendar list would be misleading too');
    assert.equal(vm.runInContext('caldavUi.collectionsError', sandbox), false);
    assert.match(document.getElementById('caldav-panel').innerHTML, /data-caldav-connect/,
      'back to the not-connected form');
  });

test('disconnectCaldav: a failed DELETE shows a toast and does not touch the state', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.disconnectCaldav();

  const toast = document.getElementById('toast');
  assert.ok(toast && toast.classList.contains('hub-toast-visible'));
});

test('setCaldavReadonly: PATCHes {readonly} and re-renders the sync-direction control', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts && opts.method,
      body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (String(url) === '/api/hub') {
      return { ok: true, status: 200, json: async () => ({ integrations: [
        { id: 'icloud_caldav', enabled: true, account: 'bot@example.com', readonly: false },
      ] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  await sandbox.setCaldavReadonly(false);

  const patch = calls.find((c) => c.method === 'PATCH');
  assert.deepEqual(patch.body, { readonly: false });
  assert.match(document.getElementById('caldav-panel').innerHTML, /2-way \(write back\)/);
});

test('setCaldavReadonly: a failed PATCH shows a toast', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.setCaldavReadonly(true);

  const toast = document.getElementById('toast');
  assert.ok(toast && toast.classList.contains('hub-toast-visible'));
});

/* ------------------------------------- Calendars picker (caldav collections) */

test('renderSettingsFull: refreshes the Calendars picker every time the Settings overlay opens', () => {
  const { document, sandbox } = newHub();
  const host = document.createElement('div');
  host._id = 'settings-full';
  document.body.appendChild(host);
  document.documentElement.getAttribute = () => null;
  let called = false;
  // Same "swap the sandbox global for a spy" trick used throughout this file
  // (e.g. sandbox.attemptToggle above) - hub.js's functions are plain global
  // bindings in the vm context, so renderSettingsFull's internal call to
  // fetchCaldavCollections resolves to this override.
  sandbox.fetchCaldavCollections = () => { called = true; };

  sandbox.renderSettingsFull();

  assert.ok(called, 'new calendars/reminder lists can appear between visits - always refetch on open');
});

test('testCaldavConnection: refreshes the Calendars picker after a test runs (a sync can surface new calendars)', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  sandbox.fetch = async () => (
    { ok: true, status: 200, json: async () => ({ ok: true, events: 0, reminders: 0 }) });
  let called = false;
  sandbox.fetchCaldavCollections = () => { called = true; };

  await sandbox.testCaldavConnection();

  assert.ok(called);
});

test('fetchCaldavCollections: GETs the collections when connected and paints the picker', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  const calls = [];
  sandbox.fetch = async (url) => {
    calls.push(String(url));
    return { ok: true, status: 200, json: async () => ({ collections: [
      { id: 'caldav:ab12', name: 'Family', color: null, comp_type: 'VEVENT', enabled: true },
    ] }) };
  };

  await sandbox.fetchCaldavCollections();

  assert.deepEqual(calls, ['/api/integrations/icloud_caldav/collections']);
  assert.match(document.getElementById('caldav-panel').innerHTML,
    /data-caldav-collection-toggle="caldav:ab12"/);
});

test('fetchCaldavCollections: not connected never calls the API and the picker stays empty', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext("hubData = { integrations: [] };", sandbox);
  let fetchCalled = false;
  sandbox.fetch = async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; };

  await sandbox.fetchCaldavCollections();

  assert.equal(fetchCalled, false, 'no network call when icloud_caldav has no stored credentials');
});

test('fetchCaldavCollections: a failed GET never throws and shows a distinct "couldn\'t load" message, ' +
  'NOT the same copy a genuinely empty account gets', async () => {
  // Regression pin: a silent-failure-hunter review caught the original
  // implementation folding "GET failed" into the exact same "No calendars
  // found yet" text a truly empty account renders - an operator with a real
  // connection problem would conclude they simply have zero calendars.
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.fetchCaldavCollections();   // must not throw

  const html = document.getElementById('caldav-panel').innerHTML;
  assert.match(html, /Couldn.t load calendars - try Test connection/);
  assert.doesNotMatch(html, /No calendars found yet/,
    'a fetch failure must never be confused with a genuinely empty account');
});

test('fetchCaldavCollections: a failed GET keeps the last good list on screen instead of wiping it', async () => {
  // Same "keep cached data, don't paint a real problem as an empty state"
  // rule fetchCalWindow already follows for the main calendar feed.
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  vm.runInContext(
    "caldavUi.collections = [{ id: 'caldav:ab12', name: 'Family', color: null, "
    + "comp_type: 'VEVENT', enabled: true }];",
    sandbox);
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.fetchCaldavCollections();

  assert.match(document.getElementById('caldav-panel').innerHTML,
    /data-caldav-collection-toggle="caldav:ab12"/, 'the previously-fetched row is still shown');
});

test('fetchCaldavCollections: a successful fetch clears a prior error flag', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  vm.runInContext('caldavUi.collectionsError = true;', sandbox);
  sandbox.fetch = async () => ({ ok: true, status: 200, json: async () => ({ collections: [] }) });

  await sandbox.fetchCaldavCollections();

  assert.equal(vm.runInContext('caldavUi.collectionsError', sandbox), false);
  assert.match(document.getElementById('caldav-panel').innerHTML, /No calendars found yet - try Test connection/);
});

test('toggleCaldavCollection: PATCHes the opposite of the current state (id is URI-encoded) and refetches', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  vm.runInContext(
    "caldavUi.collections = [{ id: 'caldav:ab12', name: 'Family', color: null, "
    + "comp_type: 'VEVENT', enabled: true }];",
    sandbox);
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts && opts.method,
      body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    if (String(url).includes('/collections/')) {
      return { ok: true, status: 200, json: async () => ({ id: 'caldav:ab12', enabled: false }) };
    }
    return { ok: true, status: 200, json: async () => ({ collections: [
      { id: 'caldav:ab12', name: 'Family', color: null, comp_type: 'VEVENT', enabled: false },
    ] }) };
  };

  await sandbox.toggleCaldavCollection('caldav:ab12');

  const patch = calls.find((c) => c.method === 'PATCH');
  assert.equal(patch.url, '/api/integrations/icloud_caldav/collections/caldav%3Aab12',
    'the colon in the id is encodeURIComponent\'d into the URL');
  assert.deepEqual(patch.body, { enabled: false });   // was on -> turn off
  assert.ok(calls.some((c) => c.url === '/api/integrations/icloud_caldav/collections' && c.method === undefined),
    'refetched the collections list afterward, same as toggleIntegration/setCaldavReadonly do');
  assert.match(document.getElementById('caldav-panel').innerHTML, /integ-switch" aria-hidden/,
    'the picker repainted with the refreshed (now off) state');
});

test('toggleCaldavCollection: a failed PATCH shows a toast and does not flip or refetch', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext(
    "hubData = { integrations: [{ id: 'icloud_caldav', enabled: true, account: 'bot@example.com' }] };",
    sandbox);
  vm.runInContext(
    "caldavUi.collections = [{ id: 'caldav:ab12', name: 'Family', color: null, "
    + "comp_type: 'VEVENT', enabled: true }];",
    sandbox);
  sandbox.fetch = async () => { throw new Error('offline'); };

  await sandbox.toggleCaldavCollection('caldav:ab12');

  const toast = document.getElementById('toast');
  assert.ok(toast && toast.classList.contains('hub-toast-visible'));
  assert.equal(vm.runInContext('caldavUi.collections[0].enabled', sandbox), true,
    'a failed write must not flip the cached state - the next repaint would show the wrong thing');
});

test('toggleCaldavCollection: an unknown id is a no-op (no PATCH, no toast)', async () => {
  const { document, sandbox } = newHub();
  seedCaldavPanel(document);
  vm.runInContext('caldavUi.collections = [];', sandbox);
  let fetchCalled = false;
  sandbox.fetch = async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; };

  await sandbox.toggleCaldavCollection('missing');

  assert.equal(fetchCalled, false);
  assert.equal(document.getElementById('toast'), null);
});
