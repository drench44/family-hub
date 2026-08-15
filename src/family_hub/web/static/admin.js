'use strict';

/* Family Hub — phone management page. People + chores CRUD against the
   /api/admin/* routes. Every mutation refreshes from GET /api/admin/state;
   422 details render inline (never alert()). Depends on common.js globals. */

// SWATCHES, paintSwatches, DAY_LABELS, buildChoreForm and freshChoreModel now
// live in common.js, shared with the hub wall — see common.js.

let people = [];
let chores = [];
const armTimers = new Map();   // deactivate confirm-arm timers, keyed per button

async function load() {
  const st = await j('/api/admin/state');
  people = st.people;
  chores = st.chores;
  renderPeople();
  renderChores();
  rebuildAddChore();
}

async function postJSON(url, method, body, errEl, onok) {
  errEl.classList.add('hidden');
  errEl.textContent = '';
  try {
    await j(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    onok();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

/* Two-tap confirm: first tap arms (turns crit), a tap within 3s commits. */
function armConfirm(btn, run) {
  if (armTimers.has(btn)) {
    clearTimeout(armTimers.get(btn));
    armTimers.delete(btn);
    btn.classList.remove('arm');
    run();
    return;
  }
  btn.classList.add('arm');
  btn.textContent = 'Tap again';
  armTimers.set(btn, setTimeout(() => {
    armTimers.delete(btn);
    btn.classList.remove('arm');
    load();     // restore the original label
  }, 3000));
}

/* ---------------------------------------------------------------- people */

const addPerson = { color: SWATCHES[0] };

function initPersonAdd() {
  const sw = document.getElementById('person-swatches');
  const paint = () => paintSwatches(sw, addPerson.color, (hx) => { addPerson.color = hx; paint(); });
  paint();
  document.getElementById('person-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const name = document.getElementById('person-name').value.trim();
    postJSON('/api/admin/people', 'POST', { name, color: addPerson.color },
      document.getElementById('person-error'), () => {
        document.getElementById('person-name').value = '';
        load();
      });
  });
}

function renderPeople() {
  document.getElementById('people-list').innerHTML = people.map((p) =>
    `<div class="admin-card${p.active ? '' : ' inactive'}" data-person="${p.id}">`
    + `<div class="row-between">`
    + `<span class="person-name" style="color:${safeColor(p.color)}">${escapeHtml(p.name)}</span>`
    + `<span class="grow"></span>`
    + `<button class="btn-quiet" type="button" data-act="edit-person">Edit</button>`
    + `<button class="btn-quiet" type="button" data-act="toggle-person">${p.active ? 'Deactivate' : 'Activate'}</button>`
    + `</div><div class="admin-form hidden" data-editor></div></div>`).join('');
}

function openPersonEditor(card, p) {
  const box = card.querySelector('[data-editor]');
  let color = p.color;
  box.innerHTML = `<div class="field"><label>Name / nickname</label>`
    + `<input class="txt-input" data-ename maxlength="30" value="${escapeHtml(p.name)}"></div>`
    + `<div class="field"><label>Color</label><div class="swatches" data-eswatches></div></div>`
    + `<div class="form-error hidden" data-eerror></div>`
    + `<button class="btn-primary" type="button" data-esave>Save</button>`;
  const sw = box.querySelector('[data-eswatches]');
  const paint = () => paintSwatches(sw, color, (hx) => { color = hx; paint(); });
  paint();
  box.classList.remove('hidden');
  box.querySelector('[data-esave]').onclick = () => {
    const name = box.querySelector('[data-ename]').value.trim();
    postJSON(`/api/admin/people/${p.id}`, 'PATCH', { name, color },
      box.querySelector('[data-eerror]'), load);
  };
}

document.getElementById('people-list').addEventListener('click', (e) => {
  const card = e.target.closest('[data-person]');
  if (!card) return;
  const p = people.find((x) => x.id === Number(card.dataset.person));
  const act = e.target.closest('[data-act]');
  if (!act) return;
  if (act.dataset.act === 'edit-person') openPersonEditor(card, p);
  else if (act.dataset.act === 'toggle-person') {
    armConfirm(act, () => postJSON(`/api/admin/people/${p.id}`, 'PATCH',
      { active: p.active ? 0 : 1 }, document.getElementById('person-error'), load));
  }
});

/* ---------------------------------------------------------------- chores */

function rebuildAddChore() {
  buildChoreForm(document.getElementById('chore-add'), freshChoreModel(), 'Add chore',
    (body, errEl) => postJSON('/api/admin/chores', 'POST', body, errEl, load), people);
}

function schedText(ch) {
  if (ch.schedule_kind === 'daily') return 'Daily';
  // A one-time chore stores its single due date as rotation_epoch.
  if (ch.schedule_kind === 'once') return `Once · ${ch.rotation_epoch}`;
  return DAY_LABELS.filter((_, i) => (ch.days_mask >> i) & 1).join('·');
}

function assignText(ch) {
  if (ch.assign_kind === 'fixed') {
    const p = people.find((x) => x.id === ch.fixed_person_id);
    return p ? p.name : '—';
  }
  return 'Rotation: ' + ch.rotation_order.map((id) => {
    const p = people.find((x) => x.id === id);
    return p ? p.name : '?';
  }).join(' → ');
}

function renderChores() {
  document.getElementById('chore-list').innerHTML = chores.map((ch) =>
    `<div class="admin-card${ch.active ? '' : ' inactive'}" data-chore="${ch.id}">`
    + `<div class="row-between">`
    + `<span class="grow"><span class="chore-icon">${escapeHtml(ch.icon)}</span> `
    + `<span class="chore-title">${escapeHtml(ch.title)}</span></span>`
    + `<button class="btn-quiet" type="button" data-act="edit-chore">Edit</button>`
    + `<button class="btn-quiet" type="button" data-act="toggle-chore">${ch.active ? 'Deactivate' : 'Activate'}</button>`
    + `</div>`
    + `<div class="hint chore-meta">${escapeHtml(schedText(ch))} · ${escapeHtml(assignText(ch))}</div>`
    + `<div class="admin-form hidden" data-editor></div></div>`).join('');
}


document.getElementById('chore-list').addEventListener('click', (e) => {
  const card = e.target.closest('[data-chore]');
  if (!card) return;
  const ch = chores.find((x) => x.id === Number(card.dataset.chore));
  const act = e.target.closest('[data-act]');
  if (!act) return;
  if (act.dataset.act === 'edit-chore') {
    const box = card.querySelector('[data-editor]');
    box.classList.remove('hidden');
    buildChoreForm(box, choreToModel(ch), 'Save',
      (body, errEl) => postJSON(`/api/admin/chores/${ch.id}`, 'PATCH', body, errEl, load), people);
  } else if (act.dataset.act === 'toggle-chore') {
    armConfirm(act, () => postJSON(`/api/admin/chores/${ch.id}`, 'PATCH',
      { active: ch.active ? 0 : 1 }, document.getElementById('chore-add').querySelector('.f-error'), load));
  }
});

/* --------------------------------------------------------------- display */
// The Light/Dark, accent and None/Wells controls: persist per-device via the
// theme.js setters (which also re-stamp <html> live), and reflect the live
// state onto the buttons. The house default (from /api/hub) is applied only
// when this device has NO override yet — WITHOUT persisting it (see applyHouse).

function reflectDisplayControls() {
  const ctl = document.getElementById('theme-ctl');
  if (!ctl) return;
  const el = document.documentElement;
  const mode = el.getAttribute('data-theme');
  const accent = el.getAttribute('data-accent');
  const cols = el.getAttribute('data-cols');
  ctl.querySelectorAll('[data-theme-set]').forEach((b) =>
    b.classList.toggle('on', b.dataset.themeSet === mode));
  ctl.querySelectorAll('[data-c]').forEach((b) =>
    b.classList.toggle('on', b.dataset.c === accent));
  ctl.querySelectorAll('[data-cols-set]').forEach((b) =>
    b.classList.toggle('on', b.dataset.colsSet === cols));
}

function applyHouseTheme(theme) {
  if (!theme || typeof theme !== 'object') return;
  try { window.FH_THEME = theme; } catch (e) { /* reference only */ }
  const noOverride = (k) => {
    try { return localStorage.getItem(k) === null; } catch (e) { return true; }
  };
  if (theme.mode && noOverride('fh.theme')) stampTheme(theme.mode);
  if (theme.accent && noOverride('fh.accent')) stampAccent(theme.accent);
  if (theme.columns && noOverride('fh.cols')) stampColumns(theme.columns);
}

function initDisplayControls() {
  const ctl = document.getElementById('theme-ctl');
  if (!ctl) return;
  ctl.addEventListener('click', (e) => {
    const t = e.target.closest('[data-theme-set]');
    if (t) { setTheme(t.dataset.themeSet); reflectDisplayControls(); return; }
    const a = e.target.closest('[data-c]');
    if (a) { setAccent(a.dataset.c); reflectDisplayControls(); return; }
    const c = e.target.closest('[data-cols-set]');
    if (c) { setColumns(c.dataset.colsSet); reflectDisplayControls(); return; }
  });
  reflectDisplayControls();
  // Pull the house default; apply it only if this device hasn't overridden.
  j('/api/hub').then((hub) => {
    applyHouseTheme(hub.theme);
    reflectDisplayControls();
  }).catch(() => { /* offline: keep the shipped/stored theme */ });
}

initPersonAdd();
initDisplayControls();
load();
