import datetime as dt

from family_hub import calendar_sync as cs
from family_hub import db as fdb
from family_hub.config import Config

TIMED_FIXTURE = {
    "id": "b", "summary": "Dentist",
    "start": {"dateTime": "2026-08-13T10:00:00-07:00"},
    "end": {"dateTime": "2026-08-13T11:00:00-07:00"},
}


def make_cfg(calendars=None, window=28):
    return Config(calendars=calendars or [], calendar_window_days=window)


class FakeClient:
    def __init__(self, pages, colors=None):
        self.pages = pages
        self.colors = colors or {}

    def configured(self):
        return True

    def fetch_events(self, cal_id, lo, hi):
        return self.pages[cal_id]

    def fetch_calendar_colors(self):
        return self.colors


def test_normalize_all_day_and_timed_and_cancelled():
    all_day = {"id": "a", "summary": "Camp", "start": {"date": "2026-08-13"},
               "end": {"date": "2026-08-14"}}
    timed = {"id": "b", "summary": "Dentist",
             "start": {"dateTime": "2026-08-13T10:00:00-07:00"},
             "end": {"dateTime": "2026-08-13T11:00:00-07:00"}}
    gone = {"id": "c", "status": "cancelled"}
    assert cs.normalize_event(all_day, "cal")["all_day"] == 1
    assert cs.normalize_event(all_day, "cal")["start_ts"] == "2026-08-13"
    assert cs.normalize_event(timed, "cal")["start_ts"] == "2026-08-13T10:00:00-07:00"
    assert cs.normalize_event(timed, "cal")["all_day"] == 0
    assert cs.normalize_event(gone, "cal") is None
    assert cs.normalize_event({"id": "d", "summary": "x"}, "cal") is None  # no start


def test_normalize_carries_details_and_defaults():
    rich = {"id": "r", "summary": "Party", "colorId": "6",
            "location": "Grandma's", "description": "cake at 3",
            "start": {"dateTime": "2026-08-13T15:00:00-07:00"},
            "end": {"dateTime": "2026-08-13T17:00:00-07:00"}}
    ev = cs.normalize_event(rich, "cal")
    assert (ev["location"], ev["description"], ev["color_id"]) == \
        ("Grandma's", "cake at 3", "6")
    bare = cs.normalize_event(TIMED_FIXTURE, "cal")
    assert (bare["location"], bare["description"], bare["color_id"]) == ("", "", None)


def test_sync_once_stores_and_reports(conn):
    cfg = make_cfg(calendars=[{"id": "cal", "label": "Fam", "color": "#fff", "person": None}])
    n = cs.sync_once(FakeClient({"cal": [TIMED_FIXTURE]}), conn, cfg,
                     dt.datetime(2026, 8, 12, 12))
    assert fdb.list_events(conn)[0]["title"] == "Dentist"
    assert fdb.kv_get(conn, "calendar_status")["ok"] is True
    assert fdb.kv_get(conn, "calendar_status")["events"] == 1
    assert n["ok"] is True


def test_sync_stores_google_calendar_colors(conn):
    cfg = make_cfg(calendars=[{"id": "cal", "label": "Fam", "color": "#fff", "person": None}])
    cs.sync_once(FakeClient({"cal": [TIMED_FIXTURE]}, colors={"cal": "#9FE1E7"}),
                 conn, cfg, dt.datetime(2026, 8, 12, 12))
    assert fdb.kv_get(conn, "calendar_colors") == {"cal": "#9FE1E7"}


def test_sync_survives_client_without_color_support(conn):
    """A client (or a Google hiccup) with no calendarList colors must not
    break the event sync — colors just fall back to config."""
    class NoColors:
        def configured(self):
            return True

        def fetch_events(self, *a):
            return [TIMED_FIXTURE]

    cfg = make_cfg(calendars=[{"id": "cal", "label": "Fam", "color": "#fff", "person": None}])
    st = cs.sync_once(NoColors(), conn, cfg, dt.datetime(2026, 8, 12, 12))
    assert st["ok"] is True and fdb.list_events(conn)[0]["title"] == "Dentist"


def test_sync_failure_reports_not_raises(conn):
    class Boom:
        def configured(self):
            return True

        def fetch_events(self, *a):
            raise RuntimeError("quota")

    cs.sync_once(Boom(), conn,
                 make_cfg(calendars=[{"id": "cal", "label": "f", "color": "#fff", "person": None}]),
                 dt.datetime(2026, 8, 12))
    st = fdb.kv_get(conn, "calendar_status")
    assert st["ok"] is False and "quota" in st["error"]


def test_sync_flags_needs_auth_on_revoked_token(conn):
    """A revoked/expired Google token surfaces as needs_auth=True so the wall
    can tell the owner to re-run setup, instead of the vague 'hit a snag'.
    _is_auth_error keys on the exception CLASS NAME (RefreshError), matching how
    google.auth raises, so this stand-in exercises the real detection path."""
    class RefreshError(Exception):
        pass

    class Revoked:
        def configured(self):
            return True

        def fetch_events(self, *a):
            raise RefreshError("Token has been expired or revoked.")

    cfg = make_cfg(calendars=[{"id": "cal", "label": "Fam", "kind": "google"}])
    st = cs.sync_once(Revoked(), conn, cfg, dt.datetime(2026, 8, 12))
    assert st["ok"] is False
    assert st.get("needs_auth") is True


def test_sync_transient_error_is_not_needs_auth(conn):
    """A plain network/quota error must NOT set needs_auth (that would wrongly
    tell the owner to re-authorize for a passing blip)."""
    class Boom:
        def configured(self):
            return True

        def fetch_events(self, *a):
            raise RuntimeError("temporary quota exceeded")

    cfg = make_cfg(calendars=[{"id": "cal", "label": "Fam", "kind": "google"}])
    st = cs.sync_once(Boom(), conn, cfg, dt.datetime(2026, 8, 12))
    assert st["ok"] is False
    assert st.get("needs_auth") is not True


def test_unconfigured_client_reports_needs_auth(conn):
    class NoTok:
        def configured(self):
            return False

    cs.sync_once(NoTok(), conn, make_cfg(calendars=[]), dt.datetime(2026, 8, 12))
    assert fdb.kv_get(conn, "calendar_status")["ok"] is False


def test_sync_preserves_prior_last_sync_on_failure(conn):
    fdb.kv_set(conn, "calendar_status",
               {"ok": True, "last_sync": "2026-08-11T00:00:00", "events": 3})

    class Boom:
        def configured(self):
            return True

        def fetch_events(self, *a):
            raise RuntimeError("boom")

    cs.sync_once(Boom(), conn,
                 make_cfg(calendars=[{"id": "cal", "label": "f", "color": "#fff", "person": None}]),
                 dt.datetime(2026, 8, 12))
    st = fdb.kv_get(conn, "calendar_status")
    assert st["ok"] is False and st["last_sync"] == "2026-08-11T00:00:00"


# ------------------------------------------------------------- ICS sources

ICS_FIXTURE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:STANDARD
DTSTART:19701101T020000
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700308T020000
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:soccer@test
SUMMARY:Soccer practice
LOCATION:THPRD field
DTSTART;TZID=America/Los_Angeles:20260810T170000
DTEND;TZID=America/Los_Angeles:20260810T180000
RRULE:FREQ=WEEKLY;BYDAY=MO
END:VEVENT
BEGIN:VEVENT
UID:camping@test
SUMMARY:Camping
DTSTART;VALUE=DATE:20260821
DTEND;VALUE=DATE:20260824
END:VEVENT
END:VCALENDAR
"""


def test_ics_events_expand_recurrence_and_all_day():
    evs = cs.ics_events(ICS_FIXTURE, "ical",
                        dt.date(2026, 8, 10), dt.date(2026, 8, 31))
    soccer = [e for e in evs if e["title"] == "Soccer practice"]
    # weekly Mondays inside the window: 8/10, 8/17, 8/24, 8/31
    assert [e["start_ts"][:10] for e in soccer] == \
        ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"]
    assert all(e["all_day"] == 0 for e in soccer)
    assert soccer[0]["location"] == "THPRD field"
    assert "17:00" in soccer[0]["start_ts"]
    # every expanded occurrence keeps a unique id (shared UID + its own start)
    assert len({e["id"] for e in soccer}) == len(soccer)
    camping = [e for e in evs if e["title"] == "Camping"][0]
    assert camping["all_day"] == 1
    assert (camping["start_ts"], camping["end_ts"]) == ("2026-08-21", "2026-08-24")


def test_ics_webcal_scheme_is_https():
    assert cs._ics_https("webcal://p.example/cal.ics") == "https://p.example/cal.ics"
    assert cs._ics_https("https://p.example/cal.ics") == "https://p.example/cal.ics"


class NotConfiguredClient:
    def configured(self):
        return False


def test_sync_mixes_ics_with_unconfigured_google(conn):
    """An ICS-only (or Apple-only) family needs no Google token: ICS feeds
    sync while the Google source just reports 'not configured'."""
    cfg = make_cfg([
        {"id": "gcal", "label": "G", "kind": "google"},
        {"id": "ical", "label": "Apple", "kind": "ics",
         "url": "webcal://p.example/cal.ics"},
    ])
    st = cs.sync_once(NotConfiguredClient(), conn, cfg,
                      dt.datetime(2026, 8, 13, 9, 0),
                      ics_fetch=lambda url: ICS_FIXTURE)
    assert st["ok"] is False and "not configured" in st["error"]
    titles = {e["title"] for e in fdb.list_events(conn)}
    assert "Soccer practice" in titles and "Camping" in titles


def test_sync_ics_failure_keeps_last_good_events(conn):
    """A dead feed must not wipe its cached events while other sources sync."""
    cfg = make_cfg([
        {"id": "feed_a", "label": "A", "kind": "ics", "url": "https://a/x.ics"},
        {"id": "feed_b", "label": "B", "kind": "ics", "url": "https://b/x.ics"},
    ])
    fdb.replace_events(conn, [{
        "id": "old", "calendar_id": "feed_b", "title": "Kept",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1,
        "updated": None}])

    def fetch(url):
        if "//b/" in url:
            raise RuntimeError("feed down")
        return ICS_FIXTURE

    st = cs.sync_once(NotConfiguredClient(), conn, cfg,
                      dt.datetime(2026, 8, 13, 9, 0), ics_fetch=fetch)
    assert st["ok"] is False and "B: feed down" in st["error"]
    by_cal = {}
    for e in fdb.list_events(conn):
        by_cal.setdefault(e["calendar_id"], []).append(e["title"])
    assert "Kept" in by_cal["feed_b"]          # last-good survived
    assert "Camping" in by_cal["feed_a"]       # healthy feed refreshed


EMPTY_ICS = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\n"
             b"END:VCALENDAR\r\n")   # syntactically valid, zero events (parses, does NOT raise)


def test_sync_valid_empty_keeps_last_good_and_flags(conn):
    """The empty-feed-wipe fix, exercised through the REAL sync_once + real DB:
    a source that PREVIOUSLY had cached events but returns a valid EMPTY result
    (an empty VCALENDAR during maintenance, or Google items:[]) — no exception —
    must NOT silently wipe its cached events. Keep last-good and flag it."""
    cfg = make_cfg([
        {"id": "feed_a", "label": "A", "kind": "ics", "url": "https://a/x.ics"},
        {"id": "feed_b", "label": "School", "kind": "ics", "url": "https://b/x.ics"},
    ])
    fdb.replace_events(conn, [
        {"id": "a1", "calendar_id": "feed_a", "title": "OldA",
         "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None},
        {"id": "b1", "calendar_id": "feed_b", "title": "SchoolDay",
         "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None},
    ])

    def fetch(url):
        if "//b/" in url:
            return EMPTY_ICS          # valid-but-empty, no exception raised
        return ICS_FIXTURE            # feed_a refreshes normally

    st = cs.sync_once(NotConfiguredClient(), conn, cfg,
                      dt.datetime(2026, 8, 13, 9, 0), ics_fetch=fetch)
    by_cal = {}
    for e in fdb.list_events(conn):
        by_cal.setdefault(e["calendar_id"], []).append(e["title"])
    assert "SchoolDay" in by_cal.get("feed_b", []), "empty feed must keep last-good, not wipe"
    assert "Camping" in by_cal.get("feed_a", []), "healthy feed still refreshed"
    assert st["ok"] is False, "a suspicious empty must flip status ok=False"
    assert "School:" in st["error"] and "no events" in st["error"]


def test_sync_genuinely_empty_source_without_cache_is_not_flagged(conn):
    """A source that was never populated and returns empty is genuinely empty:
    no error, no false 'kept last-synced' warning."""
    cfg = make_cfg([{"id": "feed", "label": "F", "kind": "ics", "url": "https://f/x.ics"}])
    st = cs.sync_once(NotConfiguredClient(), conn, cfg,
                      dt.datetime(2026, 8, 13, 9, 0), ics_fetch=lambda u: EMPTY_ICS)
    assert st["ok"] is True
    assert fdb.list_events(conn) == []


def test_sync_empty_beyond_ttl_finally_clears(conn):
    """After _EMPTY_KEEP_HOURS of CONTINUOUS emptiness, a genuinely-emptied
    source is allowed to clear instead of showing stale events forever."""
    cfg = make_cfg([{"id": "feed", "label": "F", "kind": "ics", "url": "https://f/x.ics"}])
    fdb.replace_events(conn, [{
        "id": "old", "calendar_id": "feed", "title": "Stale",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None}])
    now = dt.datetime(2026, 8, 13, 9, 0)
    # it first went empty 25h ago — past the 24h keep window
    fdb.kv_set(conn, "calendar_empty_since",
               {"feed": (now - dt.timedelta(hours=25)).isoformat()})
    cs.sync_once(NotConfiguredClient(), conn, cfg, now, ics_fetch=lambda u: EMPTY_ICS)
    assert fdb.list_events(conn) == []                     # finally cleared
    assert "feed" not in (fdb.kv_get(conn, "calendar_empty_since") or {})


def test_sync_empty_within_ttl_still_keeps(conn):
    """Within the keep window a suspicious empty is still kept (rides out a
    maintenance blip)."""
    cfg = make_cfg([{"id": "feed", "label": "F", "kind": "ics", "url": "https://f/x.ics"}])
    fdb.replace_events(conn, [{
        "id": "old", "calendar_id": "feed", "title": "Kept",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None}])
    now = dt.datetime(2026, 8, 13, 9, 0)
    fdb.kv_set(conn, "calendar_empty_since",
               {"feed": (now - dt.timedelta(hours=2)).isoformat()})   # only 2h empty
    cs.sync_once(NotConfiguredClient(), conn, cfg, now, ics_fetch=lambda u: EMPTY_ICS)
    assert [e["title"] for e in fdb.list_events(conn)] == ["Kept"]


def test_sync_empty_ttl_with_tz_aware_now(conn):
    """Production always passes a tz-AWARE `now` (dt.datetime.now(TZ)); the
    empty-since round-trip and TTL must work with aware datetimes, not just the
    naive ones the other TTL tests use. Pins the real production path."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Los_Angeles")
    cfg = make_cfg([{"id": "feed", "label": "F", "kind": "ics", "url": "https://f/x.ics"}])
    fdb.replace_events(conn, [{
        "id": "old", "calendar_id": "feed", "title": "Stale",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None}])
    now = dt.datetime(2026, 8, 13, 9, 0, tzinfo=tz)
    fdb.kv_set(conn, "calendar_empty_since",
               {"feed": (now - dt.timedelta(hours=25)).isoformat()})
    cs.sync_once(NotConfiguredClient(), conn, cfg, now, ics_fetch=lambda u: EMPTY_ICS)
    assert fdb.list_events(conn) == []   # aware-datetime round-trip -> TTL still fires


def test_sync_prunes_empty_since_for_removed_calendars(conn):
    """empty_since entries for calendars no longer in config are pruned, so the
    kv can't grow unbounded (the loop only visits current cfg.calendars)."""
    cfg = make_cfg([{"id": "keep", "label": "K", "kind": "ics", "url": "https://k/x.ics"}])
    fdb.replace_events(conn, [{
        "id": "k1", "calendar_id": "keep", "title": "K",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1, "updated": None}])
    fdb.kv_set(conn, "calendar_empty_since", {
        "keep": dt.datetime(2026, 8, 13, 8).isoformat(),
        "gone": dt.datetime(2026, 8, 13, 8).isoformat()})   # 'gone' not in config
    cs.sync_once(NotConfiguredClient(), conn, cfg, dt.datetime(2026, 8, 13, 9),
                 ics_fetch=lambda u: EMPTY_ICS)
    es = fdb.kv_get(conn, "calendar_empty_since") or {}
    assert "gone" not in es     # pruned
    assert "keep" in es         # still-configured empty source retains its clock


def test_sync_recovery_resets_empty_clock(conn):
    """A source that returns events again clears its empty-since clock, so a
    LATER empty starts the TTL fresh rather than inheriting the old age."""
    cfg = make_cfg([{"id": "ical", "label": "F", "kind": "ics", "url": "https://f/x.ics"}])
    now = dt.datetime(2026, 8, 13, 9, 0)
    fdb.kv_set(conn, "calendar_empty_since",
               {"ical": (now - dt.timedelta(hours=30)).isoformat()})
    cs.sync_once(NotConfiguredClient(), conn, cfg, now, ics_fetch=lambda u: ICS_FIXTURE)
    assert "ical" not in (fdb.kv_get(conn, "calendar_empty_since") or {})


def test_sync_total_failure_leaves_cache_untouched(conn):
    cfg = make_cfg([{"id": "feed", "label": "F", "kind": "ics",
                     "url": "https://f/x.ics"}])
    fdb.replace_events(conn, [{
        "id": "old", "calendar_id": "feed", "title": "Kept",
        "start_ts": "2026-08-14", "end_ts": "2026-08-15", "all_day": 1,
        "updated": None}])

    def fetch(url):
        raise RuntimeError("everything down")

    st = cs.sync_once(NotConfiguredClient(), conn, cfg,
                      dt.datetime(2026, 8, 13, 9, 0), ics_fetch=fetch)
    assert st["ok"] is False
    assert [e["title"] for e in fdb.list_events(conn)] == ["Kept"]
