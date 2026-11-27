"""When the wall dims: the household dim hours, and "nobody is home" read
from Home Assistant.

Dim hours are one household setting (kv `dim_hours`), shared by every wall and
phone: {"on": bool, "start": "HH:MM", "end": "HH:MM"}. The window may cross
midnight (22:00 to 06:00) or not (13:00 to 15:00); the end is exclusive. The
frontend (common.js inDimWindow) decides "is it dim now" from its own clock
every second; in_window here is the same rule, for tests and docs.

Away dimming is optional and off by default. The server asks Home Assistant
who is home (zone.home's state is the number of people in the home zone, or a
configured list of person.* entities) on a slow background loop and caches
the answer; /api/hub carries it as the `away` block. It fails soft: an answer
that is missing, stale, or unreadable never dims the wall.

Pure except read_presence, which takes the caller's httpx client.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re

log = logging.getLogger("family_hub.dimming")

KV_KEY = "dim_hours"
DIM_DEFAULT = {"on": True, "start": "22:00", "end": "06:00"}
DEFAULT_ENTITY = "zone.home"

# How often the server asks Home Assistant who is home, and how old an answer
# may get before it counts as unreadable (three missed reads: the watcher has
# stopped or HA has gone quiet, so stop trusting "nobody is home").
WATCH_S = 60.0
FRESH_S = 3 * WATCH_S + 30
TIMEOUT = 3.0

_HM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hm(value) -> int | None:
    """'HH:MM' (24-hour, two digits each) as minutes after midnight, else None."""
    if not isinstance(value, str):
        return None
    m = _HM.match(value)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def clean_hours(stored) -> dict:
    """The stored dim-hours record, made whole. A missing record is the
    default; a damaged field falls back to its default with a warning, so a
    bad write can never stop the wall from rendering."""
    out = dict(DIM_DEFAULT)
    if stored is None:
        return out
    if not isinstance(stored, dict):
        log.warning("dim hours: stored value %r is not an object; using the "
                    "default 22:00 to 06:00", stored)
        return out
    if isinstance(stored.get("on"), bool):
        out["on"] = stored["on"]
    elif "on" in stored:
        log.warning("dim hours: ignoring on=%r (needs true or false)", stored["on"])
    for key in ("start", "end"):
        if parse_hm(stored.get(key)) is not None:
            out[key] = stored[key]
        elif key in stored:
            log.warning("dim hours: ignoring %s=%r (needs HH:MM)", key, stored[key])
    if out["start"] == out["end"]:
        log.warning("dim hours: start and end are both %s; using the default "
                    "times", out["start"])
        out["start"], out["end"] = DIM_DEFAULT["start"], DIM_DEFAULT["end"]
    return out


def merge_hours(current: dict, patch: dict) -> dict:
    """Apply a Settings change to the current record. Raises ValueError, in
    plain words, for anything the wall should refuse."""
    if not isinstance(patch, dict) or not any(k in patch for k in ("on", "start", "end")):
        raise ValueError("Nothing to change.")
    out = dict(clean_hours(current))
    if "on" in patch:
        if not isinstance(patch["on"], bool):
            raise ValueError("Dim hours can only be turned on or off.")
        out["on"] = patch["on"]
    for key, word in (("start", "start"), ("end", "end")):
        if key in patch:
            if parse_hm(patch[key]) is None:
                raise ValueError(f"The {word} time must look like 22:00.")
            out[key] = patch[key]
    if out["start"] == out["end"]:
        raise ValueError("The start and end times can't be the same.")
    return out


def in_window(minutes: int, hours: dict) -> bool:
    """Is `minutes` (after midnight) inside the dim window? End exclusive."""
    if not hours.get("on"):
        return False
    s, e = parse_hm(hours.get("start")), parse_hm(hours.get("end"))
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= minutes < e
    return minutes >= s or minutes < e     # crosses midnight


# --- who is home -------------------------------------------------------------

def zone_count(body) -> int | None:
    """zone.home's state: how many people are home. None when it isn't a
    whole number of zero or more (unknown, unavailable, garbage)."""
    if not isinstance(body, dict) or not isinstance(body.get("state"), str):
        return None
    try:
        n = float(body["state"])
    except ValueError:
        return None
    if not math.isfinite(n) or n < 0 or n != int(n):
        return None
    return int(n)


def person_home(body) -> bool | None:
    """A person.* entity: True at home, False anywhere else (not_home or
    another zone), None when HA doesn't know."""
    if not isinstance(body, dict) or not isinstance(body.get("state"), str):
        return None
    state = body["state"].strip().lower()
    if state in ("", "unknown", "unavailable"):
        return None
    return state == "home"


async def _state(client, base: str, token: str, entity: str):
    """(body or None, why it failed or None, was it a refused token)."""
    try:
        r = await client.get(f"{base}/api/states/{entity}", timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (401, 403):
            return None, f"Home Assistant refused the token (HTTP {r.status_code})", True
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            return None, f"{entity}: not an object", False
        return body, None, False
    except Exception as e:
        return None, f"{entity}: {type(e).__name__}: {e}"[:200], False


async def read_presence(client, base: str, token: str, people: list[str]) -> dict:
    """Ask Home Assistant how many people are home. Never raises.

    {"ok": True, "home": n} or {"ok": False, "error": str, "auth": bool}.
    With a people list, one person at home is enough to say someone is home,
    but "nobody is home" needs every person read as away."""
    if not people:
        body, err, auth = await _state(client, base, token, DEFAULT_ENTITY)
        if err:
            return {"ok": False, "error": err, "auth": auth}
        n = zone_count(body)
        if n is None:
            return {"ok": False, "error": f"{DEFAULT_ENTITY} reads {body.get('state')!r}",
                    "auth": False}
        return {"ok": True, "home": n}
    got = await asyncio.gather(*(_state(client, base, token, p) for p in people))
    home, unknown, auth, errors = 0, [], False, []
    for ent, (body, err, refused) in zip(people, got):
        auth = auth or refused
        at_home = None if err else person_home(body)
        if at_home is None:
            unknown.append(ent)
            errors.append(err or f"{ent} reads {body.get('state')!r}")
        elif at_home:
            home += 1
    if home:
        return {"ok": True, "home": home}
    if unknown:
        return {"ok": False, "error": "; ".join(errors), "auth": auth}
    return {"ok": True, "home": 0}


def away_block(*, enabled: bool, token: bool, snap: dict | None,
               at: float | None, now: float) -> dict:
    """The /api/hub `away` block: {enabled, state, home, dim}.

    state is off | needs_auth | checking | unreadable | home | away, and dim
    is True only for a fresh, readable answer of nobody home."""
    if not enabled:
        return {"enabled": False, "state": "off", "home": None, "dim": False}
    if not token:
        return {"enabled": True, "state": "needs_auth", "home": None, "dim": False}
    if snap is None or at is None:
        return {"enabled": True, "state": "checking", "home": None, "dim": False}
    if not snap.get("ok") or now - at > FRESH_S:
        return {"enabled": True, "state": "unreadable", "home": None, "dim": False}
    n = snap.get("home")
    if not isinstance(n, int) or n < 0:
        return {"enabled": True, "state": "unreadable", "home": None, "dim": False}
    return {"enabled": True, "state": "away" if n == 0 else "home",
            "home": n, "dim": n == 0}


def demo_away(*, enabled: bool) -> dict:
    """DEMO's canned answer: two people home, so the demo wall stays bright."""
    if not enabled:
        return {"enabled": False, "state": "off", "home": None, "dim": False}
    return {"enabled": True, "state": "home", "home": 2, "dim": False}
