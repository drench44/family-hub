import pytest

from family_hub import db as fdb


@pytest.fixture
def conn(tmp_path):
    c = fdb.connect(str(tmp_path / "hub.db"))
    fdb.ensure_schema(c)
    yield c
    c.close()
