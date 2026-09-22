"""The uvicorn access log drops the routine successful polls (camera probes,
tile refreshes, the laundry stream, the health check) and keeps everything
else: errors on those paths, every write, and every other request."""
import asyncio
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from family_hub import access_log

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _record(method, path, status, client="127.0.0.1:5000"):
    """A record shaped exactly like uvicorn's own access-log call:
    '%s - "%s %s HTTP/%s" %d' with (client, method, path+query, http, status)."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', (client, method, path, "1.1", status), None)


def _kept(method, path, status):
    return access_log.QuietPollFilter().filter(_record(method, path, status))


@pytest.mark.parametrize("path", [
    "/api/tiles/camera.jpg?src=cam&probe=1758500000000",
    "/api/tiles/laundry",
    "/api/tiles/weather",
    "/api/tiles/climate",
    "/api/tiles/fleet",
    "/api/laundry/stream",
    "/api/hub",
    "/health",
])
@pytest.mark.parametrize("status", [200, 204, 304])
def test_successful_polls_are_dropped(path, status):
    assert _kept("GET", path, status) is False
    assert _kept("HEAD", path, status) is False


@pytest.mark.parametrize("status", [301, 400, 404, 429, 500, 502, 503])
def test_poll_errors_are_still_logged(status):
    # a dead camera (502), an unknown one (404), a broken db (503)
    assert _kept("GET", "/api/tiles/camera.jpg?src=cam&probe=1", status) is True
    assert _kept("GET", "/health", status) is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_are_always_logged(method):
    assert _kept(method, "/api/hub", 200) is True
    assert _kept(method, "/api/tiles/laundry", 200) is True


@pytest.mark.parametrize("path", [
    "/",
    "/hub.js?v=1.7.0",
    "/api/calendar",
    "/api/todos",
    "/api/chores/day?date=2026-09-22",
    "/api/admin/state",
    "/api/version",
    "/api/hubx",              # not a prefix match on a whole-path entry
    "/healthz",
    "/api/tilesx/laundry",
])
def test_other_requests_are_logged(path):
    assert _kept("GET", path, 200) is True


def test_unexpected_record_shapes_are_kept():
    """A record the filter cannot read (another format, a string status) is
    logged as-is rather than dropped or raising inside logging."""
    f = access_log.QuietPollFilter()
    odd = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            "plain message", None, None)
    assert f.filter(odd) is True
    short = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                              "%s %s", ("GET", "/health"), None)
    assert f.filter(short) is True
    assert f.filter(_record("GET", "/health", "not-a-status")) is True


def test_install_is_idempotent_and_survives_uvicorns_log_config():
    """uvicorn applies its own dictConfig to uvicorn.access. The filter must
    still be attached after that, and installing twice must not stack it."""
    # In a child process: dictConfig flushes and closes every handler in the
    # process, which would reach pytest's own logging if run in here.
    script = (
        "import logging, logging.config, uvicorn.config\n"
        "from family_hub import access_log\n"
        "lg = logging.getLogger('uvicorn.access')\n"
        "access_log.install(); access_log.install()\n"
        "ours = lambda: sum(isinstance(f, access_log.QuietPollFilter)"
        " for f in lg.filters)\n"
        "assert ours() == 1, 'installed twice'\n"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)\n"
        "assert ours() == 1, 'lost to dictConfig'\n"
        "print('ok')\n")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(filter(None, [SRC, os.environ.get("PYTHONPATH")]))}
    out = subprocess.run([sys.executable, "-c", script], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_real_uvicorn_access_lines_are_filtered():
    """End to end through a real uvicorn server, so the filter is tested
    against the records uvicorn actually emits, not only a hand-made copy."""
    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        status = 502 if scope["path"] == "/api/tiles/camera.jpg" and \
            b"src=dead" in scope["query_string"] else 200
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    logger = logging.getLogger("uvicorn.access")
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    grab = Grab()
    before_filters, before_level = list(logger.filters), logger.level
    logger.addHandler(grab)
    logger.setLevel(logging.INFO)
    access_log.install()

    async def run():
        server = uvicorn.Server(uvicorn.Config(
            asgi, log_config=None, lifespan="off", access_log=True))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(1000):                  # up to ~10s, never forever
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn did not start"
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as c:
            await c.get(base + "/api/tiles/camera.jpg?src=cam&probe=1")
            await c.get(base + "/health")
            await c.get(base + "/api/tiles/camera.jpg?src=dead&probe=2")
            await c.post(base + "/api/hub")
            await c.get(base + "/api/calendar")
        server.should_exit = True
        await task

    try:
        asyncio.run(run())
    finally:
        logger.removeHandler(grab)
        logger.filters[:] = before_filters
        logger.setLevel(before_level)
        sock.close()
    assert len(lines) == 3, lines
    assert "src=dead" in lines[0] and "502" in lines[0]
    assert '"POST /api/hub' in lines[1]
    assert '"GET /api/calendar' in lines[2]
