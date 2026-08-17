import datetime as dt

from family_hub import reminders as rem

TODAY = dt.date(2026, 8, 17)


def _r(rid, title, due=None, completed=False):
    return {"id": rid, "title": title, "due": due, "completed": completed,
            "list_id": "caldav:x"}


def test_group_buckets_by_due_and_drops_completed():
    items = [
        _r("1", "Overdue", "2026-08-10"),
        _r("2", "Today", "2026-08-17"),
        _r("3", "Upcoming", "2026-08-25"),
        _r("4", "Someday"),
        _r("5", "Done", "2026-08-17", completed=True),
    ]
    g = rem.group(items, TODAY)
    assert [r["title"] for r in g["overdue"]] == ["Overdue"]
    assert [r["title"] for r in g["today"]] == ["Today"]
    assert [r["title"] for r in g["upcoming"]] == ["Upcoming"]
    assert [r["title"] for r in g["no_date"]] == ["Someday"]
    assert all(all(not x["completed"] for x in g[b]) for b in rem.BUCKETS)


def test_group_sorts_by_due_then_title():
    items = [_r("1", "B", "2026-08-20"), _r("2", "A", "2026-08-20"),
             _r("3", "Z", "2026-08-19")]
    assert [r["title"] for r in rem.group(items, TODAY)["upcoming"]] == ["Z", "A", "B"]


def test_open_count_excludes_completed():
    items = [_r("1", "a"), _r("2", "b", completed=True), _r("3", "c")]
    assert rem.open_count(items) == 2


def test_parse_vtodo_fields():
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\n"
           "UID:u1\r\nSUMMARY:Buy milk\r\nDUE;VALUE=DATE:20260820\r\n"
           "PRIORITY:1\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
    out = rem.parse_vtodo(ics, "caldav:x", "Groceries")
    assert len(out) == 1
    r = out[0]
    assert r["title"] == "Buy milk" and r["id"] == "caldav:x/u1"
    assert r["due"].startswith("2026-08-20") and r["completed"] is False
    assert r["priority"] == 1 and r["list_name"] == "Groceries"


def test_parse_vtodo_completed_and_no_due():
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\n"
           "UID:u2\r\nSUMMARY:Done thing\r\nSTATUS:COMPLETED\r\n"
           "END:VTODO\r\nEND:VCALENDAR\r\n")
    r = rem.parse_vtodo(ics, "caldav:x")[0]
    assert r["completed"] is True and r["due"] is None


def test_parse_vtodo_falls_back_to_dtstart():
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:u3\r\n"
           "SUMMARY:Start dated\r\nDTSTART;VALUE=DATE:20260820\r\n"
           "END:VTODO\r\nEND:VCALENDAR\r\n")
    r = rem.parse_vtodo(ics, "caldav:x")[0]
    assert r["due"].startswith("2026-08-20")   # DUE absent -> DTSTART used
