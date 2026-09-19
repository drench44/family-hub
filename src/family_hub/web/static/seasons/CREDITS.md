# Seasonal look artwork

Every file in this folder is public domain, CC0, or made for this project,
and ships inside the app, so the wall never loads art from the internet.
Sourcing rules and how to add more: [`docs/seasonal-looks.md`](../../../../../docs/seasonal-looks.md).

## Photos

Each photo was resized and re-encoded by `scripts/prep-season-photo.py`
(WebP, metadata stripped); `fall-aspen-grove.webp` was also softened very
slightly to keep the file small.

| File | Photo | Source | Licence |
| --- | --- | --- | --- |
| `fall-aspen-grove.webp` | "Golden Aspens, Mosca Pass Trail", Patrick Myers, National Park Service (Great Sand Dunes National Park and Preserve) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Golden_Aspens,_Mosca_Pass_Trail_(36725223263).jpg) | Public domain (work of the US federal government) |
| `fall-misty-road.webp` | "Foggy fall road", Bernd Schulz | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Foggy_fall_road_(Unsplash).jpg) (from Unsplash, 2016) | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (published under Unsplash's CC0 terms, before its 2017 licence change) |
| `fall-maple-light.webp` | "Fall Foliage October 28, 2024", Luca Pfeiffer, National Park Service (Shenandoah National Park) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Fall_Foliage_October_28,_2024.jpg) | Public domain (work of the US federal government) |

## Shapes

| File | Source | Licence |
| --- | --- | --- |
| `leaf-maple.svg` | [Twemoji](https://github.com/jdecked/twemoji) 1f341 "maple leaf". Changed: fill colour removed, path numbers given explicit separators (same shape) | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/), © Twitter, Inc and other contributors |
| `leaf-slender.svg` | [Phosphor Icons](https://github.com/phosphor-icons/core) "leaf" (fill), unchanged | MIT, notice below |

The leaf shapes are used as CSS masks, so each look paints them in its own colours.

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
