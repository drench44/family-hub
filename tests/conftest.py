import pytest

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
