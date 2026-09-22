"""Pure to-do visibility and grouping logic. Stdlib only, no I/O.

Conventions:
- A todo dict carries created_at (ISO-8601 UTC), done_at (ISO-8601 UTC or
  None) and done_date (local 'YYYY-MM-DD' or None). The two done fields are
  always set together or cleared together (db.set_todo_done /
  db.clear_todo_done enforce this).
- A checked-off item stays on the main views, struck through, for
  DONE_GRACE_MIN after it was checked (long enough to see it land and tap it
  again to undo a mis-tap), then drops off into recent_done, the 30-day
  restore window ("recently done" in the full view). It used to linger until
  local midnight, which on a wall read as "checking it off did nothing"
  (operator report, 2026-09-22: an item checked at 10am was still up at
  noon).
"""
from __future__ import annotations

import datetime as dt

BUCKETS = ("now", "soon", "later")


# How long a checked item stays on the main views before it is archived.
DONE_GRACE_MIN = 5


def is_visible(todo: dict, now: dt.datetime) -> bool:
    """Open items always; a done item only inside its grace window. `now` must
    be timezone-aware. An unparseable or naive done_at hides the row (it is
    still in recent_done by its done_date) rather than pinning it forever."""
    done_at = todo.get("done_at")
    if done_at is None:
        return True
    try:
        t = dt.datetime.fromisoformat(done_at)
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        return False
    return now - t < dt.timedelta(minutes=DONE_GRACE_MIN)


def group(todos: list[dict], now: dt.datetime | None = None) -> dict:
    """Visible items by bucket. Within a bucket: open items before
    just-checked items, each oldest-first (created_at, then id). Rows with an
    unrecognized bucket are dropped, never crash."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for t in todos:
        if is_visible(t, now) and t.get("bucket") in out:
            out[t["bucket"]].append(t)
    for b in BUCKETS:
        out[b].sort(key=lambda t: (t["done_at"] is not None,
                                   t["created_at"], t["id"]))
    return out


def recent_done(todos: list[dict], today: dt.date, days: int = 30) -> list[dict]:
    """Done items from the last `days` local days (today inclusive), newest
    done_at first. The restore surface. A hand-edited row with done_date set
    but done_at NULL sorts last instead of crashing the endpoint."""
    lo = (today - dt.timedelta(days=days - 1)).isoformat()
    hi = today.isoformat()
    done = [t for t in todos
            if t.get("done_date") and lo <= t["done_date"] <= hi]
    done.sort(key=lambda t: t.get("done_at") or "", reverse=True)
    return done
