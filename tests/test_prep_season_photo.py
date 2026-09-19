"""scripts/prep-season-photo.py: the one tool that turns a downloaded photo
into a seasonal look's image. Pillow is a CI test dependency (ci.yml), so
these run everywhere; they never skip."""
import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prep(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("prep", ROOT / "scripts" / "prep-season-photo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(mod, "REPO", tmp_path)
    return mod


def _src(tmp_path, img, name="src.png", **save):
    p = tmp_path / name
    img.save(p, **save)
    return p


def _run(prep, *args):
    return prep.main(["prep", *[str(a) for a in args]])


def test_writes_a_2560_webp_with_no_metadata(prep, tmp_path):
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"                     # Make
    src = _src(tmp_path, Image.new("RGB", (3000, 2000), (200, 120, 40)), "a.jpg", exif=exif)
    assert _run(prep, src, "fall-test") == 0
    out = tmp_path / "fall-test.webp"
    data = out.read_bytes()
    assert b"EXIF" not in data and b"SecretCam" not in data, "metadata must be stripped"
    with Image.open(out) as im:
        assert im.size == (2560, 1707)


def test_transparency_is_flattened_onto_white_not_black(prep, tmp_path):
    img = Image.new("RGBA", (2600, 100), (0, 0, 0, 0))   # fully transparent (black underneath)
    src = _src(tmp_path, img)
    assert _run(prep, src, "fall-alpha") == 0
    with Image.open(tmp_path / "fall-alpha.webp") as im:
        r, g, b = im.convert("RGB").getpixel((10, 10))
    assert min(r, g, b) > 240, "transparent pixels must come out white, not black bands"


def test_refuses_16_bit_scans_rather_than_washing_them_out(prep, tmp_path):
    src = _src(tmp_path, Image.new("I;16", (2600, 100)), "scan.png")
    assert _run(prep, src, "fall-scan") == 2
    assert not (tmp_path / "fall-scan.webp").exists()


def test_never_silently_replaces_a_shipped_photo(prep, tmp_path):
    src = _src(tmp_path, Image.new("RGB", (2600, 100), (10, 20, 30)))
    (tmp_path / "fall-keep.webp").write_bytes(b"shipped")
    assert _run(prep, src, "fall-keep") == 2
    assert (tmp_path / "fall-keep.webp").read_bytes() == b"shipped"
    assert _run(prep, src, "fall-keep", "--force") == 0
    assert (tmp_path / "fall-keep.webp").read_bytes()[:4] == b"RIFF"
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"


@pytest.mark.parametrize("name", ["Fall-Aspen", "fall aspen", "../x", "aspen"])
def test_names_must_be_registry_look_ids(prep, tmp_path, name):
    src = _src(tmp_path, Image.new("RGB", (2600, 100)))
    assert _run(prep, src, name) == 2


def test_warns_when_the_source_is_too_small_to_be_sharp(prep, tmp_path, capsys):
    src = _src(tmp_path, Image.new("RGB", (1200, 800)))
    assert _run(prep, src, "fall-small") == 0
    assert "only 1200px wide" in capsys.readouterr().out


def _embedded_profile():
    # a real ICC profile to embed (sRGB; for L and palette images it does not
    # match the pixels, which is exactly the case that used to crash)
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@pytest.mark.parametrize("mode", ["P", "RGBA", "RGB", "L"])
def test_images_with_a_colour_profile_never_crash(prep, tmp_path, mode):
    """A palette PNG with an ICC profile used to raise PyCMSError (the profile
    was applied before the mode conversion). Every mode now converts."""
    img = Image.new("RGB", (2600, 100), (30, 140, 200)).convert(mode)
    src = _src(tmp_path, img, f"icc-{mode}.png", icc_profile=_embedded_profile())
    assert _run(prep, src, f"fall-icc-{mode.lower()}") == 0
    with Image.open(tmp_path / f"fall-icc-{mode.lower()}.webp") as im:
        assert im.mode == "RGB" and im.width == 2560


def test_camera_rotation_is_honoured(prep, tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6                                 # Orientation: rotate 90 CW
    src = _src(tmp_path, Image.new("RGB", (3000, 2000)), "rot.jpg", exif=exif)
    assert _run(prep, src, "fall-rot") == 0
    with Image.open(tmp_path / "fall-rot.webp") as im:
        assert im.height > im.width, "a portrait shot stays portrait"
