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
  fmtTimeRange, monthName, eventColor, wallZoom,
  isDayOutsideWindow,
  caldavTestMessage, caldavPanelHtml, caldavCollectionsHtml,
} = sandbox;
const panelFit = (...a) => ({ ...sandbox.panelFit(...a) });
const monthGrid = (...a) => JSON.parse(JSON.stringify(sandbox.monthGrid(...a)));
const shiftMonth = (...a) => ({ ...sandbox.shiftMonth(...a) });

test('addDays crosses months and years', () => {
  assert.equal(sandbox.addDays('2026-08-31', 1), '2026-09-01');
  assert.equal(sandbox.addDays('2026-01-01', -1), '2025-12-31');
});

test('expandDays: multi-day all-day covers every day (end exclusive)', () => {
  const span = { all_day: 1, start_ts: '2026-08-01', end_ts: '2026-08-04' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(span))),
    ['2026-08-01', '2026-08-02', '2026-08-03']);
  const single = { all_day: 1, start_ts: '2026-08-01', end_ts: '2026-08-02' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(single))), ['2026-08-01']);
});

test('expandDays: timed events cover start through end day, so in-progress ones stay visible', () => {
  const sameDay = { all_day: 0, start_ts: '2026-08-13T10:00:00-07:00', end_ts: '2026-08-13T11:00:00-07:00' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(sameDay))), ['2026-08-13']);
  // an overnighter occupies both days (this is what keeps a still-running
  // event on today's feed even though it started yesterday)
  const overnight = { all_day: 0, start_ts: '2026-08-12T22:00:00-07:00', end_ts: '2026-08-13T06:00:00-07:00' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(overnight))),
    ['2026-08-12', '2026-08-13']);
  // ...but an end at exactly midnight belongs to the previous day only
  const tillMidnight = { all_day: 0, start_ts: '2026-08-12T20:00:00-07:00', end_ts: '2026-08-13T00:00:00-07:00' };
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.expandDays(tillMidnight))), ['2026-08-12']);
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

test('isDayOutsideWindow: inclusive bounds, one day past either edge flips it', () => {
  const win = { from: '2026-07-02', to: '2026-09-13' };
  assert.equal(isDayOutsideWindow('2026-07-02', win), false, 'the lower bound itself is inside');
  assert.equal(isDayOutsideWindow('2026-09-13', win), false, 'the upper bound itself is inside');
  assert.equal(isDayOutsideWindow('2026-08-15', win), false, 'comfortably inside');
  assert.equal(isDayOutsideWindow('2026-07-01', win), true, 'one day before the window');
  assert.equal(isDayOutsideWindow('2026-09-14', win), true, 'one day after the window');
});

test('isDayOutsideWindow: fails open when the window is missing or malformed', () => {
  // A payload from before /api/calendar carried `window`, or one that dropped
  // it on a downgraded-status error path, must never mark a day unsynced.
  assert.equal(isDayOutsideWindow('2026-08-01', undefined), false, 'no window at all');
  assert.equal(isDayOutsideWindow('2026-08-01', null), false, 'null window');
  assert.equal(isDayOutsideWindow('2026-08-01', {}), false, 'window missing from/to');
  assert.equal(isDayOutsideWindow('2026-08-01', { from: '2026-07-01' }), false, 'window missing to');
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

test('wallZoom stays 1:1 on the exact-1920 Pi kiosk', () => {
  assert.equal(wallZoom(1920), '');   // 1920/1920 = 1 -> unset
});

test('wallZoom scales a narrower screen down to fill the width', () => {
  assert.equal(wallZoom(1440), '0.75');   // 1440/1920
  assert.equal(wallZoom(1536), '0.8');    // 1536/1920
});

test('wallZoom never upscales a wider screen (stays crisp 1:1)', () => {
  assert.equal(wallZoom(2560), '');
});

test('wallZoom leaves the mobile reflow (<=1000px) alone', () => {
  assert.equal(wallZoom(1000), '');       // at the breakpoint: mobile owns it
  assert.notEqual(wallZoom(1001), '');    // just above: the wall scales
});

test('wallZoom never collapses to zoom:0 on a zero/undefined viewport', () => {
  // a background/just-created tab measures 0; a raw ratio would set zoom:"0"
  assert.equal(wallZoom(0), '');
  assert.equal(wallZoom(undefined), '');
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
    assign_kind: 'fixed', fixed_person_id: 3, rotation_order: [], date: null,
  });
});

test('buildChorePayload: one-time chore carries its date and forces fixed', () => {
  const p = JSON.parse(JSON.stringify(sandbox.buildChorePayload({
    title: 'Return library books', icon: '📚', repeat: 'once',
    days: new Set([1, 3]), assign: 'rotation', person: '4', rot: [5, 6],
    date: '2026-08-20',
  })));
  assert.deepEqual(p, {
    title: 'Return library books', icon: '📚', schedule_kind: 'once',
    days_mask: 0,                       // a one-time chore never carries a weekly mask
    assign_kind: 'fixed',              // rotation is coerced away
    fixed_person_id: 4, rotation_order: [], date: '2026-08-20',
  });
});

test('choreToModel: one-time chore maps rotation_epoch back to the date field', () => {
  const m = sandbox.choreToModel({
    title: 'Pay dues', icon: '', schedule_kind: 'once', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 2, rotation_order: [],
    rotation_epoch: '2026-09-01',
  });
  assert.equal(m.repeat, 'once');
  assert.equal(m.date, '2026-09-01');
});

test('choreToModel: daily chore has an empty date (rotation_epoch is not a due date)', () => {
  const m = sandbox.choreToModel({
    title: 'Bed', icon: '', schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [],
    rotation_epoch: '2026-08-01',
  });
  assert.equal(m.repeat, 'daily');
  assert.equal(m.date, '');
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

test('calStatusMessage: revoked token asks to reconnect', () => {
  const m = sandbox.calStatusMessage({ ok: false, needs_auth: true });
  assert.match(m, /sign-in expired/);
  assert.match(m, /reconnect it in settings/);
});

test('calStatusMessage: not-configured vs generic error', () => {
  assert.match(sandbox.calStatusMessage({ ok: false, error: 'not configured' }), /connected yet/);
  assert.match(sandbox.calStatusMessage({ ok: false, error: 'quota exceeded' }), /hit a snag/);
});

test('calStatusMessage: copy is source-neutral (no "Google" — the hub has several sources)', () => {
  for (const st of [{ ok: false, needs_auth: true },
                    { ok: false, error: 'not configured' },
                    { ok: false, error: 'quota exceeded' }]) {
    assert.doesNotMatch(sandbox.calStatusMessage(st), /google/i);
  }
});

test('calStatusMessage: needs_auth wins over a not-configured error string', () => {
  const m = sandbox.calStatusMessage({ ok: false, needs_auth: true, error: 'not configured' });
  assert.match(m, /sign-in expired/);
});

test('caldavTestMessage: a successful test reports events + reminders counts', () => {
  assert.equal(caldavTestMessage({ ok: true, events: 12, reminders: 3 }),
    'Connected - 12 events, 3 reminders.');
});

test('caldavTestMessage: singular event/reminder counts drop the trailing s', () => {
  assert.equal(caldavTestMessage({ ok: true, events: 1, reminders: 1 }),
    'Connected - 1 event, 1 reminder.');
});

test('caldavTestMessage: a missing events/reminders count on ok:true reads as zero, not NaN', () => {
  assert.equal(caldavTestMessage({ ok: true }), 'Connected - 0 events, 0 reminders.');
});

test('caldavTestMessage: needs_auth asks to check the app-specific password', () => {
  assert.match(caldavTestMessage({ needs_auth: true }), /Sign-in rejected/);
  assert.match(caldavTestMessage({ needs_auth: true }), /app-specific password/);
});

test('caldavTestMessage: needs_auth wins over an error string on the same result', () => {
  const m = caldavTestMessage({ ok: false, needs_auth: true, error: 'HTTP 401' });
  assert.match(m, /Sign-in rejected/);
});

test('caldavTestMessage: a plain failure shows the server\'s error text', () => {
  assert.equal(caldavTestMessage({ ok: false, error: 'no credentials' }), 'no credentials');
});

test('caldavTestMessage: a failure with no error text falls back to a generic message', () => {
  assert.match(caldavTestMessage({ ok: false }), /Couldn.t connect/);
  assert.match(caldavTestMessage(null), /Couldn.t connect/);
});

test('caldavPanelHtml: not connected shows a credential form with a MASKED password field', () => {
  const html = caldavPanelHtml(null, {});
  assert.match(html, /id="caldav-user-input"/);
  assert.match(html, /id="caldav-pw-input"/);
  assert.match(html, /type="password"/);   // the hard security requirement: masked input
  assert.match(html, /data-caldav-connect/);
  assert.match(html, />Connect</);
  assert.match(html, /app-specific password/);
  assert.match(html, /Stored on this device only, never shared/);
  // never a plaintext password field
  assert.doesNotMatch(html, /type="text"[^>]*id="caldav-pw-input"/);
});

test('caldavPanelHtml: not connected + connecting disables the inputs and shows progress text', () => {
  const html = caldavPanelHtml(null, { connecting: true });
  assert.match(html, />Connecting…</);
  assert.match(html, /id="caldav-user-input"[^>]*disabled/);
  assert.match(html, /id="caldav-pw-input"[^>]*disabled/);
  assert.match(html, /data-caldav-connect[^>]*disabled/);
});

test('caldavPanelHtml: not connected + a form error shows it inline', () => {
  const html = caldavPanelHtml(null, { formError: 'Enter both fields.' });
  assert.match(html, /form-error">Enter both fields\.</);
});

test('caldavPanelHtml: never renders a password value anywhere, on any branch', () => {
  // caldavPanelHtml's signature has no password parameter at all — this pins
  // that a hypothetical caller mistake (e.g. stashing it on `ui`) still can't
  // leak it into the DOM, since the function only ever emits its own fixed
  // strings plus `integ`/`ui` fields it actually reads (id, account, status,
  // etc — never anything password-shaped).
  const ui = { formError: '', connecting: false, testing: false, testResult: null,
    password: 'hunter2', app_password: 'hunter2' };
  assert.doesNotMatch(caldavPanelHtml(null, ui), /hunter2/);
  assert.doesNotMatch(
    caldavPanelHtml({ id: 'icloud_caldav', account: 'a@example.com', enabled: true }, ui),
    /hunter2/);
});

test('caldavPanelHtml: connected shows the account, NO password field, and no SECOND enable switch', () => {
  // The enable switch for icloud_caldav already lives one section up, in the
  // generic Integrations list (#integrations-ctl, renderIntegrations), wired
  // straight through poll(), which this panel deliberately is not (see
  // renderCaldavPanel's comment). A second switch HERE would go stale after
  // a tap (nothing repaints #caldav-panel on its own) and could disagree with
  // the first one: two controls for one boolean, silently able to drift out
  // of sync. Pins that this panel carries none of that switch's markup.
  const html = caldavPanelHtml(
    { id: 'icloud_caldav', account: 'bot@example.com', enabled: true, readonly: true },
    {});
  assert.match(html, /Connected as <strong>bot@example\.com<\/strong>/);
  assert.doesNotMatch(html, /data-caldav-enable-toggle/);
  assert.doesNotMatch(html, /integ-switch/);
  assert.doesNotMatch(html, /id="caldav-pw-input"/);
  assert.doesNotMatch(html, /type="password"/);
});

test('caldavPanelHtml: connected escapes the account (XSS-safe interpolation)', () => {
  const html = caldavPanelHtml(
    { id: 'icloud_caldav', account: '<img src=x onerror=alert(1)>', enabled: true }, {});
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test('caldavPanelHtml: readonly defaults to 1-way and the seg-btn reflects it', () => {
  const oneWay = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, readonly: true }, {});
  assert.match(oneWay, /seg-btn active" type="button" data-caldav-readonly="1"/);
  const twoWay = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, readonly: false }, {});
  assert.match(twoWay, /seg-btn active" type="button" data-caldav-readonly="0"/);
  assert.match(twoWay, /2-way \(write back\)/);   // two-way is live now, not "coming soon"
});

test('caldavPanelHtml: connected + needs_auth/error status shows the reconnect/error warning', () => {
  const reconnect = caldavPanelHtml(
    { id: 'icloud_caldav', account: 'a@b.com', enabled: true, status: 'needs_auth' }, {});
  assert.match(reconnect, /integ-warn">reconnect</);
  const errored = caldavPanelHtml(
    { id: 'icloud_caldav', account: 'a@b.com', enabled: true, status: 'error' }, {});
  assert.match(errored, /integ-warn">error</);
});

test('caldavPanelHtml: an outbox backlog shows a quiet "not yet synced" note; 0/absent shows nothing', () => {
  const none = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true }, {});
  assert.doesNotMatch(none, /not yet synced/, 'no note when pending is absent');
  const zero = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, pending: 0 }, {});
  assert.doesNotMatch(zero, /not yet synced/, 'no note when pending is 0');
  const one = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, pending: 1 }, {});
  assert.match(one, /caldav-pending">1 change not yet synced/, 'singular copy');
  const many = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, pending: 3 }, {});
  assert.match(many, /caldav-pending">3 changes not yet synced/, 'plural copy');
});

test('caldavPanelHtml: connected + testing shows progress text and disables the Test button', () => {
  const html = caldavPanelHtml(
    { id: 'icloud_caldav', account: 'a@b.com', enabled: true }, { testing: true });
  assert.match(html, /data-caldav-test[^>]*disabled/);
  assert.match(html, />Testing…</);
});

test('caldavPanelHtml: connected + a test result shows the formatted message with an ok/err class', () => {
  const ok = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true },
    { testResult: { ok: true, events: 4, reminders: 2 } });
  assert.match(ok, /caldav-test-result ok">Connected - 4 events, 2 reminders\./);
  const err = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true },
    { testResult: { ok: false, error: 'boom' } });
  assert.match(err, /caldav-test-result err">boom</);
});

test('caldavCollectionsHtml: empty/null/undefined all show the same "try Test connection" empty state', () => {
  assert.match(caldavCollectionsHtml([]), /integ-empty">No calendars found yet - try Test connection</);
  assert.match(caldavCollectionsHtml(null), /integ-empty/);
  assert.match(caldavCollectionsHtml(undefined), /integ-empty/);
});

test('caldavCollectionsHtml: a VEVENT row labels "Calendar", a VTODO row labels "Reminders"', () => {
  const html = caldavCollectionsHtml([
    { id: 'caldav:ev1', name: 'Family', color: null, comp_type: 'VEVENT', enabled: true },
    { id: 'caldav:rm1', name: 'Groceries', color: null, comp_type: 'VTODO', enabled: true },
  ]);
  assert.match(html, /Family<span class="caldav-cal-kind">Calendar<\/span>/);
  assert.match(html, /Groceries<span class="caldav-cal-kind">Reminders<\/span>/);
});

test('caldavCollectionsHtml: the switch reflects each row\'s own enabled state', () => {
  const html = caldavCollectionsHtml([
    { id: 'a', name: 'On', color: null, comp_type: 'VEVENT', enabled: true },
    { id: 'b', name: 'Off', color: null, comp_type: 'VEVENT', enabled: false },
  ]);
  assert.match(html, /aria-checked="true"[^>]*data-caldav-collection-toggle="a"/s);
  assert.match(html, /On<span class="caldav-cal-kind">Calendar<\/span><\/span>\s*<span class="integ-switch on"/);
  assert.match(html, /aria-checked="false"[^>]*data-caldav-collection-toggle="b"/s);
  assert.match(html, /Off<span class="caldav-cal-kind">Calendar<\/span><\/span>\s*<span class="integ-switch" aria-hidden/,
    'no " on" class when disabled');
});

test('caldavCollectionsHtml: each row carries its id for the click handler', () => {
  const html = caldavCollectionsHtml([
    { id: 'caldav:ab12', name: 'Family', color: null, comp_type: 'VEVENT', enabled: true },
  ]);
  assert.match(html, /data-caldav-collection-toggle="caldav:ab12"/);
});

test('caldavCollectionsHtml: a non-null hex color renders a swatch dot; a null color omits it', () => {
  const withColor = caldavCollectionsHtml([
    { id: 'a', name: 'Family', color: '#ff8800', comp_type: 'VEVENT', enabled: true },
  ]);
  assert.match(withColor, /caldav-cal-dot" style="background:#ff8800"/);
  const noColor = caldavCollectionsHtml([
    { id: 'b', name: 'Family', color: null, comp_type: 'VEVENT', enabled: true },
  ]);
  assert.doesNotMatch(noColor, /caldav-cal-dot/);
});

test('caldavCollectionsHtml: a name with an emoji and a `<` is escaped (XSS-safe)', () => {
  const html = caldavCollectionsHtml([
    { id: 'a', name: 'Reminders ⚠️ <script>alert(1)</script>', color: null, comp_type: 'VTODO', enabled: true },
  ]);
  assert.match(html, /Reminders ⚠️ &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('caldavPanelHtml: connected embeds the Calendars section with its caption and the collections list', () => {
  const html = caldavPanelHtml(
    { id: 'icloud_caldav', account: 'a@b.com', enabled: true },
    { collections: [{ id: 'caldav:ab12', name: 'Family', color: null, comp_type: 'VEVENT', enabled: true }] });
  assert.match(html, /<div class="settings-k">Calendars<\/div>/);
  assert.match(html, /Pick which iCloud calendars and reminder lists show on the wall\./);
  assert.match(html, /data-caldav-collection-toggle="caldav:ab12"/);
});

test('caldavPanelHtml: not connected never renders the Calendars section', () => {
  const html = caldavPanelHtml(null, { collections: [{ id: 'a', name: 'X', comp_type: 'VEVENT', enabled: true }] });
  assert.doesNotMatch(html, /caldav-collections/);
  assert.doesNotMatch(html, /Calendars</);
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

test('reminderFailMessage: read-only / not-connected / dead-list / already-changed / generic each get their own copy', () => {
  assert.match(sandbox.reminderFailMessage('iCloud reminders are read-only (enable two-way in settings)'),
    /2-way in Settings/);
  assert.match(sandbox.reminderFailMessage('iCloud is not connected'), /isn’t connected/);
  // A dead-list add 404 ('unknown reminder list') must NOT fall into the
  // 'unknown reminder' edit-collision branch (the former contains the latter).
  assert.equal(sandbox.reminderFailMessage('unknown reminder list'),
    'That list is no longer available — pick another in Settings.');
  assert.equal(sandbox.reminderFailMessage('unknown reminder'),
    'That reminder was already changed on another device.');
  assert.equal(sandbox.reminderFailMessage('/api/reminders/toggle -> HTTP 500'),
    'Couldn’t save — check the hub and tap again.');
});

test('j: aborts a hung request when the timeout fires, so callers get a rejection', async () => {
  // The vm sandbox has no AbortController by default (so the timeout self-disables
  // in the other tests). Inject a real one + a capturable timer to drive the abort.
  const orig = { fetch: sandbox.fetch, AC: sandbox.AbortController,
    st: sandbox.setTimeout, ct: sandbox.clearTimeout };
  let fireTimeout = null;
  sandbox.AbortController = AbortController;
  sandbox.setTimeout = (fn) => { fireTimeout = fn; return 1; };
  sandbox.clearTimeout = () => {};
  let aborted = false;
  sandbox.fetch = (url, opts) => new Promise((_res, rej) => {
    opts.signal.addEventListener('abort', () => { aborted = true; rej(new Error('aborted')); });
  });
  try {
    const p = sandbox.j('/api/hub');
    assert.equal(typeof fireTimeout, 'function', 'a timeout was armed');
    fireTimeout();                       // simulate J_TIMEOUT_MS elapsing
    await assert.rejects(p);             // the hang becomes a rejection
    assert.ok(aborted, 'the fetch signal was aborted');
  } finally {
    sandbox.fetch = orig.fetch; sandbox.AbortController = orig.AC;
    sandbox.setTimeout = orig.st; sandbox.clearTimeout = orig.ct;
  }
});

test('j: preserves a caller-supplied AbortSignal and arms no internal timeout', async () => {
  const orig = { fetch: sandbox.fetch, AC: sandbox.AbortController, st: sandbox.setTimeout };
  let armed = false;
  sandbox.AbortController = AbortController;
  sandbox.setTimeout = () => { armed = true; return 1; };
  const ac = new AbortController();
  sandbox.fetch = async (url, opts) => {
    assert.equal(opts.signal, ac.signal, 'the caller signal is passed straight through');
    return { ok: true, status: 200, json: async () => ({ ok: 1 }) };
  };
  try {
    await sandbox.j('/x', { signal: ac.signal });
    assert.equal(armed, false, 'no internal timeout when the caller owns the signal');
  } finally {
    sandbox.fetch = orig.fetch; sandbox.AbortController = orig.AC; sandbox.setTimeout = orig.st;
  }
});

test('fetchTimeout: aborts a hung request when the timeout fires (shares j()\'s guard)', async () => {
  // Same drive-by-hand pattern as the j() timeout test above: inject a real
  // AbortController + a capturable timer so the abort is provable, not assumed.
  const orig = { fetch: sandbox.fetch, AC: sandbox.AbortController,
    st: sandbox.setTimeout, ct: sandbox.clearTimeout };
  let fireTimeout = null;
  sandbox.AbortController = AbortController;
  sandbox.setTimeout = (fn) => { fireTimeout = fn; return 1; };
  sandbox.clearTimeout = () => {};
  let aborted = false;
  sandbox.fetch = (url, opts) => new Promise((_res, rej) => {
    opts.signal.addEventListener('abort', () => { aborted = true; rej(new Error('aborted')); });
  });
  try {
    const p = sandbox.fetchTimeout('/api/tiles/camera.jpg?src=drive&probe=1');
    assert.equal(typeof fireTimeout, 'function', 'a timeout was armed');
    fireTimeout();                       // simulate the timeout elapsing
    await assert.rejects(p);             // the hang becomes a rejection, not a forever-pending promise
    assert.ok(aborted, 'the fetch signal was aborted');
  } finally {
    sandbox.fetch = orig.fetch; sandbox.AbortController = orig.AC;
    sandbox.setTimeout = orig.st; sandbox.clearTimeout = orig.ct;
  }
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
