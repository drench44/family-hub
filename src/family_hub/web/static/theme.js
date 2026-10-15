/* ================================================================
   FAMILY HUB - theme.js

   Runs SYNCHRONOUSLY from <head>, before the body paints, so the wall
   never flashes the wrong theme (or the wrong layout). It reads the
   persisted preferences and stamps these attributes on <html>:

     data-theme        light | soft | dark | grey | black
     data-accent       cyan | violet | amber | green
     data-cols         none | wells | lines
     data-layout       auto | desktop       (the per-device layout CHOICE)
     data-idle-return  on | off             (per-device idle auto-return; hub.js
                                              reads it — behavioral, not visual)
     data-season       on | off             (seasonal looks follow the calendar)
     data-look         none | <look id>     (DERIVED from data-season, a pinned
                                              look (fh.lookPin) and today's date;
                                              see SEASONS below)

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
     1. localStorage  (fh.theme / fh.accent / fh.cols / fh.layout / fh.idleReturn
                       / fh.season): this device's own choice
     2. window.FH_THEME  { mode, accent, columns, layout, idleReturn, season }:
        the house default. hub.js sets it from each /api/hub poll, so it is
        only there after first paint (nothing injects it before this script
        runs); at startup it is normally undefined
     3. localStorage fh.house: the last house default hub.js handed to
        rememberHouseTheme, so a reload paints the house theme at once
        instead of grey-then-house (the next poll corrects a stale copy)
     4. hardcoded default  grey / green / none / auto / on / off

   Exposes setTheme / setAccent / setColumns / setLayout / setIdleReturn /
   setSeason (plus setSeasonLook, see the seasonal looks section): each
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

  // The house default: window.FH_THEME when hub.js has set it (after a poll),
  // else the copy cached under fh.house by rememberHouseTheme below.
  var HOUSE_KEY = "fh.house";
  function cachedHouse() {
    var raw = readStored(HOUSE_KEY);
    if (!raw) return undefined;
    try {
      var v = JSON.parse(raw);
      return v && typeof v === "object" ? v : undefined;
    } catch (e) {
      return undefined;   // a corrupt copy is ignored; the next poll rewrites it
    }
  }
  function configDefault(field) {
    var cfg = window.FH_THEME;
    if (cfg && typeof cfg === "object" && cfg[field] !== undefined) return cfg[field];
    var house = cachedHouse();
    return house ? house[field] : undefined;
  }

  function resolve(allowed, storageKey, configField, hardDefault) {
    var stored = readStored(storageKey);
    if (allowed.indexOf(stored) !== -1) return stored;
    var cfg = configDefault(configField);
    if (allowed.indexOf(cfg) !== -1) return cfg;
    return hardDefault;
  }

  // Keep the house default for the next page load's first paint. Called by
  // hub.js with each /api/hub `theme`. This is NOT a device choice (those are
  // the fh.theme/... keys, which it never touches), so a later house change
  // still reaches every device that never chose. Storage failures are
  // swallowed: the cache only saves a flash, it must never break a poll.
  function rememberHouseTheme(theme) {
    if (!theme || typeof theme !== "object") return;
    var json;
    try { json = JSON.stringify(theme); } catch (e) { return; }
    if (readStored(HOUSE_KEY) === json) return;   // unchanged: skip the write
    try {
      window.localStorage.setItem(HOUSE_KEY, json);
    } catch (e) {
      // storage blocked: the next load paints grey first, as before
    }
  }
  window.rememberHouseTheme = rememberHouseTheme;

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

  // ---- seasonal looks: data-season (the choice) + data-look (what paints) ----
  // A seasonal look is a real photograph (or public-domain artwork) filling
  // the screen behind glass cards, with a matching accent, leaves drifting
  // down and a small mark by the wordmark. It follows the calendar. The design
  // standards and sourcing rules live in docs/seasonal-looks.md; read that
  // before adding a look. Two prefs drive it:
  //   fh.season          on | off   (house default: FH_THEME.season)
  //   fh.look.<season>   which of that season's looks this device likes
  // and one derived attribute, data-look, names the look actually painted
  // ("none" when the season pref is off or today falls in no season).
  //
  // SEASONS is the one registry: the Settings tiles, the date windows and the
  // allowed look ids all come from it. To add a season, add an entry here, its
  // token blocks + scene rules in styles.css (test_static.py fails until every
  // look has them), and its art in static/seasons/ with a CREDITS.md line.
  // The server never sees look ids, only on/off. Order matters: the FIRST
  // season whose window holds today wins, so a short holiday (Halloween
  // inside fall, say) must be listed BEFORE the broad season it sits in. A
  // window may wrap the new year (from Dec 1 to Feb 28 works). Dates are
  // [month, day], both 1-based.
  var SEASONS = [
    // `default: true` marks the look a device gets before it picks one.
    // `credit` is shown on the Settings tile (full attribution lives in
    // static/seasons/CREDITS.md). Ids are storage keys and image file names:
    // renaming a look is free, changing its id resets that choice everywhere.
    // Christmas looks may carry `light`: where the photo's own lights are,
    // as fractions of the photo (x, y from its top left), so hub.js can make
    // them glow in place through the cover crop. `lamps` breathe softly (lit
    // windows, a lantern), `fire` flickers, `twinkle` shimmers (string
    // lights, a tree), `size` is a glow's width as a fraction of the photo.
    // `indoor` looks keep the snow outside: it falls only in `panes` (window
    // glass), or not at all when there is no window.
    { id: "christmas", name: "Christmas", from: [12, 1], to: [12, 31], looks: [
      { id: "christmas-glowing-cabin", name: "Glowing Cabin", blurb: "Lit windows in a snowy wood at dusk", credit: "Photo by the US Fish and Wildlife Service", default: true,
        light: { lamps: [[0.447, 0.585, 0.06], [0.538, 0.565, 0.05], [0.33, 0.455, 0.03], [0.49, 0.43, 0.025], [0.655, 0.56, 0.025]] } },
      { id: "christmas-cabin-window", name: "Cabin Window", blurb: "Snowy woods through a log cabin window", credit: "Photo by the US Fish and Wildlife Service",
        light: { indoor: true, panes: [[0.300, 0.225, 0.495, 0.860], [0.530, 0.245, 0.710, 0.845]] } },
      { id: "christmas-fireside", name: "Fireside", blurb: "A lit fire under a garland, a tree glowing either side", credit: "Photo by Erin Scott, The White House",
        light: { indoor: true, size: 0.012, fire: [[0.502, 0.722, 0.09]], lamps: [[0.38, 0.465, 0.03], [0.615, 0.465, 0.03]],
          twinkle: [[0.791, 0.211], [0.167, 0.864], [0.756, 0.571], [0.259, 0.481], [0.900, 0.818], [0.743, 0.749], [0.357, 0.438], [0.218, 0.886], [0.853, 0.669], [0.216, 0.686], [0.168, 0.382], [0.781, 0.389], [0.094, 0.810], [0.791, 0.436], [0.709, 0.826], [0.237, 0.788], [0.863, 0.828], [0.236, 0.646], [0.231, 0.271], [0.851, 0.570], [0.227, 0.533], [0.155, 0.712]] } },
      { id: "christmas-fairy-lights", name: "Fairy Lights", blurb: "A tree strung with warm little lights", credit: "Photo by Joanna Kosinska",
        light: { size: 0.014, twinkle: [[0.683, 0.289], [0.283, 0.316], [0.541, 0.615], [0.355, 0.378], [0.571, 0.582], [0.505, 0.631], [0.132, 0.656], [0.197, 0.697], [0.102, 0.486], [0.789, 0.526], [0.401, 0.541], [0.334, 0.685], [0.924, 0.712], [0.777, 0.260], [0.512, 0.767], [0.838, 0.821]] } },
      { id: "christmas-tree", name: "Christmas Tree", blurb: "A decorated tree aglow with lights", credit: "Photo by Evelyn (Unsplash)",
        light: { indoor: true, size: 0.016, twinkle: [[0.537, 0.171], [0.641, 0.719], [0.595, 0.132], [0.392, 0.131], [0.523, 0.706], [0.266, 0.491], [0.281, 0.259], [0.728, 0.530], [0.591, 0.549], [0.260, 0.358], [0.344, 0.556], [0.452, 0.306], [0.759, 0.369], [0.300, 0.535], [0.696, 0.352], [0.402, 0.300], [0.702, 0.609], [0.438, 0.598], [0.327, 0.739], [0.531, 0.492]] } },
      { id: "christmas-woodsmoke", name: "Woodsmoke", blurb: "An old cabin in falling snow, smoke from the chimney", credit: "Photo by Ryan Graybill" },
      { id: "christmas-sleigh-ride", name: "Sleigh Ride", blurb: "Sleighs through a frosted pink town, 1916", credit: "Painting by Boris Kustodiev" },
      { id: "christmas-pink-dawn", name: "Pink Dawn", blurb: "A snowy patrol cabin under a pink sunrise", credit: "Photo by the National Park Service" },
    ] },
    { id: "thanksgiving", name: "Thanksgiving", from: [11, 1], to: [11, 30], looks: [
      { id: "thanksgiving-pumpkin-patch", name: "Pumpkin Patch", blurb: "Pumpkins in warm late sun by the corn", credit: "Photo by Bonnie Kittle", default: true },
      { id: "thanksgiving-golden-hour", name: "Golden Hour", blurb: "Hay bales as the sun goes down", credit: "Photo by Annie Spratt" },
      { id: "thanksgiving-cranberry-bog", name: "Cranberry Bog", blurb: "A flooded bog full of floating cranberries", credit: "Photo by Jeff the quiet" },
      { id: "thanksgiving-cranberry-harvest", name: "Cranberry Harvest", blurb: "A bog carpeted in floating cranberries, from above", credit: "Photo by Lance Cheung, USDA" },
      { id: "thanksgiving-corn-shocks", name: "Corn Shocks", blurb: "Corn shocks by a white farmhouse", credit: "Photo by Jeff the quiet" },
      { id: "thanksgiving-vine-rows", name: "Vine Rows", blurb: "Red and orange rows on an autumn hillside", credit: "Photo by Lukas Budimaier" },
      { id: "thanksgiving-hudson-autumn", name: "Hudson Autumn", blurb: "Red and gold trees over the Hudson, 1860", credit: "Painting by Jasper Cropsey" },
    ] },
    { id: "halloween", name: "Halloween", from: [10, 1], to: [10, 31], looks: [
      { id: "halloween-two-lanterns", name: "Lantern Night", blurb: "Two carved pumpkins glowing in the dark", credit: "Photo by Beth Teutschmann", default: true },
      { id: "halloween-purple-sky", name: "Witching Hour", blurb: "The Milky Way over a violet horizon", credit: "Photo by Vincentiu Solomon" },
      { id: "halloween-purple-pines", name: "Haunted Pines", blurb: "Black pines against a purple night sky", credit: "Photo by Ryan Hutton" },
      { id: "halloween-moonrise", name: "Moonrise", blurb: "A full moon through the trees", credit: "Photo by the National Park Service" },
      { id: "halloween-branches", name: "Bare Branches", blurb: "A stand of bare trees at deep dusk", credit: "Photo by Vladimir Agafonkin" },
    ] },
    { id: "fall", name: "Fall", from: [9, 1], to: [11, 30], looks: [
      { id: "fall-aspen-grove", name: "Aspen Grove", blurb: "Sunlit gold under a blue sky", credit: "Photo by Patrick Myers, NPS", default: true },
      { id: "fall-misty-road", name: "Misty Road", blurb: "A quiet road through fog and fallen leaves", credit: "Photo by Bernd Schulz" },
      { id: "fall-maple-sky", name: "Maple Sky", blurb: "Red maple leaves against a clear blue sky", credit: "Photo by Aaron Burden" },
      { id: "fall-golden-light", name: "Golden Light", blurb: "Sunlit leaves glowing gold", credit: "Photo by Karl Fredrickson" },
      { id: "fall-aspen-sky", name: "Aspen Sky", blurb: "Looking up through golden aspens", credit: "Photo by Andrew Preble" },
      { id: "fall-autumn-road", name: "Autumn Road", blurb: "A road through orange autumn woods", credit: "Photo by Patrick Tomasso" },
      { id: "fall-white-mountains", name: "White Mountains", blurb: "An autumn sunrise in the mountains, 1857", credit: "Painting by Jasper Cropsey" },
      { id: "fall-sunny-day", name: "Sunny Autumn Day", blurb: "Red and gold trees over a meadow, 1892", credit: "Painting by George Inness" },
      { id: "fall-wheat-stacks", name: "Wheat Stacks", blurb: "Stacks of wheat at the end of the day, 1890", credit: "Painting by Claude Monet" },
    ] },
  ];
  var SEASON_PREFS = ["on", "off"];
  var DEFAULT_SEASON = "off";

  function seasonById(id) {
    for (var i = 0; i < SEASONS.length; i++) if (SEASONS[i].id === id) return SEASONS[i];
    return null;
  }
  function seasonOfLook(lookId) {
    for (var i = 0; i < SEASONS.length; i++) {
      var looks = SEASONS[i].looks;
      for (var j = 0; j < looks.length; j++) if (looks[j].id === lookId) return SEASONS[i];
    }
    return null;
  }
  // month*100+day compares calendar dates without a year; a window whose start
  // is later than its end wraps the new year.
  function inWindow(season, date) {
    var md = (date.getMonth() + 1) * 100 + date.getDate();
    var from = season.from[0] * 100 + season.from[1];
    var to = season.to[0] * 100 + season.to[1];
    return from <= to ? (md >= from && md <= to) : (md >= from || md <= to);
  }
  // duck-typed, not instanceof: a Date from another realm (an iframe, a test
  // sandbox) is still a Date
  function isDate(d) {
    return !!d && typeof d.getMonth === "function" && !isNaN(d.getTime());
  }
  function seasonFor(date) {
    for (var i = 0; i < SEASONS.length; i++) if (inWindow(SEASONS[i], date)) return SEASONS[i];
    return null;
  }
  // This session's picks, held in memory as well as storage: on a kiosk WebView
  // where setItem throws, a tap must still repaint now (the same "the CURRENT
  // session is correct" promise writeStored makes for every other pref).
  var lookPicks = {};
  // The look a season paints on this device: the device's favourite if it is
  // still one of the season's looks, else the season's default look (or its
  // first, if none is marked).
  function lookFor(season) {
    var fav = lookPicks[season.id] || readStored("fh.look." + season.id);
    for (var i = 0; i < season.looks.length; i++) if (season.looks[i].id === fav) return fav;
    for (var j = 0; j < season.looks.length; j++) if (season.looks[j].default) return season.looks[j].id;
    return season.looks[0].id;
  }

  // A pinned look paints every day, whatever the date: the owner asked that
  // looks not be locked to their season. fh.lookPin holds the look id; no key
  // (or an id no longer in SEASONS) means "follow the calendar". lookPinMem
  // mirrors it for this session (undefined: not touched yet, read storage;
  // "": cleared), for the same kiosk-storage reason as lookPicks.
  var PIN_KEY = "fh.lookPin";
  var lookPinMem;
  function pinnedLook() {
    var id = lookPinMem !== undefined ? lookPinMem : readStored(PIN_KEY);
    return id && seasonOfLook(id) ? id : null;
  }
  function removeStored(key) {
    try {
      window.localStorage.removeItem(key);
    } catch (e) {
      try { console.warn("family-hub: could not clear " + key + " (storage unavailable)"); } catch (e2) { /* no console */ }
    }
  }

  function stampSeason(v) {
    root.setAttribute("data-season", v);
  }
  // Re-derive data-look from the season pref and the date. hub.js calls this on
  // its clock tick, so a wall left running rolls into (and out of) a season at
  // midnight with no reload. `date` is optional (tests pass a fixed one).
  function refreshLook(date) {
    var d = isDate(date) ? date : new Date();
    var on = root.getAttribute("data-season") === "on";
    var pin = on ? pinnedLook() : null;
    var season = on && !pin ? seasonFor(d) : null;
    var look = pin || (season ? lookFor(season) : "none");
    if (root.getAttribute("data-look") !== look) root.setAttribute("data-look", look);
    return look;
  }
  // Did someone on THIS device choose the season pref this session? The house
  // default only applies to a device that never chose, and hub.js asks this
  // as well as localStorage, because on a kiosk where storage refuses writes
  // the next poll would otherwise stamp the house value over a fresh tap.
  var seasonChosen = false;
  function seasonChoiceMade() { return seasonChosen; }
  function setSeason(v) {
    if (SEASON_PREFS.indexOf(v) === -1) return;
    seasonChosen = true;
    writeStored("fh.season", v);
    stampSeason(v);
    refreshLook();
  }
  function stampSeasonIf(v) {
    if (SEASON_PREFS.indexOf(v) === -1) return;
    stampSeason(v);
    refreshLook();
  }
  // Tapping a look shows it now and keeps it, any day of the year (a pin).
  // It also becomes that season's favourite, so going back to the calendar
  // paints it whenever its season comes round. Choosing a look is a clear "I
  // want seasonal looks", so it turns the season pref on AND saves that as
  // this device's own choice (even when a house default already stamped "on":
  // otherwise a later house change to "off" would undo the pick).
  function setSeasonLook(lookId) {
    var season = seasonOfLook(lookId);
    if (!season) return;
    lookPicks[season.id] = lookId;
    lookPinMem = lookId;
    seasonChosen = true;
    writeStored("fh.look." + season.id, lookId);
    writeStored(PIN_KEY, lookId);
    writeStored("fh.season", "on");
    stampSeason("on");
    refreshLook();
  }
  // Back to the calendar: drop the pin and turn seasonal looks on.
  function followCalendar() {
    lookPinMem = "";
    seasonChosen = true;
    removeStored(PIN_KEY);
    writeStored("fh.season", "on");
    stampSeason("on");
    refreshLook();
  }
  // The look a season would paint on this device right now (for the Settings
  // tiles' "selected" mark), and the season in force on a date (or null).
  function seasonLook(seasonId) {
    var season = seasonById(seasonId);
    return season ? lookFor(season) : null;
  }
  function activeSeason(date) {
    var s = seasonFor(isDate(date) ? date : new Date());
    return s ? s.id : null;
  }

  window.FH_SEASONS = SEASONS;
  window.setSeason = setSeason;
  window.stampSeason = stampSeasonIf;
  window.setSeasonLook = setSeasonLook;
  window.followCalendar = followCalendar;
  window.pinnedLook = pinnedLook;
  window.seasonChoiceMade = seasonChoiceMade;
  // The season whose window opens soonest after `date` (or null with no
  // seasons): lets the UI say "Fall starts Sep 1" when nothing is in season.
  window.nextSeason = function (date) {
    var d = isDate(date) ? date : new Date();
    var today = (d.getMonth() + 1) * 100 + d.getDate();
    var best = null, bestGap = Infinity;
    for (var i = 0; i < SEASONS.length; i++) {
      var start = SEASONS[i].from[0] * 100 + SEASONS[i].from[1];
      // days-ish ordering is enough: compare month*100+day, wrapping the year
      var gap = start > today ? start - today : start + 1300 - today;
      if (gap < bestGap) { bestGap = gap; best = SEASONS[i]; }
    }
    return best;
  };
  window.refreshLook = refreshLook;
  window.seasonLook = seasonLook;
  window.activeSeason = activeSeason;

  // ---- initial stamp (synchronous, before paint) ----
  stampTheme(resolve(THEMES, "fh.theme", "mode", DEFAULT_THEME));
  stampAccent(resolve(ACCENTS, "fh.accent", "accent", DEFAULT_ACCENT));
  stampColumns(resolve(COLUMNS, "fh.cols", "columns", DEFAULT_COLUMNS));
  stampLayout(resolve(LAYOUTS, "fh.layout", "layout", DEFAULT_LAYOUT));
  stampIdleReturn(resolve(IDLE_RETURNS, "fh.idleReturn", "idleReturn", DEFAULT_IDLE_RETURN));
  stampSeason(resolve(SEASON_PREFS, "fh.season", "season", DEFAULT_SEASON));
  refreshLook();
})();
