'use strict';

/* Family Hub — helpers shared by the hub wall page (hub.js) and the phone
   admin page (admin.js). Loaded as a classic script BEFORE either; its
   top-level declarations are visible to the scripts that follow. Pure: no DOM
   access here, so tests/js can load it into a vm sandbox with no document. */

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch (e) { /* not json */ }
    throw new Error(detail || `${url} -> HTTP ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
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

/* The list of days an event occupies. Timed events live on their start day;
   all-day events cover [start, end) per Google's exclusive end date. Capped
   at 62 days so a malformed span can't hang the wall. */
function expandDays(ev) {
  const s = (ev.start_ts || '').slice(0, 10);
  if (!ev.all_day) return [s];
  const e = (ev.end_ts || s).slice(0, 10);
  const out = [];
  let d = s;
  while (d < e && out.length < 62) { out.push(d); d = addDays(d, 1); }
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

function buildChorePayload(f) {
  return {
    title: (f.title || '').trim(),
    icon: (f.icon || '').trim(),
    schedule_kind: f.repeat === 'weekly' ? 'days' : 'daily',
    days_mask: choreDaysMask(f.days),
    assign_kind: f.assign,
    fixed_person_id: f.assign === 'fixed'
      ? (f.person === null || f.person === '' || f.person === undefined ? null : Number(f.person))
      : null,
    rotation_order: f.assign === 'rotation' ? (f.rot || []).slice() : [],
  };
}

function choreToModel(ch) {
  const days = new Set();
  for (let i = 0; i < 7; i++) if ((ch.days_mask >> i) & 1) days.add(i);
  return {
    title: ch.title, icon: ch.icon,
    repeat: ch.schedule_kind === 'days' ? 'weekly' : 'daily',
    days, assign: ch.assign_kind, fixed_person_id: ch.fixed_person_id,
    rot: ch.rotation_order.slice(),
  };
}

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']; // bit 0..6

/* One reusable chore form. `model` seeds it; `onsubmit(body, errEl)` fires with
   the assembled request body; `people` is the full people list (active status
   included) the form needs for the person picker + rotation names. Shared by
   the admin page (add card + every inline edit) and the hub wall. */
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
      </div>
      <div class="day-chips f-days"></div></div>
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
  const active = people.filter((p) => p.active);
  $('.f-person').innerHTML = active.map((p) =>
    `<option value="${p.id}"${p.id === model.fixed_person_id ? ' selected' : ''}>${escapeHtml(p.name)}</option>`).join('');

  const paintRepeat = () => {
    $('.f-repeat').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.repeat === model.repeat));
    $('.f-days').classList.toggle('hidden', model.repeat !== 'weekly');
  };
  const paintDays = () => {
    $('.f-days').innerHTML = DAY_LABELS.map((d, i) =>
      `<button class="day-chip${model.days.has(i) ? ' selected' : ''}" type="button" data-day="${i}">${d}</button>`).join('');
  };
  const paintAssign = () => {
    $('.f-assign').querySelectorAll('.seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.assign === model.assign));
    $('.f-person').classList.toggle('hidden', model.assign !== 'fixed');
    $('.f-rotadd').classList.toggle('hidden', model.assign !== 'rotation');
    $('.f-rotation').classList.toggle('hidden', model.assign !== 'rotation');
    $('.f-rothint').classList.toggle('hidden', model.assign !== 'rotation');
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
  paintRepeat(); paintDays(); paintAssign(); paintRotation();

  $('.f-repeat').onclick = (e) => {
    const b = e.target.closest('[data-repeat]');
    if (b) { model.repeat = b.dataset.repeat; paintRepeat(); }
  };
  $('.f-days').onclick = (e) => {
    const b = e.target.closest('[data-day]');
    if (!b) return;
    const i = Number(b.dataset.day);
    if (model.days.has(i)) model.days.delete(i); else model.days.add(i);
    paintDays();
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
    const body = buildChorePayload({
      title: $('.f-title').value,
      icon: $('.f-icon').value,
      repeat: model.repeat,
      days: model.days,
      assign: model.assign,
      person: model.assign === 'fixed' ? $('.f-person').value : null,
      rot: model.rot,
    });
    onsubmit(body, $('.f-error'));
  };
}

function freshChoreModel() {
  return { title: '', icon: '', repeat: 'daily', days: new Set(), assign: 'fixed', fixed_person_id: null, rot: [] };
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

/* The calendar-status banner message ('' = no banner). Pure so the
   needs_auth / not-connected / generic branches are testable without a DOM;
   hub.js's calStatusNote wraps the result in markup. */
function calStatusMessage(status) {
  const st = status || {};
  if (st.ok !== false) return '';
  if (st.needs_auth) {
    // A revoked/expired Google sign-in needs the owner to re-run setup, unlike
    // a transient blip (which shows the generic message below).
    return 'Google sign-in expired — re-run calendar setup to reconnect. Showing the last events we saw.';
  }
  if (String(st.error || '').includes('not configured')) {
    return 'Google Calendar isn’t connected yet — once it’s linked, the family’s events show up here.';
  }
  return 'Calendar sync hit a snag — showing the last events we saw.';
}
