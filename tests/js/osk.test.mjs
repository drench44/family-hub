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
