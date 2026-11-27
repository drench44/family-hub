"""The GitHub page (README.md) must not fall out of date.

The README is what a stranger sees first, so a feature that ships without a
word in it, a Settings section nobody mentions, a picture link that 404s or a
gallery that shows a wall from months ago all make the project look dead.
These checks catch each of those in CI and in the deploy gate.

Rebuild the pictures with `python art/make_gallery.py`; see art/README.md.
The tests that read repo files are marked engine_checkout (conftest skips them
on the staged deploy tree, which has no .git and the house README).
"""
import json
import os
import pathlib
import re
import subprocess
from types import SimpleNamespace

import pytest

from family_hub.integrations import available_integrations

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
STATIC = ROOT / "src" / "family_hub" / "web" / "static"

# How many commits to the files that change how the wall looks may pass since
# the gallery was last rebuilt. The count on 2026-11-27 was 4; 40 is roughly
# "a few weeks of ordinary work" for this repo, enough that a real redesign
# trips it and a tweak does not.
MAX_UI_CHANGES = 40
UI_FILES = ("src/family_hub/web/static/styles.css",
            "src/family_hub/web/static/hub.js",
            "src/family_hub/web/static/common.js")

# Registry id -> words the README may use for it (any one is enough, case
# does not matter). A new registry id with no entry here fails the test, so
# whoever adds an integration decides how the README names it.
README_NAMES = {
    "chores": ("chores",),
    "rewards": ("chore wallet",),
    "todos": ("to-dos",),
    "backups": ("backups",),
    "google_calendar": ("google calendar",),
    "ics_calendar": ("ics feed", "ics/webcal"),
    "icloud_caldav": ("icloud",),
    "cameras": ("cameras",),
    "weather": ("weather",),
    "climate": ("climate",),
    "laundry": ("laundry",),
    "fleet": ("fleet console",),
    "printer": ("3d printer",),
    "away_dim": ("dim when nobody is home",),
}
# Registry ids that are deliberately not in the README. Say why.
README_EXEMPT: dict[str, str] = {}

# Pictures the gallery script writes that the README does not show.
# hub.png is the full-page capture the script makes for its own use.
UNLINKED_OK = {"docs/hub.png"}


# ---- pure helpers (tested on broken input below) ----

def registry_ids() -> list[str]:
    """Every integration id the registry can list, available or not."""
    cfg = SimpleNamespace(calendars=[], fleet=None, laundry=None)
    return [i["id"] for i in available_integrations(cfg, {})]


def has_phrase(low: str, phrase: str) -> bool:
    """Whole-word match, so "ics" is not found inside "heuristics"."""
    return bool(re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", low))


def integrations_missing(readme: str, ids, names=README_NAMES, exempt=README_EXEMPT):
    low = readme.lower()
    out = []
    for iid in ids:
        if iid in exempt:
            continue
        if iid not in names:
            out.append(f"{iid} (no README name chosen in README_NAMES)")
        elif not any(has_phrase(low, w) for w in names[iid]):
            out.append(f"{iid} (README should say {names[iid][0]!r})")
    return out


def settings_sections(js: str) -> list[str]:
    block = re.search(r"const SETTINGS_SECTIONS = \[(.*?)\];", js, re.S)
    assert block, "SETTINGS_SECTIONS not found in hub.js"
    return re.findall(r"\['[\w-]+',\s*'([^']+)'\]", block.group(1))


def sections_missing(readme: str, labels):
    """A label counts only in its bold form (**Display**), which the README's
    Settings bullet uses; plain words like "display" or "looks" appear everywhere."""
    return [s for s in labels if f"**{s}**" not in readme]


def linked_pictures(readme: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="(docs/[\w./-]+\.(?:png|jpe?g))"', readme)
               + re.findall(r"\]\((docs/[\w./-]+\.(?:png|jpe?g))\)", readme))


def produced_pictures(cfg: dict) -> set[str]:
    """What art/make_gallery.py writes for the README, from gallery.json."""
    out = {"docs/hub.png", "docs/seasons.jpg", "docs/phone.png"}
    for s in cfg["shots"]:
        out |= {f"docs/gallery/{s['name']}.jpg", f"docs/gallery/{s['name']}-thumb.jpg"}
    out.add(f"docs/gallery/{cfg['phone']['name']}-thumb.jpg")
    return out


def picture_problems(readme: str, cfg: dict, on_disk: set[str]):
    linked, produced = linked_pictures(readme), produced_pictures(cfg)
    problems = [f"README links {p}, which does not exist" for p in sorted(linked - on_disk)]
    problems += [f"{p} is built by make_gallery.py but the README never shows it"
                 for p in sorted(produced - linked - UNLINKED_OK)]
    problems += [f"{p} is in docs/gallery but gallery.json does not make it"
                 for p in sorted(p for p in on_disk
                                 if p.startswith("docs/gallery/") and p not in produced)]
    return problems


def staleness_message(n: int, limit: int = MAX_UI_CHANGES):
    if n > limit:
        return (f"The README pictures are {n} UI changes old; rebuild them "
                f"with `python art/make_gallery.py`.")
    return None


# ---- the helpers catch what they claim to (broken copies) ----

def test_missing_integration_is_caught():
    assert integrations_missing("Chores and weather only", ["laundry"])
    assert integrations_missing("Laundry", ["brand_new"])        # unmapped id
    assert not integrations_missing("Laundry", ["brand_new"], exempt={"brand_new": "internal"})


def test_missing_settings_section_is_caught():
    assert sections_missing("**Display** only", ["Display", "Backups"]) == ["Backups"]
    assert sections_missing("the display modes and looks", ["Display", "Looks"]) == ["Display", "Looks"]


def test_heuristics_is_not_ics():
    assert integrations_missing("a few heuristics here", ["ics_calendar"])
    assert not integrations_missing("any ICS feed works", ["ics_calendar"])
    assert not integrations_missing("any ICS/webcal feeds", ["ics_calendar"])


def test_broken_pictures_are_caught():
    cfg = {"shots": [{"name": "dark"}], "phone": {"name": "phone"}}
    readme = '<img src="docs/gallery/dark.jpg"><img src="docs/gallery/gone.jpg">'
    on_disk = {"docs/gallery/dark.jpg", "docs/gallery/stray.jpg"}
    text = "\n".join(picture_problems(readme, cfg, on_disk))
    assert "gone.jpg, which does not exist" in text          # dead link
    assert "dark-thumb.jpg is built" in text                 # orphan produced
    assert "stray.jpg is in docs/gallery" in text            # orphan on disk


def test_staleness_message_is_plain():
    assert staleness_message(41) == ("The README pictures are 41 UI changes old; "
                                     "rebuild them with `python art/make_gallery.py`.")
    assert staleness_message(40) is None


# ---- the real README ----

@pytest.mark.engine_checkout
def test_every_integration_is_in_the_readme():
    missing = integrations_missing(README.read_text(), registry_ids())
    assert not missing, "The README does not name: " + "; ".join(missing)


@pytest.mark.engine_checkout
def test_every_settings_section_is_in_the_readme():
    labels = settings_sections((STATIC / "hub.js").read_text())
    assert len(labels) >= 5, labels
    missing = sections_missing(README.read_text(), labels)
    assert not missing, "The README never names these Settings sections: " + ", ".join(missing)


@pytest.mark.engine_checkout
def test_readme_pictures_exist_and_none_are_orphans():
    cfg = json.loads((ROOT / "art" / "gallery.json").read_text())
    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*")
               if p.suffix in (".png", ".jpg", ".jpeg")}
    problems = picture_problems(README.read_text(), cfg, on_disk)
    assert not problems, "\n".join(problems)


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)


@pytest.mark.engine_checkout
def test_readme_pictures_are_not_stale():
    probe = _git("rev-parse", "--is-shallow-repository")
    if probe.returncode != 0:
        pytest.skip("git history is not available here")
    if probe.stdout.strip() == "true":
        why = ("shallow clone: the history needed to count UI changes is missing "
               "(CI checks out with fetch-depth: 0)")
        if os.environ.get("CI"):
            pytest.fail(why + "; in CI that means the check never ran")
        pytest.skip(why)
    last = _git("log", "-1", "--format=%H", "--", "docs/gallery")
    sha = last.stdout.strip()
    if last.returncode != 0 or not sha:
        # The gallery pictures exist (the orphan test requires them), so a
        # history that never touched them means a rename or a broken pathspec,
        # never "nothing to check".
        if (ROOT / "docs" / "gallery").is_dir():
            pytest.fail("docs/gallery exists but no commit touches it: was it renamed?")
        pytest.skip("no commit has touched docs/gallery yet")
    n = _git("rev-list", "--count", f"{sha}..HEAD", "--", *UI_FILES)
    assert n.returncode == 0, n.stderr
    msg = staleness_message(int(n.stdout))
    assert msg is None, msg
