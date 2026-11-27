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
      const err = new Error((typeof detail === 'string' && detail) || `${url} -> HTTP ${r.status}`);
      err.status = r.status;   // lets a caller tell "refused" (4xx) from "down"
      err.detail = detail;     // a structured refusal (the parent PIN's {pin: ...})
      throw err;
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

/* Is `url` answering? For the camera snapshot probes in hub.js, which only
   need the status, not the image. Same AbortController-timeout guard as j()
   above: without it, a raw fetch to a connected-but-unresponsive server never
   resolves, and probes stack up until the browser's ~6-connection-per-origin
   budget is exhausted on a multi-day kiosk uptime. After reading .ok the body
   is cancelled: every 30s probe used to download a whole snapshot nobody
   read. The abort timer stays armed until that cancel is done, so a server
   that sends headers and then stalls can't hold the socket either. Rejects
   on a network failure or timeout, like fetch. Same platform guard as j():
   no timer when AbortController isn't available (the vm test sandbox). */
async function probeOk(url, ms = J_TIMEOUT_MS) {
  const ac = typeof AbortController === 'undefined' ? null : new AbortController();
  const timer = ac ? setTimeout(() => ac.abort(), ms) : null;
  try {
    const r = await (ac ? fetch(url, { signal: ac.signal }) : fetch(url));
    const ok = !!r.ok;
    if (r.body && typeof r.body.cancel === 'function') {
      try { await r.body.cancel(); } catch (e) { /* already closed or aborted */ }
    }
    return ok;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
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

/* Backup-health header badge. Pure: maps the /api/hub `backup` block to a
   descriptor {show, level, text, title}. Hidden while the backup is healthy,
   still 'unknown' (no heartbeat recorded yet), or the payload is missing;
   amber once a KNOWN backup has gone stale, or when an off-box mirror is
   configured and its last copy failed or is too old (a fresh local snapshot
   alone used to read healthy while every NAS copy failed). */
function fmtBackupAge(s) {
  if (s == null) return '—';
  const h = Math.round(s / 3600);
  return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
}
function backupBadge(status) {
  if (!status || !status.known) return { show: false };
  const limit = fmtBackupAge(status.threshold_s);
  if (status.stale) {
    const age = fmtBackupAge(status.age_s);
    return {
      show: true,
      level: 'warn',
      text: `⚠ Backup stale (${age})`,
      title: `No successful backup in ${age} (warns past ${limit})`,
    };
  }
  const r = status.remote;
  if (!r || (!r.failing && !r.stale)) return { show: false };
  const lastGood = r.last_ok
    ? `last good off-box copy ${fmtBackupAge(r.age_s)} ago`
    : 'an off-box copy has never succeeded';
  return {
    show: true,
    level: 'warn',
    text: r.failing ? '⚠ Off-box backup failing' : `⚠ Off-box backup stale (${fmtBackupAge(r.age_s)})`,
    title: `${r.failing ? 'The last off-box copy failed' : `No off-box copy in ${limit}`}; ${lastGood}. The local backup is fine.`,
  };
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
    + `<span class="cal-daynum num">${dd}</span>`
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
  // Settings holds the How it works guide: long enough to read a topic
  // without the page closing under you.
  if (view === 'settings') return 300000;
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

/* When the screen dims (body.is-night, the CSS night state). Two reasons:
   the household dim hours (/api/hub `dim.hours`, one setting for every
   screen, 10pm to 6am until someone changes it) and, on the wall, nobody
   being home (`dim.away`, read from Home Assistant by the server; see
   dimming.py). The display's own power is a Pi-side schedule. */
const DIM_DEFAULT = Object.freeze({ on: true, start: '22:00', end: '06:00' });

/* 'HH:MM' (24-hour) as minutes after midnight, else null. Pure. */
function hmMinutes(s) {
  const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(typeof s === 'string' ? s : '');
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

/* Is `mins` (after midnight) inside the dim window? The end is exclusive, and
   a window may cross midnight (22:00 to 06:00) or not (13:00 to 15:00). The
   same rule as dimming.in_window. Pure. */
function inDimWindow(mins, hours) {
  if (!hours || hours.on !== true) return false;
  const s = hmMinutes(hours.start);
  const e = hmMinutes(hours.end);
  if (s === null || e === null || s === e) return false;
  return s < e ? (mins >= s && mins < e) : (mins >= s || mins < e);
}

function nightClass(hour, minute = 0, hours = DIM_DEFAULT) {
  return inDimWindow(hour * 60 + minute, hours) ? 'is-night' : '';
}

/* The dim hours from a hub payload, or the default when it has none (before
   the first poll, an older hub) or they don't make sense. Pure. */
function dimHoursOf(data) {
  const h = data && data.dim && data.dim.hours;
  if (!h || typeof h.on !== 'boolean' || hmMinutes(h.start) === null
      || hmMinutes(h.end) === null || h.start === h.end) return DIM_DEFAULT;
  return h;
}

/* How long a nobody-home answer is trusted after it arrived: the server's own
   limit (dimming.FRESH_S, 210 s). Past it the hub may be unreachable, and a
   wall must not stay dim on an answer it can no longer refresh. */
const AWAY_FRESH_MS = 210 * 1000;

/* Should this screen be dim now? `phone` is true on the phone layout, which
   never dims for nobody home: a phone that sees an empty house is almost
   always in someone's hand away from it. Only the server's own dim flag,
   with the switch on, in a payload that arrived (`receivedAt`, epoch ms)
   within AWAY_FRESH_MS, dims for that; anything unclear leaves it bright.
   The dim hours need no hub at all: they run from this screen's clock. */
function wallDimmed(d, data, phone, receivedAt) {
  if (nightClass(d.getHours(), d.getMinutes(), dimHoursOf(data)) === 'is-night') return true;
  const away = data && data.dim && data.dim.away;
  if (phone || !away || away.enabled !== true || away.dim !== true) return false;
  return typeof receivedAt === 'number' && d.getTime() - receivedAt <= AWAY_FRESH_MS;
}

/* '22:00' -> '10pm', '21:30' -> '9:30pm', '00:00' -> 'midnight'. Pure. */
function hmWords(s) {
  const m = hmMinutes(s);
  if (m === null) return '';
  if (m === 0) return 'midnight';
  if (m === 720) return 'noon';
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h % 12 || 12}${mm ? `:${String(mm).padStart(2, '0')}` : ''}${h < 12 ? 'am' : 'pm'}`;
}

/* '10pm to 6am', or '' when the dim hours are off. Pure. */
function dimHoursWords(hours) {
  if (!hours || hours.on !== true) return '';
  return `${hmWords(hours.start)} to ${hmWords(hours.end)}`;
}

/* The Settings line under the dim hours about nobody being home, from the
   hub's `dim.away` block. '' on a hub with no Home Assistant. Pure. */
function dimAwayLine(away) {
  if (!away) return '';
  if (away.enabled !== true) {
    return 'The wall can also dim while nobody is home. Turn on Dim when nobody is home under Features & integrations.';
  }
  switch (away.state) {
    case 'away': return 'Home Assistant says nobody is home, so the wall is dim.';
    case 'confirming': return 'Home Assistant says nobody is home. The wall dims if it still says so when it asks once more, in a minute.';
    case 'stopped': return 'The hub stopped checking who is home, so the wall won’t dim for this. Restarting the hub starts it again.';
    case 'home': {
      const n = Number(away.home) || 0;
      return `Home Assistant says ${n} ${n === 1 ? 'person is' : 'people are'} home, so the wall stays bright.`;
    }
    case 'unreadable': return 'Home Assistant couldn’t be read, so the wall won’t dim for nobody home until it can.';
    case 'needs_auth': return 'Home Assistant won’t answer without its token. Set HA_TOKEN on the hub to use this.';
    default: return 'Asking Home Assistant who is home.';
  }
}

/* The <option>s for a dim time picker: every half hour, plus the saved time
   when it is off that grid (set through the API), with it selected. Pure. */
function dimTimeOptions(sel) {
  const times = [];
  for (let m = 0; m < 1440; m += 30) {
    times.push(`${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`);
  }
  if (hmMinutes(sel) !== null && !times.includes(sel)) {
    times.push(sel);
    times.sort((a, b) => hmMinutes(a) - hmMinutes(b));
  }
  return times.map((t) => `<option value="${t}"${t === sel ? ' selected' : ''}>${hmWords(t)}</option>`).join('');
}

/* The Dim hours rows of the Settings Display card. Pure. */
function dimCardHtml(data) {
  const h = dimHoursOf(data);
  const away = data && data.dim ? data.dim.away : null;
  const lock = h.on ? '' : ' disabled';
  const words = h.on ? `Every screen in the house dims from ${dimHoursWords(h)}.`
    : 'No screen dims for the time of day.';
  const awayLine = dimAwayLine(away);
  return `<div class="settings-row">${tipLabelHtml('Dim hours')}`
    + '<div class="seg" role="group" aria-label="Dim hours">'
    + `<button type="button" data-dim-set="on"${h.on ? ' class="on"' : ''}>On</button>`
    + `<button type="button" data-dim-set="off"${h.on ? '' : ' class="on"'}>Off</button>`
    + '</div>'
    + '<div class="dim-times">'
    + `<label>From <select class="txt-input" data-dim-start aria-label="Dim from"${lock}>${dimTimeOptions(h.start)}</select></label>`
    + `<label>to <select class="txt-input" data-dim-end aria-label="Dim until"${lock}>${dimTimeOptions(h.end)}</select></label>`
    + '</div>'
    + `<div class="settings-sub">${escapeHtml(words)}</div>`
    + (awayLine ? `<div class="settings-sub dim-away">${escapeHtml(awayLine)}</div>` : '')
    + '</div>';
}

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

/* A window that vouches for NOTHING: `to` is the day BEFORE `from`, so every
   day, today included, falls outside it and hatches "not synced".

   Used wherever the real synced range is unknown, because the alternative is
   isDayOutsideWindow's fail-open, which renders an unknown range as a free
   calendar — the one thing this surface must never do. Both callers reach it on
   paths that really happen: a fetch that failed with nothing cached, and the
   calendar overlay's very first paint before any fetch has resolved. */
function emptyWindow(todayStr) {
  return { from: todayStr, to: addDays(todayStr, -1) };
}

/* The calendar payload to fall back to when a /api/calendar fetch fails. Pure
   (the caller passes today) so every branch is testable without a network.

   With a PREVIOUS payload: keep its events and window, but downgrade the status
   so the "showing the last events we saw" banner fires instead of painting
   hours-old events as current.

   With NOTHING cached there is no window either — and isDayOutsideWindow fails
   open on a missing one, so every day the family paged to would read "nothing
   scheduled": a confidently-empty year, growing with the sync window. Return an
   EMPTY window instead (to = the day BEFORE from, so even today falls outside
   it) and every day hatches "not synced", which is the truth: we know nothing
   about any of them. `nothing_cached` lets the banner drop its promise of
   cached events.

   `message` keeps the real reason instead of relabelling everything
   'unreachable' — j() digs the API's `detail` out of a non-2xx body, so a 422
   (this fetch drifting past the endpoint's ceiling) stays distinguishable from a
   dead network. It rides the payload and the console for whoever diagnoses it;
   the wall's own copy stays deliberately non-technical. */
function failedCalWindow(prev, message, todayStr) {
  // Whether anything is cached is a fact about the PAYLOAD, not about which
  // failure this is, so recompute it every call. On the second consecutive
  // failure `prev` is the empty payload the first one produced: spreading a
  // fresh status over it without re-deriving the flag silently went back to
  // promising "the last events we saw" with none to show.
  const cached = !!(prev && prev.events && prev.events.length);
  const status = { ok: false, error: message || 'unreachable' };
  if (!cached) status.nothing_cached = true;
  if (prev) return { ...prev, status };
  return { status, events: [], window: emptyWindow(todayStr) };
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

/* Text color that reads on a filled event bar of `color`. Google-style solid
   bars carry dark text on the mid-tone calendar palette; a dark custom color
   (navy, maroon) flips to light so the title never disappears. A non-hex
   color (a named color, or the `transparent` safeColor hands back for a
   malformed one) can't be measured, so the text falls back to the page ink —
   readable on whatever surface shows through. */
function inkFor(color) {
  const m = /^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/.exec(String(color || '').trim());
  if (!m) return 'var(--ink)';
  let h = m[1];
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum < 0.45 ? '#F4F6FB' : '#0B1020';
}

/* Which days of a span an event covers: {first, last} 'YYYY-MM-DD'. Clamped
   so last is never before first: the sync layer lets an all-day event arrive
   with end == start (Google omits the end on some birthdays), which the
   exclusive-end rule would otherwise turn into a negative span — the same
   one-day fallback expandDays makes. */
function eventSpan(ev) {
  const first = (ev.start_ts || '').slice(0, 10);
  let last = lastVisibleDay(ev);
  if (last < first) last = first;
  return { first, last };
}

/* Lay a week's events out in lanes, Google-Calendar style. `days` is the
   week's 7 'YYYY-MM-DD' strings in order. Returns one item per event that
   touches the week: {ev, c0, c1, lane, contL, contR, bar} where c0..c1 are the
   0-based columns it covers inside this week, contL/contR say it continues
   into the previous/next week (drawn as a clipped edge), and `bar` is true
   for an all-day or multi-day event (drawn as a spanning bar) vs. a single
   timed event (drawn as dot + time + title).
   Lane assignment is first-fit over column occupancy, bars first (longest,
   then earliest), then timed events by start time — so a run of days reads
   as one unbroken bar at the top and the day's appointments sit under it in
   time order, exactly the way Google stacks them. Pure; no DOM. */
function assignLanes(events, days) {
  const idx = {};
  days.forEach((d, i) => { idx[d] = i; });
  const first = days[0], last = days[days.length - 1];
  const items = [];
  (events || []).forEach((ev) => {
    const sp = eventSpan(ev);
    if (sp.last < first || sp.first > last) return;
    const c0 = sp.first < first ? 0 : idx[sp.first];
    const c1 = sp.last > last ? days.length - 1 : idx[sp.last];
    if (c0 == null || c1 == null) return;
    items.push({
      ev, c0, c1, lane: -1,
      contL: sp.first < first, contR: sp.last > last,
      bar: !!ev.all_day || sp.last > sp.first,
      len: c1 - c0, start: ev.start_ts || '',
    });
  });
  items.sort((a, b) => (b.bar - a.bar)
    || (b.len - a.len)
    || a.start.localeCompare(b.start)
    || String(a.ev.title || '').localeCompare(String(b.ev.title || '')));
  const occ = [];   // occ[lane][col] = true
  items.forEach((it) => {
    let lane = 0;
    for (;;) {
      occ[lane] = occ[lane] || [];
      let free = true;
      for (let c = it.c0; c <= it.c1; c++) if (occ[lane][c]) { free = false; break; }
      if (free) break;
      lane++;
    }
    for (let c = it.c0; c <= it.c1; c++) occ[lane][c] = true;
    it.lane = lane;
  });
  return items.map(({ len, start, ...rest }) => rest);
}

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

/* How many UTF-16 code units the grapheme ending at index `a` occupies, so a
   single Backspace removes a whole user-perceived character - critical for
   emoji, which are 2+ code units (a plain emoji is a surrogate pair; ❤️ carries
   a variation selector; 👨‍👩‍👧 is a ZWJ sequence; 🇬🇧 is two regional
   indicators). Deleting one code unit would leave a broken half-character.
   Intl.Segmenter is the correct path (present in every engine this runs on -
   Firefox 140 wall, modern Node). The fallback, only reached if Segmenter is
   ever absent, handles surrogate-pair emoji ONLY: a variation-selector (❤️) or
   ZWJ (👨‍👩‍👧) sequence would degrade to deleting just its trailing code unit
   / pair, not the whole grapheme. That degraded case is a documented, tested
   best-effort - see osk.test.mjs. Returns >= 1 for a > 0. */
function oskGraphemeBackLen(v, a) {
  if (a <= 0) return 0;
  if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
    try {
      const seg = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
      let last = 1;
      for (const s of seg.segment(v.slice(0, a))) last = s.segment.length;
      return last;
    } catch (e) { /* fall through to the surrogate-pair fallback */ }
  }
  const lo = v.charCodeAt(a - 1);
  if (lo >= 0xDC00 && lo <= 0xDFFF && a >= 2) {
    const hi = v.charCodeAt(a - 2);
    if (hi >= 0xD800 && hi <= 0xDBFF) return 2;   // a surrogate pair is one char
  }
  return 1;
}

/* The text transform behind the on-screen keyboard (osk.js): apply one key to
   `value` at the [selStart, selEnd] selection and return the new {value, caret}.
   Kept here (no DOM) so the insert / Backspace / Space + maxlength branches are
   unit-testable, exactly like panelFit above; osk.js's key handler is a thin
   wrapper that reads the live input's value + selection, calls this, and writes
   the result back. `key` is a single character (letter, digit, symbol, or a
   whole emoji), 'Backspace', or 'Space'. `shift` capitalizes a character key (a
   no-op on digits/symbols/emoji); `maxlength` (when > 0) refuses an insert that
   would overflow - setRangeText would NOT honor it, so we enforce it here. */
function oskApplyKey(value, selStart, selEnd, key, opts) {
  const v = String(value == null ? '' : value);
  const o = opts || {};
  // Clamp the selection into range so a stale caret can't slice out of bounds.
  const a = Math.max(0, Math.min(selStart | 0, v.length));
  const b = Math.max(a, Math.min(selEnd | 0, v.length));
  if (key === 'Backspace') {
    if (b > a) return { value: v.slice(0, a) + v.slice(b), caret: a };       // drop the selection
    if (a > 0) {
      const n = oskGraphemeBackLen(v, a);   // whole grapheme (emoji-safe), not one code unit
      return { value: v.slice(0, a - n) + v.slice(a), caret: a - n };
    }
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

/* Recently-used emoji list behind the OSK's 🕐 tab, kept PURE (no DOM, no
   storage) so the dedup / most-recent-first / cap / corrupt-input rules are
   unit-testable, like oskApplyKey. osk.js's recentEmojis/pushRecent are thin
   wrappers that read/write localStorage around these.

   oskRecentRead: coerce a stored raw string into a clean list - tolerate
   malformed JSON and a non-array value (-> []), and drop anything that isn't a
   non-empty string so a stale/foreign/partial write can never render as a
   tappable "emoji". Caps to `max`. */
function oskRecentRead(raw, max) {
  let arr;
  try { arr = JSON.parse(raw == null ? '[]' : String(raw)); } catch (e) { arr = []; }
  if (!Array.isArray(arr)) return [];
  const clean = arr.filter((x) => typeof x === 'string' && x.length > 0);
  return clean.slice(0, Math.max(0, max | 0));
}

/* oskRecentPush: put `emoji` at the front, de-duplicated (a re-tap moves it up
   rather than repeating), capped to `max`. Returns a new list. */
function oskRecentPush(list, emoji, max) {
  const base = (Array.isArray(list) ? list : []).filter((x) => x !== emoji);
  base.unshift(emoji);
  return base.slice(0, Math.max(0, max | 0));
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
    // When the turn passes on: 'week' keeps one person all week; anything
    // else is the per-occurrence default. Only a rotation carries it.
    rotate_every: assign === 'rotation' && f.rotateEvery === 'week' ? 'week' : 'turn',
    // The one-time due date; the server ignores it for daily/weekly chores.
    date: kind === 'once' ? (f.date || '') : null,
    // House chore (earns in the chore wallet) or personal goal (doesn't).
    kind: f.choreKind === 'personal' ? 'personal' : 'house',
  };
}

/* Active chores that nobody can do: a fixed chore whose owner was deleted or
   turned off, or a rotation with no active member left. They appear on no
   person card, so from the wall they simply vanished (review, 2026-09-22).
   The chores edit mode lists them with an Edit button to reassign. Pure. */
function unassignedChores(chores, people) {
  const active = new Set((people || []).filter((p) => p.active).map((p) => p.id));
  return (chores || []).filter((ch) => {
    if (!ch.active) return false;
    if (ch.assign_kind === 'fixed') return !active.has(ch.fixed_person_id);
    return !(ch.rotation_order || []).some((pid) => active.has(pid));
  });
}

function unassignedChoresHtml(list) {
  if (!list || !list.length) return '';
  return `<div class="padmin padmin-unassigned">`
    + `<div class="padmin-head"><span class="padmin-head-label">No one to do these</span></div>`
    + list.map((ch) => `<div class="padmin-row">`
      + `<span class="padmin-name">${escapeHtml(ch.icon ? `${ch.icon} ${ch.title}` : ch.title)}</span>`
      + `<button class="padmin-btn" type="button" data-edit-chore="${ch.id}">Reassign</button>`
      + `</div>`).join('')
    + `</div>`;
}

/* Every chore in one list for the chores edit mode, each with what it is
   in plain words ("Every Monday. Ava and Ruby take turns, a new person
   each week.") and Edit / Delete. The person cards only show the chores ON
   the day on screen, so a Saturday-only chore could only be found by paging
   to a Saturday; here everything is one tap away. Sorted by name. Pure. */
function allChoresHtml(chores, people) {
  const list = (chores || []).filter((ch) => ch.active)
    .slice().sort((a, b) => a.title.localeCompare(b.title));
  const rows = list.map((ch) => {
    const m = choreToModel(ch);
    const label = escapeHtml(ch.icon ? `${ch.icon} ${ch.title}` : ch.title);
    return `<div class="allch-row">`
      + `<div class="allch-text"><span class="allch-title">${label}</span>`
      // a personal goal earns nothing in the chore wallet: say so, so the paid
      // chores are easy to pick out at a glance
      + (ch.kind === 'personal' ? `<span class="allch-goal">personal goal</span>` : '')
      + `<span class="allch-when">${escapeHtml(choreSummary(m, people))}</span></div>`
      + `<button class="padmin-btn" type="button" data-edit-chore="${ch.id}">Edit</button>`
      + `<button class="padmin-btn padmin-del" type="button" data-del-chore="${ch.id}" aria-label="Delete ${escapeHtml(ch.title)}">Delete</button>`
      + `</div>`;
  }).join('');
  return `<div class="padmin allch">`
    + `<div class="padmin-head"><span class="padmin-head-label">Every chore</span>`
    + `<button class="padmin-btn padmin-add" type="button" data-add-chore="">+ New chore</button></div>`
    + (rows || `<div class="cal-empty">No chores yet. Tap New chore to add the first one.</div>`)
    + `</div>`;
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
    rotateEvery: ch.rotate_every === 'week' ? 'week' : 'turn',
    choreKind: ch.kind === 'personal' ? 'personal' : 'house',
    each: [],
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
const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];   // Date#getDay order

/* Household chores people reach for most, one tap each under the emoji box.
   The OSK's emoji keyboard still offers everything else. */
const CHORE_EMOJI = ['🍽️', '🧺', '🗑️', '♻️', '🧹', '🛏️', '🚿', '🪥',
  '🐶', '🐱', '🐟', '🌱', '📚', '🏃', '💊', '🚗'];

/* "Ava", "Ava and Ruby", "Ava, Ruby and Milo". Pure. */
function listPhrase(names) {
  const n = (names || []).filter(Boolean);
  if (n.length <= 1) return n[0] || '';
  return `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`;
}

/* The days of a weekly chore in words: "day" for all seven, "weekday" or
   "weekend day" for those sets, else the names ("Monday and Thursday").
   Plural is the caller's ("every Monday", "every other Monday"). Pure. */
function choreDaysPhrase(days) {
  const set = new Set(days == null ? [] : Array.from(days));
  const key = [0, 1, 2, 3, 4, 5, 6].filter((i) => set.has(i)).join('');
  if (key === '0123456') return 'day';
  if (key === '01234') return 'weekday';
  if (key === '56') return 'Saturday and Sunday';
  return listPhrase([0, 1, 2, 3, 4, 5, 6].filter((i) => set.has(i)).map((i) => DAY_NAMES[i]));
}

/* 'YYYY-MM-DD' -> a local Date at midnight (never UTC: a phone west of
   Greenwich would otherwise read the day before). Invalid -> null. */
function parseISODate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

function shortDate(d) {
  return `${WEEKDAY_SHORT[d.getDay()]} ${MONTH_SHORT[d.getMonth()]} ${d.getDate()}`;
}

/* The chore form's plain-English summary of what is about to be saved:
   "Every Monday and Thursday. Ava and Ruby take turns, a new person each
   week." Built from the same model the payload comes from, so it can
   never describe something the save does not do. Pure. */
function choreSummary(m, people) {
  const nameOf = (id) => {
    const p = (people || []).find((x) => x.id === Number(id));
    return p ? p.name : '';
  };
  let when;
  if (m.repeat === 'weekly') {
    const daysWord = choreDaysPhrase(m.days);
    if (!daysWord) when = 'Pick the days';
    else when = Number(m.weekInterval) === 2 ? `Every other ${daysWord}` : `Every ${daysWord}`;
  } else if (m.repeat === 'interval') {
    const n = clampIntervalDays(m.intervalDays);
    when = n == null ? 'Pick how many days apart' : (n === 1 ? 'Every day' : `Every ${n} days`);
  } else if (m.repeat === 'once') {
    const d = parseISODate(m.date);
    when = d ? `Once, on ${shortDate(d)}` : 'Once';
  } else {
    when = 'Every day';
  }
  const assign = m.repeat === 'once' ? 'fixed' : m.assign;
  let who;
  if (assign === 'rotation') {
    const names = [];
    (m.rot || []).forEach((id) => { const n = nameOf(id); if (n && !names.includes(n)) names.push(n); });
    if (names.length === 0) who = 'Add the people who take turns';
    else if (names.length === 1) who = `${names[0]} every time`;
    else who = m.rotateEvery === 'week'
      ? `${listPhrase(names)} take turns, a new person each week`
      : `${listPhrase(names)} take turns, a new person each time`;
  } else if (assign === 'each') {
    const names = (m.each || []).map(nameOf).filter(Boolean);
    who = names.length ? `${listPhrase(names)} ${names.length > 1 ? 'each do their own' : 'does it'}`
      : 'Pick who does it';
  } else {
    const n = nameOf(m.fixed_person_id);
    who = n ? `${n} does it` : 'Pick who does it';
  }
  return `${when}. ${who}.`;
}

/* Rotate a turn order so the person at index `idx` is up at `slot` (the
   place whose turn the next chore day is, from the server's preview). The
   cycle itself is unchanged, only where it starts. Pure. */
function rotateOrderToStart(order, slot, idx) {
  const o = (order || []).slice();
  const n = o.length;
  if (n < 2 || slot == null) return o;
  const s = (((idx - slot) % n) + n) % n;
  return o.map((_, i) => o[(i + s) % n]);
}

/* What the form says when an edit touches who is up next. `kind` 'kept':
   the edit changed the schedule and the server restarted the turns so the
   same person keeps the next turn; 'moved': the next turn goes to someone
   else. `when` is the first preview cell's label ('This week', 'Next week',
   'Week of Oct 26', 'Today', 'Wed Oct 21'); `byWeek` says the turns go by
   the week. Pure. */
function turnNote(kind, from, to, when, byWeek) {
  const w = String(when || '');
  const span = !byWeek ? 'the next turn'
    : /^Week of /.test(w) ? `the week of ${w.slice(8)}`
      : w ? w[0].toLowerCase() + w.slice(1) : 'this week';
  if (kind === 'kept') return `${from} keeps ${span}, same as before.`;
  return `This moves ${span} from ${from} to ${to}.`;
}

/* The preview strip's cells from the server's upcoming days. A weekly
   rotation reads by WEEK ("This week · Ava", "Next week · Ruby"); anything
   else by day ("Today", "Tomorrow", "Wed Oct 21"). Pure; `today` is
   'YYYY-MM-DD'. */
function chorePreviewCells(upcoming, byWeek, today) {
  const t = parseISODate(today);
  if (!t) return [];
  const monday = (d) => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
  const days = (a, b) => Math.round((b - a) / 86400000);
  const rows = (upcoming || []).map((u) => ({ d: parseISODate(u.date), pid: u.person_id })).filter((r) => r.d);
  if (byWeek) {
    const out = [];
    const seen = new Set();
    const m0 = monday(t);
    rows.forEach((r) => {
      const wk = days(m0, monday(r.d)) / 7;
      if (seen.has(wk) || out.length >= 4) return;
      seen.add(wk);
      const mon = monday(r.d);
      const label = wk === 0 ? 'This week' : wk === 1 ? 'Next week' : `Week of ${MONTH_SHORT[mon.getMonth()]} ${mon.getDate()}`;
      out.push({ label, person_id: r.pid });
    });
    return out;
  }
  return rows.slice(0, 6).map((r) => {
    const gap = days(t, r.d);
    return { label: gap === 0 ? 'Today' : gap === 1 ? 'Tomorrow' : shortDate(r.d), person_id: r.pid };
  });
}

/* One reusable chore form. `model` seeds it; `onsubmit(body | bodies, errEl)`
   fires with the assembled request body (an ARRAY of bodies when "each of
   them" makes one chore per person); `people` is the full people list
   (active status included). `opts.preview(body)` (optional) resolves the
   server's {upcoming, slot} for the live "who's up" strip; `opts.choreId`
   rides along so an edit previews from the chore's own start date;
   `opts.allowEach` offers "Each of them" (adding only: an existing chore is
   one person's or one rotation's). Opened from the wall's all-chores edit
   mode (add + every inline edit). */
function buildChoreForm(host, model, submitLabel, onsubmit, people, opts = {}) {
  const allowEach = !!opts.allowEach;
  host.innerHTML = `
    <div class="field"><label>What needs doing?</label>
      <div class="title-row">
        <input class="txt-input f-icon" maxlength="4" autocomplete="off" aria-label="Emoji (optional)" placeholder="🙂">
        <input class="txt-input f-title" maxlength="60" autocomplete="off" aria-label="Chore name" placeholder="e.g. Dishes">
      </div>
      <div class="emoji-row f-emoji">${CHORE_EMOJI.map((e) =>
    `<button class="emoji-pick" type="button" data-emoji="${e}" aria-label="Use ${e}">${e}</button>`).join('')}</div></div>
    <div class="field"><label>How often?</label>
      <div class="segmented f-repeat">
        <button class="seg-btn" type="button" data-repeat="daily">Every day</button>
        <button class="seg-btn" type="button" data-repeat="weekly">Some days</button>
        <button class="seg-btn" type="button" data-repeat="interval">Every few days</button>
        <button class="seg-btn" type="button" data-repeat="once">Just once</button>
      </div>
      <div class="sub-field f-weekly">
        <div class="day-chips f-days"></div>
        <div class="preset-row f-presets">
          <button class="preset-btn" type="button" data-preset="weekdays">Weekdays</button>
          <button class="preset-btn" type="button" data-preset="weekends">Weekends</button>
          <button class="preset-btn" type="button" data-preset="all">All week</button>
        </div>
        <div class="segmented f-weekfreq">
          <button class="seg-btn" type="button" data-weekfreq="1">Every week</button>
          <button class="seg-btn" type="button" data-weekfreq="2">Every other week</button>
        </div>
      </div>
      <div class="interval-row f-interval">
        <span class="interval-word">Every</span>
        <input class="txt-input interval-num f-intervaldays" type="number" min="1" max="365" inputmode="numeric" autocomplete="off" aria-label="Days apart">
        <span class="interval-word">days</span>
      </div>
      <input class="txt-input f-date" type="date" aria-label="Date"></div>
    <div class="field"><label>Who does it?</label>
      <div class="segmented f-assign">
        <button class="seg-btn" type="button" data-assign="fixed">One person</button>
        <button class="seg-btn" type="button" data-assign="rotation">Take turns</button>
        ${allowEach ? '<button class="seg-btn" type="button" data-assign="each">Each of them</button>' : ''}
      </div>
      <div class="person-chips f-person" role="group" aria-label="Who does it"></div>
      <div class="person-chips f-each" role="group" aria-label="Everyone who does their own"></div>
      <div class="sub-field f-turns">
        <div class="hint f-rothint">Tap names in the order they take turns. Tap a name twice to give someone two turns.</div>
        <div class="person-chips f-rotadd"></div>
        <div class="rot-list f-rotation"></div>
        <div class="turn-when">
          <span class="turn-when-label">Switch to the next person</span>
          <div class="segmented f-rotevery">
            <button class="seg-btn" type="button" data-rotevery="turn">Each time</button>
            <button class="seg-btn" type="button" data-rotevery="week">Every week</button>
          </div>
        </div>
      </div></div>
    <div class="field f-kindfield"><label>Does it earn money?</label>
      <div class="segmented f-kind">
        <button class="seg-btn" type="button" data-kind="house">House chore</button>
        <button class="seg-btn" type="button" data-kind="personal">Personal goal</button>
      </div>
      <div class="hint">A personal goal (like reading or vitamins) stays on the card but doesn't earn money or count for the streak.</div></div>
    <div class="chore-preview f-preview hidden" aria-live="polite">
      <div class="preview-head">Coming up</div>
      <div class="preview-cells f-cells"></div>
      <div class="preview-first f-first"></div>
      <p class="turn-note f-turnnote hidden"></p>
    </div>
    <details class="remind f-remind"><summary>Phone reminders <span class="remind-count f-remindcount"></span></summary>
      <div class="time-add">
        <input class="txt-input time-input f-timeinput" type="time" autocomplete="off" aria-label="Reminder time">
        <button class="time-add-btn f-timeadd" type="button">Add time</button>
      </div>
      <div class="time-list f-times"></div>
      <div class="hint">Up to 6 times a day, sent to each person's phone list.</div></details>
    <p class="chore-summary f-summary"></p>
    <div class="form-error hidden f-error"></div>
    <button class="btn-primary" type="button" data-submit>${escapeHtml(submitLabel)}</button>`;

  const $ = (sel) => host.querySelector(sel);
  if (!model.rotateEvery) model.rotateEvery = 'turn';
  if (model.choreKind !== 'personal') model.choreKind = 'house';
  if (!Array.isArray(model.each)) model.each = [];
  $('.f-title').value = model.title || '';
  $('.f-icon').value = model.icon || '';
  $('.f-date').value = model.date || (model.repeat === 'once' ? todayISO() : '');
  $('.f-intervaldays').value = model.intervalDays == null || model.intervalDays === ''
    ? '' : String(model.intervalDays);
  const active = people.filter((p) => p.active);
  const personOf = (id) => people.find((x) => x.id === Number(id));
  if (model.times.length) $('.f-remind').open = true;   // already has times: show them
  // the server's {upcoming, slot, days, rotation_order, before} for the draft
  let lastPreview = null;
  // Editing: whoever is up next keeps the turn through a schedule change
  // (the server's keep_turn), until the person editing says who goes when
  // themselves: moves someone up or down, or picks who goes first. Adding
  // or removing someone (or turning a one-person chore into turns) still
  // keeps it.
  let keepTurn = opts.choreId != null;
  const savedRot = model.rot.slice();   // the turn order as saved

  const chip = (p, attr, on) => `<button class="person-chip${on ? ' selected' : ''}" type="button" ${attr}="${p.id}"`
    + ` aria-pressed="${on ? 'true' : 'false'}" style="--pc:${safeColor(p.color)}">`
    + `<span class="pc-dot" aria-hidden="true"></span>${escapeHtml(p.name)}</button>`;

  const draft = () => ({
    title: $('.f-title').value, icon: $('.f-icon').value, repeat: model.repeat,
    days: model.days, weekInterval: model.weekInterval,
    intervalDays: $('.f-intervaldays').value, times: model.times,
    assign: model.repeat === 'once' ? 'fixed' : model.assign,
    person: model.fixed_person_id, rot: model.rot, rotateEvery: model.rotateEvery,
    each: model.each, date: $('.f-date').value, choreKind: model.choreKind,
  });
  const paintSummary = () => {
    $('.f-summary').textContent = choreSummary({ ...model, ...draft(), fixed_person_id: model.fixed_person_id }, people);
  };

  const paintRepeat = () => {
    $('.f-repeat').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.repeat === model.repeat));
    $('.f-weekly').classList.toggle('hidden', model.repeat !== 'weekly');
    $('.f-days').classList.toggle('hidden', model.repeat !== 'weekly');
    $('.f-weekfreq').classList.toggle('hidden', model.repeat !== 'weekly');
    $('.f-interval').classList.toggle('hidden', model.repeat !== 'interval');
    $('.f-date').classList.toggle('hidden', model.repeat !== 'once');
    // a one-time item never earns or counts, so there is nothing to choose
    $('.f-kindfield').classList.toggle('hidden', model.repeat === 'once');
  };
  const paintKind = () => {
    $('.f-kind').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.kind === model.choreKind));
  };
  const paintDays = () => {
    $('.f-days').innerHTML = DAY_LABELS.map((d, i) =>
      `<button class="day-chip${model.days.has(i) ? ' selected' : ''}" type="button" data-day="${i}" aria-pressed="${model.days.has(i)}">${d}</button>`).join('');
  };
  const paintWeekFreq = () => {
    const wk = model.weekInterval === 2 ? 2 : 1;
    $('.f-weekfreq').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', Number(b.dataset.weekfreq) === wk));
  };
  // Reminder-time chips render the app's compact time label ("20:00" -> "8pm"),
  // the same fmtTime the calendar uses. The whole chip is the remove button.
  const paintTimes = () => {
    const full = model.times.length >= 6;
    $('.time-add').classList.toggle('hidden', full);
    $('.f-times').innerHTML = model.times.length
      ? model.times.map((t, i) =>
        `<button class="time-chip" type="button" data-remtime="${i}" aria-label="Remove ${escapeHtml(fmtTime('T' + t))}">${escapeHtml(fmtTime('T' + t))} ✕</button>`).join('')
      : '';
    $('.f-remindcount').textContent = model.times.length
      ? model.times.map((t) => fmtTime('T' + t)).join(', ') : 'none';
  };
  const paintAssign = () => {
    // A one-time chore is always one person: no turns, no copies.
    const once = model.repeat === 'once';
    ['rotation', 'each'].forEach((k) => {
      const b = $('.f-assign').querySelector(`[data-assign="${k}"]`);
      if (b) b.classList.toggle('hidden', once);
    });
    const assign = once ? 'fixed' : model.assign;
    $('.f-assign').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.assign === assign));
    $('.f-person').classList.toggle('hidden', assign !== 'fixed');
    $('.f-each').classList.toggle('hidden', assign !== 'each');
    $('.f-turns').classList.toggle('hidden', assign !== 'rotation');
    $('.f-rotadd').classList.toggle('hidden', assign !== 'rotation');
    $('.f-rotation').classList.toggle('hidden', assign !== 'rotation');
    $('.f-rothint').classList.toggle('hidden', assign !== 'rotation');
  };
  const paintPeople = () => {
    // a chore whose owner was turned off still shows them, so the edit can
    // keep or change it knowingly instead of silently re-picking someone
    const owner = personOf(model.fixed_person_id);
    const choices = owner && !owner.active ? [...active, owner] : active;
    $('.f-person').innerHTML = choices.map((p) => chip(p, 'data-pick', p.id === model.fixed_person_id)).join('');
    $('.f-each').innerHTML = active.map((p) => chip(p, 'data-each', model.each.includes(p.id))).join('');
  };
  const paintRotation = () => {
    $('.f-rotadd').innerHTML = active.map((p) =>
      `<button class="person-chip add" type="button" data-add="${p.id}" style="--pc:${safeColor(p.color)}">`
      + `<span class="pc-plus" aria-hidden="true">+</span>${escapeHtml(p.name)}</button>`).join('');
    $('.f-rotation').innerHTML = model.rot.length
      ? model.rot.map((id, idx) => {
        const p = personOf(id);
        const name = escapeHtml(p ? p.name : 'Someone removed');
        // an attribute, not a class (the class-styled guard reads ' x' as a class)
        const firstOne = idx === 0 ? 'disabled' : '';                     // can't move up
        const lastOne = idx === model.rot.length - 1 ? 'disabled' : '';   // can't move down
        return `<div class="rot-item" style="--pc:${safeColor(p ? p.color : '')}">`
          + `<span class="rot-num">${idx + 1}</span><span class="pc-dot" aria-hidden="true"></span>`
          + `<span class="rot-name">${name}</span>`
          + `<button class="rot-btn" type="button" data-up="${idx}" aria-label="Move ${name} earlier" ${firstOne}>↑</button>`
          + `<button class="rot-btn" type="button" data-down="${idx}" aria-label="Move ${name} later" ${lastOne}>↓</button>`
          + `<button class="rot-btn rot-del" type="button" data-rem="${idx}" aria-label="Take ${name} out">✕</button>`
          + `</div>`;
      }).join('')
      : `<div class="hint">No one yet: tap names above.</div>`;
    $('.f-rotevery').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.rotevery === model.rotateEvery));
  };

  // Live "Coming up" strip: who does it on the next few chore days, straight
  // from the server (the same code the wall runs). Debounced; a reply for
  // an older draft is dropped. Any failure just hides the strip: the form
  // still saves, and the summary sentence still describes the chore.
  let previewSeq = 0;
  let previewTimer = null;
  // the last ask failed (not: nothing to show): false, or what to say. A
  // refusal (4xx) is the server's own reason, which a Save would hit too;
  // anything else (down, slow, 5xx) can't say anything about saving.
  let previewFailed = false;
  const paintPreview = () => {
    const box = $('.f-preview');
    if (!box) return;            // the editor closed while an ask was out
    const assign = model.repeat === 'once' ? 'fixed' : model.assign;
    const pv = lastPreview;
    if (previewFailed) {
      $('.f-cells').innerHTML = `<div class="hint">${escapeHtml(previewFailed)}</div>`;
      $('.f-first').innerHTML = '';
      $('.f-first').classList.add('hidden');
      $('.f-turnnote').classList.add('hidden');
      box.classList.remove('hidden');
      return;
    }
    if (!pv || !pv.upcoming || assign === 'each') { box.classList.add('hidden'); return; }
    if (!pv.upcoming.length) {
      $('.f-cells').innerHTML = `<div class="hint">Not in the next ${Number(pv.days) || 120} days.</div>`;
      $('.f-first').innerHTML = '';
      $('.f-first').classList.add('hidden');
      $('.f-turnnote').classList.add('hidden');
      box.classList.remove('hidden');
      return;
    }
    const byWeek = assign === 'rotation' && model.rotateEvery === 'week';
    const cells = chorePreviewCells(pv.upcoming, byWeek, todayISO());
    $('.f-cells').innerHTML = cells.map((c) => {
      const p = personOf(c.person_id);
      return `<div class="preview-cell" style="--pc:${safeColor(p ? p.color : '')}">`
        + `<span class="preview-when">${escapeHtml(c.label)}</span>`
        + `<span class="preview-who"><span class="pc-dot" aria-hidden="true"></span>${escapeHtml(p ? p.name : 'No one')}</span></div>`;
    }).join('');
    // "Who's up first?": restart the cycle on anyone in it without
    // retyping the order (same people, same order, new starting point)
    const first = $('.f-first');
    const uniq = [];
    model.rot.forEach((id, idx) => { if (!uniq.some((u) => u.id === id)) uniq.push({ id, idx }); });
    const allActive = model.rot.every((id) => { const p = personOf(id); return p && p.active; });
    if (assign === 'rotation' && uniq.length > 1 && pv.slot != null && allActive) {
      const upNow = model.rot[pv.slot];
      // the slot is the FIRST upcoming day's turn, which is next week's on a
      // Saturday for a weekday chore: name the week the cells start with
      const firstWeek = cells[0] ? cells[0].label : 'This week';
      const ask = !byWeek ? 'Who goes next?'
        : firstWeek === 'This week' ? 'Who has this week?'
          : firstWeek === 'Next week' ? 'Who has next week?'
            : `Who has the ${firstWeek.replace(/^Week/, 'week')}?`;
      first.innerHTML = `<span class="first-label">${escapeHtml(ask)}</span>`
        + uniq.map((u) => {
          const p = personOf(u.id);
          const cls = u.id === upNow ? 'person-chip small selected' : 'person-chip small';
          return `<button class="${cls}" type="button" data-first="${u.idx}"`
            + ` aria-pressed="${u.id === upNow}" style="--pc:${safeColor(p.color)}"><span class="pc-dot" aria-hidden="true"></span>${escapeHtml(p.name)}</button>`;
        }).join('');
      first.classList.remove('hidden');
    } else {
      first.innerHTML = '';
      first.classList.add('hidden');
    }
    // say it when an edit moves (or keeps) the next turn: the cells alone
    // only show the new plan, not that it changed
    const note = $('.f-turnnote');
    const was = personOf(pv.before);
    const now = personOf(cells[0] ? cells[0].person_id : null);
    let say = '';
    if (assign === 'rotation' && was && now && cells[0]) {
      if (now.id !== was.id) say = turnNote('moved', was.name, now.name, cells[0].label, byWeek);
      // kept: the edit restarted the saved order to hold the turn (the same
      // order as saved means nothing moved, so nothing to say)
      else if (keepTurn && model.rot.join() !== savedRot.join()) {
        say = turnNote('kept', was.name, now.name, cells[0].label, byWeek);
      }
    }
    note.textContent = say;
    note.classList.toggle('hidden', !say);
    note.classList.toggle('moved', !!say && now.id !== was.id);
    box.classList.remove('hidden');
  };
  const refreshPreview = () => {
    paintSummary();
    if (typeof opts.preview !== 'function') return;
    clearTimeout(previewTimer);
    // numbered when the draft changes, not when the ask goes out: a reply
    // for an older draft that lands inside the debounce must not paint
    const seq = ++previewSeq;
    previewTimer = setTimeout(async () => {
      if (seq !== previewSeq || !$('.f-title')) return;   // superseded, or the editor closed
      const f = draft();
      // nothing to preview yet: no person, no turns, no days. The strip
      // waits instead of asking the server about a chore that can't save.
      const incomplete = f.assign === 'each'
        || (f.assign === 'fixed' && model.fixed_person_id == null)
        || (f.assign === 'rotation' && !model.rot.length)
        || (model.repeat === 'weekly' && model.days.size === 0)
        || (model.repeat === 'interval' && clampIntervalDays(f.intervalDays) === null)
        || (model.repeat === 'once' && !parseISODate(f.date));
      if (incomplete) { lastPreview = null; previewFailed = false; paintPreview(); return; }
      const body = buildChorePayload({ ...f, person: model.fixed_person_id });
      if (opts.choreId != null) { body.chore_id = opts.choreId; body.keep_turn = keepTurn; }
      let got = null;
      let failed = false;
      try { got = await opts.preview(body); } catch (e) {
        const refused = e && e.status >= 400 && e.status < 500 && e.message;
        failed = refused ? `${e.message[0].toUpperCase()}${e.message.slice(1)}.`
          : 'Couldn’t load who’s up next. Saving still works.';
        console.warn('chore preview failed:', e && e.message);
      }
      if (seq !== previewSeq) return;   // a newer draft owns the strip
      // keep_turn restarted the turns: show the order the save will store
      const kept = got && keepTurn && model.assign === 'rotation' && Array.isArray(got.rotation_order)
        && got.rotation_order.length === model.rot.length
        && got.rotation_order.some((id, i) => id !== model.rot[i]);
      if (kept) { model.rot = got.rotation_order.slice(); paintRotation(); }
      lastPreview = got;
      previewFailed = failed;
      paintPreview();
    }, 180);
  };
  const repaintAll = () => {
    paintRepeat(); paintDays(); paintWeekFreq(); paintTimes(); paintAssign(); paintPeople(); paintRotation();
    paintKind();
    refreshPreview();
  };
  repaintAll();

  $('.f-title').oninput = paintSummary;
  $('.f-icon').oninput = paintSummary;
  $('.f-date').onchange = refreshPreview;
  $('.f-intervaldays').oninput = refreshPreview;
  $('.f-emoji').onclick = (e) => {
    const b = e.target.closest('[data-emoji]');
    if (!b) return;
    $('.f-icon').value = b.dataset.emoji;
    paintSummary();
  };
  $('.f-repeat').onclick = (e) => {
    const b = e.target.closest('[data-repeat]');
    if (!b) return;
    model.repeat = b.dataset.repeat;
    if (model.repeat === 'once') {
      model.assign = 'fixed';                       // one-time is one person
      if (!$('.f-date').value) $('.f-date').value = todayISO();
    }
    // Seed a sensible gap the first time "Every few days" is chosen so the
    // field is never empty (empty would fail the server's interval rule).
    if (model.repeat === 'interval' && !$('.f-intervaldays').value) $('.f-intervaldays').value = '2';
    paintRepeat();
    paintAssign();                                  // turns/each show/hide with once
    refreshPreview();
  };
  $('.f-days').onclick = (e) => {
    const b = e.target.closest('[data-day]');
    if (!b) return;
    const i = Number(b.dataset.day);
    if (model.days.has(i)) model.days.delete(i); else model.days.add(i);
    paintDays();
    refreshPreview();
  };
  $('.f-presets').onclick = (e) => {
    const b = e.target.closest('[data-preset]');
    if (!b) return;
    const sets = { weekdays: [0, 1, 2, 3, 4], weekends: [5, 6], all: [0, 1, 2, 3, 4, 5, 6] };
    model.days = new Set(sets[b.dataset.preset] || []);
    paintDays();
    refreshPreview();
  };
  $('.f-weekfreq').onclick = (e) => {
    const b = e.target.closest('[data-weekfreq]');
    if (!b) return;
    model.weekInterval = Number(b.dataset.weekfreq) === 2 ? 2 : 1;
    paintWeekFreq();
    refreshPreview();
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
    if (!b) return;
    model.assign = b.dataset.assign;
    // carry the people across a switch so nobody retypes them
    if (model.assign === 'rotation' && !model.rot.length && model.fixed_person_id != null) model.rot = [model.fixed_person_id];
    if (model.assign === 'each' && !model.each.length && model.fixed_person_id != null) model.each = [model.fixed_person_id];
    paintAssign(); paintPeople(); paintRotation();
    refreshPreview();
  };
  $('.f-person').onclick = (e) => {
    const b = e.target.closest('[data-pick]');
    if (!b) return;
    model.fixed_person_id = Number(b.dataset.pick);
    paintPeople();
    refreshPreview();
  };
  $('.f-each').onclick = (e) => {
    const b = e.target.closest('[data-each]');
    if (!b) return;
    const id = Number(b.dataset.each);
    model.each = model.each.includes(id) ? model.each.filter((x) => x !== id) : [...model.each, id];
    paintPeople();
    paintSummary();
  };
  $('.f-rotadd').onclick = (e) => {
    const b = e.target.closest('[data-add]');
    if (!b) return;
    model.rot.push(Number(b.dataset.add));
    paintRotation();
    refreshPreview();
  };
  $('.f-rotation').onclick = (e) => {
    const b = e.target.closest('[data-rem]') || e.target.closest('[data-up]') || e.target.closest('[data-down]');
    if (!b || b.disabled) return;
    if (b.dataset.rem !== undefined) model.rot.splice(Number(b.dataset.rem), 1);
    else {
      const i = Number(b.dataset.up !== undefined ? b.dataset.up : b.dataset.down);
      const j = b.dataset.up !== undefined ? i - 1 : i + 1;
      if (j >= 0 && j < model.rot.length) [model.rot[i], model.rot[j]] = [model.rot[j], model.rot[i]];
      keepTurn = false;   // a move says who goes when
    }
    paintRotation();
    refreshPreview();
  };
  $('.f-rotevery').onclick = (e) => {
    const b = e.target.closest('[data-rotevery]');
    if (!b) return;
    model.rotateEvery = b.dataset.rotevery === 'week' ? 'week' : 'turn';
    paintRotation();
    refreshPreview();
  };
  $('.f-kind').onclick = (e) => {
    const b = e.target.closest('[data-kind]');
    if (!b) return;
    model.choreKind = b.dataset.kind === 'personal' ? 'personal' : 'house';
    paintKind();
  };
  $('.f-first').onclick = (e) => {
    const b = e.target.closest('[data-first]');
    if (!b || !lastPreview) return;
    model.rot = rotateOrderToStart(model.rot, lastPreview.slot, Number(b.dataset.first));
    keepTurn = false;
    paintRotation();
    refreshPreview();
  };
  $('[data-submit]').onclick = oneSaveAtATime($('[data-submit]'), () => {
    // Serialization lives in buildChorePayload (pure, tested).
    const f = draft();
    const err = $('.f-error');
    err.classList.add('hidden');
    const fail = (msg) => { err.textContent = msg; err.classList.remove('hidden'); };
    if (!f.title.trim()) return fail('Give the chore a name.');
    if (model.repeat === 'weekly' && model.days.size === 0) return fail('Pick at least one day.');
    // Mirror the server's "interval needs 1–365" rule for an inline message
    // instead of a generic toast (clampIntervalDays would send null otherwise).
    if (model.repeat === 'interval' && clampIntervalDays($('.f-intervaldays').value) === null) {
      return fail('Enter how many days apart (1–365).');
    }
    if (f.assign === 'fixed' && model.fixed_person_id == null) return fail('Pick who does it.');
    if (f.assign === 'rotation' && !model.rot.length) return fail('Add the people who take turns.');
    if (f.assign === 'each') {
      if (!model.each.length) return fail('Pick who does it.');
      // one chore per person: each keeps their own check-off and streak
      return onsubmit(model.each.map((pid) => buildChorePayload({ ...f, assign: 'fixed', person: pid })), err);
    }
    const body = buildChorePayload({ ...f, person: model.fixed_person_id });
    // a save before the preview answered still keeps the turn: the server
    // restarts the order itself
    if (opts.choreId != null) body.keep_turn = keepTurn;
    return onsubmit(body, err);
  });
  return {
    // "Each of them" saved for some people and not others: keep the form
    // open on just the ones still to save, so Save again can't duplicate
    keepOnlyEach(ids) {
      model.each = model.each.filter((id) => ids.includes(id));
      paintPeople();
      paintSummary();
    },
  };
}

/* Where a dragged item lands: the target list's open ids with `id` taken out
   and put back at `index`. Pure (the to-do board in
   hub.js drives it). */
function todoDropOrder(ids, id, index) {
  const rest = ids.filter((x) => x !== id);
  const at = Math.max(0, Math.min(index, rest.length));
  return [...rest.slice(0, at), id, ...rest.slice(at)];
}

/* What an arrow key on a focused to-do grip asks for: up/down one place in
   its list, left/right to the end of the list beside it. Returns
   {bucket, order} for a move, {edge} when the item is already at that end
   (nothing to send), or null for any other key or an unknown item. Pure
   (the to-do board in hub.js drives it). */
function todoKeyMove(buckets, id, key) {
  const keys = ['now', 'soon', 'later'];
  const open = (b) => ((buckets && buckets[b]) || []).filter((t) => !t.done_at).map((t) => t.id);
  const from = keys.find((b) => open(b).includes(id));
  if (!from) return null;
  const ids = open(from);
  const at = ids.indexOf(id);
  if (key === 'ArrowUp' || key === 'ArrowDown') {
    const to = at + (key === 'ArrowUp' ? -1 : 1);
    if (to < 0 || to >= ids.length) return { edge: key === 'ArrowUp' ? 'top' : 'bottom', bucket: from };
    return { bucket: from, order: todoDropOrder(ids, id, to) };
  }
  if (key === 'ArrowLeft' || key === 'ArrowRight') {
    const k = keys.indexOf(from) + (key === 'ArrowLeft' ? -1 : 1);
    if (k < 0 || k >= keys.length) return { edge: key === 'ArrowLeft' ? 'first' : 'last', bucket: from };
    const target = keys[k];
    return { bucket: target, order: todoDropOrder(open(target), id, open(target).length) };
  }
  return null;
}

/* Wrap a form's Save handler so it runs one write at a time. A double tap
   (or the OSK Done key clicking Save while a tap's write is still out) used
   to POST twice and create two chores or two people. The button is disabled
   and a flag set until `run`'s promise settles; both come back either way,
   so a failed save can be tried again (on success the form is closed). A
   `run` that returns no promise (a validation stop, nothing sent) releases
   at once. */
function oneSaveAtATime(btn, run) {
  let busy = false;
  return () => {
    if (busy) return undefined;
    busy = true;
    btn.disabled = true;
    const release = () => { busy = false; btn.disabled = false; };
    let p;
    try { p = run(); } catch (e) { release(); throw e; }
    if (!p || typeof p.then !== 'function') { release(); return p; }
    return p.then(release, (e) => { release(); throw e; });
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
    assign: 'fixed', fixed_person_id: null, rot: [], rotateEvery: 'turn', each: [], date: '' };
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
   listGone, twoWay}. With lists it renders a picker + the one-time sharing note;
   with no lists it renders a single "connect iCloud" line instead of a dead
   dropdown, unless the person's list is gone (then iCloud IS connected and
   every list went away, so say that). `listGone` comes from the server
   (/api/admin/state people[].list_gone); an empty list array alone can't tell
   "not connected" from "all gone". Kept as an HTML string so buildPersonForm
   can drop it into the same template pass (the live wiring + PATCH is added
   after). */
function mirrorFieldHtml(opts) {
  if (!opts || !opts.edit) return '';
  const lists = opts.reminderLists || [];
  const listId = opts.reminderListId || '';
  const gone = !!listId && !!opts.listGone;
  if (!lists.length && !gone) {
    return `<div class="field"><label>iCloud chore list</label>`
      + `<div class="hint" data-plist-empty>Connect iCloud in Settings to mirror this person’s chores to a list.</div></div>`;
  }
  const options = `<option value=""${listId ? '' : ' selected'}>— none —</option>`
    + lists.map((l) =>
      `<option value="${escapeHtml(l.id)}"${l.id === listId ? ' selected' : ''}>${escapeHtml(l.name)}</option>`).join('');
  // A saved id that matches no list still needs its own option. Without one
  // the browser shows the none option as already chosen, and picking it
  // fires no change, so the mapping could never be cleared (and a failed
  // save's undo would blank the picker). It is selected but disabled: it
  // shows what is saved and can't be picked again, so choosing none is
  // always a real change.
  const savedOption = listId && !lists.some((l) => l.id === listId)
    ? `<option value="${escapeHtml(listId)}" selected disabled>${gone ? '(list gone)' : '(current list)'}</option>`
    : '';
  const offNow = !!listId && !opts.twoWay;
  // Mapped to a list the sync no longer has: say what happened above the
  // picker. The picker shows "(list gone)" and still offers none to clear
  // the mapping, even when no lists are left.
  const goneText = lists.length
    ? 'This person’s iCloud chore list is gone; pick a new one.'
    : 'This person’s iCloud chore list is gone, and iCloud has no other lists. Make or share one in iCloud Reminders, then pick it here.';
  return `<div class="field"><label>iCloud chore list</label>`
    + (gone ? `<div class="form-error" data-plist-gone>${goneText}</div>` : '')
    + `<select class="txt-input" data-plist>${savedOption}${options}</select>`
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
        // a new list (or none) is saved: the old one being gone no longer applies
        const goneEl = $('[data-plist-gone]');
        if (goneEl) goneEl.classList.add('hidden');
      }
      paintReadonly();
    };
  }

  $('[data-psubmit]').onclick = oneSaveAtATime($('[data-psubmit]'), () =>
    onsubmit({ name: $('[data-pname]').value.trim(), color: model.color }, $('[data-perror]')));
}

/* Attempt a chore check-off/uncheck; returns true on success, false if the
   write failed. Separated from the DOM (hub.js toggleChore shows a toast when
   this returns false) so the failure-detection half is testable — a persistent
   write failure must be DETECTED, not swallowed.
   `date` is the day the wall is showing. Send it: just after midnight the wall
   still shows yesterday until its next poll, and a dateless tap would land on
   the server's new day and vanish from the row that was tapped. */
/* ------------------------------------------------------------ chore wallet */

/* Money is whole cents: "$18.50", and "30¢" under a dollar. Pure. */
function formatCents(c) {
  const n = Math.round(Number(c) || 0);
  if (n > 0 && n < 100) return `${n}¢`;
  const a = Math.abs(n);
  const dollars = String(Math.floor(a / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${n < 0 ? '-' : ''}$${dollars}.${String(a % 100).padStart(2, '0')}`;
}

/* A bonus amount reads cleaner without ".00": "$100", "$7.50". Pure. */
function formatBonus(c) {
  const n = Math.round(Number(c) || 0);
  return n >= 100 && n % 100 === 0 ? `$${n / 100}` : formatCents(n);
}

const SHIELD_SVG = (on) => `<svg class="shield ${on ? 'shield-on' : 'shield-off'}" viewBox="0 0 16 18" aria-hidden="true">`
  + '<path d="M8 1 15 4v5c0 4-3 7-7 8-4-1-7-4-7-8V4z"/></svg>';

/* The card head when the wallet is on: the streak toward the next goal and
   one shield per save held (an outline for each still to earn). Pure. */
function rewardHeadHtml(r) {
  if (!r || r.error) return '';
  const max = Math.max(0, Number(r.shield_max) || 0);
  const held = Math.min(max, Math.max(0, Number(r.shields) || 0));
  const shields = max
    ? `<span class="shields" title="${held} of ${max} saves: a missed day uses one instead of ending the streak">`
      + Array.from({ length: max }, (_, i) => SHIELD_SVG(i < held)).join('') + `</span>`
    : '';
  // streak only (earns false): the count alone, since "/ goal" is bonus progress
  if (r.earns === false) {
    return `<span class="chip-streak chip-wallet" title="${Number(r.streak)} days in a row">`
      + `🔥 <span class="num">${Number(r.streak)}</span></span>` + shields;
  }
  return `<span class="chip-streak chip-wallet" title="${Number(r.streak)} days in a row toward ${Number(r.goal)}">`
    + `🔥 <span class="num">${Number(r.streak)} / ${Number(r.goal)}</span></span>` + shields;
}

/* The wallet line under a card's chores. Someone set to streak only
   (earns false) gets none: no amount and no bonus progress, only the
   streak and shields in the card head. Pure. */
function walletLineHtml(r) {
  if (!r) return '';
  // the server could not work the wallet out: say so, never show a stale $0
  if (r.error) return `<div class="wallet-line wallet-down">Wallet unavailable right now</div>`;
  if (r.earns === false) return '';
  let right = '';
  if (Number(r.bonus_today_cents) > 0) {
    right = `<span class="wallet-bonus">+${formatBonus(r.bonus_today_cents)} bonus today</span>`;
  } else if (r.next_bonus) {
    right = `<span class="wallet-next">next bonus ${formatBonus(r.next_bonus.cents)} at ${Number(r.next_bonus.at)}</span>`;
  } else if (r.next_level) {
    right = `<span class="wallet-next">${formatCents(r.next_level.cents)} a chore at ${Number(r.next_level.at)}</span>`;
  }
  return `<div class="wallet-line"><span class="wallet-amt">Wallet <b class="num">${formatCents(r.wallet_cents)}</b></span>${right}</div>`;
}

/* What a chore row pays, or a quiet tag on a personal goal. Rows from a
   wall without the wallet carry no kind, and get nothing. Pure. */
function choreMoneyHtml(ch) {
  if (!ch || !ch.kind) return '';
  if (ch.kind === 'personal') return `<span class="chore-money chore-goal">personal goal</span>`;
  if (ch.kind === 'house' && Number(ch.cents) > 0) {
    return `<span class="chore-money num">+${formatCents(ch.cents)}</span>`;
  }
  return '';
}

/* Shown when the hub's PIN key file is gone (a restore onto a new box): no
   PIN can be checked until it is cleared on the box. */
const PIN_KEY_MISSING = 'The parent PIN can’t be checked on this hub right now. '
  + 'Clear it on the hub box (docs/backup-and-restore.md); a new one is optional.';

/* The parent-PIN refusal on a failed write, or null for any other failure:
   {pin: 'required'|'wrong'|'locked'|'key_missing', wait_s}. Pure. */
function pinRefusal(err) {
  if (!err || (err.status !== 403 && err.status !== 429)) return null;
  const d = err.detail;
  if (!d || typeof d !== 'object' || typeof d.pin !== 'string') return null;
  return { pin: d.pin, wait_s: Number(d.wait_s) || 0 };
}

function pinLockMessage(waitS) {
  const mins = Math.max(1, Math.ceil((Number(waitS) || 0) / 60));
  return `Too many wrong tries. Try again in ${mins === 1 ? 'a minute' : `${mins} minutes`}.`;
}

/* ---- Settings: the ⓘ info tips ----
   Every setting label in Settings carries a small ⓘ button. A tap opens a
   short plain explanation right under the label (pushing the rest down), a
   second tap or a tap on another tip closes it. All the words live in this
   one map so they are easy to edit. Keys are the label text, or
   'integ:<id>' for a Features & integrations switch (integrations.py).
   Examples are worded "for example" so they stay true whatever a parent
   sets. A test fails any label drawn without a tip here, and
   test_static.py fails any settings label written without tipLabelHtml.
   Checked against the code when written: change a behavior, change its tip. */
const SETTING_TIPS = {
  // Display
  'Theme': 'Sets the colors of this screen, from bright Light to dark Black. Each screen keeps its own choice, so a phone can be Light while the wall stays Black.',
  'Accent': 'The highlight color for buttons, checkmarks and headings on this screen. While a seasonal look is showing, the look picks this color instead.',
  'Columns': 'How the wall’s columns are set apart. None keeps it plain, Wells puts each column on a soft panel, and Lines draws a thin line between them.',
  'Dim hours': 'When On, every screen in the house dims during these hours, and the seasonal look stops moving. For example, 10pm to 6am dims the screens overnight. This one setting is shared by every screen.',
  // Seasonal looks
  'Mode': 'Off shows the plain theme. Calendar changes the look with the dates, for example a Halloween photo from Oct 1 to Oct 31. Pick one keeps the look you tap all year, and each screen keeps its own choice.',
  // This screen
  'Layout': 'Auto uses the phone layout on a narrow screen and the full wall on a wide one. Desktop always shows the full wall, which helps a TV that gets the phone layout by mistake.',
  'Auto-return': 'On, this screen goes back to the main page when nobody taps for a while. That is a minute and a half on most pages, three minutes on the calendar, and five on a camera or in Settings. Turn it off on a personal phone or TV.',
  // Features & integrations: one per switch
  'integ:chores': 'Shows each person’s chore card, with checkmarks and streaks, on the wall and phones. Off hides the chores, and nothing is deleted.',
  'integ:rewards': 'Lets each finished house chore add money to a wallet on that person’s card. Nothing happens until a parent taps Turn on the wallet in the Chore wallet card.',
  'integ:todos': 'Shows the shared To-Do list. Off hides the card, and the list is kept.',
  'integ:backups': 'Lets the hub save a copy of its data every hour. It only runs once a backup folder is set up, and the Backups card says what is wrong if it can’t.',
  'integ:google_calendar': 'Shows events from this hub’s Google calendars on the Calendar card. They update every five minutes.',
  'integ:ics_calendar': 'Shows events from shared calendar links, such as a school or team schedule, on the Calendar card. They update every five minutes.',
  'integ:icloud_caldav': 'Turns the connected iCloud calendars and Reminders on or off on the wall. Off hides them, and the sign-in is kept.',
  'integ:cameras': 'Shows the home cameras on the wall. Tap one to see it full size.',
  'integ:weather': 'Shows the weather now, the next day’s temperatures, and the next five days.',
  'integ:climate': 'Shows each room’s temperature and humidity. A room turns amber or red when it is too hot, too cold, too damp or too dry, and amber when its sensor stops reporting.',
  'integ:laundry': 'Shows the washer and dryer, how long each has left, and when a load finished. A finished wash that sits turns amber and says Waiting.',
  'integ:away_dim': 'Dims the wall while nobody is home, using Home Assistant. It checks once a minute and dims after hearing nobody is home twice in a row, and stays bright when it can’t tell. Phones don’t dim for this.',
  'integ:fleet': 'Shows whether the home servers and the internet are up.',
  'integ:printer': 'Shows how far along a 3D print is, when it will be done, the temperatures, and each spool in its real color.',
  'To-Do list': 'Where the To-Do card’s list lives. On this hub keeps it here, and iCloud shows your iCloud Reminders so they match your phone. With iCloud, set Sync direction to 2-way to check items off here.',
  'Sync direction': '1-way only reads from iCloud, so nothing done here changes your iCloud data. 2-way sends changes back, so checking off a reminder here checks it off on your phone too.',
  'Calendars': 'The iCloud calendars and reminder lists the hub found. Tap one to show it on the wall or hide it.',
  'Apple ID': 'The Apple ID the hub signs in with. A separate Apple ID just for the hub, with your calendars shared to it, is the safest choice.',
  'App-specific password': 'Not your normal Apple password. Make an app-specific password in your Apple account settings and paste it here.',
  // Chore wallet
  'Parent PIN': 'An optional 4 to 6 digit code. When it is set, paying out, changing amounts and fixing older days ask for it. Without it, anyone at the wall can do those.',
  'Each chore pays': 'What each finished house chore adds to that person’s wallet. For example, at 40¢ a chore, 3 chores is $1.20 that day. Personal goals and one-time items don’t earn money.',
  'Streak bonus': 'A one-time payment when a streak reaches a set number of days, for example $100 at 100 days. After the biggest step the steps start over, so the same streak pays again at 200 days.',
  'More per chore on a streak': 'Pays more for each chore once a streak is long enough. For example, if 25 days or more pays 35¢, someone on a 30 day streak earns 35¢ a chore. The highest step reached counts, and a broken streak goes back to the usual amount.',
  'Shields': 'A shield saves the streak from one missed day, and a missed day uses one by itself. By default you earn one every 20 good days and can hold 2. A saved day doesn’t add to the streak, but it doesn’t end it either.',
  'Fix yesterday until': 'Until this time each morning, yesterday’s chores can still be checked or unchecked without the PIN. For example, at 9:00 AM a chore forgotten last night can be fixed at breakfast. After that, changing yesterday needs the parent PIN, if one is set.',
  'Projected cost': 'What the wallet could cost each week, month and year with these amounts. Perfect streak is the most it can cost, and usual pace is a guess from each person’s last 8 weeks. It updates as you type, before you save.',
  'Wallets': 'What each person has earned since they were last paid. Pay them yourself, then tap Paid to set their wallet back to $0. History shows past weeks, streak bonuses and payouts.',
  'Earns money': 'On, this person’s house chores add money to their wallet. Off is streak only, so their card still shows the streak and shields, but chores add no money and no streak bonus is paid. It applies from now on, and money already earned stays in the wallet until you tap Paid.',
  // Backups
  'Status': 'Whether the hourly copies are running. When they are off, it says what to fix.',
  'Folder': 'Where the copies are saved. Put this folder on a network drive, so the copies survive if the hub’s computer breaks.',
  'Last good backup': 'How long ago the newest good copy was made. With hourly copies it should never be much more than an hour.',
  'Copies kept': 'The hub keeps 48 hourly copies, 14 daily, 8 weekly and 12 monthly, and deletes older ones by itself. So you can go back two days hour by hour, or a year month by month.',
  'Restore test': 'Once a month the hub checks that its newest copy really opens and holds the hub’s data, without touching anything in use. Test restore now runs that check right away.',
  'Settings files': 'With a backup password set, each backup also keeps a locked copy of the hub’s settings files. A new copy is saved only when they change, and the last 14 are kept.',
  'Backup password': 'Locks the settings files inside each backup. You need it to restore them on a new box, so write it down.',
  'Set a backup password': 'Pick a password of at least 8 characters to lock the settings files into each backup. You need it to restore them on a new box, so write it down.',
  'Change the backup password': 'Type the new password twice and tap Save password. Backups made from then on use the new one.',
};

let infoTipOpen = '';    // the one open tip's key ('' none), kept across repaints

/* 'tip-fix-yesterday-until' from a key: a safe, stable element id. Pure. */
function infoTipId(key) {
  return 'tip-' + String(key).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

/* Which tip is open after a tap on `key`: the same one closes, another
   one moves the open tip there. Pure. */
function infoTipNextOpen(cur, key) {
  return cur === key ? '' : key;
}

/* A label with its ⓘ button beside it. `labelHtml` is the label element,
   already escaped; `name` is what the button reads aloud ("What is
   Shields?"). A key with no text draws the label alone (the tests fail it). */
function tipHeadHtml(key, labelHtml, name) {
  if (!SETTING_TIPS[key]) return labelHtml;
  const open = infoTipOpen === key;
  return `<span class="tip-head">${labelHtml}`
    + `<button class="tip-btn" type="button" data-tip="${escapeHtml(key)}" aria-expanded="${open ? 'true' : 'false'}"`
    + ` aria-controls="${infoTipId(key)}" aria-label="What is ${escapeHtml(name || key)}?">`
    + '<span aria-hidden="true">i</span></button></span>';
}

/* The explanation, right under its label: hidden until its button opens it. */
function tipBodyHtml(key) {
  const text = SETTING_TIPS[key];
  if (!text) return '';
  return `<div class="tip-body" id="${infoTipId(key)}" data-tip-body="${escapeHtml(key)}"${infoTipOpen === key ? '' : ' hidden'}>`
    + `${escapeHtml(text)}</div>`;
}

/* The usual Settings label: the label, its ⓘ, and its explanation. `text`
   (escaped HTML) is the label's words when they differ from the key. */
function tipLabelHtml(key, text) {
  const words = text === undefined ? escapeHtml(key) : text;
  return tipHeadHtml(key, `<span class="settings-k">${words}</span>`, key) + tipBodyHtml(key);
}

/* ---- chore wallet: the Settings card (pure renderers, wired in hub.js) ---- */

const RW_ONOFF = (name, on) => `<div class="seg" role="group" aria-label="${name}">`
  + `<button type="button" data-rw-toggle="${name}" data-rw-val="1" class="${on ? 'on' : ''}">On</button>`
  + `<button type="button" data-rw-toggle="${name}" data-rw-val="0" class="${on ? '' : 'on'}">Off</button></div>`;

function rwStepsHtml(kind, steps) {
  const rows = (steps || []).map(([days, amt], i) => (kind === 'ms'
    ? `<div class="rw-step"><input class="txt-input rw-num" type="number" min="1" max="1000" inputmode="numeric" data-rw-ms-days="${i}" value="${Number(days)}" aria-label="Days in a row">`
      + `<span class="rw-word">days pays $</span><input class="txt-input rw-num" type="number" min="0" step="0.01" inputmode="decimal" data-rw-ms-dollars="${i}" value="${(Number(amt) / 100).toFixed(2).replace(/\.00$/, '')}" aria-label="Bonus in dollars">`
    : `<div class="rw-step"><input class="txt-input rw-num" type="number" min="1" max="1000" inputmode="numeric" data-rw-lv-days="${i}" value="${Number(days)}" aria-label="Days in a row">`
      + `<span class="rw-word">days or more pays</span><input class="txt-input rw-num" type="number" min="0" max="500" inputmode="numeric" data-rw-lv-cents="${i}" value="${Number(amt)}" aria-label="Cents a chore"><span class="rw-word">¢ a chore</span>`)
    + `<button class="padmin-btn" type="button" data-rw-del="${kind}:${i}" aria-label="Remove this step">✕</button></div>`).join('');
  return `<div class="rw-steps" data-rw-steps="${kind}">${rows}`
    + `<button class="padmin-btn" type="button" data-rw-add="${kind}">+ Add a step</button></div>`;
}

/* "Nov 26", "today", or "Dec 30, 2026" in another year. Pure. */
function rwSinceLabel(since, today) {
  if (since === today) return 'today';
  const [y, m, d] = String(since).split('-').map(Number);
  if (!y || !m || !d) return String(since || '');
  const short = `${MONTH_SHORT[m - 1]} ${d}`;
  return String(today).slice(0, 4) === String(y) ? short : `${short}, ${y}`;
}

const rwPlural = (n, one) => `${n} ${one}${Math.abs(n) === 1 ? '' : 's'}`;

/* One wallet's summary since the last payout (or since the wallet started),
   in plain words: "Since Nov 26: 12 chores, $3.60. Streak 47 days." Its
   total is the wallet balance (the server splits the balance itself). A
   number that is missing or broken is left out, never shown as $0.00, and
   a streak the server could not read (null) says so instead of "0 days".
   Pure. */
function walletSummaryText(s, today) {
  if (!s) return '';
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const n = num(s.chores) === null ? null : Math.round(s.chores);
  const chore = num(s.chore_cents);
  const bonus = num(s.bonus_cents);
  const total = num(s.total_cents);
  let body = null;
  if (n !== null && chore !== null) {
    if (n === 0 && chore === 0) body = bonus ? 'no chores' : 'no chores yet';
    else if (n < 0) body = `${rwPlural(-n, 'chore')} taken back, ${formatCents(chore)}`;
    else body = `${rwPlural(n, 'chore')}, ${formatCents(chore)}`;
  } else if (n !== null) {
    body = n < 0 ? `${rwPlural(-n, 'chore')} taken back` : rwPlural(n, 'chore');
  } else if (chore !== null) {
    body = `${formatCents(chore)} from chores`;
  }
  const hasBonus = bonus !== null && bonus > 0;
  const showTotal = total !== null && (hasBonus || bonus === null || chore === null);
  const head = s.since ? `Since ${rwSinceLabel(s.since, today)}` : 'Since the last payout';
  let out;
  if (body === null && !hasBonus) {
    out = total !== null ? `${head}: total ${formatCents(total)}.` : `${head}: summary unavailable.`;
  } else {
    out = `${head}: ${body === null ? `a ${formatBonus(bonus)} streak bonus`
      : body + (hasBonus ? `, plus a ${formatBonus(bonus)} streak bonus` : '')}.`;
    if (showTotal) out += ` Total ${formatCents(total)}.`;
  }
  if ('streak' in s) {
    const st = num(s.streak);
    out += st === null ? ' Streak unavailable.' : ` Streak ${rwPlural(Math.round(st), 'day')}.`;
  }
  return out;
}

/* The message after Paid: what the money was for. Pure. */
function payoutToastText(paid, today) {
  if (!paid) return 'Nothing to pay.';
  const s = paid.summary;
  let what = '';
  if (s) {
    const n = Math.round(Number(s.chores) || 0);
    const parts = [];
    if (n > 0) parts.push(rwPlural(n, 'chore'));
    if (Number(s.bonus_cents) > 0) parts.push(`a ${formatBonus(s.bonus_cents)} streak bonus`);
    what = (parts.length ? ` for ${parts.join(' and ')}` : '') + ` since ${rwSinceLabel(s.since, today)}`;
  }
  const amt = typeof paid.cents === 'number' && Number.isFinite(paid.cents) ? ` ${formatCents(paid.cents)}` : '';
  return `Paid${amt}${what}. The wallet is back to $0.`;
}

/* A person's "Earns money" On / Off. Pure. */
function rwEarnsHtml(k) {
  const on = k.earns !== false;
  const btn = (v, label) => `<button type="button" data-rw-earns="${Number(k.id)}" data-rw-val="${v}"`
    + ` class="${(v === '1') === on ? 'on' : ''}" aria-pressed="${(v === '1') === on ? 'true' : 'false'}">${label}</button>`;
  return `<div class="rw-earns theme-ctl"><span class="rw-earns-k">Earns money</span>`
    + `<div class="seg" role="group" aria-label="Earns money for ${escapeHtml(k.name)}">${btn('1', 'On')}${btn('0', 'Off')}</div></div>`;
}

/* One wallet row each. Someone on streak only shows "Streak only" in place
   of an amount and has no summary or Paid, unless money earned before the
   switch went off is still in the wallet: that shows, with Paid, until it
   is paid. Pure. */
function walletPeopleHtml(people, today) {
  const row = (k) => {
    const earns = k.earns !== false;
    const owed = Math.round(Number(k.balance_cents) || 0);
    const money = earns || owed !== 0;
    let note = '';
    if (!earns && owed > 0) note = `Streak only now. The ${formatCents(owed)} earned before stays in the wallet until you tap Paid.`;
    else if (!earns && owed < 0) note = `Streak only now. The wallet still shows ${formatCents(owed)} from before.`;
    return `<div class="padmin-row rw-kid${earns ? '' : ' rw-streak-only'}">`
    + `<span class="padmin-name" style="color:${safeColor(k.color)}">${escapeHtml(k.name)}</span>`
    + (money
      ? `<span class="rw-bal num">${formatCents(k.balance_cents)}</span>`
        + `<button class="padmin-btn" type="button" data-rw-pay="${k.id}"${k.balance_cents > 0 ? '' : ' disabled'}>Paid</button>`
      : `<span class="rw-bal rw-tag">Streak only</span>`)
    + rwEarnsHtml(k)
    + (earns && k.summary ? `<div class="rw-sum">${escapeHtml(walletSummaryText(k.summary, today))}</div>` : '')
    + (note ? `<div class="rw-sum">${escapeHtml(note)}</div>` : '')
    + `<details class="rw-hist"><summary>History</summary>`
    + ((k.history && (k.history.weeks.length || k.history.ledger.length))
      ? `<ul>${k.history.ledger.map((l) => `<li>${escapeHtml(l.day)}: ${l.kind === 'payout' ? 'paid out' : escapeHtml(l.note || 'streak bonus')} ${formatCents(l.cents)}</li>`).join('')}`
        + `${k.history.weeks.map((w) => `<li>Week of ${escapeHtml(w.week_start)}: chores earned ${formatCents(w.earned_cents)}</li>`).join('')}</ul>`
      : `<div class="hint">Nothing yet.</div>`)
    + `</details></div>`;
  };
  const here = people.filter((k) => k.active);
  const gone = people.filter((k) => !k.active);
  return here.map(row).join('')
    + (gone.length ? `<div class="padmin-head"><span class="padmin-head-label">Inactive</span></div>${gone.map(row).join('')}` : '');
}

/* The whole Chore wallet card for a /api/rewards state. Pure. */
function rewardsCardHtml(st) {
  if (!st) return `<div class="hint">Loading…</div>`;
  const intro = `<div class="settings-sub">Each chore adds money to the child’s wallet on their card, with extra for a long streak. You pay them and tap Paid.</div>`;
  if (!st.feature_on) {
    return intro + `<div class="hint">The chore wallet is switched off in Features &amp; integrations above.</div>`;
  }
  if (st.pin_key_missing) {
    return intro + `<div class="hint">${PIN_KEY_MISSING}</div>`;
  }
  // The parent PIN is optional (the owner, 2026-11-27): one row says whether
  // it is on, and offers to add or remove it.
  const pinRow = st.pin_set
    ? `<div class="settings-row">${tipLabelHtml('Parent PIN')}`
      + `<span class="hint">On. Payouts, amounts and fixes to old days ask for it.</span>`
      + `<button class="padmin-btn" type="button" data-rw-clearpin="1">Remove PIN</button></div>`
    : `<div class="settings-row">${tipLabelHtml('Parent PIN')}`
      + `<span class="hint">Off (optional). Anyone at the wall can pay out and change amounts.</span>`
      + `<button class="padmin-btn" type="button" data-rw-setpin="1">Set a PIN</button></div>`;
  if (!st.launch) {
    return intro + `<div class="hint">When it turns on, each child keeps the streak they have now, and every wallet starts at $0.</div>`
      + `<button class="btn-primary" type="button" data-rw-enable="1">Turn on the wallet</button>`
      + pinRow;
  }
  const s = st.settings;
  return intro
    + pinRow
    + `<div class="theme-ctl stack rw-form">`
    + `<div class="settings-row">${tipLabelHtml('Each chore pays')}`
    + `<div class="rw-inline"><input class="txt-input rw-num" type="number" min="0" max="500" inputmode="numeric" name="cents_per_chore" value="${Number(s.cents_per_chore)}" aria-label="Cents a chore"><span class="rw-word">¢ for each house chore done</span></div></div>`
    + `<div class="settings-row">${tipLabelHtml('Streak bonus')}${RW_ONOFF('bonus_on', s.bonus_on)}</div>`
    + `<div class="rw-sub${s.bonus_on ? '' : ' hidden'}" data-rw-for="bonus_on">${rwStepsHtml('ms', s.milestones)}</div>`
    + `<div class="settings-row">${tipLabelHtml('More per chore on a streak')}${RW_ONOFF('levels_on', s.levels_on)}</div>`
    + `<div class="rw-sub${s.levels_on ? '' : ' hidden'}" data-rw-for="levels_on">${rwStepsHtml('lv', s.levels)}</div>`
    + `<div class="settings-row">${tipLabelHtml('Shields')}${RW_ONOFF('shields_on', s.shields_on)}`
    + `<span class="hint">One for every ${Number(s.shield_every)} good days, up to ${Number(s.shield_max)}. A missed day uses one.</span></div>`
    + `<div class="settings-row">${tipLabelHtml('Fix yesterday until')}`
    + `<input class="txt-input rw-time" type="time" name="fix_cutoff" value="${escapeHtml(s.fix_cutoff)}" aria-label="Fix yesterday until"></div>`
    + `<div id="rw-projection" class="rw-projection" aria-live="polite"></div>`
    + `<div class="form-error hidden" id="rw-error"></div>`
    + `<button class="btn-primary" type="button" data-rw-save="1">Save amounts</button>`
    + `</div>`
    + `<div class="padmin rw-people"><div class="padmin-head">${tipHeadHtml('Wallets', '<span class="padmin-head-label">Wallets</span>', 'Wallets')}</div>`
    + tipBodyHtml('Wallets')
    + `<div class="settings-row rw-earns-tip">${tipLabelHtml('Earns money')}`
    + `<span class="hint">On or Off for each person below. Off is streak only.</span></div>`
    + walletPeopleHtml(st.people || [], st.today) + `</div>`;
}

/* ---- Settings > Backups (pure renderers, wired in hub.js) ---- */

/* How long ago an ISO time was, in plain words. A time a little in the
   future (a clock slightly ahead) reads as just now. Pure. */
function backupAgo(iso, nowMs) {
  const t = Date.parse(iso || '');
  if (!Number.isFinite(t)) return 'at a time the hub could not read';
  const s = Math.max(0, (nowMs - t) / 1000);
  if (s < 60) return 'just now';
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'} ago`;
  if (s < 3600) return plural(Math.floor(s / 60), 'minute');
  if (s < 48 * 3600) return plural(Math.floor(s / 3600), 'hour');
  return plural(Math.floor(s / 86400), 'day');
}

/* The day of a restore test in plain words ("November 3", with the year
   when it is not this year), or null for a time that can't be read. Pure. */
const RESTORE_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
function restoreTestDate(iso, nowMs) {
  const t = Date.parse(iso || '');
  if (!Number.isFinite(t)) return null;
  const d = new Date(t);
  const day = `${RESTORE_MONTHS[d.getMonth()]} ${d.getDate()}`;
  const now = new Date(Number.isFinite(nowMs) ? nowMs : Date.now());
  return d.getFullYear() === now.getFullYear() ? day : `${day}, ${d.getFullYear()}`;
}

/* The clock time of an ISO time ("10:08 AM"), or '' when it can't be read. Pure. */
function backupClock(iso) {
  const t = Date.parse(iso || '');
  if (!Number.isFinite(t)) return '';
  return new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/* Why backups are off, and what fixes it, by the server's why_off code. */
const BACKUP_SEE_README = 'Set BACKUP_DIR on the hub box (see Backup in the README).';
const BACKUP_OFF = {
  switched_off: 'Switched off. Turn it on under Features & integrations above.',
  no_folder: `No backup folder is set up. ${BACKUP_SEE_README}`,
  not_mounted: `No backup folder is mounted into the hub. ${BACKUP_SEE_README}`,
  not_writable: 'The hub can’t write to the backup folder. It must belong to the hub’s user on the box (see Backup in the README).',
  demo: 'Off in the demo.',
  // the folder lost the marker the first good run left there: most likely
  // the NAS share dropped and the box's empty mount folder is showing
  marker_missing: 'The backup folder isn’t the one backups went to before. Is the NAS mounted?',
};

/* What is wrong with a new backup password and its second copy, or null.
   The server checks the same length rule (backups.check_password). Pure. */
function backupPasswordProblem(a, b) {
  const pw = String(a || '');
  if (pw.length < 8) return 'The password needs at least 8 characters.';
  if (pw.length > 1024) return 'The password can be 1024 characters at most.';
  if (pw !== String(b || '')) return 'Those didn’t match. Nothing was saved.';
  return null;
}

/* The whole Backups card for a /api/backups state. Pure. */
function backupsCardHtml(st, nowMs) {
  if (!st) return `<div class="hint">Loading…</div>`;
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const row = (k, v) => `<div class="settings-row">${tipLabelHtml(k)}<span class="hint">${v}</span></div>`;
  let html = `<div class="settings-sub">The hub saves a copy of its database every hour into a backup folder. Put that folder on a NAS and the copies leave the box.</div>`;
  const status = st.enabled
    ? (st.scheduled === false ? 'Hourly copies are paused on this hub. Back up now still works.'
      : 'On. A copy every hour.')
    : (BACKUP_OFF[st.why_off] || st.why_off_text || 'Off.');
  html += row('Status', escapeHtml(status));
  if (st.why_off === 'marker_missing') {
    html += `<div class="hint">If you moved backups to a new folder on purpose, tap Use this folder.</div>`
      + `<button class="padmin-btn" type="button" data-bk-adopt="1">Use this folder</button>`;
  }
  if (st.dir) html += row('Folder', `<span class="bk-dir">${escapeHtml(st.dir)}</span>`);
  const ok = st.last_ok;
  html += row('Last good backup', ok ? escapeHtml(backupAgo(ok.at, now)) : 'None yet');
  const err = st.last_error;
  if (err && (!ok || Date.parse(err.at) > Date.parse(ok.at))) {
    html += `<div class="hint bk-error" role="status">The last try failed ${escapeHtml(backupAgo(err.at, now))}. ${escapeHtml(err.message || '')}</div>`;
  }
  const c = st.counts;
  const keep = st.keep || {};
  if (c) {
    html += row('Copies kept', `${Number(c.hourly) || 0} hourly, ${Number(c.daily) || 0} daily, `
      + `${Number(c.weekly) || 0} weekly and ${Number(c.monthly) || 0} monthly. It keeps up to `
      + `${Number(keep.hourly)}, ${Number(keep.daily)}, ${Number(keep.weekly)} and ${Number(keep.monthly)}.`);
  }
  if (st.counts_error) html += `<div class="hint bk-error">${escapeHtml(st.counts_error)}</div>`;
  const busy = !!st.running;
  html += `<button class="btn-primary" type="button" data-bk-run="1"${st.enabled && !busy ? '' : ' disabled'}>`
    + `${busy ? 'Backing up…' : 'Back up now'}</button>`;
  // The monthly restore test: proof the newest copy really opens.
  const rt = st.restore_test;
  if (rt) {
    const day = restoreTestDate(rt.at, now);
    const when = day || 'on a date the hub could not read';
    // A pass that could not open the settings files is only half a pass.
    const text = rt.ok && rt.settings_checked === false
      ? `Database restored ${day ? `on ${day}` : when}. Settings weren’t checked: ${rt.settings_skip || 'the hub did not say why'}.`
      : `Last restore test ${rt.ok ? 'passed' : 'failed'} ${when}.`;
    html += row('Restore test', escapeHtml(text));
    if (!rt.ok && rt.reason) {
      html += `<div class="hint bk-error bk-test-error" role="status">${escapeHtml(rt.reason)}</div>`;
    }
  } else {
    html += row('Restore test', 'Not tested yet. The first test runs a day after the first backup.');
  }
  if (st.restore_test_overdue) {
    html += '<div class="hint bk-error" role="status">No restore test has passed in over 35 days. Check the hub’s log.</div>';
  }
  html += `<button class="padmin-btn" type="button" data-bk-test="1"${st.enabled && !busy && ok ? '' : ' disabled'}>`
    + 'Test restore now</button>';
  // The settings files: only with a backup password, encrypted with it.
  let files;
  if (st.password_problem) {
    files = 'Not saved. Set the backup password again, because its key file on the hub can’t be used.';
  } else if (!st.password_set) {
    files = 'Not saved. Set a backup password below to save them too.';
  } else if (st.settings && st.settings.at) {
    files = `Saved with the backup password, ${backupAgo(st.settings.at, now)}.`;
  } else {
    files = 'Saved with the next backup.';
  }
  html += row('Settings files', escapeHtml(files));
  const set = !!st.password_set;
  // With a password set, say so plainly, with the day it was set when known.
  if (set) {
    const day = restoreTestDate(st.password_set_at, now);
    html += row('Backup password', day ? `Set ✓ on ${escapeHtml(day)}.` : 'Set ✓');
  }
  // What the newest settings copy holds, so "saved" is something you can check.
  const held = set && st.settings && Array.isArray(st.settings.files) ? st.settings.files : [];
  if (held.length) {
    const clock = backupClock(st.settings.at);
    html += `<div class="hint bk-held">Settings locked into the ${clock ? `${escapeHtml(clock)} ` : 'latest '}backup: `
      + `${escapeHtml(held.join(', '))}.</div>`;
  }
  if (st.settings_error) html += `<div class="hint bk-error">${escapeHtml(st.settings_error)}</div>`;
  // Set: the change form folds behind a button. Not set: the form is open.
  if (set) {
    html += `<button class="padmin-btn" type="button" data-bk-changepw="1">Change password</button>`;
  }
  html += `<div class="theme-ctl stack bk-pw${set ? ' hidden' : ''}" id="bk-pw-form">`
    + (set ? tipLabelHtml('Change the backup password') : tipLabelHtml('Set a backup password'))
    + `<div class="hint">It locks a copy of config.json, .env and the sign-ins into each backup.</div>`
    + `<input class="txt-input" type="password" name="bk_pw" autocomplete="new-password" placeholder="At least 8 characters" aria-label="Backup password">`
    + `<input class="txt-input" type="password" name="bk_pw2" autocomplete="new-password" placeholder="The same again" aria-label="The same password again">`
    + `<div class="hint">Write this down. You need it to restore the settings files on a new box.</div>`
    + (set ? `<div class="hint">Copies saved before a change still need the old password.</div>` : '')
    + `<div class="form-error hidden" id="bk-pw-error"></div>`
    + `<div class="bk-pw-btns"><button class="padmin-btn" type="button" data-bk-savepw="1">Save password</button>`
    + (set ? `<button class="padmin-btn" type="button" data-bk-cancelpw="1">Cancel</button>` : '')
    + `</div></div>`;
  return html;
}

/* The form's values -> a settings patch in cents, steps sorted by days and
   blank rows dropped. Pure. */
function rewardsDraft(f) {
  const steps = (rows, amt) => rows
    .filter((r) => String(r.days).trim() !== '' && String(amt(r)).trim() !== '')
    .map((r) => [Math.round(Number(r.days)), Math.round(Number(amt(r)))])
    .sort((a, b) => a[0] - b[0]);
  return {
    cents_per_chore: Math.round(Number(f.cents_per_chore) || 0),
    bonus_on: !!f.bonus_on, levels_on: !!f.levels_on, shields_on: !!f.shields_on,
    fix_cutoff: f.fix_cutoff,
    milestones: steps(f.milestones || [], (r) => (String(r.dollars).trim() === '' ? '' : Number(r.dollars) * 100)),
    levels: steps(f.levels || [], (r) => r.cents),
  };
}

/* The projected cost table. Pure. */
function projectionHtml(proj, people, unsaved) {
  if (!proj) return '';
  const name = (id) => (people.find((k) => k.id === id) || {}).name || '';
  const cells = (v) => `<td class="num">${formatCents(v.week)}</td><td class="num">${formatCents(v.month)}</td><td class="num">${formatCents(v.year)}</td>`;
  const rows = (label, pick) => proj.children.map((c) => `<tr><th scope="row">${escapeHtml(name(c.id))}</th>${cells(c[pick])}</tr>`).join('')
    + `<tr class="rw-total"><th scope="row">Family</th>${cells(proj.total[pick])}</tr>`;
  const table = (label, pick) => `<div class="rw-proj-head">${label}</div>`
    + `<table class="rw-proj"><thead><tr><th></th><th>Week</th><th>Month</th><th>Year</th></tr></thead>`
    + `<tbody>${rows(label, pick)}</tbody></table>`;
  const missing = (proj.partial || []).map(name).filter(Boolean);
  return tipLabelHtml('Projected cost', `Projected cost${unsaved ? ' <span class="rw-unsaved">not saved yet</span>' : ''}`)
    + (missing.length ? `<div class="hint rw-partial">Not counted: ${escapeHtml(missing.join(', '))} (couldn’t read their chores just now).</div>` : '')
    + table('Perfect streak (the most it can cost)', 'perfect')
    + table('Usual pace, about (from the last 8 weeks)', 'usual');
}

async function attemptToggle(id, done, date, headers) {
  const url = `/api/chores/${id}/complete`;
  try {
    if (done) {
      await j(date ? `${url}?date=${encodeURIComponent(date)}` : url,
        { method: 'DELETE', headers: headers || undefined });
    } else {
      await j(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(headers || {}) },
        body: JSON.stringify(date ? { date } : {}),
      });
    }
    attemptToggle.lastError = '';
    attemptToggle.lastPin = null;
    return true;
  } catch (e) {
    // keep the server's reason findable, and readable by the caller
    // (choreToggleMessage) so a refusal that can never succeed isn't told
    // "tap again"
    console.warn('chore toggle failed:', e && e.message);
    attemptToggle.lastError = (e && e.message) || '';
    attemptToggle.lastPin = pinRefusal(e);
    return false;
  }
}
attemptToggle.lastError = '';
attemptToggle.lastPin = null;

/* The toast for a refused chore tap. 'stays done' matches the detail
   app.uncomplete sends (409) for a chore finished before it came off today's
   plan: it is locked, so "tap again" would be a lie. Pure for tests. */
function choreToggleMessage(error, dayEnded) {
  if (dayEnded) return 'That day has ended, so it can’t be changed now.';
  if (String(error || '').includes('stays done')) {
    return 'That one was already finished, so it stays done.';
  }
  return todoFailMessage('');   // the shared generic "couldn't save" copy
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
  // another device checked it off mid-move (app.py todos_order)
  if (String(error || '').includes('checked-off item')) {
    return 'Someone just checked that item off, so it stayed put.';
  }
  // the server's own rule (app.py _validate_todo): retrying can't help
  if (String(error || '').includes('title must be')) {
    return 'That name is too long. Keep it to 120 characters.';
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
  if (st.loading_full) {
    // The calendar overlay's first paint runs on the home feed's narrow window
    // (14 days forward, none back), so days outside it are marked "not synced"
    // that the full fetch is about to fill in. Say it's still loading rather
    // than letting that partial reading look like the final answer. Checked
    // BEFORE the ok-status early return: this payload is ok, just incomplete.
    return 'Loading the full calendar…';
  }
  if (st.ok !== false) return '';
  if (String(st.error || '').includes('not configured')) {
    return 'No calendar is connected yet — add one in settings and the family’s events show up here.';
  }
  if (st.nothing_cached) {
    // The generic copy below promises "the last events we saw", which is a lie
    // when the very first fetch failed and there are none. Say what's true: we
    // have nothing, so nothing shown here can be trusted as complete.
    return 'Can’t reach the calendar — no events are loaded yet.';
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
   testing, testResult, formError, collections, collectionsError, user (the
   typed Apple ID, put back into its field after a redraw)}, NEVER the
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
    return `<div class="field">${tipHeadHtml('Apple ID', '<label for="caldav-user-input">Apple ID</label>', 'Apple ID')}${tipBodyHtml('Apple ID')}`
      + `<input class="txt-input" id="caldav-user-input" type="text" `
      + `value="${escapeHtml(st.user || '')}" `
      + `autocomplete="off" autocapitalize="off" spellcheck="false" ${dis(st.connecting)}></div>`
      + `<div class="field">${tipHeadHtml('App-specific password', '<label for="caldav-pw-input">App-specific password</label>', 'an App-specific password')}${tipBodyHtml('App-specific password')}`
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
  // Parked: edits iCloud refused for good (a read-only list) or whose list is
  // gone. Kept but no longer sent, so not "not yet synced". Not "on the
  // wall": an edit whose list is gone is hidden from the wall.
  const parked = Number(integ.parked) || 0;
  const parkedNote = parked > 0
    ? `<div class="caldav-parked">${parked} change${parked === 1 ? '' : 's'} iCloud would not take (read-only or deleted list); kept but not sent</div>`
    : '';
  // People whose chore list is gone from iCloud (deleted, unshared or moved):
  // the mirror leaves them out until a new list is picked, so say so plainly
  // rather than let the panel look healthy.
  const goneNote = (Array.isArray(integ.lists_gone) ? integ.lists_gone : [])
    .map((name) => `<div class="caldav-gone">${escapeHtml(name)}’s iCloud chore list is gone; pick a new one.</div>`)
    .join('');
  return `<div class="caldav-account">Connected as <strong>${escapeHtml(integ.account || 'unknown')}</strong>`
    + (warn ? `<span class="integ-warn">${warn}</span>` : '')
    + `</div>`
    + goneNote
    + pendingNote
    + parkedNote
    + `<div class="settings-row">`
    + tipLabelHtml('Sync direction')
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
    + tipLabelHtml('Calendars')
    + `<div class="hint">Pick which iCloud calendars and reminder lists show on the wall.</div>`
    + `<div class="caldav-collections-list">`
    + `${caldavCollectionsHtml(st.collections, { error: st.collectionsError })}</div>`
    + `</div>`;
}

/* ------------------------------------------------ trips (out of town) */

/* A trip is stored as away periods whose end_date is the LAST away day. The
   family thinks in "the day we're back", when chores come back, so the sheet
   and banner both show end_date + 1 and the save sends back - 1. */
function tripBackDay(endDate) {
  return endDate ? addDays(endDate, 1) : '';
}

function tripDay(dateStr) {
  const d = parseISODate(dateStr);
  return d ? shortDate(d) : '';
}

/* "Everyone" when the trip takes every person on the wall, else the names in
   wall order: "Ava", "Ava and Milo", "Ava, Milo and Ruby". Pure. */
function tripWhoLabel(personIds, people) {
  const going = new Set((personIds || []).map(Number));
  const all = people || [];
  if (all.length > 1 && all.every((p) => going.has(p.id))) return 'Everyone';
  const names = all.filter((p) => going.has(p.id)).map((p) => p.name);
  // someone on the trip who has since left the wall still counts
  const known = new Set(all.map((p) => p.id));
  const extra = [...going].filter((id) => !known.has(id)).length;
  if (extra) names.push(extra === 1 ? '1 more' : `${extra} more`);
  if (names.length <= 1) return names[0] || '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/* The wall's people plus any traveller not on it (deactivated since the trip
   was set), so a trip always names, and the sheet always shows, everyone on
   it. Pure. */
function tripPeopleWith(people, trip) {
  const out = (people || []).slice();
  const have = new Set(out.map((p) => p.id));
  ((trip && trip.people) || []).forEach((p) => {
    if (!have.has(p.id)) {
      out.push({ id: p.id, name: p.name, color: p.color, active: p.active !== false });
      have.add(p.id);
    }
  });
  return out;
}

/* The Chores card banner: one line per live trip, running ones first. Every
   string goes through escapeHtml; the trip's period ids ride on the buttons
   so a tap acts on exactly the trip that was drawn. Pure. */
function tripBannerHtml(trips, people) {
  return (trips || []).map((t) => {
    const ids = (t.ids || []).map(Number).join(',');
    const who = tripWhoLabel(t.person_ids, tripPeopleWith(people, t));
    const back = tripBackDay(t.end_date);
    let line;
    let buttons;
    if (t.state === 'now') {
      line = back ? `Away, chores resume ${tripDay(back)}` : 'Away, no back day set';
      buttons = `<button class="padmin-btn" type="button" data-trip-end="${ids}">Back early</button>`
        + `<button class="padmin-btn" type="button" data-trip-edit="${ids}">Edit</button>`;
    } else {
      line = `Trip ${tripDay(t.start_date)}`
        + (back ? `, chores resume ${tripDay(back)}` : ', no back day set');
      buttons = `<button class="padmin-btn" type="button" data-trip-edit="${ids}">Edit</button>`
        + `<button class="padmin-btn" type="button" data-trip-end="${ids}">Cancel trip</button>`;
    }
    return `<div class="card trip-banner${t.state === 'now' ? ' now' : ''}">`
      + `<span class="trip-banner-icon" aria-hidden="true">✈</span>`
      + `<span class="trip-banner-text"><span class="trip-banner-line">${escapeHtml(line)}</span>`
      + (who ? `<span class="trip-banner-who">${escapeHtml(who)}</span>` : '')
      + `</span><span class="trip-banner-acts">${buttons}</span></div>`;
  }).join('');
}

/* What the sheet is about to save, in plain words, from the same model the
   payload comes from. Pure. */
function tripSummary(model, people) {
  const who = tripWhoLabel([...model.going], people);
  if (!who) return 'Pick who is going.';
  if (!model.start) return 'Pick the day you leave.';
  if (!model.back) return 'Pick the day chores resume.';
  if (model.back <= model.start) return 'Chores have to resume after the leaving day.';
  const last = addDays(model.back, -1);
  const span = last === model.start ? `on ${tripDay(model.start)}`
    : `${tripDay(model.start)} to ${tripDay(last)}`;
  const verb = who !== 'Everyone' && model.going.size > 1 ? 'are' : 'is';
  return `${who} ${verb} away ${span}. Chores resume ${tripDay(model.back)}.`;
}

/* The request body for POST /api/trip, or a reason it can't be sent. Pure. */
function tripPayload(model) {
  if (!model.going.size) return { error: 'Pick who is going.' };
  if (!model.start) return { error: 'Pick the day you leave.' };
  if (!model.back) return { error: 'Pick the day chores resume.' };
  if (model.back <= model.start) return { error: 'Chores have to resume after the leaving day.' };
  const body = {
    person_ids: [...model.going],
    start_date: model.start,
    end_date: addDays(model.back, -1),
  };
  // 'keep' (an edit of a trip whose travellers have different backups) sends
  // no backup at all, which the server reads as "leave each one as it is".
  // Anything else is sent, null included, so "No backup" really clears it.
  if (model.backup !== 'keep') body.backup_person_id = model.backup ? Number(model.backup) : null;
  if (model.ids && model.ids.length) body.ids = model.ids.slice();
  return { body };
}

/* The trip sheet, built into the shared editor shell. model: {going: Set of
   person ids, start, back ('YYYY-MM-DD' or ''), backup (id, '' or 'keep'),
   mixed (the trip's travellers have different backups), ids (the trip being
   edited, or [])}. people: [{id, name, color}], the wall plus any traveller
   not on it. */
function buildTripForm(host, model, people, onsubmit) {
  const editing = !!(model.ids && model.ids.length);
  host.innerHTML = `
    <div class="trip-title">${editing ? 'Edit trip' : 'Going out of town'}</div>
    <div class="field"><label>Who’s going</label>
      <div class="person-chips" role="group" aria-label="Who is going" data-trip-people></div></div>
    <div class="trip-dates">
      <div class="field"><label>Leaving</label>
        <input class="txt-input" type="date" data-trip-start></div>
      <div class="field"><label>Chores resume</label>
        <input class="txt-input" type="date" data-trip-back></div>
    </div>
    <div class="field" data-trip-backup-field><label>Backup for their chores</label>
      <select class="txt-input" data-trip-backup></select>
      <div class="hint">Chores that take turns pass to whoever is home on their own.</div></div>
    <div class="trip-summary" data-trip-summary></div>
    <div class="form-error hidden" data-trip-error></div>
    <button class="btn-primary" type="button" data-trip-submit>${editing ? 'Save trip' : 'Start trip'}</button>`;
  const $ = (sel) => host.querySelector(sel);
  const startEl = $('[data-trip-start]');
  const backEl = $('[data-trip-back]');
  startEl.value = model.start || '';
  backEl.value = model.back || '';

  const paint = () => {
    $('[data-trip-people]').innerHTML = people.map((p) => {
      const on = model.going.has(p.id);
      return `<button class="person-chip${on ? ' selected' : ''}" type="button" data-trip-person="${p.id}"`
        + ` aria-pressed="${on ? 'true' : 'false'}" style="--pc:${safeColor(p.color)}">`
        + `<span class="pc-dot" aria-hidden="true"></span>${escapeHtml(p.name)}</button>`;
    }).join('');
    // the backup has to be someone staying home; with everyone going there
    // is nobody to pick, so the field goes away and no backup is sent
    // a deactivated traveller taken off the trip can't cover anyone
    const staying = people.filter((p) => !model.going.has(p.id) && p.active !== false);
    if (model.backup !== 'keep' && !staying.some((p) => String(p.id) === String(model.backup))) {
      model.backup = '';
    }
    $('[data-trip-backup-field]').classList.toggle('hidden', !staying.length && !model.mixed);
    $('[data-trip-backup]').innerHTML
      = (model.mixed ? `<option value="keep">Keep each person’s backup</option>` : '')
      + `<option value="">No backup</option>`
      + staying.map((p) => `<option value="${p.id}"${String(p.id) === String(model.backup) ? ' selected' : ''}>`
        + `${escapeHtml(p.name)}</option>`).join('');
    $('[data-trip-backup]').value = model.backup ? String(model.backup) : '';
    $('[data-trip-summary]').textContent = tripSummary(model, people);
  };
  paint();

  $('[data-trip-people]').onclick = (e) => {
    const chip = e.target.closest('[data-trip-person]');
    if (!chip) return;
    const id = Number(chip.dataset.tripPerson);
    if (model.going.has(id)) model.going.delete(id); else model.going.add(id);
    paint();
  };
  const onDate = () => {
    model.start = startEl.value;
    model.back = backEl.value;
    paint();
  };
  startEl.onchange = onDate;
  startEl.oninput = onDate;
  backEl.onchange = onDate;
  backEl.oninput = onDate;
  $('[data-trip-backup]').onchange = () => { model.backup = $('[data-trip-backup]').value; };

  const submit = $('[data-trip-submit]');
  submit.onclick = oneSaveAtATime(submit, () => {
    const errEl = $('[data-trip-error]');
    model.start = startEl.value;
    model.back = backEl.value;
    const { body, error } = tripPayload(model);
    if (error) {
      errEl.textContent = error;
      errEl.classList.remove('hidden');
      return undefined;
    }
    errEl.classList.add('hidden');
    return onsubmit(body, errEl);
  });
}

/* ------------------------------------------------ how it works (help) */

/* The "How it works" topics in Settings, in plain words, one per feature.
   Every line describes what the code does today (checked against hub.js,
   chores.py and app.py when written); change a behavior, change its line
   here. A topic shows only when its feature is on, so the help never
   explains a card this wall doesn't have.
   ctx: {
     enabled: Set of feature ids that are on (chores/todos fail open, see
              hub.js helpContext),
     calendarOn: bool, the calendar card shows (the wall's own rule,
              TAB_FEATURES.cal); falls back to any calendar source on,
     status: {id: 'ok' | 'needs_auth' | 'error' | null}, the integration rows.
              Only laundry, google_calendar and icloud_caldav report a status
              today (app.py _integ_status); every other card has none, so
              the help never claims to know whether it is connecting,
     todoSource: 'local' | 'icloud' (todoSourceOf, never the raw setting),
     todoReadonly: bool, iCloud to-dos are view-only until 2-way sync is on,
     backup: the hub's backup block, or null when it hasn't said,
     dim: the hub's dim block ({hours, away}), or null before it loads,
   }. Pure. */
const HELP_BROKEN = 'This card is not connecting right now. Fix it under Features & integrations below.';
// iCloud's status also goes to "error" when only the copy of chores into
// Reminders fails (app.py _integ_status), so it never claims the card is down.
const HELP_ICLOUD_AUTH = 'The iCloud sign-in is not working right now, so this may be out of date. Fix it under iCloud below.';
const HELP_ICLOUD_ERROR = 'iCloud is reporting a problem. It may only be the copy of chores into Reminders. Check iCloud below.';
// The help list's status lines (hub.js helpListHtml), kept here so the house-
// style test reads every string the help can show.
const HELP_LOADING = 'Loading what this wall has on.';
const HELP_UNREACHABLE = 'The hub could not be reached, so this list may be missing some cards.';
// The integrations that report a health status (app.py _integ_status).
const HELP_STATUS_IDS = new Set(['laundry', 'google_calendar', 'icloud_caldav']);

/* The chore wallet's help, in this wall's own numbers (the hub payload's
   `wallet` rules; the defaults until it loads). Pure. */
function walletHelpParas(w) {
  const pay = 'When you pay someone, open Settings, find Chore wallet and tap Paid. Their wallet goes back to $0 and the payout is kept in their history.';
  const int = (v) => Number.isInteger(v) && v >= 0;
  const steps = (v) => Array.isArray(v) && v.every((x) => Array.isArray(x) && x.length === 2 && int(x[0]) && int(x[1]));
  const ok = w && !w.error && int(w.cents_per_chore) && int(w.shield_every) && int(w.shield_max)
    && steps(w.milestones) && steps(w.levels || []) && /^\d{2}:\d{2}$/.test(String(w.fix_cutoff));
  if (!ok) {
    // Not turned on yet, or its numbers can't be read: say so, never guess.
    return [
      'Each house chore can add money to a wallet on that person’s card, with extra for a long streak. A personal goal stays on the card but doesn’t earn money or count for the streak.',
      w ? 'This wall’s wallet amounts can’t be read right now. Settings, under Chore wallet, shows them.'
        : 'A parent turns it on in Settings, under Chore wallet, and the amounts show here once it is on.',
      pay,
    ];
  }
  const s = { levels: [], ...w };
  const [hh, mm] = String(s.fix_cutoff).split(':').map(Number);
  const cutoff = `${hh % 12 || 12}:${String(mm || 0).padStart(2, '0')} ${hh < 12 ? 'in the morning' : 'pm'}`;
  const bonuses = s.bonus_on && s.milestones.length
    ? s.milestones.map(([d, c]) => `${formatBonus(c)} at ${d} days`).join(', ') : '';
  const paras = [
    `Each house chore adds ${formatCents(s.cents_per_chore)} to the wallet on that person’s card${s.levels_on && s.levels.length ? ', and more on a long streak' : ''}. The row shows what it pays. A personal goal stays on the card but doesn’t earn money or count for the streak.`,
    `The 🔥 line counts days in a row where every house chore got done. A day with nothing due counts too, and away days are skipped.${bonuses ? ` The streak bonus pays ${bonuses}, and starts again after.` : ''}`,
  ];
  if (s.shields_on && s.shield_max > 0) {
    paras.push(`Shields keep one bad day from wiping out a streak. You earn one for every ${s.shield_every} good days, up to ${s.shield_max}. A missed day uses a shield instead of starting over.`);
  }
  paras.push(`Forgot to tap something? Yesterday can be fixed until ${cutoff}. After that a parent needs to enter the PIN.`);
  paras.push('A parent can set someone to streak only, with Earns money under Wallets in Settings. Their streak and shields still count, but their chores add no money and no streak bonus is paid.');
  paras.push(pay);
  return paras;
}

function helpTopics(ctx) {
  const c = ctx || {};
  const on = (id) => !!(c.enabled && c.enabled.has(id));
  // A switched-on card can still be failing (a rejected sign-in, a dead feed).
  // Say so first, but only for the cards that report a status at all.
  const st = (id) => (on(id) && HELP_STATUS_IDS.has(id) && c.status ? c.status[id] : null);
  const bad = (id) => st(id) === 'needs_auth' || st(id) === 'error';
  const withStatus = (ids, paras) => {
    if (ids.some((id) => id !== 'icloud_caldav' && bad(id))) return [HELP_BROKEN, ...paras];
    if (ids.includes('icloud_caldav') && bad('icloud_caldav')) {
      return [st('icloud_caldav') === 'needs_auth' ? HELP_ICLOUD_AUTH : HELP_ICLOUD_ERROR, ...paras];
    }
    return paras;
  };
  const topics = [];
  if (on('chores')) {
    topics.push({
      id: 'chores', title: 'Chores and streaks',
      paras: [
        'Each person has a card in their own color. Tap a chore to check it off. Tap it again to undo.',
        'The 🔥 number is a streak. It counts days in a row where every chore got done. A day with nothing due is skipped, and today does not count against you until it is over.',
        'The little squares show the last seven days, with today outlined. Green means all done, amber means some, red means none, gray means nothing was due, and the accent color means away.',
        'Finish everything for the day and you get confetti.',
        'Tap All chores (or a name) for the big view. Use ‹ and › there to look at other days. Only today can be checked off.',
      ],
    });
    if (on('rewards')) {
      topics.push({ id: 'wallet', title: 'The chore wallet', paras: walletHelpParas(c.wallet) });
    }
    topics.push({
      id: 'trips', title: 'Going out of town',
      paras: [
        'Tap ✈ Trip on the Chores card. Pick who is going, the day you leave, and the day chores resume. Everyone starts ticked, except anyone who is already away. Untick anyone who is staying home.',
        'Chores pause for the trip and come back on their own on the day chores resume. If you get home in the evening and still want that day’s chores, pick that day. If not, pick the next day.',
        'Away days count as rest, so nobody loses a streak. A chore that takes turns passes to whoever is home. If someone stays home, you can make them the backup for the away people’s own chores.',
        'While a trip is on, the Chores card says who is away and when chores resume. Back early ends it now. Edit changes the dates or who is going. You can plan a trip ahead, and a planned trip can be canceled.',
      ],
    });
    topics.push({
      id: 'editing', title: 'Changing chores and people',
      paras: [
        'Open All chores and tap Edit chores. This only works on today.',
        'Tap + New chore to add one, the pencil to change one, or the trash can to delete one. The form asks how often and who does it, and shows who has it on the next few days before you save.',
        'Changes only affect today and later. Past days stay the way they were, so nobody’s streak changes.',
        'Scroll down to People to add someone, rename or recolor them, or set them away. Deactivate takes someone off the chores but keeps them, and Delete removes them for good.',
      ],
    });
  }
  if (on('todos')) {
    let paras;
    if (c.todoSource === 'icloud') {
      paras = withStatus(['icloud_caldav'], [
        'The To-Do card shows your iCloud Reminders, so the same items show on your phone in Reminders.',
        'The card shows what is overdue or due today, up to five. Full list shows everything.',
        c.todoReadonly
          ? 'Right now the list is view-only. To check items off here, set Sync direction to 2-way in Settings, under iCloud.'
          : 'Tap an item to check it off. The change shows up in Reminders too.',
      ]);
    } else {
      paras = [
        'One shared list for things that are not a set chore. Anyone can add an item and anyone can check it off.',
        'Items sit under Now, Soon or Later and stay until they are done.',
        'A checked item stays crossed out for five minutes, so a wrong tap can be undone. After that it moves to recently done for 30 days, where it can be put back.',
        'In the full list, drag an item by its handle to move it, or tap it to rename, move or delete it.',
      ];
    }
    topics.push({ id: 'todos', title: 'To-dos', paras });
  }
  const calIds = ['google_calendar', 'ics_calendar', 'icloud_caldav'];
  if (c.calendarOn !== undefined ? c.calendarOn : calIds.some(on)) {
    topics.push({
      id: 'calendar', title: 'Calendar',
      paras: withStatus(calIds, [
        'The Calendar card shows the next few days. Tap any event for its details.',
        'Month view shows the whole month, a week list and single days.',
        'The calendars update every five minutes. The ↻ button at the top updates them right away and reloads the page.',
      ]),
    });
  }
  if (on('cameras')) {
    topics.push({
      id: 'cameras', title: 'Cameras',
      paras: ['Tap a camera to see it full size. Camera page shows them all.'],
    });
  }
  if (on('weather') || on('climate')) {
    const paras = [];
    if (on('weather')) {
      paras.push('Weather shows the temperature now, a curve for the next day, UV, air quality, humidity and the next five days. Tap Full forecast for more.');
    }
    if (on('climate')) {
      paras.push('House climate shows each room’s temperature and humidity. A room turns amber or red when it runs hot or its sensor stops reporting. Tap Full climate for more.');
    }
    topics.push({ id: 'weather', title: on('weather') && on('climate') ? 'Weather and house climate'
      : (on('weather') ? 'Weather' : 'House climate'), paras });
  }
  if (on('laundry')) {
    topics.push({
      id: 'laundry', title: 'Laundry',
      paras: withStatus(['laundry'], [
        'The ring around each door shows how much of the cycle is left, and the words say what the machine is doing.',
        'When a load finishes it shows a check and the time it finished. Later a finished wash turns amber and says Waiting, until the dryer starts or the washer runs again.',
      ]),
    });
  }
  if (on('printer') || on('fleet')) {
    const paras = [];
    if (on('printer')) {
      paras.push('The 3D printer card shows how far along a print is, when it will be done, the temperatures, and each spool in its real color.');
    }
    if (on('fleet')) paras.push('The Fleet card shows whether the home servers and the internet are up.');
    topics.push({ id: 'printer', title: on('printer') && on('fleet') ? '3D printer and servers'
      : (on('printer') ? '3D printer' : 'Servers'), paras });
  }
  topics.push({
    id: 'phone', title: 'On a phone',
    paras: [
      'On a phone the same page uses tabs along the bottom.',
      'On an iPhone, open the hub in Safari, tap Share, then Add to Home Screen. It then opens full screen like an app.',
    ],
  });
  topics.push({
    id: 'screen', title: 'How the screen behaves',
    paras: [
      ...dimHelpParas(c.dim),
      `If nobody taps for a while, the screen goes back to the main page on its own. That takes ${helpIdleWords()}. Each screen can turn this off under This screen, Auto-return.`,
      'The ⚙ button sets the theme and accent color and turns the seasonal look on or off. All settings, at the bottom of it, has the rest. Each screen remembers its own choices.',
    ],
  });
  topics.push({ id: 'backup', title: 'Backups', paras: [helpBackupLine(c.backup),
    'The hub saves a copy of all the chores, streaks, to-dos and history on its own. Everyone’s progress can be put back from a copy if something goes wrong.',
    'Settings, under Backups, shows where the copies go and when the last one was made. Back up now makes one right away.',
    'Once a month the hub also checks that its newest copy really opens and holds the hub’s data. Settings shows when that last passed, and Test restore now checks it right away.',
    'A backup password also locks a copy of the hub’s settings files into each backup. Only someone with the password can open them, so write it down.',
    'If a copy fails or gets too old, a ⚠ backup warning shows at the top of the screen.'] });
  return topics;
}

/* The screen topic's dim lines, from this hub's `dim` block (the default
   hours until it loads), so the help says what the screens really do. Pure. */
function dimHelpParas(dim) {
  const h = dimHoursOf(dim ? { dim } : null);
  const out = [h.on
    ? `From ${dimHoursWords(h)} every screen dims, and the seasonal look stops moving. Settings, under Display, changes the hours.`
    : 'The screens don’t dim for the time of day. Settings, under Display, sets dim hours.'];
  const away = dim && dim.away;
  if (away && away.enabled === true) {
    out.push('While nobody is home the wall dims too. The hub asks Home Assistant who is home once a minute and dims after it hears nobody is home twice in a row. If it can’t tell, or the hub can’t be reached, the wall stays bright. Phones don’t dim for this.');
  } else if (away) {
    out.push('The wall can also dim while nobody is home. Turn on Dim when nobody is home under Features & integrations.');
  }
  return out;
}

/* The auto-return times in words, read from idleReturnMs so the help can
   never disagree with what the screen does. Pure. */
function helpIdleWords() {
  const mins = (v) => {
    const m = idleReturnMs(v) / 60000;
    if (m === 1.5) return 'a minute and a half';
    const words = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
    return `${words[m] || m} minute${m === 1 ? '' : 's'}`;
  };
  return `${mins('home')} on most pages, ${mins('calendar')} on the calendar, `
    + `${mins('camera')} on a camera and ${mins('settings')} in Settings`;
}

/* The live first line of the Backups topic, from the hub's backup block
   ({known, age_s, stale, remote: {failing, stale}}). Pure. */
function helpBackupLine(b) {
  const ago = (s) => {
    const h = Math.round(s / 3600);
    if (h < 1) return 'less than an hour';
    if (h < 48) return `${h} hour${h === 1 ? '' : 's'}`;
    return `${Math.round(h / 24)} days`;
  };
  if (!b || !b.known) return 'No backup has reported yet. If this stays so, check the hub.';
  // a missing, negative or non-number age is a clock or payload problem
  const age = b.age_s;
  if (typeof age !== 'number' || !Number.isFinite(age) || age < 0) {
    return 'The last backup time looks wrong. Check the hub.';
  }
  if (b.stale) return `The last backup was ${ago(age)} ago, which is too old.`;
  // same split as backupBadge: failing vs merely too old
  if (b.remote && b.remote.failing) return 'The copy off the box is failing. The copy on the hub is fine.';
  if (b.remote && b.remote.stale) return 'The copy off the box is too old. The copy on the hub is fine.';
  return `Last backup ${ago(age)} ago.`;
}

/* The topics as a list of native disclosure rows: tap a title to open it.
   No script state, so it works the same on the wall, a phone and with a
   keyboard or screen reader. `openIds` keeps rows open across a repaint.
   Every string is escaped. Pure. */
function helpHtml(topics, openIds) {
  const open = openIds || new Set();
  return (topics || []).map((t) => `<details class="help-item" data-help="${escapeHtml(t.id)}"${open.has(t.id) ? ' open' : ''}>`
    + `<summary>${escapeHtml(t.title)}</summary>`
    + `<div class="help-body">${(t.paras || []).map((p) => `<p>${escapeHtml(p)}</p>`).join('')}</div>`
    + `</details>`).join('');
}
