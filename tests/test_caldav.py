import datetime as dt
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
