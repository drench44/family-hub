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
  'panels', 'tabbar', 'overlay', 'overlay-home', 'overlay-content', 'ev-modal',
  'ev-card',
  'chore-modal', 'chore-card', 'chore-editor',
  'confirm-modal', 'confirm-card', 'confirm-msg',
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
  // document.querySelector(All) searches every registered element's parsed
  // innerHTML — enough for hub.js's document-wide lookups (probeCamera's
  // '.tile-camera[data-cam="..."]', renderPeople's '.person-card[...]').
  const document = {
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new FakeEl(registry, tag),
    addEventListener: () => {},
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

  // Timers are inert: no callback ever fires, so the poll loop and the toast
  // auto-hide don't run — the toast stays put for us to assert on.
  const sandbox = {
    document,
    window: { addEventListener: () => {}, innerWidth: 1280, innerHeight: 800 },
    innerWidth: 1280,
    innerHeight: 800,
    scrollTo: () => {},
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
  return { document, sandbox };
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
  // openOverlay('chores') writes #chores-full via innerHTML; our fake parser
  // doesn't register parsed nodes by id, so pre-register a real host FakeEl.
  const choresFull = new FakeEl(registry);
  choresFull._id = 'chores-full';
  registry['chores-full'] = choresFull;

  const completeCalls = [];
  const adminChoreCalls = [];   // POST/PATCH /api/admin/chores writes from the editor
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
    tap, tapConfirm, read,
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

test('chores overlay: a "Manage on the admin page" link shows in both modes', () => {
  const { choresFull, tap } = mountChoresFull(SAMPLE_PEOPLE);
  // view mode: the footer link to the full admin page is present
  const link = choresFull.querySelector('.manage-admin-link');
  assert.ok(link, 'the .manage-admin-link rendered in view mode');
  assert.equal(link.attrs.href, '/admin.html',
    'links to the admin page (same-origin href — works on the wall and on a phone)');
  assert.match(choresFull.innerHTML, /Manage chores on the admin page/);
  // the QR was dropped — no leftover QR markup or svg
  assert.ok(!choresFull.innerHTML.includes('<svg'), 'no QR svg (QR removed)');
  assert.ok(!choresFull.innerHTML.includes('manage-qr'), 'no leftover manage-qr markup');
  // edit mode: the footer persists (visible in BOTH view and edit mode)
  tap('[data-chedit="1"]');
  assert.ok(choresFull.querySelector('.manage-admin-link'), 'the link persists in edit mode');
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

test('sectionHead with no overlay/expandLabel emits no expand button (e.g. Cameras)', () => {
  const { sandbox } = newHub();
  const html = sandbox.sectionHead('Cameras');
  assert.match(html, /<h2>Cameras<\/h2>/);
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
  // Cameras has no expand overlay — the header carries no expand button
  assert.ok(!html.includes('class="expand"'), 'the Cameras header has no expand button');
  assert.match(html, /data-cam="cam1"/);
});

test('initTiles renders no orphan header with zero cameras', () => {
  const { document, sandbox } = newHub();
  vm.runInContext('links = { cameras: [] };', sandbox);

  sandbox.initTiles();

  assert.equal(document.getElementById('tiles').innerHTML, '',
    'no cameras configured means no "Cameras" header with nothing under it');
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
  humidity: 57, dew_point: 58.5, spark: [70, 71, 73, 75, 78, 80, 79, 77], stale: false,
};
const WX_WARN = {
  available: true, temp: 90, unit: 'F', conditions: 'Hazy', feels: 98,
  low: 75, high: 95, uv: 9, uv_desc: 'Very High', aqi: 151, aqi_cat: 'Unhealthy',
  humidity: 70, dew_point: 74, spark: [], stale: true,
};

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

test('weather card draws the sparkline (area + line + endpoint) when spark has >=2 points', () => {
  const { html } = renderWeatherHtml(WX_GOOD);
  assert.ok(html.includes('<svg class="spark"'), 'the sparkline svg is present');
  assert.match(html, /viewBox="0 0 300 46"/, 'normalized to the 0 0 300 46 viewBox');
  assert.match(html, /<path d="M[^"]*Z" fill="url\(#sg\)"/, 'gradient area path');
  assert.match(html, /<path d="M[^"]*" fill="none" stroke="var\(--accent\)"/, 'stroked line path');
  assert.match(html, /<circle cx="300"[^>]*fill="var\(--accent\)"/, 'emphasized endpoint circle');
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
