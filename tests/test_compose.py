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
