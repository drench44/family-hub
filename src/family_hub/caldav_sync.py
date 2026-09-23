"""CalDAV (iCloud) sync: discover the account's collections, pull each VEVENT
calendar's objects into the events table under the 'caldav:' source scope
(reusing calendar_sync.ics_events for parse + recurrence expansion, exactly like
public ICS feeds), pull each reminder list (VTODO) into the cal_objects store
the wall renders from, record each collection's color/name for rendering, and
(two-way) push the outbox of wall edits back.

Gated on the icloud_caldav integration toggle + credentials; takes an injected
client (see caldav_service) so it is fully testable against a fake. Never raises
(a failure records a caldav_status and keeps the last-good cache, matching the
Google/ICS sync's fails-soft rule).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from . import db as fdb
from .caldav_service import CalDavConflict, CalDavRejected
from .calendar_sync import cached_ids_in_window, ics_events

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

# A collection saved from an earlier sync that discover() stops returning
# (deleted or unshared in iCloud) is dropped once it has been missing this
# long, with its pulled reminders. Long enough that a partial discover during
# an iCloud blip never drops a list the family still has.
_LIST_GONE_HOURS = 24

# The sync discovers every few minutes. A list's missing clock only runs while
# good discovers keep seeing it missing: if the last sighting is older than
# this, the syncs in between failed (or the hub was off), so nothing says the
# list stayed gone, and the clock starts over. Without this a list seen
# missing once, then a day of iCloud failures, was dropped on the next good
# sync after only a second sighting.
_MISSING_GAP_HOURS = 2

# Why a row is parked when its list is gone. unpark_cal_objects matches on it
# when the list comes back.
_GONE_REASON = "its list is no longer in iCloud"

# Wall-clock budget for one flush of the outbox. Each DAV request may take up
# to CalDavClient.DAV_TIMEOUT_S, and the flush runs on the one sync thread the
# Google sync also uses, so a half-down iCloud must not hold it for the whole
# backlog. What is left waits for the next tick.
FLUSH_BUDGET_S = 90

# Save a reminder list's pulled changes in batches of this many writes: one
# transaction per list rather than one per object, while a very large first
# pull never holds the write lock long enough to time out a wall request
# (busy_timeout is 5s).
_PULL_BATCH = 200


def _http_status(exc):
    """The HTTP status an exception chain carries, read from its type and
    fields, never from its message text: the message holds the URL, and chore
    reminder URLs carry the chore id (familyhub-chore-403-...), so a text scan
    read a 503 on chore 403 as a refusal. Our own CalDavHTTPError (and
    CalDavRejected) carry `status`. The caldav library raises AuthorizationError
    for both 401 and 403 with no status, only the server's reason phrase in its
    `reason` field, so that field (not the message) tells them apart; an unknown
    reason reads as 401, the dead-password case. None if nothing in the chain
    says."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        status = getattr(e, "status", None)
        if isinstance(status, int) and not isinstance(status, bool) and status:
            return status
        name = type(e).__name__
        if name == "ForbiddenError":
            return 403
        if name == "AuthorizationError":
            reason = str(getattr(e, "reason", "") or "").strip().lower()
            return 403 if reason == "forbidden" else 401
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return None


def _is_auth_error(exc) -> bool:
    """True if the exception chain is a CalDAV authentication failure — a
    revoked or expired app-specific password, or wrong credentials. Decided by
    the status the chain carries (_http_status) when it has one. Otherwise it
    falls back to matching the text: 401 / 403 / 'unauthorized' / 'forbidden',
    so an untyped error from discovery still counts, across the caldav
    library's version churn (mirrors calendar_sync._is_auth_error for Google).
    iCloud answers a dead app password with 401 OR 403 depending on the path,
    so both must flag needs_auth. Distinct from a transient network/throttle
    error: an auth failure is surfaced as needs_auth so the wall shows
    'Reconnect iCloud' and keeps serving the cached view, instead of silently
    going stale.

    Known limitation: 403 is less clean than 401 — WebDAV can also return it for a
    permission-denied on one shared calendar the account can see but not read, so
    a single such collection can flip the whole account's banner to 'reconnect'
    (which reconnecting won't clear). We accept that because iCloud genuinely
    answers a dead app password with 403 on some paths, and a stuck banner is a
    better failure than silent staleness; revisit with a live-account error
    sample if false 'reconnect' prompts show up."""
    status = _http_status(exc)
    if status is not None:
        return status in (401, 403)
    e, seen = exc, 0
    while e is not None and seen < 10:
        msg = str(e).lower()
        if "unauthorized" in msg or "forbidden" in msg \
                or re.search(r"\b40[13]\b", msg):   # \b so an id like 'room4012' doesn't match
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return False


def _is_refusal(exc) -> bool:
    """True if a PUSH failed because iCloud refuses that one write for good: a
    CalDavRejected (a lasting 4xx) or a 403 anywhere in the chain (the caldav
    library raises AuthorizationError for 403 too). Decided from the status
    (_http_status), never the message text. Only asked of push errors:
    discovery and the pull already worked with these credentials this tick, so
    a 403 here is the list refusing the write (read-only or shared), not a dead
    password, and 'Reconnect iCloud' would not help."""
    e, seen = exc, 0
    while e is not None and seen < 10:
        if isinstance(e, CalDavRejected):
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        seen += 1
    return _http_status(exc) == 403


def _apply_error_persistence(conn, st, now, hard_error: bool) -> None:
    """Track how long a HARD sync error has run and mark the status `sustained`
    once it crosses _ERROR_SURFACE_HOURS. A hard error is a genuine fetch/discover
    EXCEPTION (a collection/list raised, or the whole sync did) — NOT a protective
    'kept last-synced' soft state: an empty-but-cached window or a discover blip
    keeps the cache and is not a stuck feed, so it must not feed this clock (or a
    calendar that simply has no upcoming events would falsely read 'trouble
    syncing'). An ok sync, a disabled/not-configured state, or an auth failure
    (surfaced immediately on its own) all clear the clock too — so only a
    genuinely stuck source ever reads as sustained, never a one-tick blip. Mutates
    st in place; call it right before persisting caldav_status."""
    if not (hard_error and not st.get("needs_auth")):
        fdb.kv_set(conn, "caldav_error_since", None)   # recovered / not a hard error
        return
    since = fdb.kv_get(conn, "caldav_error_since")
    if not since:
        fdb.kv_set(conn, "caldav_error_since", now.isoformat())
        return                       # first failure: start the clock, not yet sustained
    try:
        age_h = (now - dt.datetime.fromisoformat(since)).total_seconds() / 3600.0
    except Exception:
        # A corrupt/unparseable stored timestamp: log it (never swallow silently)
        # and restart the clock so a genuinely stuck feed still surfaces after a
        # fresh window, rather than the wall reading healthy forever.
        log.warning("caldav_error_since unparseable (%r); resetting clock", since,
                    exc_info=True)
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
                  seen: set, index: dict) -> str:
    """Persist one pulled CalDAV object into cal_objects (the round-trip store)
    and note its id in `seen` for the prune. Returns 'unchanged', 'stored' or
    'failed'.

    `index` (fdb.cal_object_index) maps each stored href to its id and ETag. An
    object whose ETag matches is unchanged on the server: it is only marked
    seen, never parsed or rewritten. Every tick used to re-parse and re-save
    every reminder, and completed ones pile up by the thousand. The write is
    left open in the current transaction; the caller saves the batch.

    Best-effort: a parse failure must never break the render path, so it is
    caught, logged and reported as 'failed'."""
    known = index.get(obj.get("href")) if obj.get("href") else None
    if known and obj.get("etag") and known["etag"] == obj["etag"]:
        seen.add(known["id"])
        return "unchanged"
    try:
        meta = _object_meta(obj["ics"])
        if not (meta and meta["uid"]):
            log.warning("caldav object without a UID skipped (%s)", collection_id)
            return "failed"
        oid = f"{collection_id}/{meta['uid']}"
        fdb.upsert_cal_object_synced(conn, {
            "id": oid, "collection_id": collection_id, "comp_type": comp_type,
            "uid": meta["uid"], "href": obj.get("href"), "etag": obj.get("etag"),
            "summary": meta["summary"], "raw_ics": obj["ics"],
            "sequence": meta["sequence"], "last_modified": meta["last_modified"]},
            commit=False)
        seen.add(oid)
        return "stored"
    except Exception:
        log.warning("caldav object store skipped (%s)", collection_id, exc_info=True)
        return "failed"


def _resolve_conflict(client, conn, col, row, now_iso: str,
                      conflict_href=None) -> None:
    """Server-wins resolution for a 412: adopt the server's current copy over our
    losing local change (instead of silently clobbering the concurrent phone/Siri
    edit) and log what was dropped so it's never lost silently. That includes a
    wall delete whose own DELETE conflicts: the reminder was edited on another
    device, and that edit is kept. If the object is gone on the server, the
    local row goes too, except a create, which is kept and retried. (Field-level
    auto-merge, re-applying the wall's completion onto the server's newer body,
    is a documented future step, TECHNICAL_DESIGN §5.6.)

    `row` is the version the push sent. A wall change queued while that request
    was out is handled on its own terms, never forced over here:
      - the row was deleted (a create that was still uploading): the server copy
        is queued for delete, not adopted, or the reminder would come back;
      - the row is now a queued delete: that delete came after the other
        device's edit, so it moves onto the server's current copy and goes
        ahead next flush;
      - the row was edited again: it stays queued, and its own push meets the
        same 412 next flush, where server-wins drops it (the edit was built on
        the copy that just lost, so pushing it would overwrite the phone's).
    `conflict_href` is the URL the 412 came from, which a create's row lacks."""
    href = row.get("href") or conflict_href
    fresh = client.get_object(col, href) if href else None
    have_server_copy = bool(fresh and fresh.get("ics"))
    server_href = (fresh.get("href") if fresh else None) or href
    current = fdb.get_cal_object(conn, row["id"])
    if current is None:
        if not have_server_copy:
            return
        if fdb.queue_orphan_cal_delete(conn, row, server_href,
                                       fresh.get("etag"), now_iso) is None:
            # re-added under the same id meanwhile: that row takes over the
            # iCloud copy as an update (-1 matches no revision, never SYNCED)
            fdb.mark_cal_object_pushed(conn, row["id"], server_href,
                                       fresh.get("etag"), -1)
            log.info("caldav conflict on %s: re-added on the wall; it takes over "
                     "the iCloud copy", row["id"])
        else:
            log.info("caldav conflict on %s: it was deleted on the wall; "
                     "queued the iCloud copy for delete", row["id"])
        return
    if current["local_rev"] != row["local_rev"]:
        if current["sync_state"] == "PENDING_DELETE":
            if not have_server_copy:
                fdb.finish_cal_object_delete(conn, row["id"], current["local_rev"])
                log.info("caldav conflict on %s: deleted on the wall and gone "
                         "from iCloud too", row["id"])
            elif fdb.adopt_server_copy_for_delete(conn, row["id"], server_href,
                                                  fresh.get("etag")):
                log.info("caldav conflict on %s: deleted on the wall meanwhile; "
                         "the delete goes ahead on iCloud's current copy",
                         row["id"])
            return
        log.info("caldav conflict on %s: changed on the wall meanwhile; the "
                 "newer change stays queued", row["id"])
        return
    if not have_server_copy:
        if row["sync_state"] == "PENDING_CREATE":
            # our create was refused, yet nothing is there: keep the new
            # reminder queued; the caller records this and it retries
            raise RuntimeError("create conflicted but iCloud has no copy there; "
                               "will retry")
        if fdb.finish_cal_object_delete(conn, row["id"], row["local_rev"]) \
                == "deleted":
            log.warning("caldav conflict on %s: server object gone; "
                        "dropped local edit", row["id"])
        return
    meta = _object_meta(fresh["ics"]) or {}
    adopted = fdb.upsert_cal_object_synced(conn, {
        "id": row["id"], "collection_id": row["collection_id"],
        "comp_type": row["comp_type"], "uid": row["uid"],
        "href": server_href, "etag": fresh.get("etag"),
        "summary": meta.get("summary", row.get("summary", "")),
        "raw_ics": fresh["ics"], "sequence": meta.get("sequence", 0),
        "last_modified": meta.get("last_modified")},
        force=True, expected_rev=row["local_rev"])
    if not adopted:
        log.info("caldav conflict on %s: changed on the wall meanwhile; the "
                 "newer change stays queued", row["id"])
    elif row["sync_state"] == "PENDING_DELETE":
        log.warning("caldav conflict on %s: server wins; the wall's delete was "
                    "dropped because it was edited on another device", row["id"])
    else:
        log.warning("caldav conflict on %s: server wins; dropped losing local edit",
                    row["id"])


def _delete_orphaned_upload(client, conn, col, row, href: str, etag,
                            now_iso: str) -> None:
    """The wall deleted a reminder while its create was still uploading, so the
    local row is gone but the upload put a copy in iCloud. Queue that copy for
    delete first (so a failure here is retried, counted as pending, and can't
    be revived by a pull), then try the DELETE now. Raises on failure; the
    caller records it against the queued row.

    If the id is already in use again (re-added in the moment between), the
    new row takes over the uploaded copy instead: it becomes an update to that
    URL rather than a second create."""
    rev = fdb.queue_orphan_cal_delete(conn, row, href, etag, now_iso)
    if rev is None:
        # -1 matches no revision, so this only adopts href/etag, never SYNCED
        fdb.mark_cal_object_pushed(conn, row["id"], href, etag, -1)
        log.info("caldav %s was re-added during its upload; it takes over the "
                 "iCloud copy", row["id"])
        return
    client.delete_object(col, href, base_etag=etag)
    fdb.finish_cal_object_delete(conn, row["id"], rev)
    log.info("caldav %s was deleted during its upload; removed the iCloud copy",
             row["id"])


def _record_push_failure(conn, row, what: str, exc, now_iso: str,
                         errors: list) -> bool:
    """Record one failed push. A lasting refusal (_is_refusal) counts toward
    parking the row; anything else stays retryable. Returns True if the failure
    was a 401 (the caller surfaces needs_auth). Both are read from the status
    the error carries, never its text, so a URL like familyhub-chore-403-...
    can't make a network error look like either."""
    refused = _is_refusal(exc)
    parked = fdb.record_cal_object_error(conn, row["id"], f"{what}{exc}", now_iso,
                                         permanent=refused)
    if parked:
        errors.append(f"{row['id']}: iCloud refused this change; it is kept on "
                      f"the wall and not retried: {exc}")
        log.warning("caldav %s parked after %d refusals (kept on the wall, not "
                    "retried): %s", row["id"], fdb.CAL_PARK_ATTEMPTS, exc)
    else:
        errors.append(f"{row['id']}: {what}{exc}")
    return not refused and _http_status(exc) == 401


def flush_pending(client, conn, collections, now_iso: str,
                  budget_s: float = FLUSH_BUDGET_S, clock=time.monotonic) -> dict:
    """Push the outbox — locally-edited cal_objects (wall edits) — to iCloud: PUT
    creates/updates (conditional on If-Match/If-None-Match), DELETE removals. Only
    collections discovered this round are flushable; a row whose collection wasn't
    seen this pull records why and waits for the next (or is parked, if its list
    is gone from iCloud). Per-row failures are isolated and recorded (the row
    stays PENDING and retries next sync); a 412 is resolved server-wins; an auth
    failure is surfaced so the wall shows Reconnect. A write iCloud refuses for
    good (a 403 on a read-only list, another lasting 4xx) is parked after
    CAL_PARK_ATTEMPTS tries and is never read as a dead password.

    Stops once `budget_s` of `clock` time is spent; the rest waits for the next
    tick untouched (`deferred`). Returns {pushed, conflicts, errors, needs_auth,
    deferred}. Never raises: a bad push must not blank the calendar, same
    fails-soft rule as the read path."""
    col_by_id = {"caldav:" + c["id"]: c for c in collections}
    known = None
    pushed, conflicts, errors, needs_auth = 0, 0, [], False
    rows = fdb.caldav_pending(conn)
    start = clock()
    deferred = 0
    for i, row in enumerate(rows):
        if clock() - start >= budget_s:
            deferred = len(rows) - i
            log.warning("caldav push stopped at its time budget (%ss); %d "
                        "change(s) wait for the next sync", budget_s, deferred)
            break
        col = col_by_id.get(row["collection_id"])
        if col is None:
            if known is None:
                known = {c["id"] for c in fdb.list_caldav_collections(conn)}
            if row["collection_id"] not in known:
                # the list is gone from iCloud (dropped by the sync): nothing
                # can ever take this change, so park it rather than count it
                # as "not yet synced" forever. It stays, and is logged.
                fdb.park_cal_object(conn, row["id"], _GONE_REASON, now_iso)
                log.warning("caldav %s parked: %s", row["id"], _GONE_REASON)
                continue
            # not discovered this round: record why so a stuck row is VISIBLE
            # (per-row error + it keeps counting toward the pending backlog)
            # instead of silently optimistic. Retries next sync.
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
                # only drop the row if nothing re-created or edited it while
                # the DELETE was in flight; that change survives as a fresh
                # create
                if fdb.finish_cal_object_delete(
                        conn, row["id"], row["local_rev"]) == "superseded":
                    log.info("caldav %s changed during its delete; kept queued "
                             "as a new create", row["id"])
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
                outcome = fdb.mark_cal_object_pushed(
                    conn, row["id"], href, res.get("etag"), row["local_rev"])
                if outcome == "superseded":
                    # a wall edit landed mid-upload: it stays queued (built on
                    # the etag just earned) and goes out on the next flush
                    log.info("caldav %s changed during its upload; kept queued",
                             row["id"])
                elif outcome == "gone":
                    # deleted on the wall mid-upload: not a push, a cleanup
                    _delete_orphaned_upload(client, conn, col, row, href,
                                            res.get("etag"), now_iso)
                    continue
            pushed += 1
        except CalDavConflict as e:
            try:
                _resolve_conflict(client, conn, col, row, now_iso,
                                  conflict_href=e.args[0] if e.args else None)
                conflicts += 1
            except Exception as e:
                log.warning("caldav conflict-resolve failed for %s", row["id"],
                            exc_info=True)
                if _record_push_failure(conn, row, "conflict-resolve failed: ",
                                        e, now_iso, errors):
                    needs_auth = True
        except Exception as e:
            log.warning("caldav push failed for %s", row["id"], exc_info=True)
            if _record_push_failure(conn, row, "", e, now_iso, errors):
                needs_auth = True
    return {"pushed": pushed, "conflicts": conflicts, "errors": errors,
            "needs_auth": needs_auth, "deferred": deferred}


def _resolve_kinds(conn, discovered: list[dict]) -> list[dict]:
    """The discovered collections with every kind known. discover() reports
    comp=None when a collection's supported-component set could not be read;
    guessing VEVENT turned a reminder list into a calendar for a tick. Use the
    kind stored for it instead, or, for one never seen before, skip it this
    tick (logged); the next sync reads it again."""
    stored = None
    out = []
    for col in discovered:
        if "comp" in col and col["comp"] is None:
            if stored is None:
                stored = {c["id"]: c["comp_type"]
                          for c in fdb.list_caldav_collections(conn)}
            kind = stored.get("caldav:" + col["id"])
            if kind is None:
                log.warning("caldav %s: kind (calendar or reminders) unreadable "
                            "and not known yet; skipped this sync",
                            col.get("name") or col["id"])
                continue
            col = {**col, "comp": kind}
        out.append(col)
    return out


def _drop_gone_lists(conn, discovered: list[dict], now: dt.datetime) -> None:
    """Drop saved collections iCloud no longer returns (deleted or unshared).

    A saved collection missing from a NON-empty discover starts a clock in kv
    `caldav_missing_since` ({id: {"since", "last"}}: the first and the latest
    sighting); once its sightings span _LIST_GONE_HOURS it is dropped with its
    pulled objects. An empty discover is a blip and never counts, and a gap of
    more than _MISSING_GAP_HOURS since the last sighting (failed syncs between)
    starts the clock over. Unsent wall edits for a dropped list are kept and
    parked with _GONE_REASON (logged), so they stop counting as "not yet
    synced" but are not silently lost; they go out again if the list comes
    back."""
    if not discovered:
        return
    seen = {"caldav:" + c["id"] for c in discovered}
    missing = fdb.kv_get(conn, "caldav_missing_since") or {}
    now_iso = now.isoformat()
    kept = {}
    for col in fdb.list_caldav_collections(conn):
        cid = col["id"]
        if cid in seen:
            continue
        clock = missing.get(cid)
        since = last = None
        if isinstance(clock, dict):
            since, last = clock.get("since"), clock.get("last")
        try:
            since_dt = dt.datetime.fromisoformat(since) if since else None
            last_dt = dt.datetime.fromisoformat(last) if last else None
        except Exception:
            log.warning("caldav_missing_since for %s unparseable (%r); resetting",
                        cid, clock)
            since_dt = last_dt = None
        # No last sighting (a first sighting, or a clock the older version
        # saved as a bare time) or too long since it: start over.
        if since_dt is None or last_dt is None or \
                (now - last_dt).total_seconds() / 3600.0 > _MISSING_GAP_HOURS:
            if clock is not None and since_dt is not None:
                log.info("caldav %s: no sighting for a while (failed syncs); its "
                         "missing clock starts over", cid)
            since_dt = now
        age_h = (now - since_dt).total_seconds() / 3600.0
        if age_h < _LIST_GONE_HOURS:
            kept[cid] = {"since": since_dt.isoformat(), "last": now_iso}
            continue
        name = col.get("display_name") or cid
        unsent = fdb.drop_caldav_collection(conn, cid)
        for row in unsent:
            fdb.park_cal_object(conn, row["id"], _GONE_REASON, now_iso)
        log.warning("caldav %s (%s) missing from iCloud for %.0fh: dropped it and "
                    "its synced items; %d unsent wall change(s) kept and parked",
                    name, cid, age_h, len(unsent))
        mapped = [p["name"] for p in fdb.list_people(conn)
                  if p.get("reminder_list_id") == cid]
        if mapped:
            log.warning("caldav %s was the chore list of %s; their chores are not "
                        "mirrored until a new list is picked in settings",
                        name, ", ".join(mapped))
    fdb.kv_set(conn, "caldav_missing_since", kept)


def _parked_status(conn) -> dict:
    """Status fields for parked wall changes (fdb.CAL_PARK_ATTEMPTS): a count,
    and while any exist a plain note saying they are kept but not sent."""
    parked = fdb.caldav_parked(conn)
    if not parked:
        return {"parked": 0}
    n = len(parked)
    return {"parked": n,
            "parked_note": (f"{n} wall change{'s' if n != 1 else ''} iCloud "
                            "refused or can no longer take (its list is read-only "
                            "or gone); kept but not sent")}


def sync_once(client, conn, cfg, now: dt.datetime) -> dict:
    prior = fdb.kv_get(conn, "caldav_status") or {}

    def _status(**kw):
        st = {"last_sync": prior.get("last_sync"), **kw}
        # config states (not-configured / disabled) are never a hard error
        _apply_error_persistence(conn, st, now, hard_error=False)
        fdb.kv_set(conn, "caldav_status", st)
        return st

    try:
        if client is None or not client.configured():
            return _status(ok=False, error="not configured")
        if not fdb.integration_enabled(conn, "icloud_caldav", default=True):
            return _status(ok=False, error="disabled")

        lo_dt = now - dt.timedelta(days=getattr(cfg, "calendar_past_days", 45))
        hi_dt = now + dt.timedelta(days=cfg.calendar_window_days)
        discovered = client.discover()
        now_iso = now.isoformat()
        collections = _resolve_kinds(conn, discovered)
        # Persist every discovered collection (calendars + reminder lists) so the
        # settings calendar picker has a per-collection visibility toggle; upsert
        # keeps the operator's toggle across syncs. A saved one that stops being
        # discovered is dropped only after _LIST_GONE_HOURS (_drop_gone_lists).
        for col in collections:
            cid = "caldav:" + col["id"]
            fdb.upsert_caldav_collection(
                conn, cid, col.get("comp", "VEVENT"),
                col.get("name", ""), col.get("color"), now_iso)
            if col.get("comp") == "VTODO":
                # Chore reminders never sent before the list went away were
                # forgotten by the mirror meanwhile. It queues the days still
                # wanted again this tick (same ids, so never twice); sending
                # the old ones would bring past days back on the phone.
                from . import chore_mirror
                dropped = fdb.drop_untracked_parked_creates(
                    conn, cid, _GONE_REASON, chore_mirror.UID_PREFIX)
                if dropped:
                    log.info("caldav %s is back in iCloud; %d unsent chore "
                             "reminder(s) the mirror no longer tracks were "
                             "dropped (it re-queues the days still wanted)",
                             col.get("name") or cid, dropped)
                if fdb.unpark_cal_objects(conn, cid, _GONE_REASON):
                    log.info("caldav %s is back in iCloud; its parked wall "
                             "changes will be sent again", col.get("name") or cid)
        _drop_gone_lists(conn, discovered, now)
        # Nothing reads VEVENT objects from the store (events render from the
        # events table, and only reminders are written back), so the pull no
        # longer saves them; this clears the rows an older version left.
        fdb.delete_synced_cal_objects(conn, "VEVENT")

        events: list[dict] = []
        errors: list[str] = []
        failed: list[str] = []
        needs_auth = False
        for col in (c for c in collections if c.get("comp", "VEVENT") == "VEVENT"):
            cal_id = "caldav:" + col["id"]
            n_objs = n_skipped = 0
            try:
                for obj in client.fetch_ics(col, lo_dt.date(), hi_dt.date()):
                    n_objs += 1
                    try:
                        events.extend(ics_events(
                            obj["ics"], cal_id, lo_dt.date(), hi_dt.date(),
                            now.tzinfo))
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
            except Exception as e:  # isolate one bad collection
                errors.append(f"{col.get('name') or cal_id}: {e}")
                failed.append(cal_id)
                needs_auth = needs_auth or _is_auth_error(e)

        # Reminders lists (VTODO) -> cal_objects, which the wall renders from
        # (app._visible_reminders). Unchanged objects (same ETag) are skipped
        # without a parse or a write, and a list's changes are saved in
        # batches, not one commit per reminder.
        vtodo_failed: list[str] = []
        for col in (c for c in collections if c.get("comp") == "VTODO"):
            list_id = "caldav:" + col["id"]
            seen_todos: set = set()
            n_todos = n_failed = n_writes = 0
            try:
                todos = client.fetch_todos(col)
                index = fdb.cal_object_index(conn, list_id)
                for obj in todos:
                    n_todos += 1
                    outcome = _store_object(conn, list_id, "VTODO", obj,
                                            seen_todos, index)
                    if outcome == "failed":
                        # Skip one unparseable reminder rather than failing the
                        # whole list and freezing the rest behind an error.
                        n_failed += 1
                    elif outcome == "stored":
                        n_writes += 1
                        if n_writes % _PULL_BATCH == 0:
                            conn.commit()
                if n_todos and n_failed == n_todos:
                    # Every todo failed to parse -> systematic break; flag the list
                    # (keeps its cache) rather than report it healthy-but-empty.
                    raise RuntimeError(f"parsed 0 of {n_todos} todos")
                if seen_todos:
                    fdb.prune_cal_objects(conn, list_id, seen_todos)
            except Exception as e:
                errors.append(f"{col.get('name') or list_id}: {e}")
                vtodo_failed.append(list_id)
                needs_auth = needs_auth or _is_auth_error(e)
            finally:
                conn.commit()        # what parsed before a failure is still good

        # Valid-but-empty guard (mirrors the Google/ICS path): a VEVENT
        # collection that synced ZERO events this round but HAD cached events is
        # kept, bounded by a per-collection TTL, so an iCloud maintenance blip or
        # an empty discover() doesn't silently blank the family's calendar and
        # report success.
        attempted = {"caldav:" + c["id"] for c in collections
                     if c.get("comp", "VEVENT") == "VEVENT"}
        # Only cached rows INSIDE the new window make an empty answer suspicious
        # (calendar_sync.cached_ids_in_window): a calendar whose last event
        # aged out of the lookback returns nothing honestly.
        synced_ids = {e["calendar_id"] for e in events}
        cached_ids = {cid for cid in fdb.event_calendar_ids(conn)
                      if cid.startswith("caldav:")}
        in_window = cached_ids_in_window(conn, lo_dt.date(), hi_dt.date())
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
                if cid not in in_window:
                    # genuinely empty: never had rows, or none left in the window
                    empty_since.pop(cid, None)
                    continue
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
                        # Accepting the wipe drops this collection's cached rows
                        # and deliberately appends no error, so status stays ok
                        # and no banner fires. Log it: this is the one path that
                        # silently empties a calendar the wall still reports a
                        # synced window over.
                        log.warning(
                            "%s: empty for %.0fh (>= %sh) — accepting the wipe "
                            "and dropping its cached events",
                            cid, age_h, _EMPTY_KEEP_HOURS)
                        empty_since.pop(cid, None)   # kept long enough -> accept wipe
                        continue
                suspicious.append(cid)
                errors.append(f"{cid}: returned no events (kept last-synced)")
        empty_since = {k: v for k, v in empty_since.items() if k in attempted}
        fdb.kv_set(conn, "caldav_empty_since", empty_since)

        # Reminders keep last-good on a per-list fetch failure or an empty
        # discover without any work here: a list's rows are only pruned after
        # it pulled cleanly. (The whole-list kv copy "caldav_reminders" that
        # was rewritten every tick is gone; nothing read it. Emptied once.)
        if fdb.kv_get(conn, "caldav_reminders") is not None:
            fdb.kv_set(conn, "caldav_reminders", None)
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
            # Project the wall's chore plan into each mapped person's list FIRST
            # (queues creates/deletes into the outbox), so this same flush pushes
            # them. Runs after the pull so its prune sees fresh completion state.
            from . import chore_mirror
            # iOS check-offs -> local completions (streaks) first, then project
            # the plan (which keeps completed reminders and prunes stale ones).
            chore_mirror.reconcile_completions(conn, now)
            # only trust prune decisions for VTODO lists that pulled OK this tick
            synced_vtodo = {"caldav:" + c["id"] for c in collections
                            if c.get("comp") == "VTODO"} - set(vtodo_failed)
            # reconcile() never raises — it returns {"error": True} when the
            # whole tick blew up. Discarding that made a mirror that died every
            # tick invisible (the calendar badge stayed 'ok' forever), so the
            # result is recorded for /api/admin/state and the settings row.
            mres = chore_mirror.reconcile(conn, cfg, now,
                                          synced_collections=synced_vtodo)
            mirror_status = {"ok": not mres.get("error"), "at": now.isoformat(),
                             "created": mres.get("created", 0),
                             "moved": mres.get("moved", 0),
                             "updated": mres.get("updated", 0),
                             "deleted": mres.get("deleted", 0),
                             # people left out: their chore list is gone
                             "lists_gone": mres.get("lists_gone", [])}
            if mres.get("error"):
                log.error("chore mirror reconcile reported a failed tick")
            fdb.kv_set(conn, "chore_mirror_status", mirror_status)
            flushed = flush_pending(client, conn, collections, now.isoformat())
            errors.extend(flushed["errors"])
            needs_auth = needs_auth or flushed["needs_auth"]
        else:
            # Two-way is off this tick, so the mirror didn't run — but an
            # error latched from an earlier two-way tick must not haunt the
            # settings row forever with no path to clear it. If the operator
            # switched back to read-only, quiet the chip now.
            prior_mirror = fdb.kv_get(conn, "chore_mirror_status") or {}
            if prior_mirror.get("ok") is False:
                fdb.kv_set(conn, "chore_mirror_status",
                          {"ok": True, "disabled": True, "at": now.isoformat()})

        st = {"ok": not errors, "last_sync": now.isoformat(),
              "events": len(events),
              "reminders": fdb.count_open_vtodo_objects(conn),
              # outbox backlog after this flush: un-pushed wall edits still queued
              # (0 in the normal case). Non-zero + not moving => something stuck;
              # the settings menu can surface it instead of it being invisible.
              "pending": len(fdb.caldav_pending(conn)),
              **_parked_status(conn)}
        # people whose chore list is gone: left out of the mirror until a new
        # list is picked (named here so it is not only a log line)
        from . import chore_mirror
        st["lists_gone"] = [p["name"]
                            for p in chore_mirror.people_with_gone_lists(conn)]
        if errors:
            st["error"] = "; ".join(errors)
        # Only FETCH-scope failures may hold coverage back. `errors` also
        # collects outbox-push and reminder-list problems, which say nothing
        # about how far the event fetch reached: gating on it would let a stuck
        # chore-mirror push freeze the window forever, and — before any clean
        # pass — leave no record at all, hatching the whole wall on an install
        # whose calendars pulled fine. An empty `collections` means nothing was
        # fetched from anything, so it can never evidence coverage either.
        if collections and not (failed or suspicious):
            fdb.kv_set(conn, "caldav_covered",
                       {"from": lo_dt.date().isoformat(),
                        "to": hi_dt.date().isoformat()})
        if needs_auth:
            st["needs_auth"] = True
        # Only a genuine fetch/discover EXCEPTION (a collection or reminder list
        # that raised) feeds the sustained clock — not the empty-window / discover
        # "kept last-synced" soft states, which keep the cache and aren't a stuck
        # feed. Push (outbox) failures surface via `pending` and `parked`.
        _apply_error_persistence(conn, st, now,
                                 hard_error=bool(failed or vtodo_failed))
        fdb.kv_set(conn, "caldav_status", st)
        return st
    except Exception as e:      # never kill the sync thread; keep the cache
        log.exception("caldav sync_once failed")
        # An auth failure (revoked/expired app password) from discovery is a
        # first-class state: flag needs_auth, keep serving the cached view.
        st = {"ok": False, "error": str(e), "last_sync": prior.get("last_sync"),
              # carry the outbox depth even on a failed sync — a queued wall edit
              # is most worth surfacing exactly when syncing is broken.
              "pending": len(fdb.caldav_pending(conn)),
              **_parked_status(conn)}
        if _is_auth_error(e):
            st["needs_auth"] = True
        # reaching here means the whole sync raised — a hard error (unless it was
        # auth, which _apply_error_persistence excludes)
        _apply_error_persistence(conn, st, now, hard_error=True)
        fdb.kv_set(conn, "caldav_status", st)
        return st
