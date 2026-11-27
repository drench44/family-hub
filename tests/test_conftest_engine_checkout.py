"""The engine_checkout hook in tests/conftest.py, exercised end to end.

A scratch tree with a copy of the real conftest and one marked test stands in
for each situation: a git checkout, a staged deploy tree (no .git), and a
no-.git copy where FH_REQUIRE_ENGINE_CHECKS=1 says the engine checks must run
(the OMEN test script sets it). Each runs in a child pytest so the real hook
code is what decides.
"""
import os
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

PYTEST_INI = """[pytest]
markers =
    engine_checkout: engine repo shape checks
"""
SAMPLE = """import pytest

@pytest.mark.engine_checkout
def test_marked():
    pass

def test_unmarked():
    pass
"""


def _run(tmp_path, *, git, require):
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copy(REPO / "tests" / "conftest.py", tests / "conftest.py")
    (tests / "test_sample.py").write_text(SAMPLE)
    (tmp_path / "pytest.ini").write_text(PYTEST_INI)
    if git:
        (tmp_path / ".git").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "FH_REQUIRE_ENGINE_CHECKS"}
    env["PYTHONPATH"] = str(REPO / "src")
    if require is not None:
        env["FH_REQUIRE_ENGINE_CHECKS"] = require
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_git_checkout_runs_marked_test(tmp_path):
    r = _run(tmp_path, git=True, require=None)
    assert r.returncode == 0, r.stdout
    assert "2 passed" in r.stdout


def test_git_checkout_with_requirement_still_runs(tmp_path):
    r = _run(tmp_path, git=True, require="1")
    assert r.returncode == 0, r.stdout
    assert "2 passed" in r.stdout


def test_staged_tree_skips_marked_test(tmp_path):
    """The deploy's staged tree has no .git and must keep skipping."""
    r = _run(tmp_path, git=False, require=None)
    assert r.returncode == 0, r.stdout
    assert "1 passed, 1 skipped" in r.stdout


def test_requirement_unset_zero_or_empty_still_skips(tmp_path):
    for value in ("", "0"):
        sub = tmp_path / ("v" + (value or "empty"))
        sub.mkdir()
        r = _run(sub, git=False, require=value)
        assert r.returncode == 0, r.stdout
        assert "1 skipped" in r.stdout


def test_requirement_without_git_errors_not_skips(tmp_path):
    r = _run(tmp_path, git=False, require="1")
    assert r.returncode != 0, r.stdout
    assert "1 passed, 1 error" in r.stdout
    assert "FH_REQUIRE_ENGINE_CHECKS" in r.stdout
    assert "skipped" not in r.stdout
