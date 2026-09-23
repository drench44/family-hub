"""Guards for how the image is built and how CI installs and runs things.

The web image runs as a non-root user with pinned dependencies, the build
context carries only what the Dockerfile copies (never data/, .env or the
real config.json), and CI pins its third-party actions and reads the repo
with the least permission it needs. Each of these is easy to undo by
accident in a one-line edit, so each has a test.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "web.Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
CI = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / "requirements.lock"


def _dockerfile_lines():
    return [ln.strip() for ln in DOCKERFILE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _copy_sources():
    """Every source path a COPY in web.Dockerfile reads from the context."""
    out = []
    for ln in _dockerfile_lines():
        if ln.startswith("COPY "):
            parts = [p for p in ln.split()[1:] if not p.startswith("--")]
            out += [p.rstrip("/") for p in parts[:-1]]
    return out


# --- requirements.lock -----------------------------------------------------

def test_lock_pins_every_line_exactly():
    pins = [ln for ln in LOCK.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert pins, "requirements.lock is empty"
    for ln in pins:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.+\-]+", ln), ln


def test_lock_covers_every_top_level_requirement():
    def norm(name):
        return re.sub(r"[-_.]+", "-", name).lower()
    pinned = {norm(ln.split("==")[0]) for ln in LOCK.read_text().splitlines()
              if "==" in ln and not ln.startswith("#")}
    wanted = [re.split(r"[<>=!~\[; ]", ln.strip())[0]
              for ln in (ROOT / "requirements.txt").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
    missing = [w for w in wanted if norm(w) not in pinned]
    assert missing == [], f"not pinned in requirements.lock: {missing}"


# --- web.Dockerfile ---------------------------------------------------------

def test_image_installs_with_the_lock_as_constraints():
    lines = _dockerfile_lines()
    assert any(ln.startswith("COPY ") and "requirements.lock" in ln
               for ln in lines)
    installs = [ln for ln in lines if "pip install" in ln]
    assert installs and all("-c requirements.lock" in ln for ln in installs)


def test_image_runs_as_the_uid_compose_uses():
    """compose sets `user:` too, but a plain `docker run` of the image must
    not run as root either. Same default uid:gid as compose, so /data stays
    writable either way."""
    users = [ln.split(None, 1)[1] for ln in _dockerfile_lines()
             if ln.startswith("USER ")]
    assert users, "web.Dockerfile has no USER"
    assert users[-1] not in ("root", "0", "0:0")
    compose_user = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text())["services"]["web"]["user"]
    m = re.fullmatch(r"\$\{HUB_UID:-(\d+)\}:\$\{HUB_GID:-(\d+)\}", compose_user)
    assert m, compose_user
    assert users[-1] == f"{m.group(1)}:{m.group(2)}"


def test_healthcheck_gives_the_app_a_start_period():
    checks = [ln for ln in _dockerfile_lines() if ln.startswith("HEALTHCHECK")]
    assert len(checks) == 1 and "--start-period=" in checks[0]


# --- .dockerignore ----------------------------------------------------------

def test_dockerignore_is_an_allowlist_of_what_the_dockerfile_copies():
    """The build context used to be the whole checkout: data/ (the family's
    database and tokens), .env and the real config.json were all sent to
    the builder. Ignore everything, then allow back exactly what COPY uses."""
    rules = [ln.strip() for ln in DOCKERIGNORE.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert rules[0] == "*", "the .dockerignore must start by ignoring everything"
    allowed = {r[1:].rstrip("/") for r in rules if r.startswith("!")}
    assert allowed == set(_copy_sources())
    for secret in ("data", ".env", "config.json", ".git"):
        assert secret not in allowed, secret


# --- ci.yml -----------------------------------------------------------------

def _ci():
    return yaml.safe_load(CI.read_text())


def _steps():
    for job in _ci()["jobs"].values():
        yield from job.get("steps", [])


def test_ci_reads_the_repo_with_least_permission():
    assert _ci()["permissions"] == {"contents": "read"}


def test_ci_pins_every_action_to_a_commit():
    uses = [s["uses"] for s in _steps() if "uses" in s]
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.\-]+/[\w.\-]+@[0-9a-f]{40}", u), u
    # each pin names its version, so a bump is readable in review
    for ln in CI.read_text().splitlines():
        if "uses:" in ln:
            assert re.search(r"@[0-9a-f]{40} # v\d", ln), ln


def test_ci_installs_with_the_lock_as_constraints():
    runs = [s["run"] for s in _steps() if "pip install" in s.get("run", "")]
    assert runs and all("-c requirements.lock" in r for r in runs)


def test_ci_runs_the_js_suite_once():
    """tests/test_js.py runs node --test inside pytest (and fails, never
    skips, in CI when no runner exists), so a separate node step ran the
    same suite a second time."""
    runs = [s.get("run", "") for s in _steps()]
    assert not any("node --test" in r for r in runs)
    test_js = (ROOT / "tests" / "test_js.py").read_text()
    assert 'os.environ.get("CI")' in test_js and "pytest.fail(" in test_js
