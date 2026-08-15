// Executable tests for theme.js — run with `node --test`.
//
// theme.js runs synchronously from <head> BEFORE any other script: it reads the
// persisted preferences (localStorage fh.theme/fh.accent/fh.cols, then a
// window.FH_THEME config default, then hardcoded dark/cyan/none) and stamps
// data-theme/data-accent/data-cols on <html>, and exposes setters (which
// persist) plus stamp-only appliers (which do NOT persist).
//
// Same no-dependency approach as hub-dom.test.mjs: this repo ships no
// package.json / node_modules, so theme.js (a classic <script>, no exports) is
// loaded into a vm sandbox holding a tiny fake `document.documentElement`
// (attribute bag) and a fake `window.localStorage`. Its window.* assignments
// surface on the sandbox's window; everything else stays in the IIFE closure.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'family_hub', 'web', 'static');
const themeSrc = readFileSync(join(staticDir, 'theme.js'), 'utf8');

function makeRoot() {
  const attrs = {};
  return {
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return k in attrs ? attrs[k] : null; },
  };
}

function makeStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(k, String(v)); },
    _map: map,
  };
}

// Load theme.js fresh (mirrors a page reload) with the given pre-seeded storage
// and optional window.FH_THEME config default.
function loadTheme({ storage = {}, fhTheme } = {}) {
  const root = makeRoot();
  const localStorage = makeStorage(storage);
  const win = { localStorage };
  if (fhTheme !== undefined) win.FH_THEME = fhTheme;
  const sandbox = { window: win, document: { documentElement: root } };
  vm.createContext(sandbox);
  vm.runInContext(themeSrc, sandbox);
  return { root, localStorage, win };
}

test('fresh device with no prefs stamps the hardcoded dark/cyan/none default', () => {
  const { root, localStorage } = loadTheme();
  assert.equal(root.getAttribute('data-theme'), 'dark');
  assert.equal(root.getAttribute('data-accent'), 'cyan');
  assert.equal(root.getAttribute('data-cols'), 'none');
  // the default is a fallback, NOT a stored choice — nothing was persisted
  assert.equal(localStorage.getItem('fh.theme'), null);
  assert.equal(localStorage.getItem('fh.accent'), null);
});

test('setAccent(violet) stamps data-accent and persists fh.accent', () => {
  const { root, localStorage, win } = loadTheme();
  win.setAccent('violet');
  assert.equal(root.getAttribute('data-accent'), 'violet');
  assert.equal(localStorage.getItem('fh.accent'), 'violet');
});

test('setTheme(light) stamps data-theme and persists fh.theme', () => {
  const { root, localStorage, win } = loadTheme();
  win.setTheme('light');
  assert.equal(root.getAttribute('data-theme'), 'light');
  assert.equal(localStorage.getItem('fh.theme'), 'light');
});

test('all five theme modes are accepted, stamped, and persisted', () => {
  // The wall offers five modes; "dark" is the legacy blue-navy value (labelled
  // "Blue" in the UI), joined by soft/grey/black. Each must stamp + persist.
  for (const mode of ['light', 'soft', 'dark', 'grey', 'black']) {
    const { root, localStorage, win } = loadTheme();
    win.setTheme(mode);
    assert.equal(root.getAttribute('data-theme'), mode);
    assert.equal(localStorage.getItem('fh.theme'), mode);
  }
});

test('an invalid theme is rejected: no stamp, no persist', () => {
  // Guards the THEMES whitelist now that it has five entries.
  const { root, localStorage, win } = loadTheme();
  win.setTheme('rainbow');   // not one of the five
  assert.equal(root.getAttribute('data-theme'), 'dark');   // still the default
  assert.equal(localStorage.getItem('fh.theme'), null);
});

test('setColumns(wells) stamps data-cols and persists fh.cols', () => {
  // PT1: the accept path for columns (only the reject path was covered before).
  const { root, localStorage, win } = loadTheme();
  win.setColumns('wells');
  assert.equal(root.getAttribute('data-cols'), 'wells');
  assert.equal(localStorage.getItem('fh.cols'), 'wells');
});

test('setColumns(lines) stamps data-cols and persists fh.cols', () => {
  // Lines is the third, mockup-parity separation option (added on request).
  const { root, localStorage, win } = loadTheme();
  win.setColumns('lines');
  assert.equal(root.getAttribute('data-cols'), 'lines');
  assert.equal(localStorage.getItem('fh.cols'), 'lines');
});

test('an invalid value is rejected: no stamp, no persist', () => {
  const { root, localStorage, win } = loadTheme();
  win.setColumns('stripes');   // not one of none/wells/lines
  assert.equal(root.getAttribute('data-cols'), 'none');   // still the default
  assert.equal(localStorage.getItem('fh.cols'), null);
});

test('a reload reads a persisted choice back (violet survives)', () => {
  // second page load: fh.accent already saved on this device
  const { root } = loadTheme({ storage: { 'fh.accent': 'violet' } });
  assert.equal(root.getAttribute('data-accent'), 'violet');
});

test('stampAccent applies the look WITHOUT persisting (house-default path)', () => {
  const { root, localStorage, win } = loadTheme();
  win.stampAccent('green');
  assert.equal(root.getAttribute('data-accent'), 'green');   // look changed
  assert.equal(localStorage.getItem('fh.accent'), null);     // but NOT stored
});

test('stampColumns applies the look WITHOUT persisting (house-default path)', () => {
  // PT3: only stampAccent was covered; prove stampColumns stamps but never writes.
  const { root, localStorage, win } = loadTheme();
  win.stampColumns('wells');
  assert.equal(root.getAttribute('data-cols'), 'wells');   // look changed
  assert.equal(localStorage.getItem('fh.cols'), null);     // but NOT stored
});

test('stampTheme applies the look WITHOUT persisting (house-default path)', () => {
  // PT3: prove stampTheme stamps the mode but never writes fh.theme.
  const { root, localStorage, win } = loadTheme();
  win.stampTheme('light');
  assert.equal(root.getAttribute('data-theme'), 'light');   // look changed
  assert.equal(localStorage.getItem('fh.theme'), null);     // but NOT stored
});

test('window.FH_THEME is the fallback when no localStorage override exists', () => {
  const { root, localStorage } = loadTheme({ fhTheme: { mode: 'light', accent: 'amber', columns: 'wells' } });
  assert.equal(root.getAttribute('data-theme'), 'light');
  assert.equal(root.getAttribute('data-accent'), 'amber');
  assert.equal(root.getAttribute('data-cols'), 'wells');
  // a config default is not a per-device choice — nothing persisted
  assert.equal(localStorage.getItem('fh.theme'), null);
});

test('a stored override beats the FH_THEME config default', () => {
  const { root } = loadTheme({
    storage: { 'fh.accent': 'violet' },
    fhTheme: { mode: 'light', accent: 'amber', columns: 'wells' },
  });
  assert.equal(root.getAttribute('data-accent'), 'violet');   // device choice wins
  assert.equal(root.getAttribute('data-theme'), 'light');     // config fills the rest
});
