"""The README gallery tool (art/make_gallery.py) and the art it reads.

The geometry is what made the first previews look wrong: a tall page squeezed
into a wide screen. These pin the rules that fixed it."""
import importlib.util
import json
import pathlib

import pytest
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
ART = ROOT / "art"
spec = importlib.util.spec_from_file_location("make_gallery", ART / "make_gallery.py")
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

QUAD = [[1000, 400], [2100, 400], [2100, 1060], [1000, 1060]]   # 1100 x 660, like the kitchen screens


def test_the_page_is_captured_at_the_screens_shape_so_it_is_never_stretched():
    w, h = mg.viewport_for(QUAD, 1920)
    assert w == 1920
    assert abs(w / h - 1100 / 660) < 0.002


def test_the_perspective_maps_each_screen_corner_to_the_shots_corner():
    src = [(0, 0), (1920, 0), (1920, 1152), (0, 1152)]
    a, b, c, d, e, f, g, h = mg.perspective_coeffs(QUAD, src)
    for (x, y), (u, v) in zip(QUAD, src):
        den = g * x + h * y + 1
        assert abs((a * x + b * y + c) / den - u) < 1e-6
        assert abs((d * x + e * y + f) / den - v) < 1e-6


def test_corners_in_a_line_are_refused_not_turned_into_garbage():
    flat = [[0, 0], [10, 0], [20, 0], [30, 0]]
    with pytest.raises(ValueError):
        mg.perspective_coeffs(flat, [(0, 0), (1, 0), (1, 1), (0, 1)])


def test_the_zoom_box_is_16_9_inside_the_scene_with_the_screen_at_the_asked_share():
    l, t, r, b = mg.zoom_box((3072, 1728), QUAD, 0.5)
    assert 0 <= l < r <= 3072 and 0 <= t < b <= 1728
    assert abs((r - l) / (b - t) - 16 / 9) < 0.01
    assert abs(1100 / (r - l) - 0.5) < 0.01
    assert l <= 1000 and r >= 2100 and t <= 400 and b >= 1060


def test_the_zoom_box_never_leaves_the_scene_for_a_screen_near_the_edge():
    near = [[20, 30], [1120, 30], [1120, 690], [20, 690]]
    l, t, r, b = mg.zoom_box((3072, 1728), near, 0.5)
    assert l == 0 and t == 0 and r <= 3072 and b <= 1728


def test_the_shot_lands_inside_the_screen_and_nowhere_else():
    scene = Image.new("RGB", (3072, 1728), (200, 200, 200))
    shot = Image.new("RGB", (1920, 1152), (255, 0, 0))
    out = mg.paste_on_screen(scene, shot, QUAD)
    assert out.getpixel((1550, 730)) == (255, 0, 0)
    assert out.getpixel((900, 730)) == (200, 200, 200)
    assert out.getpixel((1550, 1200)) == (200, 200, 200)


def test_the_phone_frame_keeps_the_screenshot_and_has_clear_corners():
    shot = Image.new("RGB", (390, 844), (0, 128, 0))
    framed = mg.phone_frame(shot)
    assert framed.mode == "RGBA" and framed.width > 390 and framed.height > 844
    assert framed.getpixel((0, 0))[3] == 0
    assert framed.getpixel((framed.width // 2, framed.height // 2))[:3] == (0, 128, 0)


@pytest.mark.parametrize("utc_hour", range(24))
def test_screenshots_run_in_a_zone_where_it_is_daytime_now_so_the_night_dimming_never_shows(utc_hour):
    import datetime
    from zoneinfo import ZoneInfo
    hour = json.loads((ART / "gallery.json").read_text())["hour"]
    now = datetime.datetime(2026, 11, 27, utc_hour, 17, tzinfo=datetime.timezone.utc)
    zone = mg.daytime_zone(now, hour)
    local = now.astimezone(ZoneInfo(zone))   # a real zone name, or this raises
    assert local.hour == hour
    assert not (local.hour >= 22 or local.hour < 6)   # common.js nightClass


def test_the_look_script_sets_theme_and_pins_the_look_with_seasons_on():
    s = mg.prefs_script("dark", "halloween-moonrise")
    assert 'setItem("fh.theme", "dark")' in s
    assert 'setItem("fh.season", "on")' in s and 'setItem("fh.lookPin", "halloween-moonrise")' in s
    plain = mg.prefs_script("light", None)
    assert 'setItem("fh.season", "off")' in plain and "fh.lookPin" not in plain


def test_every_gallery_shot_names_a_scene_that_exists_and_a_look_the_hub_knows():
    cfg = json.loads((ART / "gallery.json").read_text())
    screens = json.loads((ART / "kitchen" / "screens.json").read_text())
    theme_js = (ROOT / "src/family_hub/web/static/theme.js").read_text()
    names = [s["name"] for s in cfg["shots"]] + [cfg["phone"]["name"]]
    assert len(names) == len(set(names))
    for s in cfg["shots"]:
        assert s["scene"] in screens, s
        assert (ART / "kitchen" / f"{s['scene']}.jpg").exists(), s
        assert f'"{s["theme"]}"' in theme_js, s
        if s["look"]:
            assert f'id: "{s["look"]}"' in theme_js, s


def test_each_scene_has_four_corners_inside_its_picture():
    screens = json.loads((ART / "kitchen" / "screens.json").read_text())
    for name, quad in screens.items():
        with Image.open(ART / "kitchen" / f"{name}.jpg") as im:
            W, H = im.size
        assert len(quad) == 4
        assert all(0 <= x < W and 0 <= y < H for x, y in quad), name


def test_every_kept_picture_has_a_recipe_that_can_remake_it():
    for f in sorted((ART / "recipes").glob("*.json")):
        doc = json.loads(f.read_text())
        assert doc["generator"]["width"] % 32 == 0 and doc["generator"]["height"] % 32 == 0
        assert doc["pictures"], f.name
        for p in doc["pictures"]:
            assert p["prompt"].strip() and isinstance(p["seed"], int) and p["output"], (f.name, p)
    kitchen = json.loads((ART / "recipes" / "kitchen-scenes.json").read_text())
    screens = json.loads((ART / "kitchen" / "screens.json").read_text())
    assert {p["output"] for p in kitchen["pictures"]} == set(screens)
    cams = json.loads((ART / "recipes" / "demo-cameras.json").read_text())
    for p in cams["pictures"]:
        assert (ROOT / "src/family_hub/web/static/demo-cams" / f"{p['output']}.webp").exists(), p["output"]


def test_the_readme_shows_every_gallery_picture_and_each_file_exists():
    readme = (ROOT / "README.md").read_text()
    cfg = json.loads((ART / "gallery.json").read_text())
    for s in cfg["shots"]:
        assert f"docs/gallery/{s['name']}-thumb.jpg" in readme, s["name"]
        assert f"docs/gallery/{s['name']}.jpg" in readme, s["name"]
        assert (ROOT / "docs/gallery" / f"{s['name']}.jpg").exists(), s["name"]
        assert (ROOT / "docs/gallery" / f"{s['name']}-thumb.jpg").exists(), s["name"]
