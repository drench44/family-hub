# Real-Time Laundry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The laundry card reflects each machine's real status within ~5 seconds
of Home Assistant seeing it, always — not just in the endgame window.

**Architecture:** Today the pipeline is pull-based and stacks delays: the wall
polls `/api/tiles/laundry` every 60s and the server caches HA reads 25s, so any
status change (start, pause, phase word, finish outside the endgame window) can
take ~85s to appear — while HA itself is push-fed by LG ThinQ and fresh to the
second (verified live 2026-08-18: `washer_remaining_time` retimed mid-cycle at
14:38:05Z). Worse, the server only *observes* HA when a browser asks, so the
cycle log and completion memory silently depend on a wall being open. The fix
inverts the flow: a server-side background watcher polls HA every 5s
continuously, owns the transition synthesis (kv completion memory + cycle log),
keeps a current annotated snapshot, and pushes changes to clients over SSE
(`/api/laundry/stream`, native `EventSource` — no build step, no new deps).
The frontend endgame fast lane (10s chained re-polls + paired server TTL) is
retired: the watcher makes it redundant everywhere, not just near a projected
finish. The 60s poll stays as a fallback and the countdown tick is unchanged.

**Tech Stack:** FastAPI `StreamingResponse` SSE, asyncio task in the existing
lifespan, vanilla-JS `EventSource`. Zero new dependencies.

**Spec:** This plan doubles as the spec; evidence and rationale above.

## Global Constraints

- Public repo: no real entity IDs/IPs/tokens in code, tests, or docs.
- Fail-soft everywhere: a watcher/DB/HA failure must never 500 a tile or kill the loop.
- No new pip deps; vanilla JS only (no build).
- Tests must genuinely run in CI (no silent skips).
- CHANGELOG.md entry required (changelog-guard CI).
- Every removed guard gets a replacement guard for the new mechanism.
- The synthesis (kv + laundry_log) semantics do not change — only *where* it runs.

---

### Task 1: Retire the server endgame cache machinery (tiles.py)

**Files:**
- Modify: `src/family_hub/tiles.py` (constants ~30-51, `_laundry_endgame` ~308, TTL pick ~400)
- Test: `tests/test_tiles.py` (503-550)

**Interfaces:**
- Produces: `tiles.LAUNDRY_TTL == 4.0` as the single cache TTL; `tiles.laundry_tile` signature unchanged. `LAUNDRY_ENDGAME_*` and `_laundry_endgame` are GONE.

- [x] Step 1: Replace `test_laundry_endgame_window` and `test_laundry_cache_ttl_tightens_in_the_endgame` with a single test asserting the cache expires within `LAUNDRY_TTL` and that `LAUNDRY_TTL <= 5.0` (the watcher cadence bound).
- [x] Step 2: Run; expect failure (constants still 25/endgame).
- [x] Step 3: In tiles.py delete `LAUNDRY_ENDGAME_TTL/_AHEAD_MIN/_BEHIND_MIN`, `_laundry_endgame`, set `LAUNDRY_TTL = 4.0`, use it unconditionally; update comments.
- [x] Step 4: Run tests/test_tiles.py; PASS.

### Task 2: Extract the synthesis + add the watcher (app.py)

**Files:**
- Modify: `src/family_hub/app.py` (route at 1466; lifespan at 276)
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `_laundry_annotate(t: dict) -> dict` (the exact kv/log synthesis body moved out of the route); `_laundry_payload() -> dict` (demo / fresh-snapshot / inline-fetch resolution); `async _laundry_watch_tick()` (fetch + annotate + store `_laundry_snapshot`, bump `_laundry_version`, wake `_laundry_waiters`); `async laundry_watch_loop()`; watcher task started in `_lifespan` when laundry configured and `_sync_enabled()`; `LAUNDRY_WATCH_S = 5.0`, `LAUNDRY_SNAPSHOT_FRESH_S = 15.0`.
- Route `/api/tiles/laundry` serves the fresh snapshot when the watcher has one, else falls back to the current inline fetch+annotate path (keeps existing tests + resilience when the watcher is disabled/dead).
- Change wake: `_laundry_waiters` is an `asyncio.Event` replaced on each change (`old.set()` after swap); SSE waiters grab the current event and await it.

- [x] Step 1: New tests: (a) `_laundry_watch_tick` with a stubbed `tiles.laundry_tile` stores an annotated snapshot and the route serves it WITHOUT re-fetching (stub call-count stays 1); (b) a tick with changed payload replaces+sets the waiter event; an unchanged tick does not; (c) a tick that raises logs and leaves the loop alive (loop test via two ticks around a raising stub).
- [x] Step 2: Run; FAIL (functions missing).
- [x] Step 3: Implement per Interfaces; route body becomes `_laundry_payload()`; all watcher work wrapped in try/except with `log.warning`.
- [x] Step 4: Run tests/test_api.py; PASS (old synthesis tests untouched and passing).

### Task 3: SSE endpoint `/api/laundry/stream`

**Files:**
- Modify: `src/family_hub/app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `GET /api/laundry/stream` → `text/event-stream`; on connect immediately emits `data: <tile JSON>\n\n`, then a new event whenever the payload changes, `: ping\n\n` heartbeat every ≤25s otherwise; DEMO emits `fdemo.demo_laundry()`. Headers: `Cache-Control: no-cache`.

- [x] Step 1: Tests: stream's first event parses as the current tile JSON (TestClient `stream()`, read first `data:` line, close); DEMO mode first event matches demo shape (`available` true, 2 machines).
- [x] Step 2: Run; FAIL (404).
- [x] Step 3: Implement generator per Interfaces (wait on the current waiter event with `asyncio.wait_for(timeout=20)`; TimeoutError → heartbeat).
- [x] Step 4: Run; PASS.

### Task 4: Frontend — EventSource + retire the fast lane (hub.js)

**Files:**
- Modify: `src/family_hub/web/static/hub.js` (2460-2520 fast lane, bootstrapping ~3637)
- Test: `tests/test_static.py` (914-990)

**Interfaces:**
- Produces: `applyLaundry(data)` (set `laundryData`, render on change else tick — shared by poll + stream); `lnConnect()` (idempotent `EventSource('/api/laundry/stream')`, `onmessage` → `applyLaundry(JSON.parse(...))`); wake listeners (`pageshow`, `visibilitychange`→visible) call `lnConnect()` + `fetchLaundry()`. REMOVED: `lnEndgame`, `LN_FAST_POLL_MS`, `LN_ENDGAME_AHEAD_MIN/_BEHIND_MIN`, `lnFastTimer`. The 60s `fetchLaundry` poll and 30s `laundryTick` stay.

- [x] Step 1: Update `test_laundry_card_static_guards`: drop the endgame-pairing asserts; add: hub.js contains `new EventSource('/api/laundry/stream'`, an `applyLaundry` used by both `fetchLaundry` and the stream handler, a wake reconnect (`lnConnect` referenced in a `visibilitychange` or `pageshow` listener), and NO `lnEndgame`/`LN_FAST_POLL_MS` remnants; assert `app.LAUNDRY_WATCH_S <= 10`.
- [x] Step 2: Run; FAIL.
- [x] Step 3: Implement hub.js changes.
- [x] Step 4: Full suite; PASS.

### Task 5: Docs + changelog + gauntlet gates

**Files:**
- Modify: `README.md` (laundry bullets ~54-62), `CHANGELOG.md`, `docs/adding-a-feature.md` only if a new gate is warranted.

- [x] Step 1: README: laundry card is real-time (server watches HA every ~5s, pushes over SSE; log no longer depends on an open wall). CHANGELOG entry under Unreleased (guard style — check `scripts/check_changelog.py` expectations).
- [x] Step 2: Visual gates: wall at 1920px, night mode, mobile breakpoint screenshots — no visual change expected (card markup untouched); verify no regressions. `docs/hub.png` unchanged (no visual delta).
- [x] Step 3: Three-agent review (silent-failure-hunter, code-reviewer, pr-test-analyzer) on the branch diff; address findings.
- [x] Step 4: Full suite + commit + PR.

### Post-merge (live)

- Deploy per deploy-target memory; verify: `/api/version`, watcher log lines, `curl -N /api/laundry/stream` shows events, wall reflects a status change within ~5s. Ask the operator to sanity-check the phone.
