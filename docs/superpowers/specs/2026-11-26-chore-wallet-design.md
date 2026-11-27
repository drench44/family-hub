# Chore wallet and fairer streaks: design

Status: approved by the owner 2026-11-26 ("Do it", after the card sketch below).

## Why

The house runs a "100 days in a row earns $100" streak. An audit of the live
history (2026-08-12 to 2026-11-24) found it unfair and brittle:

- The streak counts *chore days*, skipping days with nothing assigned. A child
  with chores only on Fridays and Sundays needs about a year to reach 100; a
  child with daily chores needs about three months.
- `chores.streak` walks back at most 365 calendar days, so a twice-a-week child
  tops out near 104 and an away stretch can push 100 out of reach (a bug).
- One missed item out of seven resets months of work. 19 of the 20 breaks
  were one or two items on one day; one child lost a 50-day run to "Reading".
- Personal goals (vitamins, reading, workouts, driving practice) and one-time
  items (meetings) count toward the money exactly like house chores.
- Any past day up to 366 days back can be ticked by anyone, with no check.
- The wall shows only "🔥 N". No goal, no best, no record of what was paid.

The owner's bar: **"not too overly complicated so they can follow it and track
it and easily see it and be motivated to continue."** Simplicity for a child
reading the card beats completeness of the rules. When a choice below trades
fairness against simplicity, simplicity on the card wins and the fairness
lives in the rules underneath.

## Goals

1. One money number per child, in dollars, that goes up when they tap a chore.
2. A streak that means the same thing for every child: calendar days.
3. A single slip costs a shield, not the whole run.
4. Bigger bonuses at streak milestones, landing in the same wallet.
5. Only house chores earn money or affect the streak.
6. Parents pay out from the wall and a record is kept.
7. Old days cannot be quietly changed.

## Non-goals

- No separate points currency. Money is shown as money.
- No spending shop, goals jar, interest, or transfers between children.
- No per-child accounts or logins. The trust model stays LAN-only; the parent
  PIN guards a few money actions, nothing else.
- No change to how chores are scheduled, rotated or covered when away.

## Anti-goals (would make this a failure even if it works)

- A card a child has to have explained twice.
- Money that changes after the fact with no visible reason (a balance that
  drops overnight because a rule recomputed history).
- A PIN prompt in the everyday flow of ticking today's chores.

## The card (what the owner approved)

```
 Ava                          🔥 47 / 100  🛡🛡
 ───────────────────────────────────────────
 ☑ Feed the dog                       +30¢
 ☐ Daily Trash Out                    +30¢
 ☐ Read 15 min             (personal goal)
 ───────────────────────────────────────────
 Wallet  $18.50        next bonus  $100 at 100
```

Only two things are new to the child: the flame line (streak toward the next
goal, plus shields) and the wallet line. Each house chore shows what it pays;
a personal goal shows a quiet "personal goal" tag instead.

## Approaches considered

| Approach | Trade-off | Verdict |
| --- | --- | --- |
| Two pots: weekly points allowance plus streak money | Most motivating, but two numbers and two rules for a child to track | Dropped: fails the simplicity bar |
| One pot with a streak multiplier on points | One number, but "1.5x on a 25-day run" is abstract and loses the big-moment bonus | Dropped |
| **One wallet in dollars: each chore adds a little, milestones drop bonuses in** | One number, concrete, keeps the big moments | **Picked by the owner** |

## Rules

### What counts

- Each chore gets a kind: **house chore** (default) or **personal goal**.
  Personal goals render as today, earn nothing and never touch the streak.
- One-time chores (`schedule_kind = 'once'`) never earn and never touch the
  streak, whatever their kind.
- Existing chores migrate as house chores. The admin form gains the toggle.
  (Builder: the owner will re-tag vitamins, reading, workouts and the like
  after launch; we do not guess them.)

### A day's outcome, per child (house chores only)

- **Kept:** every house chore assigned that day was done, or nothing was
  assigned (a rest day now counts, which is what makes it calendar days).
- **Away:** the child was away. Neither counts nor breaks (unchanged).
- **Missed:** at least one assigned house chore was not done.
- **Today** is not judged until it ends (unchanged forgiveness).

### Streak and shields

- Streak = consecutive kept days, walking back, with away days skipped.
- **Shields:** a child earns one shield for every 20 kept days in the current
  run, holding at most 2. A missed day spends a shield automatically if one
  is held: the day shows as "saved", does not add to the streak, and does not
  break it. With no shield, a missed day ends the run (streak 0, shields 0).
- The streak and shields are **derived from the record**, never stored, so a
  late fix inside the allowed window (below) gives the shield back by itself.
- No lookback cap: the walk goes to the start of the record. (Fixes the 365
  bug.)

### Money

- Every house chore pays one rate for everyone (base plus any streak level),
  not per-chore prices: simpler to read, and the heavier load already earns
  more by count. It must stay "easy for them to understand" (owner).
- The amount is frozen into the day's record when the day is served, the
  same way the occurrence log freezes assignments, so changing the rate never
  rewrites the past. With higher pay per chore on, the level is the streak
  as of the start of that day.
- Undoing a tick takes its money back (same day or inside the fix window).
- **The wallet replaces the chore money the house paid separately** (owner,
  2026-11-26: "convert it to this, but make it fair so the ones that do more
  chores get more", with the streak as "extra incentive"; a budget of "up to
  $75 a month" for the hardest worker). Default base **30¢** per house chore.
- **Streak reward style is a setting** (owner: "maybe that could be
  configurable ... open to what you recommend"). Two styles, each on or off:
  - **Bonus at milestones:** a list of (days, amount). Default **one
    milestone, $100 at 100 days**, the deal the house already runs.
  - **Higher pay per chore:** a list of (days, cents). Suggested
    25 → 35¢, 50 → 40¢, 100 → 45¢; default **off**.
  The defaults are the house's choice: flat 30¢ plus $100 at 100.
  (This replaces the earlier $10/$25/$65 split.)
- **Milestones repeat** every cycle of the largest milestone (with only 100:
  200, 300, ...). Each milestone pays once per run, recorded as a ledger entry the
  moment the streak first reaches it. A milestone already paid stays paid
  even if a later fix changes history; the ledger is the source of truth for
  money, the record is the source of truth for the streak.
- **Wallet** = chore earnings plus milestone bonuses since the last payout.
- **Paid:** a parent taps Paid on a child, enters the PIN, and the wallet goes
  to $0. The ledger keeps date, amount and the breakdown.

### Fixing past days

- Today: anyone can tick and untick, as now.
- Yesterday: anyone can tick or untick until a cutoff time the next morning,
  default **9:00 am**, settable.
- Anything older: needs the parent PIN.
- The PIN check is on the server (the API refuses), not just hidden buttons.

### Parent PIN

- Set once in Settings (4 to 6 digits), stored hashed in kv. Until a PIN is
  set, the money features stay off and the wall shows the plain streak.
- It guards: old-day edits, Paid, reward settings (rates, milestones, cutoff,
  shields on or off), and the house/personal toggle on a chore.

### Launch: carrying over the current run

- Each child keeps the streak the wall shows on launch day (see "Launch
  numbers" for the owner's decision). Money starts at $0 on launch day, plus
  any milestones the carried-over streak already passed.

### Settled after the builder check (my calls, owner can override)

- **Record start.** The new rules start on launch day (the seed carries the
  run before it). A day the wall never served (server down) counts like an
  away day, neither kept nor missed, so an outage is never a free kept day
  and never a break. Served days are recorded in a `served_days` table the
  moment today is frozen, even when nobody had anything that day. (Resolving
  unserved days live was considered and dropped: it rewrites history from
  today's chore list, which the frozen-history rule forbids.)
- **Rest days are kept days** for the streak, shields and milestones alike.
- **Run identity.** A run starts on the first kept day after an unshielded
  miss. A milestone pays when the streak first reaches it, keyed by
  (child, milestone, run start). A late fix that shifts the run start never
  pays the same milestone twice for overlapping runs, and nothing is clawed back.
- **Wallet math.** Wallet = all chore earnings + all bonuses − all payouts.
  An edit after a payout simply moves the wallet (old-day edits need the PIN,
  so a parent made it). It may show negative; that is honest, and rare.
- **Cents and kind are frozen into the log row** when the day is served
  (new `kind` column beside `cents`), so re-tagging a chore never rewrites
  the past. Existing rows backfill to 0 cents: money starts at launch.
- **Covering.** Whoever the day's row names (the backup covering an away
  child) earns the chore and has it count toward their streak, as today.
- **PIN.** Needed to turn Rewards on and to set the PIN the first time.
  Sent with each guarded request; the wall keeps it in memory for 2 minutes.
  Five wrong tries lock PIN actions for 5 minutes.
- **Cutoff.** House time, as `_today()`. "Yesterday" means one day back only.

### Launch numbers

Recomputing a real history under the new rules can jump several children
to within days of 100 at once, so how the current run carries over decides
real money in the first week.

**Owner's decision (2026-11-26): keep today's counts.** At launch each child's
run starts from the streak the wall shows on launch day under the old rules,
stored once as a seed (`kv`: per child, seed count and seed date). From
launch onward the new rules apply on top of the seed. Milestones the seed
already passed are paid once at launch as bonus rows (with the default
single $100-at-100 milestone, nobody below 100 is paid at launch; a child
at 71 gets the $100 when the same run reaches 100). Shields start at 0 and are
earned from launch-day kept days onward.

## Where it shows

- **Wall card:** flame line with "N / next goal" and shields; per-chore
  "+30¢" or "personal goal"; wallet line with next bonus. A tick floats a
  small "+30¢". Reaching a milestone gets a bigger celebration than the
  existing all-done confetti, and the card says "+$25 bonus!" for the day.
- **Week strip:** a "saved" state (shield) beside done, partial, none, rest, away.
- **Phone:** the same two lines, sized for the phone card.
- **Settings → Rewards:** PIN, base rate, the two streak styles, cutoff,
  shields, and per child a history list (earnings by week, bonuses,
  payouts) with Paid.
- **Projected cost** (owner: "you should be able to see projected costs per
  month, per year"): live as amounts are edited, before saving. Per child and
  family total, per week, month and year, in two rows. **Perfect streak**
  (every chore done, top level reached, every milestone hit on its cycle) is
  the most it can cost. **Usual pace** uses each child's last 8 weeks
  (house chores assigned, share done, current streak).
- **Feature toggle:** a "Rewards" entry in the integrations registry. Off
  means the wall shows today's plain streak chip and no money.
- **DEMO payload:** realistic wallets, shields and a milestone day.

## Data (builder's call on exact shape)

- `chores.kind TEXT NOT NULL DEFAULT 'house'` ('house' | 'personal').
- `occurrence_log.cents INTEGER` frozen per row when served (0 for personal
  and once).
- `ledger(id, person_id, kind ['bonus','payout'], cents, run_start, milestone,
  created_at, note)`. Chore earnings are not ledger rows; they are summed
  from completions joined to the log since the last payout.
- Settings and the PIN hash in kv.

## Constraints

- Follow `docs/adding-a-feature.md` to the last item, the three-agent review,
  README and `docs/hub.png` regenerated, live verify on the wall.
- Pure logic stays in `chores.py` (stdlib only, no I/O) with oracle-style
  tests over real-shaped histories, including the audit's cases (twice-a-week
  child, one-item misses, away week, late fix refunding a shield).
- No house data in the public repo.

## Left to the builder

Exact schema and migration, endpoint shapes, how the celebration looks within
the seasonal-looks and theme rules, PIN entry UI on the wall's on-screen
keyboard (`?kiosk=1`), and the ledger list layout.
