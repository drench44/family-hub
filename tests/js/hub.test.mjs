// Executable tests for the pure helpers in common.js — run with `node --test`.
// The pytest wrapper tests/test_js.py runs this via local node or, on the box,
// a disposable node:20-alpine container.
//
// common.js is a classic script (no exports): load it into a vm sandbox with
// no `document`, and pull the functions out of the sandbox globals.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'family_hub', 'web', 'static');
const sandbox = { document: undefined };
vm.createContext(sandbox);
vm.runInContext(readFileSync(join(staticDir, 'common.js'), 'utf8'), sandbox);
const {
  escapeHtml, fmtTime, dayLabel,
  idleReturnMs, nightClass,
  fmtTimeRange, monthName, eventColor,
} = sandbox;
const panelFit = (...a) => ({ ...sandbox.panelFit(...a) });
const monthGrid = (...a) => JSON.parse(JSON.stringify(sandbox.monthGrid(...a)));
const shiftMonth = (...a) => ({ ...sandbox.shiftMonth(...a) });

test('addDays crosses months and years', () => {
  assert.equal(sandbox.addDays('2026-08-31', 1), '2026-09-01');
  assert.equal(sandbox.addDays('2026-01-01', -1), '2025-12-31');
});

test('expandDays: timed=start day; multi-day all-day covers every day (end exclusive)', () => {
  const timed = { all_day: 0, start_ts: '2026-08-13T10:00:00-07:00', end_ts: '2026-08-13T11:00:00-07:00' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(timed))), ['2026-08-13']);
  const span = { all_day: 1, start_ts: '2026-08-01', end_ts: '2026-08-04' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(span))),
    ['2026-08-01', '2026-08-02', '2026-08-03']);
  const single = { all_day: 1, start_ts: '2026-08-01', end_ts: '2026-08-02' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(single))), ['2026-08-01']);
});

test('fmtTimeRange: all-day, same-day span, cross-day span', () => {
  assert.equal(fmtTimeRange({ all_day: 1, start_ts: '2026-08-13', end_ts: '2026-08-14' }), 'all day');
  assert.equal(fmtTimeRange({ all_day: 1, start_ts: '2026-08-01', end_ts: '2026-08-09' }),
    '8/1 – 8/8 · all day');
  assert.equal(fmtTimeRange({
    all_day: 0,
    start_ts: '2026-08-13T10:00:00-07:00', end_ts: '2026-08-13T11:30:00-07:00',
  }), '10am – 11:30am');
  assert.equal(fmtTimeRange({
    all_day: 0,
    start_ts: '2026-08-12T22:00:00-07:00', end_ts: '2026-08-13T06:00:00-07:00',
  }), '8/12 10pm – 8/13 6am');
});

test('monthGrid: 42 Sunday-first cells around Aug 2026', () => {
  const g = monthGrid(2026, 8);           // Aug 1 2026 is a Saturday
  assert.equal(g.length, 42);
  assert.deepEqual(g[0], { date: '2026-07-26', inMonth: false });  // Sunday
  assert.deepEqual(g[6], { date: '2026-08-01', inMonth: true });   // Saturday col
  assert.deepEqual(g[41], { date: '2026-09-05', inMonth: false });
});

test('monthGrid handles year boundaries', () => {
  const jan = monthGrid(2027, 1);          // Jan 1 2027 is a Friday
  assert.equal(jan[0].date, '2026-12-27'); // Sunday before
  assert.ok(jan.some((c) => c.date === '2027-01-31' && c.inMonth));
});

test('shiftMonth wraps across years', () => {
  assert.deepEqual(shiftMonth(2026, 12, 1), { y: 2027, m: 1 });
  assert.deepEqual(shiftMonth(2026, 1, -1), { y: 2025, m: 12 });
  assert.deepEqual(shiftMonth(2026, 8, 0), { y: 2026, m: 8 });
});

test('monthName', () => {
  assert.equal(monthName(2026, 8), 'August 2026');
  assert.equal(monthName(2027, 1), 'January 2027');
});

test('descToText: Google HTML descriptions render as clean text', () => {
  const raw = '<b> Justice Hackworth</b><br><strong> New Leaf Pest Control</strong>'
    + '<br><strong> New Leaf Crawl Space &amp; Drainage Solutions</strong>'
    + '<br><strong> 360-562-0650</strong><br>Visit us online at:<br>'
    + '<a href="http://www.example.com/" target="_blank">www.example.com</a>';
  assert.equal(sandbox.descToText(raw),
    'Justice Hackworth\nNew Leaf Pest Control\nNew Leaf Crawl Space & Drainage Solutions'
    + '\n360-562-0650\nVisit us online at:\nwww.example.com');
});

test('descToText: plain text passes through, entities decode once', () => {
  assert.equal(sandbox.descToText('bring the card'), 'bring the card');
  assert.equal(sandbox.descToText('A &amp;amp; B'), 'A &amp; B');  // no double-decode
  assert.equal(sandbox.descToText('<ul><li>one</li><li>two</li></ul>'), '• one\n• two');
});

test('eventEnded: timed events end, all-day events never strike', () => {
  const now = Date.parse('2026-08-12T20:00:00-07:00');
  const over = { all_day: 0, start_ts: '2026-08-12T12:00:00-07:00', end_ts: '2026-08-12T13:00:00-07:00' };
  const ongoing = { all_day: 0, start_ts: '2026-08-12T19:30:00-07:00', end_ts: '2026-08-12T20:30:00-07:00' };
  const allday = { all_day: 1, start_ts: '2026-08-12', end_ts: '2026-08-13' };
  assert.equal(sandbox.eventEnded(over, now), true);
  assert.equal(sandbox.eventEnded(ongoing, now), false);
  assert.equal(sandbox.eventEnded(allday, now), false);
  assert.equal(sandbox.eventEnded({ all_day: 0, start_ts: 'garbage' }, now), false);
});

test('eventColor: explicit event color beats calendar color beats fallback', () => {
  assert.equal(eventColor({ event_color: '#F4511E', color: '#5BC9F0' }), '#F4511E');
  assert.equal(eventColor({ event_color: null, color: '#5BC9F0' }), '#5BC9F0');
  assert.equal(eventColor({}), '#8593A9');
});

test('panelFit scales a wide page to the slot width when height allows', () => {
  // 1280x720 virtual page into an 800px-wide slot capped at 720 high:
  // width is the binding constraint -> scale 0.625, no horizontal offset
  assert.deepEqual(panelFit(800, 1280, 720, 720),
    { scale: 0.625, width: 800, height: 450, offsetX: 0 });
});

test('panelFit caps by height and centers the leftover width', () => {
  // 900x900 square into an 800px slot capped at 430 high: height binds
  // (430/900 ≈ 0.478) -> content 430x430, centered with 185px on each side
  const f = panelFit(800, 900, 900, 430);
  assert.equal(f.height, 430);
  assert.equal(f.width, 430);
  assert.equal(f.offsetX, 185);
  assert.ok(Math.abs(f.scale - 430 / 900) < 1e-9);
});

test('panelFit never upscales past 1:1', () => {
  const f = panelFit(2000, 1280, 720, 2000);
  assert.equal(f.scale, 1);
  assert.deepEqual([f.width, f.height], [1280, 720]);
});

test('escapeHtml neutralizes markup', () => {
  assert.equal(escapeHtml('<b>&"\''), '&lt;b&gt;&amp;&quot;&#39;');
});

test('fmtTime am/pm, on-the-hour, and midnight/noon', () => {
  assert.equal(fmtTime('2026-08-13T07:30:00-07:00'), '7:30am');
  assert.equal(fmtTime('2026-08-13T19:05:00-07:00'), '7:05pm');
  assert.equal(fmtTime('2026-08-13T09:00:00-07:00'), '9am');       // on the hour
  assert.equal(fmtTime('2026-08-13T00:00:00-07:00'), '12am');      // midnight
  assert.equal(fmtTime('2026-08-13T12:00:00-07:00'), '12pm');      // noon
});

test('dayLabel names today, tomorrow, else weekday m/d', () => {
  assert.equal(dayLabel('2026-08-13', '2026-08-13'), 'Today');
  assert.equal(dayLabel('2026-08-14', '2026-08-13'), 'Tomorrow');
  assert.equal(dayLabel('2026-08-19', '2026-08-13'), 'Wed 8/19');
  // month boundary still works
  assert.equal(dayLabel('2026-09-01', '2026-08-31'), 'Tomorrow');
});

test('dayHeadHtml: today/tomorrow carry the date, far days carry distance', () => {
  const head = (d, t) => sandbox.dayHeadHtml(d, t);
  const today = head('2026-08-13', '2026-08-13');
  assert.ok(today.includes('<span class="cal-dayname">Today</span>'));
  assert.ok(today.includes('<span class="cal-daydate">Thu 8/13</span>'));
  assert.ok(!today.includes('cal-dayrel'));
  const tomorrow = head('2026-08-14', '2026-08-13');
  assert.ok(tomorrow.includes('<span class="cal-dayname">Tomorrow</span>'));
  assert.ok(tomorrow.includes('<span class="cal-daydate">Fri 8/14</span>'));
  const far = head('2026-08-17', '2026-08-13');
  assert.ok(far.includes('<span class="cal-dayname">Mon 8/17</span>'));
  assert.ok(far.includes('<span class="cal-dayrel">in 4 days</span>'));
  assert.ok(!far.includes('cal-daydate'));
  // past days (full-calendar week view can show them)
  assert.ok(head('2026-08-12', '2026-08-13').includes('yesterday'));
  assert.ok(head('2026-08-10', '2026-08-13').includes('3 days ago'));
  // month boundary: distance math is local-midnight, no DST drift
  assert.ok(head('2026-09-01', '2026-08-31').includes('Tomorrow'));
});

test('idleReturnMs: camera views longest, calendar gets planning time', () => {
  assert.equal(idleReturnMs('camera'), 300000);
  assert.equal(idleReturnMs('camera:wyze'), 300000);
  assert.equal(idleReturnMs('climate'), 90000);
  assert.equal(idleReturnMs('calendar'), 180000);
});

test('nightClass covers 22:00–05:59', () => {
  assert.equal(nightClass(21), '');
  assert.equal(nightClass(22), 'is-night');
  assert.equal(nightClass(5), 'is-night');
  assert.equal(nightClass(6), '');
});

// --- admin chore payload serialization (extracted into common.js so the
// admin form's model->POST logic is testable; a bad days_mask/rotation_order
// would otherwise only show up as a silent 422 or a wrong chore on the wall).

test('buildChorePayload: daily fixed chore trims and shapes correctly', () => {
  // JSON-normalize: the sandbox returns cross-realm objects (same pattern the
  // panelFit/monthGrid helpers above use for deepEqual).
  const p = JSON.parse(JSON.stringify(sandbox.buildChorePayload({
    title: '  Dishes  ', icon: '🍽️', repeat: 'daily',
    days: new Set(), assign: 'fixed', person: '3', rot: [],
  })));
  assert.deepEqual(p, {
    title: 'Dishes', icon: '🍽️', schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 3, rotation_order: [],
  });
});

test('buildChorePayload: weekly rotation computes days_mask (Mon/Wed/Fri=21)', () => {
  const p = sandbox.buildChorePayload({
    title: 'Trash', icon: '', repeat: 'weekly',
    days: new Set([0, 2, 4]), assign: 'rotation', person: null, rot: [5, 6],
  });
  assert.equal(p.schedule_kind, 'days');
  assert.equal(p.days_mask, 21);            // bits 0,2,4 -> 1+4+16
  assert.equal(p.assign_kind, 'rotation');
  assert.deepEqual(p.rotation_order, [5, 6]);
  assert.equal(p.fixed_person_id, null);
});

test('buildChorePayload: empty person selection serializes to null, not 0', () => {
  const p = sandbox.buildChorePayload({
    title: 'x', icon: '', repeat: 'daily', days: new Set(),
    assign: 'fixed', person: '', rot: [],
  });
  assert.equal(p.fixed_person_id, null);
});

// --- chore toggle failure detection (drives the "couldn't save" toast).

test('attemptToggle returns false when the write fails', async () => {
  const orig = sandbox.j;
  sandbox.j = async () => { throw new Error('write failed'); };
  try {
    assert.equal(await sandbox.attemptToggle(1, false), false);
  } finally {
    sandbox.j = orig;
  }
});

test('attemptToggle returns true and picks DELETE (done) vs POST (undone)', async () => {
  const orig = sandbox.j;
  const calls = [];
  sandbox.j = async (url, opts) => { calls.push([url, opts.method]); return {}; };
  try {
    assert.equal(await sandbox.attemptToggle(7, true), true);
    assert.equal(await sandbox.attemptToggle(8, false), true);
    assert.equal(calls.length, 2);
    assert.equal(calls[0][0], '/api/chores/7/complete');
    assert.equal(calls[0][1], 'DELETE');   // done -> DELETE
    assert.equal(calls[1][1], 'POST');     // undone -> POST
  } finally {
    sandbox.j = orig;
  }
});

// --- calendar-status banner branches (extracted from hub.js so the
// needs_auth / not-connected / generic messaging is testable).

test('calStatusMessage: ok status shows no banner', () => {
  assert.equal(sandbox.calStatusMessage({ ok: true }), '');
  assert.equal(sandbox.calStatusMessage({}), '');   // ok undefined != false -> no banner
});

test('calStatusMessage: revoked token asks to re-run setup', () => {
  const m = sandbox.calStatusMessage({ ok: false, needs_auth: true });
  assert.match(m, /sign-in expired/);
  assert.match(m, /re-run calendar setup/);
});

test('calStatusMessage: not-configured vs generic error', () => {
  assert.match(sandbox.calStatusMessage({ ok: false, error: 'not configured' }), /isn.t connected yet/);
  assert.match(sandbox.calStatusMessage({ ok: false, error: 'quota exceeded' }), /hit a snag/);
});

test('calStatusMessage: needs_auth wins over a not-configured error string', () => {
  const m = sandbox.calStatusMessage({ ok: false, needs_auth: true, error: 'not configured' });
  assert.match(m, /sign-in expired/);
});

test('chore round-trips: choreToModel -> buildChorePayload preserves schedule/assign', () => {
  const chore = {
    title: 'Trash', icon: '🗑️', schedule_kind: 'days', days_mask: 21,
    assign_kind: 'rotation', fixed_person_id: null, rotation_order: [5, 6],
  };
  const model = sandbox.choreToModel(chore);
  const payload = sandbox.buildChorePayload({
    title: chore.title, icon: chore.icon, repeat: model.repeat,
    days: model.days, assign: model.assign, person: null, rot: model.rot,
  });
  assert.equal(payload.days_mask, chore.days_mask);       // bit order survives round-trip
  assert.equal(payload.schedule_kind, chore.schedule_kind);
  assert.equal(payload.assign_kind, chore.assign_kind);
  assert.deepEqual(payload.rotation_order, chore.rotation_order);
});

test('attemptTodo: {ok:true} on success, {ok:false, error} on failure, sends JSON body', async () => {
  const origFetch = sandbox.fetch;
  try {
    const calls = [];
    sandbox.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    // spread: attemptTodo's return object is created inside the vm sandbox
    // realm, so deepEqual against a same-realm literal needs its own-props
    // copied out first (same pattern as panelFit/shiftMonth above).
    assert.deepEqual({ ...await sandbox.attemptTodo('/api/todos', 'POST',
      { title: 'x', bucket: 'now' }) }, { ok: true });
    assert.equal(calls[0].url, '/api/todos');
    assert.equal(calls[0].opts.method, 'POST');
    assert.equal(calls[0].opts.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(calls[0].opts.body), { title: 'x', bucket: 'now' });

    // no body: no content-type header, no body field
    assert.deepEqual({ ...await sandbox.attemptTodo('/api/todos/1/complete', 'POST') }, { ok: true });
    assert.equal(calls[1].opts.headers, undefined);
    assert.equal(calls[1].opts.body, undefined);

    // failure: j() surfaces the server's detail string as e.message
    sandbox.fetch = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'unknown todo' }) });
    const r = await sandbox.attemptTodo('/api/todos/1/complete', 'DELETE');
    assert.equal(r.ok, false);
    assert.equal(r.error, 'unknown todo');
  } finally {
    if (origFetch === undefined) delete sandbox.fetch;
    else sandbox.fetch = origFetch;
  }
});

test('todoFailMessage: distinguishes a concurrent-edit 404 from a generic failure', () => {
  assert.equal(sandbox.todoFailMessage('unknown todo'),
    'That item was already changed on another device.');
  assert.equal(sandbox.todoFailMessage('/api/todos/1 -> HTTP 500'),
    'Couldn’t save — check the hub and tap again.');
});

test('safeColor: passes hex/keywords through, neutralizes CSS-injection attempts', () => {
  // legitimate values survive unchanged (so inline style="" output is identical)
  assert.equal(sandbox.safeColor('#5BC9F0'), '#5BC9F0');
  assert.equal(sandbox.safeColor('#abc'), '#abc');
  assert.equal(sandbox.safeColor('red'), 'red');
  // a value carrying extra CSS declarations (`;`/`:`) can't reach the attribute
  assert.equal(sandbox.safeColor('red;background:url(x)'), 'transparent');
  assert.equal(sandbox.safeColor('#fff;position:fixed'), 'transparent');
  assert.equal(sandbox.safeColor(''), 'transparent');
  assert.equal(sandbox.safeColor(null), 'transparent');
});
