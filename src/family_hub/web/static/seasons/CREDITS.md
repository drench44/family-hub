# Seasonal look artwork

Every file in this folder is public domain, CC0, or under a permissive
licence credited below (CC-BY 4.0, MIT), and ships inside the app, so the wall never loads art from the internet.
Sourcing rules and how to add more: [`docs/seasonal-looks.md`](../../../../../docs/seasonal-looks.md).

## Photos

Each photo was resized to 2560px wide, converted to sRGB and re-encoded by
`scripts/prep-season-photo.py` (WebP, metadata stripped).

| File | Photo | Source | Licence |
| --- | --- | --- | --- |
| `fall-aspen-grove.webp` | "Golden Aspens, Mosca Pass Trail", Patrick Myers, National Park Service (Great Sand Dunes National Park and Preserve) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Golden_Aspens,_Mosca_Pass_Trail_(36725223263).jpg) | Public domain (work of the US federal government) |
| `fall-misty-road.webp` | "Foggy fall road", Bernd Schulz | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Foggy_fall_road_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `fall-maple-sky.webp` | "Fall foliage", Aaron Burden | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Fall_foliage_(Unsplash).jpg) (from Unsplash, 2015) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `halloween-moonrise.webp` | "Moonrise through the trees", Yellowstone National Park | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Moonrise_through_the_trees_(51955672977).jpg) | Public domain (work of the US federal government) |
| `halloween-two-lanterns.webp` | "Carved pumpkin as face", Beth Teutschmann | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Carved_pumpkin_as_face_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `halloween-purple-sky.webp` | "Sublime purple night sky", Vincentiu Solomon | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Sublime_purple_night_sky_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `halloween-purple-pines.webp` | "Trees against purple night sky", Ryan Hutton | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Trees_against_purple_night_sky_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `halloween-branches.webp` | "Dark branches at dusk", Vladimir Agafonkin | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Dark_branches_at_dusk_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |

## Shapes

| File | Source | Licence |
| --- | --- | --- |
| `leaf-maple.svg` | [Twemoji](https://github.com/jdecked/twemoji) 1f341 "maple leaf". Changed: fill colour removed, path numbers given explicit separators (same shape) | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/), © Twitter, Inc and other contributors |
| `leaf-slender.svg` | [Phosphor Icons](https://github.com/phosphor-icons/core) "leaf" (fill), unchanged | MIT, notice below |
| `bat.svg` | "Bat shadow black", Rugby471, [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Bat_shadow_black.svg). Changed: optimised, fill colour removed (same shape) | Public domain (released by its author) |
| `spider-web.svg` | "Spiders web", tom, [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Spiders_web.svg). Changed: optimised; the strands keep one hairline width at any size | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

Every shape is used as a CSS mask, so each look paints it in its own colours.

## Animation frames

Two sprite sheets, each one row of frames stepped through by CSS (a wingbeat,
a walk cycle). Both were reduced to a silhouette (the drawing's own colours
dropped, its outline kept), so the look paints them like every other shape.

| File | Source | Licence |
| --- | --- | --- |
| `bat-flap.png` | [Noto Animated Emoji](https://googlefonts.github.io/noto-emoji-animation/) "bat" (1f987), 15 of its 30 wingbeat frames | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/), © Google |
| `spider-walk.png` | The spider sprite from [Bug.js](https://github.com/Auz/Bug), Graham McNicoll (after Screen Bug by kernc), walk row only, 7 frames | MIT-style (Bug.js); the original Screen Bug is WTFPL |

## Phosphor Icons licence

```
MIT License

Copyright (c) 2023 Phosphor Icons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
