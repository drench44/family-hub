#!/usr/bin/env python3
"""Make the frosted-glass copy of every seasonal photo.

    pip install pillow            # a dev-only tool; the app never imports it
    python scripts/make-glass.py              # every photo in static/seasons/
    python scripts/make-glass.py fall-aspen-grove

Writes src/family_hub/web/static/seasons/glass/<name>.webp: the photo shrunk
to 640px wide, blurred and saturated the way the cards' old live blur did it
(blur(26px) saturate(1.4) on a 1920px wall), WebP at quality 80, no metadata.

Why it exists: on wide screens the cards paint this copy, fixed to the
viewport, instead of blurring whatever is behind them live
(backdrop-filter). The live blur re-ran every frame a far bat or leaf moved
under a card, and on the wall's Firefox and a laptop it sometimes drew a row
of the card as a dark streaky band ("still glitching", owner, 2026-10-14).
A still copy never re-blurs. See docs/seasonal-looks.md.

prep-season-photo.py runs this for the photo it writes, so a new look gets
its glass copy without a second step.
"""
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

REPO = Path(__file__).resolve().parents[1]
SEASONS = REPO / "src" / "family_hub" / "web" / "static" / "seasons"
GLASS = SEASONS / "glass"
LOOK_ID = re.compile(r"^[a-z]+-[a-z0-9-]+$")
WIDTH = 640
# blur(26px) on screen; the wall shows a 2560px photo at about 1920px, so the
# blur is about 35px of the full photo, or 9px at 640 wide
SIGMA = 26 * (2560 / 1920) * (WIDTH / 2560)
SATURATE = 1.4


def make(name, seasons=SEASONS):
    """Write seasons/glass/<name>.webp from seasons/<name>.webp; return its path."""
    src = seasons / f"{name}.webp"
    img = Image.open(src).convert("RGB")
    small = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS)
    # pad before blurring so the edges blur toward their own colors, not black
    pad = int(SIGMA * 3)
    big = Image.new("RGB", (small.width + 2 * pad, small.height + 2 * pad))
    big.paste(small.resize(big.size, Image.BILINEAR))
    big.paste(small, (pad, pad))
    soft = big.filter(ImageFilter.GaussianBlur(SIGMA)).crop((pad, pad, pad + small.width, pad + small.height))
    soft = ImageEnhance.Color(soft).enhance(SATURATE)
    glass = seasons / "glass"
    glass.mkdir(exist_ok=True)
    out = glass / f"{name}.webp"
    # temp file then swap, so a failed encode never leaves a half-written copy
    tmp = out.with_name(out.name + ".tmp")
    try:
        soft.save(tmp, "WEBP", quality=80, method=6)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"glass/{out.name}  {soft.width}x{soft.height}  {out.stat().st_size / 1024:.0f} KB")
    return out


def main(argv):
    names = argv[1:] or sorted(p.stem for p in SEASONS.glob("*.webp"))
    bad = [n for n in names if not LOOK_ID.match(n) or not (SEASONS / f"{n}.webp").is_file()]
    if bad:                                   # check every name before writing any
        print(f"error: no seasons/<name>.webp for {', '.join(bad)}", file=sys.stderr)
        return 2
    for name in names:
        make(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
