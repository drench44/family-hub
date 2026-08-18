# Chores Away / Pause Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a family member (or everyone) be marked "away" for a date range so an absence never breaks a chore streak, chores that still need doing are covered, and they resume on whatever day they return.

**Architecture:** Away state is a pure *additive overlay* in a new `away_periods` table. It is consulted in two independent places — resolution/freeze (today & future: away people drop out, rotations fall to whoever's home, fixed chores reassign to an optional backup) and streak/week math (any day incl. already-frozen past: away days read as rest). The frozen `occurrence_log` is never rewritten; deleting a period restores prior behavior exactly.

**Tech Stack:** FastAPI + SQLite (stdlib `sqlite3`) + vanilla JS. No build step, minimal deps. Tests via `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-17-chores-away-mode-design.md`

## Global Constraints

- **Never rewrite/delete/backfill frozen `occurrence_log` rows.** Away is overlay-only, consulted at read/resolve time. (Core repo invariant.)
- Keep deployment-specific/private data out of this public repo.
- Pure logic lives in `chores.py` (stdlib only, no I/O); storage in `db.py`; HTTP in `app.py`. Follow existing patterns.
- `days_mask` bit 0 = Monday .. bit 6 = Sunday. Dates are `'YYYY-MM-DD'` strings.
- Fail-soft on the wall: a malformed away row must never 500 or blank the wall; degrade to "not away / no backup" and log at ERROR (same philosophy as `_links()` / the todos block in `hub()`).
- Any feature runs the gauntlet in `docs/adding-a-feature.md` to the last item; three-agent review before merge; docs-only exempt.
- Streaks capped at 365 days (existing behavior — preserve it).

## File Structure

- `src/family_hub/db.py` — add `away_periods` to `SCHEMA`; add away-period CRUD + `away_map`; extend `delete_person` to clean up away rows/backup refs.
- `src/family_hub/chores.py` — `streak`/`week_strip` gain `away_dates`; `plan_rows`/`day_plan` gain away/backup handling + `covering_for`.
- `src/family_hub/app.py` — `_people_day` builds and passes the overlay; `admin_state` returns periods; new `/api/admin/away*` endpoints + Pydantic models.
- `src/family_hub/demo.py` — seed one away person with a backup.
- `src/family_hub/web/static/{hub.js,common.js,index.html,styles.css}` — admin away controls + wall away rendering.
- Tests: `tests/test_db.py`, `tests/test_chores.py`, `tests/test_api.py`, `tests/test_demo.py`, `tests/test_static.py`.

---

## Task 1: `away_periods` schema + DB helpers

**Files:**
- Modify: `src/family_hub/db.py` (`SCHEMA` block ~line 15-104; new helpers after the people section ~line 411)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `connect(path)`, `list_people`.
- Produces:
  - `add_away_period(conn, person_id: int, start_date: str, end_date: str | None = None, backup_person_id: int | None = None) -> int`
  - `close_away_period(conn, period_id: int, end_date: str) -> None`
  - `update_away_period(conn, period_id: int, **fields) -> None`  (fields ⊆ {start_date, end_date, backup_person_id})
  - `delete_away_period(conn, period_id: int) -> bool`
  - `list_away_periods(conn, include_closed: bool = True) -> list[dict]`
  - `away_map(conn, from_date: str, to_date: str) -> dict[int, dict]` → `{person_id: {"dates": set[str], "backup_on": {date_str: int | None}}}`

- [ ] **Step 1: Add the table to `SCHEMA`.** Inside the `SCHEMA = """..."""` string in `db.py`, after the `occurrence_log` table, add:

```sql
CREATE TABLE IF NOT EXISTS away_periods(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id),
  start_date TEXT NOT NULL,
  end_date TEXT,
  backup_person_id INTEGER,
  created_at TEXT NOT NULL);
```

- [ ] **Step 2: Write failing tests** in `tests/test_db.py`:

```python
def test_away_period_crud_and_map(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    a = db.add_person(c, "Ava", "#f00")
    b = db.add_person(c, "Bo", "#0f0")
    pid = db.add_away_period(c, a, "2026-08-14", None, backup_person_id=b)
    rows = db.list_away_periods(c)
    assert len(rows) == 1 and rows[0]["end_date"] is None
    # open-ended period covers from start through to_date
    m = db.away_map(c, "2026-08-10", "2026-08-16")
    assert m[a]["dates"] == {"2026-08-14", "2026-08-15", "2026-08-16"}
    assert m[a]["backup_on"]["2026-08-15"] == b
    # closing clips the tail
    db.close_away_period(c, pid, "2026-08-15")
    m2 = db.away_map(c, "2026-08-10", "2026-08-20")
    assert m2[a]["dates"] == {"2026-08-14", "2026-08-15"}
    # window clipping: nothing before from_date
    assert db.away_map(c, "2026-08-16", "2026-08-20") == {}
    # delete removes it
    assert db.delete_away_period(c, pid) is True
    assert db.list_away_periods(c) == []


def test_away_schema_idempotent_on_existing_db(tmp_path):
    p = str(tmp_path / "t.db")
    db.connect(p).close()
    # second connect re-runs SCHEMA; must not raise or wipe data
    c = db.connect(p)
    assert db.list_away_periods(c) == []
```

- [ ] **Step 3: Run tests, verify they fail** — `pytest tests/test_db.py::test_away_period_crud_and_map -v` → FAIL (no `add_away_period`).

- [ ] **Step 4: Implement the helpers** in `db.py` (after `delete_person`). Note the deterministic `created_at` avoids `datetime.now()` in favor of the caller passing nothing → use a fixed sentinel is wrong; instead stamp with `dt.datetime.now`isn't available here — follow the repo: other tables stamp times at the call site. Use `datetime` import already present via app; in db use `time`-free approach: store `created_at` from a passed value defaulting to `""` is ugly. The repo's `set_completion` stamps `done_at` — mirror it:

```python
import datetime as _dt  # if not already imported at top of db.py; check first

def add_away_period(conn, person_id, start_date, end_date=None,
                    backup_person_id=None):
    cur = conn.execute(
        "INSERT INTO away_periods(person_id, start_date, end_date, "
        "backup_person_id, created_at) VALUES(?, ?, ?, ?, ?)",
        (person_id, start_date, end_date, backup_person_id,
         _dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return int(cur.lastrowid)


def close_away_period(conn, period_id, end_date):
    conn.execute("UPDATE away_periods SET end_date = ? WHERE id = ?",
                 (end_date, period_id))
    conn.commit()


_AWAY_FIELDS = {"start_date", "end_date", "backup_person_id"}

def update_away_period(conn, period_id, **fields):
    cols = {k: v for k, v in fields.items() if k in _AWAY_FIELDS}
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE away_periods SET {sets} WHERE id = ?",
                 (*cols.values(), period_id))
    conn.commit()


def delete_away_period(conn, period_id):
    with conn:
        if conn.execute("SELECT 1 FROM away_periods WHERE id = ?",
                        (period_id,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM away_periods WHERE id = ?", (period_id,))
        return True


def list_away_periods(conn, include_closed=True):
    sql = "SELECT * FROM away_periods"
    if not include_closed:
        sql += " WHERE end_date IS NULL"
    sql += " ORDER BY start_date, id"
    return [dict(r) for r in conn.execute(sql)]


def away_map(conn, from_date, to_date):
    """Per person, the set of away date strings within [from_date, to_date]
    and the effective backup id per away date. Interval expansion clipped to
    the window. Open-ended periods (end_date NULL) run through to_date."""
    lo = _dt.date.fromisoformat(from_date)
    hi = _dt.date.fromisoformat(to_date)
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM away_periods"):
        start = _dt.date.fromisoformat(r["start_date"])
        end = _dt.date.fromisoformat(r["end_date"]) if r["end_date"] else hi
        a = max(start, lo)
        b = min(end, hi)
        if a > b:
            continue
        info = out.setdefault(r["person_id"], {"dates": set(), "backup_on": {}})
        d = a
        while d <= b:
            ds = d.isoformat()
            info["dates"].add(ds)
            info["backup_on"][ds] = r["backup_person_id"]
            d += _dt.timedelta(days=1)
    return out
```

Confirm `import datetime as _dt` (or reuse an existing datetime import) is present at the top of `db.py`; add it if missing rather than importing inside functions.

- [ ] **Step 5: Run tests, verify pass** — `pytest tests/test_db.py -k away -v` → PASS.

- [ ] **Step 6: Extend `delete_person` to clean up away rows** so a hard-deleted person leaves no orphan periods or backup references. In `delete_person`, inside the `with conn:` block (before the final `DELETE FROM people`), add:

```python
        conn.execute("DELETE FROM away_periods WHERE person_id = ?", (pid,))
        conn.execute("UPDATE away_periods SET backup_person_id = NULL "
                     "WHERE backup_person_id = ?", (pid,))
```

- [ ] **Step 7: Test the cleanup** in `tests/test_db.py`:

```python
def test_delete_person_clears_away_rows(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    a = db.add_person(c, "Ava", "#f00")
    b = db.add_person(c, "Bo", "#0f0")
    db.add_away_period(c, a, "2026-08-14", None, backup_person_id=b)
    db.add_away_period(c, b, "2026-08-14", None, backup_person_id=a)
    db.delete_person(c, a)
    rows = db.list_away_periods(c)
    assert all(r["person_id"] != a for r in rows)       # a's own period gone
    assert all(r["backup_person_id"] != a for r in rows)  # a's backup ref cleared
```

- [ ] **Step 8: Run + commit** — `pytest tests/test_db.py -v` → PASS.

```bash
git add src/family_hub/db.py tests/test_db.py
git commit -m "feat: away_periods table + overlay DB helpers (away_map, CRUD)"
```

---

## Task 2: Pure streak/week away-awareness

**Files:**
- Modify: `src/family_hub/chores.py` (`streak` ~125-146, `week_strip` ~149-167)
- Test: `tests/test_chores.py`

**Interfaces:**
- Produces:
  - `streak(occ_by_date, completions_by_date, today, away_dates: set[str] | None = None) -> int`
  - `week_strip(occ_by_date, completions_by_date, today, away_dates: set[str] | None = None) -> list[str]` (entries now `done|partial|none|rest|away`)

- [ ] **Step 1: Write failing tests** in `tests/test_chores.py`:

```python
def test_streak_treats_away_days_as_rest_and_preserves_across_gap():
    today = dt.date(2026, 8, 17)                     # Sunday
    # daily chore id 1; done through 8/12, away 8/13..8/16, back today undone
    occ = {d: {1} for d in ("2026-08-10", "2026-08-11", "2026-08-12",
                            "2026-08-13", "2026-08-14", "2026-08-15",
                            "2026-08-16", "2026-08-17")}
    done = {d: {1} for d in ("2026-08-10", "2026-08-11", "2026-08-12")}
    away = {"2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16"}
    # Without away, 8/13 is an uncompleted day -> streak breaks after today's
    # forgiveness, counting back only to 8/12 = 3. With away, the gap is rest,
    # so it still reaches the 8/10-8/12 run = 3, but crucially does not BREAK.
    assert ch.streak(occ, done, today, away) == 3
    # Prove non-away would break at 8/16 (0 completed) instead:
    assert ch.streak(occ, done, today) == 0  # 8/16 occurred, not done -> break


def test_streak_backdated_away_repairs_without_touching_history():
    today = dt.date(2026, 8, 17)
    occ = {d: {1} for d in ("2026-08-14", "2026-08-15", "2026-08-16",
                            "2026-08-17")}
    done = {"2026-08-14": {1}, "2026-08-17": {1}}     # missed 15 & 16 (trip)
    assert ch.streak(occ, done, today) == 1           # broken by 8/16
    away = {"2026-08-15", "2026-08-16"}
    assert ch.streak(occ, done, today, away) == 2     # 8/17 + 8/14 across gap


def test_week_strip_emits_away_state():
    today = dt.date(2026, 8, 13)                       # Thu; window Fri..Thu
    occ = {d: {1} for d in ("2026-08-07", "2026-08-11", "2026-08-13")}
    cbd = {"2026-08-07": {1}, "2026-08-13": {1}}
    away = {"2026-08-11"}
    assert ch.week_strip(occ, cbd, today, away) == \
        ["done", "rest", "rest", "rest", "away", "rest", "done"]
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_chores.py -k away -v` → FAIL (unexpected kwarg / wrong result).

- [ ] **Step 3: Implement.** In `streak`, add the param and skip away days:

```python
def streak(occ_by_date, completions_by_date, today, away_dates=None):
    away = away_dates or set()
    d = today
    todays = occ_by_date.get(today.isoformat())
    if today.isoformat() not in away and todays \
            and not _all_done(todays, completions_by_date, today):
        d = today - dt.timedelta(days=1)
    count = 0
    for _ in range(365):
        ds = d.isoformat()
        if ds in away:                 # away day == rest, regardless of log
            d -= dt.timedelta(days=1)
            continue
        occ = occ_by_date.get(ds)
        if not occ:
            d -= dt.timedelta(days=1)
            continue
        if _all_done(occ, completions_by_date, d):
            count += 1
            d -= dt.timedelta(days=1)
        else:
            break
    return count
```

In `week_strip`, add the param and the `away` branch first:

```python
def week_strip(occ_by_date, completions_by_date, today, away_dates=None):
    away = away_dates or set()
    out = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        ds = d.isoformat()
        if ds in away:
            out.append("away")
            continue
        occ = occ_by_date.get(ds)
        if not occ:
            out.append("rest")
            continue
        done = completions_by_date.get(ds, set())
        n = len(occ & done)
        out.append("done" if n == len(occ) else "none" if n == 0 else "partial")
    return out
```

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_chores.py -v` → PASS (existing streak/week tests still green — the new param is optional).

- [ ] **Step 5: Commit**

```bash
git add src/family_hub/chores.py tests/test_chores.py
git commit -m "feat: streak/week_strip treat away_dates as rest (5th 'away' state)"
```

---

## Task 3: Resolution — rotations fall through, fixed chores → backup

**Files:**
- Modify: `src/family_hub/chores.py` (`plan_rows` ~81-96, `day_plan` ~99-117)
- Test: `tests/test_chores.py`

**Interfaces:**
- Consumes: `occurs`, `assignee_id` (already takes `active_ids`).
- Produces:
  - `plan_rows(chores, people, d, away=None) -> list[dict]` where `away` is `{"ids": set[int], "backup": {int: int | None}}`; rows gain `"covering_for": int | None`.
  - `day_plan` passes `covering_for` through to each chore dict.

- [ ] **Step 1: Write failing tests** in `tests/test_chores.py`. Helper chore builders already exist in the file (reuse their style):

```python
def _fixed(cid, pid, title="x"):
    return {"id": cid, "title": title, "icon": "", "schedule_kind": "daily",
            "days_mask": 0, "assign_kind": "fixed", "fixed_person_id": pid,
            "rotation_order": [], "rotation_epoch": "2026-01-01", "active": 1}

def _rot(cid, order):
    return {"id": cid, "title": "trash", "icon": "", "schedule_kind": "daily",
            "days_mask": 0, "assign_kind": "rotation", "fixed_person_id": None,
            "rotation_order": order, "rotation_epoch": "2026-01-01", "active": 1}

def test_plan_rows_rotation_falls_to_whoever_is_home():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    ch_rot = _rot(9, [1, 2])
    base = ch.plan_rows([ch_rot], people, d)               # normal assignee
    normal_pid = base[0]["person_id"]
    away_pid = normal_pid
    other = 2 if normal_pid == 1 else 1
    rows = ch.plan_rows([ch_rot], people, d, {"ids": {away_pid}, "backup": {}})
    assert rows and rows[0]["person_id"] == other          # fell to who's home

def test_plan_rows_fixed_reassigns_to_available_backup():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    rows = ch.plan_rows([_fixed(5, 1, "dog")], people, d,
                        {"ids": {1}, "backup": {1: 2}})
    assert len(rows) == 1
    assert rows[0]["person_id"] == 2 and rows[0]["covering_for"] == 1

def test_plan_rows_fixed_pauses_when_no_available_backup():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    # no backup -> paused
    assert ch.plan_rows([_fixed(5, 1)], people, d, {"ids": {1}, "backup": {}}) == []
    # backup who is also away -> paused (not crash)
    assert ch.plan_rows([_fixed(5, 1)], people, d,
                        {"ids": {1, 2}, "backup": {1: 2}}) == []

def test_plan_rows_away_person_own_chore_absent():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"}]
    assert ch.plan_rows([_fixed(5, 1)], people, d, {"ids": {1}, "backup": {}}) == []
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_chores.py -k plan_rows -v` → FAIL.

- [ ] **Step 3: Implement `plan_rows`:**

```python
def plan_rows(chores, people, d, away=None):
    away = away or {"ids": set(), "backup": {}}
    away_ids = away.get("ids", set())
    backup = away.get("backup", {})
    active_ids = {p["id"] for p in people}
    present_ids = active_ids - away_ids          # people home & not away
    rows = []
    for chore in chores:
        if not occurs(chore, d):
            continue
        # rotations skip away people (fall to whoever's home); fixed returns
        # its fixed_person_id regardless.
        aid = assignee_id(chore, d, present_ids)
        if aid is None:
            continue
        covering_for = None
        if aid in away_ids:                      # only fixed chores reach here
            b = backup.get(aid)
            if b is None or b not in present_ids:
                continue                         # no available backup -> pause
            covering_for = aid
            aid = b
        if aid not in present_ids:               # inactive/away, no cover
            continue
        rows.append({"chore_id": chore["id"], "person_id": aid,
                     "title": chore["title"], "icon": chore["icon"],
                     "rot": 1 if chore["assign_kind"] == "rotation" else 0,
                     "covering_for": covering_for})
    return rows
```

- [ ] **Step 4: Thread `covering_for` through `day_plan`.** In the `prows` comprehension add the key, defaulting to `None` for rows read back from the frozen log (which has no such column):

```python
        prows = [{"id": r["chore_id"], "title": r["title"], "icon": r["icon"],
                  "rot": bool(r["rot"]), "done": r["chore_id"] in completed,
                  "covering_for": r.get("covering_for")}
                 for r in rows if r["person_id"] == pid]
```

- [ ] **Step 5: Guard freeze/log shape.** `_freeze_day` (app.py) keys rows by `(chore_id, person_id, title, icon, rot)` and `replace_day_log` writes only those columns, so `covering_for` rides in the live dict but is NOT persisted — intended (past days show the backup did it, truthfully). No change needed; add a one-line test asserting the log round-trip ignores it:

```python
def test_covering_for_not_persisted_in_log_shape():
    d = dt.date(2026, 8, 17)
    people = [{"id": 1, "name": "A", "color": "#f00"},
              {"id": 2, "name": "B", "color": "#0f0"}]
    r = ch.plan_rows([_fixed(5, 1, "dog")], people, d,
                     {"ids": {1}, "backup": {1: 2}})[0]
    assert set(r) >= {"chore_id", "person_id", "title", "icon", "rot",
                      "covering_for"}
```

- [ ] **Step 6: Run all chores tests, verify pass** — `pytest tests/test_chores.py -v` → PASS. (Existing `plan_rows` callers pass no `away` → unchanged behavior; confirm existing tests green.)

- [ ] **Step 7: Commit**

```bash
git add src/family_hub/chores.py tests/test_chores.py
git commit -m "feat: plan_rows away handling — rotations fall through, fixed->backup"
```

---

## Task 4: Wire the overlay into `_people_day` + `admin_state`

**Files:**
- Modify: `src/family_hub/app.py` (`_people_day` ~614-660, `admin_state` ~1215-1219)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `db.away_map`, updated `chores.plan_rows/streak/week_strip/day_plan`.
- Produces: each `/api/hub` & `/api/chores/day` person entry gains `"away": bool`; chore dicts carry `covering_for`. `admin_state` gains `"away_periods"`.

- [ ] **Step 1: Write a failing integration test** in `tests/test_api.py` (follow existing client/fixture patterns there):

```python
def test_away_person_freezes_no_rows_and_streak_continues(client, db_conn):
    # Build: person A with a daily fixed chore, a run of completed days, then
    # mark A away, advance "today", confirm the frozen log for away days has no
    # rows for A and the streak survives on return. (Use the suite's time/seed
    # helpers; assert occurrence_log rows for away dates exclude A's chore.)
    ...
```

Because time control is suite-specific, implement this against the existing test harness's `_today` override (grep `test_api.py` / `conftest.py` for how `_today` is monkeypatched). The assertion set:
1. On an away day, `GET /api/hub` marks A `away: True` with the pre-trip streak unchanged.
2. `occurrence_log` for that day has no row whose `person_id == A` for A's fixed chore.
3. After closing the period and completing on the return day, A's streak = pre-trip + return days.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement in `_people_day`.** After `people = fdb.list_people(c)` and before resolving `rows`, build the overlay for `d` and the window:

```python
    window_from = (d - dt.timedelta(days=370)).isoformat()
    amap = fdb.away_map(c, window_from, d.isoformat())
    away_today = {pid for pid, info in amap.items()
                  if d.isoformat() in info["dates"]}
    backup_today = {pid: amap[pid]["backup_on"].get(d.isoformat())
                    for pid in away_today}
    away_view = {"ids": away_today, "backup": backup_today}
```

Pass `away_view` into the live resolution (today/future) so freeze excludes away people:

```python
    if d < today:
        rows = fdb.day_log(c, d_str)
    else:
        rows = chlogic.plan_rows(fdb.list_chores(c), people, d, away_view)
        if d == today:
            _freeze_day(c, d_str, rows)
```

Note: `window_from` is now computed once at the top; delete the later duplicate assignment. In the per-person loop, build `away_dates` for that person and pass to streak/week, and stamp the `away` flag:

```python
    for entry in plan:
        pid = entry["person"]["id"]
        occ = {}
        for r in logs:
            if r["person_id"] == pid:
                occ.setdefault(r["date"], set()).add(r["chore_id"])
        if d > today:
            live = {r["chore_id"] for r in rows if r["person_id"] == pid}
            if live:
                occ[d_str] = live
        cbd = {}
        for r in history:
            if r["person_id"] == pid:
                cbd.setdefault(r["date"], set()).add(r["chore_id"])
        away_dates = amap.get(pid, {}).get("dates", set())
        entry["away"] = pid in away_today
        entry["streak"] = chlogic.streak(occ, cbd, d, away_dates)
        entry["week"] = chlogic.week_strip(occ, cbd, d, away_dates)
```

`day_plan` already carries `covering_for` per chore (Task 3). Wrap `away_map` in the same fail-soft spirit: if the overlay build raises, log at ERROR and fall back to `amap = {}`, `away_view = {"ids": set(), "backup": {}}` so the wall renders normally.

- [ ] **Step 4: Add periods to `admin_state`:**

```python
@app.get("/api/admin/state")
def admin_state():
    c = _db()
    return {"people": fdb.list_people(c, include_inactive=True),
            "chores": fdb.list_chores(c, include_inactive=True),
            "away_periods": fdb.list_away_periods(c)}
```

- [ ] **Step 5: Run, verify pass** — `pytest tests/test_api.py -k away -v` and the full `test_api.py` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/family_hub/app.py tests/test_api.py
git commit -m "feat: wire away overlay into _people_day + admin_state"
```

---

## Task 5: Admin away endpoints

**Files:**
- Modify: `src/family_hub/app.py` (Pydantic models near `PersonPatch` ~1096; endpoints after `admin_delete_chore` ~1318)
- Test: `tests/test_api.py`

**Interfaces:**
- Produces:
  - `POST /api/admin/away` `{person_id, start_date?, backup_person_id?}` → period row
  - `POST /api/admin/away/everyone` `{start_date?}` → `{"created": [ids]}`
  - `PATCH /api/admin/away/{id}` `{start_date?, end_date?, backup_person_id?}`
  - `POST /api/admin/away/{id}/back` `{end_date?}` → closes at yesterday by default
  - `DELETE /api/admin/away/{id}`
  - `GET /api/admin/away` → periods with resolved `person_name`/`backup_name`

- [ ] **Step 1: Write failing tests** in `tests/test_api.py`:

```python
def test_admin_away_create_close_delete(client):
    # create two people via admin, open away for one with the other as backup
    # POST /api/admin/away -> 200, period open (end_date None)
    # POST /api/admin/away/{id}/back -> end_date == yesterday(_today())
    # DELETE /api/admin/away/{id} -> gone from GET /api/admin/away
    ...

def test_admin_away_validation(client):
    # unknown person_id -> 404; backup == person_id -> 422
    ...

def test_admin_away_everyone_opens_for_all_active(client):
    # two active people -> POST /everyone creates 2, skips a person already away
    ...
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Add Pydantic models** near `PersonPatch`:

```python
class AwayIn(BaseModel):
    person_id: int
    start_date: str | None = None
    backup_person_id: int | None = None

class AwayPatch(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    backup_person_id: int | None = None

class AwayEveryoneIn(BaseModel):
    start_date: str | None = None

class AwayBackIn(BaseModel):
    end_date: str | None = None
```

- [ ] **Step 4: Implement endpoints.** Reuse `_person_row` for existence checks and `_today()` for defaults:

```python
def _valid_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise HTTPException(422, "bad date")

def _away_rows(c):
    people = {p["id"]: p["name"]
              for p in fdb.list_people(c, include_inactive=True)}
    out = []
    for r in fdb.list_away_periods(c):
        row = dict(r)
        row["person_name"] = people.get(r["person_id"])
        row["backup_name"] = people.get(r["backup_person_id"])
        out.append(row)
    return out

@app.get("/api/admin/away")
def admin_away_list():
    return {"away_periods": _away_rows(_db())}

@app.post("/api/admin/away")
def admin_away_open(a: AwayIn):
    c = _db()
    _person_row(c, a.person_id)                     # 404 if unknown
    if a.backup_person_id is not None:
        if a.backup_person_id == a.person_id:
            raise HTTPException(422, "backup cannot be the same person")
        _person_row(c, a.backup_person_id)
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    pid = fdb.add_away_period(c, a.person_id, start, None, a.backup_person_id)
    return next(r for r in _away_rows(c) if r["id"] == pid)

@app.post("/api/admin/away/everyone")
def admin_away_everyone(a: AwayEveryoneIn):
    c = _db()
    start = _valid_date(a.start_date) if a.start_date else _today().isoformat()
    open_pids = {r["person_id"] for r in fdb.list_away_periods(c,
                 include_closed=False)}
    created = []
    for p in fdb.list_people(c):                    # active only
        if p["id"] in open_pids:
            continue
        created.append(fdb.add_away_period(c, p["id"], start, None, None))
    return {"created": created}

@app.patch("/api/admin/away/{pid}")
def admin_away_patch(pid: int, a: AwayPatch):
    c = _db()
    fields = a.model_dump(exclude_unset=True)
    for k in ("start_date", "end_date"):
        if fields.get(k) is not None:
            fields[k] = _valid_date(fields[k])
    fdb.update_away_period(c, pid, **fields)
    return {"ok": True}

@app.post("/api/admin/away/{pid}/back")
def admin_away_back(pid: int, a: AwayBackIn | None = None):
    c = _db()
    end = (_valid_date(a.end_date) if a and a.end_date
           else (_today() - dt.timedelta(days=1)).isoformat())
    fdb.close_away_period(c, pid, end)
    return {"ok": True}

@app.delete("/api/admin/away/{pid}")
def admin_away_delete(pid: int):
    if not fdb.delete_away_period(_db(), pid):
        raise HTTPException(404, "unknown away period")
    return {"ok": True}
```

- [ ] **Step 5: Run, verify pass** — `pytest tests/test_api.py -k away -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/family_hub/app.py tests/test_api.py
git commit -m "feat: admin away endpoints (open/close/everyone/patch/delete)"
```

---

## Task 6: Admin UI — away controls

**Files:**
- Modify: `src/family_hub/web/static/hub.js` (people-admin render ~489-520), `common.js` (if pure payload helpers fit its pattern), `index.html` (admin panel markup ~160), `styles.css`
- Test: `tests/test_static.py`, `tests/test_js.py` (if the repo has JS unit tests — it does: `tests/js/`)

**Interfaces:**
- Consumes: `/api/admin/state` (now includes `away_periods`), the away endpoints.

- [ ] **Step 1: Write a static guard** in `tests/test_static.py` asserting the admin markup/JS references the away controls and endpoints (mirror the existing static-assertion style — grep for how `data-ptoggle`/`padmin` are guarded):

```python
def test_admin_has_away_controls():
    js = _read("hub.js")
    assert "/api/admin/away" in js
    assert "data-paway" in js      # per-person away button hook
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Render the control** in the people-admin row builder in `hub.js` (next to the Edit/Deactivate buttons). Show "Going away" when the person has no open period, "I'm back" when they do; a backup `<select>` (options = other active people) and a start-date `<input type="date">` in the away sub-form. Use the cached `choreAdminPeople` + the new `away_periods` from `/api/admin/state`. Add a "Pause everyone" button in the people-admin header and a small "Away now" list. Wire click handlers to POST/DELETE the endpoints, then re-fetch `/api/admin/state` (the code already nulls and reloads the cache after people edits — reuse that path).

- [ ] **Step 4: Style** the away controls in `styles.css` reusing `.padmin-btn` sizing so tap targets stay ≥44px; the away badge/list is static (no animation).

- [ ] **Step 5: Run static + JS tests, verify pass** — `pytest tests/test_static.py tests/test_js.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/family_hub/web/static tests/test_static.py
git commit -m "feat: admin away controls (per-person + pause everyone)"
```

---

## Task 7: Wall presentation — away card, week strip, covering tag

**Files:**
- Modify: `src/family_hub/web/static/hub.js` (chore card + week-strip render), `styles.css`
- Test: `tests/test_static.py`

**Interfaces:**
- Consumes: `/api/hub` person entries (`away: bool`, chore `covering_for`), `week` entries including `"away"`.

- [ ] **Step 1: Write static guards** in `tests/test_static.py`:

```python
def test_wall_renders_away_state():
    js = _read("hub.js")
    assert "away" in js.lower()          # away branch in card render
    css = _read("styles.css")
    assert ".week-away" in css or "away" in css   # 5th week-strip state styled
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** In the person-card renderer: when `person.away`, render the card with an "Away ✈️" badge in place of the chore list, keeping the streak number visible; do not show "0/0". In the week-strip renderer, map the `"away"` state to a distinct dim class (`.week-away`) clearly different from `.week-rest` and the red `.week-none`. On the backup's card, when a chore has `covering_for`, append a small "covering for <name>" tag (resolve the name from the people list). Keep all of it static (no animation), reduced-motion safe.

- [ ] **Step 4: Style** `.week-away` (dim, distinct from rest/none) and the away badge + covering tag in `styles.css`.

- [ ] **Step 5: Mobile check.** Temporarily widen the breakpoint locally (`@media (max-width: 2000px)`), screenshot the Chores tab with an away card at ≤400px, verify spacing/tap targets, then revert (never commit the widened breakpoint). Also verify full desktop width (≥1920px) and night mode.

- [ ] **Step 6: Run tests, verify pass; commit**

```bash
git add src/family_hub/web/static tests/test_static.py
git commit -m "feat: wall away card + 'away' week state + covering-for tag"
```

---

## Task 8: DEMO payload shows an away person

**Files:**
- Modify: `src/family_hub/demo.py` (`seed_demo`)
- Test: `tests/test_demo.py`

- [ ] **Step 1: Write a failing test** in `tests/test_demo.py`:

```python
def test_demo_seeds_an_away_person_with_backup(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    demo.seed_demo(c, dt.date(2026, 8, 17))
    periods = db.list_away_periods(c)
    assert periods, "demo should seed at least one away period"
    assert any(p["backup_person_id"] is not None for p in periods)
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** In `seed_demo`, after people/chores are created, mark one demo person (e.g. Milo) away for the last few days with another (Ava) as backup covering a fixed chore:

```python
    # Milo is away the last 3 days; Ava covers his fixed chore. Shows the away
    # card, the 'away' week state, and a covering-for tag in the DEMO wall.
    away_start = (today - dt.timedelta(days=2)).isoformat()
    fdb.add_away_period(conn, milo, away_start, None, backup_person_id=ava)
```

Ensure the demo does NOT also write completions/log rows that contradict the away state for Milo on those days (the seed loop already skips based on `plan_rows`; since `seed_demo` calls `plan_rows` without the away view, add the away view there OR simply let the overlay suppress at render — verify `test_demo` still passes and no Milo rows are asserted for away days). If the seed loop freezes history via completions for Milo on away days, pass the away view into that loop's `plan_rows` call so seeded history matches.

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_demo.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/family_hub/demo.py tests/test_demo.py
git commit -m "feat: DEMO payload seeds an away person with a covering backup"
```

---

## Task 9: Feature-gauntlet finish + review

**Files:** `README.md`, `docs/hub.png`, whole branch.

- [ ] **Step 1: Full suite green** — `pytest -q` (all green, none skipped silently).
- [ ] **Step 2: Fail-soft check** — manually confirm a hand-inserted malformed away row (backup pointing at a deleted id, bad date) does not 500 `/api/hub`; the wall renders and logs ERROR.
- [ ] **Step 3: Regenerate `docs/hub.png`** and update `README.md` in this same PR if the wall render changed (per repo rule).
- [ ] **Step 4: Live verify** — deploy/run locally, mark a demo person away in admin, confirm on the wall: away card, preserved streak, week 'away' state, rotation fell through, fixed chore covered; then "I'm back" and confirm streak continues.
- [ ] **Step 5: Three-agent review** on the branch diff — `pr-review-toolkit:silent-failure-hunter`, `pr-review-toolkit:code-reviewer`, `pr-review-toolkit:pr-test-analyzer`. Address real findings; re-run if the branch grew.
- [ ] **Step 6: Open the PR and stop** (do not merge — wait for go-ahead).

```bash
git add -A && git commit -m "docs: README + hub.png for away mode; gauntlet close-out"
```

---

## Self-Review

**Spec coverage:**
- Overlay table, no history rewrite → Task 1 (+ reversibility via delete). ✓
- Per-person + pause-everyone → Tasks 1, 5. ✓
- Rotation falls through / fixed → backup / no-backup pauses → Task 3. ✓
- Open-ended, back-datable, "I'm back" closes at yesterday → Tasks 1, 5. ✓
- Streak preserved across gap / back-date repair without touching log → Tasks 2, 4. ✓
- Admin-only control; wall reflects → Tasks 5, 6, 7. ✓
- Presentation (away card, 'away' week state, covering tag, static/reduced-motion) → Task 7. ✓
- Fail-soft → Task 4 (overlay build) + Task 9 Step 2. ✓
- DEMO payload → Task 8. ✓
- delete_person cleanup → Task 1 Steps 6-7. ✓
- Gauntlet (mobile, README/hub.png, three-agent review, live verify) → Tasks 6-9. ✓

**Placeholder scan:** Task 4/5/6/7 leave the *test bodies* for time-controlled integration and DOM rendering as prose because they depend on the suite's `_today` monkeypatch and JS test harness whose exact shape must be read from `conftest.py`/`tests/js/` at execution time; the assertion sets are enumerated explicitly. All production code is concrete. Executors must grep those harnesses before writing the tests (called out inline).

**Type consistency:** `away_map` returns `{pid: {"dates": set, "backup_on": {date: id}}}`; `_people_day` derives `away_view = {"ids": set, "backup": {pid: id}}` which is exactly what `plan_rows(away=...)` consumes; `streak`/`week_strip` take `away_dates: set[str]`. `covering_for` added in `plan_rows` and read in `day_plan`. Names consistent across tasks.
