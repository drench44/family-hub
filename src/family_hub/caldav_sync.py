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
from .calendar_sync import ics_events

log = logging.getLogger("family_hub.caldav")


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
        vevent = [c for c in client.discover()
                  if c.get("comp", "VEVENT") == "VEVENT"]

        events: list[dict] = []
        colors: dict = {}
        errors: list[str] = []
        failed: list[str] = []
        for col in vevent:
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

        # collection metadata (color/name) drives the wall's rail color + label
        fdb.kv_set(conn, "caldav_collections", colors)
        # keep a collection whose fetch failed this round (last-good survives)
        fdb.replace_events_caldav(conn, events, keep_ids=tuple(failed))

        st = {"ok": not errors, "last_sync": now.isoformat(),
              "events": len(events)}
        if errors:
            st["error"] = "; ".join(errors)
        fdb.kv_set(conn, "caldav_status", st)
        return st
    except Exception as e:      # never kill the sync thread
        log.exception("caldav sync_once failed")
        return _status(ok=False, error=str(e))
