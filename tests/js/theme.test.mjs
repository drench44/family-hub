// Executable tests for theme.js — run with `node --test`.
//
// theme.js runs synchronously from <head> BEFORE any other script: it reads the
// persisted preferences (localStorage fh.theme/fh.accent/fh.cols, then a
// window.FH_THEME config default, then hardcoded grey/green/none) and stamps
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

test('fresh device with no prefs stamps the hardcoded grey/green/none default', () => {
  const { root, localStorage } = loadTheme();
  assert.equal(root.getAttribute('data-theme'), 'grey');
  assert.equal(root.getAttribute('data-accent'), 'green');
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
  assert.equal(root.getAttribute('data-theme'), 'grey');   // still the default
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

// ---- Layout choice (data-layout) ----
// data-layout is the per-device CHOICE: "auto" (the phone/wall split is decided
// by a pure-CSS width media query — no JS needed) or "desktop" (force the full
// wall at any width, the escape hatch for a TV that mis-reports a phone-narrow
// width). theme.js stamps ONLY this attribute; the CSS keys off it plus the
// media query. There is deliberately no JS-computed "mode" and no matchMedia —
// the layout must survive theme.js not running.

test('fresh device defaults to layout=auto and persists nothing', () => {
  const { root, localStorage } = loadTheme();
  assert.equal(root.getAttribute('data-layout'), 'auto');
  assert.equal(localStorage.getItem('fh.layout'), null);   // a default, not a choice
});

test('setLayout(desktop) stamps data-layout=desktop and persists (Firestick TV)', () => {
  // The driving case: a TV browser reports a phone-narrow width, so the CSS
  // media query would pick the phone shell. Forcing desktop suppresses it.
  const { root, localStorage, win } = loadTheme();
  win.setLayout('desktop');
  assert.equal(root.getAttribute('data-layout'), 'desktop');
  assert.equal(localStorage.getItem('fh.layout'), 'desktop');
});

test('setLayout(auto) hands control back to the width media query', () => {
  const { root, localStorage, win } = loadTheme({ storage: { 'fh.layout': 'desktop' } });
  assert.equal(root.getAttribute('data-layout'), 'desktop');   // started forced-desktop
  win.setLayout('auto');
  assert.equal(root.getAttribute('data-layout'), 'auto');
  assert.equal(localStorage.getItem('fh.layout'), 'auto');
});

test('an invalid layout is rejected: no stamp change, no persist', () => {
  // "mobile" is no longer a valid value (Auto/Desktop only); neither is garbage.
  const { root, localStorage, win } = loadTheme();
  win.setLayout('mobile');
  win.setLayout('sideways');
  assert.equal(root.getAttribute('data-layout'), 'auto');    // unchanged
  assert.equal(localStorage.getItem('fh.layout'), null);
});

test('a persisted fh.layout=desktop survives a reload (the Firestick keeps its choice)', () => {
  const { root } = loadTheme({ storage: { 'fh.layout': 'desktop' } });
  assert.equal(root.getAttribute('data-layout'), 'desktop');
});

test('window.FH_THEME.layout is the fallback when no localStorage override exists', () => {
  const { root, localStorage } = loadTheme({
    fhTheme: { mode: 'grey', accent: 'green', columns: 'none', layout: 'desktop' },
  });
  assert.equal(root.getAttribute('data-layout'), 'desktop');
  assert.equal(localStorage.getItem('fh.layout'), null);     // config default, not stored
});

test('a stored fh.layout beats the FH_THEME.layout config default', () => {
  const { root } = loadTheme({
    storage: { 'fh.layout': 'auto' },
    fhTheme: { layout: 'desktop' },
  });
  assert.equal(root.getAttribute('data-layout'), 'auto');   // device choice wins
});

test('stampLayout applies the choice WITHOUT persisting (house-default path)', () => {
  const { root, localStorage, win } = loadTheme();
  win.stampLayout('desktop');
  assert.equal(root.getAttribute('data-layout'), 'desktop');
  assert.equal(localStorage.getItem('fh.layout'), null);     // but NOT stored
});

// ---- Idle auto-return choice (data-idle-return) ----
// A per-device toggle: "on" (default — the shared wall drifts back to the home
// dashboard after an idle timeout) vs "off" (a personal phone / TV stays on the
// page you opened). Behavioral only (hub.js's armIdle reads the attribute), but
// managed exactly like the other device prefs so it persists + has a house
// default. Default ON so the wall keeps its existing behavior untouched.
test('fresh device defaults to idle-return=on and persists nothing', () => {
  const { root, localStorage } = loadTheme();
  assert.equal(root.getAttribute('data-idle-return'), 'on');
  assert.equal(localStorage.getItem('fh.idleReturn'), null);   // a default, not a choice
});

test('setIdleReturn(off) stamps data-idle-return=off and persists (personal device)', () => {
  const { root, localStorage, win } = loadTheme();
  win.setIdleReturn('off');
  assert.equal(root.getAttribute('data-idle-return'), 'off');
  assert.equal(localStorage.getItem('fh.idleReturn'), 'off');
});

test('setIdleReturn(on) restores auto-return', () => {
  const { root, localStorage, win } = loadTheme({ storage: { 'fh.idleReturn': 'off' } });
  assert.equal(root.getAttribute('data-idle-return'), 'off');   // started opted-out
  win.setIdleReturn('on');
  assert.equal(root.getAttribute('data-idle-return'), 'on');
  assert.equal(localStorage.getItem('fh.idleReturn'), 'on');
});

test('an invalid idle-return value is rejected: no stamp change, no persist', () => {
  const { root, localStorage, win } = loadTheme();
  win.setIdleReturn('sometimes');
  win.setIdleReturn('');
  assert.equal(root.getAttribute('data-idle-return'), 'on');    // unchanged
  assert.equal(localStorage.getItem('fh.idleReturn'), null);
});

test('a persisted fh.idleReturn=off survives a reload', () => {
  const { root } = loadTheme({ storage: { 'fh.idleReturn': 'off' } });
  assert.equal(root.getAttribute('data-idle-return'), 'off');
});

test('window.FH_THEME.idleReturn is the fallback when no localStorage override exists', () => {
  const { root, localStorage } = loadTheme({ fhTheme: { idleReturn: 'off' } });
  assert.equal(root.getAttribute('data-idle-return'), 'off');
  assert.equal(localStorage.getItem('fh.idleReturn'), null);     // config default, not stored
});

test('a stored fh.idleReturn beats the FH_THEME.idleReturn config default', () => {
  const { root } = loadTheme({
    storage: { 'fh.idleReturn': 'on' },
    fhTheme: { idleReturn: 'off' },
  });
  assert.equal(root.getAttribute('data-idle-return'), 'on');     // device choice wins
});

test('stampIdleReturn applies the choice WITHOUT persisting (house-default path)', () => {
  const { root, localStorage, win } = loadTheme();
  win.stampIdleReturn('off');
  assert.equal(root.getAttribute('data-idle-return'), 'off');
  assert.equal(localStorage.getItem('fh.idleReturn'), null);     // but NOT stored
});
