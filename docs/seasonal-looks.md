# Seasonal looks: design standards and how to add one

A seasonal look turns the whole wall into the season: a real photograph (or
public-domain artwork) fills the screen, the dashboard floats on it as frosted
glass in the colours of your chosen theme, a few leaves drift down over it, and the accent colour and a
small mark by the wordmark match the photo. It follows the calendar when
**Season** is on, and each device picks its own look from preview tiles in
**All settings → Seasonal looks**.

This document is the standard every look is held to. It exists because the
first three attempts at fall missed, for reasons worth not repeating. Read it
before adding Halloween, Christmas, winter, spring, or anything else.

**What's next, in the owner's order:** Halloween, then Thanksgiving, then
Christmas. The first two sit inside fall's Sep 1 to Nov 30 window, so list
each one in `SEASONS` before fall, since the first matching window wins.
Each gets its own photos, falling shapes (leaves for fall; think bats or
candlelight for Halloween, snow for Christmas, never cartoon props) and a
matching accent.

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
| 4 | **Real public-domain photos behind glass cards** | "looks better"; refined below, then shipped |

**The lesson: art is not something to generate.** Every premium product uses
professional photography or commissioned illustration. When we cannot
commission art, we use real photographs and public-domain fine art. Never
draw scenes in code, never use clip-art props (cartoon pumpkins, ghosts,
Santa hats), never use emoji as decoration.

### Lessons from the fall build

Each of these cost at least one round with the owner, or would have shipped
a bug. Read them before the next season.

**How to work with the owner**
- **Show real candidates and let them pick.** The breakthrough was a
  contact sheet of 16 vetted, licence-clean images, plus trials of the best
  few behind the actual wall. The owner picked in one step. Guessing on
  their behalf burned three rounds.
- **Look at what comparable products do before designing** (section 3).
  "Look at what other apps do so we're not designing from scratch" came
  after two rejected designs.
- **Offer choices as real screenshots, never descriptions.** Offer a spectrum
  (a few looks per season) rather than one bet.

**What "done" means for a look**
- **Look at every combination full size before calling it done.** That's
  every look × all five themes, seasons off, night, reduced motion, the
  phone, the gear popover and the Settings tiles. When asked "have you
  looked at all of them?", the honest answer was no. Thumbnails and a
  sample hid these real bugs:
  - the gear popover opened *under* the cards (glass makes the top bar its
    own stacking layer);
  - the phone page spilled sideways, but only from 10 o'clock (a two-digit
    hour on the clock is wider);
  - empty checkbox rings vanished on light glass;
  - with reduced motion, resting leaves covered words forever;
  - Blue, Grey and Black were identical with a season on;
  - photos were soft (compressed too hard) and leaves were invisible
    (behind the glass, same colour as the photo).
- **Compare seasons-off against main pixel for pixel.** A seasonal change
  must not move a single pixel for a family that never turns it on.
- **Sharp beats small.** Never soften or downsize a photo to save bytes. The
  wall is a big screen seen up close, and the home network is fast.
- **Check that the moving parts can actually be seen.** Motion that nobody
  notices (leaves behind glass) is just cost.
- **Every surface belongs to someone.** Give each token a clear owner: the
  theme owns glass, wash and palette; the look owns photo, leaves and accent.
  When a look repainted everything, it flattened the five themes into one.

**Traps on the tooling side**
- **The browser serves stale CSS and JS.** Assets keep `?v=1.4.1` between
  releases, so a test browser can run old code against new files. Twice this
  looked like a bug that wasn't there. Clear the cache (Playwright:
  `Network.clearBrowserCache` + `setCacheDisabled` over CDP) before judging
  any change.
- **The demo only listens on this machine** unless started with
  `--host 0.0.0.0`. The owner couldn't open it from another device until
  that changed.
- **Windows checkouts convert to CRLF.** Multi-line find-and-replace in
  PowerShell silently matches nothing. Use an editor tool, or normalise line
  endings first, and verify every edit landed.
- **Commit, then run the full Python suite again.** The privacy scan only
  reads tracked files (it once read packed SVG path numbers as an IP
  address).

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
  `backdrop-filter: blur(22px) saturate(1.35)`. Below about 70% opacity,
  small details (empty checkbox rings, streak pills) disappear over busy
  photos. Check them.
- **The theme owns the glass and the wash; the look owns the photo.** Every
  theme keeps its character with a season on ("they should still work with
  the seasons on"): Light is 80% white glass, Soft 82% warm cream, Blue 70%
  navy, Grey 68% neutral grey, Black 76% near-black over the deepest dusk.
  Ink, borders and every other colour stay the theme's own. A look sets only
  its photo, focal point, leaf colours and accent; a test fails if a look
  sets a theme-owned token. (An early version let the look repaint
  everything, and Blue, Grey and Black all turned into one charcoal.)
- **Night:** the night dim stops the glass from blurring, so from 22:00 to
  06:00 the glass goes nearly solid (94% of the theme's surface).
- Never put `backdrop-filter` on a fixed or sticky element (iOS tap bug, see
  CLAUDE.md). Glass rules live in `:where()` so sections with their own card
  colour (today's calendar header, the weather sky) keep it.

**Colour**
- The accent comes from the photo (aspen gold, rust, maple orange), in a
  lighter tone for dark themes and a deeper one for Light/Soft so it reads on
  white glass.
- Cards stay in the theme's own colours. The photo carries the season.

**Motion**
- Only the drifting leaves (or, later, snow): six shapes, 22 to 44px,
  opaque, transform only, paused at night, still under reduced motion.
- **Two depths** (the owner's idea): six near leaves (22 to 44px, shadowed)
  drift over the cards, and six far leaves (16 to 24px, slower, no shadow)
  fall inside the photo layer, behind the glass, so a card they pass behind
  blurs them. Size, speed and blur together read as depth. Under reduced
  motion the far ones rest behind the glass and the near layer is hidden.
- The near leaves drift **over the cards** in their own layer (`.season-fx`, last in
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
5. **Style it** in `styles.css`. Copy an existing look's two blocks: the
   dark-theme block first (photo `--sn-scene`, focal point `--sn-pos`, leaf
   colours, accent), then the light-theme block (just the deeper accent).
   Add the `.season-mark` rule. Don't touch the glass, wash or any surface
   colour: those belong to the theme. `test_static.py` fails until every look
   token is there, and if a look sets a theme's token.
6. **Check it with your own eyes**, on the demo (`DEMO=1`), at full size:
   - every look × all five themes (Light, Soft, Blue, Grey, Black) at
     1920×1080, and seasons off in all five (it must match main exactly);
   - night mode (add `is-night` to `<body>`) and reduced motion;
   - a phone width (390px and 360px), with a two-digit hour on the clock
     ("12:59:59pm" is the widest; a narrower time hid an overflow);
   - the gear popover open;
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
- `hub.js` mounts two layers: `.season` (the photo) as the first child of
  `<body>`, and `.season-fx` (the leaves) as the last. It builds the Settings
  tiles from the same leaf markup.
- `styles.css` sets the tokens. The theme owns `--glass`, `--glass-edge`,
  `--sn-wash` and the palette; each look owns the rest:

  | Token | What it is | Set by |
  | --- | --- | --- |
  | `--sn-scene` | the photo | look |
  | `--sn-pos` | where the photo is anchored | look |
  | `--sn-leaf-1`…`4` | the leaf colours | look |
  | `--accent`, `--accent-ink`, `--accent-soft` | the accent, matched to the photo | look (light- and dark-theme tones) |
  | `--sn-wash` | the gradient over the photo | theme |
  | `--glass`, `--glass-edge` | the frosted cards | theme |
  | `--ground`, `--surface`, `--ink`, … | the usual palette | theme |

  The scene paints the wash, then the photo, then the ground colour.
- `/seasons/` is served `no-cache`, so a photo swapped under the same name
  reaches phones after a release.
