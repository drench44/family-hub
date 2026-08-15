'use strict';

/* Family Hub - on-screen keyboard (OSK) for the touchscreen wall.

   The wall is a Chromium kiosk on a Raspberry Pi touchscreen with NO physical
   keyboard, so tapping a text field has to summon a keyboard the family can
   type on. Desktop Chromium shows no native OSK on focus, so we build our own.

   Loaded as a classic script AFTER common.js (for oskApplyKey, the pure text
   transform) and BEFORE hub.js. It wires itself entirely through delegated
   document listeners, so hub.js stays untouched.

   GATED ON TOUCH: someone viewing the wall on a laptop/desktop has a real
   keyboard, and a fake one popping over the mouse cursor would just be in the
   way - so the whole thing no-ops unless the device reports touch points. */
(function () {
  // No document (the vm test sandbox) or no touch (a laptop): do nothing. The
  // pure transform lives in common.js (oskApplyKey) and is tested there, so the
  // gate can bail before building any DOM.
  if (typeof document === 'undefined' || typeof navigator === 'undefined') return;
  if (!(navigator.maxTouchPoints > 0)) return;

  // The inputs the keyboard serves. A `.txt-input` <select> (the chore person
  // picker) also carries the class, so oskTypeable() below excludes non-text
  // controls - the keyboard must only attach to something you can type into.
  const OSK_SEL = '#todo-add-input, .txt-input';
  const TYPEABLE_TYPES = ['text', 'search', 'url', 'email', 'tel', ''];

  const KEY_ROWS = [
    '1234567890'.split(''),
    'qwertyuiop'.split(''),
    'asdfghjkl'.split(''),
    'zxcvbnm'.split(''),
  ];

  let activeInput = null;   // the field currently being typed into
  let shiftOn = false;      // one-shot shift: capitalizes the next character
  let oskEl = null;         // the keyboard container (a fixed sibling of .wrap)

  function oskTypeable(el) {
    if (!el || typeof el.matches !== 'function' || !el.matches(OSK_SEL)) return false;
    if (el.tagName === 'TEXTAREA') return true;
    if (el.tagName !== 'INPUT') return false;   // a .txt-input <select> is not typeable
    return TYPEABLE_TYPES.indexOf((el.type || 'text').toLowerCase()) !== -1;
  }

  // A letter key's face follows the shift state (shows A when shift is armed);
  // digits and command keys never change, so only letter rows repaint.
  function paintFaces() {
    if (!oskEl) return;
    oskEl.querySelectorAll('.osk-key[data-letter]').forEach((btn) => {
      btn.textContent = shiftOn ? btn.dataset.key.toUpperCase() : btn.dataset.key;
    });
    const shift = oskEl.querySelector('.osk-shift');
    if (shift) {
      shift.classList.toggle('active', shiftOn);
      shift.setAttribute('aria-pressed', shiftOn ? 'true' : 'false');
    }
  }

  function buildKeyboard() {
    const el = document.createElement('div');
    el.className = 'osk hidden';
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'On-screen keyboard');
    el.setAttribute('aria-hidden', 'true');

    const addKey = (row, key, label, cls, isLetter) => {
      const btn = document.createElement('button');
      btn.type = 'button';   // never submit the form the field lives in
      btn.className = 'osk-key' + (cls ? ' ' + cls : '');
      btn.dataset.key = key;
      if (isLetter) btn.dataset.letter = '1';
      btn.textContent = label;
      row.appendChild(btn);
    };

    KEY_ROWS.forEach((keys, i) => {
      const row = document.createElement('div');
      row.className = 'osk-row';
      const letters = i > 0;   // row 0 is digits, rows 1-3 are letters
      keys.forEach((k) => addKey(row, k, k, '', letters));
      el.appendChild(row);
    });

    // Command row: Shift, Space (wide), Backspace, Done.
    const cmd = document.createElement('div');
    cmd.className = 'osk-row osk-row-cmd';
    addKey(cmd, 'Shift', '⇧', 'osk-shift', false);
    addKey(cmd, 'Space', 'space', 'osk-space', false);
    addKey(cmd, 'Backspace', '⌫', 'osk-back', false);
    addKey(cmd, 'Done', 'Done', 'osk-done', false);
    cmd.querySelector('.osk-shift').setAttribute('aria-pressed', 'false');
    el.appendChild(cmd);

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
    activeInput = null;
    shiftOn = false;
    oskEl.classList.add('hidden');
    oskEl.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('osk-open');
    paintFaces();
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
    const res = oskApplyKey(el.value, el.selectionStart, el.selectionEnd, key, {
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
    if (key === 'Done') {
      // Submit the field's form if it has one (fires the existing todo-add
      // submit handler); otherwise just dismiss.
      const el = activeInput;
      const form = el && el.form;
      if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
      else if (el) el.blur();
      hide();
      return;
    }
    typeKey(key);   // a character, 'Space', or 'Backspace'
  }

  oskEl = buildKeyboard();

  // FOCUS PRESERVATION: pressing a key must not steal focus from the input (a
  // blur would collapse the caret and, on the wall, dismiss the keyboard). Cancel
  // the default focus-shift on press; do the actual key work on click.
  const keepFocus = (e) => { if (e.target.closest('.osk-key')) e.preventDefault(); };
  oskEl.addEventListener('pointerdown', keepFocus);
  oskEl.addEventListener('mousedown', keepFocus);   // pointer-events-off fallback
  oskEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.osk-key');
    if (btn) onKey(btn.dataset.key);
  });

  // Show on focus of a wall text field; hide when focus leaves it for anything
  // that is not the keyboard. (Because keepFocus cancels the focus-shift, focus
  // never actually moves onto a key - but the relatedTarget guard keeps the
  // keyboard up even if a browser ever did move it there.)
  document.addEventListener('focusin', (e) => {
    if (oskTypeable(e.target)) show(e.target);
  });
  document.addEventListener('focusout', (e) => {
    if (e.target !== activeInput) return;
    if (e.relatedTarget && oskEl.contains(e.relatedTarget)) return;
    hide();
  });
})();
