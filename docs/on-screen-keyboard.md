# On-screen keyboard (OSK)

The wall is a wall-mounted touchscreen with **no physical keyboard**, so tapping
a text field (add a to-do, name a chore) has to summon a keyboard to type on.
`static/osk.js` builds a themed keyboard docked at the bottom of the screen; the
pure text transform behind it lives in `static/common.js` (`oskApplyKey`, unit
tested in `tests/js/osk.test.mjs`).

## When it activates

The keyboard is wasted (and in the way) on a laptop that already has real keys,
so it stays off unless one of these is true:

1. **A genuine touch device** — `maxTouchPoints > 0` or `(pointer: coarse)`.
   This covers phones.
2. **Kiosk mode** — the page URL carried `?kiosk=1` at least once. The flag is
   latched into `localStorage` (`oskKiosk=1`) so it survives every later
   navigation on that browser.

### Why the wall needs the flag

The wall turned out **not** to report touch to the browser at all. It's an HP
all-in-one running Firefox under a GNOME/Wayland session, and in that setup the
touchscreen is delivered to the browser as the **mouse pointer** — the page sees
`maxTouchPoints: 0` and `pointer: fine`, exactly like a desktop with a mouse. So
the touch heuristic can never fire there, and the wall silently fell back to
GNOME's own touch keyboard, which mishandles Firefox web inputs (needs two taps
to appear, and its backspace never reaches the field).

The fix is to turn the app keyboard on explicitly. **Point the wall's browser at
the hub with `?kiosk=1` once** (e.g. make the bookmark / start page
`http://<your-hub>/?kiosk=1`). After the first load the flag is remembered, so
the query string is only needed once per browser profile.

### Suppressing the OS keyboard

In kiosk mode the app keyboard must be the *only* keyboard. So kiosk mode marks
the served fields `readonly` and `inputmode="none"`: the operating system never
offers its own keyboard for a read-only field, while the app keyboard still
writes straight to the field's `.value`. On genuine touch phones this is left
off — a phone's native keyboard is the better peer there.

## Layers

- **Letters** — a digit row plus QWERTY, one-shot Shift, and a command row.
- **?123 / #+=** — two pages of punctuation and symbols.
- **Emoji** — a curated grid of common + family/chore emojis; tapping one inserts
  it and keeps the grid open so several can be added.

Backspace is **grapheme-aware** (`oskGraphemeBackLen` in `common.js`, using
`Intl.Segmenter` with a surrogate-pair fallback), so one tap deletes a whole
emoji — a plain code-unit delete would leave a broken half-character.

## Emoji rendering on the wall (fontconfig)

Some ordinary emojis (a phone, cutlery, a soccer ball) are grey/black-and-white
*by design*, and on a stock Debian/GNOME box a monochrome symbol font (Symbola /
DejaVu) can win the font fallback for those codepoints and render them as flat
**text outlines** instead of the real emoji artwork. The installed Noto Color
Emoji has color glyphs for them; it just needs to win the fallback. A one-time
per-machine fontconfig rule fixes it — prefer the color emoji font:

```xml
<!-- ~/.config/fontconfig/fonts.conf on the wall, then: fc-cache -f -->
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <match target="pattern">
    <edit name="family" mode="prepend" binding="strong">
      <string>Noto Color Emoji</string>
    </edit>
  </match>
</fontconfig>
```

Restart the browser afterwards (fontconfig is read at process start). This is a
wall-machine setting, not app code — it lives here as an operator note.

## Guards

- `tests/js/osk.test.mjs` — the pure transform, incl. symbol/emoji insert and
  grapheme-aware backspace (surrogate pair, variation selector, ZWJ sequence).
- `tests/test_static.py` — structural guards for kiosk activation, OS-keyboard
  suppression, the symbol/emoji layers, the grapheme helper, the CSS, and the
  `renderTodosPaint` focus/caret preservation that keeps the keyboard from being
  dropped by a background list refresh.
