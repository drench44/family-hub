"""iCloud CalDAV access, wrapped so the `caldav` library is swappable and the
rest of the app never imports it directly (global rule: a clean service layer
around an external API).

Credentials are the feature flag: with no ICLOUD_CALDAV_USER /
ICLOUD_CALDAV_APP_PASSWORD, `configured()` is False and the whole subsystem is
inert. The `caldav` import is lazy so this module imports without the library
installed, and the sync logic (caldav_sync.py) takes an injected client with
this shape, so it's fully testable against a fake — no live server needed:

    client.configured() -> bool
    client.discover()   -> [{"id","name","comp"('VEVENT'|'VTODO'),"color"}, ...]
    client.fetch_ics(collection, lo: date, hi: date) -> [ics_str, ...]

The collection dict a real client returns also carries a private "_cal" handle
it uses in fetch_ics; the fake omits it. `id` is a stable slug of the collection
URL, prefixed 'caldav:' by the sync so events route to the CalDAV source scope.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

log = logging.getLogger("family_hub.caldav")

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


def _creds_path(env):
    """Where UI-entered credentials are stored server-side — a file in the data
    dir (like the Google token), never in git, never in an env the operator has
    to hand out. Set by app.py from DB_PATH's dir."""
    return env.get("CALDAV_CREDS_PATH")


def caldav_credentials(env=None):
    """(user, app_password), from env first (advanced/backward-compat) then the
    server-side creds file (the settings UI writes it). Neither -> (None, None)."""
    env = env if env is not None else os.environ
    user, pw = (env.get("ICLOUD_CALDAV_USER"),
                env.get("ICLOUD_CALDAV_APP_PASSWORD"))
    if user and pw:
        return user, pw
    path = _creds_path(env)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("user"), d.get("app_password")
        except Exception:
            log.warning("caldav creds file unreadable: %s", path, exc_info=True)
    return None, None


def store_credentials(user: str, app_password: str, env=None) -> None:
    """Persist UI-entered credentials to the server-side file, mode 0600. The
    plaintext never leaves the box; no API ever returns it."""
    env = env if env is not None else os.environ
    path = _creds_path(env)
    if not path:
        raise RuntimeError("CALDAV_CREDS_PATH not set")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"user": user, "app_password": app_password}, f)


def clear_credentials(env=None) -> None:
    env = env if env is not None else os.environ
    path = _creds_path(env)
    if path and os.path.exists(path):
        os.remove(path)


def configured(env=None) -> bool:
    """Feature flag: both the bot Apple ID and its app-specific password set
    (via env OR the server-side creds file)."""
    user, pw = caldav_credentials(env)
    return bool(user and pw)


def _slug(url: str) -> str:
    """A short stable id for a collection URL (iCloud partition hosts vary; the
    path is stable). Hash keeps it filesystem/SQL-safe and length-bounded."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def client_from_env(env=None):
    """A CalDavClient from the environment, or None when not configured."""
    user, pw = caldav_credentials(env)
    if not (user and pw):
        return None
    url = (env or os.environ).get("ICLOUD_CALDAV_URL", ICLOUD_CALDAV_URL)
    return CalDavClient(user, pw, url)


class CalDavClient:
    """Thin wrapper over the `caldav` library. Discovers the account's shared
    collections and yields each object's raw ICS, which caldav_sync parses with
    the same recurrence-expanding path as public ICS feeds. All `caldav` calls
    are lazily imported and wrapped so a transient failure never escapes as
    anything but an exception the caller already handles."""

    def __init__(self, username: str, password: str, url: str = ICLOUD_CALDAV_URL):
        self.username = username
        self.password = password
        self.url = url
        self._principal = None

    def configured(self) -> bool:
        return bool(self.username and self.password)

    def _principal_obj(self):
        import caldav
        if self._principal is None:
            dav = caldav.DAVClient(url=self.url, username=self.username,
                                   password=self.password)
            self._principal = dav.principal()
        return self._principal

    def _color(self, cal) -> str | None:
        # `from caldav.elements import ical` — NOT `import caldav` +
        # caldav.elements.ical, which raises AttributeError (the submodule isn't
        # auto-imported) and silently yielded None for every calendar color.
        try:
            from caldav.elements import ical
            props = cal.get_properties([ical.CalendarColor()])
            for v in props.values():
                if v:
                    return str(v)[:9]   # iCloud returns '#RRGGBBAA'
        except Exception as e:
            log.warning("caldav calendar-color fetch failed: %s", e)
            return None
        return None

    def discover(self) -> list[dict]:
        """List the account's calendars/reminder lists. VEVENT vs VTODO comes
        from the supported-component-set; color from apple:calendar-color."""
        out = []
        for cal in self._principal_obj().calendars():
            url = str(getattr(cal, "url", "") or "")
            try:
                comps = list(cal.get_supported_components())
            except Exception:
                comps = ["VEVENT"]
            # iCloud's real layout: calendars advertise VEVENT-only and reminder
            # lists VTODO-only, so treating a both/unknown collection as VEVENT is
            # correct in practice (and the except above falls back to VEVENT).
            comp = "VTODO" if ("VTODO" in comps and "VEVENT" not in comps) \
                else "VEVENT"
            try:
                name = str(cal.get_display_name() or "")
            except Exception:
                name = str(getattr(cal, "name", "") or "")
            out.append({"id": _slug(url), "name": name, "comp": comp,
                        "color": self._color(cal), "_cal": cal})
        return out

    @staticmethod
    def _obj(o) -> dict:
        """One CalDAV object as {href, etag, ics} — href/etag are what the write
        path needs (PUT/DELETE target + If-Match), captured from the first pull."""
        return {"href": str(getattr(o, "url", "") or "") or None,
                "etag": getattr(o, "etag", None), "ics": o.data}

    def fetch_ics(self, collection: dict, lo, hi) -> list[dict]:
        """CalDAV objects (one VCALENDAR per object as {href, etag, ics}) for a
        VEVENT collection over [lo, hi]. Recurrence is expanded downstream
        (ics_events), matching the public-ICS path, so masters+overrides are
        fetched un-expanded here."""
        cal = collection["_cal"]
        import datetime as dt
        start = dt.datetime.combine(lo, dt.time.min)
        end = dt.datetime.combine(hi, dt.time.max)
        objs = cal.search(start=start, end=end, event=True, expand=False)
        return [self._obj(o) for o in objs if getattr(o, "data", None)]

    def fetch_todos(self, collection: dict) -> list[dict]:
        """CalDAV objects ({href, etag, ics}) for a reminders (VTODO) collection,
        including completed ones so grouping can decide what to show."""
        cal = collection["_cal"]
        return [self._obj(t) for t in cal.todos(include_completed=True)
                if getattr(t, "data", None)]
