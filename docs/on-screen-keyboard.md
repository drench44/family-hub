# On-screen keyboard (OSK)

The wall is a wall-mounted touchscreen with **no physical keyboard**, so tapping
a text field (add a to-do, name a chore) has to summon a keyboard to type on.
`static/osk.js` builds a themed keyboard docked at the bottom of the screen; the
pure text transform behind it lives in `static/common.js` (`oskApplyKey`, unit
tested in `tests/js/osk.test.mjs`).

## When it activates

**Kiosk (wall) only.** The keyboard exists solely for the keyboard-less wall.
Phones and laptops already have a real or native on-screen keyboard, and showing
this one there just stacks under theirs — so it does **not** activate on touch
(a phone is a touch device too). It turns on only when the page URL carried
`?kiosk=1` at least once. The flag is latched into `localStorage` (`oskKiosk=1`)
so it survives every later navigation on that browser; `?kiosk=0` clears it.

### Why an explicit flag, not touch detection

The wall doesn't report touch to the browser at all. It's an HP all-in-one
running Firefox under a GNOME/Wayland session, and there the touchscreen is
delivered to the browser as the **mouse pointer** — the page sees
`maxTouchPoints: 0` and `pointer: fine`, exactly like a desktop with a mouse. So
a touch heuristic can't identify the wall anyway; and even if it could, it would
wrongly fire on every phone. An explicit flag is the only reliable signal.
**Point the wall's browser at the hub with `?kiosk=1` once** (e.g. make the
bookmark / start page `http://<your-hub>/?kiosk=1`). After the first load the
flag is remembered, so the query string is only needed once per browser profile.

Before the flag is latched, the wall silently falls back to GNOME's own touch
keyboard, which mishandles Firefox web inputs (needs two taps to appear, and its
backspace never reaches the field) — which is the whole reason this exists.

### Suppressing the OS keyboard

The app keyboard must be the *only* keyboard on the wall. So the served fields
are marked `readonly` and `inputmode="none"`: the operating system never offers
its own keyboard for a read-only field, while the app keyboard still writes
straight to the field's `.value`.

## Layers

- **Letters** — a digit row plus QWERTY, one-shot Shift, and a command row.
- **?123 / #+=** — two pages of punctuation and symbols.
- **Emoji** — a category picker: a tab strip (smileys, people, animals, nature,
  food, activity, travel, objects, symbols) over a scrollable grid, plus a 🕐
  **Recently used** tab that fills from your taps (persisted in `localStorage`).
  Tapping an emoji inserts it and keeps the picker open so several can be added.
  Every emoji is color-verified against the wall's font (see below).

Backspace is **grapheme-aware** (`oskGraphemeBackLen` in `common.js`, using
`Intl.Segmenter` with a surrogate-pair fallback), so one tap deletes a whole
emoji — a plain code-unit delete would leave a broken half-character.

The command row ends with a discard/confirm pair: **✕ Cancel** clears the field
and closes the keyboard without saving; **Done** submits (adds the to-do / saves
the chore).

## Emoji rendering on the wall

The emoji set is chosen to render in color on the wall's **stock** font (a
Debian/GNOME box with `fonts-noto-color-emoji`) — every emoji was checked on the
real display. Some (a phone, cutlery, a soccer ball, a panda) are grey/black-
and-white *by design*; that's the real Noto Color Emoji artwork, not a broken
fallback, so it's kept.

Do **not** try to "fix" grey emoji with a broad fontconfig rule that prepends
`Noto Color Emoji` to every font pattern. That was tried and it backfired: Noto
Color Emoji also contains bare digit glyphs, so prepending it globally rendered
the wall clock's digits as full-width emoji cells — the whole page went
scattered. If a specific new emoji ever falls back to a monochrome outline on
the wall, swap it for a color-verified one in `EMOJI_CATS` (osk.js) rather than
touching system fonts.

## Guards & real-wall smoke test

- `tests/js/osk.test.mjs` — the pure transform, incl. symbol/emoji insert and
  grapheme-aware backspace (surrogate pair, variation selector, ZWJ sequence).
- `tests/test_static.py` — structural guards for kiosk activation, OS-keyboard
  suppression, the symbol/emoji layers, the grapheme helper, the CSS, the
  `renderTodosPaint` focus/caret preservation, and that `hide()` blurs a
  still-focused field (so the keyboard re-summons on the next tap).

These don't run a real browser, so some bugs only show up on the actual wall
(scattered text from a bad emoji fontconfig; the keyboard not re-summoning after
Cancel). For UI changes, run the real-wall smoke test against the running hub —
it drives the wall's Firefox with Marionette and asserts the behaviour CI can't
see:

```bash
# on a machine with a Firefox that can reach the hub (usually the wall, over SSH)
python3 scripts/wall-smoke-test.py --hub-url http://<your-hub>:8138
```

It needs `marionette_driver` (`pip install marionette_driver`, ideally in a
venv) and exits non-zero on any failure, so it can gate a deploy.
