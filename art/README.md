# Art: how every made picture in this repo is remade

Three kinds of picture in family-hub were made with a local image model, and
the README pictures are built from the demo. This folder holds everything
needed to remake any of them after the app or its look changes: the exact
recipes, the scenes the owner picked, the scripts, and the requirements and
lessons from making them the first time. Read the lessons before making new
ones; each was learned the hard way.

| What | Where it ends up | Recipe | Finish step |
|---|---|---|---|
| Christmas looks (12) | `static/seasons/christmas-*.webp` | `recipes/christmas-looks.json` | `scripts/prep-season-photo.py` |
| Demo camera stills (5) | `static/demo-cams/*.webp` | `recipes/demo-cameras.json` | `art/finish_cameras.py` |
| Kitchen scenes (3) | `art/kitchen/*.jpg` | `recipes/kitchen-scenes.json` | `art/find_screen.py` |
| README gallery | `docs/gallery/`, `docs/hub.png`, `docs/phone.png` | `art/gallery.json` | `art/make_gallery.py` |

## After a change to the app: rebuild the README pictures

One command, from the repo root, in a Python that has the app's requirements
plus Playwright (`pip install playwright`, then `python -m playwright install chromium`):

    python art/make_gallery.py

It starts the demo itself, photographs it in each look of `art/gallery.json`,
pastes each shot onto its kitchen screen and writes `docs/hub.png`,
`docs/phone.png` and `docs/gallery/`. `--only christmas phone` redoes just
those. To show a different look, theme or scene, edit `art/gallery.json`; the
README strip lists the shots by name, and `tests/test_art_gallery.py` checks
that every shot names a scene and a look that exist and that the README shows
each one.

What the script holds to, and why:

- **Never stretched.** The browser window is set to the exact shape of the
  screen it lands on (about 1920x1150). The first previews pasted a tall
  full-page screenshot into a wide screen and looked squashed.
- **Zoomed in.** The picture is cropped so the screen is half its width
  (`zoom` 0.5). The owner asked for this: "Maybe want to zoom in a bit."
- **Daytime.** The hub dims itself from 10pm to 6am. The demo and the browser
  both run in a time zone where it is early afternoon now (`hour` 13), so a
  late-evening run is not gray, and relative times (the washer's minutes left)
  stay right. Pinning the browser's clock instead made the washer read "553
  min". The script stops if the page is still dimmed.
- **Real camera pictures** show in the camera cards (the demo stills), led
  by the driveway. A camera looking out from inside the front door looked
  odd to the owner, so that still was retired.
- **A lit screen.** The shot is brightened a little (`glow`), since a screen
  reads brighter than the room; pasted as-is the dark themes looked murky.
  The light theme gets none (`"glow": 1.0` on its shot): brightened, its
  cards wash out.
- **A light theme on a dark frame.** Light on the white wall-mount scene
  did not read as a screen at all, so light themes go on the counter scene.

## Remaking or adding a generated picture

The pictures come from **FLUX.2 klein 4B** at 1536x864, then **SeedVR2**
upscaling 2x to 3072x1728. Both are Apache 2.0, so the pictures are ours to
ship. Never use a non-commercial model (Qwen-Image, for one) for anything in
this repo. `docs/seasonal-looks.md` section 5 has the full rule for AI pictures.

1. On the machine running ComfyUI and its `gen.py` helper:
   `python run_recipes.py recipes/<file>.json --gen <path to gen.py> --python <ComfyUI venv python> --out <folder>`.
   A recipe entry holds the prompt and seed, so the same model files remake
   the same picture. To make something new, add an entry with a new seed,
   and make two or three seeds of each to choose from.
2. **Look at every picture at full size**, not a thumbnail. Check for
   people, faces, text, logos, license plates, house numbers and warped
   objects (a second bezel, a melted chair). Redo a bad one with a new seed
   and a sentence in the prompt that forbids the fault.
3. **The owner picks.** Pictures that will be seen on the wall or the GitHub
   page are chosen by the owner from a contact sheet or a phone picker. Never
   ship one the owner has not picked, unless they hand the choice over.
4. Run the finish step from the table, add a row to the matching
   `CREDITS.md`, and update the recipe file to the kept seed and prompt.

### Requirements by kind

**Seasonal looks** (the owner, round 1 rejected: "They need to be more
festive, really feel like the holiday spirit and magic"):

- Festive and magical, not just pretty: classic red, green and warm gold,
  many lit windows and string lights, gentle snow, a storybook feel.
- 16:9, a calm area where the cards float (they cover most of the picture),
  no text, no logos, no people's faces.
- Each look needs `light` points in `theme.js` (lamps, fire, twinkle, panes)
  so its lights glow; `docs/seasonal-looks.md` section 6 covers the rest.

**Demo camera stills:**

- A realistic home security camera view from above (porch, yard, driveway,
  mailbox, side gate) of a made-up house, daytime.
- No people, no readable text, no license plates, no house numbers.
- The model draws a fisheye ring; `finish_cameras.py` keeps the center 70%.

**Kitchen scenes** (for the gallery):

- The camera stands dead in front of the screen, square to the wall, so the
  screen is a flat rectangle. Opposite edges more than about 5 px apart mean
  it is tilted: remake it. Three of the first six had to be redone for this.
- The screen shows a flat, even **charcoal gray** (not black, which loses its
  edges), with no reflections and no content.
- The bezel is one plain frame: no markings, no printed calendar, no second
  panel. "A single simple flat frame, the same width on all four sides" in
  the prompt fixed a warped double bezel.
- The screen fills about a third of the full picture, so the crop has room.
- `find_screen.py` finds the corners; for a scene whose wall is close to the
  screen's color, give it a rough box (evening-b needs one, see its help).

## What is not in the repo

The rejected candidates and the full-size originals stay on the owner's
machines (about 750 MB). Everything needed to remake the kept pictures is
here: a recipe and the same model files give the same picture.
