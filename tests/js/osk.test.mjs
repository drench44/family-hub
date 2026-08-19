// Executable tests for oskApplyKey - the pure text transform behind the wall's
// on-screen keyboard (osk.js). It lives in common.js (like panelFit/wallZoom) so
// it loads into a vm sandbox with no `document`; osk.js's DOM code is a thin
// wrapper that reads the live input and calls it, so these branches ARE the
// runtime behavior. Mirrors tests/js/hub.test.mjs's harness. Run: node --test.
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
// The transform returns a cross-realm object; spread its own props so deepEqual
// compares against a same-realm literal (same pattern the panelFit tests use).
const apply = (...a) => ({ ...sandbox.oskApplyKey(...a) });

test('insert a char in the middle of a string', () => {
  // "helo", caret between l and o (index 3), type 'l' -> "hello"
  assert.deepEqual(apply('helo', 3, 3, 'l', {}), { value: 'hello', caret: 4 });
});

test('insert a char at the end', () => {
  assert.deepEqual(apply('cat', 3, 3, 's', {}), { value: 'cats', caret: 4 });
});

test('a null selection (some inputs report it) is treated as caret at start', () => {
  // real browsers return null selectionStart/End for certain input types; the
  // wrapper passes them straight through, so oskApplyKey must coerce null -> 0
  // rather than mis-place the character.
  assert.deepEqual(apply('hi', null, null, 'x', {}), { value: 'xhi', caret: 1 });
});

test('a character key replaces the current selection', () => {
  // "cat" with "at" selected (1..3), type 'x' -> "cx"
  assert.deepEqual(apply('cat', 1, 3, 'x', {}), { value: 'cx', caret: 2 });
});

test('shift capitalizes a character (one-shot is applied by the caller)', () => {
  assert.deepEqual(apply('', 0, 0, 'a', { shift: true }), { value: 'A', caret: 1 });
  // shift on a digit is a harmless no-op
  assert.deepEqual(apply('', 0, 0, '5', { shift: true }), { value: '5', caret: 1 });
});

test('Space inserts a single space at the caret', () => {
  assert.deepEqual(apply('ab', 2, 2, 'Space', {}), { value: 'ab ', caret: 3 });
  // Space replaces a selection like any other key
  assert.deepEqual(apply('a-b', 1, 2, 'Space', {}), { value: 'a b', caret: 2 });
});

test('Backspace deletes the char before the caret', () => {
  assert.deepEqual(apply('hello', 5, 5, 'Backspace', {}), { value: 'hell', caret: 4 });
  // mid-string: removes the char to the left, caret follows
  assert.deepEqual(apply('hello', 2, 2, 'Backspace', {}), { value: 'hllo', caret: 1 });
});

test('Backspace deletes the whole selection (not just one char)', () => {
  // "hello" with "ell" selected (1..4) -> "ho"
  assert.deepEqual(apply('hello', 1, 4, 'Backspace', {}), { value: 'ho', caret: 1 });
});

test('Backspace at position 0 is a no-op', () => {
  assert.deepEqual(apply('hi', 0, 0, 'Backspace', {}), { value: 'hi', caret: 0 });
  assert.deepEqual(apply('', 0, 0, 'Backspace', {}), { value: '', caret: 0 });
});

test('maxlength drops a char typed at the limit, caret unchanged', () => {
  // "abc" is full at maxlength 3: the new char is refused, value + caret still
  assert.deepEqual(apply('abc', 3, 3, 'd', { maxlength: 3 }), { value: 'abc', caret: 3 });
});

test('maxlength still allows replacing a selection at the limit (length holds)', () => {
  // full at 3, but "bc" is selected (1..3) so typing 'X' keeps length 2 -> ok
  assert.deepEqual(apply('abc', 1, 3, 'X', { maxlength: 3 }), { value: 'aX', caret: 2 });
});

test('maxlength never blocks Backspace', () => {
  assert.deepEqual(apply('abc', 3, 3, 'Backspace', { maxlength: 3 }), { value: 'ab', caret: 2 });
});

test('a stale/out-of-range selection is clamped, not sliced past the end', () => {
  assert.deepEqual(apply('hi', 9, 9, 'x', {}), { value: 'hix', caret: 3 });
});

// ---- symbols + emoji (the ?123 / 😊 layers feed whole strings as `key`) ----

test('a symbol key inserts like any character', () => {
  assert.deepEqual(apply('a', 1, 1, '@', {}), { value: 'a@', caret: 2 });
  assert.deepEqual(apply('', 0, 0, '€', {}), { value: '€', caret: 1 });
});

test('an emoji inserts whole and advances the caret past all its code units', () => {
  // 😀 is a surrogate pair: length 2, so the caret lands at 2.
  assert.deepEqual(apply('', 0, 0, '😀', {}), { value: '😀', caret: 2 });
  // inserted after existing text
  assert.deepEqual(apply('hi', 2, 2, '🎉', {}), { value: 'hi🎉', caret: 4 });
});

test('Backspace deletes a whole surrogate-pair emoji, not half of it', () => {
  // "a😀": caret at end (index 3). One Backspace must remove both code units.
  assert.deepEqual(apply('a😀', 3, 3, 'Backspace', {}), { value: 'a', caret: 1 });
});

test('Backspace deletes a variation-selector emoji as one grapheme', () => {
  // ❤️ is ❤ (U+2764) + VS16 (U+FE0F) = 2 code units; delete both at once.
  const heart = '❤️';
  assert.deepEqual(apply(heart, heart.length, heart.length, 'Backspace', {}),
    { value: '', caret: 0 });
});

test('Backspace deletes a ZWJ emoji sequence as one grapheme', () => {
  // 👨‍👩‍👧 (man+ZWJ+woman+ZWJ+girl) is 8 code units but one grapheme.
  const fam = '👨‍👩‍👧';
  assert.deepEqual(apply('x' + fam, ('x' + fam).length, ('x' + fam).length, 'Backspace', {}),
    { value: 'x', caret: 1 });
});

test('Backspace still removes one plain char (no over-deletion of ASCII)', () => {
  assert.deepEqual(apply('ab', 2, 2, 'Backspace', {}), { value: 'a', caret: 1 });
});

// ---- maxlength counts an emoji as its code units (a surrogate pair is 2) ----

test('maxlength refuses an emoji that would overflow by its full width', () => {
  // 9 chars, cap 10: an emoji needs 2 units but only 1 is free -> refused whole
  assert.deepEqual(apply('123456789', 9, 9, '😀', { maxlength: 10 }),
    { value: '123456789', caret: 9 });
});

test('maxlength admits an emoji that exactly fills the field', () => {
  // 8 chars, cap 10: the 2-unit emoji fits exactly
  assert.deepEqual(apply('12345678', 8, 8, '😀', { maxlength: 10 }),
    { value: '12345678😀', caret: 10 });
});

// ---- grapheme backspace: flag + mid-string (Intl.Segmenter path) ----

test('Backspace deletes a regional-indicator flag as one grapheme', () => {
  // 🇬🇧 is two regional indicators = 4 code units, one grapheme.
  const flag = '🇬🇧';
  assert.deepEqual(apply(flag, flag.length, flag.length, 'Backspace', {}),
    { value: '', caret: 0 });
});

test('Backspace deletes an emoji sitting mid-string, not the char after it', () => {
  // "a😀b" with the caret right after the emoji (index 3) -> "ab"
  assert.deepEqual(apply('a😀b', 3, 3, 'Backspace', {}), { value: 'ab', caret: 1 });
});

// ---- the surrogate-pair FALLBACK (engine without Intl.Segmenter) ----
// Load common.js into a second context whose Intl has no Segmenter, so the
// hand-rolled fallback in oskGraphemeBackLen actually runs (the primary path
// above always has Segmenter under Node, leaving the safety net uncovered).
const noSeg = { document: undefined };
vm.createContext(noSeg);
vm.runInContext(readFileSync(join(staticDir, 'common.js'), 'utf8'), noSeg);
vm.runInContext('Intl.Segmenter = undefined;', noSeg);   // force the fallback branch
const applyNoSeg = (...a) => ({ ...noSeg.oskApplyKey(...a) });

test('fallback (no Intl.Segmenter): a surrogate-pair emoji still deletes whole', () => {
  assert.deepEqual(applyNoSeg('a😀', 3, 3, 'Backspace', {}), { value: 'a', caret: 1 });
});

test('fallback (no Intl.Segmenter): a plain char still deletes exactly one', () => {
  assert.deepEqual(applyNoSeg('ab', 2, 2, 'Backspace', {}), { value: 'a', caret: 1 });
});

test('fallback (no Intl.Segmenter): a ZWJ sequence degrades to one code-unit pair', () => {
  // Documents the KNOWN degraded behavior: without Segmenter the fallback only
  // strips the trailing surrogate pair of 👨‍👩‍👧 (the 👧), leaving a dangling
  // ZWJ. This is why Intl.Segmenter is the primary path; the fallback is a
  // best-effort net, not grapheme-complete.
  const fam = '👨‍👩‍👧';                 // 👨 ZWJ 👩 ZWJ 👧 = 8 code units
  const res = applyNoSeg('x' + fam, ('x' + fam).length, ('x' + fam).length, 'Backspace', {});
  assert.equal(res.caret, ('x' + fam).length - 2);   // only the last pair removed
  assert.equal(res.value, 'x' + fam.slice(0, -2));   // trailing ZWJ remains
});

// ---- recently-used emoji list (pure logic behind the 🕐 tab) ----
const recentRead = (...a) => [...sandbox.oskRecentRead(...a)];
const recentPush = (...a) => [...sandbox.oskRecentPush(...a)];

test('oskRecentPush puts a new emoji at the front, most-recent-first', () => {
  assert.deepEqual(recentPush(['🐶', '🍕'], '🎉', 30), ['🎉', '🐶', '🍕']);
  assert.deepEqual(recentPush([], '🐶', 30), ['🐶']);
});

test('oskRecentPush de-dupes: re-tapping moves it to front, length unchanged', () => {
  assert.deepEqual(recentPush(['🐶', '🍕', '🎉'], '🍕', 30), ['🍕', '🐶', '🎉']);
});

test('oskRecentPush caps the list, dropping the oldest', () => {
  const list = ['a', 'b', 'c'];
  assert.deepEqual(recentPush(list, 'z', 3), ['z', 'a', 'b']);   // 'c' evicted
  const full = Array.from({ length: 30 }, (_, i) => 'e' + i);
  const out = recentPush(full, 'NEW', 30);
  assert.equal(out.length, 30);
  assert.equal(out[0], 'NEW');
  assert.equal(out.includes('e29'), false);   // the oldest fell off
});

test('oskRecentPush tolerates a non-array list', () => {
  assert.deepEqual(recentPush(null, '🐶', 30), ['🐶']);
});

test('oskRecentRead returns [] for malformed / non-array / missing storage', () => {
  assert.deepEqual(recentRead('not json', 30), []);
  assert.deepEqual(recentRead('{}', 30), []);
  assert.deepEqual(recentRead('42', 30), []);
  assert.deepEqual(recentRead(null, 30), []);
  assert.deepEqual(recentRead('[]', 30), []);
});

test('oskRecentRead drops non-string / empty entries and caps length', () => {
  // corrupt/foreign stored value must not surface as tappable keys
  assert.deepEqual(recentRead('["🐶", "", 42, null, "🍕"]', 30), ['🐶', '🍕']);
  const stored = JSON.stringify(Array.from({ length: 50 }, (_, i) => 'e' + i));
  assert.equal(recentRead(stored, 30).length, 30);
});
