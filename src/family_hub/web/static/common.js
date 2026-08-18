'use strict';

/* Family Hub — helpers shared across the wall page (hub.js) and the on-screen
   keyboard (osk.js). Loaded as a classic script BEFORE them; its top-level
   declarations are visible to the scripts that follow. Some helpers here build
   DOM (the reusable chore/person editor forms) — those run only in the browser;
   the pure helpers load into a vm sandbox for tests/js with no document. */

const J_TIMEOUT_MS = 12000;   // abort a hung request (see j)

async function j(url, opts) {
  // Guard against a CONNECTED-but-unresponsive server (flaky wifi, a wedged box,
  // a router mid-reboot): a bare fetch to such a peer never rejects, so poll()
  // never reaches its offline branch and the wall shows frozen data still badged
  // "live". Abort after J_TIMEOUT_MS so the hang becomes a rejection the callers
  // already handle. Only when the platform has AbortController — the browser
  // always does; the vm test sandbox has no AbortController and stubs fetch with
  // a fast resolver that can't hang, so it needs (and gets) no timeout.
  let timer = null;
  let fetchOpts = opts;
  if (typeof AbortController !== 'undefined' && !(opts && opts.signal)) {
    const ac = new AbortController();
    timer = setTimeout(() => ac.abort(), J_TIMEOUT_MS);
    fetchOpts = { ...(opts || {}), signal: ac.signal };
  }
  try {
    const r = await fetch(url, fetchOpts);
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (e) { /* not json */ }
      throw new Error(detail || `${url} -> HTTP ${r.status}`);
    }
    // `return await` (not bare `return r.json()`) is load-bearing: it keeps the
    // finally — and so clearTimeout — deferred until the BODY resolves. A bare
    // return would disarm the abort timer the instant headers arrive, leaving a
    // server that flushes 200 headers then stalls the body hung forever (the
    // exact frozen-but-"live" failure this timeout exists to prevent).
    return r.status === 204 ? null : await r.json();
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/* Same AbortController-timeout guard as j() above, for callers that only need
   the raw Response (not j()'s JSON-decode + error-detail contract): the
   camera snapshot probes in hub.js. Without this, a raw fetch to a connected-
   but-unresponsive server never resolves, and probes stack up until the
   browser's ~6-connection-per-origin budget is exhausted on a multi-day kiosk
   uptime. Same platform guard as j(): falls back to a bare fetch when
   AbortController isn't available (the vm test sandbox). */
function fetchTimeout(url, ms = J_TIMEOUT_MS) {
  if (typeof AbortController === 'undefined') return fetch(url);
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  return fetch(url, { signal: ac.signal }).finally(() => clearTimeout(timer));
}

/* Defense in depth for inline style="" sinks: colors reaching the DOM are all
   server-constrained (person colors validated to #rrggbb, Google's own palette,
   a hardcoded event-color map), but escapeHtml neutralizes quotes/<> and NOT
   `;`/`:`, so a stray value could otherwise inject extra CSS declarations into
   the same attribute. Pass only a hex token or a bare CSS keyword through; fall
   back to a harmless value otherwise. */
function safeColor(v) {
  const s = String(v == null ? '' : v).trim();
  return /^#[0-9a-fA-F]{3,8}$/.test(s) || /^[a-zA-Z]+$/.test(s) ? s : 'transparent';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* Wall-clock time straight from the event's ISO string — the household and the
   display share one timezone, so we show the time as written rather than
   re-projecting through the viewer's clock (which would also make tests depend
   on the runner's timezone). '2026-08-13T07:30:00-07:00' -> '7:30am'. */
function fmtTime(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(iso || '');
  if (!m) return '';
  let h = parseInt(m[1], 10);
  const min = parseInt(m[2], 10);
  const ap = h < 12 ? 'am' : 'pm';
  let hh = h % 12;
  if (hh === 0) hh = 12;
  return min === 0 ? `${hh}${ap}` : `${hh}:${String(min).padStart(2, '0')}${ap}`;
}

/* 'YYYY-MM-DD' -> 'Today' | 'Tomorrow' | 'Wed 8/19' (relative to todayStr).
   Dates are built from their own components (local midnight) so the weekday is
   timezone-independent. */
function dayLabel(dateStr, todayStr) {
  if (dateStr === todayStr) return 'Today';
  const [ty, tm, td] = todayStr.split('-').map(Number);
  const tm1 = new Date(ty, tm - 1, td + 1);
  const tomorrow = `${tm1.getFullYear()}-${String(tm1.getMonth() + 1).padStart(2, '0')}-${String(tm1.getDate()).padStart(2, '0')}`;
  if (dateStr === tomorrow) return 'Tomorrow';
  const [y, m, d] = dateStr.split('-').map(Number);
  const wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date(y, m - 1, d).getDay()];
  return `${wd} ${m}/${d}`;
}

/* Agenda day header: the relative name leads, the calendar date rides along
   quietly, and once the day is more than a step away a distance hint sits on
   the right edge so "Mon 8/17" reads as "four days out" at a glance. */
function dayHeadHtml(dateStr, todayStr) {
  const [y, m, dd] = dateStr.split('-').map(Number);
  const [ty, tm, td] = todayStr.split('-').map(Number);
  const diff = Math.round(
    (new Date(y, m - 1, dd) - new Date(ty, tm - 1, td)) / 86400000);
  const wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date(y, m - 1, dd).getDay()];
  const date = `${wd} ${m}/${dd}`;
  let name = date, sub = '', rel = '';
  if (diff === 0) { name = 'Today'; sub = date; }
  else if (diff === 1) { name = 'Tomorrow'; sub = date; }
  else if (diff > 1) rel = `in ${diff} days`;
  else if (diff === -1) rel = 'yesterday';
  else rel = `${-diff} days ago`;
  return `<div class="cal-dayhead">`
    + `<span class="cal-dayname">${escapeHtml(name)}</span>`
    + (sub ? `<span class="cal-daydate">${escapeHtml(sub)}</span>` : '')
    + (rel ? `<span class="cal-dayrel">${escapeHtml(rel)}</span>` : '')
    + `</div>`;
}

/* Idle auto-return: the camera stays up longer (someone is watching a door),
   and the calendar gets planning time. */
function idleReturnMs(view) {
  if (view && view.indexOf('camera') === 0) return 300000;  // camera, camera:<src>
  if (view === 'calendar') return 180000;
  return 90000;
}

/* ---- comfort / air-quality banding -------------------------------------- */
/* Shared out-of-range coloring for the House Climate + Weather cards. Each
   returns a status band the CSS colors: 'ok' (in range, neutral ink), 'warn'
   (edge of range, amber), 'crit' (out of range, red), plus 'good' (actively
   healthy green, UV/AQI only). A missing / non-finite reading returns '' so the
   value shows a dash and carries no color — never a false "in range" green.
   Thresholds are the single source of truth; tune them here.

   A tiny reused guard: is this a real, finite reading? */
function _reading(v) {
  if (v == null || String(v).trim() === '') return null;
  const n = Number(v);
  return isFinite(n) ? n : null;
}

/* Indoor temperature (°F). Comfortable 62–79; the warm edge (80–84) matches the
   old HOT_F=80 line the room grid already warned at. */
function tempBandF(t) {
  const n = _reading(t);
  if (n === null) return '';
  if (n < 58 || n >= 85) return 'crit';
  if (n < 62 || n >= 80) return 'warn';
  return 'ok';
}

/* Indoor relative humidity (%). Ideal 30–55; >60 flags mold risk, <25 too dry. */
function humidityBand(h) {
  const n = _reading(h);
  if (n === null) return '';
  if (n < 25 || n > 60) return 'crit';
  if (n < 30 || n > 55) return 'warn';
  return 'ok';
}

/* EPA UV Index bands: 0–2 low, 3–5 moderate, 6–7 high, 8+ very high/extreme. */
function uvBand(uv) {
  const n = _reading(uv);
  if (n === null) return '';
  if (n >= 8) return 'crit';
  if (n >= 6) return 'warn';
  if (n >= 3) return 'ok';
  return 'good';
}

/* US AQI bands: 0–50 good, 51–100 moderate, 101+ unhealthy. */
function aqiBand(aqi) {
  const n = _reading(aqi);
  if (n === null) return '';
  if (n > 100) return 'crit';
  if (n > 50) return 'warn';
  return 'good';
}

/* Category-text fallbacks. The weather feed sends the number and the category
   as INDEPENDENT fields, so it can label a hazard ("Extreme" / "Unhealthy")
   while omitting the number. The number is authoritative WHEN PRESENT; these
   derive a band from the text so a danger category still colors when the number
   is missing (callers use `uvBand(n) || uvBandText(desc)`). Unrecognized text
   returns '' — never a false 'good'. */
function uvBandText(desc) {
  const s = String(desc == null ? '' : desc).toLowerCase();
  if (!s.trim()) return '';
  if (/extreme|very high|severe/.test(s)) return 'crit';
  if (/high/.test(s)) return 'warn';
  if (/moderate/.test(s)) return 'ok';
  if (/low/.test(s)) return 'good';
  return '';
}
function aqiBandText(cat) {
  const s = String(cat == null ? '' : cat).toLowerCase();
  if (!s.trim()) return '';
  if (/hazardous|unhealthy/.test(s)) return 'crit';   // incl. "very unhealthy", "…sensitive groups"
  if (/moderate/.test(s)) return 'warn';
  if (/good/.test(s)) return 'good';
  return '';
}

/* Fill fraction (0..1) for a severity meter, clamped so an over-max or negative
   reading (or a zero/absent max) can't overflow or divide-by-zero the bar. A
   missing reading fills nothing. */
function clampFrac(v, max) {
  const n = _reading(v);
  if (n === null || !(max > 0)) return 0;
  return Math.max(0, Math.min(1, n / max));
}

/* The wall dims itself overnight (CSS state); the display's own power is a
   Pi-side schedule in Phase 2. Night = 22:00 through 05:59. */
function nightClass(hour) { return (hour >= 22 || hour < 6) ? 'is-night' : ''; }

/* '8/12' style month/day from a 'YYYY-MM-DD...' string. */
function _md(s) {
  const [, m, d] = s.slice(0, 10).split('-').map(Number);
  return `${m}/${d}`;
}

/* 'YYYY-MM-DD' + n days, local-midnight math. */
function addDays(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d + n);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
}

/* The last calendar day an event is visible on. All-day ends are exclusive
   (Google's convention), so the last visible day is end-1. A timed event is
   visible through its end day INCLUSIVE (an overnighter is on both days —
   that's what keeps a still-running event on today's feed), except an end at
   exactly midnight, which belongs to the previous day. */
function lastVisibleDay(ev) {
  const s = (ev.start_ts || '').slice(0, 10);
  if (ev.all_day) return addDays((ev.end_ts || s).slice(0, 10), -1);
  const endTs = ev.end_ts || '';
  let last = endTs.slice(0, 10) || s;
  if (last > s && endTs.slice(11, 16) === '00:00') last = addDays(last, -1);
  return last;
}

/* The list of days an event occupies, start through lastVisibleDay inclusive.
   Capped at 62 days so a malformed span can't hang the wall. */
function expandDays(ev) {
  const s = (ev.start_ts || '').slice(0, 10);
  const last = lastVisibleDay(ev);
  const out = [];
  let d = s;
  while (d <= last && out.length < 62) { out.push(d); d = addDays(d, 1); }
  return out.length ? out : [s];
}

/* Event time span: 'all day' | '8/1 – 8/8 · all day' | '10am – 11:30am'
   | '8/12 10pm – 8/13 6am'. */
function fmtTimeRange(ev) {
  if (ev.all_day) {
    const s = (ev.start_ts || '').slice(0, 10);
    const e = (ev.end_ts || s).slice(0, 10);
    const last = e > s ? addDays(e, -1) : s;
    return last > s ? `${_md(s)} – ${_md(last)} · all day` : 'all day';
  }
  const d1 = (ev.start_ts || '').slice(0, 10);
  const d2 = (ev.end_ts || '').slice(0, 10);
  const t1 = fmtTime(ev.start_ts);
  const t2 = fmtTime(ev.end_ts);
  if (!t2 || (d1 === d2 && t1 === t2)) return t1;
  if (d1 === d2) return `${t1} – ${t2}`;
  return `${_md(ev.start_ts)} ${t1} – ${_md(ev.end_ts)} ${t2}`;
}

/* True when `dayISO` ('YYYY-MM-DD') falls outside the calendar sync window
   `win` ({from, to}, both 'YYYY-MM-DD', inclusive): the actual date range
   the backend's sync caches (see /api/calendar's `window` field). The month
   view can page arbitrarily far past that window; a day out here has no
   cached data to show, so it must not render as a confident empty day (issue
   #37). Plain string compare: ISO 'YYYY-MM-DD' dates sort lexically, so no
   Date parsing (and no local-midnight surprises) is needed. A missing or
   malformed window fails open (never flags a day unsynced): this is a
   display nicety, not a reason to mark up a payload from before the `window`
   field existed, or one that dropped it on a fetch error. */
function isDayOutsideWindow(dayISO, win) {
  if (!win || !win.from || !win.to) return false;
  return dayISO < win.from || dayISO > win.to;
}

/* 42 Sunday-first cells covering `month` (1-12) of `year`, each
   {date:'YYYY-MM-DD', inMonth:bool}. Local-midnight math, tz-independent.
   (Sunday-first per operator request 2026-08-13; was Monday-first.) */
function monthGrid(year, month) {
  const first = new Date(year, month - 1, 1);
  const offset = first.getDay();   // Sun=0 .. Sat=6
  const out = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(year, month - 1, 1 - offset + i);
    out.push({
      date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`,
      inMonth: d.getMonth() === month - 1,
    });
  }
  return out;
}

function shiftMonth(y, m, delta) {
  const n = y * 12 + (m - 1) + delta;
  return { y: Math.floor(n / 12), m: (n % 12 + 12) % 12 + 1 };
}

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];
function monthName(y, m) { return `${MONTH_NAMES[m - 1]} ${y}`; }

/* Effective color for an event: an explicitly colored Google event wins,
   else its calendar's configured color, else quiet gray. */
function eventColor(ev) { return ev.event_color || ev.color || '#8593A9'; }

/* A timed event whose end has passed gets struck through on the wall.
   All-day events never strike (the day itself isn't "over" until it is). */
function eventEnded(ev, nowMs) {
  if (ev.all_day) return false;
  const t = Date.parse(ev.end_ts || ev.start_ts || '');
  return Number.isFinite(t) && t < nowMs;
}

/* Google Calendar descriptions arrive as HTML. Convert to readable plain
   text: breaks/blocks -> newlines, bullets kept, tags stripped, entities
   decoded ONCE (&amp; last so &amp;lt; can't double-decode). The result is
   still escapeHtml'd before it touches the DOM — this is display cleanup,
   not sanitization. */
function descToText(s) {
  let t = String(s || '');
  t = t.replace(/<br\s*\/?>/gi, '\n');
  t = t.replace(/<\/(p|div|li|tr|h[1-6])>/gi, '\n');
  t = t.replace(/<li[^>]*>/gi, '• ');
  t = t.replace(/<[^>]+>/g, '');
  t = t.replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<').replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"').replace(/&#39;|&apos;/gi, "'")
    .replace(/&amp;/gi, '&');
  return t.split('\n').map((line) => line.trim()).join('\n')
    .replace(/\n{3,}/g, '\n\n').trim();
}

/* Fit a vw×vh virtual page into a panel slot `w` wide, capped at `maxH` tall,
   WITHOUT cropping: scale so the whole page fits (never upscaled past 1:1),
   and center any leftover width. Returns {scale, width, height, offsetX}
   where width/height are the scaled content box in CSS px. */
function panelFit(w, vw, vh, maxH) {
  const scale = Math.min(w / vw, maxH / vh, 1);
  return {
    scale,
    width: Math.round(vw * scale),
    height: Math.round(vh * scale),
    offsetX: Math.round((w - vw * scale) / 2),
  };
}

// Zoom factor that fits the fixed 1920px-wide wall onto an arbitrary screen
// (w = viewport width). Returns a CSS `zoom` string, or '' meaning "leave it
// 1:1" (unset the inline zoom). Fits to WIDTH: it fills the screen edge to edge
// horizontally and lets the page scroll vertically if a short window needs it
// (the original desktop behavior). Fitting BOTH width and height instead left
// big empty side borders on any screen whose aspect isn't 16:9 (e.g. a laptop
// whose browser chrome eats height). Only ever scales DOWN, so the exact-1920
// Pi kiosk and any wider screen stay pixel-perfect; a non-positive width (a
// background/just-created tab measures 0) and <=1000px both return '' (never
// collapse to zoom:0; the mobile reflow owns its own layout). Kept here (not in
// hub.js) so the branches are unit-testable, like panelFit above.
function wallZoom(w) {
  if (!w || w <= 1000) return '';
  const scale = w / 1920;
  return scale >= 1 ? '' : String(scale);
}

/* The text transform behind the on-screen keyboard (osk.js): apply one key to
   `value` at the [selStart, selEnd] selection and return the new {value, caret}.
   Kept here (no DOM) so the insert / Backspace / Space + maxlength branches are
   unit-testable, exactly like panelFit above; osk.js's key handler is a thin
   wrapper that reads the live input's value + selection, calls this, and writes
   the result back. `key` is a single character, 'Backspace', or 'Space'.
   `shift` capitalizes a character key (a no-op on digits/symbols); `maxlength`
   (when > 0) refuses an insert that would overflow - setRangeText would NOT
   honor it, so we enforce it here. */
function oskApplyKey(value, selStart, selEnd, key, opts) {
  const v = String(value == null ? '' : value);
  const o = opts || {};
  // Clamp the selection into range so a stale caret can't slice out of bounds.
  const a = Math.max(0, Math.min(selStart | 0, v.length));
  const b = Math.max(a, Math.min(selEnd | 0, v.length));
  if (key === 'Backspace') {
    if (b > a) return { value: v.slice(0, a) + v.slice(b), caret: a };       // drop the selection
    if (a > 0) return { value: v.slice(0, a - 1) + v.slice(a), caret: a - 1 }; // drop the char before the caret
    return { value: v, caret: a };                                            // at position 0: nothing to delete
  }
  const ch = key === 'Space' ? ' ' : (o.shift ? String(key).toUpperCase() : String(key));
  // maxlength only bites on a pure insert: replacing a selection can only keep
  // or shrink the length, so a full field drops the char with the caret unmoved.
  const nextLen = v.length - (b - a) + ch.length;
  if (o.maxlength && o.maxlength > 0 && nextLen > o.maxlength) {
    return { value: v, caret: a };
  }
  return { value: v.slice(0, a) + ch + v.slice(b), caret: a + ch.length };
}

/* Chore form <-> API payload, kept PURE (no DOM) so the admin form's
   serialization is unit-testable. A mis-serialized days_mask or rotation_order
   here would otherwise surface only as a silent 422 or a wrong-schedule chore
   on the wall — the exact untested gap the audit flagged. */
function choreDaysMask(days) {
  // Array.from handles both a Set and an array (and is realm-agnostic, unlike
  // `instanceof Set`, so this stays testable in a vm sandbox).
  const arr = days == null ? [] : Array.from(days);
  return arr.reduce((m, i) => m | (1 << i), 0);
}

/* Form `repeat` ('daily'|'weekly'|'interval'|'once') <-> API schedule_kind
   ('daily'|'days'|'interval'|'once'). Biweekly is NOT its own repeat value: it
   is 'weekly' carrying week_interval 2, the same shape the backend stores (and
   the same shape iOS's EKRecurrenceRule uses — frequency weekly, interval 2). A
   one-time chore is always one person on one date. */
function choreScheduleKind(repeat) {
  if (repeat === 'weekly') return 'days';
  if (repeat === 'interval') return 'interval';
  if (repeat === 'once') return 'once';
  return 'daily';
}

/* interval_days is "every N days" from creation, 1–365. Mirror the server's
   clamp so an out-of-range field is corrected here (and empty/NaN -> null,
   which the server rejects for an interval chore — caught inline before we
   POST, see buildChoreForm's submit guard). */
function clampIntervalDays(v) {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n) || n < 1) return null;
  return Math.min(n, 365);
}

/* due_times drive later phone notifications: up to 6 "HH:MM" (24h) strings.
   Drop anything that isn't a real time, de-dupe, sort ascending (zero-padded
   HH:MM sorts lexicographically), cap at 6 — mirroring the server's rules so
   the payload is clean regardless of what the form collected. */
function normalizeDueTimes(times) {
  const re = /^([01]\d|2[0-3]):([0-5]\d)$/;
  const seen = new Set();
  const out = [];
  (Array.isArray(times) ? times : []).forEach((t) => {
    const v = typeof t === 'string' ? t.trim() : '';
    if (re.test(v) && !seen.has(v)) { seen.add(v); out.push(v); }
  });
  out.sort();
  return out.slice(0, 6);
}

function buildChorePayload(f) {
  const kind = choreScheduleKind(f.repeat);
  const assign = kind === 'once' ? 'fixed' : f.assign;
  return {
    title: (f.title || '').trim(),
    icon: (f.icon || '').trim(),
    schedule_kind: kind,
    days_mask: kind === 'days' ? choreDaysMask(f.days) : 0,
    // Weekly cadence: 2 == biweekly, else weekly. Only 'days' carries it; every
    // other kind sends the neutral 1, the same way days_mask sends 0 off-weekly.
    week_interval: kind === 'days' && Number(f.weekInterval) === 2 ? 2 : 1,
    // Only an 'interval' chore carries a gap; null keeps it off the other kinds
    // (the server would clamp it away, but we don't send noise).
    interval_days: kind === 'interval' ? clampIntervalDays(f.intervalDays) : null,
    // Reminder times apply to any kind (0–6 HH:MM, sorted, de-duped).
    due_times: normalizeDueTimes(f.times),
    assign_kind: assign,
    fixed_person_id: assign === 'fixed'
      ? (f.person === null || f.person === '' || f.person === undefined ? null : Number(f.person))
      : null,
    rotation_order: assign === 'rotation' ? (f.rot || []).slice() : [],
    // The one-time due date; the server ignores it for daily/weekly chores.
    date: kind === 'once' ? (f.date || '') : null,
  };
}

function choreToModel(ch) {
  const days = new Set();
  for (let i = 0; i < 7; i++) if ((ch.days_mask >> i) & 1) days.add(i);
  const repeat = ch.schedule_kind === 'days' ? 'weekly'
    : ch.schedule_kind === 'interval' ? 'interval'
      : ch.schedule_kind === 'once' ? 'once' : 'daily';
  return {
    title: ch.title, icon: ch.icon,
    repeat,
    days,
    // 2 == biweekly; anything else (incl. absent) reads as plain weekly.
    weekInterval: Number(ch.week_interval) === 2 ? 2 : 1,
    // Seed the number field: '' when absent so the input renders empty.
    intervalDays: ch.interval_days == null ? '' : ch.interval_days,
    times: Array.isArray(ch.due_times) ? ch.due_times.slice() : [],
    assign: ch.assign_kind, fixed_person_id: ch.fixed_person_id,
    rot: ch.rotation_order.slice(),
    // For a one-time chore the due date is stored as rotation_epoch.
    date: ch.schedule_kind === 'once' ? (ch.rotation_epoch || '') : '',
  };
}

/* 16 vivid, cheerful hues spanning the wheel — the person-color palette. Each
   is dual-legible: WCAG contrast >= 3:1 against BOTH the light-theme card
   (#FFFFFF) and the dark-theme card (#141A26). See
   tests/test_static.py::test_swatch_hexes_meet_dual_theme_contrast. Used by the
   Chores-page inline people editor. */
const SWATCHES = ['#FA4352', '#F64E06', '#BE7A05', '#978B04',
  '#5B9904', '#049F1E', '#049C6A', '#049E8C',
  '#0594C3', '#3587FA', '#717CFB', '#9371FB',
  '#B95DFB', '#E721F9', '#F928B4', '#FA3C7B'];

/* Paint the swatch picker into `host` and wire taps to `onpick(hex)`. The
   background is a palette hex (never user input); aria-label carries the hex. */
function paintSwatches(host, selected, onpick) {
  host.innerHTML = SWATCHES.map((hx) =>
    `<button class="swatch${hx === selected ? ' selected' : ''}" type="button" `
    + `style="background:${safeColor(hx)}" data-hex="${hx}" aria-label="${escapeHtml(hx)}"></button>`).join('');
  host.onclick = (e) => {
    const b = e.target.closest('.swatch');
    if (b) onpick(b.dataset.hex);
  };
}

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']; // bit 0..6

/* One reusable chore form. `model` seeds it; `onsubmit(body, errEl)` fires with
   the assembled request body; `people` is the full people list (active status
   included) the form needs for the person picker + rotation names. Opened from
   the wall's all-chores edit mode (add + every inline edit). */
function buildChoreForm(host, model, submitLabel, onsubmit, people) {
  host.innerHTML = `
    <div class="field"><label>Title</label>
      <input class="txt-input f-title" maxlength="60" autocomplete="off"></div>
    <div class="field"><label>Emoji (optional)</label>
      <input class="txt-input f-icon" maxlength="4" autocomplete="off"></div>
    <div class="field"><label>Repeat</label>
      <div class="segmented f-repeat">
        <button class="seg-btn" type="button" data-repeat="daily">Daily</button>
        <button class="seg-btn" type="button" data-repeat="weekly">Weekly</button>
        <button class="seg-btn" type="button" data-repeat="interval">Every N days</button>
        <button class="seg-btn" type="button" data-repeat="once">Once</button>
      </div>
      <div class="day-chips f-days"></div>
      <div class="segmented f-weekfreq">
        <button class="seg-btn" type="button" data-weekfreq="1">Every week</button>
        <button class="seg-btn" type="button" data-weekfreq="2">Every 2 weeks</button>
      </div>
      <div class="interval-row f-interval">
        <span class="interval-word">Every</span>
        <input class="txt-input interval-num f-intervaldays" type="number" min="1" max="365" inputmode="numeric" autocomplete="off">
        <span class="interval-word">days</span>
      </div>
      <input class="txt-input f-date" type="date"></div>
    <div class="field"><label>Reminder times</label>
      <div class="time-add">
        <input class="txt-input time-input f-timeinput" type="time" autocomplete="off">
        <button class="time-add-btn f-timeadd" type="button">Add time</button>
      </div>
      <div class="time-list f-times"></div>
      <div class="hint">Optional phone-reminder times, up to 6.</div></div>
    <div class="field"><label>Who does it</label>
      <div class="segmented f-assign">
        <button class="seg-btn" type="button" data-assign="fixed">One person</button>
        <button class="seg-btn" type="button" data-assign="rotation">Rotation</button>
      </div>
      <select class="txt-input f-person"></select>
      <div class="rot-add f-rotadd"></div>
      <div class="rot-list f-rotation"></div>
      <div class="hint f-rothint">Tap people in the order they take turns — repeats are fine (e.g. dishes: Sam, Alex, Riley, Sam…).</div></div>
    <div class="form-error hidden f-error"></div>
    <button class="btn-primary" type="button" data-submit>${escapeHtml(submitLabel)}</button>`;

  const $ = (sel) => host.querySelector(sel);
  $('.f-title').value = model.title || '';
  $('.f-icon').value = model.icon || '';
  $('.f-date').value = model.date || (model.repeat === 'once' ? todayISO() : '');
  $('.f-intervaldays').value = model.intervalDays == null || model.intervalDays === ''
    ? '' : String(model.intervalDays);
  const active = people.filter((p) => p.active);
  $('.f-person').innerHTML = active.map((p) =>
    `<option value="${p.id}"${p.id === model.fixed_person_id ? ' selected' : ''}>${escapeHtml(p.name)}</option>`).join('');

  const paintRepeat = () => {
    $('.f-repeat').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.repeat === model.repeat));
    $('.f-days').classList.toggle('hidden', model.repeat !== 'weekly');
    $('.f-weekfreq').classList.toggle('hidden', model.repeat !== 'weekly');
    $('.f-interval').classList.toggle('hidden', model.repeat !== 'interval');
    $('.f-date').classList.toggle('hidden', model.repeat !== 'once');
  };
  const paintDays = () => {
    $('.f-days').innerHTML = DAY_LABELS.map((d, i) =>
      `<button class="day-chip${model.days.has(i) ? ' selected' : ''}" type="button" data-day="${i}">${d}</button>`).join('');
  };
  const paintWeekFreq = () => {
    const wk = model.weekInterval === 2 ? 2 : 1;
    $('.f-weekfreq').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', Number(b.dataset.weekfreq) === wk));
  };
  // Reminder-time chips render the app's compact time label ("20:00" -> "8pm"),
  // the same fmtTime the calendar uses. The whole chip is the remove button
  // (data-remtime index), mirroring the rotation list's tap-to-remove chips.
  const paintTimes = () => {
    const full = model.times.length >= 6;
    $('.time-add').classList.toggle('hidden', full);
    $('.f-times').innerHTML = model.times.length
      ? model.times.map((t, i) =>
        `<button class="time-chip" type="button" data-remtime="${i}">${escapeHtml(fmtTime('T' + t))} ✕</button>`).join('')
      : `<div class="hint">No reminder times yet.</div>`;
  };
  const paintAssign = () => {
    // A one-time chore is always one person — hide the Rotation choice entirely
    // and pin the assignment to a fixed person.
    const once = model.repeat === 'once';
    const rotBtn = $('.f-assign').querySelector('[data-assign="rotation"]');
    if (rotBtn) rotBtn.classList.toggle('hidden', once);
    const assign = once ? 'fixed' : model.assign;
    $('.f-assign').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.assign === assign));
    $('.f-person').classList.toggle('hidden', assign !== 'fixed');
    $('.f-rotadd').classList.toggle('hidden', assign !== 'rotation');
    $('.f-rotation').classList.toggle('hidden', assign !== 'rotation');
    $('.f-rothint').classList.toggle('hidden', assign !== 'rotation');
  };
  const paintRotation = () => {
    $('.f-rotadd').innerHTML = active.map((p) =>
      `<button class="day-chip" type="button" data-add="${p.id}">＋ ${escapeHtml(p.name)}</button>`).join('');
    $('.f-rotation').innerHTML = model.rot.length
      ? model.rot.map((id, idx) => {
        const p = people.find((x) => x.id === id);
        return `<button class="rot-item" type="button" data-rem="${idx}">${idx + 1}. ${escapeHtml(p ? p.name : '?')} ✕</button>`;
      }).join('')
      : `<div class="hint">No turns yet — add people above.</div>`;
  };
  paintRepeat(); paintDays(); paintWeekFreq(); paintTimes(); paintAssign(); paintRotation();

  $('.f-repeat').onclick = (e) => {
    const b = e.target.closest('[data-repeat]');
    if (!b) return;
    model.repeat = b.dataset.repeat;
    if (model.repeat === 'once') {
      model.assign = 'fixed';                       // one-time is one person
      if (!$('.f-date').value) $('.f-date').value = todayISO();
    }
    // Seed a sensible cadence the first time "Every N days" is chosen so the
    // field is never empty (empty would fail the server's interval rule).
    if (model.repeat === 'interval' && !$('.f-intervaldays').value) $('.f-intervaldays').value = '2';
    paintRepeat();
    paintAssign();                                  // rotation choice shows/hides with once
  };
  $('.f-days').onclick = (e) => {
    const b = e.target.closest('[data-day]');
    if (!b) return;
    const i = Number(b.dataset.day);
    if (model.days.has(i)) model.days.delete(i); else model.days.add(i);
    paintDays();
  };
  $('.f-weekfreq').onclick = (e) => {
    const b = e.target.closest('[data-weekfreq]');
    if (!b) return;
    model.weekInterval = Number(b.dataset.weekfreq) === 2 ? 2 : 1;
    paintWeekFreq();
  };
  $('.f-timeadd').onclick = () => {
    const v = ($('.f-timeinput').value || '').trim();
    if (!/^([01]\d|2[0-3]):([0-5]\d)$/.test(v)) return;   // empty/partial: no-op
    if (!model.times.includes(v) && model.times.length < 6) {
      model.times.push(v);
      model.times.sort();
    }
    $('.f-timeinput').value = '';
    paintTimes();
  };
  $('.f-times').onclick = (e) => {
    const b = e.target.closest('[data-remtime]');
    if (!b) return;
    model.times.splice(Number(b.dataset.remtime), 1);
    paintTimes();
  };
  $('.f-assign').onclick = (e) => {
    const b = e.target.closest('[data-assign]');
    if (b) { model.assign = b.dataset.assign; paintAssign(); }
  };
  $('.f-rotadd').onclick = (e) => {
    const b = e.target.closest('[data-add]');
    if (!b) return;
    model.rot.push(Number(b.dataset.add));
    paintRotation();
  };
  $('.f-rotation').onclick = (e) => {
    const b = e.target.closest('[data-rem]');
    if (!b) return;
    model.rot.splice(Number(b.dataset.rem), 1);
    paintRotation();
  };
  $('[data-submit]').onclick = () => {
    // Serialization lives in buildChorePayload above (pure, tested).
    const assign = model.repeat === 'once' ? 'fixed' : model.assign;
    const err = $('.f-error');
    err.classList.add('hidden');
    // Mirror the server's "interval needs 1–365" rule for an inline message
    // instead of a generic toast (clampIntervalDays would send null otherwise).
    if (model.repeat === 'interval' && clampIntervalDays($('.f-intervaldays').value) === null) {
      err.textContent = 'Enter how many days between (1–365).';
      err.classList.remove('hidden');
      return;
    }
    const body = buildChorePayload({
      title: $('.f-title').value,
      icon: $('.f-icon').value,
      repeat: model.repeat,
      days: model.days,
      weekInterval: model.weekInterval,
      intervalDays: $('.f-intervaldays').value,
      times: model.times,
      assign,
      person: assign === 'fixed' ? $('.f-person').value : null,
      rot: model.rot,
      date: $('.f-date').value,
    });
    onsubmit(body, err);
  };
}

/* Today as 'YYYY-MM-DD' in the viewer's local zone — the default due date for a
   new one-time chore (the household and display share one timezone). */
function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function freshChoreModel() {
  return { title: '', icon: '', repeat: 'daily', days: new Set(),
    weekInterval: 1, intervalDays: '', times: [],
    assign: 'fixed', fixed_person_id: null, rot: [], date: '' };
}

function freshPersonModel() {
  return { name: '', color: SWATCHES[0] };
}

/* The PATCH body that maps a person to an iCloud chore list. The picker's
   "— none —" option is value '', which clears the mapping to null; any real
   list id maps straight through. Pure so the null-clear is unit-testable
   (a wrong body would silently mis-map or 422). */
function reminderListBody(value) {
  return { reminder_list_id: value ? value : null };
}

/* The iCloud-list mapping block for the person editor (edit mode only — a new
   person has no id to PATCH yet). `opts` carries {reminderLists, reminderListId,
   twoWay}. With lists it renders a picker + the one-time sharing note; with no
   lists it renders a single "connect iCloud" line instead of a dead dropdown.
   Kept as an HTML string so buildPersonForm can drop it into the same template
   pass (the live wiring + PATCH is added after). */
function mirrorFieldHtml(opts) {
  if (!opts || !opts.edit) return '';
  const lists = opts.reminderLists || [];
  const listId = opts.reminderListId || '';
  if (!lists.length) {
    return `<div class="field"><label>iCloud chore list</label>`
      + `<div class="hint" data-plist-empty>Connect iCloud in Settings to mirror this person’s chores to a list.</div></div>`;
  }
  const options = `<option value=""${listId ? '' : ' selected'}>— none —</option>`
    + lists.map((l) =>
      `<option value="${escapeHtml(l.id)}"${l.id === listId ? ' selected' : ''}>${escapeHtml(l.name)}</option>`).join('');
  const offNow = !!listId && !opts.twoWay;
  return `<div class="field"><label>iCloud chore list</label>`
    + `<select class="txt-input" data-plist>${options}</select>`
    + `<div class="hint" data-plist-share>Chores are written to this person’s list in the hub’s iCloud account. To see them on their own iPhone, share that list to their Apple ID once from iCloud Reminders (open the list → Share List).</div>`
    + `<div class="hint${offNow ? '' : ' hidden'}" data-plist-readonly>Two-way sync is off, so chores won’t reach iCloud yet. Turn it on in Settings → iCloud.</div>`
    + `<div class="form-error hidden" data-plist-err></div></div>`;
}

/* One reusable person form (name + color swatches) for the Chores-page inline
   people editor. `model` seeds it; `onsubmit(body, errEl)` fires with
   {name, color}. `opts` (edit mode) adds the iCloud-list picker: it PATCHes
   live via `opts.onListChange(value)` — resolving false to revert + show an
   inline error — separate from the name/color Save. DOM hooks are data-*
   attributes (not classes) so the form needs no styling of its own — it reuses
   .txt-input / .swatches / .form-error / .btn-primary. */
function buildPersonForm(host, model, submitLabel, onsubmit, opts) {
  host.innerHTML = `
    <div class="field"><label>Name / nickname</label>
      <input class="txt-input" data-pname maxlength="30" autocomplete="off"></div>
    <div class="field"><label>Color</label><div class="swatches" data-pswatches></div></div>
    ${mirrorFieldHtml(opts)}
    <div class="form-error hidden" data-perror></div>
    <button class="btn-primary" type="button" data-psubmit>${escapeHtml(submitLabel)}</button>`;
  const $ = (sel) => host.querySelector(sel);
  $('[data-pname]').value = model.name || '';
  const paint = () => paintSwatches($('[data-pswatches]'), model.color,
    (hx) => { model.color = hx; paint(); });
  paint();

  // Live iCloud-list mapping (edit mode with lists available). The select
  // PATCHes on change and reflects the two-way-off note against the current
  // selection; a failed PATCH reverts to the last committed value.
  const sel = $('[data-plist]');
  if (sel && opts && opts.onListChange) {
    const twoWay = !!opts.twoWay;
    let committed = opts.reminderListId || '';
    const paintReadonly = () => {
      const el = $('[data-plist-readonly]');
      if (el) el.classList.toggle('hidden', !(sel.value && !twoWay));
    };
    sel.onchange = async () => {
      const errEl = $('[data-plist-err]');
      if (errEl) errEl.classList.add('hidden');
      const value = sel.value;
      sel.disabled = true;
      const ok = await opts.onListChange(value);
      sel.disabled = false;
      if (ok === false) {
        sel.value = committed;   // PATCH failed: undo the visible selection
        if (errEl) { errEl.textContent = 'Couldn’t update the list — check the hub and try again.'; errEl.classList.remove('hidden'); }
      } else {
        committed = value;
      }
      paintReadonly();
    };
  }

  $('[data-psubmit]').onclick = () =>
    onsubmit({ name: $('[data-pname]').value.trim(), color: model.color }, $('[data-perror]'));
}

/* Attempt a chore check-off/uncheck; returns true on success, false if the
   write failed. Separated from the DOM (hub.js toggleChore shows a toast when
   this returns false) so the failure-detection half is testable — a persistent
   write failure must be DETECTED, not swallowed. */
async function attemptToggle(id, done) {
  try {
    await j(`/api/chores/${id}/complete`, { method: done ? 'DELETE' : 'POST' });
    return true;
  } catch (e) {
    return false;
  }
}

/* Attempt a to-do write (add / move / complete / delete); resolves to
   {ok: true} on success, {ok: false, error: e.message} on failure — never
   throws. Same testable failure-detection contract as attemptToggle: the
   caller shows a toast on !ok. Carrying the error message (j() surfaces the
   server's detail string, e.g. "unknown todo") lets the caller distinguish a
   concurrent-edit 404 from a generic failure via todoFailMessage below. */
async function attemptTodo(path, method, body) {
  try {
    await j(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/* Turn an attemptTodo failure's error string into the right toast copy: a
   404 "unknown todo" means someone on another device already deleted/changed
   the item (a shared list's most common failure), which is a different
   diagnosis than "check the hub and tap again". Pure so the branch is
   testable without a DOM. */
function todoFailMessage(error) {
  // 'unknown todo' must match the detail string app.py's _todo_row raises on a
  // 404 — the JS test pins this exact substring.
  if (String(error || '').includes('unknown todo')) {
    return 'That item was already changed on another device.';
  }
  return 'Couldn’t save — check the hub and tap again.';
}

/* Same shape as todoFailMessage, for the iCloud reminder writes. The write
   endpoints answer 409 when iCloud is off or still read-only, and 404 when the
   reminder was changed/deleted on another device since the wall last read it —
   two different diagnoses. Substrings match app.py's own detail strings
   ('read-only', 'not connected', 'unknown reminder'); pure so each branch is
   testable without a DOM. */
function reminderFailMessage(error) {
  const s = String(error || '');
  if (s.includes('read-only')) {
    return 'Reminders are read-only — switch Sync direction to 2-way in Settings.';
  }
  if (s.includes('not connected')) {
    return 'iCloud isn’t connected — add it in Settings.';
  }
  // 'unknown reminder list' (a 404 from add when the target list was deleted/
  // disabled on iCloud) MUST be tested before 'unknown reminder' — the former
  // contains the latter as a substring, so the order is load-bearing: a bare
  // 'unknown reminder' check would misread a dead-list add as an edit collision.
  if (s.includes('unknown reminder list')) {
    return 'That list is no longer available — pick another in Settings.';
  }
  if (s.includes('unknown reminder')) {
    return 'That reminder was already changed on another device.';
  }
  return 'Couldn’t save — check the hub and tap again.';
}

/* The calendar-status banner message ('' = no banner). Pure so the
   needs_auth / not-connected / generic branches are testable without a DOM;
   hub.js's calStatusNote wraps the result in markup. */
function calStatusMessage(status) {
  const st = status || {};
  if (st.needs_auth) {
    // A revoked/expired calendar sign-in needs the owner to reconnect it (in the
    // settings menu), unlike a transient blip (the generic message below). The
    // hub now has several calendar sources, so this copy stays source-neutral.
    // This fires even when st.ok is true: on a mixed setup one source can be
    // healthy while another's sign-in expired, and that expiry must stay visible
    // rather than hide behind the working source.
    return st.ok
      ? 'A calendar sign-in expired — reconnect it in settings.'
      : 'A calendar sign-in expired — reconnect it in settings. Showing the last events we saw.';
  }
  if (st.degraded) {
    // A source has been failing to sync for a while (not an auth problem — a
    // stuck feed, not a momentary blip). Tell the family their calendar may be
    // behind, without the flicker of a banner on every transient hiccup. Shown
    // even when st.ok is true (another source is still healthy and rendering).
    return 'A calendar is having trouble syncing — showing the last events we saw.';
  }
  if (st.ok !== false) return '';
  if (String(st.error || '').includes('not configured')) {
    return 'No calendar is connected yet — add one in settings and the family’s events show up here.';
  }
  return 'Calendar sync hit a snag — showing the last events we saw.';
}

/* The iCloud CalDAV "Test connection" result, turned into one line of display
   copy. Pure so the ok / needs_auth / error branches are testable without a
   DOM; hub.js's renderCaldavPanel escapes and shows the result. Mirrors the
   POST /api/integrations/icloud_caldav/test contract (caldav_sync.sync_once):
   {ok:true, events, reminders} | {needs_auth:true, ...} | {ok:false, error}. */
function caldavTestMessage(result) {
  const r = result || {};
  if (r.needs_auth) return 'Sign-in rejected - check the app-specific password.';
  if (r.ok) {
    const events = Number.isFinite(r.events) ? r.events : 0;
    const reminders = Number.isFinite(r.reminders) ? r.reminders : 0;
    return `Connected - ${events} event${events === 1 ? '' : 's'}, `
      + `${reminders} reminder${reminders === 1 ? '' : 's'}.`;
  }
  if (r.error) return String(r.error);
  return 'Couldn’t connect - check the hub and try again.';
}

/* The "Calendars" sub-section inside the connected iCloud (CalDAV) panel: one
   row per discovered calendar / reminder list, each with its own visibility
   toggle. Reuses the same .integ-row / .integ-switch / role="switch" /
   aria-checked markup as the top-level Integrations list above it, so it
   reads as the same control, not a bespoke one. Pure (no DOM) so it's
   unit-testable; hub.js's caldavPanelHtml (below) embeds the result, and
   renderCaldavPanel wires the toggle taps (data-caldav-collection-toggle)
   through hub.js's delegated click listener.
   `collections` is the icloud_caldav collections list from GET
   /api/integrations/icloud_caldav/collections - each {id, name,
   color(string|null), comp_type('VEVENT'|'VTODO'), enabled} - or
   null/undefined before the first fetch resolves, which renders the same
   empty state as a genuinely empty account (this panel has no separate
   "loading" state of its own). `opts.error` (set when hub.js's
   fetchCaldavCollections's GET failed) swaps the EMPTY-list message for a
   distinct "couldn't load" one, so a real fetch failure can never be
   confused with an account that truly has zero calendars - see
   fetchCaldavCollections's comment for why that distinction matters. */
function caldavCollectionsHtml(collections, opts) {
  const list = Array.isArray(collections) ? collections : [];
  if (!list.length) {
    const msg = (opts && opts.error)
      ? 'Couldn’t load calendars - try Test connection'
      : 'No calendars found yet - try Test connection';
    return `<div class="integ-empty">${msg}</div>`;
  }
  return list.map((c) => {
    const kind = c.comp_type === 'VTODO' ? 'Reminders' : 'Calendar';
    // A null color (some iCloud calendars/lists don't set one) omits the dot
    // rather than faking a neutral one. safeColor (above) keeps a stray
    // server value from injecting extra CSS through this style attribute.
    const dot = c.color
      ? `<span class="caldav-cal-dot" style="background:${safeColor(c.color)}" aria-hidden="true"></span>`
      : '';
    return `<button class="integ-row" type="button" role="switch"`
      + ` aria-checked="${c.enabled ? 'true' : 'false'}"`
      + ` data-caldav-collection-toggle="${escapeHtml(String(c.id))}">`
      + `<span class="integ-name">${dot}${escapeHtml(c.name)}`
      + `<span class="caldav-cal-kind">${kind}</span>`
      + `</span>`
      + `<span class="integ-switch${c.enabled ? ' on' : ''}" aria-hidden="true"></span>`
      + `</button>`;
  }).join('');
}

/* The iCloud (CalDAV) settings panel body, pure (no DOM) so both branches are
   unit-testable: hub.js's renderCaldavPanel assigns the result to a host's
   innerHTML and wires the buttons/inputs.
   `integ` is the icloud_caldav entry from /api/hub's `integrations` list, or
   null/undefined when no credentials are stored yet (the not-connected form).
   `ui` is the transient view state hub.js keeps between polls: {connecting,
   testing, testResult, formError, collections, collectionsError}, NEVER the
   password itself. The password only ever lives in the password input's own
   value; it is read at submit time, sent once in the POST body, and never
   stored in JS state, a DOM attribute/dataset, or a log/toast. */
function caldavPanelHtml(integ, ui) {
  const st = ui || {};
  // A bare HTML "disabled" attribute, not a CSS class fragment. Written as a
  // helper returning a plain, unspaced word (no leading space inside the
  // quoted literal) rather than inlined the way the conditional CSS classes
  // below are (integ-switch's on/off, seg-btn's active/inactive): the static
  // "every referenced class is styled" scan (tests/test_static.py) treats
  // any ternary shaped like that inline pattern as a conditional CSS class
  // needing a matching selector, which is right for those but would be a
  // false positive for an HTML attribute that merely looks the same shape.
  const dis = (b) => (b ? 'disabled' : '');
  if (!integ) {
    return `<div class="field"><label>Apple ID</label>`
      + `<input class="txt-input" id="caldav-user-input" type="text" `
      + `autocomplete="off" autocapitalize="off" spellcheck="false" ${dis(st.connecting)}></div>`
      + `<div class="field"><label>App-specific password</label>`
      + `<input class="txt-input" id="caldav-pw-input" type="password" `
      + `autocomplete="off" ${dis(st.connecting)}></div>`
      + (st.formError ? `<div class="form-error">${escapeHtml(st.formError)}</div>` : '')
      + `<button class="btn-primary" type="button" data-caldav-connect ${dis(st.connecting)}>`
      + `${st.connecting ? 'Connecting…' : 'Connect'}</button>`
      + `<div class="hint">A dedicated bot Apple ID + an app-specific password `
      + `(appleid.apple.com). Stored on this device only, never shared.</div>`;
  }
  // Same status vocabulary as renderIntegrations: a revoked/expired sign-in
  // or a sync error is first-class, shown right on the account row.
  //
  // Deliberately NO second enable/disable switch here: the generic
  // Integrations list one section up (#integrations-ctl, renderIntegrations)
  // already has one for icloud_caldav, wired straight through poll() ->
  // renderIntegrations(), which redraws on every 60s poll. A second switch
  // in THIS panel would need its own repaint after every action that can
  // change it (poll() deliberately never calls renderCaldavPanel; see that
  // function's comment) and would drift out of sync with the first one the
  // moment either went stale: two controls for one boolean, able to
  // disagree, is worse than one. Surface only what the generic row can't:
  // the account identity and the health badge.
  const warn = integ.status === 'needs_auth' ? 'reconnect'
    : (integ.status === 'error' ? 'error' : '');
  const readonly = integ.readonly !== false;   // server default is 1-way (true)
  const resultMsg = st.testResult ? caldavTestMessage(st.testResult) : '';
  const resultCls = st.testResult ? (st.testResult.ok ? ' ok' : ' err') : '';
  // Wall edits (reminder check-off/add/delete) queue in an outbox and flush on
  // the next sync. Normally 0; a non-zero count means writes are still waiting
  // to reach iCloud (a paused sync, a transient outage). Invisible when 0/absent
  // so the panel stays quiet in the common case.
  const pending = Number(integ.pending) || 0;
  const pendingNote = pending > 0
    ? `<div class="caldav-pending">${pending} change${pending === 1 ? '' : 's'} not yet synced</div>`
    : '';
  return `<div class="caldav-account">Connected as <strong>${escapeHtml(integ.account || 'unknown')}</strong>`
    + (warn ? `<span class="integ-warn">${warn}</span>` : '')
    + `</div>`
    + pendingNote
    + `<div class="settings-row">`
    + `<span class="settings-k">Sync direction</span>`
    + `<div class="segmented" role="group" aria-label="Sync direction">`
    + `<button class="seg-btn${readonly ? ' active' : ''}" type="button" data-caldav-readonly="1">1-way (read-only)</button>`
    + `<button class="seg-btn${readonly ? '' : ' active'}" type="button" data-caldav-readonly="0">2-way (write back)</button>`
    + `</div></div>`
    + `<div class="caldav-actions">`
    + `<button class="padmin-btn" type="button" data-caldav-test ${dis(st.testing)}>`
    + `${st.testing ? 'Testing…' : 'Test connection'}</button>`
    + `<button class="padmin-btn padmin-del" type="button" data-caldav-disconnect>Disconnect</button>`
    + `</div>`
    + (resultMsg ? `<div class="caldav-test-result${resultCls}">${escapeHtml(resultMsg)}</div>` : '')
    + `<div class="caldav-collections">`
    + `<div class="settings-k">Calendars</div>`
    + `<div class="hint">Pick which iCloud calendars and reminder lists show on the wall.</div>`
    + `<div class="caldav-collections-list">`
    + `${caldavCollectionsHtml(st.collections, { error: st.collectionsError })}</div>`
    + `</div>`;
}
