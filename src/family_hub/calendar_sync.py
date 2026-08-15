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

import datetime as dt
import logging
import os

from . import db as fdb

log = logging.getLogger("family_hub.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# How long to keep a source's last-good events after it starts returning a
# valid-but-empty result, before accepting the emptiness and letting the cache
# clear. Long enough to ride out a maintenance window; short enough that a
# genuinely-emptied calendar doesn't show stale events indefinitely.
_EMPTY_KEEP_HOURS = 24


def _is_auth_error(exc) -> bool:
    """True if the exception (or its cause chain) is a Google auth/refresh
    failure — i.e. the saved token was revoked/expired and re-authorization is
    required, as distinct from a transient network/quota error. Matched by
    class name so this module still imports without the google libraries."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        if type(e).__name__ in ("RefreshError", "DefaultCredentialsError"):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def normalize_event(item: dict, calendar_id: str) -> dict | None:
    """Google API event resource -> flat row, or None if it should be dropped."""
    if item.get("status") == "cancelled":
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
            "start_ts": start["dateTime"],
            "end_ts": end.get("dateTime", start["dateTime"]),
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


def normalize_ics_event(comp, calendar_id: str) -> dict | None:
    """One (already recurrence-expanded) VEVENT -> flat row, or None."""
    start = comp.decoded("DTSTART", None)
    if start is None:
        return None
    uid = _ics_text(comp, "UID") or "no-uid"
    end = comp.decoded("DTEND", None)
    if isinstance(start, dt.datetime):
        all_day = 0
        end_v = end if isinstance(end, dt.datetime) else start
        start_ts, end_ts = start.isoformat(), end_v.isoformat()
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
               lo: dt.date, hi: dt.date) -> list[dict]:
    """Parse an ICS document and expand recurrences over [lo, hi] inclusive
    (the library's `between` end bound is exclusive for dates)."""
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(data)
    out = []
    for comp in recurring_ical_events.of(cal).between(lo, hi + dt.timedelta(days=1)):
        ev = normalize_ics_event(comp, calendar_id)
        if ev:
            out.append(ev)
    return out


def _rfc3339(d: dt.datetime) -> str:
    if d.tzinfo is None:
        return d.isoformat() + "Z"
    return d.isoformat()


class GoogleCalendarClient:
    def __init__(self, token_path: str):
        self.token_path = token_path

    def configured(self) -> bool:
        """True iff the token file exists and parses as credentials."""
        if not os.path.exists(self.token_path):
            return False
        try:
            from google.oauth2.credentials import Credentials
            Credentials.from_authorized_user_file(self.token_path, SCOPES)
            return True
        except Exception:
            return False

    def _creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
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
                        for item in client.fetch_events(cal["id"], lo, hi):
                            ev = normalize_event(item, cal["id"])
                            if ev:
                                events.append(ev)
                    except Exception as e:
                        errors.append(f"{cal.get('label', cal['id'])}: {e}")
                        failed_ids.append(cal["id"])
                        if _is_auth_error(e):
                            needs_auth = True

        fetcher = ics_fetch or fetch_ics
        for cal in ics_cals:
            try:
                events.extend(ics_events(
                    fetcher(cal["url"]), cal["id"], lo_dt.date(), hi_dt.date()))
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
        synced_ids = {e["calendar_id"] for e in events}
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
            fdb.kv_set(conn, "calendar_status", status)
            return status
        fdb.replace_events(conn, events, keep_ids=keep_ids)
        status = {"ok": not errors, "last_sync": now.isoformat(),
                  "events": len(events)}
        if errors:
            status["error"] = "; ".join(errors)
        if needs_auth:
            status["needs_auth"] = True
        fdb.kv_set(conn, "calendar_status", status)
        return status
    except Exception as e:  # never kill the caller / sync thread
        log.exception("sync_once failed")
        status = {"ok": False, "error": str(e),
                  "last_sync": prior.get("last_sync")}
        fdb.kv_set(conn, "calendar_status", status)
        return status
