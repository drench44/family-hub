"""Keep the uvicorn access log for requests worth reading.

Every open wall and phone probes each camera every 30 seconds (and every 0.7
seconds while a full-screen camera warms up), refreshes its tiles and the hub
payload every minute, and the container healthcheck hits /health every 30
seconds. Those successful polls became the biggest source of container log
lines and buried everything else.

QuietPollFilter drops only a SUCCESSFUL (2xx or 304) GET or HEAD to one of
those poll paths. An error on the same paths (a dead camera's 502, a broken
database's 503), every write, and every other request are still logged. A
record the filter cannot read is kept as-is.
"""
from __future__ import annotations

import logging

# Whole paths polled on a timer by every open screen or by the healthcheck.
QUIET_PATHS = frozenset({
    "/health",               # container healthcheck + the fleet watchdog
    "/api/hub",              # the wall's main poll, every 60s per screen
    "/api/laundry/stream",   # SSE reconnect check, every 60s per screen
})
# Path prefixes for the tile polls: weather, climate, laundry, fleet every 60s,
# and camera.jpg, the camera liveness and HD-readiness probe.
QUIET_PREFIXES = ("/api/tiles/",)

_QUIET_METHODS = frozenset({"GET", "HEAD"})


def _is_success(status: int) -> bool:
    return 200 <= status < 300 or status == 304


class QuietPollFilter(logging.Filter):
    """Drop successful poll lines from uvicorn.access. uvicorn logs each request
    as '%s - "%s %s HTTP/%s" %d' with args (client, method, path with query,
    http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, full_path, _http, status = args
        if not isinstance(status, int) or not isinstance(full_path, str):
            return True
        if method not in _QUIET_METHODS or not _is_success(status):
            return True
        path = full_path.split("?", 1)[0]
        return not (path in QUIET_PATHS or path.startswith(QUIET_PREFIXES))


def install() -> None:
    """Attach the filter to uvicorn's access logger, once. Filters attached to a
    logger survive uvicorn's own logging dictConfig, so the order of this call
    and uvicorn's startup does not matter."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietPollFilter) for f in logger.filters):
        logger.addFilter(QuietPollFilter())
