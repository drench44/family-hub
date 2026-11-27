"""Guards for the shipped docker-compose.yml: resource and log limits, health
checks, and which ports reach the LAN.

All learned on a real box. go2rtc was OOM-killed five times in ten days at a
128 MB limit, then again at 256 MB (2026-09-22) after swapping its whole
second 256 MB first; with no log caps a container's json log grows without
bound (the hub alone wrote ~19 MB a day); and go2rtc's API, published on the
LAN, handed out every camera URL and a writable config. pyyaml is always
installed here (the caldav package requires it), so these never skip.
"""
import re

import pytest
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def _services():
    return _compose()["services"]


def _mb(size: str) -> int:
    m = re.fullmatch(r"(\d+)([mg])", str(size))
    assert m, f"unexpected size {size!r}"
    return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)


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


def test_every_service_has_a_memory_limit_and_no_swap():
    """memswap_limit is RAM + swap; equal to mem_limit means no swap. Docker's
    default is twice the limit, and go2rtc spent its last minutes before the
    2026-09-22 kill swapping (and the camera tiles hung) instead of dying and
    restarting in seconds."""
    for name, svc in _services().items():
        assert svc.get("mem_limit"), f"{name} has no memory limit"
        assert svc.get("memswap_limit") == svc["mem_limit"], name


def test_go2rtc_has_room_above_the_peak_that_killed_it():
    """Killed at 256 MB RAM + 256 MB swap with both full: ~512 MB at a peak."""
    go2rtc = _services()["go2rtc"]
    assert _mb(go2rtc["mem_limit"]) > 512
    env = dict(e.split("=", 1) for e in go2rtc["environment"])
    # the Go heap soft limit leaves room under the container limit for ffmpeg
    heap = re.fullmatch(r"(\d+)MiB", env["GOMEMLIMIT"])
    assert heap and int(heap.group(1)) <= _mb(go2rtc["mem_limit"]) - 128


def test_go2rtc_image_is_pinned_to_an_exact_release():
    image = _services()["go2rtc"]["image"]
    assert re.fullmatch(r"alexxit/go2rtc:\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?", image), image


def _secs(v):
    m = re.fullmatch(r"(\d+)s", str(v))
    assert m, v
    return int(m.group(1))


def test_camera_services_have_healthchecks_using_binaries_their_images_ship():
    """go2rtc's image has curl; the wyze-bridge fork's is busybox (wget, no
    curl). Checked in the real images, 2026-09-23."""
    svcs = _services()
    g = svcs["go2rtc"]["healthcheck"]["test"]
    assert g[:2] == ["CMD", "curl"] and "http://127.0.0.1:1984/api" in g
    w = svcs["wyze-bridge"]["healthcheck"]["test"]
    assert w[0] == "CMD-SHELL"
    # Both the WebUI and the bridge's internal go2rtc: the internal one can
    # die alone (2026-09-23) and the WebUI keeps answering.
    assert "wget -q -T 4 -O /dev/null http://127.0.0.1:5080/api/health" in w[1]
    assert "&& wget -q -T 4 -O /dev/null http://127.0.0.1:1984/api && exit 0" in w[1]
    # Self-heal only after retries, and the check's own timeout covers them.
    assert w[1].startswith("for i in 1 2 3; do ") and "sleep 5" in w[1]
    assert w[1].endswith("kill -TERM 1; exit 1")
    assert _secs(svcs["wyze-bridge"]["healthcheck"]["timeout"]) >= 3 * (4 + 4 + 5)
    for name in ("go2rtc", "wyze-bridge"):
        hc = svcs[name]["healthcheck"]
        assert hc.get("interval") and hc.get("timeout") and hc.get("retries"), name


_PORT = re.compile(r"(\$\{[^}]*\}|[0-9.]+):(\d+):(\d+)(/(tcp|udp))?")


def _published(svc):
    """(host_ip, host_port, container_port) for each short-syntax port. Every
    publish must name its address: a bare "1984:1984" is every interface."""
    out = []
    for spec in svc.get("ports") or []:
        m = _PORT.fullmatch(str(spec))
        assert m, f"publish on an explicit address: {spec!r}"
        out.append((m.group(1), m.group(2), m.group(3)))
    return out


def test_no_auth_less_admin_port_is_on_the_lan():
    """go2rtc's API (1984) and the wyze-bridge WebUI have no login and show
    every camera; they are published on the box's loopback only. The browser
    reaches the camera player through the hub's /go2rtc/ proxy."""
    svcs = _services()
    assert ("127.0.0.1", "1984", "1984") in _published(svcs["go2rtc"])
    assert _published(svcs["wyze-bridge"]) == [("127.0.0.1", "5050", "5080")]
    for name, svc in svcs.items():
        for host_ip, _host_port, cport in _published(svc):
            if cport in ("1984", "5080", "5000"):
                assert host_ip == "127.0.0.1", f"{name} publishes {cport} on {host_ip}"
            else:
                # the rest is pinned to the LAN interface in .env (HUB_BIND)
                assert host_ip.startswith("${HUB_BIND"), f"{name}: {host_ip}"


def test_go2rtc_webrtc_port_stays_on_the_lan():
    """The browsers' WebRTC media comes straight from go2rtc (it carries no API)."""
    specs = _services()["go2rtc"]["ports"]
    assert "${HUB_BIND:-0.0.0.0}:8555:8555/tcp" in specs
    assert "${HUB_BIND:-0.0.0.0}:8555:8555/udp" in specs


def test_go2rtc_config_is_read_only_and_config_dirs_are_named_volumes():
    """Read-only: no API caller can save a stream (an exec: source runs a
    command). Named /config (and the bridge's /media) volumes: the images
    declare them, and an anonymous one is left behind on every `down`."""
    svcs = _services()
    assert "./data/go2rtc.yaml:/config/go2rtc.yaml:ro" in svcs["go2rtc"]["volumes"]
    assert "go2rtc-config:/config" in svcs["go2rtc"]["volumes"]
    assert "wyze-bridge-config:/config" in svcs["wyze-bridge"]["volumes"]
    assert "wyze-bridge-media:/media" in svcs["wyze-bridge"]["volumes"]
    assert set(_compose()["volumes"]) == {
        "go2rtc-config", "wyze-bridge-config", "wyze-bridge-media"}


def test_hub_reaches_go2rtc_over_the_compose_network():
    env = _services()["web"]["environment"]
    assert "GO2RTC_FETCH_BASE=http://go2rtc:1984" in env


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


def test_web_mounts_the_backup_folder_and_only_env_read_only():
    """The hub's own backup job writes to /backups, mounted from BACKUP_DIR
    (default ./backups; a NAS is any folder the host has mounted). The
    settings bundle reads .env, mounted on its own, read-only. Not the whole
    project folder: that would hand the hub .git and a second view of data/."""
    web = _services()["web"]
    vols = web["volumes"]
    assert "${BACKUP_DIR:-./backups}:/backups" in vols
    assert not any(isinstance(v, str) and v.split(":", 1)[0] in (".", "./") for v in vols)
    env_mounts = [v for v in vols if isinstance(v, dict) and v.get("source") == "./.env"]
    assert env_mounts == [{"type": "bind", "source": "./.env", "target": "/app/.env",
                           "read_only": True, "bind": {"create_host_path": False}}]
    env = web["environment"]
    assert "SETTINGS_ENV_PATH=/app/.env" in env
    # only a label for the Settings card: the host path, which the container can't see
    assert "BACKUP_DIR=${BACKUP_DIR:-./backups}" in env


def test_no_bind_mount_can_turn_a_missing_file_into_a_root_owned_folder():
    """Docker makes a missing short-form bind source as an empty root-owned
    FOLDER. A `./.env:/app/.env` mount on a clone with no .env (checked
    2026-11-27, compose v5) left a root-owned `.env/` directory behind, and
    the owner could no longer create the file there. The long form with
    create_host_path false refuses to start instead, loudly ("bind source
    path does not exist"), and the quick start creates .env first."""
    for name, svc in _services().items():
        for v in svc.get("volumes", []):
            if isinstance(v, str):
                assert v.split(":", 1)[0] not in ("./.env", ".env"), (name, v)
            else:                      # every long-form bind refuses to invent its source
                assert v.get("bind", {}).get("create_host_path") is False, (name, v)


@pytest.mark.engine_checkout
def test_the_quick_start_creates_env_before_compose_up():
    readme = (COMPOSE.parent / "README.md").read_text()
    quick = readme[readme.index("## Quick start"):]
    assert quick.index("cp .env.example .env") < quick.index("docker compose up")


@pytest.mark.engine_checkout
def test_the_default_backup_folder_ships_in_the_repo():
    """Docker would make a missing ./backups owned by root, which the hub's
    non-root user can't write. A tracked placeholder means a clone already
    has the folder, owned by whoever cloned it."""
    root = COMPOSE.parent
    assert (root / "backups" / ".gitkeep").is_file()
    ignore = (root / ".gitignore").read_text().splitlines()
    assert "backups/*" in ignore and "!backups/.gitkeep" in ignore
