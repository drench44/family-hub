#!/usr/bin/env python3
"""Prepare a photo or artwork for a seasonal look.

    pip install pillow            # a dev-only tool; the app never imports it
    python scripts/prep-season-photo.py SOURCE.jpg fall-aspen-grove [--width 2560] [--quality 82]

Writes src/family_hub/web/static/seasons/<name>.webp: at most 2560px wide
(the wall is 1920px and a phone zooms into the middle, so both stay crisp),
sRGB, WebP at quality 82, with all metadata stripped (EXIF can carry GPS and
camera serials, and this repo is public). Prints the result's size.

Sharpness beats file size here: the hub runs on the home network and a photo
is fetched once per release, then only revalidated. A detailed photo (a whole
grove of leaves) can reach ~2.3 MB and that is fine; never soften or shrink a
photo below 2560px to save bytes. Both were tried, and the wall showed it as
blur ("some of the pics look blurry").

Read docs/seasonal-looks.md first: it says which sources and licences are
allowed, how to pick an image that works behind the cards, and what to put
in static/seasons/CREDITS.md.
"""
import argparse
import io
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "src" / "family_hub" / "web" / "static" / "seasons"
LOOK_ID = re.compile(r"^[a-z]+-[a-z0-9-]+$")      # same shape theme.js's registry test enforces
MAX_BYTES = 2600 * 1024                            # same cap test_static.py enforces
# Modes Pillow converts to RGB faithfully. 16-bit greyscale (I;16, common in
# archive TIFF scans) would clamp to near-white, so it is refused, not guessed.
SAFE_MODES = {"1", "L", "LA", "P", "PA", "RGB", "RGBA", "CMYK", "YCbCr"}


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    return 2


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("name", help="the look id, e.g. fall-aspen-grove")
    ap.add_argument("--width", type=int, default=2560)
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--force", action="store_true", help="replace an existing photo of the same name")
    args = ap.parse_args(argv[1:])
    src, name = args.source, args.name
    if not LOOK_ID.match(name):
        return fail(f"name must be a look id like fall-aspen-grove (lowercase, hyphens), got {name!r}")
    if not 320 <= args.width <= 8000 or not 1 <= args.quality <= 100:
        return fail("--width must be 320-8000 and --quality 1-100")
    out = OUT_DIR / f"{name}.webp"
    if out.exists() and not args.force:
        return fail(f"{out.name} already ships; pass --force to replace it")

    img = Image.open(src)
    img = ImageOps.exif_transpose(img)       # honour camera rotation before the tags go
    if img.mode not in SAFE_MODES:
        return fail(f"unsupported image mode {img.mode} (e.g. a 16-bit scan); convert it to 8-bit RGB first")
    # Read the colour profile first: flattening builds a new image without it.
    icc = img.info.get("icc_profile")
    # Transparency: flatten onto white. Dropping alpha would keep whatever
    # colour sits under transparent pixels (usually black bands).
    if img.mode in ("P", "PA", "LA", "RGBA") or "transparency" in img.info:
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    elif img.mode not in ("RGB", "CMYK", "L"):
        img = img.convert("RGB")
    # Convert to sRGB before the profile is dropped: an Adobe RGB or Display P3
    # original (common in park-service exports) shown as if it were sRGB comes
    # out washed out. The profile itself is not written to the output. A
    # profile that doesn't match the pixels is reported, not fatal.
    if icc:
        try:
            img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                            ImageCms.createProfile("sRGB"), outputMode="RGB")
        except (ImageCms.PyCMSError, OSError) as e:
            print(f"warning: could not apply the embedded colour profile ({e}); colours used as-is")
    img = img.convert("RGB")
    if img.width < args.width:
        print(f"warning: the source is only {img.width}px wide; it will look soft on the wall. "
              f"Find an original at least {args.width}px wide.")
    elif img.width > args.width:
        img = img.resize((args.width, round(img.height * args.width / img.width)), Image.LANCZOS)

    # Write to a temp file and swap it in, so a failed encode never leaves a
    # shipped photo half-written. No exif=/icc_profile=: nothing carried over.
    tmp = out.with_name(out.name + ".tmp")
    try:
        img.save(tmp, "WEBP", quality=args.quality, method=6)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    size = out.stat().st_size
    print(f"{out.relative_to(REPO).as_posix()}  {img.width}x{img.height}  {size / 1024:.0f} KB")
    if size >= MAX_BYTES:
        print("warning: at or over the 2.6 MB test cap; try --quality 76 before anything that costs sharpness")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))