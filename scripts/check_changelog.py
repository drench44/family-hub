#!/usr/bin/env python3
"""Enforce that code changes are logged in CHANGELOG.md.

A change under ``src/`` must add a bullet to the ``## [Unreleased]`` section,
or to a release section it cuts itself (release.py run on the PR branch).
Docs-only (``*.md``) and test-only (``tests/``) diffs are exempt — the same
carve-out CLAUDE.md gives the review gate.

Runs two ways:

  * CI (blocking on PRs):   ``check_changelog.py --base <ref>``
        compares the changed files and the CHANGELOG between ``<ref>`` and the
        working tree (GitHub Actions passes the PR base).
  * Local pre-commit hook:  ``check_changelog.py --staged``
        compares staged changes against HEAD.

Exit 0 = fine, 1 = a required changelog entry is missing (with the fix printed).
Stdlib only; the pure predicates are unit-tested in tests/test_check_changelog.py.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_BULLET = re.compile(r"^[-*]\s+(.+?)\s*$")

_FIX = ("changelog-guard: code changed under src/ but CHANGELOG.md's "
        "[Unreleased] gained no entry.\n"
        "  Add a bullet under '## [Unreleased]' describing the change, e.g.:\n"
        "    ### Added\n    - What you added\n"
        "  (docs-only and test-only changes are exempt.)")


def requires_entry(changed_paths: list[str]) -> bool:
    """True iff any changed path is app code under src/ (not markdown). Docs and
    tests are exempt."""
    return any(p.startswith("src/") and not p.endswith(".md")
               for p in changed_paths)


# Exactly the files scripts/release.py writes and commits (its `rel_paths`):
# VERSION, the rolled CHANGELOG.md, and index.html with its ?v= stamps. A test
# pins this list to release.py.
RELEASE_FILES = frozenset({
    "VERSION", "CHANGELOG.md", "src/family_hub/web/static/index.html"})


def is_release(changed_paths: list[str]) -> bool:
    """True iff this diff is a release cut by scripts/release.py: it bumps
    VERSION and ROLLS [Unreleased] into a dated section rather than adding a
    bullet, so it's exempt: it can't satisfy the [Unreleased]-entry rule by
    design (that's the whole point of a release).

    It must touch VERSION and NOTHING outside RELEASE_FILES. Any VERSION change
    used to exempt the whole diff, so a feature PR that also bumped VERSION
    skipped the changelog rule."""
    paths = set(changed_paths)
    return "VERSION" in paths and paths <= RELEASE_FILES


_SECTION_HEAD = re.compile(r"^##\s*\[([^\]]+)\].*$", re.MULTILINE)


def _sections(changelog: str) -> dict[str, set[str]]:
    """Every `## [name]` section's bullets, keyed by the name in brackets."""
    heads = list(_SECTION_HEAD.finditer(changelog))
    out = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(changelog)
        out[h.group(1).strip().lower()] = {
            b.group(1) for line in changelog[h.end():end].splitlines()
            if (b := _BULLET.match(line))}
    return out


def gained_entry(base_changelog: str, head_changelog: str) -> bool:
    """True iff the change wrote at least one bullet that was nowhere in the
    base changelog, under [Unreleased] or under a release section this change
    added. The second case is the house habit of running release.py on the PR
    branch before merge: the PR's own bullets then sit in the new dated section
    and [Unreleased] is empty. Bullets merely rolled from the base's
    [Unreleased], or copied from an older release, don't count."""
    base = _sections(base_changelog)
    seen = set().union(*base.values()) if base else set()
    head = _sections(head_changelog)
    fresh = set(head.get("unreleased", set()))
    for name, bullets in head.items():
        if name != "unreleased" and name not in base:
            fresh |= bullets
    return bool(fresh - seen)


# --- git-facing wiring ------------------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def _changelog_at(ref: str) -> str:
    """CHANGELOG.md as of a VALID ref, or '' if the file simply didn't exist
    there yet (a brand-new changelog — every entry is then a genuine gain).

    Only a genuinely-absent path returns ''. Any OTHER git failure (an
    unresolvable ref, a broken repo) is NOT swallowed — it propagates and the
    guard fails loud. A silent-open guard is worse than no guard: an enforcement
    gate must fail closed, so we never let a git hiccup quietly wave a PR
    through. `ref` is always validated (merge-base / HEAD) before we get here."""
    present = subprocess.run(["git", "cat-file", "-e", f"{ref}:CHANGELOG.md"],
                             cwd=REPO_ROOT, capture_output=True, text=True)
    if present.returncode != 0:
        return ""   # path absent at this valid ref — new file
    return _git("show", f"{ref}:CHANGELOG.md")   # check=True — real errors fail loud


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enforce CHANGELOG [Unreleased] entries.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--base", help="git ref to compare against (CI / PR base)")
    g.add_argument("--staged", action="store_true",
                   help="compare staged changes against HEAD (pre-commit hook)")
    args = ap.parse_args(argv)

    changelog_now = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if args.staged:
        changed = _git("diff", "--cached", "--name-only").split()
        base_ref = "HEAD"
    else:
        base = args.base or "origin/main"
        # Compare against the MERGE BASE, not the base tip, so the changelog
        # diff lines up with the three-dot file diff below. Reading the base tip
        # instead let a PR branched before a release (which emptied [Unreleased]
        # on the base) pass for free and resurrect already-shipped bullets.
        base_ref = _git("merge-base", base, "HEAD").strip()
        changed = _git("diff", "--name-only", base_ref, "HEAD").split()

    base_changelog = _changelog_at(base_ref)

    if not requires_entry(changed):
        return 0
    if is_release(changed):
        return 0   # a release rolls [Unreleased]; it can't (and shouldn't) add one
    if gained_entry(base_changelog, changelog_now):
        return 0
    print(_FIX, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
