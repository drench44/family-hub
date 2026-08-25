"""Integration registry: the set of togglable data sources / tiles the hub can
run (its "extensions"), computed from config + environment. Pure, stdlib only.

An integration is AVAILABLE when it's configured (a Google calendar exists, a
weather feed is set, iCloud CalDAV credentials are present, ...). The DB
`integrations` table (db.py) holds only the operator's enable/disable overlay on
top of these; a fresh install seeds every available integration enabled, so the
wall is unchanged until someone flips a toggle. Adding an integration is adding a
descriptor here — the settings menu, seeding, and gating all read this list.
"""
from __future__ import annotations

# Kinds group integrations for the UI and for render gating. `calendar` and
# `caldav` both feed the calendar; `cameras`/`weather`/`climate`/`laundry`
# are tiles.
CALENDAR_KINDS = ("calendar", "caldav")


def laundry_configured(cfg, env: dict) -> bool:
    """True iff the laundry block is configured (config.py validated it into
    cfg.laundry) AND the Home Assistant token is present in the environment.
    Mirrors caldav_configured: without the credential the whole subsystem is
    inert and the integration simply isn't available."""
    return bool(getattr(cfg, "laundry", None) and env.get("HA_TOKEN"))


def caldav_configured(env: dict) -> bool:
    """True iff the iCloud CalDAV bot credentials are present. This is the
    feature flag: with no credential the whole CalDAV subsystem is inert and the
    integration simply isn't available."""
    return bool(env.get("ICLOUD_CALDAV_USER")
                and env.get("ICLOUD_CALDAV_APP_PASSWORD"))


def available_integrations(cfg, env: dict | None = None,
                           caldav_ok: bool | None = None) -> list[dict]:
    """The integrations available for THIS config/env, in display order. Each is
    {id, kind, name, available, group}, where `group` is "feature" for the core
    always-on features (chores, todos) and "integration" for everything else —
    the Settings UI uses it to head two separate lists.
    `available=False` entries are returned too so a
    caller can explain why something isn't shown, but the API/seeding filter to
    available ones. `caldav_ok` lets the caller inject the real credential state
    (env OR the server-side creds file — see caldav_service.configured); None
    falls back to the env-only check."""
    env = env or {}
    cals = getattr(cfg, "calendars", []) or []
    out: list[dict] = []

    def add(iid, kind, name, available, group="integration"):
        out.append({"id": iid, "kind": kind, "name": name,
                    "available": bool(available), "group": group})

    # Core features — always available; the operator turns them off to slim the
    # wall down. Listed first so they seed with the lowest sort and head the
    # Settings "Features" group.
    add("chores", "chores", "Chores", True, group="feature")
    add("todos", "todos", "To-Dos", True, group="feature")
    add("google_calendar", "calendar", "Google Calendar",
        any(c.get("kind", "google") == "google" for c in cals))
    add("ics_calendar", "calendar", "Shared / ICS calendars",
        any(c.get("kind") == "ics" for c in cals))
    add("icloud_caldav", "caldav", "iCloud (CalDAV)",
        caldav_ok if caldav_ok is not None else caldav_configured(env))
    add("cameras", "cameras", "Cameras",
        bool(getattr(cfg, "go2rtc_base", "")
             or (getattr(cfg, "cameras", []) or [])))
    add("weather", "weather", "Weather",
        bool(getattr(cfg, "weather_base", "")))
    add("climate", "climate", "Climate",
        bool(getattr(cfg, "climate_base", "")))
    add("laundry", "laundry", "Laundry", laundry_configured(cfg, env))
    add("fleet", "fleet", "Fleet status", bool(getattr(cfg, "fleet", None)))
    return out


def available_only(cfg, env: dict | None = None,
                   caldav_ok: bool | None = None) -> list[dict]:
    """available_integrations() filtered to the ones actually configured."""
    return [i for i in available_integrations(cfg, env, caldav_ok)
            if i["available"]]
