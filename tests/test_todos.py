import datetime as dt

from family_hub import todos as td

TODAY = dt.date(2026, 8, 14)


def _t(i, bucket="now", created="2026-08-01T10:00:00+00:00",
       done_at=None, done_date=None, title=None):
    return {"id": i, "title": title or f"item {i}", "bucket": bucket,
            "created_at": created, "done_at": done_at, "done_date": done_date}


def test_open_items_are_always_visible():
    assert td.is_visible(_t(1), TODAY) is True


def test_done_today_is_visible_done_yesterday_is_not():
    today = _t(1, done_at="2026-08-14T15:00:00+00:00", done_date="2026-08-14")
    yesterday = _t(2, done_at="2026-08-13T15:00:00+00:00", done_date="2026-08-13")
    assert td.is_visible(today, TODAY) is True
    assert td.is_visible(yesterday, TODAY) is False


def test_group_buckets_and_hides_old_done():
    items = [
        _t(1, "now"),
        _t(2, "soon"),
        _t(3, "later"),
        _t(4, "now", done_at="2026-08-13T15:00:00+00:00", done_date="2026-08-13"),
    ]
    g = td.group(items, TODAY)
    assert [t["id"] for t in g["now"]] == [1]
    assert [t["id"] for t in g["soon"]] == [2]
    assert [t["id"] for t in g["later"]] == [3]


def test_group_orders_open_oldest_first_then_done_today():
    items = [
        _t(1, "now", created="2026-08-10T00:00:00+00:00"),
        _t(2, "now", created="2026-08-02T00:00:00+00:00"),
        _t(3, "now", created="2026-08-01T00:00:00+00:00",
           done_at="2026-08-14T09:00:00+00:00", done_date="2026-08-14"),
    ]
    g = td.group(items, TODAY)
    # open items oldest-first (2 before 1); done-today (3) sorts last
    assert [t["id"] for t in g["now"]] == [2, 1, 3]


def test_group_orders_same_created_at_ties_by_id():
    items = [
        _t(2, "now", created="2026-08-01T00:00:00+00:00"),
        _t(1, "now", created="2026-08-01T00:00:00+00:00"),
    ]
    g = td.group(items, TODAY)
    assert [t["id"] for t in g["now"]] == [1, 2]


def test_group_ignores_unknown_bucket_rows():
    # a hand-edited DB row must not crash the wall
    bad = _t(1)
    bad["bucket"] = "someday"
    g = td.group([bad, _t(2, "soon")], TODAY)
    assert [t["id"] for t in g["soon"]] == [2]
    assert g["now"] == []


def test_recent_done_window_is_30_days_today_inclusive():
    # days=30, TODAY=2026-08-14: the window edge is 29 days back (2026-07-16
    # inclusive); one day further back falls outside it.
    edge = _t(1, done_at="2026-07-16T09:00:00+00:00", done_date="2026-07-16")
    outside = _t(2, done_at="2026-07-15T09:00:00+00:00", done_date="2026-07-15")
    out = td.recent_done([edge, outside], TODAY, days=30)
    assert [t["id"] for t in out] == [1]


def test_recent_done_tolerates_done_date_without_done_at():
    # a hand-edited row: done_date set but done_at NULL must not crash the
    # sort, and sorts after normal (done_at-bearing) rows.
    normal = _t(1, done_at="2026-08-14T09:00:00+00:00", done_date="2026-08-14")
    broken = _t(2, done_at=None, done_date="2026-08-13")
    out = td.recent_done([normal, broken], TODAY)
    assert [t["id"] for t in out] == [1, 2]


def test_recent_done_windows_and_orders_newest_first():
    items = [
        _t(1, done_at="2026-08-13T08:00:00+00:00", done_date="2026-08-13"),
        _t(2, done_at="2026-08-14T09:00:00+00:00", done_date="2026-08-14"),
        _t(3, done_at="2026-07-01T09:00:00+00:00", done_date="2026-07-01"),  # 44 days ago
        _t(4),  # open item, never listed
    ]
    out = td.recent_done(items, TODAY)
    assert [t["id"] for t in out] == [2, 1]
