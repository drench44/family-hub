import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config, items):
    """Tests marked engine_checkout read the engine repo's own files (its
    README, .gitignore, tracked placeholders). The deploy runs the suite on a
    staged tree with no .git and the house overlay's README, where those
    checks mean nothing, so they skip there and run everywhere else."""
    if (ROOT / ".git").exists():
        return
    skip = pytest.mark.skip(reason="not a git checkout of the engine (a staged deploy tree)")
    for item in items:
        if "engine_checkout" in item.keywords:
            item.add_marker(skip)

from family_hub import db as fdb


@pytest.fixture(autouse=True)
def _pin_key_in_tmp(tmp_path, monkeypatch):
    """The parent PIN's secret key file lives beside the database; no test may
    read or write the real data/pin.key."""
    from family_hub import parent_pin
    monkeypatch.setattr(parent_pin, "KEY_PATH", str(tmp_path / "pin.key"))
    parent_pin._warned.clear()          # each test sees its own once-only log lines


@pytest.fixture
def conn(tmp_path):
    c = fdb.connect(str(tmp_path / "hub.db"))
    fdb.ensure_schema(c)
    yield c
    c.close()
