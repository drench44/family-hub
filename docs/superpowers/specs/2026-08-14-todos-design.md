# To-Do List — Design Spec

Date: 2026-08-14
Status: approved by operator (wall surface, attribution, done-behavior choices
confirmed 2026-08-14)

## Summary

A single shared household to-do list for items that don't belong to the
scheduled chore system: anyone can add items, anyone can check them off, and
open items carry over day to day until done. Items are grouped into three
buckets: **Now / Soon / Later**. Checked items stay visible struck-through
until the end of the local day, then drop off the main views; a
"recently done" view (last 30 days) allows restoring anything checked off by
mistake.

Decisions locked with the operator:

- **Wall surface:** compact To-Do card on the wall (top *Now* items) with a
  tap-to-open full-screen overlay; phones get a fifth bottom tab.
- **Attribution:** none. No people linkage anywhere in the feature — no
  "claimed by", no "who completed it".
- **Done behavior:** linger struck-through until end of day, then hide;
  restorable from a 30-day recently-done list.

## Non-goals (explicitly out of scope)

- Due dates, reminders, or notifications. The buckets replace dates.
- People linkage of any kind.
- Manual drag-reorder / a `sort` column. Ordering is fixed (see below) until
  someone asks otherwise.
- `admin.html` changes. All management (add, rename, move, delete, restore)
  happens in the hub UI itself.
- Purging old done rows. The table stays tiny at household scale; done rows
  are kept indefinitely so restore always works.

## Storage (`db.py`)

New table appended to `SCHEMA` (`CREATE TABLE IF NOT EXISTS` is sufficient for
existing deployments; `ensure_schema` needs no new migration logic):

```sql
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  bucket TEXT NOT NULL CHECK(bucket IN ('now','soon','later')),
  created_at TEXT NOT NULL,   -- ISO-8601 UTC
  done_at TEXT,               -- ISO-8601 UTC; NULL = open
  done_date TEXT              -- local 'YYYY-MM-DD' at completion time
);
```

`done_date` is computed server-side from the app's `TZ` at the moment of
completion and drives the "linger until end of day" rule; `done_at` orders the
recently-done list. Both are NULL for open items and always set/cleared
together.

New `db.py` functions, following the existing plain-function style
(`conn` first arg, `dict` rows, `conn.commit()` per write):

- `add_todo(conn, title, bucket) -> int` — sets `created_at` to now (UTC ISO).
- `list_todos(conn) -> list[dict]` — all rows, ordered `created_at, id`.
  (Bucket grouping and visibility are the pure module's job, not SQL's.)
- `update_todo(conn, tid, **fields)` — allowlisted columns
  `{title, bucket}`, same pattern as `update_person` / `update_chore`.
- `set_todo_done(conn, tid, done_at, done_date)` and
  `clear_todo_done(conn, tid)` — set/clear both done columns together.
- `delete_todo(conn, tid)` — hard-deletes the row.

## Pure logic (`todos.py`, new module)

Mirrors `chores.py`: stdlib only, no I/O, fully unit-testable. Conventions
documented in the module docstring.

- `is_visible(todo, today: dt.date) -> bool` — open items are always visible;
  done items are visible only while `done_date == today.isoformat()`.
- `group(todos, today) -> dict` — returns
  `{"now": [...], "soon": [...], "later": [...]}` containing only visible
  items, each bucket ordered oldest-first (`created_at`, then `id` as
  tiebreaker) so long-lived items surface instead of sinking. Done-today items
  sort after open items within their bucket.
- `recent_done(todos, today, days=30) -> list` — done items with
  `done_date` within the last `days` local days, newest `done_at` first.

## API (`app.py`)

Same conventions as the chores routes: pydantic request models, `HTTPException`
422s with human-readable messages, helper `_todo_row(c, tid)` raising 404 for
unknown ids (mirroring `_person_row` / `_chore_row`).

Validation: `title` stripped, 1–120 characters; `bucket` in
`{"now", "soon", "later"}`.

- `GET /api/todos` → `{"buckets": {"now": [...], "soon": [...], "later": [...]},
  "recent_done": [...]}` — the full-screen / phone-tab payload.
- `POST /api/todos` body `{title, bucket="now"}` → the created row.
- `PATCH /api/todos/{id}` body `{title?, bucket?}` → the updated row. Bucket
  moves are just this PATCH. Works on done items too (a restored item keeps
  edits).
- `POST /api/todos/{id}/complete` → `{"ok": true}`. Sets `done_at` (UTC now)
  and `done_date` (local today via `_today()`). Idempotent: re-completing an
  already-done item refreshes both fields.
- `DELETE /api/todos/{id}/complete` → `{"ok": true}`. Clears both done fields
  (this is both "undo a mis-tap" and "restore from recently done"). No-op on
  an open item.
- `DELETE /api/todos/{id}` → `{"ok": true}`. Removes the row (open or done).

`/api/hub` gains one key so neither surface needs extra polling:

```
"todos": {"now": [...], "soon": [...], "later": [...]}
```

(visible items only, same grouping as `GET /api/todos`, no `recent_done` —
the wall card and phone tab render from this; the overlay fetches
`GET /api/todos` on open for the recently-done list).

## Wall UI (`index.html`, `hub.js`, `styles.css`)

- **Compact card** at the bottom of the chores column (`#people`), rendered on
  every hub poll like the person cards: header "To-Do", up to 5 *Now* items
  with one-tap check-off circles, then a single quiet count line for the rest
  (e.g. "3 soon · 2 later") when nonzero. Checked items render struck-through
  in place. Empty state is one calm line ("nothing on the list") — the card
  always renders so the surface is discoverable.
- **Full-screen overlay** opened by tapping the card header, using the
  existing overlay pattern (`openOverlay` / rendered content, not an iframe,
  same as the chores full view): three bucket columns (stacked on phones), an
  add input + bucket choice at the top, and per-item actions revealed by
  tapping the item row (move to Now/Soon/Later, delete). A collapsed
  "recently done" section at the bottom lists the last 30 days with a restore
  action per item.
- Check-off follows the existing chore check-off interaction pattern
  (optimistic toggle + POST + refresh on next poll), including the undo path
  (tapping a struck item un-completes it).
- Night-dim and the fails-soft conventions apply unchanged; a failed POST
  falls back to the server state on the next poll rather than erroring loudly.
- Bump every `?v=` cache-buster in both HTML files (currently 27) in the final
  change that touches the static assets.

## Phone UI

Fifth bottom tab in `index.html`'s tab bar: `✅ To-Dos` (`data-tab="todos"`).
The tab shows the full list (same content as the overlay: add input, three
bucket groups stacked, tap-to-reveal item actions, collapsed recently-done).
CSS keeps the tab bar usable at five tabs on narrow phones. The tab renders
from the `/api/hub` payload it already polls; recently-done loads on first
expand via `GET /api/todos`.

## Error handling

- Unknown todo id → 404 on PATCH / complete / uncomplete / delete.
- Invalid title/bucket → 422 with a plain message.
- The wall never blanks over a todos problem: if the `todos` key is missing or
  malformed in a hub payload, the card renders its empty state (consistent
  with the wall's fails-soft rule).

## Testing

- `tests/test_todos.py` — pure-logic unit tests: visibility across day
  boundaries, bucket grouping and ordering (oldest-first, done-today sorting
  after open), recent-done windowing and ordering.
- `tests/test_api.py` additions — CRUD round-trip, validation 422s, unknown-id
  404s, complete/undo/restore lifecycle including `done_date` behavior across
  a mocked day change, `/api/hub` containing the `todos` block, delete.
- `tests/test_db.py` additions — schema creation on a fresh DB and on an
  existing pre-todos DB file (ensure_schema idempotence).
- JS helper coverage per the existing `tests/js` pattern for any new pure
  frontend helpers (grouping/render helpers).
- Repo gate: the three review agents (`silent-failure-hunter`,
  `code-reviewer`, `pr-test-analyzer`) must run on the branch diff before
  merge, per CLAUDE.md.
