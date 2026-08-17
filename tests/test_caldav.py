import datetime as dt
from types import SimpleNamespace

from family_hub import caldav_service, caldav_sync
from family_hub import db as fdb


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
        return col.get("ics", [])


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
    cols = fdb.kv_get(conn, "caldav_collections")
    assert cols["caldav:abc"] == {"name": "Family", "color": "#FF0000"}


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
