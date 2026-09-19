#!/usr/bin/env python3
"""Prepare a photo or artwork for a seasonal look.

    pip install pillow            # a dev-only tool; the app never imports it
    python scripts/prep-season-photo.py SOURCE.jpg fall-aspen-grove [--width 2560] [--quality 80] [--soften 0]

Writes src/family_hub/web/static/seasons/<name>.webp: at most 2560px wide
(sharp on the 1920px wall and on a phone's portrait crop), sRGB, WebP at
quality 80, with all metadata stripped (EXIF can carry GPS and camera
serials, and this repo is public). Prints the result's size; aim for well
under 600 KB so a phone on cellular loads it quickly. A busy, high-detail
photo (a whole grove of leaves) compresses badly: --soften 1.2 applies a
blur too faint to see from across a kitchen and usually halves the file;
drop --width to 1920 before dropping --quality below ~72.

Read docs/seasonal-looks.md first: it says which sources and licences are
allowed, how to pick an image that works behind the cards, and what to put
in static/seasons/CREDITS.md.
"""
import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageCms, ImageFilter, ImageOps

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "src" / "family_hub" / "web" / "static" / "seasons"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("name", help="the look id, e.g. fall-aspen-grove")
    ap.add_argument("--width", type=int, default=2560)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--soften", type=float, default=0.0, help="Gaussian blur radius in px (0 = none)")
    args = ap.parse_args(argv[1:])
    src, name = args.source, args.name
    if not name.replace("-", "").isalnum():
        print(f"name must be letters, digits and hyphens (a look id), got {name!r}")
        return 2
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)       # honour camera rotation before the tags go
    # Convert to sRGB BEFORE the profile is dropped: an Adobe RGB or Display P3
    # original (common in park-service exports) shown as if it were sRGB comes
    # out washed out. The profile itself is not written to the output.
    icc = img.info.get("icc_profile")
    if icc:
        img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                        ImageCms.createProfile("sRGB"), outputMode="RGB")
    img = img.convert("RGB")                  # drops alpha/CMYK/palette; WebP wants RGB
    if img.width > args.width:
        img = img.resize((args.width, round(img.height * args.width / img.width)), Image.LANCZOS)
    if args.soften > 0:
        img = img.filter(ImageFilter.GaussianBlur(args.soften))
    out = OUT_DIR / f"{name}.webp"
    img.save(out, "WEBP", quality=args.quality, method=6)   # no exif=/icc_profile=: nothing carried over
    kb = out.stat().st_size / 1024
    print(f"{out.relative_to(REPO).as_posix()}  {img.width}x{img.height}  {kb:.0f} KB")
    if kb > 600:
        print("warning: over 600 KB; consider a calmer image or a lower quality")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
