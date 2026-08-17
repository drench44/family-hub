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
from .caldav_service import CalDavConflict
from .calendar_sync import ics_events

log = logging.getLogger("family_hub.caldav")

# Keep a collection's last-good events for up to this many hours of CONTINUOUS
# valid-but-empty results (rides out an iCloud maintenance / partition flap)
# before accepting the emptiness — mirrors calendar_sync's guard for Google/ICS.
_EMPTY_KEEP_HOURS = 24

# Surface a NON-auth sync error on the wall only after it has failed continuously
# for this long. A transient network/5xx/TLS flap self-heals within a tick or two
# and the wall keeps serving cached events, so warning on the first failure would
# just flicker a banner on and off. Long enough to ride out an iCloud maintenance
# window, short enough to surface a genuinely stuck feed within a quarter-day.
# (needs_auth is surfaced immediately elsewhere — a dead app password won't heal.)
_ERROR_SURFACE_HOURS = 6


def _is_auth_error(exc) -> bool:
    """True if the exception chain is a CalDAV authentication failure — a
    revoked or expired app-specific password, or wrong credentials. Matched by
    class name / 401 / 403 / 'unauthorized' / 'forbidden' so it works without the
    caldav library imported and across its version churn (mirrors
    calendar_sync._is_auth_error for Google). iCloud answers a dead app password
    with 401 OR 403 depending on the path, so both must flag needs_auth. Distinct
    from a transient network/throttle error: an auth failure is surfaced as
    needs_auth so the wall shows 'Reconnect iCloud' and keeps serving the cached
    view, instead of silently going stale.

    Known limitation: 403 is less clean than 401 — WebDAV can also return it for a
    permission-denied on one shared calendar the account can see but not read, so
    a single such collection can flip the whole account's banner to 'reconnect'
    (which reconnecting won't clear). We accept that because iCloud genuinely
    answers a dead app password with 403 on some paths, and a stuck banner is a
    better failure than silent staleness; revisit with a live-account error
    sample if false 'reconnect' prompts show up."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        name = type(e).__name__
        msg = str(e).lower()
        if name in ("AuthorizationError", "ForbiddenError") \
                or "unauthorized" in msg or "forbidden" in msg \
                or re.search(r"\b40[13]\b", msg):   # \b so an id like 'room4012' doesn't match
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return False


def _apply_error_persistence(conn, st, now) -> None:
    """Track how long a real (non-auth, non-config) sync error has run and mark
    the status `sustained` once it crosses _ERROR_SURFACE_HOURS. Any ok sync — or
    a mere disabled/not-configured state — clears the clock, so only a genuinely
    stuck source ever reads as sustained, never a one-tick blip. needs_auth
    surfaces immediately on its own and is deliberately excluded here. Mutates st
    in place; call it right before persisting caldav_status."""
    err = st.get("error")
    real_error = (not st.get("ok") and err
                  and err not in ("disabled", "not configured")
                  and not st.get("needs_auth"))
    if not real_error:
        fdb.kv_set(conn, "caldav_error_since", None)   # recovered / not an error
        return
    since = fdb.kv_get(conn, "caldav_error_since")
    if not since:
        fdb.kv_set(conn, "caldav_error_since", now.isoformat())
        return                       # first failure: start the clock, not yet sustained
    try:
        age_h = (now - dt.datetime.fromisoformat(since)).total_seconds() / 3600.0
    except Exception:
        fdb.kv_set(conn, "caldav_error_since", now.isoformat())
        return
    if age_h >= _ERROR_SURFACE_HOURS:
        st["sustained"] = True


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


def _resolve_conflict(client, conn, col, row) -> None:
    """Server-wins resolution for a 412: adopt the server's current copy over our
    losing local edit — instead of silently clobbering the concurrent phone/Siri
    change — and log the dropped edit so it's never lost silently. If the object
    is gone on the server, drop the local row to match. (Field-level auto-merge —
    re-applying the wall's completion onto the server's newer body — is a
    documented future step, TECHNICAL_DESIGN §5.6.)"""
    href = row.get("href")
    fresh = client.get_object(col, href) if href else None
    if not fresh or not fresh.get("ics"):
        fdb.delete_cal_object_row(conn, row["id"])
        log.warning("caldav conflict on %s: server object gone; dropped local edit",
                    row["id"])
        return
    meta = _object_meta(fresh["ics"]) or {}
    fdb.upsert_cal_object_synced(conn, {
        "id": row["id"], "collection_id": row["collection_id"],
        "comp_type": row["comp_type"], "uid": row["uid"],
        "href": fresh.get("href") or href, "etag": fresh.get("etag"),
        "summary": meta.get("summary", row.get("summary", "")),
        "raw_ics": fresh["ics"], "sequence": meta.get("sequence", 0),
        "last_modified": meta.get("last_modified")}, force=True)
    log.warning("caldav conflict on %s: server wins; dropped losing local edit",
                row["id"])


def flush_pending(client, conn, collections, now_iso: str) -> dict:
    """Push the outbox — locally-edited cal_objects (wall edits) — to iCloud: PUT
    creates/updates (conditional on If-Match/If-None-Match), DELETE removals. Only
    collections discovered this round are flushable; a row whose collection wasn't
    seen this pull records why and waits for the next. Per-row failures are
    isolated and recorded (the row stays PENDING and retries next sync); a 412 is
    resolved server-wins; an auth failure is surfaced so the wall shows Reconnect.
    Returns {pushed, conflicts, errors, needs_auth}. Never raises — a bad push
    must not blank the calendar, same fails-soft rule as the read path."""
    col_by_id = {"caldav:" + c["id"]: c for c in collections}
    pushed, conflicts, errors, needs_auth = 0, 0, [], False
    for row in fdb.caldav_pending(conn):
        col = col_by_id.get(row["collection_id"])
        if col is None:
            # not discovered this round: record why so a permanently-unroutable
            # row is VISIBLE (per-row error + it keeps counting toward the pending
            # backlog) instead of silently optimistic forever. Retries next sync.
            fdb.record_cal_object_error(
                conn, row["id"], "collection not discovered this sync", now_iso)
            continue
        try:
            if row["sync_state"] == "PENDING_DELETE":
                if row.get("href"):
                    client.delete_object(col, row["href"],
                                         base_etag=row.get("base_etag"))
                else:
                    log.warning("caldav delete of %s had no href; dropped locally",
                                row["id"])
                fdb.delete_cal_object_row(conn, row["id"])
            else:   # PENDING_CREATE | PENDING_UPDATE
                res = client.put_object(col, row.get("href"), row["raw_ics"],
                                        base_etag=row.get("base_etag"),
                                        uid=row.get("uid")) or {}
                href = res.get("href") or row.get("href")
                if not href:
                    # a create that came back with no resource URL: do NOT mark it
                    # SYNCED — a later delete would then skip the server and
                    # silently drop it. Keep PENDING_CREATE and retry.
                    raise RuntimeError("create returned no href")
                fdb.mark_cal_object_pushed(conn, row["id"], href, res.get("etag"))
            pushed += 1
        except CalDavConflict:
            try:
                _resolve_conflict(client, conn, col, row)
                conflicts += 1
            except Exception as e:
                fdb.record_cal_object_error(
                    conn, row["id"], f"conflict-resolve failed: {e}", now_iso)
                errors.append(f"{row['id']}: conflict-resolve failed: {e}")
                needs_auth = needs_auth or _is_auth_error(e)
                log.warning("caldav conflict-resolve failed for %s", row["id"],
                            exc_info=True)
        except Exception as e:
            fdb.record_cal_object_error(conn, row["id"], str(e), now_iso)
            errors.append(f"{row['id']}: {e}")
            needs_auth = needs_auth or _is_auth_error(e)
            log.warning("caldav push failed for %s", row["id"], exc_info=True)
    return {"pushed": pushed, "conflicts": conflicts, "errors": errors,
            "needs_auth": needs_auth}


def sync_once(client, conn, cfg, now: dt.datetime) -> dict:
    prior = fdb.kv_get(conn, "caldav_status") or {}

    def _status(**kw):
        st = {"last_sync": prior.get("last_sync"), **kw}
        _apply_error_persistence(conn, st, now)
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
            n_objs = n_skipped = 0
            try:
                for obj in client.fetch_ics(col, lo_dt.date(), hi_dt.date()):
                    n_objs += 1
                    _store_object(conn, cal_id, "VEVENT", obj, seen_objs)
                    try:
                        events.extend(ics_events(
                            obj["ics"], cal_id, lo_dt.date(), hi_dt.date()))
                    except Exception:
                        # One unparseable event must not freeze the whole
                        # calendar behind a collection error (which would keep
                        # every other event stale); skip it and log, letting the
                        # rest of the collection sync.
                        n_skipped += 1
                        log.warning("caldav event parse skipped (%s)", cal_id,
                                    exc_info=True)
                if n_objs and n_skipped == n_objs:
                    # EVERY fetched object failed to parse: not a transient blip
                    # but a systematic break (a parser regression or a wholesale-
                    # malformed feed). Flag the collection so ok goes False and its
                    # cache is kept, instead of reporting a healthy-but-empty
                    # calendar and silently blanking it after the empty-guard TTL.
                    raise RuntimeError(f"parsed 0 of {n_objs} objects")
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
            n_todos = n_skipped = 0
            try:
                for obj in client.fetch_todos(col):
                    n_todos += 1
                    _store_object(conn, list_id, "VTODO", obj, seen_todos)
                    try:
                        rem.extend(remlogic.parse_vtodo(
                            obj["ics"], list_id, col.get("name", "")))
                    except Exception:
                        # Skip one unparseable reminder rather than failing the
                        # whole list and freezing the rest behind an error.
                        n_skipped += 1
                        log.warning("caldav reminder parse skipped (%s)", list_id,
                                    exc_info=True)
                if n_todos and n_skipped == n_todos:
                    # Every todo failed to parse -> systematic break; flag the list
                    # (keeps its cache) rather than report it healthy-but-empty.
                    raise RuntimeError(f"parsed 0 of {n_todos} todos")
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
              "events": len(events), "reminders": remlogic.open_count(rem),
              # outbox backlog after this flush: un-pushed wall edits still queued
              # (0 in the normal case). Non-zero + not moving => something stuck;
              # the settings menu can surface it instead of it being invisible.
              "pending": len(fdb.caldav_pending(conn))}
        if errors:
            st["error"] = "; ".join(errors)
        if needs_auth:
            st["needs_auth"] = True
        _apply_error_persistence(conn, st, now)
        fdb.kv_set(conn, "caldav_status", st)
        return st
    except Exception as e:      # never kill the sync thread; keep the cache
        log.exception("caldav sync_once failed")
        # An auth failure (revoked/expired app password) from discovery is a
        # first-class state: flag needs_auth, keep serving the cached view.
        st = {"ok": False, "error": str(e), "last_sync": prior.get("last_sync"),
              # carry the outbox depth even on a failed sync — a queued wall edit
              # is most worth surfacing exactly when syncing is broken.
              "pending": len(fdb.caldav_pending(conn))}
        if _is_auth_error(e):
            st["needs_auth"] = True
        _apply_error_persistence(conn, st, now)
        fdb.kv_set(conn, "caldav_status", st)
        return st
