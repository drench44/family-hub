"""Chore mirror (P3): project the wall's chore plan into each mapped person's
iCloud reminder list, so chores show up natively on every iPhone — the Reminders
app, Siri, and (via due-times) notifications. The wall stays the source of truth;
this only pushes a rolling window of it.

Runs inside the CalDAV sync tick, gated on two-way (readonly=False). It queues
creates/deletes into the existing cal_objects outbox (flush_pending pushes them
with If-Match/server-wins) and records each occurrence in the chore_mirror ledger,
so the next pass reconciles rotation hand-offs and prunes days that fell out of
the window. Never raises — a mirror hiccup must not disrupt the calendar sync.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from . import chores as chlogic
from . import db as fdb
from . import reminders as remlogic

log = logging.getLogger("family_hub.caldav")

DEFAULT_HORIZON_DAYS = 7


def _title(chore: dict) -> str:
    icon = (chore.get("icon") or "").strip()
    return f"{icon} {chore['title']}".strip() if icon else chore["title"]


def _sig(chore: dict) -> str:
    """A content signature — what an edit would change in the reminder (title,
    icon, times). reconcile re-pushes when it drifts from the mirrored copy."""
    payload = [_title(chore), sorted(chore.get("due_times") or [])]
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _queue_create(conn, chore, diso, person_id, list_id, tz, now, now_iso) -> None:
    d = dt.date.fromisoformat(diso)
    uid = f"familyhub-chore-{chore['id']}-{diso}"
    oid = f"{list_id}/{uid}"
    title = _title(chore)
    ics = remlogic.build_chore_vtodo(uid, title, d,
                                     chore.get("due_times") or [], now, tz=tz)
    # An occurrence that is ALREADY done on the wall is (re)created completed.
    # Without this, re-desiring a pruned-then-restored occurrence (an away edit,
    # a reactivated chore) reopens a finished reminder on someone's phone and
    # nags them for work they did. queue_cal_object_create keeps the object's
    # existing server identity, so this lands as an update in place.
    try:
        if fdb.completion_exists(conn, chore["id"], diso):
            ics = remlogic.set_completed(ics, True, now)
    except Exception:
        log.warning("chore mirror could not pre-complete %s; creating it open",
                    oid, exc_info=True)
    fdb.queue_cal_object_create(conn, {
        "id": oid, "collection_id": list_id, "comp_type": "VTODO",
        "uid": uid, "summary": title, "raw_ics": ics}, now_iso)
    fdb.upsert_chore_mirror(conn, chore["id"], diso, person_id, oid, uid, _sig(chore))


def push_completion(conn, chore_id: int, date: str, completed: bool,
                    expected_person_id: int | None = None) -> bool:
    """Reflect a WALL completion/uncompletion onto the mirrored iCloud reminder
    (P4, direction wall -> iOS), via the outbox. No-op (False) when the occurrence
    isn't mirrored or its object is gone. Safe to call unconditionally from the
    chore complete/uncomplete endpoints.

    ``expected_person_id`` is who the caller just resolved as owning the day. The
    mirror ledger is only re-keyed on the next sync tick, so between an away/back
    change and that tick its row can still name the OTHER person: pushing onto it
    would mark the chore done on the away person's phone while the person who
    actually did it keeps getting nagged. Mismatch -> no-op with a warning; the
    next reconcile's moved branch relocates the (still open) reminder correctly."""
    m = fdb.get_chore_mirror(conn, chore_id, date)
    if not m:
        return False
    if expected_person_id is not None and m["person_id"] != expected_person_id:
        log.warning("chore mirror push_completion skipped: stale ledger row for "
                    "chore %s on %s (ledger person %s, resolved %s)",
                    chore_id, date, m["person_id"], expected_person_id)
        return False
    obj = fdb.get_cal_object(conn, m["cal_object_id"])
    if not obj or not obj.get("raw_ics"):
        return False
    now = dt.datetime.now(dt.timezone.utc)
    try:
        ics = remlogic.set_completed(obj["raw_ics"], completed, now)
    except Exception:
        log.warning("chore mirror push_completion skipped for %s",
                    m["cal_object_id"], exc_info=True)
        return False
    return fdb.queue_cal_object_update(
        conn, m["cal_object_id"], ics, obj.get("summary", ""), now.isoformat())


class _OwnerResolver:
    """Who owns (chore, date) RIGHT NOW, for crediting an iOS check-off.

    The frozen occurrence_log wins when it exists — it is the record of what the
    wall actually asked of whom. Otherwise the day is re-resolved live through
    the away overlay, exactly as the wall and /complete resolve it. The mirror
    ledger is only the last resort: it is written by the previous reconcile, so
    after an away/back change it can still name the wrong person until the next
    tick re-keys it (and reconcile_completions runs BEFORE that in the sync).

    Everything is lazy and fails soft: a resolver that can't read the DB simply
    falls back to the ledger rather than losing the completion."""

    def __init__(self, conn, dates):
        self._conn = conn
        self._dates = sorted(dates)
        self._plans: dict[str, dict] = {}
        self._amap = None
        self._people = None
        self._chores = None
        self._live_ok = True

    def _live(self, diso: str) -> dict:
        if diso in self._plans:
            return self._plans[diso]
        plan: dict = {}
        if self._live_ok:
            try:
                if self._amap is None:
                    self._amap = fdb.away_map(self._conn, self._dates[0],
                                              self._dates[-1])
                    self._people = fdb.list_people(self._conn)
                    self._chores = fdb.list_chores(self._conn)
                view = chlogic.away_view_on(self._amap, diso)
                rows = chlogic.plan_rows(self._chores, self._people,
                                         dt.date.fromisoformat(diso), view)
                plan = {r["chore_id"]: r["person_id"] for r in rows}
            except Exception:
                self._live_ok = False
                log.warning("chore completion reconcile: live owner resolve "
                            "failed; falling back to the ledger", exc_info=True)
        self._plans[diso] = plan
        return plan

    def owner_of(self, chore_id: int, diso: str, ledger_person_id: int) -> int:
        row = fdb.log_row(self._conn, chore_id, diso)
        if row is not None:
            return row["person_id"]
        return self._live(diso).get(chore_id, ledger_person_id)


def reconcile_completions(conn, now: dt.datetime) -> int:
    """iOS check-offs -> local completions (P4, direction iOS -> wall). ADD-ONLY:
    a reminder marked done in iOS records the local completion (so streaks stay
    right); reopening in iOS does NOT un-complete on the wall. Add-only is
    deliberate — removing here could undo a wall completion that hasn't pushed
    yet. Un-completing stays a wall action (which pushes back via push_completion).
    Returns the number of completions newly recorded. Never raises.

    The completion is credited to whoever owns the day NOW (_OwnerResolver), not
    to the ledger row's person: this runs BEFORE reconcile in the sync tick, so
    the ledger can still name the person an away/back change has since moved the
    chore away from."""
    try:
        rows = fdb.list_chore_mirror(conn)
    except Exception:
        log.exception("chore completion reconcile: list failed (non-fatal)")
        return 0
    added = 0
    if not rows:
        return 0
    owners = _OwnerResolver(conn, {m["date"] for m in rows})
    for m in rows:
        try:      # isolate per row so one poison object can't stall all streaks
            obj = fdb.get_cal_object(conn, m["cal_object_id"])
            if not obj or "STATUS:COMPLETED" not in (obj.get("raw_ics") or ""):
                continue
            if not fdb.completion_exists(conn, m["chore_id"], m["date"]):
                pid = owners.owner_of(m["chore_id"], m["date"], m["person_id"])
                fdb.set_completion(conn, m["chore_id"], m["date"], pid)
                added += 1
        except Exception:
            log.warning("chore completion reconcile skipped: %s",
                        m.get("uid"), exc_info=True)
    return added


def reconcile(conn, cfg, now: dt.datetime, synced_collections=None) -> dict:
    """Bring each mapped person's iCloud list in line with the wall's chore plan
    over [today, today+H]. No past day: the wall FREEZES history in occurrence_log,
    so recomputing yesterday's live plan could rewrite a reminder against a plan
    the wall no longer shows — the completions-before-prune ordering already
    catches a late completion. `synced_collections` (when given) is the set of
    VTODO list ids that pulled OK this tick; the prune only trusts a list's state
    when it actually synced, so a per-list outage can't delete a reminder and lose
    an iOS completion. Returns {created, moved, updated, deleted}. Never raises;
    per-occurrence failures are isolated so one poison row can't stall the rest."""
    zero = {"created": 0, "moved": 0, "updated": 0, "deleted": 0}
    try:
        mapped = {p["id"]: p["reminder_list_id"]
                  for p in fdb.list_people(conn) if p.get("reminder_list_id")}
        existing_rows = fdb.list_chore_mirror(conn)
        # Nothing mapped AND nothing already mirrored -> no work. But if rows
        # exist while nothing is mapped (every mapped person was deleted /
        # unmapped), we must still fall through to PRUNE them — otherwise their
        # reminders orphan in iCloud forever.
        if not mapped and not existing_rows:
            return dict(zero)
        tz = now.tzinfo             # wall zone (None in tests -> all-day fallback)
        now_iso = now.isoformat()
        today = now.date()
        H = getattr(cfg, "chore_mirror_horizon_days", DEFAULT_HORIZON_DAYS)
        all_people = fdb.list_people(conn)       # ALL active -> correct rotation
        chores = {c["id"]: c for c in fdb.list_chores(conn)}
        chores_list = list(chores.values())

        # Away/pause overlay over the whole horizon: the mirror mirrors what
        # the WALL shows. An away person's paused chore must not ring their
        # phone; a covered chore belongs on the BACKUP's list (and its ledger
        # row under the backup, so an iOS check-off credits the backup's
        # streak — the same rule the /complete endpoint enforces). If this
        # read throws, the outer except skips the tick entirely: no work is
        # safer than an away-blind pass that would bounce reminders back to
        # the away person.
        amap = fdb.away_map(conn, today.isoformat(),
                            (today + dt.timedelta(days=H)).isoformat())

        desired: dict = {}
        for i in range(0, H + 1):
            d = today + dt.timedelta(days=i)
            view = chlogic.away_view_on(amap, d.isoformat())
            for row in chlogic.plan_rows(chores_list, all_people, d, view):
                lid = mapped.get(row["person_id"])
                if lid:
                    desired[(row["chore_id"], d.isoformat())] = (row["person_id"], lid)

        existing = {(m["chore_id"], m["date"]): m for m in existing_rows}
        created = moved = updated = deleted = 0

        for (cid, diso), (pid, lid) in desired.items():
            try:
                chore = chores.get(cid)
                if chore is None:
                    continue
                cur = existing.get((cid, diso))
                obj = fdb.get_cal_object(conn, cur["cal_object_id"]) if cur else None
                completed = bool(obj and "STATUS:COMPLETED" in (obj.get("raw_ics") or ""))
                if cur is None or obj is None:
                    # not mirrored, or its object vanished (deleted in iOS / a
                    # conflict dropped the row) -> (re)create; the wall is source
                    # of truth. Clear an orphaned ledger row first.
                    if cur is not None:
                        fdb.delete_chore_mirror(conn, cid, diso)
                    _queue_create(conn, chore, diso, pid, lid, tz, now, now_iso)
                    created += 1
                elif cur["person_id"] != pid:     # rotation handed off to another person
                    if not completed:             # never delete a DONE reminder (history)
                        fdb.queue_cal_object_delete(conn, cur["cal_object_id"], now_iso)
                    fdb.delete_chore_mirror(conn, cid, diso)
                    _queue_create(conn, chore, diso, pid, lid, tz, now, now_iso)
                    moved += 1
                elif cur.get("sig") != _sig(chore) and not completed:
                    # the chore's title/icon/times were edited -> refresh in place.
                    # A completed occurrence is left alone (its stale sig lets a
                    # later reopen re-sync).
                    d = dt.date.fromisoformat(diso)
                    ics = remlogic.build_chore_vtodo(
                        cur["uid"], _title(chore), d,
                        chore.get("due_times") or [], now, tz=tz)
                    fdb.queue_cal_object_update(
                        conn, cur["cal_object_id"], ics, _title(chore), now_iso)
                    fdb.upsert_chore_mirror(conn, cid, diso, pid, cur["cal_object_id"],
                                            cur["uid"], _sig(chore))
                    updated += 1
            except Exception:
                log.warning("chore mirror occurrence skipped: %s %s",
                            cid, diso, exc_info=True)

        # prune occurrences that fell out of the window (past, chore deleted /
        # deactivated). Never delete a COMPLETED reminder — that's history; just
        # forget the ledger row. Skip lists that didn't sync this tick, so a stale
        # local copy can't drive a delete that loses an iOS completion.
        for (cid, diso), m in existing.items():
            if (cid, diso) in desired:
                continue
            try:
                coll = m["cal_object_id"].split("/", 1)[0]
                if synced_collections is not None and coll not in synced_collections:
                    continue
                obj = fdb.get_cal_object(conn, m["cal_object_id"])
                if not (obj and "STATUS:COMPLETED" in (obj.get("raw_ics") or "")):
                    fdb.queue_cal_object_delete(conn, m["cal_object_id"], now_iso)
                fdb.delete_chore_mirror(conn, cid, diso)
                deleted += 1
            except Exception:
                log.warning("chore mirror prune skipped: %s %s",
                            cid, diso, exc_info=True)

        return {"created": created, "moved": moved, "updated": updated,
                "deleted": deleted}
    except Exception:
        log.exception("chore mirror reconcile failed (non-fatal)")
        return {**zero, "error": True}
