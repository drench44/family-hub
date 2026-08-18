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


# --- write-side ICS transforms (two-way) ----------------------------------

_NOW = dt.datetime(2026, 8, 17, 15, 30, 0, tzinfo=dt.timezone.utc)
_OPEN = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:u1\r\n"
         "SUMMARY:Buy milk\r\nSTATUS:NEEDS-ACTION\r\nSEQUENCE:2\r\n"
         "END:VTODO\r\nEND:VCALENDAR\r\n")


def test_set_completed_marks_done_and_bumps_sequence():
    ics = rem.set_completed(_OPEN, True, _NOW)
    r = rem.parse_vtodo(ics, "caldav:x")[0]
    assert r["completed"] is True
    assert "STATUS:COMPLETED" in ics
    assert "COMPLETED:20260817T153000Z" in ics    # stamped in UTC
    assert "PERCENT-COMPLETE:100" in ics
    assert "SEQUENCE:3" in ics                     # 2 -> 3 (newer revision)


def test_set_completed_reopen_clears_completed():
    done = rem.set_completed(_OPEN, True, _NOW)
    reopened = rem.set_completed(done, False, _NOW)
    assert "STATUS:NEEDS-ACTION" in reopened
    assert "COMPLETED:" not in reopened            # stale COMPLETED not left behind
    assert rem.parse_vtodo(reopened, "caldav:x")[0]["completed"] is False


def test_set_completed_raises_without_vtodo():
    try:
        rem.set_completed("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
                          True, _NOW)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_vtodo_all_day_due():
    ics = rem.build_vtodo("U-NEW", "Water plants", _NOW, due=dt.date(2026, 8, 20))
    r = rem.parse_vtodo(ics, "caldav:x")[0]
    assert r["title"] == "Water plants" and r["id"] == "caldav:x/U-NEW"
    assert r["completed"] is False and r["due"].startswith("2026-08-20")
    assert "VALUE=DATE:20260820" in ics            # all-day, not a timed DUE


def test_build_vtodo_no_due():
    ics = rem.build_vtodo("U2", "Someday", _NOW)
    r = rem.parse_vtodo(ics, "caldav:x")[0]
    assert r["due"] is None and "DUE" not in ics


def test_build_chore_vtodo_all_day_and_timed():
    from zoneinfo import ZoneInfo
    now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)
    tz = ZoneInfo("America/Los_Angeles")   # PDT = UTC-7
    a = rem.build_chore_vtodo("familyhub-chore-5-2026-08-20", "🧹 Sweep",
                              dt.date(2026, 8, 20), [], now)
    assert "VALUE=DATE:20260820" in a and "VALARM" not in a
    t = rem.build_chore_vtodo("familyhub-chore-6-2026-08-20", "Feed dog",
                              dt.date(2026, 8, 20), ["18:00", "07:00"], now, tz=tz)
    assert "DUE:20260820T140000Z" in t                       # 07:00 PDT -> 14:00Z
    assert "TRIGGER;VALUE=DATE-TIME:20260820T140000Z" in t   # typed absolute UTC
    assert "20260821T010000Z" in t                           # 18:00 PDT -> next-day Z
    assert t.count("BEGIN:VALARM") == 2


def test_build_chore_vtodo_dtstamp_is_utc_from_local_now():
    """Regression: a LOCAL-zone tz-aware now (what the sync tick passes) must
    still stamp DTSTAMP/CREATED in UTC 'Z' — not a bare local TZID iCloud rejects."""
    from zoneinfo import ZoneInfo
    now = dt.datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    ics = rem.build_chore_vtodo("u", "Dishes", dt.date(2026, 8, 20), [], now)
    assert "DTSTAMP:20260817T190000Z" in ics       # 12:00 PDT -> 19:00Z
    assert "CREATED:20260817T190000Z" in ics
    assert "TZID" not in ics
