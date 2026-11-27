"""Turn generated camera pictures into the demo's camera stills.

    python art/finish_cameras.py <folder of <output>.png from run_recipes.py>

Keeps the center 70% (it drops the black fisheye ring the model draws), cuts
16:9 and writes 1280x720 webp to src/family_hub/web/static/demo-cams/.
Add a row to that folder's CREDITS.md for any new still.
"""
import pathlib
import sys

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "src/family_hub/web/static/demo-cams"
KEEP = 0.70


def finish(src, dst):
    im = Image.open(src).convert("RGB")
    w, h = im.size
    cw, ch = int(w * KEEP), int(w * KEEP * 9 / 16)
    box = ((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)
    im.crop(box).resize((1280, 720), Image.LANCZOS).save(dst, quality=80, method=6)


if __name__ == "__main__":
    for p in sorted(pathlib.Path(sys.argv[1]).glob("*.png")):
        finish(p, OUT / f"{p.stem}.webp")
        print(OUT / f"{p.stem}.webp")
