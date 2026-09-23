"""Calendar read-only sync: Google Calendar and ICS feeds.

A calendar source in config is Google by default; `"kind": "ics"` with a
`"url"` (https:// or webcal://) syncs any ICS feed instead — iCloud shared
calendars, holiday feeds, school calendars — with full recurrence expansion.

`normalize_event`, `ics_events` and `sync_once` are pure/testable and never
touch the network (clients/fetchers are injected). `GoogleCalendarClient` is
the real Google adapter; its imports (and the icalendar ones) are lazy so the
module imports without the libraries installed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import tempfile

from . import db as fdb

log = logging.getLogger("family_hub.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

def _mark_error_since(status: dict, prior: dict, now: dt.datetime) -> None:
    """Carry the start of a run of NON-auth errors across ticks, so the wall can
    wait out a brief Google/network hiccup instead of flashing "sync hit a snag"
    on the first failed fetch (app._calendar_status_agg reads it). An auth
    failure is surfaced at once and a clean sync clears the clock."""
    if status.get("ok") or status.get("needs_auth"):
        return
    since = prior.get("error_since")
    if not since and prior.get("ok") is False and not prior.get("needs_auth"):
        # an error that was already running before this clock existed (the
        # first sync after deploy, or a status written by another path) is
        # dated from the last good sync, not restarted at now
        since = prior.get("last_sync")
    status["error_since"] = since or now.isoformat()


# How long to keep a source's last-good events after it starts returning a
# valid-but-empty result, before accepting the emptiness and letting the cache
# clear. Long enough to ride out a maintenance window; short enough that a
# genuinely-emptied calendar doesn't show stale events indefinitely.
_EMPTY_KEEP_HOURS = 24


def _is_auth_error(exc) -> bool:
    """True if the exception (or its cause chain) is a Google auth/refresh
    failure — i.e. the saved token was revoked/expired and re-authorization is
    required, as distinct from a transient network/quota error. Matched by
    class name so this module still imports without the google libraries.

    google-auth raises RefreshError for BOTH a revoked token and an outage at
    Google's token server (5xx, "internal_failure"); it marks the second kind
    `retryable`. An outage must not ask the family to reconnect a sign-in that
    is fine, so a retryable RefreshError is not an auth error."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        name = type(e).__name__
        if name == "RefreshError" and getattr(e, "retryable", False):
            return False
        if name in ("RefreshError", "DefaultCredentialsError"):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


# Google event types that are not events the family should see on the wall:
# a work-location marker and a focus-time block. Out-of-office is kept (it
# says someone is away).
_HIDDEN_EVENT_TYPES = {"workingLocation", "focusTime"}


_BAD_TIMES_WARNED: set = set()


def _local_iso(value: str, tz) -> str:
    """An ISO timestamp in the house time zone. Google and ICS feeds carry the
    SOURCE calendar's offset (a calendar set to a fixed -07:00 keeps -07:00
    through November, so its 2:45 class read 3:45 on the wall after the clocks
    changed; review, 2026-09-22). The wall reads the date and clock time
    straight off this text, so it must already be local. A floating (naive)
    time has no zone to convert and is left as written."""
    if tz is None:
        return value
    try:
        t = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        if value not in _BAD_TIMES_WARNED:      # once per value, not every sync
            _BAD_TIMES_WARNED.add(value)
            log.warning("event time %r did not parse; left as sent", value)
        return value
    if t.tzinfo is None:
        return value
    return t.astimezone(tz).isoformat()


def intended_drop(item: dict) -> bool:
    """A Google item we drop on purpose (cancelled, declined, a non-event
    type), as opposed to one we could not read. Only these count toward "the
    calendar answered" in sync_once's empty guard, so a feed that suddenly
    sends only malformed items still reads as suspicious."""
    return (item.get("status") == "cancelled"
            or item.get("eventType") in _HIDDEN_EVENT_TYPES
            or _declined_by_me(item))


def _declined_by_me(item: dict) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined"
               for a in item.get("attendees") or [])


def normalize_event(item: dict, calendar_id: str, tz=None) -> dict | None:
    """Google API event resource -> flat row, or None if it should be dropped.
    `tz` (the house zone) converts timed events; None leaves them as sent."""
    if item.get("status") == "cancelled":
        return None
    if item.get("eventType") in _HIDDEN_EVENT_TYPES or _declined_by_me(item):
        return None
    start = item.get("start") or {}
    end = item.get("end") or {}
    title = item.get("summary") or "(no title)"
    details = {
        "location": item.get("location", ""),
        "description": item.get("description", ""),
        "color_id": item.get("colorId"),
    }
    if "date" in start:  # all-day
        return {
            "id": item["id"], "calendar_id": calendar_id, "title": title,
            "start_ts": start["date"], "end_ts": end.get("date", start["date"]),
            "all_day": 1, "updated": item.get("updated"), **details,
        }
    if "dateTime" in start:  # timed
        return {
            "id": item["id"], "calendar_id": calendar_id, "title": title,
            "start_ts": _local_iso(start["dateTime"], tz),
            "end_ts": _local_iso(end.get("dateTime", start["dateTime"]), tz),
            "all_day": 0, "updated": item.get("updated"), **details,
        }
    return None  # missing start


def _ics_https(url: str) -> str:
    """Apple shares calendars as webcal:// links; that scheme is just https."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def fetch_ics(url: str) -> bytes:
    import httpx
    r = httpx.get(_ics_https(url), timeout=20.0, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _ics_text(comp, key: str) -> str:
    v = comp.get(key)
    return str(v) if v is not None else ""


def normalize_ics_event(comp, calendar_id: str, tz=None) -> dict | None:
    """One (already recurrence-expanded) VEVENT -> flat row, or None.
    `tz` (the house zone) converts timed events; None leaves them as sent."""
    # A cancelled event, or one cancelled occurrence of a recurring one (an
    # override carrying STATUS:CANCELLED), is not on the calendar.
    if _ics_text(comp, "STATUS").upper() == "CANCELLED":
        return None
    start = comp.decoded("DTSTART", None)
    if start is None:
        return None
    uid = _ics_text(comp, "UID") or "no-uid"
    end = comp.decoded("DTEND", None)
    if isinstance(start, dt.datetime):
        all_day = 0
        end_v = end if isinstance(end, dt.datetime) else start
        start_ts = _local_iso(start.isoformat(), tz)
        end_ts = _local_iso(end_v.isoformat(), tz)
    else:  # date-only = all-day; ICS DTEND is exclusive, like Google's
        all_day = 1
        end_v = end if isinstance(end, dt.date) else start + dt.timedelta(days=1)
        start_ts, end_ts = start.isoformat(), end_v.isoformat()
    return {
        # occurrences of a recurring event share a UID — the start makes it unique
        "id": f"{calendar_id}/{uid}/{start_ts}", "calendar_id": calendar_id,
        "title": _ics_text(comp, "SUMMARY") or "(no title)",
        "start_ts": start_ts, "end_ts": end_ts, "all_day": all_day,
        "updated": None, "location": _ics_text(comp, "LOCATION"),
        "description": _ics_text(comp, "DESCRIPTION"), "color_id": None,
    }


def ics_events(data: bytes, calendar_id: str,
               lo: dt.date, hi: dt.date, tz=None,
               stats: dict | None = None) -> list[dict]:
    """Parse an ICS document and expand recurrences over [lo, hi] inclusive
    (the library's `between` end bound is exclusive for dates). `stats`, when
    given, receives {"raw": n}: how many occurrences the feed returned that were
    either kept or dropped on purpose (cancelled); an unreadable one does not
    count (see sync_once's empty guard)."""
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(data)
    out = []
    answered = 0
    for comp in recurring_ical_events.of(cal).between(lo, hi + dt.timedelta(days=1)):
        ev = normalize_ics_event(comp, calendar_id, tz)
        if ev:
            out.append(ev)
            answered += 1
        elif _ics_text(comp, "STATUS").upper() == "CANCELLED":
            answered += 1                      # dropped on purpose
    if stats is not None:
        stats["raw"] = answered
    return out


def _rfc3339(d: dt.datetime) -> str:
    if d.tzinfo is None:
        return d.isoformat() + "Z"
    return d.isoformat()


def _write_secret_atomic(path: str, text: str) -> None:
    """Replace `path` with `text` all at once, owner-only (mode 600). Written to
    a temp file in the same directory, flushed to disk, then swapped in with
    os.replace, so a crash or full disk mid-write can never leave a half token
    behind (which reads as "not connected" until setup is re-run). The temp
    file is removed if anything fails."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".token-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class GoogleCalendarClient:
    def __init__(self, token_path: str):
        self.token_path = token_path
        self._bad_token_warned = False   # throttle: configured() runs every sync tick

    def configured(self) -> bool:
        """True iff the token file exists and parses as credentials."""
        if not os.path.exists(self.token_path):
            self._bad_token_warned = False
            return False
        try:
            from google.oauth2.credentials import Credentials
            Credentials.from_authorized_user_file(self.token_path, SCOPES)
            self._bad_token_warned = False
            return True
        except Exception:
            # The file is PRESENT but won't parse (half-written during a refresh,
            # hand-edited, or a lib schema change). That's a different condition
            # from "never set up" — reporting it silently as "not connected yet"
            # sends the owner to re-run setup for the wrong reason. Log it so the
            # real cause is diagnosable; still fail closed to unconfigured. Warn
            # only once per bad-token episode (configured() runs every sync tick,
            # so a persistently-corrupt file must not spam a stack trace each cycle).
            if not self._bad_token_warned:
                log.warning("token file %s is present but did not parse as "
                            "credentials; treating as not connected",
                            self.token_path, exc_info=True)
                self._bad_token_warned = True
            return False

    def _creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                _write_secret_atomic(self.token_path, creds.to_json())
            except OSError:
                # The refreshed creds still work for this tick, and the old
                # file keeps its refresh token, so the next tick refreshes
                # again. Say so rather than failing the sync over it.
                log.warning("could not save the refreshed token to %s; "
                            "keeping the old file", self.token_path,
                            exc_info=True)
        return creds

    def fetch_calendar_colors(self) -> dict:
        """calendar_id -> the USER'S chosen color for it (calendarList
        backgroundColor — the sidebar color in Google Calendar)."""
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=self._creds(),
                        cache_discovery=False)
        out = {}
        page_token = None
        while True:
            resp = service.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                if item.get("backgroundColor"):
                    out[item["id"]] = item["backgroundColor"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                return out

    def fetch_events(self, calendar_id: str, time_min_iso: str,
                     time_max_iso: str) -> list[dict]:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=self._creds(),
                        cache_discovery=False)
        items: list[dict] = []
        page_token = None
        while True:
            resp = service.events().list(
                calendarId=calendar_id, timeMin=time_min_iso,
                timeMax=time_max_iso, singleEvents=True, orderBy="startTime",
                maxResults=250, pageToken=page_token).execute()
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items


def sync_once(client, conn, cfg, now: dt.datetime, ics_fetch=None) -> dict:
    """Fetch every configured calendar source (Google and/or ICS) into the
    events table and record a `calendar_status` kv row. Never raises. Failures
    are isolated per source: a dead feed keeps its last-good cached events
    while the healthy sources still refresh; only a total failure (every
    source down) leaves the whole cache untouched."""
    prior = fdb.kv_get(conn, "calendar_status") or {}
    try:
        if not cfg.calendars:
            # fresh install: keep the wall's "not connected yet" hint up
            status = {"ok": False, "error": "not configured",
                      "last_sync": prior.get("last_sync")}
            fdb.kv_set(conn, "calendar_status", status)
            return status
        lo_dt = now - dt.timedelta(days=getattr(cfg, "calendar_past_days", 1))
        hi_dt = now + dt.timedelta(days=cfg.calendar_window_days)
        google_cals = [c for c in cfg.calendars
                       if c.get("kind", "google") == "google"]
        ics_cals = [c for c in cfg.calendars if c.get("kind") == "ics"]
        events: list[dict] = []
        errors: list[str] = []
        failed_ids: list[str] = []
        # Calendars that answered with at least one item, even if every item
        # was then filtered out (declined, cancelled, working-location). The
        # empty guard below must not read those as a suspicious empty feed:
        # it kept the just-declined event for a day and raised a false "snag"
        # (review, 2026-09-22).
        answered_ids: set = set()
        needs_auth = False

        if google_cals:
            if not client.configured():
                errors.append("not configured")
                failed_ids += [c["id"] for c in google_cals]
            else:
                # the user's own calendar colors (best-effort: a color hiccup
                # must never break the event sync)
                try:
                    colors = getattr(client, "fetch_calendar_colors", lambda: {})()
                    if colors:
                        fdb.kv_set(conn, "calendar_colors", colors)
                except Exception as e:
                    if _is_auth_error(e):
                        needs_auth = True
                    log.warning("calendar colors fetch failed (non-fatal): %s", e)
                lo, hi = _rfc3339(lo_dt), _rfc3339(hi_dt)
                for cal in google_cals:
                    try:
                        items = client.fetch_events(cal["id"], lo, hi)
                        kept = intended = 0
                        for item in items:
                            ev = normalize_event(item, cal["id"], now.tzinfo)
                            if ev:
                                events.append(ev)
                                kept += 1
                            elif intended_drop(item):
                                intended += 1
                            else:
                                log.warning("%s: an event could not be read (no start): %s",
                                            cal.get("label", cal["id"]), item.get("id"))
                        if kept or intended:
                            answered_ids.add(cal["id"])
                        if intended:
                            log.debug("%s: %d events not shown (cancelled, declined, "
                                      "or not an event)", cal.get("label", cal["id"]), intended)
                    except Exception as e:
                        errors.append(f"{cal.get('label', cal['id'])}: {e}")
                        failed_ids.append(cal["id"])
                        if _is_auth_error(e):
                            needs_auth = True

        fetcher = ics_fetch or fetch_ics
        for cal in ics_cals:
            try:
                stats: dict = {}
                events.extend(ics_events(
                    fetcher(cal["url"]), cal["id"], lo_dt.date(), hi_dt.date(),
                    now.tzinfo, stats))
                if stats.get("raw"):
                    answered_ids.add(cal["id"])
            except Exception as e:
                errors.append(f"{cal.get('label', cal['id'])}: {e}")
                failed_ids.append(cal["id"])

        # Guard against a valid-but-empty fetch wiping last-good events. An
        # HTML/garbage response already RAISES in ics_events (icalendar rejects
        # non-VCALENDAR data) and is handled above; the remaining risk is a
        # source that returns a syntactically-valid EMPTY result (an empty
        # VCALENDAR during maintenance, or Google items:[]). If a source that
        # currently HAS cached events returns zero, keep its last-good rows and
        # flag it, rather than silently deleting a calendar the family relies on.
        #
        # Bounded by a TTL: last-good is kept for up to _EMPTY_KEEP_HOURS of
        # CONTINUOUS emptiness (rides out maintenance windows), after which a
        # genuinely-emptied calendar is finally allowed to clear instead of
        # showing stale events forever. Per-source "empty since" is tracked in kv.
        synced_ids = {e["calendar_id"] for e in events} | answered_ids
        cached_ids = fdb.event_calendar_ids(conn)
        empty_since = fdb.kv_get(conn, "calendar_empty_since") or {}
        suspicious_empty = []
        for cal in cfg.calendars:
            cid = cal["id"]
            if cid in failed_ids:
                continue
            if cid in synced_ids:
                empty_since.pop(cid, None)     # returned events -> reset the empty clock
                continue
            if cid not in cached_ids:
                continue                        # genuinely empty (never had events)
            since = empty_since.get(cid)
            if since is None:
                empty_since[cid] = now.isoformat()   # first empty: start the clock, keep
            else:
                try:
                    age_h = (now - dt.datetime.fromisoformat(since)).total_seconds() / 3600.0
                except Exception as e:
                    # A malformed/mismatched stored timestamp (e.g. a naive vs
                    # tz-aware clock). Don't keep forever (that would strand the
                    # calendar) and don't swallow silently: log and RESET the
                    # clock so the TTL self-heals and starts fresh from now.
                    log.warning("bad empty_since for %s (%r); resetting clock: %s", cid, since, e)
                    empty_since[cid] = now.isoformat()
                    age_h = 0.0
                if age_h >= _EMPTY_KEEP_HOURS:
                    # Accepting the wipe drops every cached row for this
                    # calendar, and deliberately does NOT append to `errors`, so
                    # status stays ok and no banner fires. Say so in the log at
                    # least: this is the one path that silently empties a
                    # calendar the family still sees a window over.
                    log.warning(
                        "%s: empty for %.0fh (>= %sh) — accepting the wipe and "
                        "dropping its cached events", cid, age_h, _EMPTY_KEEP_HOURS)
                    empty_since.pop(cid, None)         # kept long enough -> accept the empty (wipe)
                    continue
            suspicious_empty.append(cid)
            errors.append(
                f"{cal.get('label', cid)}: returned no events (kept last-synced)")
        # Prune clocks for calendars no longer in config (the loop only visits
        # current cfg.calendars, so a removed one would never be popped).
        cfg_ids = {c["id"] for c in cfg.calendars}
        empty_since = {k: v for k, v in empty_since.items() if k in cfg_ids}
        fdb.kv_set(conn, "calendar_empty_since", empty_since)
        keep_ids = tuple(failed_ids) + tuple(suspicious_empty)

        if cfg.calendars and len(keep_ids) == len(cfg.calendars):
            # nothing usable synced — keep the whole cache as-is. Build the
            # status inline (rather than raising into the outer handler) so the
            # needs_auth flag and the prior last_sync survive this path too.
            status = {"ok": False,
                      "error": "; ".join(errors) or "no events from any source",
                      "last_sync": prior.get("last_sync")}
            if needs_auth:
                status["needs_auth"] = True
            _mark_error_since(status, prior, now)
            fdb.kv_set(conn, "calendar_status", status)
            return status
        fdb.replace_events(conn, events, keep_ids=keep_ids)
        status = {"ok": not errors, "last_sync": now.isoformat(),
                  "events": len(events)}
        if errors:
            status["error"] = "; ".join(errors)
        # Coverage records how far the EVENT FETCH reached, so only a fetch-scope
        # failure may hold it back: a calendar that errored, or was kept back on a
        # suspicious empty, still holds rows covering just the OLD span, and
        # /api/calendar must keep reporting that narrower window rather than
        # promising days nobody fetched.
        #
        # Every `errors` entry in THIS module already pairs with a failed_ids or
        # suspicious_empty entry, so gating on `errors` would behave identically
        # today — no test distinguishes them, and none claims to. It is written in
        # fetch-scope terms regardless, to say what the condition actually means
        # and to keep a future non-fetch error (a push, a mirror, a reminder list
        # — precisely what caldav_sync already collects into its own `errors`)
        # from quietly freezing the window and hatching a healthy calendar.
        if not (failed_ids or suspicious_empty):
            fdb.kv_set(conn, "calendar_covered",
                       {"from": lo_dt.date().isoformat(),
                        "to": hi_dt.date().isoformat()})
        if needs_auth:
            status["needs_auth"] = True
        _mark_error_since(status, prior, now)
        fdb.kv_set(conn, "calendar_status", status)
        return status
    except Exception as e:  # never kill the caller / sync thread
        log.exception("sync_once failed")
        status = {"ok": False, "error": str(e),
                  "last_sync": prior.get("last_sync")}
        _mark_error_since(status, prior, now)
        fdb.kv_set(conn, "calendar_status", status)
        return status
