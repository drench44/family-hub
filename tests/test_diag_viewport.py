"""/api/diag/viewport — client viewport telemetry for the iOS Chrome tab-bar
gap self-heal (#45/#53/#80).

hub.js fire-and-forgets the raw innerHeight / visualViewport / learned-max
numbers on every wake so a real stuck-short occurrence is READ off the box
instead of guessed a fifth time. The endpoint is pure diagnostics: it must
NEVER raise and never hard-validate (a diagnostic that 422s or 500s is worse
than useless), and it must not let a malformed or oversized body grow memory.
"""
import importlib
import json

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


def test_records_a_wake_report_and_reads_it_back(client):
    r = client.post("/api/diag/viewport", json={
        "reason": "reload", "inner": 700, "learnedMax": 780,
        "shortfall": 80, "orient": "portrait"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    body = client.get("/api/diag/viewport").json()
    recent = body["recent"]
    assert recent and recent[-1]["reason"] == "reload"
    assert recent[-1]["shortfall"] == 80
    assert "at" in recent[-1], "each report is timestamped so the box is readable"
    # a reload is also pinned in its own protected buffer
    assert body["reloads"] and body["reloads"][-1]["reason"] == "reload"


def test_reload_events_survive_a_flood_of_routine_reports(client):
    client.post("/api/diag/viewport", json={"reason": "reload", "inner": 700})
    # bury it under far more than the 50-slot recent buffer of routine reports
    for i in range(80):
        client.post("/api/diag/viewport", json={"reason": "hold:too-soon", "inner": i})
    body = client.get("/api/diag/viewport").json()
    assert all(e.get("reason") != "reload" for e in body["recent"]), \
        "the reload aged out of the recent buffer (expected)"
    assert any(e.get("reason") == "reload" for e in body["reloads"]), \
        "but the protected reloads buffer must still hold it"


def test_a_nan_or_infinity_value_never_breaks_the_read_side(client):
    # json.loads accepts these non-standard tokens; if stored raw the GET would
    # 500 (JSONResponse serializes with allow_nan=False).
    assert client.post("/api/diag/viewport",
                       content=b'{"reason":"reload","inner":NaN,"vv":Infinity}'
                       ).status_code == 200
    r = client.get("/api/diag/viewport")
    assert r.status_code == 200, "GET must not 500 after a NaN/Infinity report"
    r.json()  # must be valid, serializable JSON


def test_a_declared_oversized_body_is_dropped_not_buffered(client):
    big = "x" * 20000
    r = client.post("/api/diag/viewport", json={"reason": "ok", "junk": big})
    assert r.status_code == 200
    recent = client.get("/api/diag/viewport").json()["recent"]
    assert not recent, "an oversized report must be dropped, not stored"


def test_client_cannot_clobber_the_server_timestamp(client):
    client.post("/api/diag/viewport", json={"reason": "ok", "at": "1999-01-01T00:00:00"})
    at = client.get("/api/diag/viewport").json()["recent"][-1]["at"]
    assert not at.startswith("1999"), "the server owns the 'at' timestamp"


def test_garbage_body_never_500s_or_422s(client):
    # sendBeacon or a wedged client can send anything; a diagnostic must swallow
    # it, not reject it.
    for body in (b"not json at all", b"", b"[1,2,3]", b"\x00\x01\x02"):
        r = client.post("/api/diag/viewport", content=body)
        assert r.status_code == 200, f"diag rejected {body!r} with {r.status_code}"


def test_a_non_dict_json_body_is_tolerated(client):
    assert client.post("/api/diag/viewport", json=42).status_code == 200
    assert client.post("/api/diag/viewport", json="hello").status_code == 200


def test_buffer_is_bounded_so_a_flood_cannot_grow_memory(client):
    for i in range(120):
        client.post("/api/diag/viewport", json={"reason": "ok", "inner": i})
    recent = client.get("/api/diag/viewport").json()["recent"]
    assert len(recent) <= 50, "the ring buffer must cap regardless of flood"
    # newest kept, oldest dropped
    assert recent[-1]["inner"] == 119


def test_oversized_fields_are_truncated_not_stored_whole(client):
    huge = "x" * 5000
    client.post("/api/diag/viewport", json={"reason": "ok", "junk": huge})
    entry = client.get("/api/diag/viewport").json()["recent"][-1]
    assert len(entry.get("junk", "")) <= 80, "string fields must be truncated"


def test_get_is_empty_before_any_report(client):
    assert client.get("/api/diag/viewport").json() == {"recent": [], "reloads": []}
