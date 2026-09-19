# Seasonal looks: design standards and how to add one

A seasonal look turns the whole wall into the season: a real photograph (or
public-domain artwork) fills the screen, the dashboard floats on it as frosted
glass, a few leaves drift down behind the cards, and the accent colour and a
small mark by the wordmark match the photo. It follows the calendar when
**Season** is on, and each device picks its own look from preview tiles in
**All settings → Seasonal looks**.

This document is the standard every look is held to. It exists because the
first three attempts at fall missed, for reasons worth not repeating. Read it
before adding Halloween, Christmas, winter, spring, or anything else.

---

## 1. The bar

The owner's words, which are the spec:

- "nice and appealing, pleasing to look at, where you get that holiday or
  seasonal feel, not cheap or cheesy"
- "Apple or Google design standards"
- "keep it happy colors"
- "just want it to feel like you are immersed in that holiday or season fully"
- "keep the menu system clean and organized"

## 2. What we tried, and why it failed

| Attempt | What it was | Verdict |
| --- | --- | --- |
| 1 | Layered hill silhouettes (CSS masks) in dusk palettes, cards solid on top | Muddy, barely visible, and the dark reds read as "the whole world is on fire" |
| 2 | Flat illustrated scenes drawn by a generator script (barn, pumpkins, trees as circles) | "too cartoonish", "some of these shapes look too basic" |
| 3 | The same generator with organic shapes (noisy canopies, spruce silhouettes, fractal mountains) | "look like a 3rd grader made them in MS Paint in 2003" |
| 4 | **Real public-domain photos behind glass cards** | Shipped |

**The lesson: art is not something to generate.** Every premium product uses
professional photography or commissioned illustration. When we cannot
commission art, we use real photographs and public-domain fine art. Never
draw scenes in code, never use clip-art props (cartoon pumpkins, ghosts,
Santa hats), never use emoji as decoration.

## 3. What other products do

Surveyed before settling on this design (2026-09):

- **Apple.** macOS dynamic wallpapers are one photographed scene at several
  times of day; Apple TV aerials have day and night variants; StandBy shows
  photos. Apple never puts holiday skins on its UI. Season is carried by
  light and landscape.
- **Google.** Calendar's twelve monthly header illustrations were drawn by a
  named illustrator (Lotta Nieminen) and say the season through palette, not
  props. Nest Hub's photo frame uses curated photography and fine art.
- **DAKboard** (the closest product to this one). A photo background with a
  per-block blur and an adjustable dark gradient overlay so text over a busy
  photo stays readable.
- **Skylight, Echo Show, Samsung Family Hub, Aura, Cozyla.** Photos and
  curated art. Holiday flavour is small accents, not a costume. The clip-art
  end of the market (some Mango Display templates, Etsy screensaver packs,
  licensed cartoon characters) is exactly what we avoid.

Patterns we copied: real photos, a readability layer between the art and the
UI, day and evening variants of one image, automatic by date with a manual
pick from thumbnails. Pitfall we weigh: animation over the UI. Our leaves
started behind the glass and nobody could see them, so they now drift over
the cards, kept few, small and slow (see Motion below).

## 4. Visual standards

**Image**
- A real photograph or public-domain artwork, never generated.
- Calm and happy: the season shown through subject and light. Sunlit gold,
  soft fog, backlit leaves. No red washes, no gloom, no clip-art props.
- Immersive: it fills the screen (`cover`), not a strip at the bottom.
- Sharp: the original must be at least 2560px wide, shipped at 2560px, never
  softened. A photo that is soft by nature (fog, a blurred background) still
  needs its subject in focus.
- It must survive being cropped: the wall shows the whole frame, a phone in
  portrait shows roughly the middle third. Set the look's `--sn-pos` to its
  focal point.

**Readability layer** (the wall is a working tool first)
- A **wash** over the photo: a light, airy gradient for Light/Soft and a
  deeper dusk gradient for Blue/Grey/Black. The same photo serves both.
- **Glass** for everything on top: cards, their buttons, the section titles
  and the top bar get a translucent fill (`--glass`) and
  `backdrop-filter: blur(22px) saturate(1.35)`. Light glass is 80% white,
  dark glass about 68% charcoal. Below that, small details (empty checkbox
  rings, streak pills) disappear over busy photos. Check them.
- Never put `backdrop-filter` on a fixed or sticky element (iOS tap bug, see
  CLAUDE.md). Glass rules live in `:where()` so sections with their own card
  colour (today's calendar header, the weather sky) keep it.

**Colour**
- The accent comes from the photo (aspen gold, rust, maple orange), in a
  lighter tone for dark themes and a deeper one for Light/Soft so it reads on
  white glass.
- Cards stay neutral charcoal or white. The photo carries the season.

**Motion**
- Only the drifting leaves (or, later, snow): six shapes, 22 to 44px,
  opaque, transform only, paused at night, still under reduced motion.
- They drift **over the cards** in their own layer (`.season-fx`, last in
  `<body>`). Behind the glass they were nearly invisible ("the leaves
  falling are a bit hard to see"), and the same gold as the photo hid them
  further. The layer never takes a tap, stays under the top bar and every
  menu and overlay, and stops above the phone's tab bar. Keep it to a
  handful of slow shapes, so it reads as weather, not as noise over text.
- Each leaf gets a small soft drop shadow so it lifts off a photo of the
  same colour. Never blur a moving layer: a blurred moving element makes the
  wall's small GPU (an i3 iGPU) re-blur it every frame.

**Mark**
- A small silhouette beside the wordmark in the accent colour (a leaf for
  fall). One per look, from a licensed icon set.

**Menu**
- The gear popover has only **Season: Off / On**. Everything else lives in
  **All settings → Seasonal looks**: tiles grouped by season, each showing
  a live preview, a name, one line of description and a credit.

## 5. Sourcing and licences

This repo is public and MIT licensed, so every file must be redistributable.

**Use**
- **US federal works** (National Park Service, US Fish and Wildlife, NASA):
  public domain. Wikimedia Commons hosts many NPS photos in full resolution.
- **Wikimedia Commons** files marked CC0 or public domain, after checking
  the licence review on the file page.
- **Museum open access, CC0:** Art Institute of Chicago, The Met, National
  Gallery of Art, Cleveland Museum of Art, Smithsonian. Great for
  illustration-style looks: Hiroshige and Hokusai woodblock prints, Monet,
  Van Gogh, the Hudson River School.
- **Unsplash photos uploaded before June 2017** (CC0 at the time), which
  Commons mirrors as "(Unsplash)" files.
- **Icons:** Phosphor (MIT) and Twemoji (CC-BY 4.0), credited.

**Avoid**
- Freepik and Vecteezy: their free licences forbid redistributing the file.
- The current Unsplash, Pexels and Pixabay licences: free to use, but they
  forbid "compiling" the images into a similar service. That's a grey area
  for a public repo, so don't use them.
- Kawase Hasui and other 1920s–30s Japanese prints: US copyright was likely
  restored under URAA and may still apply.
- Anything share-alike (CC-BY-SA) or non-commercial (NC).

Every file in `static/seasons/` gets a row in `static/seasons/CREDITS.md`:
file, title and creator, source link, licence, and what we changed. A test
fails if one is missing.

**Candidates already vetted for later** (all CC0 or public domain; details
in the 2026-09 sourcing notes): Jasper Cropsey *Autumn on the Hudson* (NGA),
Hiroshige *Maples at Mama* (Met), George Inness *Sunny Autumn Day* (CMA),
Monet *Stacks of Wheat, End of Day, Autumn* (AIC), Tosa Mitsuoki *Autumn
Maples with Poem Slips* screen (AIC), Karl Fredrickson "Trees in Fall" bokeh
(Unsplash 2015, CC0), Aaron Burden maple canopy on blue sky (Unsplash 2015,
CC0), Shenandoah NPS rolling hills.

## 6. Adding a look or a season

1. **Find the image** under the rules above. Download the original and
   write down its title, creator, source URL and licence.
2. **Prepare it:** `pip install pillow`, then
   `python scripts/prep-season-photo.py original.jpg <look-id>`. That writes
   `static/seasons/<look-id>.webp`: 2560px wide, converted to sRGB,
   re-encoded and stripped of metadata. **Never soften it or make it narrower
   to save bytes.** We tried that on the aspen photo and the wall showed it
   as blur ("some of the pics look blurry"). The hub runs on the home network
   and fetches each photo once per release, so ~2 MB for a leafy photo is
   fine. If a file tops the 2.6 MB guard, lower `--quality` to 76 first. The
   source needs to be at least 2560px wide; smaller originals will look soft.
3. **Credit it:** add a row to `static/seasons/CREDITS.md`.
4. **Register it** in `theme.js`'s `SEASONS`. Give it a name, a blurb and a
   credit, and mark one look per season `default: true`. A new season needs
   its date window. List a short holiday (Halloween) *before* the broad
   season it falls inside (fall), because the first window that matches wins.
5. **Style it** in `styles.css`. Copy an existing look's two palette blocks:
   the evening block first, the daytime block second. Set `--sn-scene`,
   `--sn-pos`, `--sn-wash`, `--glass`, the accent and the leaf colours, then
   add the `.season-mark` rule. `test_static.py` fails until every token is
   there.
6. **Check it with your own eyes**, on the demo (`DEMO=1`), at full size:
   - every look × Light and a dark theme at 1920×1080;
   - a phone width (390px);
   - the Settings tiles;
   - the empty checkbox rings, the streak pills, the section titles and the
     top-bar text.

   A thumbnail grid is not a review.
7. **Run the gauntlet** in `docs/adding-a-feature.md`. Run the full Python
   suite again **after committing**, because the privacy scan only reads
   tracked files.

## 7. How it works (reference)

- `theme.js` owns the registry, the prefs (`fh.season`, `fh.look.<season>`)
  and the derived `data-look` attribute on `<html>`. It stamps them before
  first paint, and `tickClock` in hub.js re-derives them when the date turns.
- `hub.js` mounts the `.season` layer (the leaves) as the first child of
  `<body>`, and builds the Settings tiles from the same markup.
- `styles.css` sets the look tokens per look and theme family:

  | Token | What it is |
  | --- | --- |
  | `--sn-scene` | the photo |
  | `--sn-pos` | where the photo is anchored |
  | `--sn-wash` | the gradient over the photo |
  | `--glass`, `--glass-edge` | the frosted cards |
  | `--sn-leaf-1`…`4` | the leaf colours |
  | `--ground`, `--surface`, … `--accent` | the usual palette |

  The scene paints the wash, then the photo, then the ground colour.
- `/seasons/` is served `no-cache`, so a photo swapped under the same name
  reaches phones after a release.
