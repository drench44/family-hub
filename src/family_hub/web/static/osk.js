'use strict';

/* Family Hub - on-screen keyboard (OSK) for the touchscreen wall.

   The wall is an HP all-in-one touchscreen (Firefox under GNOME/Wayland) with NO
   physical keyboard, so tapping a text field has to summon a keyboard the family
   can type on. The browser shows no usable native keyboard there, so we build
   our own.

   Loaded as a classic script AFTER common.js (for oskApplyKey, the pure text
   transform) and BEFORE hub.js. It wires itself entirely through delegated
   document listeners, so hub.js stays untouched.

   KIOSK-ONLY: this exists solely for the keyboard-less wall. Phones and laptops
   already have their own keyboard, and ours would just stack under it - so the
   whole thing no-ops unless the wall has latched ?kiosk=1 (see the gate below),
   NOT on touch detection (a phone is a touch device too). */
(function () {
  // No document (the vm test sandbox): do nothing. The pure transform lives in
  // common.js (oskApplyKey) and is tested there, so the gate can bail before
  // building any DOM.
  if (typeof document === 'undefined' || typeof navigator === 'undefined') return;

  // KIOSK-ONLY. This keyboard exists solely for the keyboard-less WALL. Phones
  // and laptops already have a real (or native on-screen) keyboard, and showing
  // our board there just stacks under theirs - so we do NOT gate on touch (a
  // phone is a touch device too). We activate only when the wall has latched
  // kiosk mode: it opens the hub once with ?kiosk=1, which we persist to
  // localStorage so every later navigation stays in kiosk mode. ?kiosk=0 clears
  // it. (The wall itself is an HP touchscreen on Firefox/Wayland that reports NO
  // touch to the browser anyway - the panel arrives as a mouse - so an explicit
  // flag, not touch detection, is the only reliable signal there.)
  let kiosk = false;
  try {
    const q = location.search;
    if (/[?&]kiosk=0(?:&|$)/.test(q)) localStorage.removeItem('oskKiosk');   // escape hatch
    else if (/[?&]kiosk=1(?:&|$)/.test(q)) localStorage.setItem('oskKiosk', '1');
    kiosk = localStorage.getItem('oskKiosk') === '1';
  } catch (e) {
    // Storage blocked (private mode / locked-down profile) leaves the wall with
    // NO keyboard, so leave a breadcrumb instead of failing silently.
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[osk] kiosk flag unreadable (storage blocked); on-screen keyboard may be off', e);
    }
  }

  if (!kiosk) return;

  // The inputs the keyboard serves. A `.txt-input` <select> (the chore person
  // picker) also carries the class, so oskTypeable() below excludes non-text
  // controls - the keyboard must only attach to something you can type into.
  const OSK_SEL = '#todo-add-input, .txt-input';
  // Only input types whose selection API works: email/number report null
  // selectionStart, which would land every keystroke at index 0 (reversed text).
  const TYPEABLE_TYPES = ['text', 'search', 'url', 'tel', ''];

  // Layer layouts. 'letters' carries a digit row on top for quick numeric entry
  // on the wall; '?123' opens a two-page symbol set; the 😊 key opens a curated
  // emoji grid. Symbol rows are explicit arrays (not string.split) so an
  // apostrophe or backslash key is unambiguous.
  const LETTER_ROWS = [
    'qwertyuiop'.split(''),
    'asdfghjkl'.split(''),
    'zxcvbnm'.split(''),
  ];
  const DIGIT_ROW = '1234567890'.split('');
  const SYM1_ROWS = [
    '1234567890'.split(''),
    ['-', '/', ':', ';', '(', ')', '$', '&', '@', '"'],
    ['.', ',', '?', '!', "'", '+', '=', '_'],
  ];
  const SYM2_ROWS = [
    ['[', ']', '{', '}', '#', '%', '^', '*', '+', '='],
    ['_', '\\', '|', '~', '<', '>', '€', '£', '¥', '•'],
    ['.', ',', '?', '!', "'"],
  ];
  // The emoji layer is a category picker: a tab strip (each tab a category
  // glyph) over a scrollable grid. Every emoji here was color-verified on the
  // wall's Noto Color Emoji (some are grey/black by design - a panda, a soccer
  // ball, a phone - but they ARE the real emoji art, not text-fallback). Some
  // carry a variation selector - Backspace is grapheme-aware (oskApplyKey) so
  // each deletes whole. The 'recent' category is filled from localStorage at
  // render (see recentEmojis); its `emojis` here is just a placeholder.
  const EMOJI_CATS = [
    { id: 'recent', tab: '🕐', emojis: [] },
    { id: 'smileys', tab: '😀', emojis: [
      '😀', '😃', '😄', '😁', '😆', '😅', '😂', '🤣', '🙂', '😉',
      '😊', '😇', '🥰', '😍', '😘', '😗', '😋', '😜', '🤪', '😝',
      '🤗', '🤔', '🤨', '😐', '😶', '😏', '😌', '😴', '😷', '🤒',
      '🤢', '🤮', '🥵', '🥶', '🤯', '🤠', '🥳', '😎', '🤓', '🧐',
      '😕', '🙁', '😮', '😲', '😳', '🥺', '😢', '😭', '😱', '😤',
      '😡', '🤬', '😈', '💀', '👻', '👽', '🤖', '💩'] },
    { id: 'people', tab: '👋', emojis: [
      '👍', '👎', '👌', '✌️', '🤞', '🤟', '🤙', '👈', '👉', '👆',
      '👇', '✋', '🖐️', '🖖', '👋', '🤝', '🙏', '✍️', '💪', '👀',
      '👂', '👃', '👶', '🧒', '👦', '👧', '🧑', '👨', '👩', '👴',
      '👵', '🎅', '🤶', '🦸', '🦹', '🧙', '🧚', '🧛', '🧜', '🚶',
      '🏃', '💃', '🕺', '🧘'] },
    { id: 'animals', tab: '🐶', emojis: [
      '🐶', '🐱', '🐭', '🐹', '🐰', '🦊', '🐻', '🐼', '🐨', '🐯',
      '🦁', '🐮', '🐷', '🐸', '🐵', '🐔', '🐧', '🐦', '🐤', '🦆',
      '🦅', '🦉', '🦇', '🐺', '🐗', '🐴', '🦄', '🐝', '🐛', '🦋',
      '🐌', '🐞', '🐢', '🐍', '🦖', '🐙', '🦀', '🐠', '🐟', '🐬',
      '🐳', '🐋', '🦈', '🐊', '🐘', '🐪', '🦒', '🐐', '🦌', '🐕',
      '🐈', '🐓', '🦃', '🦚', '🦜', '🕊️', '🐇', '🐁', '🐿️', '🐾'] },
    { id: 'nature', tab: '🌿', emojis: [
      '🌵', '🎄', '🌲', '🌳', '🌴', '🌱', '🌿', '☘️', '🍀', '🍄',
      '🌾', '💐', '🌷', '🌹', '🥀', '🌺', '🌸', '🌼', '🌻', '🌙',
      '⭐', '🌟', '✨', '⚡', '☄️', '💥', '🔥', '🌈', '☀️', '⛅',
      '☁️', '🌧️', '⛈️', '❄️', '☃️', '⛄', '💨', '💧', '💦', '🌊'] },
    { id: 'food', tab: '🍎', emojis: [
      '🍏', '🍎', '🍐', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🍒',
      '🍑', '🥭', '🍍', '🥥', '🥝', '🍅', '🥑', '🍆', '🥦', '🌽',
      '🥕', '🧄', '🧅', '🥔', '🍞', '🥐', '🧀', '🥚', '🥓', '🍗',
      '🌭', '🍔', '🍟', '🍕', '🥪', '🌮', '🌯', '🥗', '🍝', '🍜',
      '🍲', '🍣', '🍦', '🍰', '🎂', '🍪', '🍫', '🍬', '🍭', '🍿',
      '☕', '🍵', '🥤', '🍺', '🍻', '🍷', '🥂'] },
    { id: 'activity', tab: '⚽', emojis: [
      '⚽', '🏀', '🏈', '⚾', '🥎', '🎾', '🏐', '🏉', '🎱', '🏓',
      '🏸', '🥅', '⛳', '🏹', '🎣', '🥊', '🛹', '⛸️', '🎿', '🏂',
      '🏋️', '🚴', '🏆', '🥇', '🥈', '🥉', '🏅', '🎖️', '🎗️', '🎪',
      '🤹', '🎨', '🎬', '🎤', '🎧', '🎼', '🎹', '🥁', '🎷', '🎸',
      '🎻', '🎲', '🎯', '🎳', '🎮', '🧩'] },
    { id: 'travel', tab: '🚗', emojis: [
      '🚗', '🚕', '🚙', '🚌', '🚑', '🚒', '🚓', '🚐', '🚚', '🚜',
      '🚲', '🛵', '🏍️', '🚨', '✈️', '🚀', '🛸', '🚁', '🚢', '⛵',
      '🚤', '⛽', '🚦', '🗺️', '🗽', '🗼', '🏰', '🎡', '🎢', '🎠',
      '⛲', '🏖️', '🏝️', '🌋', '⛺', '🏠', '🏡', '🏢', '🏬', '🏥',
      '🏦', '🏫', '⛪', '🕌', '🏙️', '🌃', '🌉', '🌅', '🌄', '🌇'] },
    { id: 'objects', tab: '💡', emojis: [
      '⌚', '📱', '💻', '🖥️', '⌨️', '🖨️', '🖱️', '💽', '💾', '💿',
      '📷', '📹', '🎥', '📞', '☎️', '📺', '📻', '🎙️', '⏰', '⏳',
      '🔋', '🔌', '💡', '🔦', '🕯️', '🧯', '💰', '💳', '💎', '⚖️',
      '🔧', '🔨', '🛠️', '⚙️', '🧲', '💣', '🔪', '🛡️', '🚬', '🔮',
      '🧿', '🔭', '🔬', '💊', '💉', '🩸', '🌡️', '🧹', '🧺', '🧻',
      '🚽', '🚿', '🛁', '🧼', '🧴', '🔑', '🚪', '🛏️', '🧸', '🖼️',
      '🛍️', '🛒', '🎁', '🎈', '🎉', '🎊', '✉️', '📦', '📫', '📮',
      '📅', '📆', '📋', '📁', '📰', '📓', '📚', '📖', '🔖', '📎',
      '📏', '📌', '✂️', '🖊️', '✏️', '📝', '🔍', '🔒', '🔓'] },
    { id: 'symbols', tab: '❤️', emojis: [
      '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💔',
      '❣️', '💕', '💞', '💓', '💗', '💖', '💘', '💝', '💯', '💢',
      '💥', '💫', '💬', '💭', '💤', '✅', '❌', '⭕', '🛑', '⛔',
      '🚫', '❗', '❓', '⚠️', '♻️', '🔔', '🎵', '🎶', '➡️', '⬅️',
      '⬆️', '⬇️', '▶️', '⏸️', '⏹️', '🔴', '🟠', '🟡', '🟢', '🔵',
      '🟣', '⚫', '⚪', '🟥', '🟨', '🟩', '🟦', '⬛', '⬜'] },
  ];

  let activeInput = null;   // the field currently being typed into
  let shiftOn = false;      // one-shot shift: capitalizes the next character
  let layer = 'letters';    // 'letters' | 'sym1' | 'sym2' | 'emoji'
  let emojiCat = 'smileys'; // active category within the emoji layer
  let oskEl = null;         // the keyboard container (a fixed sibling of .wrap)
  let keysEl = null;        // the layer-specific rows, re-rendered on a switch

  // Every emoji we ship, so a stored "recent" value that isn't one of ours (a
  // stale schema, a partial write, tampering on this shared kiosk origin) can
  // never surface as a tappable key or get inserted.
  const KNOWN_EMOJI = new Set();
  EMOJI_CATS.forEach((c) => c.emojis.forEach((e) => KNOWN_EMOJI.add(e)));

  // Recently-used emojis, most-recent-first, capped. Persisted per browser so
  // the family's go-to emojis are one tap away on the 🕐 tab. The dedup/cap/
  // sanitize logic is pure and tested in common.js (oskRecentRead/oskRecentPush);
  // these wrappers just do the localStorage I/O. Storage failures degrade to an
  // empty list - the picker still works, just without history.
  const RECENT_MAX = 30;
  function recentEmojis() {
    let raw = null;
    try { raw = localStorage.getItem('oskEmojiRecent'); } catch (e) { return []; }
    return oskRecentRead(raw, RECENT_MAX).filter((e) => KNOWN_EMOJI.has(e));
  }
  function pushRecent(emoji) {
    const next = oskRecentPush(recentEmojis(), emoji, RECENT_MAX);   // pure; no I/O
    try { localStorage.setItem('oskEmojiRecent', JSON.stringify(next)); }
    catch (e) { /* storage blocked/full: skip persisting, insertion still works */ }
  }

  function oskTypeable(el) {
    if (!el || typeof el.matches !== 'function' || !el.matches(OSK_SEL)) return false;
    if (el.tagName === 'TEXTAREA') return true;
    if (el.tagName !== 'INPUT') return false;   // a .txt-input <select> is not typeable
    return TYPEABLE_TYPES.indexOf((el.type || 'text').toLowerCase()) !== -1;
  }

  function addKey(row, key, label, cls, isLetter) {
    const btn = document.createElement('button');
    btn.type = 'button';   // never submit the form the field lives in
    btn.className = 'osk-key' + (cls ? ' ' + cls : '');
    btn.dataset.key = key;
    if (isLetter) btn.dataset.letter = '1';
    btn.textContent = label;
    row.appendChild(btn);
    return btn;
  }

  function addRow(parent, keys, isLetter) {
    const row = document.createElement('div');
    row.className = 'osk-row';
    keys.forEach((k) => addKey(row, k, k, '', isLetter));
    parent.appendChild(row);
  }

  // A letter key's face follows the shift state (shows A when shift is armed);
  // only the letters layer has letter faces + a Shift key.
  function paintFaces() {
    if (!keysEl) return;
    keysEl.querySelectorAll('.osk-key[data-letter]').forEach((btn) => {
      btn.textContent = shiftOn ? btn.dataset.key.toUpperCase() : btn.dataset.key;
    });
    const shift = keysEl.querySelector('.osk-shift');
    if (shift) {
      shift.classList.toggle('active', shiftOn);
      shift.setAttribute('aria-pressed', shiftOn ? 'true' : 'false');
    }
  }

  // Render the current layer's rows + its command row into keysEl. The command
  // row's mode keys (?123 / ABC / #+= / 😊) carry a `Layer:<name>` data-key the
  // click handler routes to setLayer; everything else is a real character or an
  // action (Space / Backspace / Done / Shift).
  function renderLayer() {
    keysEl.textContent = '';
    if (layer === 'emoji') {
      // Category tab strip: each tab a `EmojiCat:<id>` key the click handler
      // routes to a re-render on the chosen category.
      const tabs = document.createElement('div');
      tabs.className = 'osk-emoji-cats';
      EMOJI_CATS.forEach((c) => {
        const t = addKey(tabs, 'EmojiCat:' + c.id, c.tab, 'osk-emoji-cat', false);
        if (c.id === emojiCat) t.classList.add('active');
      });
      keysEl.appendChild(tabs);
      const grid = document.createElement('div');
      grid.className = 'osk-emoji-grid';
      const cat = EMOJI_CATS.find((c) => c.id === emojiCat) || EMOJI_CATS[1];
      const list = cat.id === 'recent' ? recentEmojis() : cat.emojis;
      if (!list.length) {
        const empty = document.createElement('div');
        empty.className = 'osk-emoji-empty';
        empty.textContent = 'Emojis you use show up here.';
        grid.appendChild(empty);
      } else {
        list.forEach((e) => addKey(grid, e, e, 'osk-emoji', false));
      }
      keysEl.appendChild(grid);
    } else if (layer === 'sym1') {
      SYM1_ROWS.forEach((r) => addRow(keysEl, r, false));
    } else if (layer === 'sym2') {
      SYM2_ROWS.forEach((r) => addRow(keysEl, r, false));
    } else {
      addRow(keysEl, DIGIT_ROW, false);
      LETTER_ROWS.forEach((r) => addRow(keysEl, r, true));
    }

    const cmd = document.createElement('div');
    cmd.className = 'osk-row osk-row-cmd';
    if (layer === 'letters') {
      addKey(cmd, 'Shift', '⇧', 'osk-shift', false).setAttribute('aria-pressed', 'false');
      addKey(cmd, 'Layer:sym1', '?123', 'osk-mode', false);
    } else if (layer === 'sym1') {
      addKey(cmd, 'Layer:letters', 'ABC', 'osk-mode', false);
      addKey(cmd, 'Layer:sym2', '#+=', 'osk-mode', false);
    } else if (layer === 'sym2') {
      addKey(cmd, 'Layer:letters', 'ABC', 'osk-mode', false);
      addKey(cmd, 'Layer:sym1', '123', 'osk-mode', false);
    } else {   // emoji
      addKey(cmd, 'Layer:letters', 'ABC', 'osk-mode', false);
    }
    if (layer !== 'emoji') addKey(cmd, 'Layer:emoji', '😊', 'osk-emoji-toggle', false);
    addKey(cmd, 'Space', 'space', 'osk-space', false);
    addKey(cmd, 'Backspace', '⌫', 'osk-back', false);
    // ✕ Cancel sits beside Done as the discard/confirm pair: it dismisses the
    // keyboard WITHOUT submitting and clears what you typed (see onKey).
    addKey(cmd, 'Cancel', '✕', 'osk-cancel', false).setAttribute('aria-label', 'Close and discard');
    addKey(cmd, 'Done', 'Done', 'osk-done', false);
    keysEl.appendChild(cmd);
    paintFaces();
  }

  function setLayer(name) {
    layer = name;
    shiftOn = false;   // shift only applies to the letters layer, freshly off
    renderLayer();
  }

  function buildKeyboard() {
    const el = document.createElement('div');
    el.className = 'osk hidden';
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'On-screen keyboard');
    el.setAttribute('aria-hidden', 'true');
    keysEl = document.createElement('div');
    keysEl.className = 'osk-keys';
    el.appendChild(keysEl);
    layer = 'letters';
    renderLayer();
    document.body.appendChild(el);
    return el;
  }

  function show(input) {
    activeInput = input;
    oskEl.classList.remove('hidden');
    oskEl.setAttribute('aria-hidden', 'false');
    document.body.classList.add('osk-open');
    // Bring the field above the docked keyboard in any scrollable container
    // (the To-Do overlay / chore editor both scroll); a no-op for a field
    // already in view.
    try { input.scrollIntoView({ block: 'center' }); } catch (e) { /* older engine */ }
  }

  function hide() {
    const el = activeInput;
    activeInput = null;
    shiftOn = false;
    oskEl.classList.add('hidden');
    oskEl.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('osk-open');
    // Blur the field if it is still focused (Done/Cancel hide it while the osk's
    // keepFocus kept focus on the input). Otherwise the field stays focused with
    // no keyboard, and tapping it again fires no focusin - so the keyboard could
    // not be re-summoned by tapping the same box, and a later Done would no-op
    // on a null activeInput. Blurring makes the next tap re-focus -> re-show.
    if (el && typeof el.blur === 'function' && document.activeElement === el) {
      try { el.blur(); } catch (e) { /* detached */ }
    }
    if (layer !== 'letters') setLayer('letters');   // reopen on the letters layer
    else paintFaces();
  }

  // Apply one key to the active input through oskApplyKey (the pure, tested
  // transform), then fire a bubbling 'input' event so the app's own handlers
  // (the To-Do draft-preservation, the chore form) see the change. Writing the
  // computed value ourselves - rather than setRangeText - keeps maxlength
  // honored (setRangeText ignores it) and keeps the runtime path identical to
  // the unit-tested one.
  function typeKey(key) {
    const el = activeInput;
    if (!el) return;
    const max = el.maxLength;   // the DOM returns -1 when the attribute is absent
    // A selection-less input type would report null here; default to the end of
    // the field so a keystroke appends rather than landing at index 0.
    const start = el.selectionStart == null ? el.value.length : el.selectionStart;
    const end = el.selectionEnd == null ? el.value.length : el.selectionEnd;
    const res = oskApplyKey(el.value, start, end, key, {
      shift: shiftOn,
      maxlength: max > 0 ? max : 0,
    });
    el.value = res.value;
    try { el.setSelectionRange(res.caret, res.caret); } catch (e) { /* detached */ }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    if (key !== 'Backspace') { shiftOn = false; paintFaces(); }   // one-shot shift is spent
  }

  function onKey(key) {
    if (key === 'Shift') { shiftOn = !shiftOn; paintFaces(); return; }
    if (key === 'Cancel') {
      // Close without saving: clear what was typed and dismiss. Nothing is
      // committed - unlike Done, this never submits. Clearing the DOM value is
      // safe because no unsaved edit was ever persisted - an existing record's
      // stored value is untouched and reappears on reopen.
      const el = activeInput;
      if (el) {
        el.value = '';
        try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) { /* detached */ }
      }
      hide();
      return;
    }
    if (key === 'Done') {
      const el = activeInput;
      const form = el && el.form;
      if (form && typeof form.requestSubmit === 'function') {
        form.requestSubmit();   // e.g. #todo-add-form: adds the to-do
      } else if (el) {
        // The chore editor is a non-<form> div with a [data-submit] Save button,
        // so el.form is null there. Commit through that button (its own
        // validation surfaces an error if the chore is incomplete) instead of
        // dismissing the keyboard with nothing saved.
        const scope = el.closest('.chore-modal, .chore-card, dialog, [role="dialog"]');
        const submit = scope && scope.querySelector('[data-submit]');
        if (submit) submit.click(); else el.blur();
      }
      hide();
      return;
    }
    typeKey(key);   // a character, 'Space', or 'Backspace'
  }

  oskEl = buildKeyboard();

  // OS-KEYBOARD SUPPRESSION: THIS keyboard is the only one the wall wants, so
  // stop the browser/compositor from also summoning GNOME's touch keyboard for
  // these fields. inputmode="none" alone was not enough on Firefox/Wayland -
  // GNOME's keyboard still muscled in once typing started - so we mark the field
  // readonly: the OS never offers a keyboard for a read-only field, yet the app
  // keyboard writes straight to its .value regardless (see typeKey). The whole
  // module only runs in kiosk mode (gated above), so this is unconditional here.
  // The To-Do add input and chore fields are (re)created dynamically, so stamp
  // existing ones now and watch for new ones.
  //
  // TRAP for future work: a readonly control is "barred from constraint
  // validation", so a native <form> submit (the requestSubmit path in onKey's
  // Done branch) will NOT enforce `required`/`pattern` on a stamped field. Today
  // that is harmless - the only stamped, form-submitting field is
  // #todo-add-input, which has no HTML validation and is JS-guarded in addTodo.
  // If a REQUIRED field is ever added under OSK_SEL and committed via a <form>,
  // enforce its validity in JS before submitting (readonly hides it from the
  // browser's own check).
  const kioskStamp = (el) => {
    if (!oskTypeable(el)) return;
    el.setAttribute('inputmode', 'none');
    el.readOnly = true;             // suppresses the OS keyboard; JS still writes .value
  };
  const stampTree = (root) => {
    if (!root || !root.querySelectorAll) return;
    if (root.matches && root.matches(OSK_SEL)) kioskStamp(root);
    root.querySelectorAll(OSK_SEL).forEach(kioskStamp);
  };
  stampTree(document);
  // The observer stays attached for the life of the (24/7) kiosk on purpose: the
  // served fields are created and destroyed continuously as overlays open and
  // lists repaint. It only walks element additions (nodeType 1), and the
  // synchronous focusin backstop below covers correctness even if a mutation is
  // missed - so this is a cheap safety net, not a correctness dependency.
  new MutationObserver((muts) => {
    muts.forEach((m) => m.addedNodes && m.addedNodes.forEach((n) => {
      if (n.nodeType === 1) stampTree(n);
    }));
  }).observe(document.body, { childList: true, subtree: true });

  // FOCUS PRESERVATION: pressing a key must not steal focus from the input (a
  // blur would collapse the caret and, on the wall, dismiss the keyboard). Cancel
  // the default focus-shift on press; do the actual key work on click.
  // preventDefault on ANY press inside the keyboard - not just on a key - so a
  // tap that lands in the gaps/padding between keys (real dead zones on an
  // imprecise touchscreen) still doesn't blur the input and dismiss the board.
  // The whole .osk is user-select:none with no focusable non-button content, so
  // cancelling the focus-shift everywhere is safe.
  const keepFocus = (e) => { e.preventDefault(); };
  oskEl.addEventListener('pointerdown', keepFocus);
  oskEl.addEventListener('mousedown', keepFocus);   // pointer-events-off fallback
  oskEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.osk-key');
    if (!btn) return;
    const key = btn.dataset.key;
    if (key.indexOf('Layer:') === 0) { setLayer(key.slice(6)); return; }
    if (key.indexOf('EmojiCat:') === 0) { emojiCat = key.slice(9); renderLayer(); return; }
    // Record the emoji, then insert. We deliberately do NOT re-render the grid
    // here even when the recent tab is showing: reshuffling emojis under the
    // finger would fight rapid multi-emoji entry. The recent order refreshes on
    // the next open of the tab.
    if (btn.classList.contains('osk-emoji')) pushRecent(key);
    onKey(key);
  });

  // Show on focus of a wall text field; hide when focus leaves it for anything
  // that is not the keyboard. (Because keepFocus cancels the focus-shift, focus
  // never actually moves onto a key - but the relatedTarget guard keeps the
  // keyboard up even if a browser ever did move it there.)
  document.addEventListener('focusin', (e) => {
    if (!oskTypeable(e.target)) return;
    kioskStamp(e.target);   // synchronous backstop for a just-created field
    show(e.target);
  });
  document.addEventListener('focusout', (e) => {
    if (e.target !== activeInput) return;
    if (e.relatedTarget && oskEl.contains(e.relatedTarget)) return;
    hide();
  });
})();
