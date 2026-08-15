"""Pure to-do visibility and grouping logic. Stdlib only, no I/O.

Conventions:
- A todo dict carries created_at (ISO-8601 UTC), done_at (ISO-8601 UTC or
  None) and done_date (local 'YYYY-MM-DD' or None). The two done fields are
  always set together or cleared together (db.set_todo_done /
  db.clear_todo_done enforce this).
- Done items linger on the main views for the rest of their local done_date,
  then drop off; recent_done keeps a 30-day restore window.
"""
from __future__ import annotations

import datetime as dt

BUCKETS = ("now", "soon", "later")


def is_visible(todo: dict, today: dt.date) -> bool:
    if todo.get("done_at") is None:
        return True
    return todo.get("done_date") == today.isoformat()


def group(todos: list[dict], today: dt.date) -> dict:
    """Visible items by bucket. Within a bucket: open items before done-today
    items, each oldest-first (created_at, then id). Rows with an unrecognized
    bucket are dropped, never crash."""
    out: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for t in todos:
        if is_visible(t, today) and t.get("bucket") in out:
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
