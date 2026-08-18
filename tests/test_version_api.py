"""/api/version — a debug/ops readout of what's actually deployed.

Returns the running SemVer plus the build hash (the asset-content token
/api/hub also carries). No changelog — that lives on GitHub. Must never 500.
"""
import importlib
import json
import re

import pytest
from fastapi.testclient import TestClient


def _write_cfg(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 8138, "calendars": [], "cameras": []}))
    return str(p)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", _write_cfg(tmp_path))
    import family_hub.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        yield c


def test_version_endpoint_reports_version_and_build(client):
    import family_hub
    body = client.get("/api/version").json()
    assert body["version"] == family_hub.__version__
    assert re.fullmatch(r"[0-9a-f]{12}", body["build"]), \
        f"build must be a 12-char hex token, got {body.get('build')!r}"


def test_version_endpoint_carries_no_changelog(client):
    """The changelog lives on GitHub, not on the wall — the endpoint is a bare
    version readout, not a release-notes feed."""
    body = client.get("/api/version").json()
    assert "entries" not in body


def test_version_endpoint_never_500s(client):
    assert client.get("/api/version").status_code == 200
