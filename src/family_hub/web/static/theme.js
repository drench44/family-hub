/* ================================================================
   FAMILY HUB - theme.js

   Runs SYNCHRONOUSLY from <head>, before the body paints, so the wall
   never flashes the wrong theme. It reads the persisted preferences and
   stamps three attributes on <html>:

     data-theme   light | soft | dark | grey | black
     data-accent  cyan | violet | amber | green
     data-cols    none | wells | lines

   Fallback order for each preference:
     1. localStorage  (fh.theme / fh.accent / fh.cols)
     2. window.FH_THEME  { mode, accent, columns }, injected by the
        page from server config (arrives in a later task; may be undefined now)
     3. hardcoded default  grey / green / none

   Exposes setTheme(mode) / setAccent(name) / setColumns(name): each
   validates its value, writes the localStorage key, and re-stamps the
   attribute on <html> live, so a control can apply a change without a reload.

   Dependency-free, no network, no house data. LAN wall display safe.
   ================================================================ */
(function () {
  "use strict";

  var root = document.documentElement;

  var THEMES = ["light", "soft", "dark", "grey", "black"];
  var ACCENTS = ["cyan", "violet", "amber", "green"];
  var COLUMNS = ["none", "wells", "lines"];

  var DEFAULT_THEME = "grey";
  var DEFAULT_ACCENT = "green";
  var DEFAULT_COLUMNS = "none";

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
      /* storage unavailable; the live attribute still updates below */
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

  window.setTheme = setTheme;
  window.setAccent = setAccent;
  window.setColumns = setColumns;

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
  window.stampTheme = stampThemeIf;
  window.stampAccent = stampAccentIf;
  window.stampColumns = stampColumnsIf;

  // ---- initial stamp (synchronous, before paint) ----
  stampTheme(resolve(THEMES, "fh.theme", "mode", DEFAULT_THEME));
  stampAccent(resolve(ACCENTS, "fh.accent", "accent", DEFAULT_ACCENT));
  stampColumns(resolve(COLUMNS, "fh.cols", "columns", DEFAULT_COLUMNS));
})();
