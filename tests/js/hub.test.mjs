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
  backupBadge,
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
  // Settings holds How it works: long enough to read a topic
  assert.equal(idleReturnMs('settings'), 300000);
});

test('nightClass covers 22:00–05:59', () => {
  assert.equal(nightClass(21), '');
  assert.equal(nightClass(22), 'is-night');
  assert.equal(nightClass(5), 'is-night');
  assert.equal(nightClass(6), '');
});

// --- dim hours and away dimming (common.js; the server side is dimming.py) ---

const night = { on: true, start: '22:00', end: '06:00' };
const hub = (dim) => ({ dim });

test('nightClass follows the dim hours, minutes included', () => {
  const nc = sandbox.nightClass;
  const late = { on: true, start: '21:30', end: '07:15' };
  assert.equal(nc(21, 29, late), '');
  assert.equal(nc(21, 30, late), 'is-night');
  assert.equal(nc(7, 14, late), 'is-night');
  assert.equal(nc(7, 15, late), '');
});

test('inDimWindow: a window across midnight, one inside a day, and Off', () => {
  const w = sandbox.inDimWindow;
  const m = (h, mm = 0) => h * 60 + mm;
  assert.ok(w(m(22), night) && w(m(23, 59), night) && w(m(0), night) && w(m(5, 59), night));
  assert.ok(!w(m(6), night) && !w(m(12), night) && !w(m(21, 59), night));
  const nap = { on: true, start: '13:00', end: '15:30' };
  assert.ok(w(m(13), nap) && w(m(15, 29), nap));
  assert.ok(!w(m(15, 30), nap) && !w(m(0, 30), nap) && !w(m(12, 59), nap));
  assert.ok(!w(m(23), { ...night, on: false }), 'Off never dims for the time of day');
  assert.ok(!w(m(23), { on: true, start: '22:00', end: '22:00' }), 'an empty window never dims');
});

test('dimHoursOf reads the hub payload and falls back to 10pm to 6am', () => {
  const d = (x) => ({ ...sandbox.dimHoursOf(x) });
  assert.deepEqual(d(null), night, 'before the first poll: the old fixed hours');
  assert.deepEqual(d({}), night, 'an older hub with no dim block');
  assert.deepEqual(d(hub({ hours: { on: false, start: '21:00', end: '07:00' } })),
    { on: false, start: '21:00', end: '07:00' });
  for (const bad of [{ on: 'yes', start: '21:00', end: '07:00' },
    { on: true, start: '9pm', end: '07:00' }, { on: true, start: '07:00', end: '07:00' }]) {
    assert.deepEqual(d(hub({ hours: bad })), night, JSON.stringify(bad));
  }
});

test('wallDimmed: night by the clock, or nobody home on the wall only', () => {
  const at = (h, mm = 0) => new Date(2026, 10, 27, h, mm);
  const wd = sandbox.wallDimmed;
  assert.equal(wd(at(23), null, false), true, 'no payload yet: the old 10pm rule');
  assert.equal(wd(at(12), null, false), false);
  const off = hub({ hours: { ...night, on: false }, away: null });
  assert.equal(wd(at(23), off, false), false, 'dim hours Off');
  const empty = hub({ hours: night, away: { enabled: true, state: 'away', home: 0, dim: true } });
  assert.equal(wd(at(12), empty, false, at(12).getTime()), true, 'nobody home dims the wall at noon');
  assert.equal(wd(at(12), empty, true, at(12).getTime()), false, 'a phone is in someone\'s hand away from home: no away dim');
  assert.equal(wd(at(23), empty, true, at(23).getTime()), true, 'the phone still dims at night');
  const home = hub({ hours: night, away: { enabled: true, state: 'home', home: 2, dim: false } });
  assert.equal(wd(at(12), home, false), false);
  const unread = hub({ hours: night, away: { enabled: true, state: 'unreadable', home: null, dim: false } });
  assert.equal(wd(at(12), unread, false), false, 'Home Assistant unreadable never dims');
  const switchedOff = hub({ hours: night, away: { enabled: false, state: 'off', home: null, dim: true } });
  assert.equal(wd(at(12), switchedOff, false, at(12).getTime()), false, 'a stray dim flag with the switch off is ignored');
});

test('wallDimmed: an old nobody-home answer stops dimming; night still works offline', () => {
  const at = (h, mm = 0, ss = 0) => new Date(2026, 10, 27, h, mm, ss);
  const wd = sandbox.wallDimmed;
  const empty = hub({ hours: night, away: { enabled: true, state: 'away', home: 0, dim: true } });
  const got = at(12).getTime();
  assert.equal(wd(at(12, 3, 30), empty, false, got), true, '210 s later it still counts');
  assert.equal(wd(at(12, 3, 31), empty, false, got), false,
    'older than the server\'s own 210 s: the hub may be gone, so the wall comes back up');
  assert.equal(wd(at(12), empty, false, null), false, 'no arrival time: never trusted');
  assert.equal(wd(at(23), empty, false, got), true, 'the dim hours run from the local clock');
});

test('dimHoursWords says the window in plain clock words', () => {
  const w = sandbox.dimHoursWords;
  assert.equal(w(night), '10pm to 6am');
  assert.equal(w({ on: true, start: '21:30', end: '07:15' }), '9:30pm to 7:15am');
  assert.equal(w({ on: true, start: '00:00', end: '12:00' }), 'midnight to noon');
  assert.equal(w({ on: true, start: '12:30', end: '13:00' }), '12:30pm to 1pm');
  assert.equal(w({ ...night, on: false }), '');
});

test('dimAwayLine: one plain sentence per state', () => {
  const l = sandbox.dimAwayLine;
  assert.equal(l(null), '', 'no Home Assistant: nothing to say');
  assert.match(l({ enabled: false, state: 'off' }), /Dim when nobody is home/);
  assert.match(l({ enabled: true, state: 'checking' }), /Asking Home Assistant/);
  assert.match(l({ enabled: true, state: 'away', home: 0, dim: true }), /nobody is home/);
  assert.match(l({ enabled: true, state: 'home', home: 1 }), /1 person is home/);
  assert.match(l({ enabled: true, state: 'home', home: 3 }), /3 people are home/);
  assert.match(l({ enabled: true, state: 'unreadable' }), /couldn.t be read/);
  assert.match(l({ enabled: true, state: 'unreadable' }), /won.t dim/);
  assert.match(l({ enabled: true, state: 'needs_auth' }), /HA_TOKEN/);
  assert.match(l({ enabled: true, state: 'confirming', home: 0 }), /nobody is home.*once more/);
  const stopped = l({ enabled: true, state: 'stopped' });
  assert.match(stopped, /hub stopped checking/);
  assert.doesNotMatch(stopped, /Home Assistant couldn/, 'never blames Home Assistant for the hub');
  for (const st of ['off', 'checking', 'away', 'home', 'unreadable', 'needs_auth', 'confirming', 'stopped']) {
    const text = l({ enabled: st !== 'off', state: st, home: 0 });
    assert.ok(!text.includes(String.fromCharCode(0x2014)), `${st}: no long dash`);
  }
});

test('dimTimeOptions: every half hour, the saved time selected, an off-grid one kept', () => {
  const o = sandbox.dimTimeOptions;
  const html = o('22:00');
  assert.equal((html.match(/<option /g) || []).length, 48);
  assert.match(html, /<option value="22:00" selected>10pm<\/option>/);
  assert.match(html, /<option value="00:00">midnight<\/option>/);
  const odd = o('21:45');
  assert.equal((odd.match(/<option /g) || []).length, 49);
  assert.match(odd, /<option value="21:30">9:30pm<\/option><option value="21:45" selected>9:45pm<\/option><option value="22:00">/);
});

test('dimCardHtml: the On/Off switch, both times, and the away line only with Home Assistant', () => {
  const html = sandbox.dimCardHtml(hub({ hours: night, away: null }));
  assert.match(html, /data-dim-set="on" class="on"/);
  assert.match(html, /data-dim-set="off"/);
  assert.match(html, /data-dim-start/);
  assert.match(html, /data-dim-end/);
  assert.match(html, /Every screen in the house dims from 10pm to 6am/);
  assert.doesNotMatch(html, /dim-away/, 'no Home Assistant: no away line');
  const offHtml = sandbox.dimCardHtml(hub({ hours: { ...night, on: false }, away: null }));
  assert.match(offHtml, /data-dim-set="off" class="on"/);
  assert.match(offHtml, /<select[^>]*data-dim-start[^>]*disabled/, 'times are kept but locked while Off');
  assert.match(offHtml, /No screen dims for the time of day/);
  const withHa = sandbox.dimCardHtml(hub({ hours: night,
    away: { enabled: true, state: 'unreadable', home: null, dim: false } }));
  assert.match(withHa, /class="settings-sub dim-away"/);
  assert.match(withHa, /couldn&#39;t be read|couldn’t be read/);
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
    week_interval: 1, interval_days: null, due_times: [],
    assign_kind: 'fixed', fixed_person_id: 3, rotation_order: [], rotate_every: 'turn', date: null,
    kind: 'house',
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
    week_interval: 1, interval_days: null, due_times: [],
    assign_kind: 'fixed',              // rotation is coerced away
    fixed_person_id: 4, rotation_order: [], rotate_every: 'turn', date: '2026-08-20',
    kind: 'house',
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

// --- routine types added in P1: biweekly (weekly + week_interval 2),
// every-N-days (schedule_kind interval + interval_days), and reminder times
// (due_times). Each must round-trip to the right shape and keep the other
// kinds' fields clean.

test('buildChorePayload: biweekly is schedule_kind days with week_interval 2', () => {
  const p = sandbox.buildChorePayload({
    title: 'Bins', icon: '', repeat: 'weekly', days: new Set([2]),   // Wed
    weekInterval: 2, assign: 'fixed', person: '1', rot: [],
  });
  assert.equal(p.schedule_kind, 'days');
  assert.equal(p.days_mask, 4);              // bit 2
  assert.equal(p.week_interval, 2);          // biweekly
  assert.equal(p.interval_days, null);       // not an interval chore
});

test('buildChorePayload: plain weekly carries week_interval 1', () => {
  const p = sandbox.buildChorePayload({
    title: 'Bins', icon: '', repeat: 'weekly', days: new Set([2]),
    weekInterval: 1, assign: 'fixed', person: '1', rot: [],
  });
  assert.equal(p.week_interval, 1);
});

test('buildChorePayload: every-N-days is schedule_kind interval + interval_days', () => {
  const p = sandbox.buildChorePayload({
    title: 'Water plants', icon: '🪴', repeat: 'interval', intervalDays: '3',
    days: new Set([1, 2]),                    // ignored off a weekly kind
    weekInterval: 2,                          // ignored off a weekly kind
    assign: 'fixed', person: '5', rot: [],
  });
  assert.equal(p.schedule_kind, 'interval');
  assert.equal(p.interval_days, 3);
  assert.equal(p.days_mask, 0);              // no weekly mask on an interval chore
  assert.equal(p.week_interval, 1);          // neutral off the weekly kind
});

test('buildChorePayload: interval clamps out-of-range and rejects empty', () => {
  const over = sandbox.buildChorePayload({
    title: 'x', icon: '', repeat: 'interval', intervalDays: '400',
    days: new Set(), assign: 'fixed', person: '1', rot: [],
  });
  assert.equal(over.interval_days, 365);     // clamped to the max
  const empty = sandbox.buildChorePayload({
    title: 'x', icon: '', repeat: 'interval', intervalDays: '',
    days: new Set(), assign: 'fixed', person: '1', rot: [],
  });
  assert.equal(empty.interval_days, null);   // empty -> null (server rejects; UI guards first)
});

test('buildChorePayload: due_times de-dupe, sort, drop junk, cap at 6 — any kind', () => {
  const p = sandbox.buildChorePayload({
    title: 'x', icon: '', repeat: 'daily', days: new Set(),
    assign: 'fixed', person: '1', rot: [],
    times: ['20:00', '07:30', '20:00', '7:30', '24:00', 'nope', '  08:15  '],
  });
  // '7:30' (unpadded), '24:00' (out of range) and 'nope' are dropped; the
  // zero-padded '07:30' survives; '20:00' de-duped; '  08:15  ' trimmed;
  // result sorted ascending. (spread: sandbox-realm array -> test realm)
  assert.deepEqual([...p.due_times], ['07:30', '08:15', '20:00']);
});

test('buildChorePayload: due_times caps at 6 in ascending order', () => {
  const p = sandbox.buildChorePayload({
    title: 'x', icon: '', repeat: 'daily', days: new Set(),
    assign: 'fixed', person: '1', rot: [],
    times: ['09:00', '08:00', '07:00', '06:00', '05:00', '04:00', '03:00'],
  });
  assert.deepEqual([...p.due_times], ['03:00', '04:00', '05:00', '06:00', '07:00', '08:00']);
});

test('choreToModel: an interval chore maps back to repeat interval + intervalDays', () => {
  const m = sandbox.choreToModel({
    title: 'Water plants', icon: '🪴', schedule_kind: 'interval', days_mask: 0,
    interval_days: 3, week_interval: 1, due_times: ['08:00'],
    assign_kind: 'fixed', fixed_person_id: 5, rotation_order: [],
  });
  assert.equal(m.repeat, 'interval');
  assert.equal(m.intervalDays, 3);
  assert.equal(m.weekInterval, 1);
  assert.deepEqual([...m.times], ['08:00']);
});

test('choreToModel: a biweekly chore maps back to weekly + weekInterval 2', () => {
  const m = sandbox.choreToModel({
    title: 'Bins', icon: '', schedule_kind: 'days', days_mask: 4,
    week_interval: 2, interval_days: null, due_times: [],
    assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [],
  });
  assert.equal(m.repeat, 'weekly');
  assert.equal(m.weekInterval, 2);
  assert.ok(m.days.has(2));
});

test('choreToModel: missing due_times/interval fields degrade to []/empty, weekly=1', () => {
  const m = sandbox.choreToModel({
    title: 'Bed', icon: '', schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [],
  });
  assert.deepEqual([...m.times], []);
  assert.equal(m.intervalDays, '');
  assert.equal(m.weekInterval, 1);
});

// --- person -> iCloud list mapping body (the "— none —" option clears to null).

test('reminderListBody: a list id maps through; "" and null clear to null', () => {
  assert.deepEqual({ ...sandbox.reminderListBody('caldav:emma') }, { reminder_list_id: 'caldav:emma' });
  assert.deepEqual({ ...sandbox.reminderListBody('') }, { reminder_list_id: null });
  assert.deepEqual({ ...sandbox.reminderListBody(null) }, { reminder_list_id: null });
  assert.deepEqual({ ...sandbox.reminderListBody(undefined) }, { reminder_list_id: null });
});

// --- chore toggle carries the day the wall is showing.
// Just after midnight the wall still shows yesterday until its next poll. A tap
// with no date landed on the server's NEW day, so the check-off vanished.

test('attemptToggle sends the shown date: POST body and DELETE query', async () => {
  const orig = sandbox.j;
  const calls = [];
  sandbox.j = async (url, opts) => { calls.push([url, opts]); return {}; };
  try {
    assert.equal(await sandbox.attemptToggle(7, false, '2026-09-21'), true);
    assert.equal(calls[0][0], '/api/chores/7/complete');
    assert.equal(calls[0][1].method, 'POST');
    assert.deepEqual(JSON.parse(calls[0][1].body), { date: '2026-09-21' });
    assert.equal(calls[0][1].headers['Content-Type'], 'application/json');
    assert.equal(await sandbox.attemptToggle(7, true, '2026-09-21'), true);
    assert.equal(calls[1][0], '/api/chores/7/complete?date=2026-09-21');
    assert.equal(calls[1][1].method, 'DELETE');
    // no date known yet (before the first poll): fall back to the server's day
    await sandbox.attemptToggle(8, false);
    assert.deepEqual(JSON.parse(calls[2][1].body), {});
    await sandbox.attemptToggle(8, true);
    assert.equal(calls[3][0], '/api/chores/8/complete');
  } finally {
    sandbox.j = orig;
  }
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

test('calStatusMessage: an in-flight first paint says the full calendar is still loading', () => {
  // The seeded payload is ok:true but INCOMPLETE (the home feed's 14-day
  // window), so days outside it show "not synced" that the full fetch will
  // fill in. Without this branch the ok-status early return gives '' and the
  // partial view reads as final, with nothing on screen to say otherwise.
  const m = sandbox.calStatusMessage({ ok: true, loading_full: true });
  assert.match(m, /loading/i);
  assert.notEqual(m, '', 'an ok-but-incomplete payload must not render silently');
});

test('calStatusMessage: a failure with nothing cached never promises cached events', () => {
  // The generic copy says "showing the last events we saw", which is a lie when
  // the first fetch failed and there are none to show.
  const m = sandbox.calStatusMessage({ ok: false, error: 'unreachable', nothing_cached: true });
  assert.match(m, /no events are loaded/i);
  assert.doesNotMatch(m, /last events we saw/i);
});

test('emptyWindow: vouches for nothing — every day, today included, falls outside it', () => {
  const win = sandbox.emptyWindow('2026-09-16');
  assert.equal(win.from, '2026-09-16');
  assert.equal(win.to, '2026-09-15', 'to is the day BEFORE from, so the range is empty');
  for (const day of ['2026-09-16', '2026-09-15', '2026-09-17', '2027-03-01', '2020-01-01']) {
    assert.equal(sandbox.isDayOutsideWindow(day, win), true, `${day} must hatch`);
  }
});

test('failedCalWindow: with a cached payload, keeps events + window and downgrades status', () => {
  const prev = { status: { ok: true }, events: [{ id: 'e1' }],
    window: { from: '2026-09-01', to: '2026-10-01' } };
  const out = sandbox.failedCalWindow(prev, 'boom', '2026-09-16');
  assert.equal(out.events.length, 1, 'cached events survive a failed refresh');
  assert.deepEqual({ ...out.window }, { from: '2026-09-01', to: '2026-10-01' });
  assert.equal(out.status.ok, false, 'a stale ok:true would paint old events as current');
  assert.equal(out.status.error, 'boom');
  assert.ok(!out.status.nothing_cached, 'there IS something cached');
});

test('failedCalWindow: with nothing cached, returns an EMPTY window so no day reads as free', () => {
  const out = sandbox.failedCalWindow(null, 'days/past out of range', '2026-09-16');
  assert.equal(out.events.length, 0);
  assert.equal(out.status.ok, false);
  // the real reason survives — a 422 is an operator bug, not flaky wifi
  assert.equal(out.status.error, 'days/past out of range');
  assert.equal(out.status.nothing_cached, true);
  // every day, INCLUDING today, must fall outside the window and hatch
  assert.equal(sandbox.isDayOutsideWindow('2026-09-16', out.window), true, 'today hatches');
  assert.equal(sandbox.isDayOutsideWindow('2027-03-01', out.window), true, 'next year hatches');
  assert.equal(sandbox.isDayOutsideWindow('2026-01-01', out.window), true, 'the past hatches');
});

test('failedCalWindow: a SECOND consecutive failure still reports nothing cached', () => {
  // fetchCalWindow runs on every calendar open / refresh, so two failures in a
  // row is the NORMAL case while the server is down. Feeding the first failure's
  // own (empty) payload back in must not drop the flag, or the banner goes back
  // to promising "the last events we saw" with zero events to show.
  const first = sandbox.failedCalWindow(null, 'unreachable', '2026-09-16');
  const second = sandbox.failedCalWindow(first, 'unreachable', '2026-09-16');
  assert.equal(second.events.length, 0);
  assert.equal(second.status.nothing_cached, true, 'the flag survives a refresh');
  assert.doesNotMatch(sandbox.calStatusMessage(second.status), /last events we saw/i);
  // and the window stays empty, so days keep hatching
  assert.equal(sandbox.isDayOutsideWindow('2026-09-16', second.window), true);
});

test('failedCalWindow: a missing message still yields a usable error, never undefined', () => {
  assert.equal(sandbox.failedCalWindow(null, '', '2026-09-16').status.error, 'unreachable');
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

test('calStatusMessage: an expired source still warns even when another source is ok', () => {
  // Mixed setup: Google ok, iCloud password revoked -> agg is {ok:true,
  // needs_auth:true}. The reconnect banner must still show; otherwise the iCloud
  // half silently goes stale behind a "connected" wall and nobody reconnects it.
  const m = sandbox.calStatusMessage({ ok: true, needs_auth: true });
  assert.match(m, /sign-in expired/);
  assert.match(m, /reconnect it in settings/);
  // ...but don't claim we're "showing the last events we saw" — the healthy
  // source is fresh, only the expired one is stale.
  assert.doesNotMatch(m, /last events/);
});

test('calStatusMessage: a sustained non-auth failure warns even when another source is ok', () => {
  // agg is {ok:true, degraded:true}: a stuck iCloud (network/5xx, not auth) behind
  // a healthy Google. The wall should say a calendar is having trouble, not stay
  // silent — but it is NOT a reconnect prompt.
  const m = sandbox.calStatusMessage({ ok: true, degraded: true });
  assert.match(m, /trouble syncing/);
  assert.doesNotMatch(m, /sign-in expired/);
});

test('calStatusMessage: needs_auth outranks degraded on the same status', () => {
  const m = sandbox.calStatusMessage({ ok: true, needs_auth: true, degraded: true });
  assert.match(m, /sign-in expired/);       // reconnect is the louder signal
  assert.doesNotMatch(m, /trouble syncing/);
});

test('calStatusMessage: a healthy status shows no banner (no false degraded)', () => {
  assert.equal(sandbox.calStatusMessage({ ok: true }), '');
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

test('caldavPanelHtml: not connected puts the Apple ID draft back, escaped', () => {
  // A redraw (the "enter both" error, a failed connect) used to wipe the typed
  // Apple ID. The draft rides in ui.user; it is escaped like every other sink.
  const html = caldavPanelHtml(null, { user: 'me"><img src=x>@example.com' });
  assert.match(html, /id="caldav-user-input"[^>]*value="me&quot;&gt;&lt;img src=x&gt;@example\.com"/);
  assert.doesNotMatch(html, /<img/);
  assert.match(caldavPanelHtml(null, {}), /id="caldav-user-input"[^>]*value=""/, 'no draft: empty');
  // the password field never takes a value from anywhere
  assert.doesNotMatch(caldavPanelHtml(null, { user: 'x' }), /id="caldav-pw-input"[^>]*value=/);
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

test('caldavPanelHtml: parked wall changes get their own note; 0/absent shows nothing', () => {
  const none = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true }, {});
  assert.doesNotMatch(none, /caldav-parked/, 'no note when parked is absent');
  const zero = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, parked: 0 }, {});
  assert.doesNotMatch(zero, /caldav-parked/, 'no note when parked is 0');
  const one = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, parked: 1 }, {});
  assert.match(one, /caldav-parked">1 change iCloud would not take/, 'singular copy');
  const many = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, parked: 2 }, {});
  assert.match(many, /caldav-parked">2 changes iCloud would not take/, 'plural copy');
  // A change parked because its list is gone is hidden from the wall, so the
  // note must not promise it is "on the wall".
  const note = one.match(/caldav-parked">([^<]*)</)[1];
  assert.match(note, /kept but not sent/, 'says it is kept and not sent');
  assert.doesNotMatch(note, /on the wall/, 'never claims it shows on the wall');
});

test('caldavPanelHtml: a person whose chore list is gone gets a plain line; none shows nothing', () => {
  const none = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true }, {});
  assert.doesNotMatch(none, /caldav-gone/, 'no line when lists_gone is absent');
  const empty = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true, lists_gone: [] }, {});
  assert.doesNotMatch(empty, /caldav-gone/, 'no line when nobody is affected');
  const two = caldavPanelHtml({ id: 'icloud_caldav', account: 'a@b.com', enabled: true,
    lists_gone: ['Sam', '<b>Bo</b>'] }, {});
  assert.match(two, /caldav-gone">Sam’s iCloud chore list is gone; pick a new one\.</, 'one line per person');
  assert.match(two, /caldav-gone">&lt;b&gt;Bo&lt;\/b&gt;’s iCloud chore list/, 'names are escaped');
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
  assert.match(html, /<span class="settings-k">Calendars<\/span><button class="tip-btn"/);
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

test('probeOk: aborts a hung request when the timeout fires (shares j()\'s guard)', async () => {
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
    const p = sandbox.probeOk('/api/tiles/camera.jpg?src=drive&probe=1');
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

// --- comfort/air-quality banding (common.js): out-of-range coloring for the
// House Climate + Weather cards. Pure threshold logic, tested here; the render
// functions that consume it are covered in hub-dom.test.mjs. Bands: 'ok' (in
// range), 'warn' (edge of range), 'crit' (out of range), 'good' (actively
// healthy, for UV/AQI), '' (no reading -> no color).
test('tempBandF: indoor comfort bands + missing readings', () => {
  const b = sandbox.tempBandF;
  assert.equal(b(72), 'ok');            // comfortable
  assert.equal(b(62), 'ok');            // low edge of ok
  assert.equal(b(79), 'ok');            // high edge of ok
  assert.equal(b(60), 'warn');          // chilly
  assert.equal(b(80), 'warn');          // warm (matches the old HOT_F line)
  assert.equal(b(84), 'warn');          // still just warn
  assert.equal(b(57), 'crit');          // cold
  assert.equal(b(85), 'crit');          // hot
  assert.equal(b(null), '');            // no reading
  assert.equal(b(''), '');
  assert.equal(b('nope'), '');
});

test('humidityBand: indoor RH bands + missing readings', () => {
  const b = sandbox.humidityBand;
  assert.equal(b(45), 'ok');            // ideal
  assert.equal(b(30), 'ok');            // low edge of ok
  assert.equal(b(55), 'ok');            // high edge of ok
  assert.equal(b(28), 'warn');          // a bit dry
  assert.equal(b(58), 'warn');          // a bit humid
  assert.equal(b(24), 'crit');          // too dry
  assert.equal(b(61), 'crit');          // mold risk
  assert.equal(b(null), '');
  assert.equal(b(''), '');
});

test('uvBand: EPA UV Index bands', () => {
  const b = sandbox.uvBand;
  assert.equal(b(0), 'good');           // low
  assert.equal(b(2), 'good');
  assert.equal(b(3), 'ok');             // moderate
  assert.equal(b(5), 'ok');
  assert.equal(b(6), 'warn');           // high
  assert.equal(b(7), 'warn');
  assert.equal(b(8), 'crit');           // very high
  assert.equal(b(11), 'crit');          // extreme
  assert.equal(b(null), '');
  assert.equal(b(''), '');
});

test('aqiBand: US AQI bands', () => {
  const b = sandbox.aqiBand;
  assert.equal(b(0), 'good');
  assert.equal(b(50), 'good');          // top of good
  assert.equal(b(51), 'warn');          // moderate
  assert.equal(b(100), 'warn');
  assert.equal(b(101), 'crit');         // unhealthy
  assert.equal(b(180), 'crit');
  assert.equal(b(null), '');
  assert.equal(b(''), '');
});

test('uvBandText: category fallback used when the UV number is missing', () => {
  const b = sandbox.uvBandText;
  assert.equal(b('Low'), 'good');
  assert.equal(b('Moderate'), 'ok');
  assert.equal(b('High'), 'warn');
  assert.equal(b('Very High'), 'crit');   // "very high" out-ranks the bare "high"
  assert.equal(b('Extreme'), 'crit');
  assert.equal(b('Severe'), 'crit');
  assert.equal(b(''), '');
  assert.equal(b(null), '');
  assert.equal(b('Gibberish'), '');       // unrecognized -> no color, never a false good
});

test('aqiBandText: category fallback used when the AQI number is missing', () => {
  const b = sandbox.aqiBandText;
  assert.equal(b('Good'), 'good');
  assert.equal(b('Moderate'), 'warn');
  assert.equal(b('Unhealthy for Sensitive Groups'), 'crit');
  assert.equal(b('Unhealthy'), 'crit');
  assert.equal(b('Very Unhealthy'), 'crit');
  assert.equal(b('Hazardous'), 'crit');
  assert.equal(b(''), '');
  assert.equal(b(null), '');
  assert.equal(b('Gibberish'), '');
});

test('tempBandF: lower crit/warn boundaries are exact (58 warn, not crit)', () => {
  const b = sandbox.tempBandF;
  assert.equal(b(58), 'warn');   // the <58 crit line: 58 is warn, not crit
  assert.equal(b(85), 'crit');   // the >=85 crit line (upper, already pinned)
});

test('humidityBand: exact edge boundaries (25 warn, 60 warn)', () => {
  const b = sandbox.humidityBand;
  assert.equal(b(25), 'warn');   // the <25 crit line: 25 is warn
  assert.equal(b(60), 'warn');   // the >60 mold-risk crit line: 60 is warn, not crit
});

test('clampFrac: bounded 0..1 fill fraction for the meters', () => {
  const f = sandbox.clampFrac;
  assert.equal(f(0, 11), 0);
  assert.equal(f(11, 11), 1);
  assert.equal(f(22, 11), 1);           // over max clamps to full
  assert.equal(f(-3, 11), 0);           // negative clamps to empty
  assert.equal(f(5.5, 11), 0.5);
  assert.equal(f(null, 11), 0);         // no reading -> empty bar
  assert.equal(f('', 200), 0);
  assert.equal(f(50, 0), 0);            // guard divide-by-zero
});

// ----------------------------------------------------------- backupBadge

test('backupBadge: hidden when payload missing, unknown, or fresh', () => {
  assert.equal(backupBadge(null).show, false);
  assert.equal(backupBadge(undefined).show, false);
  assert.equal(backupBadge({ known: false, stale: false, age_s: null }).show, false);
  assert.equal(backupBadge({ known: true, stale: false, age_s: 3600 }).show, false);
});

test('backupBadge: amber with age when a known backup is stale', () => {
  const b = backupBadge({ known: true, stale: true, age_s: 144000, threshold_s: 129600 });
  assert.equal(b.show, true);
  assert.equal(b.level, 'warn');
  assert.match(b.text, /Backup stale/);
  assert.match(b.text, /40h/);   // 144000s
});

// The local snapshot can be fresh while every off-box copy fails. That used to
// read as a healthy backup; a failing or stale remote now shows the badge.
test('backupBadge: a healthy remote (or none configured) stays hidden', () => {
  const ok = { ok: true, last_ok: 'x', age_s: 3600, stale: false, failing: false };
  assert.equal(backupBadge({ known: true, stale: false, age_s: 60, remote: ok }).show, false);
  assert.equal(backupBadge({ known: true, stale: false, age_s: 60, remote: null }).show, false);
});

test('backupBadge: amber when the off-box copy is failing', () => {
  const b = backupBadge({ known: true, stale: false, age_s: 60, threshold_s: 129600,
    remote: { ok: false, last_ok: 'x', age_s: 7200, stale: false, failing: true } });
  assert.equal(b.show, true);
  assert.equal(b.level, 'warn');
  assert.match(b.text, /Off-box backup failing/);
  assert.match(b.title, /2h/);          // last good copy 7200s ago
});

test('backupBadge: amber with age when the off-box copy is stale', () => {
  const b = backupBadge({ known: true, stale: false, age_s: 60, threshold_s: 129600,
    remote: { ok: true, last_ok: 'x', age_s: 144000, stale: true, failing: false } });
  assert.equal(b.show, true);
  assert.match(b.text, /Off-box backup stale \(40h\)/);
});

test('backupBadge: a remote that never succeeded says so', () => {
  const b = backupBadge({ known: true, stale: false, age_s: 60, threshold_s: 129600,
    remote: { ok: false, last_ok: null, age_s: null, stale: true, failing: true } });
  assert.equal(b.show, true);
  assert.match(b.title, /never/i);
});

test('backupBadge: a stale LOCAL backup still wins the badge text', () => {
  const b = backupBadge({ known: true, stale: true, age_s: 144000, threshold_s: 129600,
    remote: { ok: false, last_ok: null, age_s: null, stale: true, failing: true } });
  assert.match(b.text, /^⚠ Backup stale/);
});

/* ---- month lanes (the Google-style spanning bars) ---- */
const WEEK = ['2026-08-23', '2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28', '2026-08-29'];

test('assignLanes: a multi-day all-day event spans its columns as one bar, clipped to the week', () => {
  const fair = { id: 'fair', title: 'Fair', all_day: 1, start_ts: '2026-08-26', end_ts: '2026-09-01' };
  const [it] = sandbox.assignLanes([fair], WEEK);
  assert.equal(it.c0, 3, 'starts on Wed');
  assert.equal(it.c1, 6, 'runs through Sat (clipped)');
  assert.equal(it.contR, true, 'continues into next week');
  assert.equal(it.contL, false);
  assert.equal(it.bar, true);
  assert.equal(it.lane, 0);
  // next week: the same event starts clipped on the left, ends Mon (end exclusive)
  const next = WEEK.map((d) => sandbox.addDays(d, 7));
  const [n] = sandbox.assignLanes([fair], next);
  assert.deepEqual([n.c0, n.c1, n.contL, n.contR], [0, 1, true, false]);
});

test('assignLanes: bars stack above timed events; overlapping bars take separate lanes; a free lane is reused', () => {
  const evs = [
    { id: 't', title: 'Dentist', all_day: 0, start_ts: '2026-08-26T08:00:00', end_ts: '2026-08-26T09:00:00' },
    { id: 'a', title: 'A', all_day: 1, start_ts: '2026-08-26', end_ts: '2026-08-29' },
    { id: 'b', title: 'B', all_day: 1, start_ts: '2026-08-27', end_ts: '2026-08-28' },
    { id: 'c', title: 'C', all_day: 1, start_ts: '2026-08-23', end_ts: '2026-08-24' },
  ];
  const by = Object.fromEntries(sandbox.assignLanes(evs, WEEK).map((it) => [it.ev.id, it]));
  assert.equal(by.a.lane, 0, 'longest bar first');
  assert.equal(by.c.lane, 0, 'Sun-only bar reuses lane 0 (no overlap with A)');
  assert.equal(by.b.lane, 1, 'B overlaps A, next lane');
  assert.equal(by.t.bar, false, 'single timed event is not a bar');
  assert.equal(by.t.lane, 1, 'timed event takes the first free lane under the bars on its day');
});

test('assignLanes: events outside the week are dropped; a timed overnighter is a 2-day bar', () => {
  const far = { id: 'f', title: 'Far', all_day: 1, start_ts: '2026-09-10', end_ts: '2026-09-11' };
  const night = { id: 'n', title: 'Night', all_day: 0, start_ts: '2026-08-28T22:00:00', end_ts: '2026-08-29T06:00:00' };
  const out = sandbox.assignLanes([far, night], WEEK);
  assert.equal(out.length, 1);
  assert.deepEqual([out[0].ev.id, out[0].c0, out[0].c1, out[0].bar], ['n', 5, 6, true]);
});

test('inkFor: dark text on mid/light bar colors, light text on dark ones, dark fallback for non-hex', () => {
  assert.equal(sandbox.inkFor('#F6BF26'), '#0B1020');   // Google "banana"
  assert.equal(sandbox.inkFor('#7BC8EE'), '#0B1020');
  assert.equal(sandbox.inkFor('#1F3A93'), '#F4F6FB');   // navy
  assert.equal(sandbox.inkFor('#333'), '#F4F6FB');      // 3-digit form
  assert.equal(sandbox.inkFor('transparent'), 'var(--ink)');   // unmeasurable → page ink, never invisible
});

test('assignLanes: an all-day event with end == start (server allows it) is a one-day bar, never dropped or negative', () => {
  // Sunday start: the old negative span fell before the week and vanished.
  const sun = { id: 's', title: 'S', all_day: 1, start_ts: '2026-08-23', end_ts: '2026-08-23' };
  // Midweek: it kept c1 < c0 and painted over whatever held lane 0.
  const tue = { id: 't', title: 'T', all_day: 1, start_ts: '2026-08-25', end_ts: '2026-08-25' };
  const other = { id: 'o', title: 'O', all_day: 1, start_ts: '2026-08-25', end_ts: '2026-08-26' };
  const back = { id: 'b', title: 'B', all_day: 0, start_ts: '2026-08-27T10:00:00', end_ts: '2026-08-26T09:00:00' };
  const by = Object.fromEntries(sandbox.assignLanes([sun, tue, other, back], WEEK).map((it) => [it.ev.id, it]));
  assert.deepEqual([by.s.c0, by.s.c1], [0, 0]);
  assert.deepEqual([by.t.c0, by.t.c1], [2, 2]);
  assert.notEqual(by.t.lane, by.o.lane, 'two events on the same day never share a lane');
  assert.deepEqual([by.b.c0, by.b.c1], [4, 4], 'end-before-start clamps to the start day');
});

/* review 2026-09-22: a chore nobody can do used to vanish from every card */
test('unassignedChores: fixed owner gone or turned off, or a rotation with nobody active', () => {
  const people = [{ id: 1, active: 1 }, { id: 2, active: 0 }];
  const chores = [
    { id: 10, active: 1, assign_kind: 'fixed', fixed_person_id: 1 },       // fine
    { id: 11, active: 1, assign_kind: 'fixed', fixed_person_id: 2 },       // owner turned off
    { id: 12, active: 1, assign_kind: 'fixed', fixed_person_id: null },    // owner deleted
    { id: 13, active: 1, assign_kind: 'rotation', rotation_order: [2] },   // nobody active
    { id: 14, active: 1, assign_kind: 'rotation', rotation_order: [2, 1] }, // fine
    { id: 15, active: 0, assign_kind: 'fixed', fixed_person_id: null },    // chore itself off
    { id: 16, active: 1, assign_kind: 'rotation', rotation_order: [] },    // everyone deleted
  ];
  assert.deepEqual([...sandbox.unassignedChores(chores, people)].map((c) => c.id), [11, 12, 13, 16]);
});

test('unassignedChoresHtml: a Reassign button per chore that opens the editor; nothing when empty', () => {
  assert.equal(sandbox.unassignedChoresHtml([]), '');
  const html = sandbox.unassignedChoresHtml([{ id: 12, icon: '🐶', title: 'Feed <dog>' }]);
  assert.match(html, /No one to do these/);
  assert.match(html, /data-edit-chore="12"/);
  assert.match(html, /Feed &lt;dog&gt;/);
});

test('choreToggleMessage: a locked chore says it stays done, never "tap again"', () => {
  assert.equal(sandbox.choreToggleMessage('this chore was finished before it came off today’s plan; it stays done', false),
    'That one was already finished, so it stays done.');
  assert.equal(sandbox.choreToggleMessage('', true), 'That day has ended, so it can’t be changed now.');
  assert.match(sandbox.choreToggleMessage('boom', false), /tap again/);
});


// --- the plain-words chore form (2026-10-19) --------------------------------

test('buildChorePayload: a weekly rotation carries rotate_every week; one person never does', () => {
  const rot = JSON.parse(JSON.stringify(sandbox.buildChorePayload({
    title: 'Dishes', repeat: 'daily', days: new Set(), assign: 'rotation', rot: [1, 2], rotateEvery: 'week',
  })));
  assert.equal(rot.rotate_every, 'week');
  assert.deepEqual(rot.rotation_order, [1, 2]);
  const one = JSON.parse(JSON.stringify(sandbox.buildChorePayload({
    title: 'Dishes', repeat: 'daily', days: new Set(), assign: 'fixed', person: 1, rotateEvery: 'week',
  })));
  assert.equal(one.rotate_every, 'turn');
  const once = JSON.parse(JSON.stringify(sandbox.buildChorePayload({
    title: 'x', repeat: 'once', assign: 'rotation', rot: [1, 2], rotateEvery: 'week', person: 1, date: '2026-10-20',
  })));
  assert.equal(once.rotate_every, 'turn', 'a one-time chore is one person, so it never rotates');
});

test('choreToModel: rotate_every round-trips; anything else reads as each time', () => {
  const base = { title: 'D', icon: '', schedule_kind: 'daily', days_mask: 0, assign_kind: 'rotation',
    fixed_person_id: null, rotation_order: [1, 2], rotation_epoch: '2026-08-01' };
  assert.equal(sandbox.choreToModel({ ...base, rotate_every: 'week' }).rotateEvery, 'week');
  assert.equal(sandbox.choreToModel({ ...base, rotate_every: 'turn' }).rotateEvery, 'turn');
  assert.equal(sandbox.choreToModel(base).rotateEvery, 'turn');
});

const FAM = [{ id: 1, name: 'Ava', active: 1 }, { id: 2, name: 'Ruby', active: 1 }, { id: 3, name: 'Milo', active: 1 }];

test('choreSummary: says when and who in plain words', () => {
  const S = (m) => sandbox.choreSummary({ days: new Set(), rot: [], each: [], ...m }, FAM);
  assert.equal(S({ repeat: 'daily', assign: 'fixed', fixed_person_id: 1 }), 'Every day. Ava does it.');
  assert.equal(S({ repeat: 'weekly', days: new Set([0, 3]), weekInterval: 1, assign: 'fixed', fixed_person_id: 2 }),
    'Every Monday and Thursday. Ruby does it.');
  assert.equal(S({ repeat: 'weekly', days: new Set([1]), weekInterval: 2, assign: 'fixed', fixed_person_id: 2 }),
    'Every other Tuesday. Ruby does it.');
  assert.equal(S({ repeat: 'weekly', days: new Set([0, 1, 2, 3, 4]), weekInterval: 1, assign: 'fixed', fixed_person_id: 1 }),
    'Every weekday. Ava does it.');
  assert.equal(S({ repeat: 'interval', intervalDays: '3', assign: 'fixed', fixed_person_id: 1 }), 'Every 3 days. Ava does it.');
  assert.equal(S({ repeat: 'daily', assign: 'rotation', rot: [1, 2], rotateEvery: 'week' }),
    'Every day. Ava and Ruby take turns, a new person each week.');
  assert.equal(S({ repeat: 'daily', assign: 'rotation', rot: [1, 2, 1], rotateEvery: 'turn' }),
    'Every day. Ava and Ruby take turns, a new person each time.');
  assert.equal(S({ repeat: 'daily', assign: 'each', each: [1, 2, 3] }), 'Every day. Ava, Ruby and Milo each do their own.');
  assert.equal(S({ repeat: 'daily', assign: 'fixed', fixed_person_id: null }), 'Every day. Pick who does it.');
  // a one-time chore is one person whatever the turn setting says
  assert.match(S({ repeat: 'once', date: '2026-10-21', assign: 'rotation', rot: [1, 2], fixed_person_id: 3 }),
    /^Once, on Wed Oct 21\. Milo does it\.$/);
});

test('choreSummary: escapes nothing itself (callers put it in textContent or escapeHtml)', () => {
  const s = sandbox.choreSummary({ repeat: 'daily', assign: 'fixed', fixed_person_id: 9, days: new Set(), rot: [], each: [] },
    [{ id: 9, name: '<b>x</b>', active: 1 }]);
  assert.equal(s, 'Every day. <b>x</b> does it.');
  assert.match(sandbox.allChoresHtml([{ id: 1, title: 'T', icon: '', active: 1, schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 9, rotation_order: [], rotation_epoch: '2026-01-01' }],
  [{ id: 9, name: '<b>x</b>', active: 1 }]), /&lt;b&gt;x&lt;\/b&gt; does it/);
});

test('rotateOrderToStart: keeps the cycle, moves its start', () => {
  const R = (o, slot, idx) => JSON.parse(JSON.stringify(sandbox.rotateOrderToStart(o, slot, idx)));
  // slot 0 is up next; make Ruby (index 1) go first
  assert.deepEqual(R([1, 2, 3], 0, 1), [2, 3, 1]);
  // slot 1 is up next (Ruby); make Ava (index 0) the one at slot 1
  assert.deepEqual(R([1, 2, 3], 1, 0), [3, 1, 2]);
  assert.deepEqual(R([1, 2], 1, 1), [1, 2], 'already up: unchanged');
  assert.deepEqual(R([1], 0, 0), [1]);
  assert.deepEqual(R([1, 2], null, 1), [1, 2], 'no preview slot: unchanged');
});

test('turnNote: says in plain words when an edit keeps or moves the next turn', () => {
  const N = sandbox.turnNote;
  assert.equal(N('moved', 'Ava', 'Ruby', 'This week', true), 'This moves this week from Ava to Ruby.');
  assert.equal(N('moved', 'Ava', 'Ruby', 'Next week', true), 'This moves next week from Ava to Ruby.');
  assert.equal(N('moved', 'Ava', 'Ruby', 'Week of Oct 26', true), 'This moves the week of Oct 26 from Ava to Ruby.');
  assert.equal(N('kept', 'Ava', 'Ava', 'This week', true), 'Ava keeps this week, same as before.');
  // turn by turn: the cell label is a day, the turn is "the next turn"
  assert.equal(N('moved', 'Ava', 'Ruby', 'Today', false), 'This moves the next turn from Ava to Ruby.');
  assert.equal(N('kept', 'Ava', 'Ava', 'Wed Oct 21', false), 'Ava keeps the next turn, same as before.');
});

test('chorePreviewCells: by week for a weekly rotation, by day otherwise', () => {
  const up = [];
  for (let i = 0; i < 20; i++) {
    const d = new Date(2026, 9, 19 + i);   // Mon Oct 19 onward
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    up.push({ date: iso, person_id: Math.floor(i / 7) % 2 ? 2 : 1 });
  }
  const weeks = JSON.parse(JSON.stringify(sandbox.chorePreviewCells(up, true, '2026-10-19')));
  assert.deepEqual(weeks, [
    { label: 'This week', person_id: 1 }, { label: 'Next week', person_id: 2 }, { label: 'Week of Nov 2', person_id: 1 }]);
  const days = JSON.parse(JSON.stringify(sandbox.chorePreviewCells(up.slice(1), false, '2026-10-19')));
  assert.equal(days.length, 6);
  assert.deepEqual(days.slice(0, 2), [{ label: 'Tomorrow', person_id: 1 }, { label: 'Wed Oct 21', person_id: 1 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.chorePreviewCells(up, false, 'nope'))), []);
});

test('todoDropOrder: puts the item at the slot, never twice', () => {
  const D = (ids, id, i) => JSON.parse(JSON.stringify(sandbox.todoDropOrder(ids, id, i)));
  assert.deepEqual(D([1, 2, 3], 3, 0), [3, 1, 2]);
  assert.deepEqual(D([1, 2, 3], 1, 2), [2, 3, 1]);
  assert.deepEqual(D([1, 2], 9, 1), [1, 9, 2], 'from another list');
  assert.deepEqual(D([1, 2], 9, 99), [1, 2, 9], 'past the end lands last');
  assert.deepEqual(D([], 9, 0), [9]);
});

test('todoKeyMove: arrows move one place, or to the end of the next list, and stop at the ends', () => {
  const B = { now: [{ id: 1 }, { id: 2 }, { id: 9, done_at: 'x' }], soon: [{ id: 3 }], later: [] };
  const K = (id, key) => JSON.parse(JSON.stringify(sandbox.todoKeyMove(B, id, key)));
  assert.deepEqual(K(1, 'ArrowDown'), { bucket: 'now', order: [2, 1] });
  assert.deepEqual(K(2, 'ArrowUp'), { bucket: 'now', order: [2, 1] });
  assert.deepEqual(K(1, 'ArrowUp'), { edge: 'top', bucket: 'now' });
  assert.deepEqual(K(2, 'ArrowDown'), { edge: 'bottom', bucket: 'now' }, 'a checked item does not count');
  assert.deepEqual(K(1, 'ArrowRight'), { bucket: 'soon', order: [3, 1] });
  assert.deepEqual(K(3, 'ArrowRight'), { bucket: 'later', order: [3] });
  assert.deepEqual(K(1, 'ArrowLeft'), { edge: 'first', bucket: 'now' });
  assert.equal(sandbox.todoKeyMove(B, 9, 'ArrowDown'), null, 'a checked item has no grip to move');
  assert.equal(sandbox.todoKeyMove(B, 1, 'Enter'), null);
});

test('todoFailMessage: a name the server refuses says so instead of "tap again"', () => {
  assert.match(sandbox.todoFailMessage('title must be 1-120 characters'), /120 characters/);
  assert.match(sandbox.todoFailMessage('unknown todo'), /another device/);
  assert.match(sandbox.todoFailMessage('Failed to fetch'), /check the hub/);
});

test('todoFailMessage: an item checked off elsewhere mid-move says so', () => {
  assert.match(sandbox.todoFailMessage("a checked-off item can't be moved"), /checked that item off/);
});

// --- trips (out of town) ------------------------------------------------------

const TP = [{ id: 1, name: 'Ava' }, { id: 2, name: 'Milo' }, { id: 3, name: 'Ruby' }];

test('tripWhoLabel: Everyone, one name, a list, and someone no longer on the wall', () => {
  assert.equal(sandbox.tripWhoLabel([1, 2, 3], TP), 'Everyone');
  assert.equal(sandbox.tripWhoLabel([2], TP), 'Milo');
  assert.equal(sandbox.tripWhoLabel([1, 3], TP), 'Ava and Ruby');
  assert.equal(sandbox.tripWhoLabel([3, 1, 2], TP.slice(0, 2).concat([{ id: 4, name: 'Jo' }])),
    'Ava, Milo and 1 more');
  assert.equal(sandbox.tripWhoLabel([1], [{ id: 1, name: 'Ava' }]), 'Ava',
    'a one-person household is a name, not "Everyone"');
});

test('tripSummary + tripPayload: the back day is the day chores return', () => {
  const m = { going: new Set([1, 2, 3]), start: '2026-11-24', back: '2026-11-29', backup: '', ids: [] };
  assert.equal(sandbox.tripSummary(m, TP),
    'Everyone is away Tue Nov 24 to Sat Nov 28. Chores resume Sun Nov 29.');
  assert.match(sandbox.tripSummary({ ...m, going: new Set([1, 3]) }, TP), /^Ava and Ruby are away/);
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.tripPayload(m))),
    { body: { person_ids: [1, 2, 3], start_date: '2026-11-24', end_date: '2026-11-28', backup_person_id: null } });
  const one = { going: new Set([2]), start: '2026-11-24', back: '2026-11-25', backup: '1', ids: [7] };
  assert.equal(sandbox.tripSummary(one, TP), 'Milo is away on Tue Nov 24. Chores resume Wed Nov 25.');
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.tripPayload(one))).body,
    { person_ids: [2], start_date: '2026-11-24', end_date: '2026-11-24', backup_person_id: 1, ids: [7] });
});

test('tripPayload refuses an empty, undated or backwards trip', () => {
  const base = { going: new Set([1]), start: '2026-11-24', back: '2026-11-26', backup: '', ids: [] };
  assert.match(sandbox.tripPayload({ ...base, going: new Set() }).error, /who/);
  assert.match(sandbox.tripPayload({ ...base, back: '' }).error, /resume/);
  assert.match(sandbox.tripPayload({ ...base, start: '' }).error, /leave/);
  assert.match(sandbox.tripPayload({ ...base, back: '2026-11-24' }).error, /after/);
});

test('tripBannerHtml escapes names and carries the trip ids', () => {
  const html = sandbox.tripBannerHtml([{ ids: [5, 6], person_ids: [9], start_date: '2026-11-24',
    end_date: '2026-11-25', state: 'now' }], [{ id: 9, name: '<b>x</b>' }]);
  assert.ok(html.includes('&lt;b&gt;x&lt;/b&gt;') && !html.includes('<b>x'));
  assert.ok(html.includes('data-trip-end="5,6"'));
  assert.match(html, /Away, chores resume Thu Nov 26/);
});

test('tripPayload: "keep" sends no backup so an edit leaves each one alone', () => {
  const m = { going: new Set([1]), start: '2026-11-24', back: '2026-11-26', backup: 'keep', ids: [4, 5] };
  const { body } = JSON.parse(JSON.stringify(sandbox.tripPayload(m)));
  assert.ok(!('backup_person_id' in body), 'no backup key at all');
  assert.deepEqual(body.ids, [4, 5]);
});

test('tripPeopleWith adds a traveller who has left the wall, once', () => {
  const got = sandbox.tripPeopleWith(TP, { people: [{ id: 2, name: 'Milo' }, { id: 9, name: 'Jo', color: '#123456' }] });
  assert.deepEqual(JSON.parse(JSON.stringify(got.map((p) => p.id))), [1, 2, 3, 9]);
  const html = sandbox.tripBannerHtml([{ ids: [1], person_ids: [9], start_date: '2026-11-24',
    end_date: null, state: 'now', people: [{ id: 9, name: 'Jo' }] }], TP);
  assert.match(html, /Jo/, 'the banner names them instead of "1 more"');
});


// --- How it works (help) --------------------------------------------------------

const helpIds = (ctx) => sandbox.helpTopics(ctx).map((t) => t.id);
const ctxOf = (ids, extra = {}) => ({ enabled: new Set(ids), status: {}, todoSource: 'local',
  todoReadonly: true, backup: null, ...extra });
const topic = (ctx, id) => sandbox.helpTopics(ctx).find((t) => t.id === id);
const plain = (v) => JSON.parse(JSON.stringify(v));
const BROKEN = /not connecting right now/;

test('helpTopics: each topic follows its feature switch; phone, screen and backups always show', () => {
  assert.deepEqual(plain(helpIds(ctxOf([]))), ['phone', 'screen', 'backup']);
  assert.deepEqual(plain(helpIds(ctxOf(['chores']))),
    ['chores', 'trips', 'editing', 'phone', 'screen', 'backup']);
  assert.ok(helpIds(ctxOf(['ics_calendar'])).includes('calendar'), 'any calendar source shows Calendar');
  assert.ok(!helpIds(ctxOf(['ics_calendar'], { calendarOn: false })).includes('calendar'),
    'the wall\'s calendar rule wins when given');
});

test('helpTopics: the trips line says a tick means going, and who starts unticked', () => {
  const text = topic(ctxOf(['chores']), 'trips').paras.join(' ');
  assert.match(text, /Everyone starts ticked, except anyone who is already away\. Untick anyone who is staying home\./);
  assert.ok(!/Everyone at home starts ticked/.test(text), 'the old, wrong wording is gone');
});

test('helpTopics: half-on cards name only what is on; weather and climate together', () => {
  const w = topic(ctxOf(['climate']), 'weather');
  assert.equal(w.title, 'House climate');
  assert.equal(w.paras.length, 1);
  assert.ok(!/Full forecast/.test(w.paras[0]), 'no weather text when only climate is on');
  const both = topic(ctxOf(['weather', 'climate']), 'weather');
  assert.equal(both.title, 'Weather and house climate');
  assert.equal(both.paras.length, 2);
  assert.match(both.paras[0], /Full forecast/);
  assert.match(both.paras[1], /Full climate/);
  const p = topic(ctxOf(['fleet']), 'printer');
  assert.equal(p.title, 'Servers');
  assert.ok(!/printer/i.test(p.paras.join(' ')));
});

test('helpTopics: the To-Do topic matches the list and whether it can be changed', () => {
  assert.match(topic(ctxOf(['todos']), 'todos').paras.join(' '), /Now, Soon or Later/);
  const ro = topic(ctxOf(['todos'], { todoSource: 'icloud', todoReadonly: true }), 'todos').paras.join(' ');
  assert.match(ro, /iCloud Reminders/);
  assert.match(ro, /view-only/);
  assert.match(ro, /Sync direction to 2-way/);
  assert.match(ro, /overdue or due today, up to five\. Full list shows everything/);
  const rw = topic(ctxOf(['todos'], { todoSource: 'icloud', todoReadonly: false }), 'todos').paras.join(' ');
  assert.match(rw, /Tap an item to check it off/);
  assert.ok(!/view-only/.test(rw));
});

test('helpTopics: "not connecting" only for cards that report a status, and only when on', () => {
  assert.match(topic(ctxOf(['laundry'], { status: { laundry: 'needs_auth' } }), 'laundry').paras[0], BROKEN);
  assert.match(topic(ctxOf(['google_calendar'], { status: { google_calendar: 'error' } }), 'calendar').paras[0], BROKEN);
  assert.ok(!BROKEN.test(topic(ctxOf(['laundry'], { status: { laundry: 'ok' } }), 'laundry').paras[0]));
  // switched off: no topic, and so no warning
  assert.ok(!helpIds(ctxOf([], { status: { laundry: 'needs_auth' } })).includes('laundry'));
  // cards that never report a status never claim one, whatever the payload says
  for (const id of ['weather', 'climate', 'cameras', 'printer', 'fleet']) {
    const all = sandbox.helpTopics(ctxOf([id], { status: { [id]: 'error' } }));
    assert.ok(!all.some((t) => t.paras.some((x) => BROKEN.test(x))), `${id} never says not connecting`);
  }
});

test('helpTopics: a broken iCloud never claims the to-do card or calendar is down outright', () => {
  const todoErr = topic(ctxOf(['todos'], { todoSource: 'icloud', status: { icloud_caldav: 'error' },
    enabled: new Set(['todos', 'icloud_caldav']) }), 'todos').paras[0];
  assert.match(todoErr, /may only be the copy of chores into Reminders/);
  assert.ok(!BROKEN.test(todoErr));
  const todoAuth = topic(ctxOf([], { todoSource: 'icloud', status: { icloud_caldav: 'needs_auth' },
    enabled: new Set(['todos', 'icloud_caldav']) }), 'todos').paras[0];
  assert.match(todoAuth, /iCloud sign-in is not working/);
  const cal = topic(ctxOf(['icloud_caldav'], { status: { icloud_caldav: 'error' } }), 'calendar').paras[0];
  assert.match(cal, /may only be the copy of chores into Reminders/);
});

test('helpBackupLine: every state and the age boundaries', () => {
  const L = (b) => sandbox.helpBackupLine(b);
  const ok = (age_s, remote) => ({ known: true, stale: false, age_s, remote });
  const cases = [
    [null, 'No backup has reported yet. If this stays so, check the hub.'],
    [{ known: false, age_s: 10 }, 'No backup has reported yet. If this stays so, check the hub.'],
    [{ known: true, age_s: null }, 'The last backup time looks wrong. Check the hub.'],
    [{ known: true }, 'The last backup time looks wrong. Check the hub.'],
    [{ known: true, age_s: -5 }, 'The last backup time looks wrong. Check the hub.'],
    [{ known: true, age_s: NaN }, 'The last backup time looks wrong. Check the hub.'],
    [{ known: true, age_s: '3600' }, 'The last backup time looks wrong. Check the hub.'],
    [{ known: true, stale: true, age_s: 3 * 86400, remote: { failing: true } },
      'The last backup was 3 days ago, which is too old.'],          // local stale beats remote failing
    [ok(7200, { failing: true, stale: true }), 'The copy off the box is failing. The copy on the hub is fine.'],
    [ok(7200, { failing: false, stale: true }), 'The copy off the box is too old. The copy on the hub is fine.'],
    [ok(600), 'Last backup less than an hour ago.'],
    [ok(1800), 'Last backup 1 hour ago.'],
    [ok(47 * 3600), 'Last backup 47 hours ago.'],
    [ok(48 * 3600), 'Last backup 2 days ago.'],
  ];
  for (const [b, want] of cases) assert.equal(L(b), want, JSON.stringify(b));
});

test('helpTopics: the screen topic reads its times from idleReturnMs', () => {
  const t = topic(ctxOf([]), 'screen').paras.join(' ');
  assert.match(t, /a minute and a half on most pages, three minutes on the calendar, five minutes on a camera and five minutes in Settings/);
});

test('helpTopics: the screen topic says this hub\'s dim hours and whether an empty house dims it', () => {
  const screen = (dim) => topic(ctxOf([], { dim }), 'screen').paras.join(' ');
  assert.match(screen(null), /From 10pm to 6am every screen dims/, 'before the first poll: the default');
  assert.match(screen({ hours: { on: true, start: '21:30', end: '07:00' }, away: null }),
    /From 9:30pm to 7am every screen dims/);
  const off = screen({ hours: { on: false, start: '22:00', end: '06:00' }, away: null });
  assert.match(off, /don.t dim for the time of day/);
  assert.match(off, /Settings, under Display/);
  assert.doesNotMatch(off, /nobody is home/, 'no Home Assistant: no away line');
  const on = screen({ hours: { on: true, start: '22:00', end: '06:00' },
    away: { enabled: true, state: 'home', home: 2, dim: false } });
  assert.match(on, /While nobody is home the wall dims too/);
  assert.match(on, /stays bright/, 'and says what happens when it can\'t tell');
  const offSwitch = screen({ hours: { on: true, start: '22:00', end: '06:00' },
    away: { enabled: false, state: 'off', home: null, dim: false } });
  assert.match(offSwitch, /can also dim while nobody is home/);
});

test('help text: no em or en dashes, and no names, in every string the help can show', () => {
  const ctxs = [
    ctxOf(['chores', 'todos', 'google_calendar', 'cameras', 'weather', 'climate', 'laundry', 'printer',
      'fleet'], { backup: { known: true, age_s: 60 }, status: { laundry: 'error' } }),
    ctxOf(['todos', 'icloud_caldav'], { todoSource: 'icloud', todoReadonly: true, status: { icloud_caldav: 'error' } }),
    ctxOf(['todos', 'icloud_caldav'], { todoSource: 'icloud', todoReadonly: false, status: { icloud_caldav: 'needs_auth' } }),
    ctxOf([], { dim: { hours: { on: false, start: '22:00', end: '06:00' }, away: { enabled: false, state: 'off' } } }),
    ctxOf([], { dim: { hours: { on: true, start: '21:30', end: '07:00' }, away: { enabled: true, state: 'away', home: 0, dim: true } } }),
  ];
  const strings = ctxs.flatMap((c) => sandbox.helpTopics(c).flatMap((t) => [t.title, ...t.paras]));
  // top-level consts live in the script scope, not on the sandbox object
  for (const k of ['HELP_LOADING', 'HELP_UNREACHABLE', 'HELP_BROKEN', 'HELP_ICLOUD_AUTH', 'HELP_ICLOUD_ERROR']) {
    strings.push(vm.runInContext(k, sandbox));
  }
  for (const b of [null, { known: true, age_s: -1 }, { known: true, stale: true, age_s: 9e5 },
    { known: true, age_s: 60, remote: { failing: true } }, { known: true, age_s: 60, remote: { stale: true } }]) {
    strings.push(sandbox.helpBackupLine(b));
  }
  assert.ok(strings.every((x) => typeof x === 'string' && x.length), 'every string is real text');
  const text = strings.join(' ');
  assert.ok(text.includes('view-only') && text.includes('Tap an item to check it off'), 'both iCloud texts scanned');
  assert.ok(!text.includes(String.fromCharCode(0x2014)), 'no em dashes');
  assert.ok(!text.includes(String.fromCharCode(0x2013)), 'no en dashes');
  // The help speaks about "each person", never a named one: any capitalized
  // word that is not ordinary sentence text, a feature or a UI label would be
  // a name, and this repo is public.
  const allowed = new Set(['A', 'Add', 'After', 'All', 'Anyone', 'Assistant', 'Auto-return', 'Away', 'Back', 'Backups',
    'Dim', 'Display', 'Phones', 'Turn',
    'Calendar', 'Camera', 'Cameras', 'Changes', 'Changing', 'Check', 'Chores', 'D', 'Deactivate', 'Delete',
    'Each', 'Edit', 'Everyone', 'Everyone’s', 'Features', 'Finish', 'Fix', 'Fleet', 'From', 'Full', 'Going',
    'Green', 'Home', 'House', 'How', 'If', 'In', 'Items', 'It', 'Last', 'Later', 'Laundry', 'Loading', 'Month',
    'New', 'No', 'Now', 'On', 'Once', 'One', 'Only', 'Open', 'Past', 'People', 'Pick', 'Reminders', 'Right', 'Safari',
    'Screen', 'Scroll', 'Servers', 'Settings', 'Share', 'Soon', 'Sync', 'Tap', 'Test', 'That', 'The', 'This',
    'To', 'To-Do', 'To-dos', 'Trip', 'UV', 'Untick', 'Use', 'Waiting', 'Weather', 'When', 'While', 'You', 'iCloud',
    'iPhone']);
  const words = text.match(/\b[A-Z][\w’-]*/g) || [];
  const odd = [...new Set(words)].filter((w) => !allowed.has(w));
  assert.deepEqual(odd, [], `capitalized words that could be names: ${odd.join(', ')}`);
});

test('helpHtml escapes every string, keeps open rows open, one details row per topic', () => {
  const html = sandbox.helpHtml([{ id: 'x"y', title: '<b>T</b>', paras: ['a & b'] }, { id: 'z', title: 'Z', paras: [] }],
    new Set(['z']));
  assert.ok(html.includes('&lt;b&gt;T&lt;/b&gt;') && !html.includes('<b>T'));
  assert.ok(html.includes('a &amp; b'));
  assert.equal((html.match(/<details /g) || []).length, 2);
  assert.equal((html.match(/<\/details>/g) || []).length, 2, 'balanced');
  assert.match(html, /data-help="z" open>/, 'a topic open before a repaint stays open');
  assert.ok(!/data-help="x&quot;y" open/.test(html));
});


/* ------------------------------------------------------------ chore wallet */

test('formatBonus: whole dollars drop the cents', () => {
  assert.equal(sandbox.formatBonus(10000), '$100');
  assert.equal(sandbox.formatBonus(750), '$7.50');
  assert.equal(sandbox.formatBonus(35), '35¢');
});

test('formatCents: dollars, cents under a dollar, zero and below', () => {
  assert.equal(sandbox.formatCents(1850), '$18.50');
  assert.equal(sandbox.formatCents(30), '30¢');
  assert.equal(sandbox.formatCents(100), '$1.00');
  assert.equal(sandbox.formatCents(0), '$0.00');
  assert.equal(sandbox.formatCents(-250), '-$2.50');
  assert.equal(sandbox.formatCents(118924), '$1,189.24');
});

const R = {
  streak: 47, goal: 100, shields: 2, shield_max: 2, wallet_cents: 1850,
  rate_cents: 30, next_bonus: { at: 100, cents: 10000 }, next_level: null,
  bonus_today_cents: 0, outcomes7: ['kept', 'kept', 'saved', 'kept', 'kept', 'kept', 'open'],
};

test('rewardHeadHtml: streak toward the goal and one mark per shield', () => {
  const h = sandbox.rewardHeadHtml(R);
  assert.match(h, /47 \/ 100/);
  assert.equal((h.match(/shield-on/g) || []).length, 2);
  assert.equal((h.match(/shield-off/g) || []).length, 0);
  const one = sandbox.rewardHeadHtml({ ...R, shields: 1 });
  assert.equal((one.match(/shield-on/g) || []).length, 1);
  assert.equal((one.match(/shield-off/g) || []).length, 1);
  // shields switched off: no shield marks at all
  assert.doesNotMatch(sandbox.rewardHeadHtml({ ...R, shields: 0, shield_max: 0 }), /shield-/);
  assert.equal(sandbox.rewardHeadHtml(null), '');
});

test('walletLineHtml: the wallet and what comes next', () => {
  const w = sandbox.walletLineHtml(R);
  assert.match(w, /\$18\.50/);
  assert.match(w, /next bonus \$100 at 100/);
  const lv = sandbox.walletLineHtml({ ...R, next_bonus: null, next_level: { at: 25, cents: 35 } });
  assert.match(lv, /35¢ a chore at 25/);
  const none = sandbox.walletLineHtml({ ...R, next_bonus: null, next_level: null });
  assert.doesNotMatch(none, /next/);
  const paid = sandbox.walletLineHtml({ ...R, bonus_today_cents: 10000 });
  assert.match(paid, /\+\$100 bonus today/);
  assert.doesNotMatch(paid, /next bonus/);
  assert.equal(sandbox.walletLineHtml(undefined), '');
});

test('choreMoneyHtml: what a row pays, a personal goal tag, or nothing', () => {
  assert.match(sandbox.choreMoneyHtml({ kind: 'house', cents: 30 }), /\+30¢/);
  assert.match(sandbox.choreMoneyHtml({ kind: 'personal', cents: 0 }), /personal goal/);
  assert.equal(sandbox.choreMoneyHtml({ kind: 'once', cents: 0 }), '');
  assert.equal(sandbox.choreMoneyHtml({ title: 'no wallet' }), '');
});

test('pinRefusal: reads the server refusal off a failed write', () => {
  const e = (status, detail) => Object.assign(new Error('x'), { status, detail });
  assert.deepEqual({ ...sandbox.pinRefusal(e(403, { pin: 'required' })) }, { pin: 'required', wait_s: 0 });
  assert.deepEqual({ ...sandbox.pinRefusal(e(403, { pin: 'wrong' })) }, { pin: 'wrong', wait_s: 0 });
  assert.deepEqual({ ...sandbox.pinRefusal(e(429, { pin: 'locked', wait_s: 120 })) }, { pin: 'locked', wait_s: 120 });
  assert.equal(sandbox.pinRefusal(e(403, 'forbidden')), null);
  assert.equal(sandbox.pinRefusal(e(500, { pin: 'required' })), null);
  assert.equal(sandbox.pinRefusal(null), null);
});

test('pinLockMessage: says how long to wait in plain words', () => {
  assert.equal(sandbox.pinLockMessage(30), 'Too many wrong tries. Try again in a minute.');
  assert.equal(sandbox.pinLockMessage(240), 'Too many wrong tries. Try again in 4 minutes.');
});

test('buildChorePayload and choreToModel carry house chore or personal goal', () => {
  const p = sandbox.buildChorePayload({
    title: 'Read', repeat: 'daily', days: new Set(), assign: 'fixed', person: '3',
    rot: [], choreKind: 'personal',
  });
  assert.equal(p.kind, 'personal');
  assert.equal(sandbox.buildChorePayload({ title: 'x', repeat: 'daily', days: new Set(),
    assign: 'fixed', person: '3', rot: [], choreKind: 'nonsense' }).kind, 'house');
  const m = sandbox.choreToModel({ title: 'Read', icon: '', schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [], kind: 'personal' });
  assert.equal(m.choreKind, 'personal');
  const old = sandbox.choreToModel({ title: 'Bed', icon: '', schedule_kind: 'daily', days_mask: 0,
    assign_kind: 'fixed', fixed_person_id: 1, rotation_order: [] });
  assert.equal(old.choreKind, 'house');
});

/* ------------------------------------------------ chore wallet: Settings */

const RW_SET = {
  cents_per_chore: 30, bonus_on: true, milestones: [[100, 10000]],
  levels_on: false, levels: [[25, 35], [50, 40], [100, 45]],
  shields_on: true, shield_every: 20, shield_max: 2, fix_cutoff: '09:00',
};
const RW_STATE = (over) => ({
  settings: RW_SET, pin_set: true, feature_on: true,
  launch: { day: '2026-12-01', seeds: {} }, active: true,
  people: [
    { id: 1, name: 'Ava', color: '#5BC9F0', active: true, balance_cents: 1850,
      pace: { chores_per_week: 14, done_share: 0.9 },
      history: { weeks: [{ week_start: '2026-11-30', earned_cents: 420 }],
        ledger: [{ kind: 'payout', cents: 500, day: '2026-12-02', note: 'Paid' }] } },
    { id: 2, name: 'Ben', color: '#F05B5B', active: false, balance_cents: 90,
      pace: { chores_per_week: 0, done_share: 1 },
      history: { weeks: [], ledger: [] } },
  ],
  ...over,
});

test('rewardsCardHtml: no PIN is fine: it can turn on, and offers a PIN as optional', () => {
  // the owner, 2026-11-27: "I don't even need the pin. Could we make that optional?"
  const h = sandbox.rewardsCardHtml(RW_STATE({ pin_set: false, launch: null, active: false }));
  assert.match(h, /data-rw-enable/);
  assert.match(h, /data-rw-setpin/);
  assert.match(h, /optional/i);
  assert.doesNotMatch(h, /data-rw-clearpin/);
});

test('rewardsCardHtml: the running wallet shows the PIN choice both ways', () => {
  const off = sandbox.rewardsCardHtml(RW_STATE({ pin_set: false }));
  assert.match(off, /data-rw-save/);
  assert.match(off, /data-rw-setpin/);
  assert.match(off, /anyone/i);
  const on = sandbox.rewardsCardHtml(RW_STATE({ pin_set: true }));
  assert.match(on, /data-rw-clearpin/);
  assert.doesNotMatch(on, /data-rw-setpin/);
});

test('rewardsCardHtml: PIN set, not on yet, offers to turn it on and says what happens', () => {
  const h = sandbox.rewardsCardHtml(RW_STATE({ launch: null, active: false }));
  assert.match(h, /data-rw-enable/);
  assert.match(h, /keeps the streak/);
  assert.doesNotMatch(h, /data-rw-pay/);
});

test('rewardsCardHtml: on, shows the amounts, the projection host and each wallet', () => {
  const h = sandbox.rewardsCardHtml(RW_STATE());
  assert.match(h, /name="cents_per_chore" value="30"/);
  assert.match(h, /data-rw-ms-days="0" value="100"/);
  assert.match(h, /data-rw-ms-dollars="0" value="100"/);    // $100, shown in dollars
  assert.match(h, /name="fix_cutoff" value="09:00"/);
  assert.match(h, /id="rw-projection"/);
  assert.match(h, /data-rw-pay="1"/);
  assert.match(h, /\$18\.50/);
  // a person who has gone but still has money is listed under Inactive
  assert.match(h, /Inactive[\s\S]*Ben[\s\S]*90¢/);
  assert.match(h, /data-rw-save/);
});

/* the wallet summary (owner, 2026-11-27: a summary per kid, and "an easy way
   to reset after payout"; Paid zeroes the wallet, so it starts over itself) */
const SUM = (over) => ({ since: '2026-11-26', paid_before: false, chores: 12,
  chore_cents: 360, bonus_cents: 0, total_cents: 360, streak: 47, ...over });

test('walletSummaryText: chores, money and streak in plain words', () => {
  assert.equal(sandbox.walletSummaryText(SUM(), '2026-11-27'),
    'Since Nov 26: 12 chores, $3.60. Streak 47 days.');
  assert.equal(sandbox.walletSummaryText(SUM({ chores: 1, chore_cents: 30, total_cents: 30, streak: 1 }), '2026-11-27'),
    'Since Nov 26: 1 chore, 30¢. Streak 1 day.');
});

test('walletSummaryText: a bonus shows with the total, which is the balance', () => {
  assert.equal(sandbox.walletSummaryText(SUM({ bonus_cents: 10000, total_cents: 10360 }), '2026-11-27'),
    'Since Nov 26: 12 chores, $3.60, plus a $100 streak bonus. Total $103.60. Streak 47 days.');
  assert.equal(sandbox.walletSummaryText(SUM({ chores: 0, chore_cents: 0, bonus_cents: 10000, total_cents: 10000 }), '2026-11-27'),
    'Since Nov 26: no chores, plus a $100 streak bonus. Total $100.00. Streak 47 days.');
});

test('walletSummaryText: paid today starts over, with nothing yet', () => {
  assert.equal(sandbox.walletSummaryText(SUM({ since: '2026-11-27', paid_before: true, chores: 0,
    chore_cents: 0, total_cents: 0 }), '2026-11-27'),
  'Since today: no chores yet. Streak 47 days.');
});

test('walletSummaryText: a chore taken back after a payout says so', () => {
  assert.equal(sandbox.walletSummaryText(SUM({ chores: -1, chore_cents: -30, total_cents: -30 }), '2026-11-27'),
    'Since Nov 26: 1 chore taken back, -$0.30. Streak 47 days.');
});

test('walletSummaryText: a streak that could not be read says so, never 0 days', () => {
  assert.equal(sandbox.walletSummaryText(SUM({ streak: null, since: '2026-12-30' }), '2027-01-02'),
    'Since Dec 30, 2026: 12 chores, $3.60. Streak unavailable.');
  // no streak key at all (an older answer): nothing is said about it
  const { streak, ...noKey } = SUM();
  assert.equal(sandbox.walletSummaryText(noKey, '2026-11-27'), 'Since Nov 26: 12 chores, $3.60.');
  assert.equal(sandbox.walletSummaryText(null, '2026-11-27'), '');
});

test('walletSummaryText: missing or broken numbers are left out, never shown as $0.00', () => {
  const t = (over) => sandbox.walletSummaryText(SUM(over), '2026-11-27');
  // chore money missing: the count and the total still show
  assert.equal(t({ chore_cents: undefined }), 'Since Nov 26: 12 chores. Total $3.60. Streak 47 days.');
  assert.equal(t({ chore_cents: NaN }), 'Since Nov 26: 12 chores. Total $3.60. Streak 47 days.');
  // the count missing: the money still shows
  assert.equal(t({ chores: 'x' }), 'Since Nov 26: $3.60 from chores. Streak 47 days.');
  // a bonus that isn't a number is not a bonus of $0
  assert.equal(t({ bonus_cents: NaN }), 'Since Nov 26: 12 chores, $3.60. Total $3.60. Streak 47 days.');
  // nothing usable at all
  const h = t({ chores: null, chore_cents: null, bonus_cents: null, total_cents: null });
  assert.equal(h, 'Since Nov 26: summary unavailable. Streak 47 days.');
  assert.doesNotMatch(h, /\$0/);
  assert.doesNotMatch(t({ streak: NaN }), /0 days/);
});

test('payoutToastText: says what was paid for', () => {
  assert.equal(sandbox.payoutToastText({ cents: 360, summary: SUM() }, '2026-11-27'),
    'Paid $3.60 for 12 chores since Nov 26. The wallet is back to $0.');
  assert.equal(sandbox.payoutToastText({ cents: 10360, summary: SUM({ bonus_cents: 10000, total_cents: 10360 }) }, '2026-11-27'),
    'Paid $103.60 for 12 chores and a $100 streak bonus since Nov 26. The wallet is back to $0.');
  assert.equal(sandbox.payoutToastText({ cents: 360 }, '2026-11-27'),
    'Paid $3.60. The wallet is back to $0.');
  assert.equal(sandbox.payoutToastText(null, '2026-11-27'), 'Nothing to pay.');
  // an amount that isn't a number is not "$0.00"
  assert.equal(sandbox.payoutToastText({ summary: SUM() }, '2026-11-27'),
    'Paid for 12 chores since Nov 26. The wallet is back to $0.');
});

test('rewardsCardHtml: each wallet line has its summary under it', () => {
  const st = RW_STATE({ today: '2026-12-03' });
  st.people[0].summary = SUM({ total_cents: 1850, chore_cents: 1850, chores: 62, since: '2026-12-02' });
  const h = sandbox.rewardsCardHtml(st);
  assert.match(h, /\$18\.50[\s\S]*data-rw-pay="1"[\s\S]*class="rw-sum"[^>]*>Since Dec 2: 62 chores, \$18\.50\. Streak 47 days\.</);
  // Ben has no summary (an older answer): no empty line for him
  assert.equal((h.match(/class="rw-sum"/g) || []).length, 1);
});

test('rewardsCardHtml: the switch off in Features says so', () => {
  const h = sandbox.rewardsCardHtml(RW_STATE({ feature_on: false, active: false }));
  assert.match(h, /switched off/);
});

test('rewardsDraft: reads dollars as cents and keeps steps in order', () => {
  const d = sandbox.rewardsDraft({
    cents_per_chore: '35', bonus_on: true, levels_on: false, shields_on: true,
    fix_cutoff: '08:30',
    milestones: [{ days: '100', dollars: '100' }, { days: '25', dollars: '7.50' }, { days: '', dollars: '5' }],
    levels: [{ days: '50', cents: '40' }],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(d)), {
    cents_per_chore: 35, bonus_on: true, levels_on: false, shields_on: true,
    fix_cutoff: '08:30',
    milestones: [[25, 750], [100, 10000]],
    levels: [[50, 40]],
  });
});

test('projectionHtml: per child and family, week month year, both rows', () => {
  const proj = {
    children: [{ id: 1, perfect: { week: 1780, month: 7713, year: 92560 },
      usual: { week: 900, month: 3900, year: 46800 } }],
    total: { perfect: { week: 1780, month: 7713, year: 92560 },
      usual: { week: 900, month: 3900, year: 46800 } },
  };
  const h = sandbox.projectionHtml(proj, RW_STATE().people, true);
  assert.match(h, /Ava/);
  assert.match(h, /\$17\.80/);
  assert.match(h, /\$925\.60/);
  assert.match(h, /Perfect streak/);
  assert.match(h, /Usual pace, about/);
  assert.match(h, /Family/);
  assert.match(h, /not saved yet/);
  assert.doesNotMatch(sandbox.projectionHtml(proj, RW_STATE().people, false), /not saved yet/);
});

test('helpTopics: the chore wallet topic only when its switch is on', () => {
  const on = sandbox.helpTopics({ enabled: new Set(['chores', 'rewards']) });
  assert.ok(on.some((t) => t.id === 'wallet'));
  const off = sandbox.helpTopics({ enabled: new Set(['chores']) });
  assert.ok(!off.some((t) => t.id === 'wallet'));
});

/* ---- review fixes (2026-11-26) ---- */

test('walletLineHtml / rewardHeadHtml: a broken wallet says so', () => {
  assert.match(sandbox.walletLineHtml({ error: true }), /Wallet unavailable/);
  assert.equal(sandbox.rewardHeadHtml({ error: true }), '');
});

test('projectionHtml: names anyone it could not count', () => {
  const proj = { children: [], total: { perfect: { week: 0, month: 0, year: 0 },
    usual: { week: 0, month: 0, year: 0 } }, partial: [1] };
  assert.match(sandbox.projectionHtml(proj, RW_STATE().people, false), /Not counted: Ava/);
});


test('helpTopics: the wallet topic uses this wall’s own numbers', () => {
  const t = sandbox.helpTopics({ enabled: new Set(['chores', 'rewards']),
    wallet: { cents_per_chore: 25, fix_cutoff: '08:30', shields_on: true,
      shield_every: 10, shield_max: 1, bonus_on: true, milestones: [[50, 5000]] } });
  const text = t.find((x) => x.id === 'wallet').paras.join(' ');
  assert.match(text, /8:30/);
  assert.match(text, /every 10 good days, up to 1\b/);
  assert.match(text, /25¢/);
  assert.match(text, /\$50 at 50 days/);
  const off = sandbox.helpTopics({ enabled: new Set(['chores', 'rewards']),
    wallet: { cents_per_chore: 30, fix_cutoff: '09:00', shields_on: false,
      shield_every: 20, shield_max: 2, bonus_on: false, milestones: [] } });
  const t2 = off.find((x) => x.id === 'wallet').paras.join(' ');
  assert.doesNotMatch(t2, /Shields keep/);
  assert.match(t2, /9:00/);
});


test('walletHelpParas: off or unreadable says so instead of guessing numbers', () => {
  const none = sandbox.walletHelpParas(null).join(' ');
  assert.match(none, /turns it on in Settings/);
  assert.doesNotMatch(none, /9:00|30¢|20 good days/);
  const bad = sandbox.walletHelpParas({ error: true }).join(' ');
  assert.match(bad, /can’t be read right now/);
  const junk = sandbox.walletHelpParas({ cents_per_chore: 30, fix_cutoff: null, shields_on: true,
    shield_every: 20, shield_max: 2, bonus_on: true, milestones: null, levels: [] }).join(' ');
  assert.match(junk, /can’t be read right now/);
  assert.doesNotMatch(junk, /NaN/);
});

test('allChoresHtml: personal goals are tagged so the paid ones are easy to scan', () => {
  const base = { schedule_kind: 'daily', days_mask: 0, assign_kind: 'fixed',
    fixed_person_id: 1, rotation_order: [], active: 1 };
  const h = sandbox.allChoresHtml([
    { ...base, id: 1, title: 'Dishes', icon: '', kind: 'house' },
    { ...base, id: 2, title: 'Vitamins', icon: '', kind: 'personal' },
  ], [{ id: 1, name: 'Ava', active: 1 }]);
  assert.equal((h.match(/allch-goal/g) || []).length, 1);
  assert.match(h, /Vitamins<\/span><span class="allch-goal">personal goal/);
});

test('rewardsCardHtml: a lost PIN key says how to fix it and offers no money controls', () => {
  const h = sandbox.rewardsCardHtml(RW_STATE({ pin_set: true, pin_key_missing: true }));
  assert.match(h, /can’t be checked on this hub/);
  assert.doesNotMatch(h, /data-rw-pay|data-rw-save|data-rw-setpin/);
});

test('pinRefusal: reads a lost-key refusal', () => {
  assert.deepEqual({ ...sandbox.pinRefusal({ status: 403, detail: { pin: 'key_missing' } }) },
    { pin: 'key_missing', wait_s: 0 });
});


/* ------------------------------------------------------------ backups card */

const NOW_MS = Date.parse('2026-11-27T18:00:00Z');
const agoIso = (s) => new Date(NOW_MS - s * 1000).toISOString();
const BK = (extra = {}) => ({
  enabled: true, why_off: null, switch_on: true, scheduled: true, every_s: 3600,
  dir: '/mnt/nas/family-hub', running: false,
  last_ok: { at: agoIso(20 * 60), snapshot: 'hub-20261127-0940.db', bytes: 1843200 },
  last_error: null, settings: null, settings_error: null,
  counts: { hourly: 12, daily: 1, weekly: 1, monthly: 1, settings: 0 }, counts_error: null,
  keep: { hourly: 48, daily: 14, weekly: 8, monthly: 12, settings: 14 },
  password_set: false, password_problem: false, ...extra,
});

test('backupAgo: plain words, and a bad or future time never claims an age', () => {
  const A = (s) => sandbox.backupAgo(agoIso(s), NOW_MS);
  assert.equal(A(20), 'just now');
  assert.equal(A(-300), 'just now', 'a clock a little ahead is not "in 5 minutes"');
  assert.equal(A(60), '1 minute ago');
  assert.equal(A(20 * 60), '20 minutes ago');
  assert.equal(A(3600), '1 hour ago');
  assert.equal(A(5 * 3600), '5 hours ago');
  assert.equal(A(47 * 3600), '47 hours ago');
  assert.equal(A(3 * 86400), '3 days ago');
  assert.equal(sandbox.backupAgo(null, NOW_MS), 'at a time the hub could not read');
  assert.equal(sandbox.backupAgo('garbage', NOW_MS), 'at a time the hub could not read');
});

test('backupsCardHtml: loading, then the working state with every row', () => {
  assert.match(sandbox.backupsCardHtml(null, NOW_MS), /Loading/);
  const html = sandbox.backupsCardHtml(BK(), NOW_MS);
  assert.match(html, /On\. A copy every hour\./);
  assert.match(html, /\/mnt\/nas\/family-hub/);
  assert.match(html, /Last good backup<\/span><button class="tip-btn"[^]*?<\/div><span class="hint">20 minutes ago/);
  assert.match(html, /12 hourly, 1 daily, 1 weekly and 1 monthly\. It keeps up to 48, 14, 8 and 12\./);
  assert.match(html, /<button class="btn-primary" type="button" data-bk-run="1">Back up now<\/button>/);
  assert.match(html, /Set a backup password/);
  assert.match(html, /Write this down\. You need it to restore the settings files on a new box\./);
  assert.match(html, /type="password" name="bk_pw"/);
  assert.match(html, /type="password" name="bk_pw2"/);
  assert.ok(!/old password/.test(html), 'nothing about an old password before one is set');
  assert.equal((html.match(/<div/g) || []).length, (html.match(/<\/div>/g) || []).length, 'balanced divs');
});

test('backupsCardHtml: no copy yet, and paused hourly runs', () => {
  const html = sandbox.backupsCardHtml(BK({ last_ok: null, scheduled: false }), NOW_MS);
  assert.match(html, /None yet/);
  assert.match(html, /Hourly copies are paused on this hub\. Back up now still works\./);
});

test('backupsCardHtml: each reason it is off, with Back up now disabled', () => {
  const cases = {
    switched_off: /Switched off\. Turn it on under Features &amp; integrations above\./,
    no_folder: /No backup folder is set up\. Set BACKUP_DIR on the hub box \(see Backup in the README\)\./,
    not_mounted: /No backup folder is mounted into the hub\. Set BACKUP_DIR on the hub box \(see Backup in the README\)\./,
    not_writable: /The hub can’t write to the backup folder\. It must belong to the hub’s user on the box \(see Backup in the README\)\./,
    demo: /Off in the demo\./,
  };
  for (const [why, re] of Object.entries(cases)) {
    const html = sandbox.backupsCardHtml(BK({ enabled: false, why_off: why, counts: null }), NOW_MS);
    assert.match(html, re, why);
    assert.match(html, /data-bk-run="1" disabled>Back up now/, `${why}: button disabled`);
  }
});

test('backupsCardHtml: a failure newer than the last good copy is shown; an old one is not', () => {
  const fresh = sandbox.backupsCardHtml(BK({ last_error: { at: agoIso(5 * 60),
    message: 'The backup folder is full <b>x</b>.' } }), NOW_MS);
  assert.match(fresh, /The last try failed 5 minutes ago\. The backup folder is full &lt;b&gt;x&lt;\/b&gt;\./);
  assert.ok(!fresh.includes('<b>x'), 'the message is escaped');
  const old = sandbox.backupsCardHtml(BK({ last_error: { at: agoIso(3 * 3600), message: 'old' } }), NOW_MS);
  assert.ok(!/The last try failed/.test(old));
  const never = sandbox.backupsCardHtml(BK({ last_ok: null, last_error: { at: agoIso(60), message: 'nope' } }), NOW_MS);
  assert.match(never, /The last try failed 1 minute ago\. nope/);
});

test('backupsCardHtml: the settings files row for each password state', () => {
  const unset = sandbox.backupsCardHtml(BK(), NOW_MS);
  assert.match(unset, /Not saved\. Set a backup password below to save them too\./);
  const waiting = sandbox.backupsCardHtml(BK({ password_set: true }), NOW_MS);
  assert.match(waiting, /Saved with the next backup\./);
  assert.match(waiting, /Change the backup password/);
  assert.match(waiting, /Copies saved before a change still need the old password\./);
  const saved = sandbox.backupsCardHtml(BK({ password_set: true,
    settings: { at: agoIso(2 * 3600), file: 'settings-20261127-0800.fhb' } }), NOW_MS);
  assert.match(saved, /Saved with the backup password, 2 hours ago\./);
  const problem = sandbox.backupsCardHtml(BK({ password_problem: true }), NOW_MS);
  assert.match(problem, /Not saved\. Set the backup password again, because its key file on the hub can’t be used\./);
  const err = sandbox.backupsCardHtml(BK({ password_set: true, settings_error: 'Couldn’t read .env <x>.' }), NOW_MS);
  assert.match(err, /Couldn’t read \.env &lt;x&gt;\./);
});

test('backupsCardHtml: with a password set, a clear status row, what the last copy holds, and the form folded away', () => {
  const at = agoIso(3 * 86400);
  const copy = agoIso(20 * 60);
  const html = sandbox.backupsCardHtml(BK({ password_set: true, password_set_at: at,
    settings: { at: copy, file: 'settings-x.fhb',
      files: ['config.json', 'env', 'pin.key', 'token.json', 'wyze.env'] } }), NOW_MS);
  const day = sandbox.restoreTestDate(at, NOW_MS);
  assert.match(html, new RegExp(`Backup password</span><button class="tip-btn"[^]*?</div><span class="hint">Set ✓ on ${day}\\.`));
  const clock = sandbox.backupClock(copy);
  assert.match(clock, /\d{1,2}:\d{2}/);
  assert.ok(html.includes(`Settings locked into the ${clock} backup: config.json, env, pin.key, token.json, wyze.env.`));
  assert.match(html, /<button class="padmin-btn" type="button" data-bk-changepw="1">Change password<\/button>/);
  assert.match(html, /class="theme-ctl stack bk-pw hidden" id="bk-pw-form"/, 'the form starts folded');
  assert.match(html, /type="password" name="bk_pw"/, 'still in the page, so the save wiring finds it');
  assert.equal((html.match(/<div/g) || []).length, (html.match(/<\/div>/g) || []).length, 'balanced divs');
});

test('backupsCardHtml: a set password with no stored date still says Set, and an unset one keeps the open form', () => {
  const noDate = sandbox.backupsCardHtml(BK({ password_set: true }), NOW_MS);
  assert.match(noDate, /Backup password<\/span><button class="tip-btn"[^]*?<\/div><span class="hint">Set ✓<\/span>/);
  const unset = sandbox.backupsCardHtml(BK(), NOW_MS);
  assert.ok(!/data-bk-changepw/.test(unset) && !/Set ✓/.test(unset));
  assert.ok(!/bk-pw hidden/.test(unset), 'no folded form when nothing is set');
  assert.match(unset, /Set a backup password/);
  const bad = sandbox.backupsCardHtml(BK({ password_problem: true }), NOW_MS);
  assert.ok(!/Set ✓/.test(bad) && !/bk-pw hidden/.test(bad), 'an unusable key still shows the open form');
});

test('backupsCardHtml: a running backup and an unreadable folder', () => {
  const html = sandbox.backupsCardHtml(BK({ running: true, counts: null,
    counts_error: 'Couldn’t read the backup folder (gone).' }), NOW_MS);
  assert.match(html, /data-bk-run="1" disabled>Backing up/);
  assert.match(html, /Couldn’t read the backup folder \(gone\)\./);
});

test('backupsCardHtml: the folder name is escaped', () => {
  const html = sandbox.backupsCardHtml(BK({ dir: '/mnt/"x"<y>' }), NOW_MS);
  assert.ok(html.includes('/mnt/&quot;x&quot;&lt;y&gt;') && !html.includes('<y>'));
});

test('backupPasswordProblem: length and a matching second copy', () => {
  assert.equal(sandbox.backupPasswordProblem('short', 'short'), 'The password needs at least 8 characters.');
  assert.equal(sandbox.backupPasswordProblem('long enough', 'long enougj'), 'Those didn’t match. Nothing was saved.');
  assert.equal(sandbox.backupPasswordProblem('long enough', 'long enough'), null);
  assert.equal(sandbox.backupPasswordProblem('x'.repeat(1025), 'x'.repeat(1025)), 'The password can be 1024 characters at most.');
});

test('helpTopics: the Backups topic points at Settings and the password', () => {
  const t = topic(ctxOf([]), 'backup').paras.join(' ');
  assert.match(t, /Settings, under Backups, shows where the copies go/);
  assert.match(t, /backup password/);
  assert.match(t, /Once a month the hub also checks that its newest copy really opens/);
  assert.match(t, /Test restore now/);
});

test('backups card text: no em or en dashes in any state', () => {
  const states = [null, BK(), BK({ password_set: true, settings: { at: agoIso(60), file: 'x' } }),
    BK({ password_problem: true, last_error: { at: agoIso(1), message: 'm' } })];
  for (const why of ['switched_off', 'no_folder', 'not_mounted', 'not_writable', 'demo']) {
    states.push(BK({ enabled: false, why_off: why }));
  }
  const text = states.map((s) => sandbox.backupsCardHtml(s, NOW_MS)).join(' ');
  assert.ok(!text.includes(String.fromCharCode(0x2014)), 'no em dashes');
  assert.ok(!text.includes(String.fromCharCode(0x2013)), 'no en dashes');
});

test('backupsCardHtml: a folder that lost its marker asks about the NAS and offers Use this folder', () => {
  const html = sandbox.backupsCardHtml(BK({ enabled: false, why_off: 'marker_missing' }), NOW_MS);
  assert.match(html, /The backup folder isn’t the one backups went to before\. Is the NAS mounted\?/);
  assert.match(html, /<button class="padmin-btn" type="button" data-bk-adopt="1">Use this folder<\/button>/);
  assert.match(html, /data-bk-run="1" disabled>/);
  assert.ok(!/data-bk-adopt/.test(sandbox.backupsCardHtml(BK(), NOW_MS)), 'no adopt button when all is well');
});

/* ------------------------------------------------------ backups: restore test */

// local wall-clock instants, so the dates read the same under TZ=UTC
const localIso = (y, m, d, h = 12) => new Date(y, m - 1, d, h).toISOString();
const localMs = (y, m, d, h = 12) => new Date(y, m - 1, d, h).getTime();

test('restoreTestDate: a plain date, the year only when it is not this one', () => {
  const now = localMs(2026, 11, 27);
  assert.equal(sandbox.restoreTestDate(localIso(2026, 11, 3), now), 'November 3');
  assert.equal(sandbox.restoreTestDate(localIso(2025, 12, 30), now), 'December 30, 2025');
  assert.equal(sandbox.restoreTestDate('garbage', now), null);
  assert.equal(sandbox.restoreTestDate(null, now), null);
});

test('backupsCardHtml: no restore test yet says when the first one runs', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test: null }), NOW_MS);
  assert.match(html, /Restore test<\/span><button class="tip-btn"[^]*?<\/div><span class="hint">Not tested yet\. The first test runs a day after the first backup\./);
  assert.match(html, /<button class="padmin-btn" type="button" data-bk-test="1">Test restore now<\/button>/);
});

test('backupsCardHtml: a passed restore test shows its date', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test: { at: localIso(2026, 11, 3), ok: true,
    snapshot: 'hub-20261103-0930.db', reason: null } }), localMs(2026, 11, 27));
  assert.match(html, /Restore test<\/span><button class="tip-btn"[^]*?<\/div><span class="hint">Last restore test passed November 3\./);
  assert.ok(!/bk-test-error/.test(html));
});

test('backupsCardHtml: a failed restore test shows the reason, escaped', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test: { at: localIso(2026, 11, 26), ok: false,
    snapshot: 'hub-x.db', reason: 'The newest backup (hub-x.db) has <b>2</b> people.' } }), localMs(2026, 11, 27));
  assert.match(html, /Last restore test failed November 26\./);
  assert.match(html, /<div class="hint bk-error bk-test-error" role="status">The newest backup \(hub-x\.db\) has &lt;b&gt;2&lt;\/b&gt; people\.<\/div>/);
  assert.ok(!html.includes('<b>2'));
});

test('backupsCardHtml: Test restore now is off with nothing to test, while busy, or when off', () => {
  assert.match(sandbox.backupsCardHtml(BK({ last_ok: null }), NOW_MS), /data-bk-test="1" disabled>Test restore now/);
  assert.match(sandbox.backupsCardHtml(BK({ running: true }), NOW_MS), /data-bk-test="1" disabled>Test restore now/);
  assert.match(sandbox.backupsCardHtml(BK({ enabled: false, why_off: 'switched_off' }), NOW_MS),
    /data-bk-test="1" disabled>Test restore now/);
});

test('backupsCardHtml: a restore test time the hub could not read still says pass or fail', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test: { at: 'garbage', ok: false, reason: 'r' } }), NOW_MS);
  assert.match(html, /Last restore test failed on a date the hub could not read\./);
});

test('backupsCardHtml: a pass that skipped the settings says so, not a full pass', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test: { at: localIso(2026, 11, 27), ok: true,
    reason: null, settings_checked: false, settings_skip: 'no backup password set' } }), localMs(2026, 11, 27, 18));
  assert.match(html, /Restore test<\/span><button class="tip-btn"[^]*?<\/div><span class="hint">Database restored on November 27\. Settings weren’t checked: no backup password set\./);
  assert.ok(!/Last restore test passed/.test(html));
  const full = sandbox.backupsCardHtml(BK({ restore_test: { at: localIso(2026, 11, 27), ok: true,
    reason: null, settings_checked: true, settings_skip: null } }), localMs(2026, 11, 27, 18));
  assert.match(full, /Last restore test passed November 27\./);
  const esc = sandbox.backupsCardHtml(BK({ restore_test: { at: localIso(2026, 11, 27), ok: true,
    settings_checked: false, settings_skip: '<i>x</i>' } }), localMs(2026, 11, 27, 18));
  assert.ok(esc.includes('&lt;i&gt;x&lt;/i&gt;') && !esc.includes('<i>x'));
});

test('backupsCardHtml: an overdue restore test is flagged', () => {
  const html = sandbox.backupsCardHtml(BK({ restore_test_overdue: true, restore_test: {
    at: agoIso(40 * 86400), ok: true, settings_checked: true } }), NOW_MS);
  assert.match(html, /<div class="hint bk-error" role="status">No restore test has passed in over 35 days\. Check the hub’s log\.<\/div>/);
  assert.ok(!/over 35 days/.test(sandbox.backupsCardHtml(BK(), NOW_MS)));
});

test('restore test text: no em or en dashes, balanced divs', () => {
  const now = localMs(2026, 11, 27);
  const states = [BK({ restore_test: null }),
    BK({ restore_test: { at: localIso(2026, 11, 3), ok: true, reason: null } }),
    BK({ restore_test: { at: localIso(2026, 11, 3), ok: false, reason: 'x' } })];
  for (const s of states) {
    const html = sandbox.backupsCardHtml(s, now);
    assert.ok(!html.includes(String.fromCharCode(0x2014)), 'no em dashes');
    assert.ok(!html.includes(String.fromCharCode(0x2013)), 'no en dashes');
    assert.equal((html.match(/<div/g) || []).length, (html.match(/<\/div>/g) || []).length);
  }
});

/* ------------------------------------------------ Settings info tips */

const TIP_PROJ = {
  children: [{ id: 1, perfect: { week: 100, month: 433, year: 5200 }, usual: { week: 50, month: 217, year: 2600 } }],
  total: { perfect: { week: 100, month: 433, year: 5200 }, usual: { week: 50, month: 217, year: 2600 } },
};

// Every Settings builder in every state that draws a different set of labels.
const tipSurfaces = () => [
  sandbox.dimCardHtml(null),
  sandbox.dimCardHtml({ dim: { hours: { on: true, start: '22:00', end: '06:00' },
    away: { enabled: true, state: 'home', home: 2 } } }),
  sandbox.rewardsCardHtml(RW_STATE()),
  sandbox.rewardsCardHtml(RW_STATE({ pin_set: false })),
  sandbox.rewardsCardHtml(RW_STATE({ launch: null, active: false })),
  sandbox.rewardsCardHtml(RW_STATE({ settings: { ...RW_SET, levels_on: true } })),
  sandbox.projectionHtml(TIP_PROJ, RW_STATE().people, true),
  sandbox.backupsCardHtml(BK(), NOW_MS),
  sandbox.backupsCardHtml(BK({ password_set: true, restore_test: { at: agoIso(86400), ok: true } }), NOW_MS),
  sandbox.backupsCardHtml(BK({ enabled: false, why_off: 'no_folder', dir: null, last_ok: null, counts: null }), NOW_MS),
  sandbox.caldavPanelHtml(null, {}),
  sandbox.caldavPanelHtml({ id: 'icloud_caldav', account: 'bot@example.com', readonly: true }, { collections: [] }),
];

// Every setting label in the markup: a .settings-k (its badge included), the
// Wallets head, or a form field's <label>.
const labelsIn = (html) => {
  const out = [];
  const re = /<span class="settings-k">[\s\S]*?<\/span>(?=<button)|<span class="padmin-head-label">Wallets<\/span>|<label for="[^"]+">[^<]*<\/label>|<(div|span) class="settings-k">[^<]*<\/\1>/g;
  let m;
  while ((m = re.exec(html))) out.push(m[0]);
  return out;
};

test('info tips: the text map is filled in, plain and short', () => {
  const tips = vm.runInContext('SETTING_TIPS', sandbox);
  const keys = Object.keys(tips);
  assert.ok(keys.length >= 40, `a tip for every setting (have ${keys.length})`);
  for (const k of keys) {
    const t = tips[k];
    assert.equal(typeof t, 'string', k);
    const sentences = t.split(/(?<=[.!?])\s+(?=[A-Z0-9])/).filter(Boolean);
    assert.ok(sentences.length >= 1 && sentences.length <= 3, `${k}: 1 to 3 sentences, got ${sentences.length}`);
    assert.ok(t.length <= 320, `${k}: short enough to read at a glance (${t.length} chars)`);
    assert.ok(!t.includes(String.fromCharCode(0x2014)) && !t.includes(String.fromCharCode(0x2013)), `${k}: no em or en dashes`);
    assert.doesNotMatch(t, /colour|centre|favourite|licence/i, `${k}: American spelling`);
    assert.doesNotMatch(t, /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/, `${k}: no addresses`);
    assert.doesNotMatch(t, /:\s/, `${k}: no colons inside a sentence`);
  }
});

test('info tips: one ⓘ button per label, wired for a screen reader, closed by default', () => {
  vm.runInContext("infoTipOpen = ''", sandbox);
  const h = sandbox.tipLabelHtml('Shields');
  assert.match(h, /<span class="tip-head"><span class="settings-k">Shields<\/span><button class="tip-btn" type="button"/);
  assert.match(h, /data-tip="Shields"/);
  assert.match(h, /aria-expanded="false"/);
  assert.match(h, /aria-controls="tip-shields"/);
  assert.match(h, /aria-label="What is Shields\?"/);
  assert.match(h, /<div class="tip-body" id="tip-shields" data-tip-body="Shields" hidden>/);
  assert.match(h, /A shield saves/, 'the explanation is in the markup, so opening it needs no fetch');
});

test('info tips: the open tip stays open through a repaint, and only that one', () => {
  vm.runInContext("infoTipOpen = 'Shields'", sandbox);
  try {
    const h = sandbox.rewardsCardHtml(RW_STATE());
    assert.match(h, /data-tip="Shields" aria-expanded="true"/);
    assert.match(h, /id="tip-shields" data-tip-body="Shields">/, 'not hidden');
    assert.equal((h.match(/aria-expanded="true"/g) || []).length, 1, 'one open at a time');
    assert.equal((h.match(/class="tip-body"[^>]* hidden>/g) || []).length,
      (h.match(/class="tip-body"/g) || []).length - 1);
  } finally {
    vm.runInContext("infoTipOpen = ''", sandbox);
  }
});

test('info tips: a tap opens, the same tap closes, another tap moves it', () => {
  assert.equal(sandbox.infoTipNextOpen('', 'Shields'), 'Shields');
  assert.equal(sandbox.infoTipNextOpen('Shields', 'Shields'), '');
  assert.equal(sandbox.infoTipNextOpen('Shields', 'Theme'), 'Theme');
});

test('info tips: ids are safe and unique for every key', () => {
  const keys = Object.keys(vm.runInContext('SETTING_TIPS', sandbox));
  const ids = keys.map((k) => sandbox.infoTipId(k));
  ids.forEach((id) => assert.match(id, /^tip-[a-z0-9-]+$/));
  assert.equal(new Set(ids).size, ids.length, 'no two tips share an id');
  assert.equal(sandbox.infoTipId('integ:icloud_caldav'), 'tip-integ-icloud-caldav');
});

test('info tips: every setting label on every Settings card has its tip', () => {
  const tips = vm.runInContext('SETTING_TIPS', sandbox);
  let seen = 0;
  for (const html of tipSurfaces()) {
    for (const label of labelsIn(html)) {
      seen += 1;
      // the label sits in a tip head with its button right after it
      const at = html.indexOf(label);
      assert.ok(html.slice(Math.max(0, at - 40), at).endsWith('<span class="tip-head">'),
        `label has no tip head: ${label}`);
      const btn = /^<button class="tip-btn" type="button" data-tip="([^"]+)"/.exec(html.slice(at + label.length));
      assert.ok(btn, `label has no tip button after it: ${label}`);
      assert.ok(tips[btn[1].replace(/&amp;/g, '&')], `tip text missing for ${btn[1]}`);
    }
  }
  assert.ok(seen >= 25, `scanned the real labels (${seen})`);
});

test('info tips: a key with no text still draws the label, with no empty button', () => {
  const h = sandbox.tipLabelHtml('No such setting');
  assert.match(h, /<span class="settings-k">No such setting<\/span>/);
  assert.doesNotMatch(h, /tip-btn/);
});
