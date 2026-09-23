"""Guards for the shipped docker-compose.yml's resource and log limits.

Both were learned on a real box: go2rtc was OOM-killed five times in nine days
at a 128 MB limit, and with no log caps a container's json log grows without
bound (the hub alone wrote ~19 MB a day). pyyaml is always installed here (the
caldav package requires it), so these never skip.
"""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_every_service_caps_its_logs():
    services = _services()
    assert services, "no services parsed"
    for name, svc in services.items():
        logging_cfg = svc.get("logging")
        assert logging_cfg, f"{name} has no log cap"
        assert logging_cfg.get("driver") == "json-file", name
        opts = logging_cfg.get("options") or {}
        assert opts.get("max-size") == "10m", name
        assert str(opts.get("max-file")) == "5", name


def test_go2rtc_has_room_above_its_old_oom_limit():
    assert _services()["go2rtc"]["mem_limit"] == "256m"


def test_web_service_is_hardened():
    """The hub needs no Linux capabilities and never escalates: it runs as
    a fixed non-root uid, serves an unprivileged port and writes only /data.
    Dropping every capability and forbidding new privileges costs nothing
    and turns a code-execution bug into much less. go2rtc and wyze-bridge
    are third-party images whose needs are not pinned down, so only the
    web service carries these."""
    web = _services()["web"]
    assert web["user"] == "${HUB_UID:-1000}:${HUB_GID:-1000}"
    assert "no-new-privileges:true" in web.get("security_opt", [])
    assert web.get("cap_drop") == ["ALL"]
    assert "cap_add" not in web
    assert not web.get("privileged")
