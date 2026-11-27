# Chore wallet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each child gets one dollar wallet that replaces separate chore pay: 30¢ per house chore, a streak reward in a configurable style ($100 at 100 calendar days by default, optional higher pay per chore), shields, a parent PIN, projected costs, and a payout record.

**Architecture:** Pure rules in a new stdlib-only `rewards.py` (mirrors `chores.py`), fed frozen rows from `occurrence_log` (which gains `cents` and `kind`) plus a new `served_days` and `ledger` table. `app.py` adds a `reward` block per person to `/api/hub` and `/api/chores/day` only while the `rewards` feature is on and a PIN is set; off, the wall is byte-for-byte today's. The wall renders two new card lines; Settings gains a Rewards card.

**Tech Stack:** Python 3 / FastAPI / SQLite (stdlib `sqlite3`, `hashlib`), vanilla JS, node `--test` for JS, pytest.

**Spec:** `docs/superpowers/specs/2026-11-26-chore-wallet-design.md` (read it first; this plan does not repeat its reasons).

## Global Constraints

- Defaults: `cents_per_chore = 30`; `bonus_on = True`, `milestones = [[100, 10000]]` (days, cents), repeating every cycle of the largest milestone; `levels_on = False`, `levels = [[25, 35], [50, 40], [100, 45]]` (days, cents per chore at that streak or more); `shield_every = 20`, `shield_max = 2`; `fix_cutoff = "09:00"` house time (`_today()`'s clock); PIN 4 to 6 digits; 5 wrong tries lock PIN actions 5 minutes; wall keeps the PIN in memory 2 minutes.
- Money is integer cents everywhere. Display `$18.50`, `+30¢`.
- Only `kind = 'house'` rows that are not `schedule_kind = 'once'` earn or count. Kind and cents are frozen per log row; existing rows backfill `cents = 0`, `kind = 'house'`.
- Launch (the moment Rewards is first enabled): seed each active person with the OLD `chores.streak` value as of that day; pay already-passed milestones once as bonus rows; shields start 0; money starts at $0.
- Rewards off (feature toggle off, or no PIN): no `reward` keys in any payload, old `chores.streak` chip, old past-day rules. Existing tests stay green unchanged.
- The PIN check is server side. PIN travels in header `X-Parent-Pin`. Stored as `pbkdf2_hmac('sha256', pin, salt, 200_000)` in kv `rewards.pin` = `{"salt": hex, "hash": hex}`. Never logged.
- Public repo: no house names, LAN IPs or amounts tied to this family in tests, DEMO or docs (demo uses the existing demo people).
- Every new wall string follows the owner's plain-words rule; no colons inside sentences in help copy.
- `docs/adding-a-feature.md` runs to the last item (registry toggle, fail-soft, DEMO, mobile, motion/reduced motion, tests + guards, visual gates, README + `docs/hub.png`, three-agent review, live verify).
- Heavy runs (full pytest, all JS tests) on the OMEN: `ssh omen 'cd ~/workspace/family-hub && git fetch -q && git checkout -q rewards-wallet && git pull -q && python -m pytest -q && node --test tests/js/'`. The Mac runs single test files only.

## Review Focus

1. **A rest day today.** Someone with nothing assigned today sees the streak include today at once (it is kept); a child with chores not yet done sees it exclude today, never drop. Test in Task 2.
2. **Untick after a milestone paid.** Streak 25 reached, bonus paid, then the 25th day's chore is unticked inside the window: the streak drops to 24, the bonus stays, and re-ticking does not pay it again. Test in Task 4.
3. **Clock past the cutoff while the wall is open.** At 9:01 the "yesterday" rows on the day browser become PIN-guarded; a tap without a PIN gets a 403 the wall turns into the PIN prompt, not a silent failure. Tests in Tasks 5 and 7.
4. **Person deactivated or deleted with money in the wallet.** Their ledger rows stay; Settings still lists them under "Inactive" with their balance and Paid. Test in Task 6.
5. **Wrong PIN storm.** The sixth try inside 5 minutes is refused even with the right PIN, and the response says how long to wait. Test in Task 3.

---

### Task 1: Schema and frozen money columns

**Files:**
- Modify: `src/family_hub/db.py` (SCHEMA, `ensure_schema`, `replace_day_log`, `backfill_occurrence_log`, `add_chore`/`update_chore` field list)
- Modify: `src/family_hub/app.py` (`_freeze_day` key + rows, `ChoreIn`/`ChorePatch`, `_validate_chore`)
- Test: `tests/test_db.py`, `tests/test_api.py`

**Interfaces:**
- Produces:
  - `chores.kind TEXT NOT NULL DEFAULT 'house'` ('house' | 'personal').
  - `occurrence_log.cents INTEGER NOT NULL DEFAULT 0`, `occurrence_log.kind TEXT NOT NULL DEFAULT 'house'`.
  - `served_days(date TEXT PRIMARY KEY)`; migration inserts `SELECT DISTINCT date FROM occurrence_log`.
  - `ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('bonus','payout')), cents INTEGER NOT NULL, run_key TEXT, at_streak INTEGER, day TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')` with `UNIQUE(person_id, run_key, at_streak)`.
  - `fdb.mark_served(conn, date: str) -> None`, `fdb.served_between(conn, a: str, b: str) -> set[str]`.
  - `fdb.add_ledger(conn, *, person_id, kind, cents, day, run_key=None, at_streak=None, note='') -> int | None` (None when the UNIQUE key already exists), `fdb.ledger_for(conn, person_id: int | None = None) -> list[dict]`.
  - Log rows carry `cents` and `kind`; `_freeze_day` compares them too and writes `cents = wallet.cents_for_today(...)` (Task 4) for house non-once rows only when rewards is active, else 0. (Task 6 supplies `rewards_active(c) -> bool` and `reward_settings(c) -> dict`; until Tasks 4 and 6 land, pass 0.)

- [ ] **Step 1: Failing tests.** `test_migration_adds_kind_cents_served_ledger` (old-shape db → `ensure_schema` → columns exist, old rows `cents == 0`, `kind == 'house'`, `served_days` holds every distinct log date); `test_add_ledger_is_idempotent_on_run_and_streak` (second identical insert returns None, one row); `test_chore_kind_round_trips_through_admin_api` (POST with `kind: "personal"`, GET shows it; bad value → 422).
- [ ] **Step 2:** Run `python -m pytest tests/test_db.py -q -k "kind or ledger"`; expect FAIL.
- [ ] **Step 3:** Implement as listed, following the additive-ALTER pattern and dated comments in `ensure_schema`.
- [ ] **Step 4:** Run the same file tests; expect PASS. Run `tests/test_api.py` on the OMEN (big file).
- [ ] **Step 5:** Commit `Chores: freeze each row's kind and cents; served days and the wallet ledger`.

### Task 2: The rules (pure)

**Files:**
- Create: `src/family_hub/rewards.py`
- Test: `tests/test_rewards.py`

**Interfaces:**
- Consumes: frozen row dicts `{date, chore_id, person_id, kind, cents, once: bool}` and completion keys.
- Produces:
  - `@dataclass(frozen=True) class Run: streak: int; shields: int; run_key: str | None; outcomes: dict[str, str]` where an outcome is `'kept' | 'saved' | 'missed' | 'away' | 'unserved' | 'open'` (`open` = today not finished).
  - `run_state(rows_by_date: dict[str, list[dict]], done: set[tuple[int, str]], *, start: date, today: date, away: set[str], served: set[str], seed: int = 0, seed_key: str | None = None, shield_every=20, shield_max=2, shields_on=True) -> Run`.
  - `milestones_crossed(prev_streak: int, streak: int, milestones: list[list[int]]) -> list[tuple[int, int]]` returning `(at_streak, cents)` for every threshold in `(prev, streak]`; the list repeats every cycle of its largest day count (`[[100, 10000]]` → 100, 200, ...; `[[25, a], [100, b]]` → 25, 100, 125, 200, ...). Empty list → `[]`.
  - `next_milestone(streak: int, milestones) -> tuple[int, int] | None` (at, cents).
  - `rate_for(streak: int, settings: dict) -> int` cents per chore: `cents_per_chore`, or with `levels_on` the highest level whose days ≤ streak.
  - `next_level(streak: int, settings: dict) -> tuple[int, int] | None` (at, cents), None when levels are off or the top is reached.
  - `goal_for(streak: int, settings: dict) -> int` the next milestone's day count when bonuses are on, else the next level's, else the next multiple of 100.
  - `format_cents(c: int) -> str` (`1850 → "$18.50"`, `30 → "30¢"`).
  - `project(per_child: list[dict], settings: dict) -> dict`. Input per child `{id, chores_per_week: float, done_share: float, streak: int}`. Output `{"children": [{id, perfect: {week, month, year}, usual: {week, month, year}}], "total": {perfect: {...}, usual: {...}}}` in cents, month = week × 52 / 12, year = week × 52. **Perfect:** every chore done, rate at the top level (levels on) else base, plus each milestone's cents × 7 / its cycle days per week. **Usual:** `chores_per_week × done_share × rate_for(streak)`, plus milestones scaled by `done_share ** 7` (the chance a week passes with no miss; shields ignored, so it stays an under-promise of streak money and is labeled "about").

Algorithm for `run_state` (the one part the signature does not settle): walk dates `start .. today`. `away` or not in `served` → skip (outcome away/unserved). Items = rows that are house and not once. Kept if every item is done (empty items = kept). Today: kept if kept, else `open` and stop. Kept → `streak += 1`, `kept_in_run += 1`, `run_key = run_key or (seed_key if seed else date)`, and when `shields_on and kept_in_run % shield_every == 0 and shields < shield_max` → `shields += 1`. Not kept → if `shields`: `shields -= 1`, outcome `saved`; else `missed`, `streak = kept_in_run = shields = 0`, `run_key = None`, and the seed no longer applies. Start the walk with `streak = seed`, `run_key = seed_key if seed else None`, `kept_in_run = 0`.

- [ ] **Step 1: Failing tests**, each with the spec's values:
  - `test_rest_days_count_as_kept` (Fri/Sun-only child, 14 calendar days all done → streak 14).
  - `test_today_unfinished_is_open_not_missed` and `test_rest_day_today_counts_now` (Review Focus 1).
  - `test_one_miss_spends_a_shield` (20 kept, 1 miss, 5 kept → streak 25, shields 0, outcome of the miss `saved`).
  - `test_shields_cap_at_two` (60 kept → shields 2).
  - `test_miss_without_shield_resets` (5 kept, miss → streak 0, run_key None).
  - `test_away_and_unserved_are_neutral` (kept, away 3 days, unserved 1 day, kept → streak 2).
  - `test_personal_and_once_rows_never_count` (personal and once undone, house done → kept).
  - `test_seed_carries_and_keys_the_run` (seed 71, 4 kept → streak 75, run_key == seed_key).
  - `test_no_lookback_cap` (400 kept days → 400).
  - `test_milestones_default_hundred_repeats` (`[[100, 10000]]`: `(99, 100)` → `[(100, 10000)]`; `(150, 201)` → `[(200, 10000)]`; `(0, 0)` → `[]`).
  - `test_milestones_custom_list_repeats` (`[[25, 1000], [100, 6500]]`: `(99, 126)` → `[(100, 6500), (125, 1000)]`).
  - `test_rate_for_levels` (levels off → 30 at any streak; on → 30 at 24, 35 at 25, 40 at 71, 45 at 100).
  - `test_next_milestone_and_goal` (default: `71` → `(100, 10000)`, goal 100; `100` → `(200, 10000)`, goal 200; bonuses off and levels on: `47` → goal 50).
  - `test_project_perfect_and_usual` (one child 36 chores a week, done 1.0, defaults → perfect week = 36 × 30 + 10000 × 7 / 100 = 1780 cents, month = 1780 × 52 / 12 rounded; done 0.9 → usual chore part = round(36 × 0.9 × 30); totals add up across two children).
  - `test_format_cents`.
- [ ] **Step 2:** `python -m pytest tests/test_rewards.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `rewards.py`, stdlib only, no I/O, module docstring in the style of `chores.py`.
- [ ] **Step 4:** Same command → PASS.
- [ ] **Step 5:** Commit `Rewards: calendar-day streaks, shields and milestones (pure rules)`.

### Task 3: Parent PIN

**Files:**
- Create: `src/family_hub/parent_pin.py`
- Modify: `src/family_hub/app.py` (routes)
- Test: `tests/test_parent_pin.py`, `tests/test_api.py`

**Interfaces:**
- Produces:
  - `parent_pin.set_pin(conn, pin: str, *, current: str | None) -> None` (raises `ValueError` on a bad format or a wrong `current` when one is set).
  - `parent_pin.check(conn, pin: str | None, now: datetime) -> tuple[bool, int]` → (ok, seconds_locked). Counts failures in kv `rewards.pin_fail` = `{"n": int, "since": iso}`; 5 failures inside 5 minutes lock for 5 minutes from the fifth.
  - `parent_pin.is_set(conn) -> bool`.
  - `app.require_parent(request) -> None` raising `HTTPException(403, {"pin": "required"})`, `(403, {"pin": "wrong"})` or `(429, {"pin": "locked", "wait_s": n})`.
  - Routes: `POST /api/rewards/pin {pin, current?}` (set or change; `current` required when set), `POST /api/rewards/pin/check` (header only; 204 or the errors above).
- [ ] **Step 1: Failing tests.** `test_pin_rejects_bad_format` (`"12"`, `"1234567"`, `"12a4"`), `test_pin_hash_not_plaintext` (kv value has no `"1234"`), `test_change_needs_current`, `test_lockout_after_five` (Review Focus 5: sixth try with the right PIN → `(False, >0)`; after 5 minutes → ok), `test_check_route_codes`.
- [ ] **Step 2:** Run `python -m pytest tests/test_parent_pin.py -q` → FAIL.
- [ ] **Step 3:** Implement with `hashlib.pbkdf2_hmac`, `secrets.token_bytes(16)`, `hmac.compare_digest`.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5:** Commit `Rewards: a parent PIN, hashed, with a lockout`.

### Task 4: Ledger and milestone awards

**Files:**
- Create: `src/family_hub/wallet.py` (I/O glue between db and `rewards.py`)
- Test: `tests/test_wallet.py`

**Interfaces:**
- Consumes: Task 1 db helpers, Task 2 `run_state`/`milestones_crossed`, settings from Task 6's `reward_settings(conn)` (stub a dict in tests).
- Produces:
  - `wallet.person_run(conn, pid: int, today: date, settings: dict, launch: dict, away: set[str]) -> rewards.Run`.
  - `wallet.award(conn, pid: int, run: Run, settings: dict, today: date) -> list[dict]` (new bonus rows). Pays each threshold `milestones_crossed(last_paid_at(pid, run.run_key), run.streak, ...)` keyed `(pid, run.run_key, at)`. Overlap rule: skip a threshold when a bonus at the same `at_streak` exists for `pid` with `day >= run_start_date(run.run_key)`.
  - `wallet.balance_cents(conn, pid: int, launch_day: str) -> int` = sum of `occurrence_log.cents` on house non-once rows owned by `pid` with a completion and `date >= launch_day`, plus bonuses, minus payouts.
  - `wallet.pay_out(conn, pid: int, today: date) -> dict | None` writes a payout row for the current balance (None when balance ≤ 0).
  - `wallet.launch(conn, today: date, settings: dict) -> dict` writes kv `rewards.launch` = `{"day": iso, "seeds": {pid: old_streak}}` once (no-op returning the existing record when present) and awards passed milestones with `run_key = "seed:" + day`.
  - `wallet.history(conn, pid: int, launch_day: str) -> list[dict]` rows `{week_start, earned_cents}` plus ledger rows, newest first.
  - `wallet.cents_for_today(conn, pid: int, today: date, settings: dict, launch: dict, away: set[str]) -> int` = `rewards.rate_for(streak as of the end of yesterday, settings)`; Task 1's `_freeze_day` uses it for each house non-once row.
  - `wallet.pace(conn, pid: int, today: date) -> dict` `{chores_per_week, done_share, streak}` from the last 56 days of house non-once rows (input to `rewards.project`).
- [ ] **Step 1: Failing tests.** `test_launch_pays_passed_milestones_once` (custom milestones `[[25, 1000], [50, 2500], [100, 10000]]`, seed 71 → bonuses 1000 + 2500; with the default `[[100, 10000]]` seed 71 → no rows; second `launch` call adds nothing), `test_seeded_run_pays_100_when_it_gets_there` (seed 71, 29 kept → one 10000 bonus keyed `seed:<day>`), `test_levels_freeze_rate_per_day` (levels on, streak 24 at the end of yesterday → 30, 25 → 35; turning levels off later leaves those rows unchanged), `test_pace_from_last_8_weeks`, `test_award_pays_on_crossing_and_never_twice` and `test_untick_keeps_paid_bonus` (Review Focus 2), `test_balance_counts_only_house_non_once_after_launch`, `test_rate_change_does_not_rewrite_past` (rows frozen at 30 stay 30 after setting 50), `test_payout_zeroes_and_records`, `test_balance_can_go_negative_after_edit`.
- [ ] **Step 2:** `python -m pytest tests/test_wallet.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5:** Commit `Rewards: the wallet, milestone bonuses and payouts`.

### Task 5: Old-day lock

**Files:**
- Modify: `src/family_hub/app.py` (`complete`, `uncomplete`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `rewards_active(c)` (Task 6), `require_parent` (Task 3), `reward_settings(c)["fix_cutoff"]`.
- Produces: `app._edit_allowed(d: date, now: datetime, cutoff: str) -> bool` (today; or yesterday before cutoff). When rewards is active and `_edit_allowed` is false, both routes call `require_parent(request)` before touching anything.
- [ ] **Step 1: Failing tests.** `test_yesterday_editable_until_cutoff`, `test_older_day_needs_pin` (403 `{"pin":"required"}`, then 200 with the header), `test_lock_off_when_rewards_off` (old behavior, 366-day window kept), `test_cutoff_passes_while_open` (Review Focus 3: freeze clock at 09:01 → yesterday needs PIN).
- [ ] **Step 2:** Run those tests by name → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5:** Commit `Rewards: old days need a parent once money is on`.

### Task 6: Feature toggle, settings and the payload

**Files:**
- Modify: `src/family_hub/integrations.py` (add `add("rewards", "rewards", "Chore wallet", True, group="feature")` after chores)
- Modify: `src/family_hub/app.py` (`_people_day`, `hub()`, `chores_day`, new routes), `src/family_hub/demo.py`
- Test: `tests/test_api.py`, `tests/test_integrations.py`, `tests/test_demo.py`

**Interfaces:**
- Produces:
  - `app.reward_settings(c) -> dict` (defaults merged under kv `rewards.settings`), `app.rewards_active(c) -> bool` (toggle enabled AND PIN set AND `rewards.launch` exists).
  - Routes, all PIN-guarded: `GET /api/rewards` (settings, launch, per person `{id, name, active, balance_cents, history, pace}`), `POST /api/rewards/projection` (body = draft settings, unsaved; returns `rewards.project` over every active person's `wallet.pace`, so the card updates as amounts are typed), `PUT /api/rewards/settings` (validates: base 0 to 500 cents, each list sorted by days, days 1 to 1000, cents 0 to 100000), `POST /api/rewards/enable` (requires PIN set; calls `wallet.launch`), `POST /api/rewards/payout/{pid}`.
  - `_people_day` marks today served (`fdb.mark_served`) when `away_ok`, and when `rewards_active` adds per person `entry["reward"] = {"streak", "goal", "shields", "shield_max", "wallet_cents", "rate_cents", "next_bonus": {"at", "cents"} | null, "next_level": {"at", "cents"} | null, "bonus_today_cents", "outcomes7": [...]}` and per chore row `cents` and `kind`. Awards run inside the hub build for `d == today` only, wrapped fail-soft (log and omit `reward`, never 500).
  - Chore admin: changing `kind` requires the PIN when rewards is active.
  - DEMO: two demo children with wallets, one with 2 shields, one on a milestone day.
- [ ] **Step 1: Failing tests.** `test_hub_has_no_reward_when_off` (payload identical keys to main), `test_hub_reward_block_shape`, `test_enable_requires_pin_and_seeds`, `test_settings_put_needs_pin`, `test_reward_failure_is_fail_soft` (monkeypatch `wallet.person_run` to raise → 200, no `reward`), `test_inactive_person_keeps_balance` (Review Focus 4), `test_rewards_in_registry_as_feature`, `test_demo_payload_has_rewards`.
- [ ] **Step 2:** Run by name → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS; run `tests/test_api.py` on the OMEN.
- [ ] **Step 5:** Commit `Rewards: feature switch, settings and the wall payload`.

### Task 7: The card, the celebration and the PIN prompt

**Files:**
- Modify: `src/family_hub/web/static/hub.js` (`personCardHtml`, `choreRowHtml`, `weekStripHtml`, celebration, chore editor kind control, PIN modal), `src/family_hub/web/static/styles.css`
- Test: `tests/js/hub.test.mjs`, `tests/js/hub-dom.test.mjs`, `tests/test_static.py`

**Interfaces:**
- Consumes: Task 6 payload.
- Produces:
  - `rewardHeadHtml(r) -> string`: `🔥 47 / 100` plus one 🛡 per held shield (empty outline for unheld up to `shield_max`), replacing the old chip only when `p.reward` exists.
  - `walletLineHtml(r) -> string`: `Wallet $18.50` left; right side `next bonus $100 at 100` when `next_bonus`, else `35¢ a chore at 25` when `next_level`, else nothing; on a bonus day `+$100 bonus today`.
  - Row suffix: house rows with `cents > 0` get `+30¢`; personal rows get `personal goal`; once rows nothing.
  - Week strip state `saved` (shield glyph), styled through theme tokens only.
  - On a tick that adds money, a `+30¢` float from the row (opacity/transform only, none under reduced motion). On `bonus_today_cents` turning non-zero, a bigger burst than `celebrate` and the bonus line.
  - `askParentPin(reason: string) -> Promise<string | null>`: modal with digits, works with the OSK (`?kiosk=1`), caches the PIN 2 minutes in memory only (never localStorage). Any fetch that gets 403 `{"pin":...}` retries once through it; 429 shows "Try again in N minutes".
  - Chore editor: a House chore / Personal goal two-button segment.
- [ ] **Step 1: Failing tests.** JS: `rewardHeadHtml` shows `47 / 100` and two shields; `walletLineHtml` formats cents; personal row tag; no reward → markup identical to main (`personCardHtml` snapshot). Static: PIN never written to `localStorage`; float animations exist only under `prefers-reduced-motion: no-preference`; new colors come from tokens (existing token guard).
- [ ] **Step 2:** `node --test tests/js/hub.test.mjs` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS; `tests/test_static.py` single file.
- [ ] **Step 5:** Commit `Rewards: the wallet on the wall`.

### Task 8: Settings → Chore wallet

**Files:**
- Modify: `src/family_hub/web/static/hub.js` (`renderSettingsFull`, new `rewardsCardHtml`), `src/family_hub/web/static/common.js` (`helpTopics` gains "The chore wallet"), `styles.css`
- Test: `tests/js/hub-dom.test.mjs`, `tests/test_static.py`

**Interfaces:**
- Produces: a "Chore wallet" settings card after Features. No PIN set: one "Set a parent PIN" button. PIN set, not enabled: "Turn on the wallet" (explains the carry-over in one line). Enabled: base rate; "Streak reward" with two switches, "Bonus at milestones" (editable rows of days and amount, default one row 100 days $100) and "Higher pay per chore" (rows of days and cents, default off); cutoff; shields on/off; all behind one PIN prompt per edit session. Under the amounts, a **Projected cost** table (per child and Family total; columns Week, Month, Year; two rows, "Perfect streak" and "Usual pace, about") that re-fetches `/api/rewards/projection` 300 ms after each edit and labels itself "not saved yet" until Save; then per child `Name  $18.50  [Paid]` and a "History" disclosure (weeks earned, bonuses, payouts). Inactive people listed under "Inactive" when they hold a non-zero balance or history.
- Help topic copy, plain words: what earns, the streak, shields, bonuses, fixing yesterday until the cutoff, Paid.
- [ ] **Step 1: Failing tests.** DOM: each of the three states renders its controls; editing the base rate re-renders the projection from a stubbed fetch and shows "not saved yet"; Paid calls `/api/rewards/payout/<id>` after the PIN prompt; help topic present only when the feature is on.
- [ ] **Step 2:** → FAIL. **Step 3:** Implement. **Step 4:** → PASS.
- [ ] **Step 5:** Commit `Rewards: the settings card and help`.

### Task 9: The gauntlet, docs, review, ship, launch

**Files:** `README.md` (chores section and the stale Backup paragraph is NOT touched here; it belongs to the backups project), `CHANGELOG.md`, `docs/hub.png`, `TECHNICAL_DESIGN.md` (rewards section).

- [ ] **Step 1:** Walk `docs/adding-a-feature.md` top to bottom; tick each line in the PR body with evidence.
- [ ] **Step 2:** Visual gates at 1920×1080 on `DEMO=1`, all five themes, night, reduced motion, phone 390 and 360, a seasonal look on, the PIN modal and the settings card; inspect each myself before anything goes to the owner.
- [ ] **Step 3:** Full suite and JS tests on the OMEN (command in Global Constraints); expect all pass, no new skips.
- [ ] **Step 4:** Three review agents on the branch diff (silent-failure-hunter, code-reviewer, pr-test-analyzer); fix real findings; re-run if the branch grew.
- [ ] **Step 5:** `~/.claude/bin/ship open --band 3 --why "money and a PIN gate on the family wall"`, then `ship land <pr> --merge` once green.
- [ ] **Step 6:** Deploy per the deploy-target memory; live check `/health/full`, the wall card with rewards OFF (unchanged), then show the owner real screenshots.
- [ ] **Step 7:** The owner sets the PIN and taps "Turn on the wallet" on the wall (the PIN is theirs to choose). Then verify live: seeds match the old streaks, no launch bonus rows with the default milestone, the projection matches a hand calculation from the live pace, a tick moves the wallet 30¢.
