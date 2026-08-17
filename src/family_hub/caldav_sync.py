"""CalDAV (iCloud) read sync: discover the account's collections, pull each
VEVENT calendar's objects into the events table under the 'caldav:' source scope
(reusing calendar_sync.ics_events for parse + recurrence expansion, exactly like
public ICS feeds), and record each collection's color/name in kv for rendering.

Reminders (VTODO) are a later slice. Gated on the icloud_caldav integration
toggle + credentials; takes an injected client (see caldav_service) so it is
fully testable against a fake. Never raises — a failure records a caldav_status
and keeps the last-good cache, matching the Google/ICS sync's fails-soft rule.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import db as fdb
from . import reminders as remlogic
from .calendar_sync import ics_events

log = logging.getLogger("family_hub.caldav")


def _is_auth_error(exc) -> bool:
    """True if the exception chain is a CalDAV authentication failure — a
    revoked or expired app-specific password, or wrong credentials. Matched by
    class name / 401 / 'unauthorized' so it works without the caldav library
    imported and across its version churn (mirrors calendar_sync._is_auth_error
    for Google). Distinct from a transient network/throttle error: an auth
    failure is surfaced as needs_auth so the wall shows 'Reconnect iCloud' and
    keeps serving the cached view, instead of silently going stale."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        name = type(e).__name__
        msg = str(e).lower()
        if name in ("AuthorizationError",) or "401" in msg \
                or "unauthorized" in msg:
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return False


def sync_once(client, conn, cfg, now: dt.datetime) -> dict:
    prior = fdb.kv_get(conn, "caldav_status") or {}

    def _status(**kw):
        st = {"last_sync": prior.get("last_sync"), **kw}
        fdb.kv_set(conn, "caldav_status", st)
        return st

    try:
        if client is None or not client.configured():
            return _status(ok=False, error="not configured")
        if not fdb.integration_enabled(conn, "icloud_caldav", default=True):
            return _status(ok=False, error="disabled")

        lo_dt = now - dt.timedelta(days=getattr(cfg, "calendar_past_days", 45))
        hi_dt = now + dt.timedelta(days=cfg.calendar_window_days)
        collections = client.discover()

        events: list[dict] = []
        colors: dict = {}
        errors: list[str] = []
        failed: list[str] = []
        needs_auth = False
        for col in (c for c in collections if c.get("comp", "VEVENT") == "VEVENT"):
            cal_id = "caldav:" + col["id"]
            colors[cal_id] = {"name": col.get("name", ""),
                              "color": col.get("color")}
            try:
                for ics in client.fetch_ics(col, lo_dt.date(), hi_dt.date()):
                    events.extend(
                        ics_events(ics, cal_id, lo_dt.date(), hi_dt.date()))
            except Exception as e:  # isolate one bad collection
                errors.append(f"{col.get('name') or cal_id}: {e}")
                failed.append(cal_id)
                needs_auth = needs_auth or _is_auth_error(e)

        # Reminders lists (VTODO). Read-only for now; stored whole in kv and
        # grouped at render (reminders.group).
        rem: list[dict] = []
        for col in (c for c in collections if c.get("comp") == "VTODO"):
            list_id = "caldav:" + col["id"]
            try:
                for ics in client.fetch_todos(col):
                    rem.extend(remlogic.parse_vtodo(ics, list_id,
                                                    col.get("name", "")))
            except Exception as e:
                errors.append(f"{col.get('name') or list_id}: {e}")
                needs_auth = needs_auth or _is_auth_error(e)

        # collection metadata (color/name) drives the wall's rail color + label
        fdb.kv_set(conn, "caldav_collections", colors)
        fdb.kv_set(conn, "caldav_reminders", rem)
        # keep a collection whose fetch failed this round (last-good survives)
        fdb.replace_events_caldav(conn, events, keep_ids=tuple(failed))

        st = {"ok": not errors, "last_sync": now.isoformat(),
              "events": len(events), "reminders": remlogic.open_count(rem)}
        if errors:
            st["error"] = "; ".join(errors)
        if needs_auth:
            st["needs_auth"] = True
        fdb.kv_set(conn, "caldav_status", st)
        return st
    except Exception as e:      # never kill the sync thread; keep the cache
        log.exception("caldav sync_once failed")
        # An auth failure (revoked/expired app password) from discovery is a
        # first-class state: flag needs_auth, keep serving the cached view.
        st = {"ok": False, "error": str(e), "last_sync": prior.get("last_sync")}
        if _is_auth_error(e):
            st["needs_auth"] = True
        fdb.kv_set(conn, "caldav_status", st)
        return st
