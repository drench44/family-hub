"""Rebuild the README pictures from the demo.

    python3 art/make_gallery.py            (or --only dark phone, to redo some)

Run it from a Python that has the app's requirements plus Playwright with
Chromium (pip install playwright; python -m playwright install chromium). It
starts the demo itself on 127.0.0.1:8141 (--port to change) and stops it after.

It writes, all from the same demo:
  docs/hub.png                     the full page at 1920 px wide
  docs/phone.png                   the phone home screen in a phone frame
  docs/gallery/<name>.jpg          each shot of art/gallery.json on its kitchen screen
  docs/gallery/<name>-thumb.jpg    the small copy the README strip shows

Rules it keeps (art/README.md has the why): the page is captured at exactly
the screen's shape, so it is never stretched; the picture is cropped around
the screen so the screen fills `zoom` of its width.
"""
import argparse
import datetime
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parent.parent
ART = ROOT / "art"
DOCS = ROOT / "docs"


# ---- geometry (pure, tested in tests/test_art_gallery.py) ----

def screen_size(quad):
    """Mean width and height of a screen given as TL, TR, BR, BL corners."""
    w = (quad[1][0] - quad[0][0] + quad[2][0] - quad[3][0]) / 2
    h = (quad[3][1] - quad[0][1] + quad[2][1] - quad[1][1]) / 2
    return w, h


def viewport_for(quad, page_width):
    """The browser size whose shape matches the screen, page_width wide."""
    w, h = screen_size(quad)
    return page_width, round(page_width * h / w)


def solve(a, b):
    """Solve a x = b for a small square system (Gaussian elimination with partial pivoting)."""
    n = len(b)
    m = [list(map(float, row)) + [float(v)] for row, v in zip(a, b)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ValueError("the four corners do not make a usable screen (three in a line?)")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def perspective_coeffs(dst, src):
    """PIL PERSPECTIVE coefficients mapping output points dst onto input points src."""
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return solve(m, [c for p in src for c in p])


def zoom_box(size, quad, fill):
    """A 16:9 crop box around the screen in which the screen is `fill` of the width."""
    W, H = size
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    cw = min(W, (max(xs) - min(xs)) / fill)
    ch = min(H, cw * 9 / 16)
    cw = ch * 16 / 9
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2 + ch * 0.06   # a little more room below: the counter, the shelf
    left = min(max(0, cx - cw / 2), W - cw)
    top = min(max(0, cy - ch / 2), H - ch)
    return round(left), round(top), round(left + cw), round(top + ch)


def paste_on_screen(scene, shot, quad):
    """Warp `shot` onto the screen corners of `scene` (both PIL images)."""
    W, H = scene.size
    w, h = shot.size
    warped = shot.transform((W, H), Image.PERSPECTIVE,
                            perspective_coeffs(quad, [(0, 0), (w, 0), (w, h), (0, h)]), Image.BICUBIC)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon([tuple(p) for p in quad], fill=255)
    out = scene.copy()
    out.paste(warped, (0, 0), mask.filter(ImageFilter.GaussianBlur(0.8)))
    return out


def phone_frame(shot):
    """The phone screenshot inside a plain black phone body, on a transparent background."""
    w, h = shot.size
    pad = round(w * 0.045)
    r_out, r_in = round(w * 0.14), round(w * 0.1)
    body = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(body).rounded_rectangle((0, 0, body.width - 1, body.height - 1), r_out, fill=(18, 18, 20, 255))
    inner = Image.new("L", shot.size, 0)
    ImageDraw.Draw(inner).rounded_rectangle((0, 0, w - 1, h - 1), r_in, fill=255)
    body.paste(shot.convert("RGB"), (pad, pad), inner)
    return body


# ---- capture ----

def prefs_script(theme, look):
    """Page script that sets this device's look before the hub paints (theme.js reads these keys)."""
    items = {"fh.theme": theme, "fh.season": "on" if look else "off"}
    if look:
        items["fh.lookPin"] = look
    return "".join(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in items.items())


def daytime_zone(now_utc, hour):
    """A fixed time zone in which it is about `hour` o'clock now, as an IANA
    name. The hub dims itself from 22:00 to 06:00 local time (body.is-night),
    so a screenshot taken in the evening came out gray. Pinning the browser's
    clock instead broke every relative time (a washer "553 min" from done), so
    the demo server and the browser both run in this zone: real instants, a
    daytime local hour, and one shared date."""
    off = (hour - now_utc.hour) % 24
    if off > 14:
        off -= 24
    if off == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}"   # the Etc/GMT signs are inverted by design


def capture(browser, url, width, height, theme, look, zone, full_page=False, scale=1):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale,
                              timezone_id=zone)
    ctx.add_init_script("localStorage.clear();" + prefs_script(theme, look))
    page = ctx.new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2500)   # cards, camera stills and the look's picture settle
    if look:
        got = page.evaluate("document.documentElement.getAttribute('data-look')")
        if got != look:
            raise SystemExit(f"the page shows look {got!r}, not {look!r}: is the look id right?")
    if page.evaluate("document.body.classList.contains('is-night')"):
        raise SystemExit(f"the hub is in its night dimming in {zone}: check gallery.json hour")
    png = page.screenshot(full_page=full_page)
    ctx.close()
    return Image.open(io.BytesIO(png)).convert("RGB")


def serve_demo(port, zone):
    """Start the demo on 127.0.0.1:port in `zone`, wait until it answers, return the process."""
    tmp = tempfile.mkdtemp(prefix="fh-gallery-")
    env = {**os.environ, "DEMO": "1", "DISABLE_SYNC": "1", "DB_PATH": f"{tmp}/demo.db",
           "CONFIG_PATH": str(ROOT / "config.demo.json"), "PYTHONPATH": str(ROOT / "src"), "TZ": zone}
    log = open(f"{tmp}/demo.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "family_hub.app:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit(f"the demo stopped at start; see {tmp}/demo.log")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            return proc
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"the demo did not answer in 30 s; see {tmp}/demo.log")


def save_shot(pic, gal, name, cfg):
    big = pic.resize((cfg["out_width"], round(cfg["out_width"] * pic.height / pic.width)), Image.LANCZOS)
    big.save(gal / f"{name}.jpg", quality=86)
    big.resize((cfg["thumb_width"], round(cfg["thumb_width"] * big.height / big.width)),
               Image.LANCZOS).save(gal / f"{name}-thumb.jpg", quality=85)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8141, help="port for the demo this script starts")
    ap.add_argument("--only", nargs="*", help="shot names to redo: hub, phone or a gallery.json name")
    a = ap.parse_args()
    cfg = json.loads((ART / "gallery.json").read_text())
    screens = json.loads((ART / "kitchen" / "screens.json").read_text())
    gal = DOCS / "gallery"
    gal.mkdir(exist_ok=True)
    want = (lambda n: not a.only or n in a.only)
    zone = daytime_zone(datetime.datetime.now(datetime.timezone.utc), cfg["hour"])
    url = f"http://127.0.0.1:{a.port}/"
    demo = serve_demo(a.port, zone)
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            if want("hub"):
                capture(browser, url, cfg["page_width"], 1080, "grey", None, zone,
                        full_page=True).save(DOCS / "hub.png")
                print("docs/hub.png")
            ph = cfg["phone"]
            if want(ph["name"]):
                shot = capture(browser, url, ph["width"], ph["height"], ph["theme"], ph["look"], zone,
                               scale=ph["scale"])
                framed = phone_frame(shot)
                framed.save(DOCS / "phone.png")
                thumb_h = round(cfg["thumb_width"] * 9 / 16)
                t = framed.copy()
                t.thumbnail((cfg["thumb_width"], thumb_h))
                canvas = Image.new("RGB", (cfg["thumb_width"], thumb_h), (24, 26, 30))
                canvas.paste(t, ((canvas.width - t.width) // 2, (canvas.height - t.height) // 2), t)
                canvas.save(gal / f"{ph['name']}-thumb.jpg", quality=85)
                print("docs/phone.png")
            for s in cfg["shots"]:
                if not want(s["name"]):
                    continue
                quad = screens[s["scene"]]
                vw, vh = viewport_for(quad, cfg["page_width"])
                shot = capture(browser, url, vw, vh, s["theme"], s["look"], zone)
                scene = Image.open(ART / "kitchen" / f"{s['scene']}.jpg").convert("RGB")
                save_shot(paste_on_screen(scene, shot, quad).crop(zoom_box(scene.size, quad, cfg["zoom"])),
                          gal, s["name"], cfg)
                print(f"docs/gallery/{s['name']}.jpg")
            browser.close()
    finally:
        demo.terminate()


if __name__ == "__main__":
    main()
