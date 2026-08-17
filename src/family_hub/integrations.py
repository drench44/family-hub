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
# `caldav` both feed the calendar; `cameras`/`weather`/`climate` are tiles.
CALENDAR_KINDS = ("calendar", "caldav")


def caldav_configured(env: dict) -> bool:
    """True iff the iCloud CalDAV bot credentials are present. This is the
    feature flag: with no credential the whole CalDAV subsystem is inert and the
    integration simply isn't available."""
    return bool(env.get("ICLOUD_CALDAV_USER")
                and env.get("ICLOUD_CALDAV_APP_PASSWORD"))


def available_integrations(cfg, env: dict | None = None) -> list[dict]:
    """The integrations available for THIS config/env, in display order. Each is
    {id, kind, name, available}. `available=False` entries are returned too so a
    caller can explain why something isn't shown, but the API/seeding filter to
    available ones."""
    env = env or {}
    cals = getattr(cfg, "calendars", []) or []
    out: list[dict] = []

    def add(iid, kind, name, available):
        out.append({"id": iid, "kind": kind, "name": name,
                    "available": bool(available)})

    add("google_calendar", "calendar", "Google Calendar",
        any(c.get("kind", "google") == "google" for c in cals))
    add("ics_calendar", "calendar", "Shared / ICS calendars",
        any(c.get("kind") == "ics" for c in cals))
    add("icloud_caldav", "caldav", "iCloud (CalDAV)",
        caldav_configured(env))
    add("cameras", "cameras", "Cameras",
        bool(getattr(cfg, "go2rtc_base", "")
             or (getattr(cfg, "cameras", []) or [])))
    add("weather", "weather", "Weather",
        bool(getattr(cfg, "weather_base", "")))
    add("climate", "climate", "Climate",
        bool(getattr(cfg, "climate_base", "")))
    return out


def available_only(cfg, env: dict | None = None) -> list[dict]:
    """available_integrations() filtered to the ones actually configured."""
    return [i for i in available_integrations(cfg, env) if i["available"]]


def calendar_kind_enabled(enabled_ids: set, cal_kind: str) -> bool:
    """Whether events from a calendar of `cal_kind` ('google' | 'ics') should
    render, given the set of enabled integration ids. Used to hide a disabled
    calendar source's events without touching the sync cache."""
    integ = "google_calendar" if cal_kind == "google" else "ics_calendar"
    return integ in enabled_ids
