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
// `now` pins "today" for theme.js's own new Date() calls (the seasonal look is
// date-derived), so no assertion depends on the day the suite runs.
function loadTheme({ storage = {}, fhTheme, now } = {}) {
  const root = makeRoot();
  const localStorage = makeStorage(storage);
  const win = { localStorage };
  if (fhTheme !== undefined) win.FH_THEME = fhTheme;
  const sandbox = { window: win, document: { documentElement: root } };
  if (now) {
    sandbox.Date = class extends Date {
      constructor(...a) { if (a.length) super(...a); else super(now.getTime()); }
    };
  }
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

// ---------------------------------------------------------------- seasonal looks
// data-season (on|off) is the per-device choice; data-look is DERIVED from it
// and the date through theme.js's SEASONS registry. Dates are built from local
// components (new Date(y, m, d)) so these hold under TZ=UTC as well.
const day = (m, d) => new Date(2026, m - 1, d, 12, 0, 0);

test('fresh device: seasonal looks default off, paint nothing, persist nothing', () => {
  const { root, localStorage } = loadTheme();
  assert.equal(root.getAttribute('data-season'), 'off');
  assert.equal(root.getAttribute('data-look'), 'none');
  assert.equal(localStorage.getItem('fh.season'), null);
});

test('setSeason(on) persists and paints the season\'s default look inside its window', () => {
  const { root, localStorage, win } = loadTheme();
  win.setSeason('on');
  assert.equal(localStorage.getItem('fh.season'), 'on');
  assert.equal(win.refreshLook(day(11, 15)), 'fall-aspen-grove');
  assert.equal(root.getAttribute('data-look'), 'fall-aspen-grove');
});

test('outside every season window the look is none even with seasons on', () => {
  const { root, win } = loadTheme({ storage: { 'fh.season': 'on' } });
  win.refreshLook(day(1, 15));
  assert.equal(root.getAttribute('data-look'), 'none');
  win.refreshLook(day(7, 4));
  assert.equal(root.getAttribute('data-look'), 'none');
});

test('the fall window is inclusive at both ends: Sep 1 and Nov 30 in, Aug 31 and Dec 1 out', () => {
  const { win } = loadTheme({ storage: { 'fh.season': 'on' } });
  assert.equal(win.refreshLook(day(8, 31)), 'none');
  assert.equal(win.refreshLook(day(9, 1)), 'fall-aspen-grove');
  assert.equal(win.refreshLook(day(11, 30)), 'fall-aspen-grove');
  assert.equal(win.refreshLook(day(12, 1)), 'none');
});

test('setSeason(off) takes the look down immediately', () => {
  const { root, win } = loadTheme({ storage: { 'fh.season': 'on' } });
  win.refreshLook(day(11, 1));
  win.setSeason('off');
  assert.equal(root.getAttribute('data-look'), 'none');
});

test('setSeasonLook stores the favourite for ITS season and turns seasons on', () => {
  const { root, localStorage, win } = loadTheme();
  win.setSeasonLook('fall-maple-sky');
  assert.equal(localStorage.getItem('fh.look.fall'), 'fall-maple-sky');
  assert.equal(localStorage.getItem('fh.season'), 'on');
  assert.equal(root.getAttribute('data-season'), 'on');
  assert.equal(win.refreshLook(day(11, 20)), 'fall-maple-sky');
  assert.equal(win.seasonLook('fall'), 'fall-maple-sky');
});

test('a stored favourite survives a reload', () => {
  const { win } = loadTheme({ storage: { 'fh.season': 'on', 'fh.look.fall': 'fall-aspen-grove' } });
  assert.equal(win.refreshLook(day(9, 30)), 'fall-aspen-grove');
});

test('an unknown stored favourite falls back to the season\'s default look', () => {
  // e.g. a look renamed or removed in a later release
  const { win } = loadTheme({ storage: { 'fh.season': 'on', 'fh.look.fall': 'fall-gone' } });
  assert.equal(win.refreshLook(day(11, 5)), 'fall-aspen-grove');
});

test('invalid season values and unknown look ids are rejected: no stamp, no persist', () => {
  const { root, localStorage, win } = loadTheme();
  win.setSeason('sometimes');
  assert.equal(root.getAttribute('data-season'), 'off');
  assert.equal(localStorage.getItem('fh.season'), null);
  win.setSeasonLook('winter-nope');
  assert.equal(root.getAttribute('data-season'), 'off');
  assert.equal(localStorage._map.size, 0, 'nothing written for an unknown look');
});

test('FH_THEME.season is the house fallback; a stored choice beats it; stampSeason never persists', () => {
  const house = loadTheme({ fhTheme: { season: 'on' } });
  assert.equal(house.root.getAttribute('data-season'), 'on');
  assert.equal(house.localStorage.getItem('fh.season'), null);

  const mine = loadTheme({ storage: { 'fh.season': 'off' }, fhTheme: { season: 'on' } });
  assert.equal(mine.root.getAttribute('data-season'), 'off');

  const fresh = loadTheme();
  fresh.win.stampSeason('on');
  assert.equal(fresh.root.getAttribute('data-season'), 'on');
  assert.equal(fresh.localStorage.getItem('fh.season'), null, 'house path must not persist');
  fresh.win.stampSeason('bogus');
  assert.equal(fresh.root.getAttribute('data-season'), 'on', 'invalid house value ignored');
});

test('a window may wrap the new year, and the FIRST matching season wins', () => {
  // Pins the two rules the registry comment promises future seasons: a
  // Dec->Feb window works, and a short holiday listed before its broad
  // season takes precedence inside it.
  const { win } = loadTheme({ storage: { 'fh.season': 'on' } });
  win.FH_SEASONS.push({ id: 'winter', name: 'Winter', from: [12, 1], to: [2, 28],
    looks: [{ id: 'winter-snow', name: 'Snow' }] });
  win.FH_SEASONS.unshift({ id: 'harvest', name: 'Harvest', from: [11, 20], to: [11, 27],
    looks: [{ id: 'harvest-table', name: 'Table' }] });
  assert.equal(win.refreshLook(day(12, 25)), 'winter-snow');
  assert.equal(win.refreshLook(day(1, 10)), 'winter-snow');
  assert.equal(win.refreshLook(day(3, 1)), 'none');
  assert.equal(win.refreshLook(day(11, 25)), 'harvest-table');
  assert.equal(win.refreshLook(day(11, 19)), 'fall-aspen-grove');
  assert.equal(win.activeSeason(day(11, 25)), 'harvest');
});

test('Halloween owns October from inside fall, and fall keeps the rest', () => {
  // The registry comment's rule, now with real seasons: Halloween is listed
  // BEFORE fall and its window sits inside fall's, so October paints
  // Halloween and the days on either side paint fall.
  const { win } = loadTheme({ storage: { 'fh.season': 'on' } });
  assert.equal(win.activeSeason(day(9, 30)), 'fall');
  assert.equal(win.activeSeason(day(10, 1)), 'halloween');
  assert.equal(win.activeSeason(day(10, 31)), 'halloween');
  assert.equal(win.activeSeason(day(11, 1)), 'fall');
  assert.equal(win.refreshLook(day(10, 15)), 'halloween-two-lanterns',
    'the marked default is what a fresh device paints all October');
  assert.equal(win.refreshLook(day(11, 15)), 'fall-aspen-grove');
  const ids = win.FH_SEASONS.map((season) => season.id);
  assert.ok(ids.indexOf('halloween') < ids.indexOf('fall'),
    'a short holiday must be listed before the season it sits inside');
});

test('a season inside another season is always listed first', () => {
  // The rule the registry comment promises, checked against whatever is
  // actually registered rather than against today's two: the first matching
  // window wins, so a holiday inside a broad season must come before it or
  // it can never paint. Thanksgiving inside fall is next.
  const { win } = loadTheme();
  const span = (s2) => {
    const from = s2.from[0] * 100 + s2.from[1];
    const to = s2.to[0] * 100 + s2.to[1];
    return { from, to, wraps: to < from };
  };
  const seasons = win.FH_SEASONS.map((s2, i) => ({ id: s2.id, i, ...span(s2) }));
  for (const a of seasons) {
    for (const b of seasons) {
      if (a === b || a.wraps || b.wraps) continue;
      const aInsideB = a.from >= b.from && a.to <= b.to;
      if (aInsideB) {
        assert.ok(a.i < b.i,
          `${a.id}'s window sits inside ${b.id}'s, so it must be listed first or it never paints`);
      }
    }
  }
});

test('the season registry is well formed', () => {
  const { win } = loadTheme();
  const ids = new Set();
  assert.ok(win.FH_SEASONS.length >= 1);
  for (const s of win.FH_SEASONS) {
    assert.match(s.id, /^[a-z]+$/);
    for (const [m, d] of [s.from, s.to]) {
      assert.ok(m >= 1 && m <= 12 && d >= 1 && d <= 31, `${s.id} has a real date`);
    }
    assert.ok(s.looks.length >= 1, `${s.id} has at least one look`);
    // test_static.py's _look_ids() finds look ids as the hyphenated ids in the
    // registry; a look id without a hyphen would drop out of its CSS guards
    for (const l of s.looks) assert.match(l.id, /^[a-z]+-[a-z0-9-]+$/);
    for (const l of s.looks) {
      assert.ok(l.id.startsWith(s.id + '-'), `${l.id} is prefixed by its season`);
      assert.ok(!ids.has(l.id), `${l.id} is unique`);
      ids.add(l.id);
      assert.ok(l.name && l.blurb, `${l.id} has a name and a blurb for its tile`);
    }
  }
});

test('a look pick repaints NOW even when storage refuses the write (kiosk WebView)', () => {
  // a NON-default look: with the default, a lost write would fall back to the
  // same answer and this test would pass without the in-memory fix
  const { root, win, localStorage } = loadTheme({ storage: { 'fh.season': 'on' } });
  localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
  win.setSeasonLook('fall-maple-sky');
  assert.equal(win.refreshLook(day(11, 2)), 'fall-maple-sky', 'held in memory for this session');
  assert.equal(root.getAttribute('data-look'), 'fall-maple-sky');
  assert.equal(win.seasonLook('fall'), 'fall-maple-sky', 'the tile marks what paints');
});

test('seasonChoiceMade: false until someone on this device picks, then true', () => {
  const { win, localStorage } = loadTheme();
  localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
  assert.equal(win.seasonChoiceMade(), false);
  win.setSeason('on');
  assert.equal(win.seasonChoiceMade(), true, 'remembered even though storage refused the write');
  const other = loadTheme();
  other.win.setSeasonLook('fall-misty-road');
  assert.equal(other.win.seasonChoiceMade(), true);
  const house = loadTheme();
  house.win.stampSeason('on');
  assert.equal(house.win.seasonChoiceMade(), false, 'a house default is not a choice');
});

test('nextSeason names the season that opens soonest, wrapping the year', () => {
  const { win } = loadTheme();
  assert.equal(win.nextSeason(day(1, 15)).id, 'fall');
  assert.equal(win.nextSeason(day(12, 20)).id, 'fall', 'after fall ends, next fall');
  win.FH_SEASONS.push({ id: 'winter', name: 'Winter', from: [12, 1], to: [2, 28], looks: [{ id: 'winter-snow', name: 'Snow' }] });
  assert.equal(win.nextSeason(day(11, 30)).id, 'winter');
  assert.equal(win.nextSeason(day(3, 1)).id, 'fall');
});

test('picking a look saves "on" as this device\'s own choice, even under a house "on"', () => {
  // a house default stamps "on" without persisting; a later house "off" must
  // not undo a look the family deliberately picked on this device
  const { localStorage, win } = loadTheme({ fhTheme: { season: 'on' } });
  assert.equal(localStorage.getItem('fh.season'), null);
  win.setSeasonLook('fall-maple-sky');
  assert.equal(localStorage.getItem('fh.season'), 'on');
});

// ---- every setter repaints on the spot (no refreshLook call from the test) ----
// Without these, deleting refreshLook() from a setter would leave a tap doing
// nothing visible until midnight and every other test green.
test('setSeason(on) repaints immediately in season, and stays none out of season', () => {
  const inFall = loadTheme({ now: day(11, 15) });
  inFall.win.setSeason('on');
  assert.equal(inFall.root.getAttribute('data-look'), 'fall-aspen-grove');
  const inJuly = loadTheme({ now: day(7, 4) });
  inJuly.win.setSeason('on');
  assert.equal(inJuly.root.getAttribute('data-look'), 'none');
});

test('setSeasonLook repaints immediately with the picked look', () => {
  const { root, win } = loadTheme({ now: day(11, 15) });
  win.setSeasonLook('fall-maple-sky');
  assert.equal(root.getAttribute('data-look'), 'fall-maple-sky');
  win.setSeasonLook('fall-aspen-grove');
  assert.equal(root.getAttribute('data-look'), 'fall-aspen-grove');
});

test('the house stampSeason repaints immediately too (applyHouseTheme runs after first paint)', () => {
  const { root, win } = loadTheme({ now: day(11, 15) });
  assert.equal(root.getAttribute('data-look'), 'none');
  win.stampSeason('on');
  assert.equal(root.getAttribute('data-look'), 'fall-aspen-grove');
});

test('first paint derives the look from a stored "on" and today', () => {
  const { root } = loadTheme({ storage: { 'fh.season': 'on', 'fh.look.fall': 'fall-maple-sky' }, now: day(11, 1) });
  assert.equal(root.getAttribute('data-look'), 'fall-maple-sky');
});

test('refreshLook with a missing or invalid date falls back to now', () => {
  const { win } = loadTheme({ storage: { 'fh.season': 'on' }, now: day(11, 15) });
  assert.equal(win.refreshLook(new Date('garbage')), 'fall-aspen-grove');
  assert.equal(win.refreshLook(), 'fall-aspen-grove');
  assert.equal(win.refreshLook('2026-01-01'), 'fall-aspen-grove', 'a string is not a Date');
});

test('each season marks exactly one default look', () => {
  const { win } = loadTheme();
  for (const s of win.FH_SEASONS) {
    assert.equal(s.looks.filter((l) => l.default).length, 1, `${s.id} marks exactly one default`);
  }
});

test('the default flag, not list order, picks the look a fresh device gets', () => {
  const { win } = loadTheme({ storage: { 'fh.season': 'on' } });
  win.FH_SEASONS.unshift({ id: 'spring', name: 'Spring', from: [3, 1], to: [5, 31], looks: [
    { id: 'spring-a', name: 'A', blurb: 'a', credit: 'x' },
    { id: 'spring-b', name: 'B', blurb: 'b', credit: 'y', default: true },
  ] });
  assert.equal(win.refreshLook(day(4, 10)), 'spring-b');
  win.FH_SEASONS[0].looks[1].default = false;
  assert.equal(win.refreshLook(day(4, 11)), 'spring-a', 'no default marked: the first look');
});
