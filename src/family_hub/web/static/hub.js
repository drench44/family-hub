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

let links = {};
let weatherData = null;   // last /api/tiles/weather payload (native weather card)
let climateData = null;   // last /api/tiles/climate payload (native climate card)
let weatherFails = 0;     // consecutive weather fetch failures (see fetchWeather)
let climateFails = 0;     // consecutive climate fetch failures (see fetchClimate)
const TILE_FAIL_LIMIT = 3;   // keep the last good card until this many in a row
let warnedNoWeatherSlot = false;   // one-time warn: weather_base set, no 'weather' panel
let warnedNoClimateSlot = false;   // one-time warn: climate_base set, no 'climate' panel
let lastPeople = [];      // remember done-counts to fire the celebration once
const celebrated = new Set();

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
   shows every day. */
function agendaHtml(events, startStr, todayStr, maxDays, skipEmptyAfter) {
  const byDay = bucketByDay(events);
  let html = '';
  for (let i = 0; i < maxDays; i++) {
    const d = addDays(startStr, i);
    const evs = byDay[d] || [];
    if (skipEmptyAfter != null && i >= skipEmptyAfter && evs.length === 0) continue;
    const isToday = d === todayStr;
    html += `<div class="card cal-day${isToday ? ' is-today' : ''}">`
      + dayHeadHtml(d, todayStr)
      + (evs.length ? evs.map((ev) => eventRow(ev, d)).join('')
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
function sectionHead(label, { overlay, expandLabel } = {}) {
  const act = (overlay && expandLabel)
    ? `<span class="act"><button class="expand" type="button"`
      + ` data-overlay="${escapeHtml(overlay)}">⛶ ${escapeHtml(expandLabel)}</button></span>`
    : '';
  return `<div class="shead"><span class="tick"></span><h2>${escapeHtml(label)}</h2>${act}</div>`;
}

function renderCalendar(data) {
  indexEvents(data.calendar.events);
  document.getElementById('cal').innerHTML =
    sectionHead('Calendar', { overlay: 'calendar', expandLabel: 'Month view' })
    + calStatusNote(data.calendar)
    + agendaHtml(data.calendar.events, data.date, data.date, 5, 2);
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

function monthCellHtml(cell, byDay, todayStr) {
  const evs = byDay[cell.date] || [];
  const shown = evs.slice(0, 3);
  const more = evs.length - shown.length;
  const cls = ['mg-day'];
  if (!cell.inMonth) cls.push('mg-out');
  if (cell.date === todayStr) cls.push('mg-today');
  const dayNum = Number(cell.date.slice(8, 10));
  return `<div class="${cls.join(' ')}" data-date="${cell.date}" tabindex="0">`
    + `<span class="mg-num num">${dayNum}</span>`
    + shown.map((ev) =>
      `<span class="mg-ev" data-eid="${escapeHtml(ev.id)}">`
      + `<span class="mg-dot" style="background:${safeColor(eventColor(ev))}"></span>`
      + `<span class="mg-ev-title">${escapeHtml(ev.title)}</span></span>`).join('')
    + (more > 0 ? `<span class="mg-more">+${more} more</span>` : '')
    + `</div>`;
}

function monthHtml(y, m, events, todayStr) {
  const byDay = bucketByDay(events);
  const heads = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    .map((d) => `<span class="mg-head">${d}</span>`).join('');
  const cells = monthGrid(y, m).map((c) => monthCellHtml(c, byDay, todayStr)).join('');
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
  let title = '';
  let body = '';
  if (calState.mode === 'day') {
    title = monthName(calState.y, calState.m);
    body = `<button class="cal-back" type="button" data-calback="1">‹ back to month</button>`
      + agendaHtml(events, calState.day, todayStr, 1, null);
  } else if (calState.mode === 'agenda') {
    title = monthName(calState.y, calState.m);
    body = agendaHtml(events, calState.weekStart, todayStr, 7, null);
  } else {
    title = monthName(calState.y, calState.m);
    body = monthHtml(calState.y, calState.m, events, todayStr);
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
const todoState = { data: null, addBucket: 'now', openId: null };

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

function todosFullHtml() {
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
  try {
    todoState.data = await j('/api/todos');
  } catch (e) {
    // keep the last data (or null -> unreachable message). But if this was a
    // REFRESH (data already populated from a prior load) a silent catch would
    // let the stale pre-mutation list sit on screen while the conn badge
    // still says live, so surface it instead of hiding a failed post-mutation
    // GET behind reassuring-looking old data.
    if (todoState.data != null) showToast('Couldn’t refresh the list — check the hub.');
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
    ok = (await fetch(`/api/tiles/camera.jpg?src=${encodeURIComponent(cam.src)}&probe=${Date.now()}`)).ok;
  } catch (e) { /* down */ }
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
      live = (await fetch(
        `/api/tiles/camera.jpg?src=${encodeURIComponent(cam.hd_src)}&probe=${Date.now()}`)).ok;
    } catch (e) { /* still connecting — treated as not-yet-live */ }
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
    if (todoState.data) renderTodosPaint();  // instant paint from cache
    renderTodosFull();                       // then refresh from the API
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

function armIdle() {
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(closeOverlay, idleReturnMs(openView));
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

/* A quality chip (.q good|warn) — only when there's a label to show. */
function wxChip(cls, label) {
  return (label != null && String(label).trim() !== '')
    ? ` <span class="q ${cls}">${escapeHtml(String(label))}</span>` : '';
}

function wxStat(k, vHtml) {
  return `<div class="stat"><div class="k">${k}</div><div class="v num">${vHtml}</div></div>`;
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

  // Chip logic: UV warns when high (>=6 or a "high"-ish desc); AQI is good when
  // the category says so or the number is <=50, else it warns.
  const uvWarn = Number(wx.uv) >= 6 || /high|extreme|severe|very/i.test(String(wx.uv_desc || ''));
  const uvChip = wxChip(uvWarn ? 'warn' : 'good', wx.uv_desc);
  const aqiGood = /good/i.test(String(wx.aqi_cat || '')) || (wx.aqi != null && Number(wx.aqi) <= 50);
  const aqiChip = wxChip(aqiGood ? 'good' : 'warn', wx.aqi_cat);

  const stats = wxStat('High', wxVal(wx.high, '°'))
    + wxStat('Low', wxVal(wx.low, '°'))
    + wxStat('UV Index', `${wxVal(wx.uv, '')}${uvChip}`)
    + wxStat('Air Quality', `${wxVal(wx.aqi, '')}${aqiChip}`)
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

/* A room runs "hot" (and so warns) at or above HOT_F. The mockup marks a 77°
   room warn, but a real indoor room that warm is normal in summer; 80°F is a
   defensible "actually too hot inside" line. TUNABLE at T10/dogfood against the
   real house feed. A stale sensor also warns (see roomWarns), independent of
   temperature. */
const HOT_F = 80;

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

/* A room warns when its sensor is stale OR it runs hot (temp_f >= HOT_F). A
   non-finite / missing temp_f never triggers the hot branch (it shows as --). */
function roomWarns(room) {
  const t = Number(room.temp_f);
  const hot = room.temp_f != null && String(room.temp_f) !== '' && isFinite(t) && t >= HOT_F;
  return !!room.stale || hot;
}

/* One room row: NAME · temp_f° · humidity%. Missing/non-finite temp -> "--",
   missing humidity -> "—". Values are rounded to whole units to match the
   mockup's clean glance. The name is escaped. */
function roomRowHtml(room) {
  const t = Number(room.temp_f);
  const tempOk = room.temp_f != null && String(room.temp_f) !== '' && isFinite(t);
  const tempCell = tempOk ? `${Math.round(t)}°` : '--';
  const h = Number(room.humidity);
  const humOk = room.humidity != null && String(room.humidity) !== '' && isFinite(h);
  const humCell = humOk ? `${Math.round(h)}%` : '—';
  const name = escapeHtml(String(room.name == null ? '' : room.name));
  return `<div class="room${roomWarns(room) ? ' warn' : ''}">`
    + `<span class="rk">${name}</span>`
    + `<span class="rv num">${tempCell}</span>`
    + `<span class="rh num">${humCell}</span>`
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

window.addEventListener('resize', () => {
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
    || shown('ev-modal') || shown('chore-modal') || shown('confirm-modal')
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
  let state;
  try {
    state = await j('/api/admin/state');   // {people (flat, active flags), chores (full records)}
  } catch (e) {
    showToast('Couldn’t open the editor — check the hub and try again.');
    return;
  }
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
  if (e.target.closest('#overlay-home')) { closeDeleteConfirm(); closeChoreEditor(); closeEventDetail(); closeOverlay(); }
});
['pointerdown', 'touchstart', 'keydown'].forEach((evt) =>
  document.addEventListener(evt, () => { noteInteraction(); if (openView) armIdle(); }, { passive: true }));
document.addEventListener('submit', (e) => {
  if (e.target && e.target.id === 'todo-add-form') { e.preventDefault(); addTodo(); }
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
  reflectThemeControls();
}

// Mirror the live <html> data-* state onto the popover's control buttons.
function reflectThemeControls() {
  const pop = document.getElementById('theme-pop');
  if (!pop) return;
  const el = document.documentElement;
  const mode = el.getAttribute('data-theme');
  const accent = el.getAttribute('data-accent');
  const cols = el.getAttribute('data-cols');
  pop.querySelectorAll('[data-theme-set]').forEach((b) =>
    b.classList.toggle('on', b.dataset.themeSet === mode));
  pop.querySelectorAll('[data-c]').forEach((b) =>
    b.classList.toggle('on', b.dataset.c === accent));
  pop.querySelectorAll('[data-cols-set]').forEach((b) =>
    b.classList.toggle('on', b.dataset.colsSet === cols));
}

function closeThemePop() {
  const pop = document.getElementById('theme-pop');
  const gear = document.getElementById('wall-gear');
  if (pop) pop.classList.remove('open');
  if (gear) gear.setAttribute('aria-expanded', 'false');
}

// Separate delegated listener (the big one above owns the dashboard surfaces).
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
  const t = e.target.closest('#theme-pop [data-theme-set]');
  if (t) { setTheme(t.dataset.themeSet); reflectThemeControls(); return; }
  const a = e.target.closest('#theme-pop [data-c]');
  if (a) { setAccent(a.dataset.c); reflectThemeControls(); return; }
  const c = e.target.closest('#theme-pop [data-cols-set]');
  if (c) { setColumns(c.dataset.colsSet); reflectThemeControls(); return; }
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
setInterval(probeCamera, CAM_PROBE_MS);
