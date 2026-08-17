/* ================================================================
   FAMILY HUB - theme.js

   Runs SYNCHRONOUSLY from <head>, before the body paints, so the wall
   never flashes the wrong theme (or the wrong layout). It reads the
   persisted preferences and stamps five attributes on <html>:

     data-theme        light | soft | dark | grey | black
     data-accent       cyan | violet | amber | green
     data-cols         none | wells | lines
     data-layout       auto | desktop       (the per-device layout CHOICE)
     data-idle-return  on | off             (per-device idle auto-return; hub.js
                                              reads it — behavioral, not visual)

   data-layout is the ONLY layout attribute. In "auto" the phone/wall split
   is decided by a pure-CSS width media query (max-width:1000px) — no JS
   needed, so a phone still gets the mobile layout even if this script never
   runs. Choosing "desktop" forces the full wall at ANY width by suppressing
   that media query (the CSS keys the shell off :root:not([data-layout=
   "desktop"])); this is the escape hatch for a TV browser that mis-reports a
   phone-narrow width (e.g. a Fire TV) and would otherwise be stuck in the
   phone layout. Stamping data-layout before paint keeps the wall from
   flashing the phone shell first on such a device.

   Fallback order for each preference:
     1. localStorage  (fh.theme / fh.accent / fh.cols / fh.layout / fh.idleReturn)
     2. window.FH_THEME  { mode, accent, columns, layout, idleReturn }, injected
        by the page from server config (may be undefined)
     3. hardcoded default  grey / green / none / auto / on

   Exposes setTheme / setAccent / setColumns / setLayout / setIdleReturn: each
   validates its value, writes the localStorage key, and re-stamps live, so a
   control can apply a change without a reload.

   Dependency-free, no network, no house data. LAN wall display safe.
   ================================================================ */
(function () {
  "use strict";

  var root = document.documentElement;

  var THEMES = ["light", "soft", "dark", "grey", "black"];
  var ACCENTS = ["cyan", "violet", "amber", "green"];
  var COLUMNS = ["none", "wells", "lines"];
  var LAYOUTS = ["auto", "desktop"];
  var IDLE_RETURNS = ["on", "off"];

  var DEFAULT_THEME = "grey";
  var DEFAULT_ACCENT = "green";
  var DEFAULT_COLUMNS = "none";
  var DEFAULT_LAYOUT = "auto";
  var DEFAULT_IDLE_RETURN = "on";

  // localStorage can throw (private mode / disabled storage); never let that
  // break first paint.
  function readStored(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      // Storage unavailable/locked-down (e.g. a kiosk TV WebView). The live
      // attribute still updates below, so the CURRENT session is correct — but
      // the choice won't survive a reload. Leave a breadcrumb so a
      // "my forced-Desktop TV reverted after reboot" report is diagnosable
      // rather than silent; never rethrow (that would break first paint).
      try { console.warn("family-hub: could not persist " + key + " (storage unavailable)"); } catch (e2) { /* no console */ }
    }
  }

  // The page may inject window.FH_THEME = { mode, accent, columns } from config.
  function configDefault(field) {
    var cfg = window.FH_THEME;
    return cfg && typeof cfg === "object" ? cfg[field] : undefined;
  }

  function resolve(allowed, storageKey, configField, hardDefault) {
    var stored = readStored(storageKey);
    if (allowed.indexOf(stored) !== -1) return stored;
    var cfg = configDefault(configField);
    if (allowed.indexOf(cfg) !== -1) return cfg;
    return hardDefault;
  }

  function stampTheme(mode) {
    root.setAttribute("data-theme", mode);
  }
  function stampAccent(name) {
    root.setAttribute("data-accent", name);
  }
  function stampColumns(name) {
    root.setAttribute("data-cols", name);
  }

  // ---- layout: a single data-layout attribute (auto | desktop) ----
  // The CSS does the rest: a width media query handles "auto", and
  // :root:not([data-layout="desktop"]) lets "desktop" suppress the phone shell
  // at any width. No matchMedia and no computed "mode" — keeping the phone
  // layout a pure-CSS concern is what makes it survive this script not running.
  function stampLayout(layout) {
    root.setAttribute("data-layout", layout);
  }

  // ---- idle auto-return: data-idle-return (on | off) ----
  // Behavioral, not visual: hub.js's armIdle() reads this attribute and skips
  // arming the return-home timer when it is "off". Managed here (rather than ad
  // hoc in hub.js) so it shares the same localStorage + house-default + setter
  // machinery as the other device prefs. Default "on" keeps the shared wall's
  // existing drift-back-to-home behavior; a personal phone/TV sets "off".
  function stampIdleReturn(v) {
    root.setAttribute("data-idle-return", v);
  }

  // ---- public setters: validate, persist, re-stamp live ----
  function setTheme(mode) {
    if (THEMES.indexOf(mode) === -1) return;
    writeStored("fh.theme", mode);
    stampTheme(mode);
  }
  function setAccent(name) {
    if (ACCENTS.indexOf(name) === -1) return;
    writeStored("fh.accent", name);
    stampAccent(name);
  }
  function setColumns(name) {
    if (COLUMNS.indexOf(name) === -1) return;
    writeStored("fh.cols", name);
    stampColumns(name);
  }
  function setLayout(layout) {
    if (LAYOUTS.indexOf(layout) === -1) return;
    writeStored("fh.layout", layout);
    stampLayout(layout);
  }
  function setIdleReturn(v) {
    if (IDLE_RETURNS.indexOf(v) === -1) return;
    writeStored("fh.idleReturn", v);
    stampIdleReturn(v);
  }

  window.setTheme = setTheme;
  window.setAccent = setAccent;
  window.setColumns = setColumns;
  window.setLayout = setLayout;
  window.setIdleReturn = setIdleReturn;

  // ---- stamp-only appliers: validate + re-stamp live, WITHOUT persisting ----
  // The house default from server config (window.FH_THEME / /api/hub) is applied
  // through these on a fresh device: it must change the LOOK without writing a
  // localStorage override (that key means "this device chose this"; writing the
  // house value there would freeze the device against future house-default
  // changes). Only the user's own control taps go through the setters above,
  // which do persist. See Task 5.
  function stampThemeIf(mode) {
    if (THEMES.indexOf(mode) !== -1) stampTheme(mode);
  }
  function stampAccentIf(name) {
    if (ACCENTS.indexOf(name) !== -1) stampAccent(name);
  }
  function stampColumnsIf(name) {
    if (COLUMNS.indexOf(name) !== -1) stampColumns(name);
  }
  function stampLayoutIf(layout) {
    if (LAYOUTS.indexOf(layout) !== -1) stampLayout(layout);
  }
  function stampIdleReturnIf(v) {
    if (IDLE_RETURNS.indexOf(v) !== -1) stampIdleReturn(v);
  }
  window.stampTheme = stampThemeIf;
  window.stampAccent = stampAccentIf;
  window.stampColumns = stampColumnsIf;
  window.stampLayout = stampLayoutIf;
  window.stampIdleReturn = stampIdleReturnIf;

  // ---- initial stamp (synchronous, before paint) ----
  stampTheme(resolve(THEMES, "fh.theme", "mode", DEFAULT_THEME));
  stampAccent(resolve(ACCENTS, "fh.accent", "accent", DEFAULT_ACCENT));
  stampColumns(resolve(COLUMNS, "fh.cols", "columns", DEFAULT_COLUMNS));
  stampLayout(resolve(LAYOUTS, "fh.layout", "layout", DEFAULT_LAYOUT));
  stampIdleReturn(resolve(IDLE_RETURNS, "fh.idleReturn", "idleReturn", DEFAULT_IDLE_RETURN));
})();
