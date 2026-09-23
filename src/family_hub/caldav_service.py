"""iCloud CalDAV access, wrapped so the `caldav` library is swappable and the
rest of the app never imports it directly (global rule: a clean service layer
around an external API).

Credentials are the feature flag: with no ICLOUD_CALDAV_USER /
ICLOUD_CALDAV_APP_PASSWORD, `configured()` is False and the whole subsystem is
inert. The `caldav` import is lazy so this module imports without the library
installed, and the sync logic (caldav_sync.py) takes an injected client with
this shape, so it's fully testable against a fake — no live server needed:

    client.configured() -> bool
    client.discover()   -> [{"id","name","comp"('VEVENT'|'VTODO'|None),"color"}, ...]
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

from .calendar_sync import _write_secret_atomic

log = logging.getLogger("family_hub.caldav")

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


def _creds_path(env):
    """Where UI-entered credentials are stored server-side — a file in the data
    dir (like the Google token), never in git, never in an env the operator has
    to hand out. Set by app.py from DB_PATH's dir."""
    return env.get("CALDAV_CREDS_PATH")


def env_credentials_set(env=None) -> bool:
    """True when ICLOUD_CALDAV_USER + ICLOUD_CALDAV_APP_PASSWORD are both set.
    Those win over the settings file, so while they are set the settings
    screen can neither change the account nor disconnect it (the routes say so
    with a 409 rather than answering ok and changing nothing)."""
    env = env if env is not None else os.environ
    return bool(env.get("ICLOUD_CALDAV_USER")
                and env.get("ICLOUD_CALDAV_APP_PASSWORD"))


def caldav_credentials(env=None):
    """(user, app_password), from env first (advanced/backward-compat) then the
    server-side creds file (the settings UI writes it). Neither -> (None, None)."""
    env = env if env is not None else os.environ
    if env_credentials_set(env):
        return env["ICLOUD_CALDAV_USER"], env["ICLOUD_CALDAV_APP_PASSWORD"]
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
    plaintext never leaves the box; no API ever returns it.

    Written with calendar_sync._write_secret_atomic (temp file, 0600, fsync,
    rename): opening the file in place kept an older file's looser mode (the
    0600 in os.open only applies on create) and a crash mid-write left a
    half file that read as "not connected"."""
    env = env if env is not None else os.environ
    path = _creds_path(env)
    if not path:
        raise RuntimeError("CALDAV_CREDS_PATH not set")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    _write_secret_atomic(path, json.dumps({"user": user,
                                           "app_password": app_password}))


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


class CalDavConflict(Exception):
    """A conditional write was rejected 412: the server object changed under us
    (If-Match no longer matches, or If-None-Match:* found the resource present).
    flush_pending resolves this server-wins rather than silently overwriting the
    other writer's change — the TECHNICAL_DESIGN §5.6 optimistic-concurrency path."""


class CalDavHTTPError(RuntimeError):
    """A write or fetch answered with an error status. `status` is the HTTP
    status, so callers decide from it and never from the message text (which
    holds the URL, and chore reminder URLs carry numbers like 403)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CalDavRejected(CalDavHTTPError):
    """The server refused a write for a reason retrying will not fix (a 4xx such
    as 403 on a read-only or shared list, 400, 405, 409, 415). flush_pending
    counts these toward parking the row instead of retrying it forever, and never
    reads one as a dead password. `status` is the HTTP status."""


# 4xx answers that can clear up on their own, so they stay plain retryable
# errors: 401 (auth, surfaced as reconnect), 407/408 (proxy auth, timeout),
# 412 (a conflict, resolved server-wins), 423/425/429 (locked, too early, rate
# limited). Every other 4xx is a lasting refusal.
_RETRYABLE_4XX = {401, 407, 408, 412, 423, 425, 429}


def _raise_for_write(method: str, url: str, resp, status: int) -> None:
    reason = getattr(resp, "reason", "")
    msg = f"{method} {url} -> {status} {reason}"
    if 400 <= status < 500 and status not in _RETRYABLE_4XX:
        raise CalDavRejected(status, msg)
    raise CalDavHTTPError(status, msg)


_ICAL_CT = "text/calendar; charset=utf-8"


def _resp_status(resp) -> int:
    try:
        return int(getattr(resp, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _resp_etag(resp):
    """The ETag the server assigned this write, from the response headers
    (case-insensitively). None if absent — the next pull reconciles it."""
    h = getattr(resp, "headers", None) or {}
    getter = getattr(h, "get", None)
    if getter:
        return getter("ETag") or getter("etag")
    try:
        return dict(h).get("ETag")
    except Exception:
        return None


def _uid_from_ics(ics: str) -> str:
    """The first component UID, for the create resource filename. Lazy icalendar
    so the module imports without it."""
    import icalendar
    cal = icalendar.Calendar.from_ical(ics)
    for comp in cal.walk():
        if comp.name in ("VTODO", "VEVENT"):
            return str(comp.get("UID") or "")
    return ""


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

    # Per-request ceiling for every DAV call. Without it the sync thread — which
    # also drives the chore mirror and the outbox flush — can hang indefinitely
    # on a REPORT that iCloud never answers, with nothing logged and
    # caldav_status still showing its last healthy sync (the issue #32 freeze
    # class). The event search spans calendar_window_days, so a wider window
    # makes a slow answer likelier, not rarer.
    DAV_TIMEOUT_S = 30

    def _principal_obj(self):
        import caldav
        if self._principal is None:
            dav = caldav.DAVClient(url=self.url, username=self.username,
                                   password=self.password,
                                   timeout=self.DAV_TIMEOUT_S)
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
        from the supported-component-set; color from apple:calendar-color.

        If the component set can't be read this time, "comp" is None: guessing
        VEVENT turned a reminder list into a calendar for a tick. The sync then
        uses the kind it stored for that collection, or skips it this tick."""
        out = []
        for cal in self._principal_obj().calendars():
            url = str(getattr(cal, "url", "") or "")
            try:
                comps = list(cal.get_supported_components())
            except Exception:
                log.warning("caldav supported-components read failed for %s; "
                            "kind unknown this sync", url, exc_info=True)
                comps = None
            # iCloud's real layout: calendars advertise VEVENT-only and reminder
            # lists VTODO-only, so treating a both-kinds collection as VEVENT
            # is correct in practice.
            if comps is None:
                comp = None
            else:
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
        including completed ones: the chore mirror learns about a check-off on
        a phone from the pulled STATUS:COMPLETED, and a reminder missing from
        the pull reads as deleted in iCloud (and is re-created). The sync skips
        any object whose ETag is unchanged, so the completed ones cost the
        transfer only. (A server-side time-range cut was not used: iCloud's
        handling of time-range on VTODOs without dates is not verified.)"""
        cal = collection["_cal"]
        return [self._obj(t) for t in cal.todos(include_completed=True)
                if getattr(t, "data", None)]

    # --- write side (two-way) ---------------------------------------------
    #
    # These issue the DAV requests at the low level (client.put / client.request)
    # rather than via the library's high-level save()/delete(), for one reason:
    # explicit conditional headers. save() only emits If-Match when the resource
    # object happens to carry an .etag, which a freshly-built one never does — so
    # every high-level write is an unconditional overwrite. Sending If-Match /
    # If-None-Match ourselves gives real optimistic concurrency (a 412 on a
    # concurrent phone/Siri edit) instead of a silent last-write-wins clobber.

    def put_object(self, collection: dict, href, ics: str,
                   base_etag=None, uid=None) -> dict:
        """Create (href=None) or update (href set) one object; returns {href,etag}.
        Update sends If-Match: base_etag; create sends If-None-Match:* to a
        UID-derived URL under the collection. Raises CalDavConflict on 412 (the
        server changed under us). A None etag back is fine — the next pull
        reconciles it."""
        cal = collection["_cal"]
        client = cal.client
        if href:
            headers = {"Content-Type": _ICAL_CT}
            if base_etag:
                headers["If-Match"] = base_etag
            resp = client.put(href, ics, headers=headers)
            url = href
        else:
            base = str(getattr(cal, "url", "") or "").rstrip("/")
            url = f"{base}/{uid or _uid_from_ics(ics)}.ics"
            resp = client.put(url, ics, headers={"Content-Type": _ICAL_CT,
                                                 "If-None-Match": "*"})
        status = _resp_status(resp)
        if status == 412:
            raise CalDavConflict(url)
        if status and not (200 <= status < 300):
            _raise_for_write("PUT", url, resp, status)
        return {"href": url, "etag": _resp_etag(resp)}

    def delete_object(self, collection: dict, href: str, base_etag=None) -> None:
        """DELETE one object by href, conditional on If-Match when we have a base
        etag. Raises CalDavConflict on 412; a 404 (already gone) is success."""
        headers = {"If-Match": base_etag} if base_etag else None
        resp = collection["_cal"].client.request(href, "DELETE", "", headers)
        status = _resp_status(resp)
        if status == 412:
            raise CalDavConflict(href)
        if status and status not in (200, 202, 204, 404):
            _raise_for_write("DELETE", href, resp, status)

    def get_object(self, collection: dict, href: str):
        """Fetch one object's current server state as {href, etag, ics}, or None
        if it's gone (404). Used to resolve a write conflict server-wins."""
        resp = collection["_cal"].client.request(href, "GET")
        status = _resp_status(resp)
        if status == 404:
            return None
        if status and not (200 <= status < 300):
            raise CalDavHTTPError(
                status, f"GET {href} -> {status} {getattr(resp, 'reason', '')}")
        body = getattr(resp, "raw", None)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return {"href": href, "etag": _resp_etag(resp), "ics": body}
