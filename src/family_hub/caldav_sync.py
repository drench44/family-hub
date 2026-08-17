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
import re

from . import db as fdb
from . import reminders as remlogic
from .calendar_sync import ics_events

log = logging.getLogger("family_hub.caldav")

# Keep a collection's last-good events for up to this many hours of CONTINUOUS
# valid-but-empty results (rides out an iCloud maintenance / partition flap)
# before accepting the emptiness — mirrors calendar_sync's guard for Google/ICS.
_EMPTY_KEEP_HOURS = 24


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
        if name in ("AuthorizationError",) or "unauthorized" in msg \
                or re.search(r"\b401\b", msg):   # \b so an id like 'room401' doesn't match
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return False


def _object_meta(raw_ics: str):
    """uid/summary/sequence/last_modified from one object's ICS (first VEVENT or
    VTODO), for the cal_objects store. None if there's no component."""
    import icalendar
    cal = icalendar.Calendar.from_ical(raw_ics)
    for comp in cal.walk():
        if comp.name in ("VEVENT", "VTODO"):
            try:
                seq = int(comp.get("SEQUENCE")) if comp.get("SEQUENCE") is not None else 0
            except Exception:
                seq = 0
            lm = comp.get("LAST-MODIFIED")
            try:
                lm_iso = lm.dt.isoformat() if lm is not None else None
            except Exception:
                lm_iso = str(lm) if lm else None
            return {"comp_type": comp.name, "uid": str(comp.get("UID") or ""),
                    "summary": str(comp.get("SUMMARY") or ""),
                    "sequence": seq, "last_modified": lm_iso}
    return None


def _store_object(conn, collection_id: str, comp_type: str, obj: dict,
                  seen: set) -> None:
    """Persist one CalDAV object into cal_objects (the round-trip store) and note
    its id in `seen` for the prune. Best-effort: a parse failure here must never
    break the render path, so it is caught and skipped."""
    try:
        meta = _object_meta(obj["ics"])
        if not (meta and meta["uid"]):
            return
        oid = f"{collection_id}/{meta['uid']}"
        fdb.upsert_cal_object_synced(conn, {
            "id": oid, "collection_id": collection_id, "comp_type": comp_type,
            "uid": meta["uid"], "href": obj.get("href"), "etag": obj.get("etag"),
            "summary": meta["summary"], "raw_ics": obj["ics"],
            "sequence": meta["sequence"], "last_modified": meta["last_modified"]})
        seen.add(oid)
    except Exception:
        log.warning("caldav object store skipped (%s)", collection_id, exc_info=True)


def flush_pending(client, conn, collections, now_iso: str) -> dict:
    """Push the outbox — locally-edited cal_objects (wall edits) — to iCloud: PUT
    creates/updates, DELETE removals. Only collections discovered this round are
    flushable; a row whose collection wasn't seen this pull waits for the next.
    Per-row failures are isolated and recorded (the row stays PENDING and retries
    next sync); an auth failure is surfaced so the wall shows Reconnect. Returns
    {pushed, errors, needs_auth}. Never raises — a bad push must not blank the
    calendar, same fails-soft rule as the read path."""
    col_by_id = {"caldav:" + c["id"]: c for c in collections}
    pushed, errors, needs_auth = 0, [], False
    for row in fdb.caldav_pending(conn):
        col = col_by_id.get(row["collection_id"])
        if col is None:
            continue   # collection not in this discover; retry next round
        try:
            if row["sync_state"] == "PENDING_DELETE":
                if row.get("href"):
                    client.delete_object(col, row["href"])
                fdb.delete_cal_object_row(conn, row["id"])
            else:   # PENDING_CREATE | PENDING_UPDATE
                res = client.put_object(col, row.get("href"), row["raw_ics"])
                fdb.mark_cal_object_pushed(
                    conn, row["id"], (res or {}).get("href") or row.get("href"),
                    (res or {}).get("etag"))
            pushed += 1
        except Exception as e:
            fdb.record_cal_object_error(conn, row["id"], str(e), now_iso)
            errors.append(f"{row['id']}: {e}")
            needs_auth = needs_auth or _is_auth_error(e)
            log.warning("caldav push failed for %s", row["id"], exc_info=True)
    return {"pushed": pushed, "errors": errors, "needs_auth": needs_auth}


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
        # Persist every discovered collection (calendars + reminder lists) so the
        # settings calendar picker has a per-collection visibility toggle; upsert
        # keeps the operator's toggle across syncs. Never pruned — a discover blip
        # must not drop the picker state.
        now_iso = now.isoformat()
        for col in collections:
            fdb.upsert_caldav_collection(
                conn, "caldav:" + col["id"], col.get("comp", "VEVENT"),
                col.get("name", ""), col.get("color"), now_iso)

        events: list[dict] = []
        errors: list[str] = []
        failed: list[str] = []
        needs_auth = False
        for col in (c for c in collections if c.get("comp", "VEVENT") == "VEVENT"):
            cal_id = "caldav:" + col["id"]
            seen_objs: set = set()
            try:
                for obj in client.fetch_ics(col, lo_dt.date(), hi_dt.date()):
                    _store_object(conn, cal_id, "VEVENT", obj, seen_objs)
                    events.extend(
                        ics_events(obj["ics"], cal_id, lo_dt.date(), hi_dt.date()))
                if seen_objs:   # prune only when the collection returned objects
                    fdb.prune_cal_objects(conn, cal_id, seen_objs)
            except Exception as e:  # isolate one bad collection
                errors.append(f"{col.get('name') or cal_id}: {e}")
                failed.append(cal_id)
                needs_auth = needs_auth or _is_auth_error(e)

        # Reminders lists (VTODO). Read-only for now; stored whole in kv and
        # grouped at render (reminders.group).
        rem: list[dict] = []
        vtodo_failed: list[str] = []
        for col in (c for c in collections if c.get("comp") == "VTODO"):
            list_id = "caldav:" + col["id"]
            seen_todos: set = set()
            try:
                for obj in client.fetch_todos(col):
                    _store_object(conn, list_id, "VTODO", obj, seen_todos)
                    rem.extend(remlogic.parse_vtodo(obj["ics"], list_id,
                                                    col.get("name", "")))
                if seen_todos:
                    fdb.prune_cal_objects(conn, list_id, seen_todos)
            except Exception as e:
                errors.append(f"{col.get('name') or list_id}: {e}")
                vtodo_failed.append(list_id)
                needs_auth = needs_auth or _is_auth_error(e)

        # Valid-but-empty guard (mirrors the Google/ICS path): a VEVENT
        # collection that synced ZERO events this round but HAD cached events is
        # kept, bounded by a per-collection TTL, so an iCloud maintenance blip or
        # an empty discover() doesn't silently blank the family's calendar and
        # report success.
        attempted = {"caldav:" + c["id"] for c in collections
                     if c.get("comp", "VEVENT") == "VEVENT"}
        synced_ids = {e["calendar_id"] for e in events}
        cached_ids = {cid for cid in fdb.event_calendar_ids(conn)
                      if cid.startswith("caldav:")}
        empty_since = fdb.kv_get(conn, "caldav_empty_since") or {}
        suspicious: list[str] = []
        if not collections and cached_ids:
            # empty/partial discover with a live cache -> keep everything, flag
            suspicious = list(cached_ids)
            errors.append("discover returned no collections (kept last-synced)")
        else:
            for cid in attempted:
                if cid in failed or cid in synced_ids:
                    empty_since.pop(cid, None)      # returned events -> reset clock
                    continue
                if cid not in cached_ids:
                    continue                        # genuinely empty, never had rows
                since = empty_since.get(cid)
                if since is None:
                    empty_since[cid] = now.isoformat()
                else:
                    try:
                        age_h = (now - dt.datetime.fromisoformat(since)).total_seconds() / 3600.0
                    except Exception:
                        empty_since[cid] = now.isoformat()
                        age_h = 0.0
                    if age_h >= _EMPTY_KEEP_HOURS:
                        empty_since.pop(cid, None)   # kept long enough -> accept wipe
                        continue
                suspicious.append(cid)
                errors.append(f"{cid}: returned no events (kept last-synced)")
        empty_since = {k: v for k, v in empty_since.items() if k in attempted}
        fdb.kv_set(conn, "caldav_empty_since", empty_since)

        # Reminders keep last-good on a per-list fetch failure or an empty
        # discover, rather than dropping them (they aren't in `rem`).
        if not collections:
            rem = fdb.kv_get(conn, "caldav_reminders") or []
        elif vtodo_failed:
            keep = set(vtodo_failed)
            rem.extend(r for r in (fdb.kv_get(conn, "caldav_reminders") or [])
                       if r.get("list_id") in keep)

        fdb.kv_set(conn, "caldav_reminders", rem)
        fdb.replace_events_caldav(
            conn, events, keep_ids=tuple(set(failed) | set(suspicious)))

        # Two-way: push the outbox (wall edits) to iCloud AFTER the pull, so the
        # pull's prune has already kept un-pushed PENDING rows (no create-then-
        # prune race). Gated on the operator's 1-way/2-way choice via the
        # integration's `readonly` flag (default True = read-only until they opt
        # into writes in settings — never touch someone's iCloud unasked). The
        # read path overlays these same PENDING edits, so the wall reflects a
        # click instantly regardless of when the push lands.
        cfg_row = fdb.integration_config(conn, "icloud_caldav") or {}
        if not cfg_row.get("readonly", True):
            flushed = flush_pending(client, conn, collections, now.isoformat())
            errors.extend(flushed["errors"])
            needs_auth = needs_auth or flushed["needs_auth"]

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
