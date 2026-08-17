'use strict';

/* Family Hub — the wall page. Polls /api/hub every 60s, renders the calendar +
   per-person chore cards + tiles, and opens full-screen overlays (climate /
   weather / camera iframes, and big calendar / chores views built from this
   page's own DOM) with an idle auto-return. Depends on common.js globals. */

const POLL_MS = 60000;
const CAM_PROBE_MS = 30000;
const CAM_HD_POLL_MS = 700;   // HD-twin liveness RETRY cadence (first check fires immediately)
const CAM_HD_TRIES = 12;      // ~8s budget, then give up and keep the warm stream
const CAM_HD_FADE_MS = 450;   // drop the base after the cross-fade; keep > the .cam-hd-upgrade CSS transition (0.4s)
// Per-attempt timeout for the HD reveal probe: well under J_TIMEOUT_MS (common.js,
// 12s) on purpose. revealHdWhenLive's own ~8s give-up budget (CAM_HD_TRIES x
// CAM_HD_POLL_MS) assumes each attempt resolves quickly; a wedged HD producer
// hanging every attempt for the full 12s would balloon that to minutes instead
// of ~8s, so this is deliberately scoped short relative to the retry loop
// while staying well above the "returns a frame in about a second" happy path.
const CAM_HD_PROBE_TIMEOUT_MS = 3000;

let links = {};
let weatherData = null;   // last /api/tiles/weather payload (native weather card)
let climateData = null;   // last /api/tiles/climate payload (native climate card)
let weatherFails = 0;     // consecutive weather fetch failures (see fetchWeather)
let climateFails = 0;     // consecutive climate fetch failures (see fetchClimate)
let lastIntegrations = [];  // last /api/hub integrations block (settings toggles)
const TILE_FAIL_LIMIT = 3;   // keep the last good card until this many in a row
let warnedNoWeatherSlot = false;   // one-time warn: weather_base set, no 'weather' panel
let warnedNoClimateSlot = false;   // one-time warn: climate_base set, no 'climate' panel
let lastPeople = [];      // remember done-counts to fire the celebration once
const celebrated = new Set();

/* Transient iCloud (CalDAV) settings-panel UI state (Settings overlay,
   Integrations section). Deliberately NEVER the password; that only ever
   lives in the #caldav-pw-input's own value, read at submit time and sent
   once. Kept out of poll()'s refresh loop (see renderCaldavPanel) so a
   Connecting…/Testing… state, a form error, or the last test result doesn't
   get wiped mid-interaction (or mid-typing) by a background poll tick;
   it's reset explicitly where that's the right call (disconnect, a fresh
   Settings-overlay open). `collections` is the Calendars picker's discovered
   iCloud calendars + reminder lists (fetchCaldavCollections), starting empty
   until the first fetch resolves - the same empty state a genuinely empty
   account shows (see caldavCollectionsHtml in common.js), UNLESS
   `collectionsError` is set: a failed refresh keeps the last list shown
   (never wipes it to empty on a transient blip) and flags it so the empty
   state, if the list really is empty, reads as "couldn't load" rather than
   "you have zero calendars" - same rule fetchCalWindow follows for the main
   calendar feed. */
let caldavUi = {
  connecting: false, testing: false, testResult: null, formError: '',
  collections: [], collectionsError: false,
};

/* ----------------------------------------------------------- clock + night */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function tickClock() {
  const d = new Date();
  const wd = WDAYS[d.getDay()];
  document.getElementById('clock-date').textContent =
    `${wd} ${MONTHS[d.getMonth()]} ${d.getDate()}`;
  const hh = d.getHours();
  const h12 = ((hh % 12) || 12);
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  document.getElementById('clock-time').textContent =
    `${h12}:${mm}:${ss}${hh < 12 ? 'am' : 'pm'}`;
  document.body.classList.toggle('is-night', nightClass(hh) === 'is-night');
}

/* --------------------------------------------------------------- calendar */

/* Every event we've seen this session, by id — the detail card reads from
   here whether the row was tapped on the home feed or in the full calendar. */
const evIndex = {};

function indexEvents(evs) {
  (evs || []).forEach((ev) => { evIndex[ev.id] = ev; });
}

/* Old ids from calendar windows that have scrolled into the past are never
   referenced again but would accumulate over a multi-day uptime. Once the index
   grows large, rebuild it from the only two live sources — the home feed and the
   last-fetched month window — which keeps everything currently on screen
   tappable and drops the rest. A no-op until the cap, so normal use is untouched. */
function pruneEvIndex() {
  if (Object.keys(evIndex).length <= 4000) return;
  for (const k in evIndex) delete evIndex[k];
  indexEvents(hubData && hubData.calendar && hubData.calendar.events);
  indexEvents(calWin && calWin.events);
}

function sortDayEvents(evs) {
  return evs.sort((a, b) => (b.all_day - a.all_day) || a.start_ts.localeCompare(b.start_ts));
}

function bucketByDay(evs) {
  const byDay = {};
  (evs || []).forEach((ev) => {
    expandDays(ev).forEach((d) => {       // multi-day all-day events cover
      (byDay[d] = byDay[d] || []).push(ev); // every day of their span
    });
  });
  Object.values(byDay).forEach(sortDayEvents);
  return byDay;
}

function eventRow(ev, day) {
  const color = safeColor(eventColor(ev));
  const ended = eventEnded(ev, Date.now());
  // On a day after the event's start the start time is stale: show the end
  // time on the final day (marked "→"), and treat a full middle day of a
  // multi-day timed span as all-day — never a "→ <final end>" that reads as
  // if it ended that day.
  const continuation = !ev.all_day && day && (ev.start_ts || '').slice(0, 10) < day;
  const endDay = continuation && day === lastVisibleDay(ev);
  const allDayCell = `<span class="cal-allday" style="border-color:${escapeHtml(color)};color:${escapeHtml(color)}">all day</span>`;
  let timeCell;
  if (ev.all_day || (continuation && !endDay)) {
    timeCell = allDayCell;
  } else if (continuation) {
    timeCell = `<span class="cal-time num">→ ${escapeHtml(fmtTime(ev.end_ts))}</span>`;
  } else {
    timeCell = `<span class="cal-time num">${escapeHtml(fmtTime(ev.start_ts))}</span>`;
  }
  return `<div class="cal-ev${ended ? ' ended' : ''}" data-eid="${escapeHtml(ev.id)}" tabindex="0">`
    + `<span class="cal-rail" style="background:${escapeHtml(color)}"></span>`
    + timeCell
    + `<span class="cal-title">${escapeHtml(ev.title)}</span>`
    + `</div>`;
}

/* Agenda list over [startStr, startStr+maxDays). `skipEmptyAfter` keeps the
   home feed compact (empty days beyond tomorrow vanish); the full calendar
   shows every day. `win` (optional, the calendar payload's sync window) marks
   an empty day that falls OUTSIDE it as "not synced" rather than "nothing
   scheduled": the day browser can page past the synced range (issue #37),
   and a genuinely free day must stay visually distinct from one Google was
   never asked about. `win` is optional (not every caller has fetched a
   calendar payload with a `window` field) and a missing/omitted one simply
   never marks anything (isDayOutsideWindow fails open): the 5-day home feed
   passes its own calendar.window too, it just never reaches far enough
   forward to trip it under the default sync window. */
function agendaHtml(events, startStr, todayStr, maxDays, skipEmptyAfter, win) {
  const byDay = bucketByDay(events);
  let html = '';
  for (let i = 0; i < maxDays; i++) {
    const d = addDays(startStr, i);
    const evs = byDay[d] || [];
    if (skipEmptyAfter != null && i >= skipEmptyAfter && evs.length === 0) continue;
    const isToday = d === todayStr;
    const unsynced = evs.length === 0 && isDayOutsideWindow(d, win);
    html += `<div class="card cal-day${isToday ? ' is-today' : ''}${unsynced ? ' cal-day-unsynced' : ''}">`
      + dayHeadHtml(d, todayStr)
      + (evs.length ? evs.map((ev) => eventRow(ev, d)).join('')
        : unsynced
          ? `<div class="cal-empty">not synced yet — this day is past the synced window</div>`
          : `<div class="cal-empty">nothing scheduled</div>`)
      + `</div>`;
  }
  return html;
}

function calStatusNote(cal) {
  // Branch logic lives in common.js calStatusMessage (pure, tested).
  const msg = calStatusMessage(cal.status);
  return msg ? `<div class="cal-note">${escapeHtml(msg)}</div>` : '';
}

/* The ONE section header, generated one way for every section (Task 2). Emits
   the shared `.shead` markup — a tick, the label, and (only when BOTH an overlay
   key and an expand label are given) a `.expand` button that opens that overlay.
   The ⛶ glyph is prepended here, so callers pass clean labels. Every
   config/user-derived value (label, expandLabel, overlay) is escaped — panelHtml
   feeds config-derived p.label / p.id through here. */
function sectionHead(label, { overlay, expandLabel, chip } = {}) {
  const chipHtml = chip ? `<span class="shead-chip">${escapeHtml(chip)}</span>` : '';
  const act = (overlay && expandLabel)
    ? `<span class="act"><button class="expand" type="button"`
      + ` data-overlay="${escapeHtml(overlay)}">⛶ ${escapeHtml(expandLabel)}</button></span>`
    : '';
  return `<div class="shead"><span class="tick"></span><h2>${escapeHtml(label)}</h2>${chipHtml}${act}</div>`;
}

function renderCalendar(data) {
  indexEvents(data.calendar.events);
  document.getElementById('cal').innerHTML =
    sectionHead('Calendar', { overlay: 'calendar', expandLabel: 'Month view' })
    + calStatusNote(data.calendar)
    + agendaHtml(data.calendar.events, data.date, data.date, 5, 2, data.calendar.window);
}

/* ---------------------------------------------------- full calendar view */

/* State for the full-screen calendar: month grid <-> agenda week <-> one-day
   drill-in, paging over the synced window (cal past/future days). */
const calState = { mode: 'month', y: 0, m: 0, weekStart: '', day: '' };
let calWin = null;    // {status, events} — the full cached window

async function fetchCalWindow() {
  try {
    calWin = await j('/api/calendar?days=90&past=45');
    indexEvents(calWin.events);
  } catch (e) {
    // On a failed refresh keep the cached events, but DON'T preserve a stale
    // ok:true status — downgrade it so the existing "showing the last events we
    // saw" banner fires instead of painting hours-old events as current.
    console.warn('calendar window refresh failed; keeping cached events', e);
    calWin = calWin
      ? { ...calWin, status: { ok: false, error: 'unreachable' } }
      : { status: { ok: false, error: 'unreachable' }, events: [] };
  }
}

/* `win` (the calendar payload's sync window, optional) marks a cell OUTSIDE it
   as "not synced" (issue #37): the month view can page arbitrarily far past
   the range the backend actually caches, and an out-of-window day rendered
   the same as an in-window free day is a confident-but-wrong "nothing here":
   the family would trust an empty grid Google actually has events on. Only an
   in-month, EMPTY, out-of-window cell is marked: same rule as agendaHtml for
   "empty" (the backend never caches events past its own window, so a stray
   cached event on an out-of-window day never happens in practice, but this
   keeps one from rendering under a contradictory "not synced" mark); and
   `.mg-out` (adjacent-month padding, already opacity-dimmed) is excluded so
   the mark is never rendered at .mg-out's low opacity, which would make the
   hatch and caption nearly illegible right where they're least needed (the
   grid's own filler cells, not the page the family is actually reading). */
function monthCellHtml(cell, byDay, todayStr, win) {
  const evs = byDay[cell.date] || [];
  const shown = evs.slice(0, 3);
  const more = evs.length - shown.length;
  const unsynced = cell.inMonth && evs.length === 0 && isDayOutsideWindow(cell.date, win);
  const cls = ['mg-day'];
  if (!cell.inMonth) cls.push('mg-out');
  if (cell.date === todayStr) cls.push('mg-today');
  if (unsynced) cls.push('mg-unsynced');
  const dayNum = Number(cell.date.slice(8, 10));
  return `<div class="${cls.join(' ')}" data-date="${cell.date}" tabindex="0">`
    + `<span class="mg-num num">${dayNum}</span>`
    + shown.map((ev) =>
      `<span class="mg-ev" data-eid="${escapeHtml(ev.id)}">`
      + `<span class="mg-dot" style="background:${safeColor(eventColor(ev))}"></span>`
      + `<span class="mg-ev-title">${escapeHtml(ev.title)}</span></span>`).join('')
    + (more > 0 ? `<span class="mg-more">+${more} more</span>` : '')
    + (unsynced ? `<span class="mg-unsynced-mark">not synced</span>` : '')
    + `</div>`;
}

function monthHtml(y, m, events, todayStr, win) {
  const byDay = bucketByDay(events);
  const heads = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    .map((d) => `<span class="mg-head">${d}</span>`).join('');
  const cells = monthGrid(y, m).map((c) => monthCellHtml(c, byDay, todayStr, win)).join('');
  return `<div class="mgrid">${heads}${cells}</div>`;
}

function calNavHtml(title) {
  const seg = (mode, label) => {
    const active = calState.mode === mode
      || (mode === 'month' && calState.mode === 'day');
    return `<button class="seg-btn${active ? ' active' : ''}"`
      + ` type="button" data-calview="${mode}">${label}</button>`;
  };
  return `<div class="cal-nav">`
    + `<button class="cal-nav-btn" type="button" data-calnav="prev">‹</button>`
    + `<button class="cal-nav-btn cal-nav-today" type="button" data-calnav="today">Today</button>`
    + `<button class="cal-nav-btn" type="button" data-calnav="next">›</button>`
    + `<span class="cal-nav-title">${escapeHtml(title)}</span>`
    + `<span class="spacer"></span>`
    + `<div class="segmented">${seg('month', 'Month')}${seg('agenda', 'Week')}</div>`
    + `</div>`;
}

function renderCalFull() {
  const host = document.getElementById('cal-full');
  if (!host) return;
  const todayStr = data_date || new Date().toISOString().slice(0, 10);
  const events = (calWin && calWin.events) || [];
  const win = calWin && calWin.window;   // the backend's actual sync range (issue #37)
  let title = '';
  let body = '';
  if (calState.mode === 'day') {
    title = monthName(calState.y, calState.m);
    body = `<button class="cal-back" type="button" data-calback="1">‹ back to month</button>`
      + agendaHtml(events, calState.day, todayStr, 1, null, win);
  } else if (calState.mode === 'agenda') {
    title = monthName(calState.y, calState.m);
    body = agendaHtml(events, calState.weekStart, todayStr, 7, null, win);
  } else {
    title = monthName(calState.y, calState.m);
    body = monthHtml(calState.y, calState.m, events, todayStr, win);
  }
  host.innerHTML = calStatusNote(calWin || { status: {} })
    + calNavHtml(title) + `<div class="cal-body">${body}</div>`;
}

function calGoToday() {
  const todayStr = data_date || new Date().toISOString().slice(0, 10);
  calState.y = Number(todayStr.slice(0, 4));
  calState.m = Number(todayStr.slice(5, 7));
  calState.weekStart = todayStr;
  calState.day = todayStr;
}

function calNav(dir) {
  if (dir === 'today') { calGoToday(); if (calState.mode === 'day') calState.mode = 'month'; renderCalFull(); return; }
  const step = dir === 'next' ? 1 : -1;
  if (calState.mode === 'agenda') {
    calState.weekStart = addDays(calState.weekStart, step * 7);
    calState.y = Number(calState.weekStart.slice(0, 4));
    calState.m = Number(calState.weekStart.slice(5, 7));
  } else {
    const s = shiftMonth(calState.y, calState.m, step);
    calState.y = s.y; calState.m = s.m;
    if (calState.mode === 'day') calState.mode = 'month';
  }
  renderCalFull();
}

/* ------------------------------------------------------ event detail card */

function openEventDetail(eid) {
  const ev = evIndex[eid];
  if (!ev) return;
  const color = safeColor(eventColor(ev));
  const todayStr = data_date || '';
  const dayLine = `${dayLabel(ev.start_ts.slice(0, 10), todayStr)} · ${fmtTimeRange(ev)}`;
  const loc = ev.location
    ? `<div class="ev-loc">📍 ${escapeHtml(ev.location)}</div>` : '';
  let desc = '';
  if (ev.description) {
    const text = descToText(ev.description);
    const trimmed = text.length > 700 ? `${text.slice(0, 700)}…` : text;
    if (trimmed) desc = `<div class="ev-desc">${escapeHtml(trimmed)}</div>`;
  }
  const chip = ev.label
    ? `<span class="ev-chip" style="border-color:${escapeHtml(color)};color:${escapeHtml(color)}">${escapeHtml(ev.label)}</span>`
    : '';
  document.getElementById('ev-card').innerHTML =
    `<button class="ev-close" type="button">✕</button>`
    + `<div class="ev-rail" style="background:${escapeHtml(color)}"></div>`
    + `<div class="ev-title">${escapeHtml(ev.title)}</div>`
    + `<div class="ev-when num">${escapeHtml(dayLine)}</div>`
    + chip + loc + desc;
  document.getElementById('ev-modal').classList.remove('hidden');
  if (openView) armIdle();
}

function closeEventDetail() {
  document.getElementById('ev-modal').classList.add('hidden');
}

/* --------------------------------------------------------------- people */

function weekStripHtml(week) {
  return `<div class="week-strip">` + week.map((st, i) => {
    const today = i === week.length - 1 ? ' ws-today' : '';
    return `<span class="ws-cell ws-${st}${today}"></span>`;
  }).join('') + `</div>`;
}

function choreRowHtml(ch, firstName, opts = {}) {
  const { readonly = false, editing = false } = opts;
  const icon = ch.icon ? `<span class="chore-icon">${escapeHtml(ch.icon)}</span>` : '';
  const rot = ch.rot ? `<span class="chore-rot">↻ ${escapeHtml(firstName)}</span>` : '';
  const cls = `chore-row${ch.done ? ' done' : ''}${readonly ? ' readonly' : ''}${editing ? ' is-editing' : ''}`;
  const body = `<span class="chore-check">✓</span>`
    + `<span class="chore-body">${icon}<span class="chore-title">${escapeHtml(ch.title)}</span>${rot}</span>`;
  if (readonly) return `<div class="${cls}">${body}</div>`;   // past/future: look, don't touch
  // edit mode: the tap opens the editor (Task 5), it does NOT complete the
  // chore — so the row carries data-edit-chore, never data-chore. The trash
  // control is a plain <span> (not a nested <button>, which is invalid inside a
  // button); its data-del-chore is checked BEFORE data-edit-chore in the click
  // handler, so tapping delete opens the confirm, never the editor.
  if (editing) return `<button class="${cls}" type="button" data-edit-chore="${ch.id}">`
    + body
    + `<span class="chore-edit-hint" aria-hidden="true">✎</span>`
    + `<span class="chore-del" data-del-chore="${ch.id}"`
    + ` aria-label="Delete ${escapeHtml(ch.title)}">🗑</span>`
    + `</button>`;
  return `<button class="${cls}" type="button" data-chore="${ch.id}">${body}</button>`;
}

function personCardHtml(p, opts = {}) {
  const { readonly = false, editing = false } = opts;
  const first = (p.person.name || '').split(' ')[0];
  // The 🔥 count is chore-days finished in a row (a day with no chores neither
  // counts nor breaks it; see chores.streak), NOT today's completed count. The
  // tooltip spells this out since the bare number can't; no visible sub-label.
  const streak = p.streak >= 2
    ? `<span class="chip-streak" title="${p.streak} chore days finished in a row (a day with no chores neither counts nor breaks it)">`
      + `🔥 ${p.streak}</span>` : '';
  const rows = p.chores.length
    ? p.chores.map((ch) => choreRowHtml(ch, first, { readonly, editing })).join('')
    : (editing ? '' : `<div class="cal-empty">nothing this day</div>`);
  // edit mode grows a per-person "+ Add chore" row (wired in Task 5); it's the
  // only way to reach the add editor for a person, so it shows even for someone
  // with no chores yet.
  const addRow = editing
    ? `<button class="chore-row chore-add" type="button" data-add-chore="${p.person.id}">`
      + `<span class="chore-check chore-add-plus">+</span>`
      + `<span class="chore-body"><span class="chore-title">Add chore</span></span>`
      + `</button>`
    : '';
  // --pc drives the done check + strike color: completion wears YOUR color
  return `<div class="card person-card${editing ? ' is-editing' : ''}" data-person="${p.person.id}" style="--pc:${safeColor(p.person.color)}">`
    + `<div class="person-head">`
    + `<span class="person-name" style="color:${safeColor(p.person.color)}">${escapeHtml(p.person.name)}</span>`
    + streak
    + weekStripHtml(p.week)
    + `</div>`
    + rows
    + addRow
    + `</div>`;
}

/* ------------------------------------------------- full chores day browser */

const choreState = { day: '', editing: false };

function choresNavHtml() {
  const todayStr = data_date;
  const label = dayLabel(choreState.day, todayStr);
  // The Edit/Done toggle only appears on today — history isn't editable.
  const editToggle = choreState.day === data_date
    ? `<span class="spacer"></span>`
      + `<button class="cal-nav-btn ch-edit-toggle${choreState.editing ? ' active' : ''}"`
      + ` type="button" data-chedit="1">${choreState.editing ? 'Done' : 'Edit'}</button>`
    : '';
  return `<div class="cal-nav">`
    + `<button class="cal-nav-btn" type="button" data-chnav="prev">‹</button>`
    + `<button class="cal-nav-btn cal-nav-today" type="button" data-chnav="today">Today</button>`
    + `<button class="cal-nav-btn" type="button" data-chnav="next">›</button>`
    + `<span class="cal-nav-title">${escapeHtml(label)}</span>`
    + `<span class="cal-nav-date num">${escapeHtml(choreState.day)}</span>`
    + editToggle
    + `</div>`;
}

async function renderChoresFull(prefetched) {
  const host = document.getElementById('chores-full');
  if (!host) return;
  let people = prefetched;
  if (!people) {
    try {
      people = (await j(`/api/chores/day?date=${choreState.day}`)).people;
    } catch (e) {
      host.innerHTML = choresNavHtml()
        + `<div class="cal-empty">couldn’t load that day — is the hub reachable?</div>`;
      return;
    }
  }
  const readonly = choreState.day !== data_date;
  // editing is a today-only mode; a readonly (past/future) day can never be in it
  const editing = choreState.editing && !readonly;
  // The inline people editor needs the flat people list (colors + active flags)
  // that /api/chores/day doesn't carry. When it's already cached we render it in
  // this same pass; otherwise we fetch it and re-render once it lands, so the
  // person cards still paint instantly on the first pass.
  let peopleAdmin = '';
  if (editing) {
    if (choreAdminPeople) peopleAdmin = peopleAdminHtml(choreAdminPeople);
    else if (choreAdminError) peopleAdmin = `<div class="cal-empty">couldn’t load people — is the hub reachable? Tap Done, then Edit to retry.</div>`;
    else ensurePeopleThenRerender();
  } else {
    choreAdminPeople = null;   // drop stale cache when leaving edit
    choreAdminError = false;
  }
  host.innerHTML = choresNavHtml()
    + (people.length
      ? people.map((p) => personCardHtml(p, { readonly, editing })).join('')
      : `<div class="cal-empty">no people yet</div>`)
    + peopleAdmin;
}

/* Fetch the flat people list, cache it, and repaint the chores view once — but
   only if edit mode is still open on today (the fetch may outlive it). On
   failure the cache stays null so a later render retries; the editor just
   doesn't appear rather than wedging the view. */
async function ensurePeopleThenRerender() {
  if (choreAdminLoading) return;   // a fetch from an earlier render pass is in flight
  choreAdminLoading = true;
  try {
    choreAdminPeople = (await j('/api/admin/state')).people;
    choreAdminError = false;
  } catch (e) {
    choreAdminError = true;   // render a visible note instead of vanishing
  } finally {
    choreAdminLoading = false;
  }
  if (openView === 'chores' && choreState.editing && choreState.day === data_date) {
    renderChoresFull(hubData ? hubData.people : null);
  }
}

/* Flat people list (id/name/color/active) for the inline people editor —
   /api/chores/day doesn't carry colors or active flags, so edit mode pulls
   /api/admin/state. Cached while editing; nulled on leave and after any people
   mutation so the next render re-fetches fresh. `choreAdminError` records a
   failed load so the section shows a visible note instead of vanishing
   silently. */
let choreAdminPeople = null;
let choreAdminError = false;
let choreAdminLoading = false;   // in-flight guard: don't stack concurrent fetches

/* The inline people-management section, shown under the person cards in edit
   mode: rename/recolor, deactivate/reactivate, hard-delete, and add a person —
   the whole household editable from the wall. */
function peopleAdminHtml(people) {
  const rows = (people || []).map((p) =>
    `<div class="padmin-row${p.active ? '' : ' inactive'}" data-padmin="${p.id}">`
    + `<span class="padmin-name" style="color:${safeColor(p.color)}">${escapeHtml(p.name)}</span>`
    + `<button class="padmin-btn" type="button" data-pedit="${p.id}">Edit</button>`
    + `<button class="padmin-btn" type="button" data-ptoggle="${p.id}">${p.active ? 'Deactivate' : 'Activate'}</button>`
    + `<button class="padmin-btn padmin-del" type="button" data-pdel="${p.id}">Delete</button>`
    + `</div>`).join('');
  return `<div class="padmin">`
    + `<div class="padmin-head">People</div>`
    + rows
    + `<button class="chore-row chore-add" type="button" data-padd="1">`
    + `<span class="chore-check chore-add-plus">+</span>`
    + `<span class="chore-body"><span class="chore-title">Add person</span></span>`
    + `</button>`
    + `</div>`;
}

function renderPeople(data) {
  const host = document.getElementById('people');
  if (!data.people.length) {
    host.innerHTML = `<div class="empty-hub">No people yet`
      + `<div class="empty-sub">tap All chores, then Edit to add your family</div></div>`;
    lastPeople = [];
    return;
  }
  host.innerHTML =
    sectionHead('Chores', { overlay: 'chores', expandLabel: 'All chores' })
    + data.people.map((p) => personCardHtml(p, { readonly: false, editing: false })).join('');
  fireCelebrations(data.people);
  lastPeople = data.people;
}

/* Wall-only: the to-do card sits under the calendar, in its own grid slot
   (operator, 2026-08-14) — separate from the person cards it used to trail. */
function renderTodoSlot(data) {
  const host = document.getElementById('todo-slot');
  // The To-Do surface can be backed by the local whiteboard (default) or by
  // iCloud Reminders (settings picker). Render whichever the operator chose —
  // but fall back to local if iCloud is no longer available (e.g. disconnected
  // out-of-band) so it never strands on a reassuring-but-empty iCloud card.
  const caldavAvail = (data.integrations || []).some((i) => i.id === 'icloud_caldav');
  if (data.todo_source === 'icloud' && caldavAvail) {
    host.innerHTML = reminderCardHtml(data.reminders, !!data.reminders_writable);
    return;
  }
  // todos_ok===false means the server's todos read/group threw and it shipped
  // empty buckets as a fail-soft. Render a "couldn't load" note, NOT a
  // reassuring empty card the family would read as "all caught up".
  host.innerHTML = todoCardHtml(data.todos, data.todos_ok !== false);
}

/* ---------------------------------------------------------------- to-dos */

/* One shared household list, no people linkage. The wall card renders from
   the hub payload every poll; the full view (overlay on the wall, To-Dos tab
   on phones) fetches /api/todos on entry and after its own writes ONLY, so a
   60s poll can never wipe a half-typed add box. */
const todoState = { data: null, reminders: null, source: 'local', addBucket: 'now', openId: null };

function todoRowHtml(t, full) {
  const done = !!t.done_at;
  const checkLabel = done ? 'mark not done' : 'mark done';
  if (!full) {
    return `<button class="todo-row${done ? ' done' : ''}" type="button" data-todo="${t.id}" aria-label="${checkLabel}: ${escapeHtml(t.title)}">`
      + `<span class="todo-check">✓</span>`
      + `<span class="todo-title">${escapeHtml(t.title)}</span></button>`;
  }
  const isOpen = todoState.openId === t.id;
  const actions = isOpen
    ? `<div class="todo-actions">`
      + ['now', 'soon', 'later'].filter((b) => b !== t.bucket).map((b) =>
        `<button class="todo-act" type="button" data-todo-move="${b}" data-tid="${t.id}">→ ${b}</button>`).join('')
      + `<button class="todo-act todo-act-del" type="button" data-todo-del="${t.id}">delete</button>`
      + `</div>`
    : '';
  return `<div class="todo-row-full${done ? ' done' : ''}">`
    + `<button class="todo-row-main" type="button" data-todo="${t.id}" aria-label="${checkLabel}: ${escapeHtml(t.title)}">`
    + `<span class="todo-check">✓</span></button>`
    + `<button class="todo-body" type="button" data-todo-open="${t.id}">`
    + `<span class="todo-title">${escapeHtml(t.title)}</span></button>`
    + actions
    + `</div>`;
}

/* Counts OPEN items only (not done_at) in one bucket, so the wall card can
   render three independent chips: N now / N soon / N later. */
function todoBucketCount(list) {
  return (list || []).filter((t) => !t.done_at).length;
}

function todoCardHtml(todos, ok = true) {
  const b = todos || {};
  const nowItems = (b.now || []).slice(0, 5);
  const rows = !ok
    // NOT "is the hub reachable?" — /api/hub just answered, so it demonstrably
    // is; this is an internal read error (logged server-side at ERROR). Don't
    // send the family to power-cycle a router that's fine.
    ? `<div class="cal-empty">couldn’t load the list — something went wrong</div>`
    : nowItems.length
      ? nowItems.map((t) => todoRowHtml(t, false)).join('')
      : `<div class="cal-empty">nothing on the list</div>`;
  const chips = [['now', true], ['soon', false], ['later', false]]
    .map(([bk, isNow]) => `<span class="chip${isNow ? ' now' : ''}">`
      + `${todoBucketCount(b[bk])} ${bk}</span>`).join('');
  // Header stays OUTSIDE the box (sectionHead, Task 2); the rows + count
  // chips are the boxed unit, matching .cal-day/.person-card (Task 3).
  return sectionHead('To-Do', { overlay: 'todos', expandLabel: 'Full list' })
    + `<div class="card todo">`
    + rows
    + `<div class="foot">${chips}</div>`
    + `</div>`;
}

/* ------------------------------------------------- iCloud reminders view */

/* The To-Do surface, backed by iCloud Reminders instead of the local list.
   Reminders arrive already grouped by due (overdue/today/upcoming/no_date) and
   with completed ones dropped, so every row shown is open. When two-way is on
   (`writable`) a row is a tap-to-complete control; when it's read-only the same
   rows render inert — same information, no controls — matching how read-only
   chores render (look, don't touch). */
const REM_BUCKETS = [['overdue', 'Overdue'], ['today', 'Today'],
  ['upcoming', 'Upcoming'], ['no_date', 'No date']];

/* A high-priority reminder gets a single quiet "!" mark (shape + text, never
   colour alone) so an urgent item stands out on the wall. RFC 5545 priority is
   1 (highest) .. 9 (lowest); Apple's "High" is 1. Treat 1-4 as high; medium/low
   stay unmarked to keep the surface calm. */
function reminderPriHtml(priority) {
  const p = Number(priority);
  if (!Number.isFinite(p) || p < 1 || p > 4) return '';
  return `<span class="rem-pri" title="High priority" aria-label="High priority">!</span>`;
}

/* The exact due date, shown only where the bucket alone is ambiguous: "upcoming"
   spans days to weeks, and "overdue" wants the how-long-ago. Today/no_date carry
   their date in the bucket label already, so they stay clean. */
function reminderDueHtml(bucket, due) {
  if (!due || (bucket !== 'upcoming' && bucket !== 'overdue')) return '';
  return `<span class="rem-due">${escapeHtml(dayLabel(due.slice(0, 10), data_date))}</span>`;
}

function reminderRowHtml(r, full, writable) {
  const title = escapeHtml(r.title);
  const id = escapeHtml(r.id);
  // title + due + priority ride together in a .todo-body so the meta sits inline
  // beside the title (not as separate flex children of the row).
  const body = `<span class="todo-title">${title}</span>`
    + reminderDueHtml(r.bucket, r.due) + reminderPriHtml(r.priority);
  if (!full) {
    // Home card (wall): a compact tap-to-complete row, or an inert one when
    // read-only. The inert row carries no data-reminder, so a tap no-ops.
    if (!writable) {
      return `<div class="todo-row"><span class="todo-check"></span>`
        + `<span class="todo-body">${body}</span></div>`;
    }
    return `<button class="todo-row" type="button" data-reminder="${id}"`
      + ` aria-label="mark done: ${title}"><span class="todo-check">✓</span>`
      + `<span class="todo-body">${body}</span></button>`;
  }
  // Full view. Read-only: a plain row, no check button, no delete affordance.
  if (!writable) {
    return `<div class="todo-row-full"><span class="todo-check"></span>`
      + `<span class="todo-body">${body}</span></div>`;
  }
  const isOpen = String(todoState.openId) === String(r.id);
  const actions = isOpen
    ? `<div class="todo-actions">`
      + `<button class="todo-act todo-act-del" type="button" data-reminder-del="${id}">delete</button>`
      + `</div>`
    : '';
  return `<div class="todo-row-full">`
    + `<button class="todo-row-main" type="button" data-reminder="${id}"`
    + ` aria-label="mark done: ${title}"><span class="todo-check">✓</span></button>`
    + `<button class="todo-body" type="button" data-reminder-open="${id}">${body}</button>`
    + actions
    + `</div>`;
}

/* Wall home card: the pressing reminders (overdue + today, overdue first, up to
   five) plus three count chips. Mirrors todoCardHtml's shape so the To-Do slot
   reads the same whichever source backs it. */
function reminderCardHtml(buckets, writable) {
  const b = buckets || {};
  const tag = (bk) => (b[bk] || []).map((r) => ({ ...r, bucket: bk }));
  const pressing = [...tag('overdue'), ...tag('today')].slice(0, 5);
  const rows = pressing.length
    ? pressing.map((r) => reminderRowHtml(r, false, writable)).join('')
    : `<div class="cal-empty">nothing on the list</div>`;
  const chips = [['overdue', true], ['today', false], ['upcoming', false]]
    .map(([bk, lead]) => `<span class="chip${lead ? ' now' : ''}">`
      + `${(b[bk] || []).length} ${bk}</span>`).join('');
  return sectionHead('To-Do', { overlay: 'todos', expandLabel: 'Full list', chip: 'iCloud' })
    + `<div class="card todo">`
    + rows
    + `<div class="foot">${chips}</div>`
    + `</div>`;
}

/* The add row for the reminders full view. Zero lists hides it (handled by the
   caller); one list targets it directly with a naming placeholder; more than one
   adds a compact list picker so a wall-added reminder lands in the right list. */
function reminderAddHtml(lists) {
  const single = lists.length === 1;
  const placeholder = single ? `Add to ${lists[0].name}…` : 'Add a reminder…';
  const select = single ? ''
    : `<select id="todo-list-select" class="todo-list-select" aria-label="Reminder list">`
      + lists.map((l, i) => `<option value="${escapeHtml(l.id)}"${i === 0 ? ' selected' : ''}>`
        + `${escapeHtml(l.name)}</option>`).join('')
      + `</select>`;
  return `<form id="todo-add-form" class="todo-add">`
    + `<input id="todo-add-input" maxlength="120" placeholder="${escapeHtml(placeholder)}"`
    + ` autocomplete="off" aria-label="Add a reminder">`
    + select
    + `<button class="cal-nav-btn" type="submit">Add</button>`
    + `</form>`;
}

function remindersFullHtml() {
  const r = todoState.reminders;
  if (!r) {
    return `<div class="cal-empty">couldn’t load reminders — is the hub reachable?</div>`;
  }
  if (r.configured === false) {
    return `<div class="cal-empty">iCloud isn’t connected — add it in Settings.</div>`;
  }
  const writable = !!r.writable;
  const buckets = r.buckets || {};
  const lists = (hubData && hubData.reminder_lists) || [];
  const total = REM_BUCKETS.reduce((n, [bk]) => n + (buckets[bk] || []).length, 0);
  const sections = REM_BUCKETS
    .filter(([bk]) => (buckets[bk] || []).length)
    .map(([bk, label]) => `<div class="card pad todo-section">`
      + `<div class="todo-sec-head">${label}</div>`
      + buckets[bk].map((rm) => reminderRowHtml({ ...rm, bucket: bk }, true, writable)).join('')
      + `</div>`).join('');
  const add = (writable && lists.length) ? reminderAddHtml(lists) : '';
  return `<div class="shead"><span class="shead-chip">iCloud</span></div>`
    + add
    + `<div class="rem-list">`
    + (total ? sections : `<div class="cal-empty">nothing on the list</div>`)
    + `</div>`;
}

function todosFullHtml() {
  if (todoState.source === 'icloud') return remindersFullHtml();
  const d = todoState.data;
  if (!d) {
    return `<div class="cal-empty">couldn’t load the list — is the hub reachable?</div>`;
  }
  const seg = ['now', 'soon', 'later'].map((bk) => {
    const active = todoState.addBucket === bk;
    return `<button class="seg-btn${active ? ' active' : ''}"`
      + ` type="button" data-todo-bucket="${bk}">${bk[0].toUpperCase()}${bk.slice(1)}</button>`;
  }).join('');
  const section = (b, label) => {
    const items = d.buckets[b] || [];
    // Boxed like every other section (Task 3 consistency pass) — .pad is the
    // existing bare-.card padding utility, unused until now.
    return `<div class="card pad todo-section"><div class="todo-sec-head">${label}</div>`
      + (items.length ? items.map((t) => todoRowHtml(t, true)).join('')
        : `<div class="cal-empty">nothing here</div>`)
      + `</div>`;
  };
  const recent = (d.recent_done || []).map((t) =>
    `<div class="todo-row-full done"><span class="todo-check todo-check-static">✓</span>`
    + `<span class="todo-title">${escapeHtml(t.title)}</span>`
    + `<button class="todo-act" type="button" data-todo-restore="${t.id}">restore</button></div>`).join('');
  return `<form id="todo-add-form" class="todo-add">`
    + `<input id="todo-add-input" maxlength="120" placeholder="Add a to-do…" autocomplete="off" aria-label="Add a to-do">`
    + `<div class="segmented">${seg}</div>`
    + `<button class="cal-nav-btn" type="submit">Add</button>`
    + `</form>`
    + `<div class="todo-cols">`
    + section('now', 'Now') + section('soon', 'Soon') + section('later', 'Later')
    + `</div>`
    + `<details class="todo-recent"><summary>recently done</summary>`
    + (recent || `<div class="cal-empty">nothing in the last 30 days</div>`)
    + `</details>`;
}

/* Paint into the overlay host when it exists, else the phone tab section,
   clearing the other so #todo-add-input never exists twice in the DOM. */
function renderTodosPaint() {
  const overlayHost = document.getElementById('todos-full');
  const pageHost = document.getElementById('todos-page');
  const host = overlayHost || pageHost;
  if (!host) return;
  const other = overlayHost ? pageHost : null;
  if (other) other.innerHTML = '';
  const prev = document.getElementById('todo-add-input');
  const draft = prev ? prev.value : '';
  const wasOpen = !!document.querySelector('.todo-recent[open]');
  host.innerHTML = todosFullHtml();
  const inp = document.getElementById('todo-add-input');
  if (inp && draft) inp.value = draft;
  const recent = host.querySelector('.todo-recent');
  if (recent && wasOpen) recent.open = true;
}

async function renderTodosFull() {
  // The full view follows the same source as the home card. Read it from the
  // last hub payload so the fetch below hits the right endpoint.
  const source = (hubData && hubData.todo_source) || 'local';
  todoState.source = source;
  const icloud = source === 'icloud';
  const had = icloud ? todoState.reminders != null : todoState.data != null;
  try {
    if (icloud) todoState.reminders = await j('/api/reminders');
    else todoState.data = await j('/api/todos');
  } catch (e) {
    // keep the last data (or null -> unreachable message). But if this was a
    // REFRESH (data already populated from a prior load) a silent catch would
    // let the stale pre-mutation list sit on screen while the conn badge
    // still says live, so surface it instead of hiding a failed post-mutation
    // GET behind reassuring-looking old data.
    if (had) showToast('Couldn’t refresh the list — check the hub.');
  }
  renderTodosPaint();
}

function todosViewActive() {
  return openView === 'todos' || document.body.dataset.tab === 'todos';
}

async function refreshTodos() {
  await poll();                                  // wall card + hub payload
  if (todosViewActive()) await renderTodosFull(); // full view, when showing
}

async function toggleTodo(id, done) {
  const r = await attemptTodo(`/api/todos/${id}/complete`, done ? 'DELETE' : 'POST');
  if (!r.ok) showToast(todoFailMessage(r.error));
  await refreshTodos();
}

async function addTodo() {
  const input = document.getElementById('todo-add-input');
  const title = ((input && input.value) || '').trim();
  if (!title) return;
  const r = await attemptTodo('/api/todos', 'POST',
    { title, bucket: todoState.addBucket });
  if (!r.ok) { showToast(todoFailMessage(r.error)); return; }
  if (input) input.value = '';
  await refreshTodos();
}

async function moveTodo(id, bucket) {
  const r = await attemptTodo(`/api/todos/${id}`, 'PATCH', { bucket });
  if (!r.ok) showToast(todoFailMessage(r.error));
  todoState.openId = null;
  await refreshTodos();
}

async function deleteTodo(id) {
  const r = await attemptTodo(`/api/todos/${id}`, 'DELETE');
  if (!r.ok) showToast(todoFailMessage(r.error));
  todoState.openId = null;
  await refreshTodos();
}

/* ------- iCloud reminder writes (two-way; only wired when writable) ------- */

/* Check off / reopen a reminder. The click handler flips the row's .done class
   first (optimistic), then this writes and refreshes; a completed reminder drops
   out of the open buckets on the next read, so a checked row simply disappears. */
async function toggleReminder(id, completed) {
  const r = await attemptTodo('/api/reminders/toggle', 'POST', { id, completed });
  if (!r.ok) {
    showToast(reminderFailMessage(r.error));
    // The write failed, so UNDO the click handler's optimistic .done flip. Can't
    // lean on refreshTodos()'s repaint: its poll() only repaints the home card
    // inside a successful try, so offline (write fails AND the poll fails too)
    // the flipped row would strand looking "done". Repaint both surfaces from
    // the UNCHANGED cache instead — same as the chore/todo paths, which show the
    // unchanged row offline (they just never optimistically flip in the first
    // place). renderTodosPaint no-ops when no full view is mounted.
    if (hubData) renderTodoSlot(hubData);
    renderTodosPaint();
    return;
  }
  await refreshTodos();
}

async function addReminder() {
  const input = document.getElementById('todo-add-input');
  const title = ((input && input.value) || '').trim();
  if (!title) return;
  const lists = (hubData && hubData.reminder_lists) || [];
  if (!lists.length) return;                       // no target list -> nothing to do
  const sel = document.getElementById('todo-list-select');
  const listId = sel ? sel.value : lists[0].id;    // single list needs no picker
  const r = await attemptTodo('/api/reminders/add', 'POST', { list_id: listId, title });
  if (!r.ok) { showToast(reminderFailMessage(r.error)); return; }
  if (input) input.value = '';
  await refreshTodos();
}

async function deleteReminder(id) {
  const r = await attemptTodo('/api/reminders/delete', 'POST', { id });
  if (!r.ok) showToast(reminderFailMessage(r.error));
  todoState.openId = null;
  await refreshTodos();
}

/* Confetti + flash the first time a person finishes all their chores today. */
function fireCelebrations(people) {
  // Prune keys from earlier days so the Set can't grow unbounded across a
  // multi-day wall uptime (one key per person per completed day).
  for (const k of celebrated) { if (!k.endsWith(`:${data_date}`)) celebrated.delete(k); }
  people.forEach((p) => {
    const key = `${p.person.id}:${data_date}`;
    const complete = p.total > 0 && p.done_count === p.total;
    if (!complete) { celebrated.delete(key); return; }
    if (celebrated.has(key)) return;
    // only celebrate a *transition* to complete (not on first load of a done day)
    const prev = lastPeople.find((q) => q.person.id === p.person.id);
    if (!prev || prev.done_count === prev.total) { celebrated.add(key); return; }
    celebrated.add(key);
    const card = document.querySelector(`.person-card[data-person="${p.person.id}"]`);
    if (card) celebrate(card, p.person.color);
  });
}

function celebrate(card, color) {
  card.classList.add('card-celebrate');
  const burst = document.createElement('div');
  burst.className = 'confetti';
  for (let i = 0; i < 8; i++) {
    const bit = document.createElement('span');
    bit.className = 'confetti-particle';
    bit.style.background = color;
    bit.style.setProperty('--dx', `${Math.round((i - 3.5) * 22)}px`);
    bit.style.setProperty('--dy', `${28 + (i % 3) * 16}px`);
    burst.appendChild(bit);
  }
  card.appendChild(burst);
  setTimeout(() => { card.classList.remove('card-celebrate'); burst.remove(); }, 1000);
}

/* --------------------------------------------------------------- tiles */

function tileCamera(cam) {
  if (cam.demo) return tileCameraDemo(cam);
  // Live WebRTC embed (sub-second), not a snapshot loop — the 5s JPEG refresh
  // read as choppy on the wall. Starts offline; the probe flips it live.
  const src = escapeHtml(cam.src);
  return `<button class="card tile tile-camera is-offline" type="button" data-overlay="camera:${src}" data-cam="${src}">`
    + `<div class="tile-label">${escapeHtml(cam.label)}</div>`
    + `<iframe class="cam-frame hidden" title="${escapeHtml(cam.label)} camera"></iframe>`
    + `<span class="tile-live hidden">● LIVE</span>`
    + `<span class="tile-offline">offline</span>`
    + `</button>`;
}

/* Demo mode: no go2rtc stream to embed, so paint a static gradient placeholder
   with a small "DEMO VIEW" marker in the same tile chrome. The tile still opens
   full-screen (a matching full-bleed placeholder); the probe skips it. */
function tileCameraDemo(cam) {
  const src = escapeHtml(cam.src);
  const tone = cam.tone === 'warm' ? 'warm' : 'cool';
  return `<button class="card tile tile-camera" type="button" data-overlay="camera:${src}" data-cam="${src}">`
    + `<div class="tile-label">${escapeHtml(cam.label)}</div>`
    + `<div class="cam-demo cam-demo-${tone}"><span class="cam-demo-mark">Demo view</span></div>`
    + `<span class="tile-live">● LIVE</span>`
    + `</button>`;
}

/* Weather/climate glances live in the always-on panels (2026-08-12: the
   small duplicate tiles were removed); cameras are config-driven tiles. */
let tilesBuilt = false;
function initTiles() {
  if (tilesBuilt || !links.cameras) return;
  const cams = links.cameras || [];
  document.getElementById('tiles').innerHTML = cams.length
    ? sectionHead('Cameras', { overlay: 'cameras-page', expandLabel: 'Camera page' })
      + cams.map(tileCamera).join('')
    : '';
  tilesBuilt = true;
}

/* Cameras-tab 2x2 live grid (phones/tablets). Same config-driven tiles as the
   wall column, but from the camera_page list and laid out 2-up by CSS (see
   .camgrid). Hidden on the wall; shown only under the mobile Cameras tab. */
let camGridBuilt = false;
function initCamGrid() {
  // Guard on links.camera_page like initTiles guards on links.cameras: don't
  // latch built until the links payload has arrived.
  if (camGridBuilt || !links.camera_page) return;
  const el = document.getElementById('camgrid');
  if (!el) return;
  el.innerHTML = links.camera_page.map(tileCamera).join('');
  camGridBuilt = true;
}

/* Live iframes can't report stream health cross-origin, so a snapshot probe
   per camera toggles each tile's live/offline state (shares the same go2rtc
   producer as the stream — cheap). All cameras probe in PARALLEL: the probes
   are independent, and a serial loop made every tile queue behind the slowest
   probe ahead of it (a cold snapshot is seconds). */
async function probeCamera() {
  // Probe the UNION of the wall column and the Cameras-tab grid, deduped by
  // src: one camera can appear on both surfaces (its snapshot producer is
  // shared, so one probe serves every tile of that src — see probeOneCamera).
  const bySrc = new Map();
  for (const cam of [...(links.cameras || []), ...(links.camera_page || [])]) {
    if (!bySrc.has(cam.src)) bySrc.set(cam.src, cam);
  }
  await Promise.all([...bySrc.values()].map(probeOneCamera));
}

async function probeOneCamera(cam) {
  if (cam.demo) return;   // placeholder tile: no stream, nothing to probe
  // A src can render on two surfaces (wall column + Cameras-tab grid); update
  // EVERY tile for it. One snapshot probe drives them all — the producer is
  // shared — but each tile owns its own frame/live/offline state.
  const tiles = document.querySelectorAll(`.tile-camera[data-cam="${cam.src}"]`);
  if (!tiles.length) return;
  // Start each VISIBLE tile's stream WITH the probe, not after it: both share
  // the same go2rtc producer, so the WebRTC connect overlaps the probe's
  // round-trip and the tile paints on the stream's next keyframe instead of
  // queueing behind the snapshot. The frame stays hidden (offline badge up)
  // until the probe confirms live — a dead camera never shows a black frame.
  // Never start a stream into a hidden tile (mobile: cams live behind their
  // tab; offsetParent is null while display:none).
  tiles.forEach((tile) => {
    const frame = tile.querySelector('.cam-frame');
    if (frame && !frame.src && tile.offsetParent !== null) frame.src = cam.tile;
  });
  let ok = false;
  try {
    // fetchTimeout (common.js): bounds the probe with J_TIMEOUT_MS so a
    // connected-but-unresponsive server can't leave it in flight forever.
    ok = (await fetchTimeout(`/api/tiles/camera.jpg?src=${encodeURIComponent(cam.src)}&probe=${Date.now()}`)).ok;
  } catch (e) { /* down (or timed out) */ }
  // Offline -> live transition: reload the frame for a deterministic fresh
  // connect. A player that connected against a DEAD producer may never have
  // established its session, and its self-reconnect is go2rtc internals this
  // repo doesn't control — don't trust it to revive under the LIVE badge.
  // Healthy cycles never touch src (reassigning restarts a live stream), and
  // the reload happens only while the frame is hidden behind the offline
  // badge, so nothing visible ever cold-restarts.
  tiles.forEach((tile) => {
    const frame = tile.querySelector('.cam-frame');
    if (!frame) return;
    if (ok && frame.dataset.reconnect) {
      delete frame.dataset.reconnect;
      frame.src = cam.tile;
    } else if (!ok && frame.src) {
      frame.dataset.reconnect = '1';
    }
    frame.classList.toggle('hidden', !ok);
    tile.querySelector('.tile-live').classList.toggle('hidden', !ok);
    tile.querySelector('.tile-offline').classList.toggle('hidden', ok);
    tile.classList.toggle('is-offline', !ok);
  });
}

/* ------------------------------------------------------------ mobile tabs */

/* The tab bar only renders under the CSS mobile breakpoint; each tab shows a
   slice of the one page. Panels and camera frames were display:none while
   hidden, so entering a tab re-fits panels / starts the paused streams. */
/* Jump the page back to the top. window.scrollTo alone is unreliable on iOS
   Safari (it gets ignored right after a tap / during momentum scrolling), so
   also zero the scrolling element and both document roots — whichever one is
   actually the scroller then lands at the top. */
function scrollPageToTop() {
  scrollTo(0, 0);
  const se = document.scrollingElement;
  if (se) se.scrollTop = 0;
  if (document.documentElement) document.documentElement.scrollTop = 0;
  if (document.body) document.body.scrollTop = 0;
  // On the phone the scroller is .wrap (the app-shell content region), not the
  // window — reset it too so a tab tap lands at the top there as well.
  const wrap = document.querySelector('.wrap');
  if (wrap) wrap.scrollTop = 0;
}

function setTab(tab) {
  document.body.dataset.tab = tab;
  document.querySelectorAll('.tab-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'weather') { wirePanels(); fitPanels(); }  // deferred while hidden
  if (tab === 'cams') probeCamera();
  if (tab === 'todos') renderTodosFull();
  scrollPageToTop();   // every tab tap lands at the top, not mid-scroll
}

/* --------------------------------------------------------------- overlays */

const overlay = () => document.getElementById('overlay');
let idleTimer = null;
let openView = null;

function makeIframe(src) {
  const f = document.createElement('iframe');
  f.className = 'overlay-frame';
  f.src = src;
  return f;
}

/* Full-screen view of a FIXED-SIZE kiosk page (the 1024x600 almanac sheet):
   scale it to fill the screen (upscaling allowed) and center it — embedding
   it raw leaves the sheet adrift in its own bezel surround. */
function makeFittedIframe(src, vw, vh) {
  const f = document.createElement('iframe');
  f.className = 'overlay-frame overlay-fitted';
  const scale = Math.min(innerWidth / vw, innerHeight / vh);
  f.style.width = `${vw}px`;
  f.style.height = `${vh}px`;
  f.style.transform = `scale(${scale})`;
  f.style.left = `${Math.round((innerWidth - vw * scale) / 2)}px`;
  f.style.top = `${Math.round((innerHeight - vh * scale) / 2)}px`;
  f.src = src;
  return f;
}

/* Full-screen a camera without the cold-start black wait. The tile's stream is
   already warm on the wall (its tile iframe is a live consumer), so show that
   immediately as the base layer. If the camera has a distinct HD twin, load it
   in front (transparent) and reveal it ONLY once its stream is confirmed live —
   we probe the HD snapshot rather than trust a blind timer, so a dead or slow HD
   never blanks the wall: the warm base stays until the HD is proven, and stays
   for good if the HD never comes up. Once the HD is live we cross-fade and drop
   the base so a single stream decodes. */
function openCameraFull(content, cam, view) {
  if (cam.demo) {
    // No stream to full-screen; show the same gradient placeholder full-bleed.
    const tone = cam.tone === 'warm' ? 'warm' : 'cool';
    const ph = document.createElement('div');
    ph.className = `cam-demo cam-demo-${tone} cam-demo-full`;
    ph.innerHTML = '<span class="cam-demo-mark">Demo view</span>';
    content.appendChild(ph);
    return;
  }
  const base = makeIframe(cam.tile || cam.full);
  content.appendChild(base);
  if (!cam.has_hd) return;   // no distinct HD stream — the warm stream is all there is
  const hd = makeIframe(cam.full);
  hd.classList.add('cam-hd-upgrade');   // absolute-fill in front, starts transparent
  content.appendChild(hd);
  revealHdWhenLive(cam, view, base, hd, 0);
}

/* Poll the HD stream's snapshot; reveal it only when it actually answers.
   The first check fires immediately — the probe itself is the wait (go2rtc
   connects the HD's RTSP session and returns a frame in about a second), so a
   built-in first-poll delay just added CAM_HD_POLL_MS of warm-stream time to
   every full-screen open. Retries stay on the CAM_HD_POLL_MS cadence. */
function revealHdWhenLive(cam, view, base, hd, tries) {
  setTimeout(async () => {
    if (openView !== view || !hd.parentNode) return;   // overlay closed / switched
    let live = false;
    try {
      // fetchTimeout (common.js), scoped to CAM_HD_PROBE_TIMEOUT_MS (not the
      // longer default): a wedged HD producer must not hang this retry loop
      // long enough to blow its own ~8s give-up budget (see the constant above).
      live = (await fetchTimeout(
        `/api/tiles/camera.jpg?src=${encodeURIComponent(cam.hd_src)}&probe=${Date.now()}`,
        CAM_HD_PROBE_TIMEOUT_MS)).ok;
    } catch (e) { /* still connecting, down, or timed out: treated as not-yet-live */ }
    if (openView !== view || !hd.parentNode) return;   // re-check after the await
    if (live) {
      hd.classList.add('ready');   // HD confirmed live — cross-fade it over the warm base
      setTimeout(() => {
        if (openView === view && base.parentNode) base.remove();   // one stream from here
      }, CAM_HD_FADE_MS);
    } else if (tries + 1 < CAM_HD_TRIES) {
      revealHdWhenLive(cam, view, base, hd, tries + 1);   // keep waiting on the warm stream
    } else {
      hd.remove();   // HD never came up in budget — drop the dead layer, keep the warm stream
    }
  }, tries === 0 ? 0 : CAM_HD_POLL_MS);
}

function openOverlay(view) {
  const content = document.getElementById('overlay-content');
  content.innerHTML = '';
  openView = view;
  if (view.indexOf('panel:') === 0) {
    const p = (links.panels || []).find((x) => x.id === view.slice(6));
    // 'fit' scales a fixed vw x vh sheet to fill the screen centered;
    // 'native' embeds the (viewport-responsive) page raw.
    if (p) content.appendChild(p.full === 'fit'
      ? makeFittedIframe(p.full_url || p.url, p.vw, p.vh)
      : makeIframe(p.full_url || p.url));
  } else if (view.indexOf('camera:') === 0) {
    // Resolve a camera tap from either surface. First match wins, so a src on
    // both lists uses the wall entry — fine because a shared src carries the
    // same hd/full config in both; grid-only cameras (the ones that can differ)
    // live only in camera_page and resolve there.
    const cam = [...(links.cameras || []), ...(links.camera_page || [])]
      .find((c) => c.src === view.slice(7));
    if (cam) openCameraFull(content, cam, view);
  } else if (view === 'cameras-page') {
    // Full-screen 2x2 live grid — the "camera page" reachable from the wall's
    // Cameras header (the wall has no tab bar). Same tiles as the mobile
    // Cameras tab, from camera_page. Tapping a tile still opens that one cam
    // full-screen (each tile keeps its data-overlay="camera:<src>"). Streams
    // are started by the probe below, AFTER the overlay opens (a tile's stream
    // must never start while it's display:none — see probeOneCamera).
    const cams = links.camera_page || [];
    // Guard the empty case: the header button shows whenever the wall has any
    // camera, but camera_page can be empty (e.g. every entry dropped as
    // malformed server-side). Never open a featureless black overlay — say so.
    content.innerHTML = cams.length
      ? `<div class="camera-page">${cams.map(tileCamera).join('')}</div>`
      : `<div class="camera-page camera-page-empty">No cameras configured.</div>`;
  } else if (view === 'calendar') {
    content.innerHTML = `<div class="overlay-panel"><div id="cal-full"></div></div>`;
    calState.mode = 'month';
    calGoToday();
    renderCalFull();                       // instant paint from cache
    fetchCalWindow().then(renderCalFull);  // then refresh from the API
  } else if (view === 'chores') {
    content.innerHTML = `<div class="overlay-panel"><div id="chores-full"></div></div>`;
    choreState.day = data_date;
    choreState.editing = false;   // always open in check-off mode
    renderChoresFull(hubData ? hubData.people : null);  // instant paint, today
  } else if (view === 'todos') {
    content.innerHTML = `<div class="overlay-panel"><div id="todos-full"></div></div>`;
    todoState.source = (hubData && hubData.todo_source) || 'local';
    const cache = todoState.source === 'icloud' ? todoState.reminders : todoState.data;
    if (cache) renderTodosPaint();           // instant paint from cache
    renderTodosFull();                       // then refresh from the API
  } else if (view === 'settings') {
    content.innerHTML = `<div class="overlay-panel"><div id="settings-full"></div></div>`;
    caldavUi.formError = '';   // a stale validation message shouldn't outlive a reopen
    renderSettingsFull();      // instant paint from cache (hubData / lastIntegrations)
  }
  overlay().classList.add('open');
  // Lock the page behind the overlay: the tall wall page keeps its own
  // scrollbar otherwise, which shows beside a full-screen overlay (the camera
  // page especially). Cleared in closeOverlay.
  document.body.classList.add('overlay-open');
  // Start + probe the grid streams now that the overlay is visible (offsetParent
  // is non-null once .open is set), so the live tiles connect and reveal.
  if (view === 'cameras-page' && (links.camera_page || []).length) probeCamera();
  armIdle();
}

function closeOverlay() {
  overlay().classList.remove('open');
  document.body.classList.remove('overlay-open');
  document.getElementById('overlay-content').innerHTML = '';
  openView = null;
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  scrollPageToTop();   // coming home always lands at the top of the page
}

/* The fixed-position modals that layer above #overlay (siblings, not
   children): closeAllOverlays (below) and wallBusy (~1500) both need this
   exact set (one to close them, the other to detect any of them still shown),
   so it's named once here rather than hardcoded twice, which is exactly how
   the idle-timer/#overlay-home pair drifted apart in the first place (issue
   #34: only closeOverlay() knew to close the overlay, nothing closed these). */
const MODAL_CLOSERS = {
  'ev-modal': () => closeEventDetail(),
  'chore-modal': () => closeChoreEditor(),
  'confirm-modal': () => closeDeleteConfirm(),
};

/* Close EVERY full-screen surface in one call: the MODAL_CLOSERS modals are
   fixed siblings of #overlay, not children of it, so closeOverlay() alone
   leaves any of them stranded open over the home wall. Shared by the
   #overlay-home tap and the idle auto-return (armIdle) below so the two
   "go home" paths can't drift apart again. */
function closeAllOverlays() {
  Object.values(MODAL_CLOSERS).forEach((close) => close());
  closeOverlay();
}

/* Whether this device drifts back to the home wall after an idle timeout. The
   shared wall wants it (a public dashboard shouldn't sit on whatever overlay
   someone left open); a personal phone/TV opts out via Settings -> Display so
   it stays on the view being read. Default ON: only an explicit "off" (stamped
   by theme.js from fh.idleReturn) disables it, so a device where theme.js never
   ran still auto-returns. */
function idleReturnEnabled() {
  return document.documentElement.getAttribute('data-idle-return') !== 'off';
}

function armIdle() {
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  if (!idleReturnEnabled()) return;   // this device opted out — never yank it home
  idleTimer = setTimeout(closeAllOverlays, idleReturnMs(openView));
}

/* --------------------------------------------------------------- polling */

let hubData = null;
let loadedBuild = null;   // /api/hub build token at page load; a change => deploy => reload
let lastInteraction = 0;  // ms of the last user touch/keypress (see noteInteraction)
const INTERACTION_QUIET_MS = 4000;
// Records a user touch/keypress so the deploy auto-reload defers for a few
// seconds afterwards. wallBusy()'s modal checks don't cover the bare wall:
// chore rows are tappable on the main face with NO overlay open, so without
// this a deploy could reload the page out from under a tap. The optional `ts`
// exists only so tests can place the last interaction at a chosen instant;
// production always calls it with no argument.
function noteInteraction(ts) { lastInteraction = (ts === undefined ? Date.now() : ts); }
let data_date = '';
let panelsWired = false;

/* Size each panel so its WHOLE page fits — no cropping, panel no taller than
   its content. Each iframe renders at a fixed virtual viewport (data-vw/vh)
   and is scaled down to the slot via panelFit (common.js, unit-tested). */
const PANEL_MAX_H = 430;

function fitPanel(frame) {
  const view = frame.parentElement;
  view.style.width = '';                 // measure the slot, not a prior fit
  const w = view.clientWidth;
  if (!w) return;   // panels are display:none on phones
  // vw/vh = the VISIBLE region; data-page-w keeps the embedded page laid out
  // at its full design width while data-crop/-left pan the view to a card.
  const vw = Number(frame.dataset.vw);
  const vh = Number(frame.dataset.vh);
  const pageW = Number(frame.dataset.pageW || frame.dataset.vw);
  const cropT = Number(frame.dataset.crop || 0);
  const cropL = Number(frame.dataset.cropLeft || 0);
  const f = panelFit(w, vw, vh, PANEL_MAX_H);
  frame.style.width = `${pageW}px`;
  frame.style.height = `${vh + cropT}px`;
  frame.style.transform = `scale(${f.scale}) translate(-${cropL}px, -${cropT}px)`;
  // hug the scaled content exactly (centered in the card) — no letterbox bars
  view.style.width = `${f.width}px`;
  view.style.height = `${f.height}px`;
  view.style.margin = '0 auto';
}

function fitPanels() {
  document.querySelectorAll('.panel-frame').forEach(fitPanel);
}

/* Build the always-on panels from the config-driven list, then point each at
   its live dashboard — once. The iframes keep their own live state;
   re-setting src every poll would flash-reload them. */
let panelsBuilt = false;

function panelHtml(p) {
  return `<div class="card panel">`
    + sectionHead(p.label, { overlay: `panel:${p.id}`, expandLabel: 'Full screen' })
    + `<div class="panel-view"><iframe class="panel-frame" id="frame-${escapeHtml(p.id)}"`
    + ` title="${escapeHtml(p.label)}" data-vw="${Number(p.vw)}" data-vh="${Number(p.vh)}"`
    + ` data-page-w="${Number(p.page_w || p.vw)}" data-crop="${Number(p.crop_top || 0)}"`
    + ` data-crop-left="${Number(p.crop_left || 0)}"></iframe></div>`
    + `</div>`;
}

/* The 'weather' panel is no longer an always-on iframe embed — it renders as a
   native summary card (weatherSlotHtml + renderWeather). The 'weather' entry
   STAYS in links.panels so panel:weather still resolves the full almanac URL for
   the "Full forecast" overlay (openOverlay). Non-weather panels keep the embed. */
function weatherSlotHtml() {
  return `<div class="weather-slot" id="weather-slot"></div>`;
}

/* The 'climate' panel mirrors 'weather': no always-on iframe embed — it renders
   as a native house-climate summary card (climateSlotHtml + renderClimate). The
   'climate' entry STAYS in links.panels so panel:climate still resolves the full
   house-climate dashboard URL for the "Full climate" overlay (openOverlay). */
function climateSlotHtml() {
  return `<div class="rooms-slot" id="climate-slot"></div>`;
}

function buildPanels() {
  if (panelsBuilt || !links.panels) return;
  document.getElementById('panels').innerHTML =
    (links.panels || []).map((p) => {
      if (p.id === 'weather') return weatherSlotHtml();
      if (p.id === 'climate') return climateSlotHtml();
      return panelHtml(p);
    }).join('');
  panelsBuilt = true;
  renderWeather();   // fill the just-built weather slot from cached data (if any)
  renderClimate();   // fill the just-built climate slot from cached data (if any)
}

function wirePanels() {
  if (panelsWired) return;
  buildPanels();
  // The weather + climate panels are native cards (no iframe); wire only embeds.
  const ps = (links.panels || []).filter((p) => p.id !== 'weather' && p.id !== 'climate');
  if (!ps.length) { panelsWired = true; return; }
  const first = document.getElementById(`frame-${ps[0].id}`);
  if (!first) return;
  if (first.offsetParent === null) return;  // mobile: wire on Weather tab entry
  fitPanels();          // size BEFORE loading so the pages lay out to fit
  ps.forEach((p) => {
    const f = document.getElementById(`frame-${p.id}`);
    if (f) f.src = p.url;
  });
  panelsWired = true;
}

/* ------------------------------------------------- native weather card */

/* A weather emoji for the .sun slot, derived from the conditions text (the wx
   feed carries no icon field). Falls back to partly-cloudy for anything we don't
   recognize. */
function wxIcon(conditions) {
  const c = String(conditions || '').toLowerCase();
  if (/thunder|storm|lightning/.test(c)) return '⛈️';
  if (/snow|flurr|sleet|\bice\b/.test(c)) return '❄️';
  if (/rain|drizzle|shower/.test(c)) return '🌧️';
  if (/fog|mist|haze|smoke/.test(c)) return '🌫️';
  if (/partly|mostly sunny|few cloud|scattered/.test(c)) return '⛅';
  if (/cloud|overcast/.test(c)) return '☁️';
  if (/clear|sun|fair/.test(c)) return '☀️';
  return '⛅';
}

/* Split a temperature into the big whole-number part and the ".<frac>°<unit>"
   tail, matching the mockup (74 | .8°F). Carries a .95→x.0 rounding case. */
function wxTempParts(temp, unit) {
  // The feed's unit may already carry the degree sign ("°F"); strip a leading
  // one so we always render exactly one ° and never "°°F".
  const u = unit ? escapeHtml(String(unit).replace(/^\s*°/, '')) : '';
  const t = Number(temp);
  if (!isFinite(t)) return { whole: '--', deg: `°${u}` };
  let whole = Math.trunc(t);
  let dec = Math.round(Math.abs(t - whole) * 10);
  if (dec === 10) { whole += (t < 0 ? -1 : 1); dec = 0; }
  return { whole: String(whole), deg: `.${dec}°${u}` };
}

/* A stat value cell: the value + suffix, or an en-dash when the field is null. */
function wxVal(v, suffix) {
  return (v == null || String(v) === '') ? '–' : `${escapeHtml(String(v))}${suffix}`;
}

function wxStat(k, vHtml) {
  return `<div class="stat"><div class="k">${k}</div><div class="v num">${vHtml}</div></div>`;
}

/* A weather stat with a severity meter — the UV and Air-Quality metrics. The
   value is tinted to its `band` (the standard EPA/AQI scale from the NUMBER when
   present, else a category-text fallback — see the caller), the category shows
   as a small label, and a proportional bar fills to `pct` (0..100) in the band's
   color. `band` is '' | 'good' | 'ok' | 'warn' | 'crit'; only good/warn/crit
   tint (an 'ok'/absent reading stays neutral ink over a grey bar). */
function wxMeterStat(k, value, suffix, band, label, pct) {
  const st = (band === 'good' || band === 'warn' || band === 'crit') ? ` st-${band}` : '';
  // The category is normally a quiet grey sub-label under the colored number.
  // But when the number is MISSING, the label is the only signal, so it inherits
  // the band tint too — a feed that omits the number but says "Unhealthy" must
  // never read as calm grey (see uvBand/aqiBand text fallback in common.js).
  const missing = value == null || String(value).trim() === '' || !isFinite(Number(value));
  const lblCls = missing ? `wx-band${st}` : 'wx-band';
  const lbl = (label != null && String(label).trim() !== '')
    ? `<span class="${lblCls}">${escapeHtml(String(label))}</span>` : '';
  return `<div class="stat wx-meter"><div class="k">${k}</div>`
    + `<div class="v num${st}">${wxVal(value, suffix)}${lbl}</div>`
    + `<div class="bar" aria-hidden="true"><i class="bar-fill${st}" style="width:${pct}%"></i></div>`
    + `</div>`;
}

/* Temperature curve for the weather card: a ~24h window (observed past +
   forecast ahead) normalized into the 0 0 300 46 viewBox (padded top/bottom so
   the line clears the edges). `nowIndex` marks the current hour: the line is
   solid for the observed past and lighter for the forecast ahead, with a faint
   vertical guide and a dot at "now". Absent/out-of-range nowIndex -> all solid,
   dot on the last point. Returns '' (hide it) when fewer than 2 numeric points
   survive the filter. */
function sparkSvg(spark, nowIndex) {
  const vals = (Array.isArray(spark) ? spark : [])
    .filter((v) => typeof v === 'number' && isFinite(v));
  if (vals.length < 2) return '';
  const W = 300, H = 46, PT = 8, PB = 8, usable = H - PT - PB;
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const n = vals.length;
  const rnd = (x) => Math.round(x * 10) / 10;
  const pts = vals.map((v, i) => ({
    x: rnd((i / (n - 1)) * W),
    y: rnd(PT + (1 - (v - min) / range) * usable),
  }));
  const path = (seg) => seg.map((p, i) => `${i ? 'L' : 'M'}${p.x},${p.y}`).join(' ');
  const ni = (Number.isInteger(nowIndex) && nowIndex >= 0 && nowIndex < n) ? nowIndex : n - 1;
  const now = pts[ni];
  const area = `${path(pts)} L${pts[n - 1].x},${H} L${pts[0].x},${H} Z`;
  const past = path(pts.slice(0, ni + 1));      // observed
  const future = ni < n - 1 ? path(pts.slice(ni)) : '';   // forecast (overlaps at now)
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + `<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0" stop-color="var(--accent)" stop-opacity=".26"/>`
    + `<stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>`
    + `<path d="${area}" fill="url(#sg)"/>`
    + (future ? `<path d="${future}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-opacity=".38"/>` : '')
    + `<path d="${past}" fill="none" stroke="var(--accent)" stroke-width="2"/>`
    + `<line x1="${now.x}" y1="0" x2="${now.x}" y2="${H}" stroke="var(--accent)" stroke-opacity=".2" stroke-width="1"/>`
    + `<circle cx="${now.x}" cy="${now.y}" r="2.6" fill="var(--accent)"/>`
    + `</svg>`;
}

function weatherCardHtml(wx) {
  const tp = wxTempParts(wx.temp, wx.unit);
  const condText = wx.conditions != null ? String(wx.conditions) : '';
  const feelsPart = (wx.feels != null && String(wx.feels) !== '')
    ? `${condText ? ' · ' : ''}feels ${escapeHtml(String(wx.feels))}°` : '';
  const stalePart = wx.stale ? ` · <span class="wx-stale">stale</span>` : '';
  const cond = `${escapeHtml(condText)}${feelsPart}${stalePart}`;

  // UV + AQI get a colored severity meter (wxMeterStat). Color follows the
  // NUMBER (uvBand/aqiBand); the feed's category text is only the label. Meter
  // fill: UV against a full-scale 11 (EPA extreme), AQI against 200 (its
  // "very unhealthy" line), both clamped so an off-scale reading can't overflow.
  const uvB = uvBand(wx.uv) || uvBandText(wx.uv_desc);
  const aqiB = aqiBand(wx.aqi) || aqiBandText(wx.aqi_cat);
  const stats = wxStat('High', wxVal(wx.high, '°'))
    + wxStat('Low', wxVal(wx.low, '°'))
    + wxMeterStat('UV Index', wx.uv, '', uvB, wx.uv_desc, Math.round(clampFrac(wx.uv, 11) * 100))
    + wxMeterStat('Air Quality', wx.aqi, '', aqiB, wx.aqi_cat, Math.round(clampFrac(wx.aqi, 200) * 100))
    + wxStat('Humidity', wxVal(wx.humidity, '%'))
    + wxStat('Dew point', wxVal(wx.dew_point, '°'));

  return `<article class="card wx">`
    + `<div class="top"><div>`
    + `<div class="tline"><span class="temp num">${tp.whole}</span>`
    + `<span class="deg num">${tp.deg}</span></div>`
    + `<div class="cond">${cond}</div>`
    + `</div><span class="sun">${wxIcon(wx.conditions)}</span></div>`
    + sparkSvg(wx.spark, wx.spark_now)
    + `<div class="stats">${stats}</div>`
    + `</article>`;
}

/* Paint the native weather card into its slot (built by buildPanels). The header
   sits OUTSIDE the card (sectionHead), like every other section; the "Full
   forecast" button opens the rich almanac full-screen via panel:weather. When
   the feed is unavailable (offline / proxy off / fetch error) the column is
   never blanked — a slim offline note stands in for the card, header intact so
   the almanac stays one tap away. */
function renderWeather(wx = weatherData) {
  const host = document.getElementById('weather-slot');
  if (!host) {
    // No slot yet. If a 'weather' panel IS configured, buildPanels will create the
    // slot and a later render fills it — this is just the boot race (the feed
    // fetch resolving before /api/hub), so stay silent. Only warn when the config
    // (links.panels, once loaded) genuinely has no 'weather' panel, so the card
    // has nowhere to render at all. Warn ONCE, not every 60s poll.
    const panelsLoaded = Array.isArray(links && links.panels);
    const configured = panelsLoaded && links.panels.some((p) => p && p.id === 'weather');
    if (wx && wx.available && panelsLoaded && !configured && !warnedNoWeatherSlot) {
      warnedNoWeatherSlot = true;
      console.warn("weather_base is set but no 'weather' panel is configured; "
        + "the weather card has nowhere to render.");
    }
    return;
  }
  const head = sectionHead('Weather', { overlay: 'panel:weather', expandLabel: 'Full forecast' });
  // null = not fetched yet (boot): show a neutral placeholder, never the offline
  // note, so the card doesn't flash "unavailable" before the first fetch (T7).
  const body = wx == null
    ? `<div class="card wx-loading" aria-hidden="true"></div>`
    : wx.available
      ? weatherCardHtml(wx)
      : `<div class="wx-offline">Weather unavailable</div>`;
  host.innerHTML = head + body;
}

/* Poll the fail-soft weather endpoint on the hub cadence; any error is treated
   as unavailable (never throws). */
async function fetchWeather() {
  try {
    weatherData = await j('/api/tiles/weather');
    weatherFails = 0;
  } catch (e) {
    // Keep the last good card on a single flaky poll instead of blinking to
    // "unavailable" for 60s; only give up after several failures in a row (or
    // if we never had data). poll()'s offline badge already flags the outage.
    weatherFails += 1;
    if (!weatherData || weatherFails >= TILE_FAIL_LIMIT) weatherData = { available: false };
  }
  renderWeather();
}

/* ------------------------------------------------- native climate card */

/* The outdoor-air sensor is EXCLUDED from the house-climate card — its
   temperature already shows in the Weather section above, so listing it here is
   redundant. The backend /api/tiles/climate is a FAITHFUL passthrough (it
   includes every sensor); this frontend predicate is the sole filter.
   Predicate: a room is OUTDOOR when its NAME (case-insensitive, trimmed) is
   "outside" or "outdoor". Channel is NOT used: house-climate labels legitimate
   indoor rooms (e.g. a crawl space) with an "outdoor" channel, so filtering on
   channel would wrongly hide them. Everything not named outside/outdoor shows. */
const OUTDOOR_KEYS = new Set(['outside', 'outdoor']);
function isIndoorRoom(room) {
  if (!room || typeof room !== 'object') return false;
  const name = String(room.name == null ? '' : room.name).trim().toLowerCase();
  return !OUTDOOR_KEYS.has(name);
}

/* The room's overall comfort band: the worst of its temperature band, its
   humidity band, and a stale sensor (which warns on its own, like the old
   grid). Comfortable readings rank 'good' (a calm green dot); no readings at
   all rank '' (a neutral dot). Drives the status dot + the optional row class. */
function roomBand(room) {
  const rank = { '': 0, ok: 1, good: 1, warn: 2, crit: 3 };
  let worst = 0;
  const bump = (b) => { if (rank[b] > worst) worst = rank[b]; };
  bump(tempBandF(room.temp_f));
  bump(humidityBand(room.humidity));
  if (room && room.stale) bump('warn');
  return ['', 'good', 'warn', 'crit'][worst];
}

/* One room row: a comfort status dot · NAME · temp_f° · humidity%. Missing/
   non-finite temp -> "--", missing humidity -> "—". Out-of-range temp/humidity
   cells are tinted to their band (warn amber / crit red); comfortable cells stay
   neutral. Values are rounded to whole units; the name is escaped. */
function roomRowHtml(room) {
  const t = Number(room.temp_f);
  const tempOk = room.temp_f != null && String(room.temp_f) !== '' && isFinite(t);
  const tempCell = tempOk ? `${Math.round(t)}°` : '--';
  const h = Number(room.humidity);
  const humOk = room.humidity != null && String(room.humidity) !== '' && isFinite(h);
  const humCell = humOk ? `${Math.round(h)}%` : '—';
  const name = escapeHtml(String(room.name == null ? '' : room.name));
  const tb = tempBandF(room.temp_f);
  const hb = humidityBand(room.humidity);
  const cell = (b) => ((b === 'warn' || b === 'crit') ? ` st-${b}` : '');
  const band = roomBand(room);
  const rowCls = (band === 'warn' || band === 'crit') ? ` ${band}` : '';
  const dotCls = band ? ` st-${band}` : '';
  return `<div class="room${rowCls}">`
    + `<span class="dot${dotCls}"></span>`
    + `<span class="rk">${name}</span>`
    + `<span class="rv num${cell(tb)}">${tempCell}</span>`
    + `<span class="rh num${cell(hb)}">${humCell}</span>`
    + `</div>`;
}

function climateCardHtml(cl) {
  const rooms = (Array.isArray(cl.rooms) ? cl.rooms : []).filter(isIndoorRoom);
  const rows = rooms.length
    ? rooms.map(roomRowHtml).join('')
    : `<div class="cal-empty">no indoor sensors reporting</div>`;
  // Whole-house indoor aggregate (from /api/humidity) as a labeled footer — the
  // per-room grid has no Dew column, so show RH + dew explicitly here. Fail-soft:
  // only the values actually present render.
  const rh = cl.indoor_rh, dp = cl.indoor_dp;
  const parts = [];
  if (Number.isFinite(rh)) parts.push(`<b class="num">${Math.round(rh)}%</b> RH`);
  if (Number.isFinite(dp)) parts.push(`<b class="num">${Math.round(dp)}°</b> dew`);
  const foot = parts.length
    ? `<div class="rfoot"><span class="rk">Indoor</span>`
      + `<span class="rfoot-v">${parts.join(' · ')}</span></div>` : '';
  return `<article class="card rooms">`
    + `<div class="rhead"><span class="rk"></span>`
    + `<span class="u">Temp</span><span class="u">Humidity</span></div>`
    + rows
    + foot
    + `</article>`;
}

/* Paint the native climate card into its slot (built by buildPanels). The header
   sits OUTSIDE the card (sectionHead), like every other section; the "Full
   climate" button opens the rich house-climate dashboard full-screen via
   panel:climate. When the feed is unavailable (proxy off / fetch error) the
   column is never blanked — a slim offline note stands in for the card, header
   intact so the full dashboard stays one tap away. */
function renderClimate(cl = climateData) {
  const host = document.getElementById('climate-slot');
  if (!host) {
    // No slot yet. If a 'climate' panel IS configured, buildPanels will create the
    // slot and a later render fills it — this is just the boot race (the feed
    // fetch resolving before /api/hub), so stay silent. Only warn when the config
    // (links.panels, once loaded) genuinely has no 'climate' panel, so the card
    // has nowhere to render at all. Warn ONCE, not every 60s poll.
    const panelsLoaded = Array.isArray(links && links.panels);
    const configured = panelsLoaded && links.panels.some((p) => p && p.id === 'climate');
    if (cl && cl.available && panelsLoaded && !configured && !warnedNoClimateSlot) {
      warnedNoClimateSlot = true;
      console.warn("climate_base is set but no 'climate' panel is configured; "
        + "the climate card has nowhere to render.");
    }
    return;
  }
  const head = sectionHead('House Climate', { overlay: 'panel:climate', expandLabel: 'Full climate' });
  // null = not fetched yet (boot): neutral placeholder, never the offline note (T7).
  const body = cl == null
    ? `<div class="card wx-loading" aria-hidden="true"></div>`
    : cl.available
      ? climateCardHtml(cl)
      : `<div class="wx-offline">Climate unavailable</div>`;
  host.innerHTML = head + body;
}

/* Poll the fail-soft climate endpoint on the hub cadence; any error is treated
   as unavailable (never throws). */
async function fetchClimate() {
  try {
    climateData = await j('/api/tiles/climate');
    climateFails = 0;
  } catch (e) {
    // Same as fetchWeather: hold the last good reading through a transient blip
    // rather than flashing "unavailable"; fall back only after repeated misses.
    climateFails += 1;
    if (!climateData || climateFails >= TILE_FAIL_LIMIT) climateData = { available: false };
  }
  renderClimate();
}

let fitDebounce = null;
/* Fit-to-screen. The wall is authored at a fixed 1920x1080 canvas. On the
   target Pi kiosk that IS the viewport, so nothing scales (1:1). On any other
   screen — a laptop, a differently-sized monitor — scale the whole wall down to
   fit, so the right-hand columns are never clipped and there is never a
   horizontal scrollbar. Only ever scale DOWN (big screens stay 1:1, centered,
   rather than upscaling to blur). The mobile reflow (<=1000px) owns its own
   single-column layout, so leave it untouched there. */
function fitWall() {
  const wrap = document.querySelector('.wrap');
  if (!wrap) { console.warn('fitWall: .wrap not found; wall will not fit-scale'); return; }
  wrap.style.zoom = wallZoom(window.innerWidth);
}
fitWall();

/* Phone-shell height. The shell body is `height: var(--app-h, 100dvh)`; we drive
   --app-h from window.innerHeight because iOS Safari leaves a STALE 100dvh after
   a bfcache / app-switch restore (returning to an already-open tab): the in-flow
   tab bar then floats above a black gap until a full reload (operator report,
   2026-08-17). Re-measured on the lifecycle events iOS doesn't reliably relayout
   for — pageshow (incl. bfcache `persisted`), visibilitychange back to visible,
   resize, orientationchange. Only the mobile-mode body consumes the var, so
   setting it on the wall/desktop is a harmless no-op. */
function syncAppHeight() {
  // iOS Safari can transiently report innerHeight:0 on these very lifecycle
  // events; a literal --app-h:0px would collapse the shell to a black screen
  // (0px is a "valid" value, so the 100dvh fallback would NOT save it). Ignore a
  // non-positive reading and leave the last good height standing.
  const h = window.innerHeight;
  if (h > 0) document.documentElement.style.setProperty('--app-h', `${h}px`);
}
syncAppHeight();
window.addEventListener('pageshow', syncAppHeight);
window.addEventListener('orientationchange', syncAppHeight);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') syncAppHeight();
});

window.addEventListener('resize', () => {
  syncAppHeight();
  fitWall();
  clearTimeout(fitDebounce);
  // wirePanels too: growing past the mobile breakpoint reveals panels that
  // were hidden (and so unwired) at load — don't leave them dark until poll
  fitDebounce = setTimeout(() => { wirePanels(); fitPanels(); }, 150);
});

/* Watchdog: the embedded dashboards poll their own data continuously, but if
   a page's JS ever wedges the panel would sit stale forever. A staggered
   half-hourly hard reload self-heals both panels. */
const PANEL_RELOAD_MS = 30 * 60 * 1000;
let panelReloadCount = 0;
function reloadPanel(id, base) {
  const f = document.getElementById(id);
  // skip hidden frames (mobile, off the Weather tab) — no background streams
  if (f && base && f.offsetParent !== null) {
    f.src = `${base}${base.includes('?') ? '&' : '?'}r=${panelReloadCount}`;
  }
}
setInterval(() => {
  panelReloadCount += 1;
  (links.panels || []).forEach((p, i) =>   // staggered a minute apart
    setTimeout(() => reloadPanel(`frame-${p.id}`, p.url), i * 60000));
}, PANEL_RELOAD_MS);

/* True while the wall is showing something the user is mid-interaction with, so
   the auto-reload defers instead of yanking it away. */
function wallBusy() {
  const hasClass = (id, cls) => {
    const el = document.getElementById(id);
    return !!(el && el.classList.contains(cls));
  };
  const shown = (id) => {
    const el = document.getElementById(id);
    return !!(el && !el.classList.contains('hidden'));
  };
  return hasClass('overlay', 'open') || hasClass('theme-pop', 'open')
    || Object.keys(MODAL_CLOSERS).some(shown)   // same modal set closeAllOverlays closes
    // a direct tap on the bare wall (e.g. a chore toggle) opens no overlay, so
    // defer the reload for a short quiet window after any recent interaction
    || (Date.now() - lastInteraction < INTERACTION_QUIET_MS);
}

async function poll() {
  try {
    const data = await j('/api/hub');
    hubData = data;
    // Auto-reload when a deploy changes the baked frontend (the server's build
    // token changes), so the kiosk picks up updates without a manual refresh —
    // but never mid-interaction (defer to a later poll once the wall is idle).
    if (data.build) {
      if (loadedBuild === null) loadedBuild = data.build;
      else if (data.build !== loadedBuild && !wallBusy()) { location.reload(); return; }
    }
    data_date = data.date;
    links = data.links || {};
    applyHouseTheme(data.theme);   // house default on a fresh (un-overridden) device
    wirePanels();
    initTiles();      // camera tiles are config-driven; build once links exist
    initCamGrid();    // Cameras-tab 2x2 grid, also config-driven
    renderCalendar(data);
    renderPeople(data);
    renderTodoSlot(data);
    renderIntegrations(data);
    pruneEvIndex();
    document.body.dataset.conn = 'up';
    document.getElementById('conn-word').textContent = 'live';
  } catch (e) {
    document.body.dataset.conn = 'down';
    document.getElementById('conn-word').textContent = 'offline';
  }
}

// The 60s interval must not STACK polls: with the fetch timeout above, a poll to
// a wedged server stays in flight up to J_TIMEOUT_MS, and an unguarded interval
// would keep firing new requests every 60s until the browser's ~6-connection
// budget fills with dead sockets. Skip a scheduled tick while one is still
// running. (Direct poll() calls — a chore/todo write's refresh — are user-paced
// and intentionally always run, so the guard lives here, not inside poll().)
let scheduledPollInFlight = false;
function scheduledPoll() {
  if (scheduledPollInFlight) return;
  scheduledPollInFlight = true;
  poll().finally(() => { scheduledPollInFlight = false; });
}

// Same guard, same reason, for the camera probe interval: probeOneCamera's
// fetches now carry a J_TIMEOUT_MS bound (see fetchTimeout in common.js), but
// without this an unguarded CAM_PROBE_MS interval would still keep firing a
// fresh probeCamera() every 30s on top of one still waiting out that timeout,
// stacking requests toward the browser's connection budget. Direct
// probeCamera() calls (tab switch, overlay open) are user-paced and
// intentionally always run, so the guard lives here, not inside probeCamera().
let scheduledProbeCameraInFlight = false;
function scheduledProbeCamera() {
  if (scheduledProbeCameraInFlight) return;
  scheduledProbeCameraInFlight = true;
  probeCamera().finally(() => { scheduledProbeCameraInFlight = false; });
}

let _toastTimer = null;
function showToast(msg) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'hub-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;   // textContent, not innerHTML: never interpolates markup
  el.classList.add('hub-toast-visible');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('hub-toast-visible'), 4000);
}

async function toggleChore(id, done) {
  // attemptToggle (common.js) returns false if the write failed. Surface it
  // with a toast instead of swallowing: under a PERSISTENT write failure (full
  // disk / read-only SD card on a kiosk) the poll() below re-renders the chore
  // as undone, so a silent catch makes the tap look like it did nothing.
  const ok = await attemptToggle(id, done);
  if (!ok) showToast('Couldn’t save — check the hub and tap again.');
  await poll();
  // keep the full-screen chores view in step when it's open on today
  if (openView === 'chores') renderChoresFull(hubData ? hubData.people : null);
}

/* ----------------------------------------------- chore editor (add / edit) */

/* The shared chore form (common.js buildChoreForm), opened from the all-chores
   overlay's edit mode. It needs the FLAT people list (with active flags) and,
   to edit, the FULL chore record — neither of which the wall's /api/hub payload
   carries (its chore rows are just {id,title,icon,done,rot}; its people are
   nested under .person). So the editor pulls /api/admin/state, the flat+full
   source that feeds buildChoreForm/choreToModel. */

function closeChoreEditor() {
  document.getElementById('chore-modal').classList.add('hidden');
  document.getElementById('chore-editor').innerHTML = '';   // drop the old form
}

/* After a successful create/patch: close the editor, re-fetch (poll refreshes
   hubData), and repaint the chores view — staying in edit mode so the user can
   keep going. */
async function refreshChoresAfterEdit() {
  closeChoreEditor();
  await poll();
  if (openView === 'chores') renderChoresFull(hubData ? hubData.people : null);
}

/* POST/PATCH the chore via the shared j() helper. On failure the editor stays
   OPEN (input isn't lost) and the reason surfaces as a toast — same
   detect-don't-swallow contract as toggleChore. */
async function submitChore(url, method, body) {
  try {
    await j(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    showToast(e.message || 'Couldn’t save the chore — check the hub and try again.');
    return;
  }
  await refreshChoresAfterEdit();
}

async function openChoreEditor(seed) {
  // data-add-chore/data-edit-chore only render inside the chores overlay's
  // edit mode, so openView is always truthy ('chores') here. Remember it: if
  // the idle timer or a home tap runs closeAllOverlays() while this fetch is
  // in flight, openView goes back to null, and this must NOT reopen a modal
  // over a wall the user (or the idle return) already left: same stale-async
  // guard as ensurePeopleThenRerender below.
  const view = openView;
  let state;
  try {
    state = await j('/api/admin/state');   // {people (flat, active flags), chores (full records)}
  } catch (e) {
    showToast('Couldn’t open the editor — check the hub and try again.');
    return;
  }
  if (openView !== view) return;   // overlay closed/switched while this was in flight
  const host = document.getElementById('chore-editor');
  let model;
  let label;
  let onsubmit;
  if (seed.mode === 'edit') {
    const ch = state.chores.find((x) => x.id === seed.choreId);
    if (!ch) { showToast('That chore is no longer here — refreshing.'); refreshChoresAfterEdit(); return; }
    model = choreToModel(ch);
    label = 'Save';
    onsubmit = (body) => submitChore(`/api/admin/chores/${ch.id}`, 'PATCH', body);
  } else {
    // add: seed the person picker to the card's person (preselected)
    model = { ...freshChoreModel(), fixed_person_id: seed.personId };
    label = 'Add chore';
    onsubmit = (body) => submitChore('/api/admin/chores', 'POST', body);
  }
  buildChoreForm(host, model, label, onsubmit, state.people);
  document.getElementById('chore-modal').classList.remove('hidden');
  if (openView) armIdle();   // keep the overlay alive while the editor is up
}

/* ------------------------------------------- people editor (add / edit) */

/* After a people mutation: drop the cached admin people so the next render
   re-fetches, poll() to refresh the wall's person cards (a rename/recolor/
   activate shows there too), and repaint the chores view staying in edit. */
async function refreshPeopleAdmin() {
  choreAdminPeople = null;
  await poll();
  if (openView === 'chores') renderChoresFull(hubData ? hubData.people : null);
}

/* POST/PATCH a person via the shared person form. On failure the editor stays
   OPEN and the reason renders inline (same contract as the chore editor). */
async function submitPerson(url, method, body, errEl) {
  try {
    await j(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    if (errEl) { errEl.textContent = e.message; errEl.classList.remove('hidden'); }
    return;
  }
  closeChoreEditor();          // reuses the chore-modal shell
  await refreshPeopleAdmin();
}

/* Open the shared person form in the chore-modal shell. Edit reads the cached
   admin people (loaded when edit mode opened); add starts from a fresh model. */
function openPersonEditor(seed) {
  const host = document.getElementById('chore-editor');
  let model;
  let label;
  let onsubmit;
  if (seed.mode === 'edit') {
    const p = (choreAdminPeople || []).find((x) => x.id === seed.personId);
    if (!p) { showToast('That person is no longer here — refreshing.'); refreshPeopleAdmin(); return; }
    model = { name: p.name, color: p.color };
    label = 'Save';
    onsubmit = (body, errEl) => submitPerson(`/api/admin/people/${p.id}`, 'PATCH', body, errEl);
  } else {
    model = freshPersonModel();
    label = 'Add person';
    onsubmit = (body, errEl) => submitPerson('/api/admin/people', 'POST', body, errEl);
  }
  buildPersonForm(host, model, label, onsubmit);
  document.getElementById('chore-modal').classList.remove('hidden');
  if (openView) armIdle();
}

/* Deactivate / reactivate a person (the reversible alternative to delete). */
async function togglePersonActive(pid) {
  const p = (choreAdminPeople || []).find((x) => x.id === pid);
  if (!p) {
    // the cache was dropped between render and tap (a concurrent refresh) —
    // give feedback and refresh rather than swallowing the tap
    showToast('That person is no longer here — refreshing.');
    refreshPeopleAdmin();
    return;
  }
  try {
    await j(`/api/admin/people/${pid}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: p.active ? 0 : 1 }),
    });
  } catch (e) {
    showToast(e.message || 'Couldn’t update — check the hub and try again.');
    return;
  }
  await refreshPeopleAdmin();
}

/* ------------------------------------------------ delete (custom confirm) */

/* A custom confirm — NOT window.confirm, which kiosks block and which looks
   wrong on the wall. It layers above the chores overlay (like the editor) and
   names the target. Cancel dismisses with no write; Delete fires the DELETE.
   Shared by chore delete and person hard-delete: `pendingDelete.kind` picks
   which endpoint + refresh confirmDelete runs. */
let pendingDelete = null;   // { kind: 'chore' | 'person', id }

/* The wall's /api/hub payload carries each chore's title (nested under a
   person), so the confirm can name the chore without another fetch. */
function choreTitleById(cid) {
  for (const p of (hubData ? hubData.people : [])) {
    const ch = (p.chores || []).find((c) => Number(c.id) === Number(cid));
    if (ch) return ch.title;
  }
  return '';
}

function openDeleteConfirm(cid) {
  pendingDelete = { kind: 'chore', id: cid };
  const title = choreTitleById(cid);
  document.getElementById('confirm-msg').textContent =
    title ? `Delete “${title}”?` : 'Delete this chore?';
  document.getElementById('confirm-sub').textContent =
    'It stays on past days; it’s removed from today on.';
  document.getElementById('confirm-modal').classList.remove('hidden');
  if (openView) armIdle();
}

/* Person hard-delete confirm — a distinct, blunter warning than chore delete:
   the person is removed for good (deactivate is the reversible option). */
function openPersonDeleteConfirm(pid) {
  const p = (choreAdminPeople || []).find((x) => x.id === pid);
  pendingDelete = { kind: 'person', id: pid };
  document.getElementById('confirm-msg').textContent =
    p ? `Delete ${p.name}?` : 'Delete this person?';
  document.getElementById('confirm-sub').textContent =
    'Removed for good. Past days keep their record. To pause instead, use Deactivate.';
  document.getElementById('confirm-modal').classList.remove('hidden');
  if (openView) armIdle();
}

function closeDeleteConfirm() {
  pendingDelete = null;
  document.getElementById('confirm-modal').classList.add('hidden');
}

/* Confirmed: DELETE the chore or person, then refresh staying in edit mode. On
   failure the reason surfaces as a toast and nothing is removed —
   detect-don't-swallow, like submitChore/toggleChore. */
async function confirmDelete() {
  const target = pendingDelete;
  if (!target) return;
  closeDeleteConfirm();
  const url = target.kind === 'person'
    ? `/api/admin/people/${target.id}` : `/api/admin/chores/${target.id}`;
  try {
    await j(url, { method: 'DELETE' });
  } catch (e) {
    showToast(e.message || 'Couldn’t delete — check the hub and try again.');
    return;
  }
  if (target.kind === 'person') await refreshPeopleAdmin();
  else await refreshChoresAfterEdit();
}

/* --------------------------------------------------------------- wiring */

document.addEventListener('click', (e) => {
  const tabBtn = e.target.closest('.tab-btn');
  if (tabBtn) { setTab(tabBtn.dataset.tab); return; }
  // delete confirm (above everything): Cancel or a backdrop tap dismisses it
  // with NO write; Delete fires the DELETE. Checked before the editor so its
  // own backdrop logic can't run against this modal.
  if (e.target.closest('[data-confirm-cancel]')
      || (e.target.closest('.confirm-modal') && !e.target.closest('.confirm-card'))) {
    closeDeleteConfirm(); return;
  }
  if (e.target.closest('[data-confirm-del]')) { confirmDelete(); return; }
  // chore editor (above the overlay): ✕ or a backdrop tap dismisses it
  if (e.target.closest('.chore-close')
      || (e.target.closest('.chore-modal') && !e.target.closest('.chore-card'))) {
    closeChoreEditor(); return;
  }
  // event detail card first: close controls, then any tapped event row/chip
  if (e.target.closest('.ev-close')
      || (e.target.closest('.ev-modal') && !e.target.closest('.ev-card'))) {
    closeEventDetail(); return;
  }
  const evRow = e.target.closest('[data-eid]');
  if (evRow) { openEventDetail(evRow.dataset.eid); return; }
  // full-calendar controls
  const nav = e.target.closest('[data-calnav]');
  if (nav) { calNav(nav.dataset.calnav); return; }
  const viewBtn = e.target.closest('[data-calview]');
  if (viewBtn) { calState.mode = viewBtn.dataset.calview; renderCalFull(); return; }
  if (e.target.closest('[data-calback]')) { calState.mode = 'month'; renderCalFull(); return; }
  const mgDay = e.target.closest('.mg-day');
  if (mgDay) {
    calState.mode = 'day'; calState.day = mgDay.dataset.date;
    renderCalFull(); return;
  }
  // chores edit toggle (today only) — flips edit mode, then repaints today
  const chedit = e.target.closest('[data-chedit]');
  if (chedit) {
    choreState.editing = !choreState.editing;
    renderChoresFull(hubData ? hubData.people : null);
    return;
  }
  // chores day browser nav
  const chnav = e.target.closest('[data-chnav]');
  if (chnav) {
    const dir = chnav.dataset.chnav;
    choreState.day = dir === 'today' ? data_date
      : addDays(choreState.day, dir === 'next' ? 1 : -1);
    // Edit mode is today-only; never carry it off today. Clearing it (rather
    // than just suppressing the render) means returning to today lands in
    // check-off mode, not silently back beside the save controls.
    if (choreState.day !== data_date) choreState.editing = false;
    renderChoresFull(choreState.day === data_date && hubData ? hubData.people : null);
    return;
  }
  // edit mode: "+ Add chore" opens the editor seeded to that person; a chore
  // row opens it seeded from that chore. Both carry .chore-row, so these must
  // come BEFORE the check-off branch below.
  const addChore = e.target.closest('[data-add-chore]');
  if (addChore) {
    openChoreEditor({ mode: 'add', personId: Number(addChore.dataset.addChore) });
    return;
  }
  // delete sits INSIDE the data-edit-chore row, so it MUST be checked first —
  // otherwise the same tap would also fall through and open the editor.
  const delChore = e.target.closest('[data-del-chore]');
  if (delChore) {
    openDeleteConfirm(Number(delChore.dataset.delChore));
    return;
  }
  const editChore = e.target.closest('[data-edit-chore]');
  if (editChore) {
    openChoreEditor({ mode: 'edit', choreId: Number(editChore.dataset.editChore) });
    return;
  }
  // inline people admin (edit mode): add / edit / deactivate / hard-delete. The
  // delete confirm dispatches by kind (see confirmDelete).
  if (e.target.closest('[data-padd]')) { openPersonEditor({ mode: 'add' }); return; }
  const pedit = e.target.closest('[data-pedit]');
  if (pedit) { openPersonEditor({ mode: 'edit', personId: Number(pedit.dataset.pedit) }); return; }
  const ptoggle = e.target.closest('[data-ptoggle]');
  if (ptoggle) { togglePersonActive(Number(ptoggle.dataset.ptoggle)); return; }
  const pdel = e.target.closest('[data-pdel]');
  if (pdel) { openPersonDeleteConfirm(Number(pdel.dataset.pdel)); return; }
  // to-dos: action buttons first, then check-off rows, then the add controls
  const tmove = e.target.closest('[data-todo-move]');
  if (tmove) { moveTodo(tmove.dataset.tid, tmove.dataset.todoMove); return; }
  const tdel = e.target.closest('[data-todo-del]');
  if (tdel) { deleteTodo(tdel.dataset.todoDel); return; }
  const trestore = e.target.closest('[data-todo-restore]');
  if (trestore) { toggleTodo(trestore.dataset.todoRestore, true); return; }
  const topen = e.target.closest('[data-todo-open]');
  if (topen) {
    const id = Number(topen.dataset.todoOpen);
    todoState.openId = todoState.openId === id ? null : id;
    renderTodosPaint();
    return;
  }
  const trow = e.target.closest('[data-todo]');
  if (trow) {
    toggleTodo(trow.dataset.todo,
      !!trow.closest('.todo-row.done, .todo-row-full.done'));
    return;
  }
  const tseg = e.target.closest('[data-todo-bucket]');
  if (tseg) { todoState.addBucket = tseg.dataset.todoBucket; renderTodosPaint(); return; }
  // iCloud reminders (two-way): delete + open-actions before the check row, same
  // ordering rule as the to-dos above. Read-only rows carry no data-reminder, so
  // they fall through to nothing — look, don't touch.
  const remdel = e.target.closest('[data-reminder-del]');
  if (remdel) { deleteReminder(remdel.dataset.reminderDel); return; }
  const remopen = e.target.closest('[data-reminder-open]');
  if (remopen) {
    const id = remopen.dataset.reminderOpen;
    todoState.openId = String(todoState.openId) === id ? null : id;
    renderTodosPaint();
    return;
  }
  const remrow = e.target.closest('[data-reminder]');
  if (remrow) {
    // Optimistic: flip the row now so the wall feels instant; the write +
    // refresh below reconciles (a completed reminder then drops off the list).
    const rowEl = remrow.closest('.todo-row-full') || remrow.closest('.todo-row') || remrow;
    const wasDone = rowEl.classList.contains('done');
    rowEl.classList.toggle('done', !wasDone);
    toggleReminder(remrow.dataset.reminder, !wasDone);
    return;
  }
  // home surfaces (readonly rows carry no data-chore — look, don't touch)
  const chore = e.target.closest('.chore-row');
  if (chore && chore.dataset.chore) {
    toggleChore(chore.dataset.chore, chore.classList.contains('done'));
    return;
  }
  if (chore) return;
  const expand = e.target.closest('.expand');
  if (expand) { openOverlay(expand.dataset.overlay); return; }
  const tile = e.target.closest('.tile');
  if (tile) { openOverlay(tile.dataset.overlay); return; }
  const head = e.target.closest('.person-head');
  if (head) { openOverlay('chores'); return; }
  const day = e.target.closest('.cal-day');
  if (day && !openView) { openOverlay('calendar'); return; }
  if (e.target.closest('#overlay-home')) { closeAllOverlays(); }
});
['pointerdown', 'touchstart', 'keydown'].forEach((evt) =>
  document.addEventListener(evt, () => { noteInteraction(); if (openView) armIdle(); }, { passive: true }));
document.addEventListener('submit', (e) => {
  if (e.target && e.target.id === 'todo-add-form') {
    e.preventDefault();
    // One add form id, two backends: dispatch by the source the view is showing.
    if (todoState.source === 'icloud') addReminder(); else addTodo();
  }
});

/* ------------------- persisted display controls (Task 5) ------------------ */
// applyHouseTheme stamps the server's house default on a device that has made
// NO local choice yet. It deliberately does NOT persist (the stamp-only
// appliers from theme.js): localStorage is the per-device override, and writing
// the house value there would freeze the device against a later house-default
// change. Only a user's own tap (the setters below) persists.
function applyHouseTheme(theme) {
  if (!theme || typeof theme !== 'object') return;
  try { window.FH_THEME = theme; } catch (e) { /* reference only */ }
  const noOverride = (k) => {
    try { return localStorage.getItem(k) === null; } catch (e) { return true; }
  };
  if (theme.mode && noOverride('fh.theme')) stampTheme(theme.mode);
  if (theme.accent && noOverride('fh.accent')) stampAccent(theme.accent);
  if (theme.columns && noOverride('fh.cols')) stampColumns(theme.columns);
  // layout + idle-return are per-device prefs that also accept a house default,
  // same fresh-device-only semantics as the three above (server sends them only
  // when configured, so these no-op on a default install).
  if (theme.layout && noOverride('fh.layout')) stampLayout(theme.layout);
  if (theme.idleReturn && noOverride('fh.idleReturn')) stampIdleReturn(theme.idleReturn);
  reflectThemeControls();
}

// Mirror the live <html> data-* state onto every Display control on the page:
// the quick gear popover's AND (when open) the full Settings overlay's own
// copy, so the two surfaces can never show a stale/conflicting selection.
// Scoped to .theme-ctl containers (both surfaces wrap their Theme/Accent/
// Columns controls in one), not a bare document-wide attribute query: a
// future unrelated element that happened to carry data-theme-set/-c/
// -cols-set for some other purpose would otherwise silently wire into this.
function reflectThemeControls() {
  const el = document.documentElement;
  const mode = el.getAttribute('data-theme');
  const accent = el.getAttribute('data-accent');
  const cols = el.getAttribute('data-cols');
  const layout = el.getAttribute('data-layout');
  // default ON: an unstamped attribute reflects as 'on', never a blank control
  const idle = el.getAttribute('data-idle-return') === 'off' ? 'off' : 'on';
  document.querySelectorAll('.theme-ctl').forEach((ctl) => {
    ctl.querySelectorAll('[data-theme-set]').forEach((b) =>
      b.classList.toggle('on', b.dataset.themeSet === mode));
    ctl.querySelectorAll('[data-c]').forEach((b) =>
      b.classList.toggle('on', b.dataset.c === accent));
    ctl.querySelectorAll('[data-cols-set]').forEach((b) =>
      b.classList.toggle('on', b.dataset.colsSet === cols));
    // Layout marks the active choice (auto/desktop) — the control reflects what
    // the operator picked (data-layout on <html>, stamped by theme.js).
    ctl.querySelectorAll('[data-layout-set]').forEach((b) =>
      b.classList.toggle('on', b.dataset.layoutSet === layout));
    // Auto-return On/Off (data-idle-return), same reflection shape.
    ctl.querySelectorAll('[data-idle-set]').forEach((b) =>
      b.classList.toggle('on', b.dataset.idleSet === idle));
  });
}

/* The settings popover's Integrations section: one on/off switch per available
   data source / tile. Renders from the /api/hub `integrations` block each poll,
   and mirrors each disabled one onto a body class so CSS hides its tile. */
function renderIntegrations(data) {
  const list = (data && data.integrations) || [];
  lastIntegrations = list;
  list.forEach((it) =>
    document.body.classList.toggle('integ-off-' + it.id, !it.enabled));
  const host = document.getElementById('integrations-ctl');
  if (!host) return;
  host.innerHTML = list.length
    ? list.map((it) => {
      // Auth-failure / error is a first-class state: a revoked or expired login
      // shows "reconnect" so the family knows to fix it (the cached view stays).
      const warn = it.status === 'needs_auth' ? 'reconnect'
        : (it.status === 'error' ? 'error' : '');
      return `<button class="integ-row" type="button" role="switch"`
        + ` aria-checked="${it.enabled ? 'true' : 'false'}"`
        + ` data-integ-toggle="${escapeHtml(it.id)}">`
        + `<span class="integ-name">${escapeHtml(it.name)}`
        + (warn ? `<span class="integ-warn">${warn}</span>` : '')
        + `</span>`
        + `<span class="integ-switch${it.enabled ? ' on' : ''}" aria-hidden="true"></span>`
        + `</button>`;
    }).join('')
    : `<div class="integ-empty">none configured</div>`;
}

async function toggleIntegration(id) {
  const cur = (lastIntegrations.find((x) => x.id === id) || {}).enabled;
  // attemptTodo resolves to {ok, error?} and never throws, so the failure
  // check must read .ok (a bare `!r` is always false: even {ok:false} is a
  // truthy object). Left unchecked, a failed PATCH silently reverted the
  // switch on the next poll() with no toast: the exact silent-failure this
  // repo's review gate calls out. Caught fixing the CalDAV enable switch,
  // which reuses this same function.
  const r = await attemptTodo('/api/integrations/' + encodeURIComponent(id),
    'PATCH', { enabled: !cur });
  if (!r.ok) { showToast('Couldn’t save — check the hub and tap again.'); return; }
  await poll();   // re-render the toggles + tile gating from fresh state
}

/* --------------------------------------------- Settings overlay (T: gear) */
/* The full-screen Settings overlay (openOverlay('settings')): Display (a
   second copy of the gear popover's Theme/Accent/Columns controls, laid out
   with room to breathe; reflectThemeControls keeps both copies in sync) and
   Integrations (the existing switch list, plus the richer iCloud CalDAV panel
   below it). Built the same way as chores-full/todos-full/cal-full: an
   instant paint from cached state, wired through the same delegated click
   listener as the rest of the app. */
function renderSettingsFull() {
  const host = document.getElementById('settings-full');
  if (!host) return;
  host.innerHTML = `<div class="overlay-title">Settings</div>`
    + `<div class="card pad settings-card">`
    + `<div class="shead"><span class="tick"></span><h2>Display</h2></div>`
    + `<div class="theme-ctl">`
    + `<div class="settings-row"><span class="settings-k">Theme</span>`
    + `<div class="seg seg-theme" role="group" aria-label="Theme">`
    + `<button type="button" data-theme-set="light">Light</button>`
    + `<button type="button" data-theme-set="soft">Soft</button>`
    + `<button type="button" data-theme-set="dark">Blue</button>`
    + `<button type="button" data-theme-set="grey">Grey</button>`
    + `<button type="button" data-theme-set="black">Black</button>`
    + `</div></div>`
    + `<div class="settings-row"><span class="settings-k">Accent</span>`
    + `<div class="swatches" role="group" aria-label="Accent color">`
    + `<button class="swatch" type="button" data-c="cyan" aria-label="Cyan accent"></button>`
    + `<button class="swatch" type="button" data-c="violet" aria-label="Violet accent"></button>`
    + `<button class="swatch" type="button" data-c="amber" aria-label="Amber accent"></button>`
    + `<button class="swatch" type="button" data-c="green" aria-label="Green accent"></button>`
    + `</div></div>`
    + `<div class="settings-row"><span class="settings-k">Columns</span>`
    + `<div class="seg" role="group" aria-label="Column separation">`
    + `<button type="button" data-cols-set="none">None</button>`
    + `<button type="button" data-cols-set="wells">Wells</button>`
    + `<button type="button" data-cols-set="lines">Lines</button>`
    + `</div></div>`
    // Auto follows screen width; Desktop forces the full wall at any width
    // (the escape hatch for a TV that mis-reports a phone-narrow width).
    + `<div class="settings-row"><span class="settings-k">Layout</span>`
    + `<div class="seg" role="group" aria-label="Layout mode">`
    + `<button type="button" data-layout-set="auto">Auto</button>`
    + `<button type="button" data-layout-set="desktop">Desktop</button>`
    + `</div></div>`
    // Auto-return: On = drift back to the home wall after an idle timeout (the
    // shared-wall default); Off = stay on the page you opened (a personal phone/TV).
    + `<div class="settings-row"><span class="settings-k">Auto-return</span>`
    + `<div class="seg" role="group" aria-label="Return to home when idle">`
    + `<button type="button" data-idle-set="on">On</button>`
    + `<button type="button" data-idle-set="off">Off</button>`
    + `</div></div>`
    + `</div></div>`
    + `<div class="card pad settings-card">`
    + `<div class="shead"><span class="tick"></span><h2>Integrations</h2></div>`
    + `<div class="integrations-ctl" id="integrations-ctl" role="group" aria-label="Integrations"></div>`
    + `<div class="todo-source-ctl" id="todo-source-ctl"></div>`
    + `<div class="caldav-panel" id="caldav-panel"></div>`
    + `</div>`;
  reflectThemeControls();
  renderIntegrations(hubData || { integrations: lastIntegrations });
  renderTodoSourcePicker();
  renderCaldavPanel();
  fetchCaldavCollections();   // refresh the Calendars picker every time the panel opens
}

/* The To-Do source picker inside the Integrations card: choose whether the
   To-Do surface reads the local whiteboard or iCloud Reminders. Shown only when
   CalDAV is available at all (an icloud_caldav entry exists) — there's nothing
   to pick otherwise. Its own host + render fn (like renderIntegrations /
   renderCaldavPanel) so a source switch can repaint just this row without the
   collections re-fetch renderSettingsFull does. */
function renderTodoSourcePicker() {
  const host = document.getElementById('todo-source-ctl');
  if (!host) return;
  const caldav = caldavIntegration();
  if (!caldav) { host.innerHTML = ''; return; }
  const source = (hubData && hubData.todo_source) || 'local';
  const readonly = caldav.readonly !== false;   // server default is 1-way (true)
  // iCloud chosen but still read-only: reminders show but can't be checked off
  // until two-way is on. The Sync direction toggle sits just below in this card.
  const hint = (source === 'icloud' && readonly)
    ? `<div class="hint">Reminders show read-only until you set Sync direction to 2-way, below.</div>`
    : '';
  host.innerHTML = `<div class="settings-row"><span class="settings-k">To-Do list</span>`
    + `<div class="segmented" role="group" aria-label="To-Do list source">`
    + `<button class="seg-btn${source === 'local' ? ' active' : ''}" type="button" data-todo-source="local">On this hub</button>`
    + `<button class="seg-btn${source === 'icloud' ? ' active' : ''}" type="button" data-todo-source="icloud">iCloud</button>`
    + `</div></div>${hint}`;
}

async function setTodoSource(source) {
  if (source === ((hubData && hubData.todo_source) || 'local')) return;   // no-op tap
  const r = await attemptTodo('/api/todo-source', 'PATCH', { source });
  if (!r.ok) { showToast('Couldn’t switch the To-Do list — check the hub and tap again.'); return; }
  await poll();                 // hubData.todo_source updates; the home card repaints
  renderTodoSourcePicker();     // reflect the new selection + read-only hint
  if (todosViewActive()) await renderTodosFull();   // repaint the open full view for the new source
}

/* The live icloud_caldav entry from /api/hub's `integrations` list (or
   lastIntegrations before the first poll lands), or null when no credentials
   are stored yet. Shared by renderCaldavPanel and fetchCaldavCollections so
   both agree on what "connected" means. */
function caldavIntegration() {
  const list = (hubData && hubData.integrations) || lastIntegrations || [];
  return list.find((it) => it.id === 'icloud_caldav') || null;
}

/* The iCloud (CalDAV) account panel inside the Integrations card. Markup
   comes from common.js caldavPanelHtml (pure, tested); this just supplies the
   live integration entry + the transient UI state and writes the result.
   Deliberately NOT called from poll(): unlike the plain integrations switch
   list (which is safe to redraw every 60s), the not-connected state holds
   live text-input fields the operator may be mid-typing into, and refreshing
   it on a timer would wipe an in-progress Apple ID / password. It's refreshed
   explicitly instead, after every action that can change what it should show
   (connect, disconnect, test, the enable switch, the readonly toggle, a
   Calendars-picker fetch/toggle) and once up front when the Settings overlay
   opens. */
function renderCaldavPanel() {
  const host = document.getElementById('caldav-panel');
  if (!host) return;
  host.innerHTML = caldavPanelHtml(caldavIntegration(), caldavUi);
}

/* GET the discovered iCloud calendars + reminder lists for the connected
   panel's Calendars picker. Fire-and-forget from renderSettingsFull (panel
   open) and testCaldavConnection (a test just ran a sync, which can surface
   new calendars), the same un-awaited "refresh a data source in the
   background" pattern this file already uses for fetchWeather/fetchClimate,
   and awaited directly by toggleCaldavCollection, which needs the refreshed
   list before it re-renders. Guarded: not connected, or any fetch failure,
   just leaves the list empty, which caldavCollectionsHtml renders as its own
   empty state - never throws out of a render-triggering call. */
async function fetchCaldavCollections() {
  if (!caldavIntegration()) {
    caldavUi.collections = [];
    caldavUi.collectionsError = false;
    renderCaldavPanel();
    return;
  }
  try {
    const r = await j('/api/integrations/icloud_caldav/collections');
    caldavUi.collections = (r && Array.isArray(r.collections)) ? r.collections : [];
    caldavUi.collectionsError = false;
  } catch (e) {
    // Same rule fetchCalWindow (above) follows for the calendar feed: keep
    // whatever list was last shown rather than wiping it to empty, and log +
    // flag the failure so it can't be confused with a genuinely empty
    // account. Without this, a transient network blip renders the exact same
    // "No calendars found yet" copy as zero calendars actually existing -
    // caldavCollectionsHtml uses collectionsError to tell the two apart.
    console.warn('caldav collections refresh failed; keeping the last list shown', e);
    caldavUi.collectionsError = true;
  }
  renderCaldavPanel();
}

/* Show/hide one iCloud calendar or reminder list on the wall. id contains a
   colon (e.g. "caldav:ab12"), so it's encodeURIComponent'd into the URL path;
   caldavCollectionsHtml separately escapeHtml's it into the data attribute
   this reads from - two different sinks, two different encodings. On a
   failed PATCH the switch must NOT flip (attemptTodo never throws, so the
   check reads .ok, same fix as toggleIntegration above). */
async function toggleCaldavCollection(id) {
  const cur = (caldavUi.collections || []).find((c) => String(c.id) === id);
  if (!cur) return;
  const r = await attemptTodo(
    '/api/integrations/icloud_caldav/collections/' + encodeURIComponent(id),
    'PATCH', { enabled: !cur.enabled });
  if (!r.ok) { showToast('Couldn’t save — check the hub and tap again.'); return; }
  await fetchCaldavCollections();
}

/* Store the operator-entered iCloud credentials (POST, server-side file,
   never echoed back), then re-poll so the panel flips to the connected view,
   and auto-run a Test so they immediately see whether they typed the right
   app-specific password rather than finding out on the next sync. */
async function connectCaldav() {
  const userInput = document.getElementById('caldav-user-input');
  const pwInput = document.getElementById('caldav-pw-input');
  const user = ((userInput && userInput.value) || '').trim();
  const pw = (pwInput && pwInput.value) || '';
  if (!user || !pw) {
    caldavUi.formError = 'Enter both the Apple ID and the app-specific password.';
    renderCaldavPanel();
    return;
  }
  caldavUi.formError = '';
  caldavUi.connecting = true;
  renderCaldavPanel();
  try {
    await j('/api/integrations/icloud_caldav/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, app_password: pw }),
    });
  } catch (e) {
    caldavUi.connecting = false;
    showToast(e.message || 'Couldn’t connect - check the Apple ID and password.');
    renderCaldavPanel();
    return;
  }
  // The password's only job was to reach that POST body. Blank it the instant
  // the request succeeds; never rely solely on the next render to clear it.
  if (pwInput) pwInput.value = '';
  caldavUi.connecting = false;
  await poll();   // hubData.integrations now carries icloud_caldav + its account
  renderCaldavPanel();
  await testCaldavConnection();
}

/* POST /api/integrations/icloud_caldav/test never throws on a bad sign-in
   (it reports needs_auth/error in the 200 body); only a network-level
   failure lands in the catch, folded into the same {ok:false, error} shape
   so caldavTestMessage has one contract to format either way. */
async function testCaldavConnection() {
  caldavUi.testing = true;
  renderCaldavPanel();
  let result;
  try {
    result = await j('/api/integrations/icloud_caldav/test', { method: 'POST' });
  } catch (e) {
    result = { ok: false, error: e.message || 'Couldn’t reach the hub.' };
  }
  caldavUi.testing = false;
  caldavUi.testResult = result;
  renderCaldavPanel();
  // A test just ran (or attempted) a sync server-side, which can surface new
  // calendars/reminder lists - refresh the Calendars picker independently of
  // the result text above, which is already showing.
  fetchCaldavCollections();
}

async function disconnectCaldav() {
  const r = await attemptTodo('/api/integrations/icloud_caldav/credentials', 'DELETE');
  if (!r.ok) { showToast('Couldn’t disconnect - check the hub and try again.'); return; }
  caldavUi.testResult = null;   // stale "connected" test result would be misleading now
  caldavUi.formError = '';
  caldavUi.collections = [];   // stale calendar list would be misleading too
  caldavUi.collectionsError = false;
  await poll();
  renderCaldavPanel();
}

async function setCaldavReadonly(readonly) {
  const r = await attemptTodo('/api/integrations/icloud_caldav', 'PATCH', { readonly });
  if (!r.ok) { showToast('Couldn’t save — check the hub and tap again.'); return; }
  await poll();
  renderCaldavPanel();
}

function closeThemePop() {
  const pop = document.getElementById('theme-pop');
  const gear = document.getElementById('wall-gear');
  if (pop) pop.classList.remove('open');
  if (gear) gear.setAttribute('aria-expanded', 'false');
}

// Separate delegated listener (the big one above owns the dashboard surfaces):
// the gear popover, the Settings overlay's Display + Integrations controls
// (same data-* attributes, generalized below to match either surface), and
// the iCloud CalDAV panel's actions.
document.addEventListener('click', (e) => {
  const pop = document.getElementById('theme-pop');
  // Refresh button: reload the wall (picks up a new deploy, unsticks a stale page).
  if (e.target.closest('#wall-refresh')) { location.reload(); return; }
  const gear = e.target.closest('#wall-gear');
  if (gear && pop) {
    const open = pop.classList.toggle('open');
    gear.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) reflectThemeControls();
    return;
  }
  // The popover's "All settings" row: close the quick popover (it sits at a
  // higher z-index than the full-screen overlay and would otherwise float
  // over it) and open the real thing.
  if (e.target.closest('#theme-pop [data-open-settings]')) {
    closeThemePop();
    openOverlay('settings');
    return;
  }
  // Theme/Accent/Columns: scoped to '.theme-ctl [...]', not bare '[...]'.
  // NOT #theme-pop specifically (the Settings overlay renders its own copy
  // of these same buttons, see renderSettingsFull), but still narrowed to
  // "inside a .theme-ctl", the wrapper both surfaces use, so an unrelated
  // future element elsewhere on the page can't accidentally wire into
  // setTheme/setAccent/setColumns just by reusing one of these attribute
  // names for something else.
  const t = e.target.closest('.theme-ctl [data-theme-set]');
  if (t) { setTheme(t.dataset.themeSet); reflectThemeControls(); return; }
  const a = e.target.closest('.theme-ctl [data-c]');
  if (a) { setAccent(a.dataset.c); reflectThemeControls(); return; }
  const c = e.target.closest('.theme-ctl [data-cols-set]');
  if (c) { setColumns(c.dataset.colsSet); reflectThemeControls(); return; }
  // Layout (Auto/Desktop): scoped to '.theme-ctl [...]' like the controls above,
  // so a tap works in either the gear popover or the Settings overlay's copy.
  const ly = e.target.closest('.theme-ctl [data-layout-set]');
  if (ly) { setLayout(ly.dataset.layoutSet); reflectThemeControls(); return; }
  // Auto-return On/Off. Re-arm on the spot when an overlay is open, so flipping
  // it OFF clears the pending return-home timer (and ON re-arms it) immediately,
  // not only on the next interaction.
  const ir = e.target.closest('.theme-ctl [data-idle-set]');
  if (ir) { setIdleReturn(ir.dataset.idleSet); reflectThemeControls(); if (openView) armIdle(); return; }
  // Integrations switch list: also unscoped now that it only ever renders
  // inside the Settings overlay (#integrations-ctl), never the popover.
  const ig = e.target.closest('[data-integ-toggle]');
  if (ig) { toggleIntegration(ig.dataset.integToggle); return; }
  // To-Do source picker (local vs iCloud), shown in the Integrations card.
  const ts = e.target.closest('[data-todo-source]');
  if (ts) { setTodoSource(ts.dataset.todoSource); return; }
  // iCloud (CalDAV) panel actions. (No data-caldav-enable-toggle: the panel
  // deliberately has no second enable switch; see caldavPanelHtml.)
  if (e.target.closest('[data-caldav-connect]')) { connectCaldav(); return; }
  if (e.target.closest('[data-caldav-disconnect]')) { disconnectCaldav(); return; }
  if (e.target.closest('[data-caldav-test]')) { testCaldavConnection(); return; }
  const ro = e.target.closest('[data-caldav-readonly]');
  if (ro) { setCaldavReadonly(ro.dataset.caldavReadonly === '1'); return; }
  const cc = e.target.closest('[data-caldav-collection-toggle]');
  if (cc) { toggleCaldavCollection(cc.dataset.caldavCollectionToggle); return; }
  // a tap anywhere outside an open popover dismisses it
  if (pop && pop.classList.contains('open') && !e.target.closest('#theme-pop')) {
    closeThemePop();
  }
});

// Escape also dismisses the gear popover (T5a) — parity with the outside-tap.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const pop = document.getElementById('theme-pop');
  if (pop && pop.classList.contains('open')) closeThemePop();
});

tickClock();
setInterval(tickClock, 1000);
poll().then(probeCamera);
fetchWeather();
fetchClimate();
setInterval(scheduledPoll, POLL_MS);
setInterval(fetchWeather, POLL_MS);
setInterval(fetchClimate, POLL_MS);
setInterval(scheduledProbeCamera, CAM_PROBE_MS);
