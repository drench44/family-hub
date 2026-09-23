// DOM tests for osk.js's wiring (show/hide), run with `node --test`.
// osk.test.mjs covers the pure text transform (oskApplyKey in common.js); this
// file loads osk.js itself into a vm context in kiosk mode against a small
// fake DOM, so the keyboard's life cycle can be driven: a focusin shows it,
// and the MutationObserver callback (captured, fired by hand) reacts to
// fields leaving the DOM. Same no-dependency approach as hub-dom.test.mjs:
// no jsdom, no node_modules.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'family_hub', 'web', 'static');
const commonSrc = readFileSync(join(staticDir, 'common.js'), 'utf8');
const oskSrc = readFileSync(join(staticDir, 'osk.js'), 'utf8');

// One compound selector: an optional tag, then #id / .class / [attr] parts.
// Enough for osk.js's own lookups ('.osk-key[data-letter]', '.osk-shift',
// '#todo-add-input', '.txt-input'); anything else throws so a silent miss
// can't send a test down the wrong branch.
function matchOne(el, sel) {
  const m = /^([a-z]+)?((?:[#.][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/.exec(sel);
  if (!m) throw new Error(`unsupported selector in fake DOM: ${sel}`);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  const re = /([#.])([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let p;
  while ((p = re.exec(m[2]))) {
    if (p[1] === '#' && el.id !== p[2]) return false;
    if (p[1] === '.' && !el.classList.contains(p[2])) return false;
    if (p[3]) {
      const key = p[3].startsWith('data-')
        ? p[3].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
      if (key ? !(key in el.dataset) : !(p[3] in el.attrs)) return false;
      if (p[4] !== undefined && (key ? el.dataset[key] : el.attrs[p[3]]) !== p[4]) return false;
    }
  }
  return true;
}

class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase();
    this.doc = doc;
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.dataset = {};
    this.attrs = {};
    this.id = '';
    this.type = '';
    this.value = '';
    this.form = null;
    this.maxLength = -1;
    this.listeners = {};
    this._text = '';
  }

  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    const api = {
      add: (...c) => { el.className = [...new Set([...list(), ...c])].join(' '); },
      remove: (...c) => { el.className = list().filter((x) => !c.includes(x)).join(' '); },
      contains: (c) => list().includes(c),
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(c) : !!force;
        if (on) api.add(c); else api.remove(c);
        return on;
      },
    };
    return api;
  }

  get textContent() { return this._text; }

  set textContent(v) { this.children = []; this._text = String(v); }

  get isConnected() {
    for (let n = this; n; n = n.parentNode) if (n === this.doc.body) return true;
    return false;
  }

  setAttribute(k, v) { this.attrs[k] = String(v); }

  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }

  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }

  remove() {
    if (!this.parentNode) return;
    const kids = this.parentNode.children;
    kids.splice(kids.indexOf(this), 1);
    this.parentNode = null;
  }

  contains(n) {
    for (let x = n; x; x = x.parentNode) if (x === this) return true;
    return false;
  }

  matches(sel) { return sel.split(',').some((s) => matchOne(this, s.trim())); }

  closest(sel) {
    for (let n = this; n && n.matches; n = n.parentNode) if (n.matches(sel)) return n;
    return null;
  }

  // Like a browser: click() on a disabled button does nothing.
  click() { if (!this.disabled && this.onclick) this.onclick(); }

  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }

  addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }

  focus() { this.doc.activeElement = this; }

  blur() { if (this.doc.activeElement === this) this.doc.activeElement = this.doc.body; }

  scrollIntoView() {}

  setSelectionRange() {}

  dispatchEvent() { return true; }
}

function loadOsk() {
  const docListeners = {};
  const document = {
    addEventListener: (t, fn) => { (docListeners[t] || (docListeners[t] = [])).push(fn); },
  };
  document.createElement = (tag) => new El(tag, document);
  document.body = new El('body', document);
  document.activeElement = document.body;
  document.querySelectorAll = (sel) => document.body.querySelectorAll(sel);
  const observers = [];
  const store = new Map([['oskKiosk', '1']]);
  const sandbox = {
    document,
    navigator: {},
    location: { search: '' },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
    MutationObserver: class {
      constructor(cb) { this.cb = cb; observers.push(this); }
      observe() {}
    },
    Event: class { constructor(type, opts) { this.type = type; Object.assign(this, opts); } },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(commonSrc, sandbox);
  vm.runInContext(oskSrc, sandbox);
  const osk = document.body.children.find((c) => c.classList.contains('osk'));
  assert.ok(osk, 'kiosk mode built the keyboard');
  const fire = (type, ev) => (docListeners[type] || []).forEach((fn) => fn(ev));
  // What the browser does after DOM changes: run every observer's callback.
  const mutated = (records) => observers.forEach((o) => o.cb(records));
  const field = (parent) => {
    const inp = new El('input', document);
    inp.id = 'todo-add-input';
    inp.type = 'text';
    parent.appendChild(inp);
    return inp;
  };
  return { document, osk, fire, mutated, field, sandbox };
}

test('osk: focusing a wall text field shows the keyboard', () => {
  const { document, osk, fire, field } = loadOsk();
  const inp = field(document.body);
  inp.focus();
  fire('focusin', { target: inp });
  assert.ok(!osk.classList.contains('hidden'), 'keyboard shown');
  assert.ok(document.body.classList.contains('osk-open'));
});

test('osk: the keyboard hides when its field leaves the DOM with no blur (idle-closed overlay)', () => {
  // Firefox fires no blur/focusout for a focused field that is removed from
  // the DOM, so an overlay closed by the idle timer left the keyboard docked
  // over the home wall with nothing to type into.
  const { document, osk, fire, mutated, field } = loadOsk();
  const overlayContent = new El('div', document);
  document.body.appendChild(overlayContent);
  const panel = new El('div', document);
  overlayContent.appendChild(panel);
  const inp = field(panel);
  inp.focus();
  fire('focusin', { target: inp });
  assert.ok(!osk.classList.contains('hidden'));
  panel.remove();                                   // closeOverlay: innerHTML = ''
  mutated([{ addedNodes: [], removedNodes: [panel] }]);
  assert.ok(osk.classList.contains('hidden'), 'keyboard hidden once its field is gone');
  assert.ok(!document.body.classList.contains('osk-open'));
  assert.equal(osk.getAttribute('aria-hidden'), 'true');
});

test('osk: a repaint that swaps in a new focused field keeps the keyboard up', () => {
  // renderTodosPaint rebuilds the add input and re-focuses the new one, so the
  // new field's focusin lands before the observer sees the old one go.
  const { document, osk, fire, mutated, field } = loadOsk();
  const host = new El('div', document);
  document.body.appendChild(host);
  const old = field(host);
  old.focus();
  fire('focusin', { target: old });
  old.remove();
  const fresh = field(host);
  fresh.focus();
  fire('focusin', { target: fresh });
  mutated([{ addedNodes: [fresh], removedNodes: [old] }]);
  assert.ok(!osk.classList.contains('hidden'), 'still shown for the new field');
});

// The chore and person editors are plain divs in the .chore-card modal, not
// <form>s, so Done commits through their Save button. The chore form's is
// [data-submit]; the person form's is [data-psubmit].
function editorWithSave(document, saveAttr) {
  const card = new El('div', document);
  card.className = 'chore-card';
  document.body.appendChild(card);
  const inp = new El('input', document);
  inp.className = 'txt-input';
  inp.type = 'text';
  card.appendChild(inp);
  const save = new El('button', document);
  save.dataset[saveAttr] = '';
  card.appendChild(save);
  return { inp, save };
}

function pressDone(osk) {
  const done = osk.querySelectorAll('.osk-key').find((b) => b.dataset.key === 'Done');
  assert.ok(done, 'the keyboard has a Done key');
  (osk.listeners.click || []).forEach((fn) => fn({ target: done }));
}

test('osk: Done saves the person form too (its Save is [data-psubmit])', () => {
  const { document, osk, fire } = loadOsk();
  const { inp, save } = editorWithSave(document, 'psubmit');
  let saves = 0;
  save.onclick = () => { saves += 1; };
  inp.focus();
  fire('focusin', { target: inp });
  pressDone(osk);
  assert.equal(saves, 1, 'Done clicked the person Save');
});

test('osk: Done still saves the chore form ([data-submit])', () => {
  const { document, osk, fire } = loadOsk();
  const { inp, save } = editorWithSave(document, 'submit');
  let saves = 0;
  save.onclick = () => { saves += 1; };
  inp.focus();
  fire('focusin', { target: inp });
  pressDone(osk);
  assert.equal(saves, 1);
});

test('osk: Done pressed again while a save is still out sends nothing new', () => {
  // The one-save-at-a-time guard (common.js oneSaveAtATime) sits on the Save
  // button, so it holds for the Done key's click as well as a finger's tap.
  const { document, osk, fire, sandbox } = loadOsk();
  const { inp, save } = editorWithSave(document, 'submit');
  let saves = 0;
  save.onclick = sandbox.oneSaveAtATime(save, () => { saves += 1; return new Promise(() => {}); });
  inp.focus();
  fire('focusin', { target: inp });
  pressDone(osk);
  inp.focus();
  fire('focusin', { target: inp });
  pressDone(osk);
  assert.equal(saves, 1, 'one write, not two');
});
