import datetime as dt
import logging
from types import SimpleNamespace

from family_hub import caldav_service, caldav_sync
from family_hub import db as fdb
from family_hub import reminders as remlogic


def _ics(uid, summary, start, end):
    """A minimal all-day VEVENT (VALUE=DATE), the shape iCloud returns per
    object; ics_events parses + expands it exactly like a public ICS feed."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{summary}\r\n"
        f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n")


class FakeCalDav:
    """A stand-in for caldav_service.CalDavClient — canned collections + ICS, no
    network. Matches the injected-client shape caldav_sync depends on."""
    def __init__(self, collections):
        self._cols = collections

    def configured(self):
        return True

    def discover(self):
        return [{"id": c["id"], "name": c["name"],
                 "comp": c.get("comp", "VEVENT"), "color": c.get("color")}
                for c in self._cols]

    def fetch_ics(self, collection, lo, hi):
        col = next(c for c in self._cols if c["id"] == collection["id"])
        return [{"href": f"h/{collection['id']}/{i}", "etag": f"e{i}", "ics": s}
                for i, s in enumerate(col.get("ics", []))]

    def fetch_todos(self, collection):
        col = next(c for c in self._cols if c["id"] == collection["id"])
        return [{"href": f"h/{collection['id']}/{i}", "etag": f"e{i}", "ics": s}
                for i, s in enumerate(col.get("todos", []))]


_CFG = SimpleNamespace(calendar_window_days=28, calendar_past_days=45)
_NOW = dt.datetime(2026, 8, 17, 12, 0, 0)


def test_caldav_sync_pulls_events_and_records_collection_colors(conn):
    client = FakeCalDav([
        {"id": "abc", "name": "Family", "color": "#FF0000", "comp": "VEVENT",
         "ics": [_ics("u1", "Dentist", "20260820", "20260821")]},
        {"id": "rem", "name": "Reminders", "comp": "VTODO", "ics": []},  # ignored
    ])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["ok"] is True and st["events"] == 1
    rows = fdb.list_events(conn)
    assert len(rows) == 1
    assert rows[0]["calendar_id"] == "caldav:abc" and rows[0]["title"] == "Dentist"
    cols = {col["id"]: col for col in fdb.list_caldav_collections(conn)}
    assert cols["caldav:abc"]["display_name"] == "Family"
    assert cols["caldav:abc"]["color"] == "#FF0000"
    assert cols["caldav:abc"]["enabled"] is True


def test_caldav_sync_records_the_span_it_actually_covered(conn):
    """Same contract as the Google sync: the window /api/calendar reports must
    follow real coverage, so record what this pass actually pulled."""
    client = FakeCalDav([
        {"id": "abc", "name": "Family", "comp": "VEVENT",
         "ics": [_ics("u1", "Dentist", "20260820", "20260821")]},
    ])
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    cov = fdb.kv_get(conn, "caldav_covered")
    assert cov["to"] == (_NOW.date() + dt.timedelta(days=28)).isoformat()
    assert cov["from"] == (_NOW.date() - dt.timedelta(days=45)).isoformat()


def test_caldav_sync_with_a_failing_collection_does_not_advance_coverage(conn):
    """A collection that raised keeps its old rows, which cover only the old
    span; the recorded coverage must not move past what was really fetched."""
    class _BoomCalDav(FakeCalDav):
        def fetch_ics(self, collection, lo, hi):
            raise RuntimeError("iCloud said no")

    client = _BoomCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT", "ics": []}])
    prior = {"from": "2026-08-01", "to": "2026-08-20"}
    fdb.kv_set(conn, "caldav_covered", prior)
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["ok"] is False
    assert fdb.kv_get(conn, "caldav_covered") == prior


def test_caldav_records_coverage_even_when_a_reminder_list_fails(conn):
    """Coverage records how far the EVENT fetch reached. A VTODO (reminders) list
    that failed says nothing about that, and freezing coverage on it would hatch
    a calendar that pulled its full window perfectly — on a first run, the whole
    wall."""
    class _TodoBoom(FakeCalDav):
        def fetch_todos(self, collection):
            raise RuntimeError("reminders unavailable")

    client = _TodoBoom([
        {"id": "abc", "name": "Family", "comp": "VEVENT",
         "ics": [_ics("u1", "Dentist", "20260820", "20260821")]},
        {"id": "rem", "name": "Reminders", "comp": "VTODO"},
    ])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["ok"] is False, "the reminder failure is still reported"
    cov = fdb.kv_get(conn, "caldav_covered")
    assert cov is not None, "the event fetch succeeded; coverage must advance"
    assert cov["to"] == (_NOW.date() + dt.timedelta(days=28)).isoformat()


def test_caldav_empty_discover_records_no_coverage(conn):
    """Discover returning nothing means nothing was fetched from anything, which
    is not evidence of coverage — recording it would report days as synced that
    nobody ever asked iCloud about."""
    st = caldav_sync.sync_once(FakeCalDav([]), conn, _CFG, _NOW)
    assert fdb.kv_get(conn, "caldav_covered") is None
    assert st is not None


def _one_event_client():
    return FakeCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT",
                        "ics": [_ics("u1", "Dentist", "20260820", "20260821")]}])


def test_caldav_suspicious_empty_does_not_advance_coverage(conn):
    """A collection that returns nothing while holding cached rows is kept back
    (suspicious), so its rows still cover only the OLD span. Gating coverage on
    `failed` alone would march the window forward over days nobody re-fetched."""
    caldav_sync.sync_once(_one_event_client(), conn, _CFG, _NOW)
    first = fdb.kv_get(conn, "caldav_covered")
    assert first is not None

    later = _NOW + dt.timedelta(days=10)
    empty = FakeCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT", "ics": []}])
    st = caldav_sync.sync_once(empty, conn, _CFG, later)
    assert st["ok"] is False, "a suspicious empty is reported"
    assert fdb.kv_get(conn, "caldav_covered") == first, \
        "coverage must not advance over a collection that was kept back"


def test_caldav_logs_when_it_accepts_an_empty_wipe(conn, caplog):
    """The twin of the Google-side guard: this wipe appends no error, so status
    stays ok and no banner fires. The log line is the only signal the collection's
    cached events were just dropped."""
    caldav_sync.sync_once(_one_event_client(), conn, _CFG, _NOW)
    assert fdb.list_events(conn), "seeded rows to be wiped"
    # it first went empty 25h ago, past the keep window
    fdb.kv_set(conn, "caldav_empty_since",
               {"caldav:abc": (_NOW - dt.timedelta(hours=25)).isoformat()})
    empty = FakeCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT", "ics": []}])
    with caplog.at_level(logging.WARNING):
        caldav_sync.sync_once(empty, conn, _CFG, _NOW)
    assert "accepting the wipe" in "\n".join(r.getMessage() for r in caplog.records)


def test_caldav_empty_wipe_boundary_is_inclusive(conn):
    """Exactly _EMPTY_KEEP_HOURS of emptiness accepts the wipe (>=), not one tick
    more. Pre-existing behavior, previously only tested at 25h against a 24h
    window, so the boundary itself was free to drift."""
    caldav_sync.sync_once(_one_event_client(), conn, _CFG, _NOW)
    assert fdb.list_events(conn), "seeded rows to be wiped"
    fdb.kv_set(conn, "caldav_empty_since",
               {"caldav:abc": (_NOW - dt.timedelta(
                   hours=caldav_sync._EMPTY_KEEP_HOURS)).isoformat()})
    empty = FakeCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT", "ics": []}])
    caldav_sync.sync_once(empty, conn, _CFG, _NOW)
    assert fdb.list_events(conn) == [], "at exactly the TTL the wipe is accepted"


def test_caldav_client_sends_a_request_timeout(monkeypatch):
    """Every DAV call needs a ceiling. Without one a REPORT iCloud never answers
    hangs the single sync thread — calendar, chore mirror and outbox flush all
    stall, nothing is logged, and caldav_status still shows its last healthy
    sync (the issue #32 freeze class). The event search spans the whole
    calendar window, so a wider window makes a slow answer likelier."""
    import caldav
    seen = {}

    class _FakeDav:
        def __init__(self, **kw):
            seen.update(kw)

        def principal(self):
            return "P"

    monkeypatch.setattr(caldav, "DAVClient", _FakeDav)
    client = caldav_service.CalDavClient(username="u", password="p",
                                         url="https://example.invalid/")
    assert client._principal_obj() == "P"
    assert seen.get("timeout") == caldav_service.CalDavClient.DAV_TIMEOUT_S
    assert isinstance(seen["timeout"], (int, float)) and seen["timeout"] > 0


def test_caldav_sync_skips_when_unconfigured(conn):
    st = caldav_sync.sync_once(None, conn, _CFG, _NOW)
    assert st["ok"] is False and st["error"] == "not configured"
    assert fdb.list_events(conn) == []


def test_caldav_sync_skips_when_integration_disabled(conn):
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_enabled(conn, "icloud_caldav", False)
    client = FakeCalDav([
        {"id": "abc", "name": "F", "comp": "VEVENT",
         "ics": [_ics("u1", "X", "20260820", "20260821")]}])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["error"] == "disabled" and fdb.list_events(conn) == []


def test_caldav_sync_isolates_a_failing_collection(conn):
    class Flaky(FakeCalDav):
        def fetch_ics(self, collection, lo, hi):
            if collection["id"] == "bad":
                raise RuntimeError("boom")
            return super().fetch_ics(collection, lo, hi)
    client = Flaky([
        {"id": "good", "name": "Good", "comp": "VEVENT",
         "ics": [_ics("u1", "Kept", "20260820", "20260821")]},
        {"id": "bad", "name": "Bad", "comp": "VEVENT", "ics": []},
    ])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["ok"] is False and "Bad" in st["error"]      # bad flagged
    assert [r["title"] for r in fdb.list_events(conn)] == ["Kept"]  # good kept


def test_replace_events_source_isolation(conn):
    g = {"id": "g1", "calendar_id": "gcal", "title": "G",
         "start_ts": "2026-08-20", "end_ts": "2026-08-21", "all_day": 1}
    c = {"id": "c1", "calendar_id": "caldav:x", "title": "C",
         "start_ts": "2026-08-20", "end_ts": "2026-08-21", "all_day": 1}
    fdb.replace_events(conn, [g])
    fdb.replace_events_caldav(conn, [c])
    assert {r["calendar_id"] for r in fdb.list_events(conn)} == {"gcal", "caldav:x"}
    # a Google/ICS replace never deletes CalDAV rows, and vice versa
    fdb.replace_events(conn, [])
    assert {r["calendar_id"] for r in fdb.list_events(conn)} == {"caldav:x"}
    fdb.replace_events(conn, [g])
    fdb.replace_events_caldav(conn, [])
    assert {r["calendar_id"] for r in fdb.list_events(conn)} == {"gcal"}


def test_caldav_service_configured_and_client_from_env():
    assert caldav_service.configured({}) is False
    env = {"ICLOUD_CALDAV_USER": "bot@icloud.com",
           "ICLOUD_CALDAV_APP_PASSWORD": "abcd-efgh"}
    assert caldav_service.configured(env) is True
    assert caldav_service.client_from_env({}) is None
    client = caldav_service.client_from_env(env)
    assert client is not None and client.configured() is True
    assert client.url == caldav_service.ICLOUD_CALDAV_URL


_VTODO = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\n"
          "SUMMARY:Buy milk\r\nDUE;VALUE=DATE:20260820\r\nEND:VTODO\r\n"
          "END:VCALENDAR\r\n")


def test_caldav_sync_pulls_reminders(conn):
    client = FakeCalDav([
        {"id": "cal", "name": "Family", "comp": "VEVENT",
         "ics": [_ics("u1", "Dentist", "20260820", "20260821")]},
        {"id": "rem", "name": "Groceries", "comp": "VTODO", "todos": [_VTODO]},
    ])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["reminders"] == 1 and st["events"] == 1
    rems = fdb.kv_get(conn, "caldav_reminders")
    assert [r["title"] for r in rems] == ["Buy milk"]
    assert rems[0]["list_name"] == "Groceries"


def test_is_auth_error_detects_401_and_ignores_transient():
    assert caldav_sync._is_auth_error(RuntimeError("HTTP 401 Unauthorized")) is True
    assert caldav_sync._is_auth_error(RuntimeError("connection reset")) is False


def test_is_auth_error_detects_403_and_forbidden():
    # iCloud answers a dead app password with 403 on some paths, not only 401;
    # both must flag needs_auth so the wall prompts a reconnect (not a generic
    # "snag" that nobody acts on).
    assert caldav_sync._is_auth_error(RuntimeError("HTTP 403 Forbidden")) is True
    assert caldav_sync._is_auth_error(RuntimeError("403")) is True

    class ForbiddenError(Exception):
        pass
    assert caldav_sync._is_auth_error(ForbiddenError("nope")) is True
    # a bare id containing 403 must NOT false-positive (word-boundary guard)
    assert caldav_sync._is_auth_error(RuntimeError("event room4030")) is False
    # other 4xx/5xx are NOT auth — guards the 40[13] class from widening to 4xx
    assert caldav_sync._is_auth_error(RuntimeError("HTTP 404 Not Found")) is False
    assert caldav_sync._is_auth_error(RuntimeError("HTTP 500 Server Error")) is False


def test_caldav_sync_flags_needs_auth_and_keeps_cache(conn):
    # a previously-cached CalDAV event; an expired login must NOT wipe it
    fdb.replace_events_caldav(conn, [{"id": "old", "calendar_id": "caldav:cal",
        "title": "Kept", "start_ts": "2026-08-20", "end_ts": "2026-08-21",
        "all_day": 1}])

    class AuthFail:
        def configured(self):
            return True

        def discover(self):
            raise RuntimeError("401 Unauthorized")

    st = caldav_sync.sync_once(AuthFail(), conn, _CFG, _NOW)
    assert st["ok"] is False and st.get("needs_auth") is True
    # cached event survives (read-only degradation), status recorded
    assert [r["title"] for r in fdb.list_events(conn)] == ["Kept"]
    assert fdb.kv_get(conn, "caldav_status")["needs_auth"] is True


def test_caldav_sync_keeps_last_good_on_valid_empty_then_wipes_after_ttl(conn):
    full = FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT",
                        "ics": [_ics("u1", "Dentist", "20260820", "20260821")]}])
    caldav_sync.sync_once(full, conn, _CFG, _NOW)
    assert [r["title"] for r in fdb.list_events(conn)] == ["Dentist"]
    # same collection now returns 0 events without raising -> KEPT (within TTL)
    empty = FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}])
    st = caldav_sync.sync_once(empty, conn, _CFG, _NOW)
    assert st["ok"] is False and "kept last-synced" in st["error"]
    assert [r["title"] for r in fdb.list_events(conn)] == ["Dentist"]
    # past the TTL, the emptiness is finally accepted
    st = caldav_sync.sync_once(empty, conn, _CFG, _NOW + dt.timedelta(hours=25))
    assert fdb.list_events(conn) == []


def test_caldav_sync_empty_discover_keeps_cache(conn):
    caldav_sync.sync_once(
        FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT",
                     "ics": [_ics("u1", "Kept", "20260820", "20260821")]}]),
        conn, _CFG, _NOW)
    st = caldav_sync.sync_once(FakeCalDav([]), conn, _CFG, _NOW)  # maintenance blip
    assert st["ok"] is False and "no collections" in st["error"]
    assert [r["title"] for r in fdb.list_events(conn)] == ["Kept"]


def test_caldav_sync_keeps_cache_of_a_failing_collection(conn):
    fdb.replace_events_caldav(conn, [{"id": "old", "calendar_id": "caldav:bad",
        "title": "Kept", "start_ts": "2026-08-20", "end_ts": "2026-08-21",
        "all_day": 1}])

    class Flaky(FakeCalDav):
        def fetch_ics(self, collection, lo, hi):
            if collection["id"] == "bad":
                raise RuntimeError("boom")
            return super().fetch_ics(collection, lo, hi)
    caldav_sync.sync_once(Flaky([
        {"id": "good", "name": "Good", "comp": "VEVENT",
         "ics": [_ics("u1", "Fresh", "20260820", "20260821")]},
        {"id": "bad", "name": "Bad", "comp": "VEVENT", "ics": []},
    ]), conn, _CFG, _NOW)
    assert {r["title"] for r in fdb.list_events(conn)} == {"Fresh", "Kept"}


def test_caldav_sync_one_bad_object_does_not_freeze_the_collection(conn):
    # A single unparseable object in a collection must be skipped, not fail the
    # whole calendar (which would keep every other event stale behind an error).
    st = caldav_sync.sync_once(FakeCalDav([
        {"id": "cal", "name": "Family", "comp": "VEVENT", "ics": [
            _ics("u1", "Before", "20260820", "20260821"),
            "THIS IS NOT ICS",                       # raises in ics_events
            _ics("u2", "After", "20260822", "20260823"),
        ]},
    ]), conn, _CFG, _NOW)
    # the two good events synced; the collection is NOT marked failed
    assert {r["title"] for r in fdb.list_events(conn)} == {"Before", "After"}
    assert st["ok"] is True


def test_caldav_sync_all_objects_unparseable_flags_the_collection(conn):
    # A SYSTEMATIC parse failure (every object throws) is not a transient blip —
    # it must NOT report a healthy-but-empty calendar. The collection is flagged
    # (ok False, cache kept) instead of silently blanking behind a green wall.
    fdb.replace_events_caldav(conn, [{"id": "old", "calendar_id": "caldav:cal",
        "title": "Kept", "start_ts": "2026-08-20", "end_ts": "2026-08-21",
        "all_day": 1}])
    st = caldav_sync.sync_once(FakeCalDav([
        {"id": "cal", "name": "Family", "comp": "VEVENT",
         "ics": ["NOT ICS ONE", "NOT ICS TWO"]},   # every object raises
    ]), conn, _CFG, _NOW)
    assert st["ok"] is False and "parsed 0 of 2" in st["error"]
    assert st.get("needs_auth") is not True        # a parse break is not an auth failure
    assert [r["title"] for r in fdb.list_events(conn)] == ["Kept"]   # cache kept


def test_caldav_sync_one_bad_reminder_does_not_freeze_the_list(conn):
    # The per-object skip applies to VTODO too: a bad todo between good ones is
    # skipped, the rest of the list still syncs.
    st = caldav_sync.sync_once(FakeCalDav([
        {"id": "rem", "name": "Groceries", "comp": "VTODO", "todos": [
            _VTODO,                                  # "Buy milk" (good)
            "THIS IS NOT ICS",                       # raises in parse_vtodo
            _VTODO.replace("t1", "t2").replace("Buy milk", "Buy eggs"),
        ]},
    ]), conn, _CFG, _NOW)
    assert st["ok"] is True
    titles = {r["title"] for r in (fdb.kv_get(conn, "caldav_reminders") or [])}
    assert titles == {"Buy milk", "Buy eggs"}


class _NonAuthFlaky(FakeCalDav):
    """Every VEVENT fetch raises a NON-auth error (network reset) — ok False,
    needs_auth False, so it exercises the sustained-error path, not reconnect."""
    def fetch_ics(self, collection, lo, hi):
        raise RuntimeError("connection reset")


def test_caldav_sync_flags_a_sustained_nonauth_error_after_the_threshold(conn):
    # A non-auth failure is NOT surfaced on the first tick (would flicker on a
    # transient blip); only once it has run continuously past the threshold.
    client = _NonAuthFlaky([{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}])
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert st["ok"] is False and not st.get("needs_auth")
    assert not st.get("sustained")                        # first failure: clock starts
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW + dt.timedelta(hours=1))
    assert not st.get("sustained")                        # still under the threshold
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW + dt.timedelta(hours=6))
    assert st.get("sustained") is True                    # exactly at the threshold (>=)
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW + dt.timedelta(hours=7))
    assert st.get("sustained") is True                    # persisted past 6h -> surfaced


def test_caldav_sync_empty_but_cached_window_never_reads_sustained(conn):
    # A calendar that simply has no events in the window hits the empty-keep guard
    # each tick (ok False, "kept last-synced") but is NOT a stuck feed — it must
    # never surface as sustained/degraded, even past the error threshold. (Guards
    # against feeding the clock off the protective soft-empty message.)
    full = FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT",
                        "ics": [_ics("u1", "Dentist", "20260820", "20260821")]}])
    caldav_sync.sync_once(full, conn, _CFG, _NOW)                 # seed a cached event
    empty = FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}])
    caldav_sync.sync_once(empty, conn, _CFG, _NOW)               # empty-keep begins
    st = caldav_sync.sync_once(empty, conn, _CFG, _NOW + dt.timedelta(hours=7))
    assert "kept last-synced" in st["error"]      # it IS the soft empty state
    assert not st.get("sustained")                # ...but never a hard error


def test_caldav_sync_disable_clears_the_error_clock(conn):
    # Toggling iCloud off clears the running error clock, so a re-enable doesn't
    # instantly read sustained from a stale pre-disable timestamp.
    cols = [{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}]
    caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG, _NOW)          # clock starts
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_enabled(conn, "icloud_caldav", False)
    caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG,
                          _NOW + dt.timedelta(hours=1))                   # disabled -> clears
    fdb.set_integration_enabled(conn, "icloud_caldav", True)
    st = caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG,
                               _NOW + dt.timedelta(hours=8))
    assert not st.get("sustained")                # fresh clock after re-enable


def test_caldav_sync_recovers_from_a_corrupt_error_clock(conn):
    # A garbage stored timestamp must not crash or silently pin the clock at ~0h
    # forever: it's logged, reset to a valid value, and the feed can still surface.
    cols = [{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}]
    fdb.kv_set(conn, "caldav_error_since", "not-a-date")
    st = caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG, _NOW)
    assert not st.get("sustained")                # corrupt -> reset, not sustained
    dt.datetime.fromisoformat(fdb.kv_get(conn, "caldav_error_since"))  # now parses (no raise)


def test_caldav_sync_recovery_resets_the_error_clock(conn):
    cols = [{"id": "cal", "name": "F", "comp": "VEVENT",
             "ics": [_ics("u1", "X", "20260820", "20260821")]}]
    caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG, _NOW)         # clock starts
    caldav_sync.sync_once(FakeCalDav(cols), conn, _CFG,
                          _NOW + dt.timedelta(hours=1))                  # ok -> clears clock
    # a NEW error long afterward must start a FRESH clock, not inherit the old one
    st = caldav_sync.sync_once(_NonAuthFlaky(cols), conn, _CFG,
                               _NOW + dt.timedelta(hours=8))
    assert not st.get("sustained")                        # ~0h into the new error


def test_caldav_sync_auth_error_is_never_marked_sustained(conn):
    # An auth failure surfaces immediately (needs_auth) and must not also flow
    # through the sustained path — reconnect is the louder, correct signal.
    class AuthFail(FakeCalDav):
        def discover(self):
            raise RuntimeError("401 Unauthorized")
    client = AuthFail([])
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    st = caldav_sync.sync_once(client, conn, _CFG, _NOW + dt.timedelta(hours=8))
    assert st.get("needs_auth") is True
    assert not st.get("sustained")


def test_caldav_sync_inner_401_sets_needs_auth(conn):
    class Auth401(FakeCalDav):
        def fetch_ics(self, collection, lo, hi):
            raise RuntimeError("HTTP 401 Unauthorized")
    st = caldav_sync.sync_once(
        Auth401([{"id": "cal", "name": "F", "comp": "VEVENT", "ics": []}]),
        conn, _CFG, _NOW)
    assert st.get("needs_auth") is True


def test_caldav_sync_vtodo_fetch_failure_is_isolated(conn):
    class BadTodos(FakeCalDav):
        def fetch_todos(self, collection):
            raise RuntimeError("boom")
    st = caldav_sync.sync_once(BadTodos([
        {"id": "cal", "name": "F", "comp": "VEVENT",
         "ics": [_ics("u1", "E", "20260820", "20260821")]},
        {"id": "rem", "name": "R", "comp": "VTODO", "todos": []},
    ]), conn, _CFG, _NOW)
    assert st["events"] == 1 and "R" in st["error"]   # events synced despite VTODO fail


def test_caldav_sync_keeps_reminders_of_a_failing_list(conn):
    caldav_sync.sync_once(
        FakeCalDav([{"id": "rem", "name": "R", "comp": "VTODO", "todos": [_VTODO]}]),
        conn, _CFG, _NOW)
    assert [r["title"] for r in fdb.kv_get(conn, "caldav_reminders")] == ["Buy milk"]

    class BadTodos(FakeCalDav):
        def fetch_todos(self, collection):
            raise RuntimeError("boom")
    caldav_sync.sync_once(
        BadTodos([{"id": "rem", "name": "R", "comp": "VTODO", "todos": []}]),
        conn, _CFG, _NOW)
    assert [r["title"] for r in fdb.kv_get(conn, "caldav_reminders")] == ["Buy milk"]


def test_is_auth_error_walks_chain_and_class_name_and_no_false_positive():
    outer = RuntimeError("sync failed")
    outer.__cause__ = RuntimeError("HTTP 401")
    assert caldav_sync._is_auth_error(outer) is True

    class AuthorizationError(Exception):
        pass
    assert caldav_sync._is_auth_error(AuthorizationError("denied")) is True
    # an id/text containing 401 as a substring must NOT false-positive
    assert caldav_sync._is_auth_error(RuntimeError("event room4012")) is False


def test_caldav_sync_stores_objects_with_round_trip_fields(conn):
    client = FakeCalDav([
        {"id": "cal", "name": "F", "comp": "VEVENT",
         "ics": [_ics("u1", "Dentist", "20260820", "20260821")]},
        {"id": "rem", "name": "R", "comp": "VTODO", "todos": [_VTODO]},
    ])
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    objs = {o["id"]: o for o in fdb.list_cal_objects(conn)}
    ev = objs["caldav:cal/u1"]
    assert ev["comp_type"] == "VEVENT" and ev["uid"] == "u1"
    assert ev["raw_ics"] and "Dentist" in ev["raw_ics"]          # C1 fidelity
    assert ev["href"] and ev["etag"] and ev["base_etag"] == ev["etag"]
    assert ev["sync_state"] == "SYNCED"
    assert objs["caldav:rem/t1"]["comp_type"] == "VTODO"          # VTODO stored too


def test_caldav_sync_prunes_remotely_deleted_objects(conn):
    caldav_sync.sync_once(FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT",
        "ics": [_ics("u1", "A", "20260820", "20260821"),
                _ics("u2", "B", "20260820", "20260821")]}]), conn, _CFG, _NOW)
    assert len(fdb.list_cal_objects(conn, "VEVENT")) == 2
    # u2 deleted remotely -> gone from cal_objects after the next pull
    caldav_sync.sync_once(FakeCalDav([{"id": "cal", "name": "F", "comp": "VEVENT",
        "ics": [_ics("u1", "A", "20260820", "20260821")]}]), conn, _CFG, _NOW)
    assert {o["uid"] for o in fdb.list_cal_objects(conn, "VEVENT")} == {"u1"}


def test_upsert_cal_object_synced_never_clobbers_pending(conn):
    # a local pending edit, as the write slice will create
    conn.execute("INSERT INTO cal_objects(id, collection_id, comp_type, uid, "
                 "summary, sync_state) VALUES('caldav:x/u1','caldav:x','VTODO',"
                 "'u1','local edit','PENDING_UPDATE')")
    conn.commit()
    # a server pull must NOT overwrite it
    fdb.upsert_cal_object_synced(conn, {"id": "caldav:x/u1", "collection_id":
        "caldav:x", "comp_type": "VTODO", "uid": "u1", "summary": "server", "raw_ics": "X"})
    row = fdb.list_cal_objects(conn)[0]
    assert row["summary"] == "local edit" and row["sync_state"] == "PENDING_UPDATE"
    assert [o["id"] for o in fdb.caldav_pending(conn)] == ["caldav:x/u1"]  # in the outbox


class _RacyConn:
    """Wraps a connection and runs `hook` (another connection's write) at the
    first point this connection holds no open transaction: right after the
    first statement or commit. That is exactly where a wall edit from a
    request thread can land while the sync thread is mid-upsert."""

    def __init__(self, real, hook):
        self._real, self._hook, self._fired = real, hook, False

    def _maybe(self):
        if not self._fired and not self._real.in_transaction:
            self._fired = True
            self._hook()

    def execute(self, *a):
        cur = self._real.execute(*a)
        self._maybe()
        return cur

    def commit(self):
        self._real.commit()
        self._maybe()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_upsert_cal_object_synced_cannot_lose_a_racing_local_edit(tmp_path):
    """The pull checked sync_state, then wrote in a second statement. A wall
    edit committed from another thread in between was stomped back to SYNCED
    with the server copy, and the edit never reached iCloud. The check and the
    write must be one atomic statement."""
    path = str(tmp_path / "hub.db")
    sync_conn = fdb.connect(path)
    fdb.ensure_schema(sync_conn)
    wall_conn = fdb.connect(path)
    oid = "caldav:x/u1"
    fdb.upsert_cal_object_synced(sync_conn, {"id": oid, "collection_id": "caldav:x",
        "comp_type": "VTODO", "uid": "u1", "summary": "old", "raw_ics": "O",
        "etag": "e1"})

    def wall_edit():
        fdb.queue_cal_object_update(wall_conn, oid, "L", "local edit",
                                    "2026-08-17T12:00:00+00:00")

    fdb.upsert_cal_object_synced(_RacyConn(sync_conn, wall_edit), {
        "id": oid, "collection_id": "caldav:x", "comp_type": "VTODO",
        "uid": "u1", "summary": "server", "raw_ics": "S", "etag": "e2"})
    row = fdb.get_cal_object(sync_conn, oid)
    assert row["sync_state"] == "PENDING_UPDATE", "the local edit was stomped"
    assert row["summary"] == "local edit"
    assert [o["id"] for o in fdb.caldav_pending(sync_conn)] == [oid]
    sync_conn.close()
    wall_conn.close()


def test_upsert_cal_object_synced_resets_sync_bookkeeping(conn):
    """A server-wins (force) or plain pull adopts the server copy wholesale:
    the old retry count, error and local-edit stamp must not survive it."""
    oid = "caldav:x/u1"
    conn.execute("INSERT INTO cal_objects(id, collection_id, comp_type, uid, "
                 "summary, sync_state, local_modified_at, sync_attempts, "
                 "last_sync_error, href) VALUES(?, 'caldav:x', 'VTODO', 'u1', "
                 "'local', 'PENDING_UPDATE', 't', 4, 'boom', 'h/old')", (oid,))
    conn.commit()
    fdb.upsert_cal_object_synced(conn, {"id": oid, "collection_id": "caldav:x",
        "comp_type": "VTODO", "uid": "u1", "summary": "server", "raw_ics": "S",
        "etag": "e9", "href": "h/new", "sequence": 3}, force=True)
    row = fdb.get_cal_object(conn, oid)
    assert row["sync_state"] == "SYNCED" and row["summary"] == "server"
    assert row["etag"] == row["base_etag"] == "e9" and row["href"] == "h/new"
    assert row["sequence"] == 3
    assert row["sync_attempts"] == 0 and row["last_sync_error"] is None
    assert row["local_modified_at"] is None


def test_caldav_credentials_file_storage(tmp_path):
    import os
    import stat
    path = str(tmp_path / "caldav.json")
    env = {"CALDAV_CREDS_PATH": path}
    assert caldav_service.configured(env) is False
    caldav_service.store_credentials("bot@icloud.com", "abcd-efgh", env)
    assert caldav_service.configured(env) is True
    assert caldav_service.caldav_credentials(env) == ("bot@icloud.com", "abcd-efgh")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600           # secret file perms
    # env creds take precedence over the file (advanced/backward-compat)
    env2 = {**env, "ICLOUD_CALDAV_USER": "env@x",
            "ICLOUD_CALDAV_APP_PASSWORD": "envpw"}
    assert caldav_service.caldav_credentials(env2) == ("env@x", "envpw")
    caldav_service.clear_credentials(env)
    assert caldav_service.configured(env) is False


def test_caldav_collections_upsert_preserves_toggle(conn):
    fdb.upsert_caldav_collection(conn, "caldav:a", "VEVENT", "Family", "#FF0000", "t1")
    assert fdb.caldav_collection_enabled(conn, "caldav:a") is True
    fdb.set_caldav_collection_enabled(conn, "caldav:a", False)
    # re-discovery updates metadata but keeps the operator's OFF toggle
    fdb.upsert_caldav_collection(conn, "caldav:a", "VEVENT", "Renamed", "#00FF00", "t2")
    col = fdb.list_caldav_collections(conn)[0]
    assert col["display_name"] == "Renamed" and col["color"] == "#00FF00"
    assert col["enabled"] is False


def test_caldav_sync_records_collections(conn):
    caldav_sync.sync_once(FakeCalDav([
        {"id": "cal", "name": "Family", "color": "#FF0000", "comp": "VEVENT",
         "ics": [_ics("u1", "E", "20260820", "20260821")]},
        {"id": "rem", "name": "Groceries", "comp": "VTODO", "todos": [_VTODO]},
    ]), conn, _CFG, _NOW)
    cols = {c["id"]: c for c in fdb.list_caldav_collections(conn)}
    assert cols["caldav:cal"]["comp_type"] == "VEVENT" and cols["caldav:cal"]["display_name"] == "Family"
    assert cols["caldav:rem"]["comp_type"] == "VTODO"   # reminder list recorded too


# --- two-way write path (outbox flush) ------------------------------------

_UTC_NOW = dt.datetime(2026, 8, 17, 12, 0, 0, tzinfo=dt.timezone.utc)


class WriteFake(FakeCalDav):
    """FakeCalDav plus a recording write side + a tiny in-memory 'server' (href ->
    ics) so the outbox flush AND conflict resolution are testable without a
    network."""
    def __init__(self, collections):
        super().__init__(collections)
        self.puts = []      # (collection_id, href_or_None, ics)
        self.deletes = []   # (collection_id, href)
        self.server = {}    # href -> ics, for get_object (server-wins resolve)
        self._n = 0

    def put_object(self, collection, href, ics, base_etag=None, uid=None):
        self.puts.append((collection["id"], href, ics))
        if href is None:                       # create -> server assigns a URL
            self._n += 1
            href = f"h/{collection['id']}/new{self._n}"
        self.server[href] = ics
        return {"href": href, "etag": "srv-etag"}

    def delete_object(self, collection, href, base_etag=None):
        self.deletes.append((collection["id"], href))
        self.server.pop(href, None)

    def get_object(self, collection, href):
        ics = self.server.get(href)
        return {"href": href, "etag": "srv-etag2", "ics": ics} if ics else None


def _seed_vtodo_collection(conn, cid="rem", name="Groceries"):
    fdb.upsert_caldav_collection(conn, "caldav:" + cid, "VTODO", name, None,
                                 "2026-08-17T00:00:00")


def test_flush_pushes_create_and_marks_synced(conn):
    _seed_vtodo_collection(conn)
    ics = remlogic.build_vtodo("U-NEW", "Water plants", _UTC_NOW)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "U-NEW", "summary": "Water plants",
        "raw_ics": ics}, "t0")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["pushed"] == 1 and client.puts[0][1] is None   # create = no href
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row["sync_state"] == "SYNCED"
    assert row["href"] == "h/rem/new1" and row["etag"] == "srv-etag"
    assert fdb.caldav_pending(conn) == []                     # outbox drained


def test_flush_pushes_update_from_toggle(conn):
    _seed_vtodo_collection(conn)
    fdb.upsert_cal_object_synced(conn, {
        "id": "caldav:rem/t1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": "h/rem/0", "etag": "e0", "summary": "Buy milk",
        "raw_ics": _VTODO, "sequence": 0, "last_modified": None})
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    assert fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert client.puts[0][1] == "h/rem/0"                     # update = PUT to href
    assert "STATUS:COMPLETED" in client.puts[0][2]
    assert fdb.get_cal_object(conn, "caldav:rem/t1")["sync_state"] == "SYNCED"


def test_flush_deletes_and_removes_row(conn):
    _seed_vtodo_collection(conn)
    fdb.upsert_cal_object_synced(conn, {
        "id": "caldav:rem/t1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": "h/rem/0", "etag": "e0", "summary": "Buy milk",
        "raw_ics": _VTODO, "sequence": 0, "last_modified": None})
    assert fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert client.deletes == [("rem", "h/rem/0")]
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None  # row gone locally


def test_delete_of_unpushed_create_never_hits_server(conn):
    _seed_vtodo_collection(conn)
    ics = remlogic.build_vtodo("U-NEW", "Oops", _UTC_NOW)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "U-NEW", "summary": "Oops",
        "raw_ics": ics}, "t0")
    # deleting a create that never synced just drops the row (nothing on server)
    assert fdb.queue_cal_object_delete(conn, "caldav:rem/U-NEW", "t1")
    assert fdb.get_cal_object(conn, "caldav:rem/U-NEW") is None
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert client.puts == [] and client.deletes == []


def test_flush_isolates_and_records_error_keeping_pending(conn):
    _seed_vtodo_collection(conn)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "U1", "summary": "x", "raw_ics": remlogic.build_vtodo("U1", "x", _UTC_NOW)}, "t0")

    class Boom(WriteFake):
        def put_object(self, collection, href, ics, base_etag=None, uid=None):
            raise RuntimeError("HTTP 401 Unauthorized")

    client = Boom([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["pushed"] == 0 and res["needs_auth"] is True
    row = fdb.get_cal_object(conn, "caldav:rem/U1")
    assert row["sync_state"] == "PENDING_CREATE"              # still queued, retries
    assert row["sync_attempts"] == 1 and "401" in row["last_sync_error"]


def test_sync_once_skips_flush_when_readonly(conn):
    _seed_vtodo_collection(conn)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "U1", "summary": "x", "raw_ics": remlogic.build_vtodo("U1", "x", _UTC_NOW)}, "t0")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO", "todos": []}])
    # default: readonly (1-way) -> outbox is NOT pushed
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert client.puts == []
    assert fdb.get_cal_object(conn, "caldav:rem/U1")["sync_state"] == "PENDING_CREATE"
    # switch to 2-way -> the next sync flushes it
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    assert len(client.puts) == 1
    assert fdb.get_cal_object(conn, "caldav:rem/U1")["sync_state"] == "SYNCED"


# --- conditional writes (If-Match / If-None-Match) adapter, P1 -------------
# The real CalDavClient.put/delete/get_object go through the low-level DAV
# request; exercise the ADAPTER (header construction, create URL, status
# handling, etag extraction) with a fake DAVClient — the only part a live server
# is needed for is the socket transport itself.

class _Resp:
    def __init__(self, status, headers=None, raw=b""):
        self.status = status
        self.headers = headers or {}
        self.raw = raw
        self.reason = ""


class _DAV:
    def __init__(self, put_resp=None, req_resp=None):
        self.calls = []
        self._put_resp = put_resp or _Resp(201, {"ETag": "srv-1"})
        self._req_resp = req_resp

    def put(self, url, body, headers=None):
        self.calls.append(("PUT", url, dict(headers or {}), body))
        return self._put_resp

    def request(self, url, method="GET", body="", headers=None):
        self.calls.append((method, url, dict(headers or {}), body))
        if self._req_resp is not None:
            return self._req_resp
        if method == "GET":
            return _Resp(200, {"ETag": "g1"}, _VTODO.encode())
        return _Resp(204)


class _Cal:
    def __init__(self, client, url):
        self.client = client
        self.url = url


def _client_and_col(dav):
    return caldav_service.CalDavClient("u", "p"), {"_cal": _Cal(dav, "https://x/cal/")}


def test_put_object_update_sends_if_match():
    dav = _DAV()
    cl, col = _client_and_col(dav)
    res = cl.put_object(col, "https://x/cal/t1.ics", "ICS", base_etag="e0")
    assert res == {"href": "https://x/cal/t1.ics", "etag": "srv-1"}
    method, url, headers, _ = dav.calls[0]
    assert method == "PUT" and headers.get("If-Match") == "e0"


def test_put_object_create_builds_url_and_if_none_match():
    dav = _DAV()
    cl, col = _client_and_col(dav)
    res = cl.put_object(col, None, "ICS", uid="U1")
    method, url, headers, _ = dav.calls[0]
    assert url == "https://x/cal/U1.ics"                 # UID-derived resource URL
    assert headers.get("If-None-Match") == "*" and "If-Match" not in headers
    assert res["href"] == "https://x/cal/U1.ics"


def test_put_object_412_raises_conflict():
    dav = _DAV(put_resp=_Resp(412))
    cl, col = _client_and_col(dav)
    try:
        cl.put_object(col, "https://x/cal/t1.ics", "ICS", base_etag="e0")
        assert False, "expected CalDavConflict"
    except caldav_service.CalDavConflict:
        pass


def test_delete_object_sends_if_match_and_404_is_ok():
    dav = _DAV(req_resp=_Resp(404))
    cl, col = _client_and_col(dav)
    cl.delete_object(col, "https://x/cal/t1.ics", base_etag="e0")   # 404 = fine
    method, url, headers, _ = dav.calls[0]
    assert method == "DELETE" and headers.get("If-Match") == "e0"


def test_delete_object_412_raises_conflict():
    dav = _DAV(req_resp=_Resp(412))
    cl, col = _client_and_col(dav)
    try:
        cl.delete_object(col, "https://x/cal/t1.ics", base_etag="e0")
        assert False, "expected CalDavConflict"
    except caldav_service.CalDavConflict:
        pass


def test_get_object_none_on_404_and_body_on_200():
    cl, col = _client_and_col(_DAV(req_resp=_Resp(404)))
    assert cl.get_object(col, "https://x/cal/t1.ics") is None
    cl2, col2 = _client_and_col(_DAV())               # default GET -> 200 + body
    got = cl2.get_object(col2, "https://x/cal/t1.ics")
    assert got["etag"] == "g1" and "Buy milk" in got["ics"]


# --- conflict resolution in flush (server-wins), P + silent-failure F3 -----

_SERVER_NEWER = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\n"
                 "SUMMARY:Milk (edited on phone)\r\nSTATUS:NEEDS-ACTION\r\n"
                 "END:VTODO\r\nEND:VCALENDAR\r\n")


def _seed_synced_todo(conn):
    _seed_vtodo_collection(conn)
    fdb.upsert_cal_object_synced(conn, {
        "id": "caldav:rem/t1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": "h/rem/0", "etag": "e0", "summary": "Buy milk",
        "raw_ics": _VTODO, "sequence": 0, "last_modified": None})


def test_flush_conflict_server_wins(conn):
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")

    class Conflict(WriteFake):
        def put_object(self, collection, href, ics, base_etag=None, uid=None):
            raise caldav_service.CalDavConflict(href)

    client = Conflict([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    client.server["h/rem/0"] = _SERVER_NEWER          # the concurrent phone edit
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["conflicts"] == 1 and res["errors"] == []
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "SYNCED"
    assert "edited on phone" in row["raw_ics"]         # server won, not our edit
    assert row["etag"] == "srv-etag2"


def test_flush_conflict_server_gone_drops_row(conn):
    _seed_synced_todo(conn)
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")   # PENDING_DELETE

    class Conflict(WriteFake):
        def delete_object(self, collection, href, base_etag=None):
            raise caldav_service.CalDavConflict(href)

    client = Conflict([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    # server has nothing at that href -> get_object None -> drop local row
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["conflicts"] == 1
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None


# --- P2: create-then-prune race driven through sync_once ------------------

def test_sync_once_create_survives_prune_and_pushes(conn):
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    fdb.upsert_caldav_collection(conn, "caldav:rem", "VTODO", "Groceries", None, "t")
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "U1", "summary": "new", "raw_ics": remlogic.build_vtodo("U1", "new", _UTC_NOW)}, "t0")
    # the pull returns a DIFFERENT todo, so prune fires for this collection...
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO",
                         "todos": [_VTODO]}])
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    # ...and the un-pushed PENDING_CREATE must have survived the prune AND pushed
    assert len(client.puts) == 1
    assert fdb.get_cal_object(conn, "caldav:rem/U1")["sync_state"] == "SYNCED"


# --- P3: flush skips + records when the collection isn't discovered --------

def test_flush_records_when_collection_not_discovered(conn):
    _seed_vtodo_collection(conn)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "U1", "summary": "x", "raw_ics": remlogic.build_vtodo("U1", "x", _UTC_NOW)}, "t0")
    client = WriteFake([{"id": "other", "name": "Other", "comp": "VTODO"}])  # NOT rem
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert client.puts == [] and res["pushed"] == 0 and res["errors"] == []
    row = fdb.get_cal_object(conn, "caldav:rem/U1")
    assert row["sync_state"] == "PENDING_CREATE"          # still queued, retries
    assert row["sync_attempts"] == 1
    assert "not discovered" in row["last_sync_error"]


# --- P6: PENDING_DELETE with no href skips the server DELETE ---------------

def test_flush_delete_without_href_drops_locally(conn):
    _seed_vtodo_collection(conn)
    fdb.upsert_cal_object_synced(conn, {
        "id": "caldav:rem/t1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": None, "etag": None, "summary": "x",
        "raw_ics": _VTODO, "sequence": 0, "last_modified": None})
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")   # SYNCED->PENDING_DELETE
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert client.deletes == []                               # no server call
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None  # dropped locally


# --- P7: full reconciliation loop (push -> SYNCED -> re-pull adopts body) ---

def test_reconciliation_repull_adopts_server_body(conn):
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO",
                         "todos": [_SERVER_NEWER]}])
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    caldav_sync.sync_once(client, conn, _CFG, _NOW)          # pull(t1 old) -> flush(push)
    assert fdb.get_cal_object(conn, "caldav:rem/t1")["sync_state"] == "SYNCED"
    # a later pull with the server's canonical body reconciles raw_ics
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert "edited on phone" in row["raw_ics"]               # server body adopted


# --- P4: set_integration_config fails loud on a missing row ----------------

def test_set_integration_config_raises_on_missing_row(conn):
    raised = False
    try:
        fdb.set_integration_config(conn, "nope", {"readonly": False})
    except KeyError:
        raised = True
    assert raised, "silent no-op would let a two-way toggle appear-to-save yet not apply"
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    assert fdb.integration_config(conn, "icloud_caldav")["readonly"] is False


def test_caldav_status_pending_survives_sync_failure(conn):
    _seed_vtodo_collection(conn)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U1", "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "U1", "summary": "x", "raw_ics": remlogic.build_vtodo("U1", "x", _UTC_NOW)}, "t0")

    class AuthFail:
        def configured(self):
            return True

        def discover(self):
            raise RuntimeError("401 Unauthorized")

    st = caldav_sync.sync_once(AuthFail(), conn, _CFG, _NOW)
    assert st["ok"] is False and st.get("needs_auth") is True
    assert st["pending"] == 1    # a queued wall edit stays visible during an outage


def _two_way_mirror_fixture(conn):
    """A mapped person + chore + two-way iCloud, ready to drive through
    sync_once (returns the fake client)."""
    pid = fdb.add_person(conn, "Emma", "#5BC9F0")
    fdb.upsert_caldav_collection(conn, "caldav:rem", "VTODO", "Emma", None, "t")
    fdb.update_person(conn, pid, reminder_list_id="caldav:rem")
    fdb.add_chore(conn, title="Dishes", icon="", schedule_kind="daily", days_mask=0,
                  assign_kind="fixed", fixed_person_id=pid, rotation_order=[],
                  rotation_epoch="2026-08-01")
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    return WriteFake([{"id": "rem", "name": "Emma", "comp": "VTODO", "todos": []}])


def test_sync_once_records_chore_mirror_status(conn):
    """M1: the mirror reconcile's result was thrown away, so a mirror that died
    every tick was invisible — the calendar badge stayed 'ok' forever. Each tick
    now records a status the admin surface can read."""
    client = _two_way_mirror_fixture(conn)
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    st = fdb.kv_get(conn, "chore_mirror_status")
    assert st["ok"] is True and st["at"] == _NOW.isoformat()
    assert st["created"] >= 1 and st["deleted"] == 0


def test_sync_once_records_chore_mirror_failure(conn, monkeypatch):
    """M1: reconcile() swallows its own exceptions and returns {"error": True}.
    That signal must reach the status, not the floor."""
    from family_hub import chore_mirror
    client = _two_way_mirror_fixture(conn)
    monkeypatch.setattr(chore_mirror, "reconcile",
                        lambda *a, **k: {"created": 0, "moved": 0, "updated": 0,
                                         "deleted": 0, "error": True})
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    st = fdb.kv_get(conn, "chore_mirror_status")
    assert st["ok"] is False and st["at"] == _NOW.isoformat()


def test_sync_once_clears_stale_mirror_error_on_readonly(conn):
    """M2: a mirror error latched from an earlier two-way tick must not haunt
    the settings row forever. If the operator switches iCloud back to
    read-only, the NEXT tick (which skips the mirror entirely) has to clear
    the stale error rather than leaving chore_mirror_status stuck at ok=False
    with no path back to a quiet chip."""
    fdb.kv_set(conn, "chore_mirror_status",
               {"ok": False, "at": "2026-08-01T00:00:00", "created": 0,
                "moved": 0, "updated": 0, "deleted": 0})
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": True})
    client = FakeCalDav([])
    caldav_sync.sync_once(client, conn, _CFG, _NOW)
    st = fdb.kv_get(conn, "chore_mirror_status")
    assert st.get("ok") is not False, \
        "a readonly tick must clear a stale mirror error, not leave it latched"


def test_sync_once_mirrors_chores_to_icloud(conn):
    """End-to-end: a mapped person + chore, driven through sync_once with a real
    LOCAL-zone now, pushes the chore occurrences to iCloud with UTC DTSTAMP."""
    from zoneinfo import ZoneInfo
    pid = fdb.add_person(conn, "Emma", "#5BC9F0")
    fdb.upsert_caldav_collection(conn, "caldav:rem", "VTODO", "Emma", None, "t")
    fdb.update_person(conn, pid, reminder_list_id="caldav:rem")
    fdb.add_chore(conn, title="Dishes", icon="", schedule_kind="daily", days_mask=0,
                  assign_kind="fixed", fixed_person_id=pid, rotation_order=[],
                  rotation_epoch="2026-08-01")
    fdb.seed_integration(conn, "icloud_caldav", "caldav")
    fdb.set_integration_config(conn, "icloud_caldav", {"readonly": False})
    tznow = dt.datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    client = WriteFake([{"id": "rem", "name": "Emma", "comp": "VTODO", "todos": []}])
    caldav_sync.sync_once(client, conn, _CFG, tznow)
    dishes = [p for p in client.puts if "Dishes" in p[2]]
    assert dishes and all(p[0] == "rem" for p in dishes)               # pushed to Emma's list
    assert any("DTSTAMP:20260817T190000Z" in p[2] for p in dishes)     # 12 PDT -> 19Z (UTC fix)
    assert fdb.list_chore_mirror(conn)
    assert all(o["sync_state"] == "SYNCED" for o in fdb.list_cal_objects(conn, "VTODO"))


# --- push race: a wall edit that lands while the upload is in flight ---------

def _wall_conn(conn):
    """A SECOND connection to the same db file, the way the wall's request
    threads write while the sync thread holds its own connection."""
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    return fdb.connect(path)


class MidFlightEdit(WriteFake):
    """WriteFake whose first PUT/DELETE runs `during` (a wall change on another
    connection) while the upload is in flight, and records the If-Match each
    request carried."""
    def __init__(self, collections, during):
        super().__init__(collections)
        self._during = during
        self.if_match = []

    def _fire(self):
        if self._during:
            during, self._during = self._during, None
            during()

    def put_object(self, collection, href, ics, base_etag=None, uid=None):
        self.if_match.append(base_etag)
        res = super().put_object(collection, href, ics, base_etag, uid)
        self._fire()
        return res

    def delete_object(self, collection, href, base_etag=None):
        self.if_match.append(base_etag)
        super().delete_object(collection, href, base_etag)
        self._fire()


def test_edit_during_update_push_is_not_lost(conn):
    """The lost-edit race: the sync thread PUTs version A of a reminder; while
    that upload is in flight the wall saves version B. Marking the row SYNCED
    after the upload used to throw B away (the next pull overwrote it with A).
    B must stay queued, built on the etag the upload just earned, and reach
    iCloud on the next flush without a conflict."""
    _seed_synced_todo(conn)
    first = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", first, "Buy milk", "t0")
    second = first.replace("SUMMARY:Buy milk", "SUMMARY:Buy oat milk")
    wall = _wall_conn(conn)

    def wall_edit():
        assert fdb.queue_cal_object_update(wall, "caldav:rem/t1", second,
                                           "Buy oat milk", "t0b")

    client = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                           wall_edit)
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["pushed"] == 1 and res["errors"] == []
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "PENDING_UPDATE", "the newer wall edit was lost"
    assert row["raw_ics"] == second and row["summary"] == "Buy oat milk"
    assert row["base_etag"] == "srv-etag"      # built on what the upload earned
    # the next flush sends B conditional on the etag A's upload got back
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert res["pushed"] == 1 and res["conflicts"] == 0
    assert client.if_match == ["e0", "srv-etag"]
    assert client.server["h/rem/0"] == second
    assert fdb.get_cal_object(conn, "caldav:rem/t1")["sync_state"] == "SYNCED"
    wall.close()


def test_edit_during_create_push_becomes_an_update(conn):
    """A reminder added on the wall and edited while its create is uploading:
    the create gave it a server URL, so the queued edit must become an UPDATE
    to that URL. Left as a create it would PUT a duplicate, and a delete would
    skip the server entirely."""
    _seed_vtodo_collection(conn)
    ics = remlogic.build_vtodo("U-NEW", "Water plants", _UTC_NOW)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "U-NEW", "summary": "Water plants",
        "raw_ics": ics}, "t0")
    edited = remlogic.set_completed(ics, True, _UTC_NOW)
    wall = _wall_conn(conn)

    def wall_edit():
        assert fdb.queue_cal_object_update(wall, "caldav:rem/U-NEW", edited,
                                           "Water plants", "t0b")

    client = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                           wall_edit)
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row["sync_state"] == "PENDING_UPDATE"
    assert row["href"] == "h/rem/new1" and row["base_etag"] == "srv-etag"
    assert row["raw_ics"] == edited
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert [p[1] for p in client.puts] == [None, "h/rem/new1"]   # no duplicate
    assert fdb.get_cal_object(conn, "caldav:rem/U-NEW")["sync_state"] == "SYNCED"
    wall.close()


def _queue_new_oops(conn):
    _seed_vtodo_collection(conn)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "U-NEW", "summary": "Oops",
        "raw_ics": remlogic.build_vtodo("U-NEW", "Oops", _UTC_NOW)}, "t0")


def test_delete_during_create_push_removes_the_server_copy(conn):
    """A reminder added and then deleted on the wall while its create is still
    uploading: the delete dropped the local row (it looked unpushed), but the
    upload landed it in iCloud. The flush must delete that server copy, or the
    next pull brings the deleted reminder back."""
    _queue_new_oops(conn)
    wall = _wall_conn(conn)

    def wall_delete():
        assert fdb.queue_cal_object_delete(wall, "caldav:rem/U-NEW", "t0b")

    client = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                           wall_delete)
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["errors"] == [] and res["pushed"] == 0
    assert client.deletes == [("rem", "h/rem/new1")]
    assert client.if_match == [None, "srv-etag"]
    assert client.server == {}
    assert fdb.get_cal_object(conn, "caldav:rem/U-NEW") is None
    wall.close()


def test_delete_during_create_push_that_fails_is_reported(conn):
    """If removing that orphaned server copy fails, the flush says so (an error
    in the result) instead of counting a clean push."""
    _queue_new_oops(conn)
    wall = _wall_conn(conn)

    class DeleteFails(MidFlightEdit):
        def delete_object(self, collection, href, base_etag=None):
            raise RuntimeError("HTTP 401 Unauthorized")

    client = DeleteFails(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/U-NEW", "t0b"))
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["pushed"] == 0 and res["needs_auth"] is True
    assert len(res["errors"]) == 1 and "401" in res["errors"][0]
    # the server copy is remembered as a queued delete, so it is retried and
    # counted as pending, and a pull can't bring the reminder back meanwhile
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row["sync_state"] == "PENDING_DELETE"
    assert row["href"] == "h/rem/new1" and row["base_etag"] == "srv-etag"
    assert row["sync_attempts"] == 1 and "401" in row["last_sync_error"]
    _store_pulled(conn, "caldav:rem/U-NEW", "h/rem/new1", "srv-etag")
    assert fdb.get_cal_object(conn, "caldav:rem/U-NEW")["sync_state"] == \
        "PENDING_DELETE"
    # the next flush, with iCloud reachable again, finishes the delete
    ok = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    ok.server["h/rem/new1"] = "x"
    caldav_sync.flush_pending(ok, conn, ok.discover(), "t2")
    assert ok.deletes == [("rem", "h/rem/new1")]
    assert fdb.get_cal_object(conn, "caldav:rem/U-NEW") is None
    wall.close()


def _store_pulled(conn, oid, href, etag):
    """What a pull does with a server copy: the routine (non-forced) upsert."""
    cid, uid = oid.split("/", 1)
    fdb.upsert_cal_object_synced(conn, {
        "id": oid, "collection_id": cid, "comp_type": "VTODO", "uid": uid,
        "href": href, "etag": etag, "summary": "Oops",
        "raw_ics": remlogic.build_vtodo(uid, "Oops", _UTC_NOW), "sequence": 0,
        "last_modified": None})


def test_recreate_during_delete_push_is_not_lost(conn):
    """The chore mirror re-creates an occurrence while its old copy's DELETE is
    in flight. Dropping the row after the DELETE used to lose the re-create.
    It must survive as a fresh create (the server copy it pointed at is gone)."""
    _seed_synced_todo(conn)
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")
    wall = _wall_conn(conn)

    def wall_recreate():
        fdb.queue_cal_object_create(wall, {
            "id": "caldav:rem/t1", "collection_id": "caldav:rem",
            "comp_type": "VTODO", "uid": "t1", "summary": "Buy milk",
            "raw_ics": _VTODO}, "t0b")

    client = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                           wall_recreate)
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert client.deletes == [("rem", "h/rem/0")]
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row is not None, "the re-create was dropped with the deleted row"
    assert row["sync_state"] == "PENDING_CREATE"
    assert row["href"] is None and row["base_etag"] is None
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert client.puts[-1][1] is None                       # pushed as a create
    assert fdb.get_cal_object(conn, "caldav:rem/t1")["sync_state"] == "SYNCED"
    wall.close()


def test_every_queued_change_bumps_the_local_revision(conn):
    """The push compares this revision to know whether the row is still the
    version it uploaded, so every kind of queued wall change must move it."""
    _seed_synced_todo(conn)
    oid = "caldav:rem/t1"
    revs = [fdb.get_cal_object(conn, oid)["local_rev"]]
    fdb.queue_cal_object_update(conn, oid, _VTODO, "Buy milk", "t1")
    revs.append(fdb.get_cal_object(conn, oid)["local_rev"])
    fdb.queue_cal_object_create(conn, {
        "id": oid, "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "summary": "Buy milk", "raw_ics": _VTODO}, "t2")
    revs.append(fdb.get_cal_object(conn, oid)["local_rev"])
    fdb.queue_cal_object_delete(conn, oid, "t3")
    revs.append(fdb.get_cal_object(conn, oid)["local_rev"])
    assert revs == sorted(set(revs)), revs                  # strictly increasing


class ConflictMidFlight(MidFlightEdit):
    """The PUT or DELETE runs `during`, then answers 412: the server copy
    moved while the wall changed the row too."""
    def put_object(self, collection, href, ics, base_etag=None, uid=None):
        self.if_match.append(base_etag)
        self._fire()
        raise caldav_service.CalDavConflict(href)

    def delete_object(self, collection, href, base_etag=None):
        self.if_match.append(base_etag)
        self._fire()
        raise caldav_service.CalDavConflict(href)


def test_delete_during_a_conflicted_update_keeps_the_delete(conn):
    """The wall deletes a reminder while its update is uploading, and the
    upload hits a 412. Server-wins used to force the row back to SYNCED and
    drop the delete. The delete is the newest wish: it must stay queued, on
    the server's current etag, and go out next flush."""
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    wall = _wall_conn(conn)
    client = ConflictMidFlight(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/t1", "t0b"))
    client.server["h/rem/0"] = _SERVER_NEWER
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row is not None and row["sync_state"] == "PENDING_DELETE"
    assert row["base_etag"] == "srv-etag2"          # the server's current copy
    ok = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}], None)
    ok.server["h/rem/0"] = _SERVER_NEWER
    caldav_sync.flush_pending(ok, conn, ok.discover(), "t2")
    assert ok.deletes == [("rem", "h/rem/0")] and ok.if_match == ["srv-etag2"]
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None
    wall.close()


def test_delete_during_a_conflicted_create_does_not_resurrect(conn):
    """A create that hits a 412 (the object already exists at its URL) while
    the wall deletes it: the resolver used to insert the server copy as
    SYNCED, bringing the deleted reminder back. It must queue the server copy
    for delete instead."""
    _queue_new_oops(conn)
    wall = _wall_conn(conn)

    class CreateConflict(ConflictMidFlight):
        def put_object(self, collection, href, ics, base_etag=None, uid=None):
            self._fire()     # If-None-Match:* found the UID URL taken
            raise caldav_service.CalDavConflict("h/rem/U-NEW.ics")

        def get_object(self, collection, href):
            assert href == "h/rem/U-NEW.ics"
            return {"href": href, "etag": "srv-etag2",
                    "ics": remlogic.build_vtodo("U-NEW", "Oops", _UTC_NOW)}

    client = CreateConflict(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/U-NEW", "t0b"))
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row is not None, "the server copy was forgotten; a pull revives it"
    assert row["sync_state"] == "PENDING_DELETE", "the deleted reminder came back"
    assert (row["href"], row["base_etag"]) == ("h/rem/U-NEW.ics", "srv-etag2")
    wall.close()


def test_conflicted_create_readded_meanwhile_takes_over_the_server_copy(
        conn, monkeypatch):
    """The narrow case where the id is re-added between the resolver seeing
    the row gone and queueing the orphan delete. The new row must take over
    the iCloud copy as an update; left without an href, its create would 412
    again and server-wins would throw the re-add away."""
    _queue_new_oops(conn)
    sent = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    readded = remlogic.build_vtodo("U-NEW", "Oops again", _UTC_NOW)
    fdb.queue_cal_object_create(conn, {
        "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "U-NEW", "summary": "Oops again",
        "raw_ics": readded}, "t0c")
    real_get = fdb.get_cal_object
    seen = []

    def gone_once(c, oid):                  # the resolver's look saw it gone
        if not seen:
            seen.append(oid)
            return None
        return real_get(c, oid)

    monkeypatch.setattr(fdb, "get_cal_object", gone_once)

    class Server(WriteFake):
        def get_object(self, collection, href):
            return {"href": href, "etag": "srv-etag2",
                    "ics": remlogic.build_vtodo("U-NEW", "Oops", _UTC_NOW)}

    client = Server([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    caldav_sync._resolve_conflict(client, conn, client.discover()[0], sent, "t1",
                                  conflict_href="h/rem/U-NEW.ics")
    monkeypatch.setattr(fdb, "get_cal_object", real_get)
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row["sync_state"] == "PENDING_UPDATE" and row["raw_ics"] == readded
    assert (row["href"], row["base_etag"]) == ("h/rem/U-NEW.ics", "srv-etag2")


def test_edit_during_a_conflicted_update_is_resolved_next_round(conn):
    """A newer wall edit that lands while a 412 upload is out is not forced
    over in the same step (that would drop it without a word). It stays
    queued; its own push resolves it next flush (see the round-two test)."""
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    newer = done.replace("SUMMARY:Buy milk", "SUMMARY:Buy oat milk")
    wall = _wall_conn(conn)
    client = ConflictMidFlight(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_update(wall, "caldav:rem/t1", newer,
                                            "Buy oat milk", "t0b"))
    client.server["h/rem/0"] = _SERVER_NEWER
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "PENDING_UPDATE" and row["raw_ics"] == newer
    wall.close()


def test_double_delete_during_the_delete_just_drops_the_row(conn, caplog):
    """A second tap on delete while the first DELETE is out: the server copy
    is gone either way, so the row is dropped with no alarming warning."""
    _seed_synced_todo(conn)
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")
    wall = _wall_conn(conn)
    client = MidFlightEdit(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/t1", "t0b"))
    with caplog.at_level(logging.INFO, logger="family_hub"):
        caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None
    assert client.deletes == [("rem", "h/rem/0")]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    wall.close()


def test_delete_during_update_push_keeps_the_delete(conn):
    """The wall deletes a reminder while its update is uploading (no
    conflict). The delete must stay queued, on the etag the upload earned,
    and go out next flush. Marking it SYNCED or turning it back into an
    update would bring the reminder back."""
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    wall = _wall_conn(conn)
    client = MidFlightEdit(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/t1", "t0b"))
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "PENDING_DELETE"
    assert row["base_etag"] == "srv-etag"
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert client.deletes == [("rem", "h/rem/0")]
    assert client.if_match == ["e0", "srv-etag"]
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None
    wall.close()


def test_update_never_revives_a_queued_delete(conn):
    """An edit to a reminder that is already queued for delete (a stale
    second screen) is refused: it used to turn the delete into an update."""
    _seed_synced_todo(conn)
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")
    assert fdb.queue_cal_object_update(conn, "caldav:rem/t1", _VTODO,
                                       "Buy milk", "t1") is False
    assert fdb.get_cal_object(conn, "caldav:rem/t1")["sync_state"] == "PENDING_DELETE"


def test_same_id_recreated_during_its_create_upload_is_not_marked_synced(conn):
    """Delete then re-add under the same id while the first create uploads.
    The new row must not reuse the uploaded row's revision, or the push
    would mark the new body SYNCED although only the old body reached
    iCloud. It goes out as an update to the URL the upload earned."""
    _queue_new_oops(conn)
    second = remlogic.build_vtodo("U-NEW", "Oops again", _UTC_NOW)
    wall = _wall_conn(conn)

    def delete_then_readd():
        fdb.queue_cal_object_delete(wall, "caldav:rem/U-NEW", "t0b")
        fdb.queue_cal_object_create(wall, {
            "id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
            "comp_type": "VTODO", "uid": "U-NEW", "summary": "Oops again",
            "raw_ics": second}, "t0c")

    client = MidFlightEdit([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                           delete_then_readd)
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row["sync_state"] == "PENDING_UPDATE" and row["raw_ics"] == second
    assert row["href"] == "h/rem/new1"
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert client.server["h/rem/new1"] == second
    wall.close()


def test_a_delete_that_conflicts_itself_keeps_the_phone_edit_and_says_so(
        conn, caplog):
    """A queued delete whose own DELETE hits a 412 (the reminder was edited on
    another device first) follows server-wins: the edited reminder stays, and
    the log says a wall DELETE was dropped, not an edit."""
    _seed_synced_todo(conn)
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t0")
    client = ConflictMidFlight([{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
                               None)
    client.server["h/rem/0"] = _SERVER_NEWER
    with caplog.at_level(logging.WARNING, logger="family_hub"):
        res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert res["conflicts"] == 1
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "SYNCED" and "edited on phone" in row["raw_ics"]
    assert any("delete" in r.getMessage() and "edited on another device"
               in r.getMessage() for r in caplog.records)


def test_a_newer_edit_during_a_conflict_loses_to_the_server_next_round(
        conn, caplog):
    """Round two of the conflicted-edit case: the newer wall edit was built on
    the losing local copy, so pushing it would overwrite the phone's change.
    Its own push then hits the same 412 and server-wins drops it, with a
    warning. Pinned so the docs can say exactly this."""
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    newer = done.replace("SUMMARY:Buy milk", "SUMMARY:Buy oat milk")
    wall = _wall_conn(conn)
    client = ConflictMidFlight(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_update(wall, "caldav:rem/t1", newer,
                                            "Buy oat milk", "t0b"))
    client.server["h/rem/0"] = _SERVER_NEWER
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    with caplog.at_level(logging.WARNING, logger="family_hub"):
        caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    row = fdb.get_cal_object(conn, "caldav:rem/t1")
    assert row["sync_state"] == "SYNCED" and "edited on phone" in row["raw_ics"]
    assert any("server wins" in r.getMessage() for r in caplog.records)
    wall.close()


def test_a_create_conflict_with_no_server_copy_keeps_the_reminder(conn):
    """A create 412s (If-None-Match:*) but the GET then finds nothing there.
    Dropping the row lost a reminder the family just added. It stays queued
    as a create, with the error recorded, and retries next sync."""
    _queue_new_oops(conn)

    class Odd(ConflictMidFlight):
        def get_object(self, collection, href):
            return None

    client = Odd([{"id": "rem", "name": "Groceries", "comp": "VTODO"}], None)
    res = caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    row = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert row is not None, "a brand-new reminder was dropped"
    assert row["sync_state"] == "PENDING_CREATE"
    assert row["sync_attempts"] == 1 and row["last_sync_error"]
    assert len(res["errors"]) == 1


def test_forced_upsert_with_a_revision_never_inserts(conn):
    """The conflict path's forced write names the revision it resolves. If
    the row was deleted on the wall meanwhile, it must not be re-inserted as
    SYNCED (that would bring a deleted reminder back)."""
    wrote = fdb.upsert_cal_object_synced(conn, {
        "id": "caldav:rem/gone", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "gone", "href": "h/x", "etag": "e",
        "summary": "x", "raw_ics": _VTODO, "sequence": 0,
        "last_modified": None}, force=True, expected_rev=3)
    assert wrote is False
    assert fdb.get_cal_object(conn, "caldav:rem/gone") is None


def test_orphan_whose_id_came_back_adopts_the_uploaded_copy(conn):
    """The row was deleted during its create upload and a new row took the
    same id before the cleanup ran. The new row must point at the copy the
    upload made (as an update), not create a second one."""
    _seed_vtodo_collection(conn)
    row = {"id": "caldav:rem/U-NEW", "collection_id": "caldav:rem",
           "comp_type": "VTODO", "uid": "U-NEW", "summary": "Oops",
           "raw_ics": "OLD", "local_rev": 1}
    second = remlogic.build_vtodo("U-NEW", "Oops again", _UTC_NOW)
    fdb.queue_cal_object_create(conn, {**row, "raw_ics": second}, "t0c")
    client = WriteFake([{"id": "rem", "name": "Groceries", "comp": "VTODO"}])
    col = client.discover()[0]
    col = {**col, "id": "rem"}
    caldav_sync._delete_orphaned_upload(client, conn, col, row, "h/rem/new1",
                                        "srv-etag", "t1")
    assert client.deletes == []
    cur = fdb.get_cal_object(conn, "caldav:rem/U-NEW")
    assert cur["sync_state"] == "PENDING_UPDATE" and cur["raw_ics"] == second
    assert (cur["href"], cur["base_etag"]) == ("h/rem/new1", "srv-etag")


def test_forced_upsert_leaves_a_newer_revision_alone(conn):
    """The conflict write names the revision it resolves; a row that moved on
    to a newer revision (a wall edit since) is not overwritten."""
    _seed_synced_todo(conn)
    oid = "caldav:rem/t1"
    newer = _VTODO.replace("Buy milk", "Buy oat milk")
    fdb.queue_cal_object_update(conn, oid, newer, "Buy oat milk", "t0")
    rev = fdb.get_cal_object(conn, oid)["local_rev"]
    wrote = fdb.upsert_cal_object_synced(conn, {
        "id": oid, "collection_id": "caldav:rem", "comp_type": "VTODO",
        "uid": "t1", "href": "h/rem/0", "etag": "srv", "summary": "server",
        "raw_ics": _SERVER_NEWER, "sequence": 1, "last_modified": None},
        force=True, expected_rev=rev - 1)
    assert wrote is False
    row = fdb.get_cal_object(conn, oid)
    assert row["sync_state"] == "PENDING_UPDATE" and row["raw_ics"] == newer


def test_conflicted_update_deleted_meanwhile_and_gone_from_icloud_is_dropped(conn):
    """The wall deletes a reminder while its update 412s, and iCloud no longer
    has it either. Both sides agree it is gone: the row is dropped now, not
    kept to send a pointless DELETE."""
    _seed_synced_todo(conn)
    done = remlogic.set_completed(_VTODO, True, _UTC_NOW)
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", done, "Buy milk", "t0")
    wall = _wall_conn(conn)
    client = ConflictMidFlight(
        [{"id": "rem", "name": "Groceries", "comp": "VTODO"}],
        lambda: fdb.queue_cal_object_delete(wall, "caldav:rem/t1", "t0b"))
    caldav_sync.flush_pending(client, conn, client.discover(), "t1")
    assert fdb.get_cal_object(conn, "caldav:rem/t1") is None
    caldav_sync.flush_pending(client, conn, client.discover(), "t2")
    assert client.deletes == []
    wall.close()


def test_every_queue_path_takes_a_revision_above_all_others(conn):
    """Every queued change takes its revision from the one shared counter, so
    it is above every revision any row holds (an upload in flight holds one).
    A per-row +1 could repeat another row's value, or the value a DELETE in
    flight is holding for this same id."""
    _seed_synced_todo(conn)
    _queue_new_oops(conn)

    def top():
        return max(r["local_rev"] for r in fdb.list_cal_objects(conn))

    def rev(oid):
        return fdb.get_cal_object(conn, oid)["local_rev"]

    def push_other_ahead():
        # the other row edited a few times, so its revision is well past
        # t1's: a per-row +1 on t1 would land at or below it
        for n in range(3):
            fdb.queue_cal_object_update(conn, "caldav:rem/U-NEW", f"X{n}", "Oops", "t")

    push_other_ahead()
    before = top()
    fdb.queue_cal_object_update(conn, "caldav:rem/t1", _VTODO, "Buy milk", "t1")
    assert rev("caldav:rem/t1") > before
    push_other_ahead()
    before = top()
    fdb.queue_cal_object_create(conn, {             # existing id: the upsert path
        "id": "caldav:rem/t1", "collection_id": "caldav:rem",
        "comp_type": "VTODO", "uid": "t1", "summary": "Buy milk",
        "raw_ics": _VTODO}, "t2")
    assert rev("caldav:rem/t1") > before
    push_other_ahead()
    before = top()
    fdb.queue_cal_object_delete(conn, "caldav:rem/t1", "t3")
    assert rev("caldav:rem/t1") > before
    before = top()
    fdb.queue_cal_object_update(conn, "caldav:rem/U-NEW", "X", "Oops", "t4")
    assert rev("caldav:rem/U-NEW") > before


def test_caldav_timed_events_are_stored_in_the_house_time_zone(conn):
    # review 2026-09-22: the same "2:45 class shows 3:45" bug, via iCloud
    from zoneinfo import ZoneInfo
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:t1\r\n"
           "SUMMARY:Class\r\nDTSTART:20260820T170000Z\r\nDTEND:20260820T180000Z\r\n"
           "END:VEVENT\r\nEND:VCALENDAR\r\n")
    client = FakeCalDav([{"id": "abc", "name": "Family", "comp": "VEVENT", "ics": [ics]}])
    now = dt.datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    caldav_sync.sync_once(client, conn, _CFG, now)
    assert fdb.list_events(conn)[0]["start_ts"] == "2026-08-20T10:00:00-07:00"
