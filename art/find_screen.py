"""Find the screen's four corners in a kitchen scene and save them.

    python art/find_screen.py art/kitchen/<scene>.jpg [x0,y0,x1,y1]

The optional box (scene pixels) is a rough frame around the screen, for a
scene whose wall is close to the screen's color; evening-b needs
1120,390,2330,1170.

The scenes are made with a flat, even charcoal screen (see art/README.md), so
the screen is the big block of pixels near the color at the middle of the dark
area. It prints the corners (top-left, top-right, bottom-right, bottom-left)
and the two edge lengths each way: opposite edges more than about 5 px apart
mean the camera was not square to the screen, so remake the scene rather than
paste onto a tilted screen. Writes the corners into art/kitchen/screens.json.
Needs numpy.
"""
import json
import pathlib
import sys

import numpy as np
from PIL import Image

KITCHEN = pathlib.Path(__file__).resolve().parent / "kitchen"


def corners(path, near=None):
    """`near` (x0, y0, x1, y1): a rough box around the screen, for a scene whose
    walls are close to the screen's color (evening-b needed one)."""
    im = np.asarray(Image.open(path).convert("RGB")).astype(float)
    lum = im.mean(axis=2)
    if near:
        x0, y0, x1, y1 = near
        mask = np.zeros(lum.shape, bool)
        mask[y0:y1, x0:x1] = (lum < 140)[y0:y1, x0:x1]
    else:
        ys, xs = np.nonzero(lum < 110)
        cy, cx = int(np.median(ys)), int(np.median(xs))
        c = np.median(im[cy - 30:cy + 30, cx - 30:cx + 30].reshape(-1, 3), axis=0)
        mask = np.abs(im - c).max(axis=2) < 22
    cols, rows = mask.sum(axis=0), mask.sum(axis=1)
    xr = np.nonzero(cols > 0.5 * cols.max())[0]
    yr = np.nonzero(rows > 0.5 * rows.max())[0]
    x0, x1, y0, y1 = xr.min(), xr.max(), yr.min(), yr.max()
    m = np.zeros_like(mask)
    m[y0:y1 + 1, x0:x1 + 1] = mask[y0:y1 + 1, x0:x1 + 1]

    def side_x(a, b):
        left, right = [], []
        for y in range(a, b):
            row = np.nonzero(m[y])[0]
            if len(row):
                left.append(row.min())
                right.append(row.max())
        return int(np.median(left)), int(np.median(right))

    tl_x, tr_x = side_x(y0 + 15, y0 + 45)
    bl_x, br_x = side_x(y1 - 45, y1 - 15)
    top = (lambda x: int(np.nonzero(m[:, x])[0].min()))
    bot = (lambda x: int(np.nonzero(m[:, x])[0].max()))
    return [[tl_x, top(tl_x + 8)], [tr_x, top(tr_x - 8)], [br_x, bot(br_x - 8)], [bl_x, bot(bl_x + 8)]]


if __name__ == "__main__":
    path = pathlib.Path(sys.argv[1])
    near = [int(v) for v in sys.argv[2].split(",")] if len(sys.argv) > 2 else None
    q = corners(path, near)
    print(path.stem, q, "top/bottom width", q[1][0] - q[0][0], q[2][0] - q[3][0],
          "left/right height", q[3][1] - q[0][1], q[2][1] - q[1][1])
    f = KITCHEN / "screens.json"
    screens = json.loads(f.read_text()) if f.exists() else {}
    screens[path.stem] = q
    f.write_text(json.dumps(screens, indent=1) + "\n")
