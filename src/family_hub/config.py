"""Load config.json into a Config dataclass. No secrets live here."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Config:
    port: int = 8138
    climate_base: str = ""     # optional JSON tile proxy (/api/tiles/climate)
    weather_base: str = ""     # optional JSON tile proxy (/api/tiles/weather)
    go2rtc_base: str = ""      # go2rtc restreamer; empty = no cameras
    calendar_window_days: int = 28
    calendar_past_days: int = 45   # month view browses back this far
    # calendar sources: {"id","label","color"} plus optionally
    # "kind": "google" (default, needs the OAuth token) or "ics" with a
    # "url" (https:// or webcal:// feed — iCloud shared calendars, holiday
    # feeds, school calendars).
    calendars: list[dict] = field(default_factory=list)
    # go2rtc streams shown as camera tiles, in order: [{"src","label"}, ...]
    cameras: list[dict] = field(default_factory=list)
    # Cameras-tab 2x2 grid, in DOM (row-major) order: same entry shape as
    # `cameras`. Lets the phone/tablet Cameras page show a different set/order
    # than the wall's camera column (e.g. add a camera that isn't on the wall).
    # Empty falls back to `cameras`, so a config that never sets it still works.
    camera_page: list[dict] = field(default_factory=list)
    # always-on dashboard embeds, in order. Each: {"id","label","url","vw",
    # "vh"} plus optional "page_w" (lay the page out wider than the visible
    # region), "crop_top"/"crop_left" (pan the region to a card), "full"
    # ("native" embeds the page raw full-screen; "fit" scales a fixed
    # vw x vh sheet to fill the screen), and "full_url" (override URL for
    # full-screen; defaults to "url").
    panels: list[dict] = field(default_factory=list)
    # Optional house-default display theme for a FRESH device that has no
    # per-device override yet: {"mode","accent","columns"}. None = no house
    # override (a fresh device keeps the shipped grey/green/none). The frontend
    # never persists this into localStorage — it only stamps it live — so
    # changing it here re-themes every un-overridden device on next poll.
    theme: dict | None = None


# Allowed values per theme axis; anything else is dropped (never crashes).
_THEME_AXES = {
    "mode": {"light", "soft", "dark", "grey", "black"},
    "accent": {"cyan", "violet", "amber", "green"},
    "columns": {"none", "wells", "lines"},
    # per-device prefs that also accept a house default (applied by the frontend's
    # applyHouseTheme on a device that has made no local choice)
    "layout": {"auto", "desktop"},
    "idleReturn": {"on", "off"},
}


def _clean_theme(raw_theme: object) -> dict | None:
    """Keep only the valid axes from a config `theme` block. Returns None when
    absent or when nothing valid survives, which the API reports as "no house
    override" (the frontend then falls back to grey/green/none)."""
    if not isinstance(raw_theme, dict):
        return None
    cleaned = {
        axis: raw_theme[axis]
        for axis, allowed in _THEME_AXES.items()
        if raw_theme.get(axis) in allowed
    }
    return cleaned or None


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return Config(
        port=int(raw.get("port", 8138)),
        climate_base=raw.get("climate_base", ""),
        weather_base=raw.get("weather_base", ""),
        go2rtc_base=raw.get("go2rtc_base", ""),
        calendar_window_days=int(raw.get("calendar_window_days", 28)),
        calendar_past_days=int(raw.get("calendar_past_days", 45)),
        calendars=list(raw.get("calendars", [])),
        cameras=list(raw.get("cameras", [])),
        camera_page=list(raw.get("camera_page", [])),
        panels=list(raw.get("panels", [])),
        theme=_clean_theme(raw.get("theme")),
    )
