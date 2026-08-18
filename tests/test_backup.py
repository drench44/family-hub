"""Integration tests for backup/family-hub-backup.sh.

The script is the family's only insurance for hub.db (chores/people/todos
history originates here — a lost db loses it for good). These tests run the
REAL script against a temp SQLite db and assert the behaviour we depend on:
tiered snapshots (hourly/daily/weekly/monthly), per-tier pruning, off-box
replication when FH_REMOTE is set, and fail-loud on a missing source.

The script is driven entirely by env so it is deterministic under test:
  FH_DB, FH_OUT, FH_REMOTE, FH_SKIP_REMOTE, FH_NOW (a 'YYYYmmddHHMM' clock),
  and the per-tier keep counts (…_KEEP) are overridable.
"""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "backup" / "family-hub-backup.sh"


def _make_db(path: Path, rows=3):
    c = sqlite3.connect(path)
    c.execute("create table todos (id integer primary key, title text)")
    c.executemany("insert into todos (title) values (?)",
                  [(f"t{i}",) for i in range(rows)])
    c.commit()
    c.close()


def _run(db, out, *, now="202608180930", remote=None, skip_remote=False,
         extra_env=None, check=True):
    env = {
        "FH_DB": str(db), "FH_OUT": str(out), "FH_NOW": now,
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    if remote is not None:
        env["FH_REMOTE"] = str(remote)
    if skip_remote:
        env["FH_SKIP_REMOTE"] = "1"
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(SCRIPT)], env=env,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"backup failed ({r.returncode}): {r.stderr}\n{r.stdout}")
    return r


def _snaps(out, tier):
    return sorted((out / tier).glob("hub-*.db"))


def test_creates_a_valid_hourly_snapshot(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db, rows=5)
    out = tmp_path / "backup"
    _run(db, out)
    hourly = _snaps(out, "hourly")
    assert len(hourly) == 1, "one hourly snapshot per run"
    # it's a real, readable copy with the data intact
    c = sqlite3.connect(hourly[0])
    assert c.execute("select count(*) from todos").fetchone()[0] == 5
    c.close()


def test_one_run_seeds_every_tier(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    _run(db, out)
    for tier in ("hourly", "daily", "weekly", "monthly"):
        assert len(_snaps(out, tier)) == 1, f"{tier} tier seeded from the run"


def test_hourly_tier_prunes_to_keep(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    # five hourly runs, keep only 3 — oldest two pruned
    for hh in ("0900", "1000", "1100", "1200", "1300"):
        _run(db, out, now=f"20260818{hh}", extra_env={"HOURLY_KEEP": "3"})
    hourly = _snaps(out, "hourly")
    assert len(hourly) == 3, "hourly pruned to HOURLY_KEEP"
    assert hourly[-1].name == "hub-20260818-1300.db", "newest kept"
    assert hourly[0].name == "hub-20260818-1100.db", "oldest three kept, first two pruned"


def test_daily_tier_holds_one_per_day_and_prunes(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    # several runs across 4 distinct days, twice each; daily keeps 2
    for day in ("20260815", "20260816", "20260817", "20260818"):
        _run(db, out, now=f"{day}0800", extra_env={"DAILY_KEEP": "2"})
        _run(db, out, now=f"{day}1600", extra_env={"DAILY_KEEP": "2"})
    daily = _snaps(out, "daily")
    assert len(daily) == 2, "one snapshot per day, pruned to DAILY_KEEP"
    assert daily[-1].name == "hub-20260818.db"
    assert daily[0].name == "hub-20260817.db"


def test_offbox_replication_mirrors_when_FH_REMOTE_set(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    remote = tmp_path / "nas"
    _run(db, out, remote=remote)
    assert _snaps(remote, "hourly"), "the snapshot is replicated off-box"
    assert _snaps(remote, "daily"), "tiers replicate too"


def test_skip_remote_flag_keeps_it_local(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    remote = tmp_path / "nas"
    _run(db, out, remote=remote, skip_remote=True)
    assert _snaps(out, "hourly"), "local snapshot still taken"
    assert not remote.exists() or not _snaps(remote, "hourly"), \
        "FH_SKIP_REMOTE suppresses off-box replication (pre-deploy local-only)"


def test_fails_loud_on_missing_source_db(tmp_path):
    out = tmp_path / "backup"
    r = _run(tmp_path / "nope.db", out, check=False)
    assert r.returncode != 0, "a missing source db must fail, not silently no-op"
    assert not (out / "hourly").exists() or not _snaps(out, "hourly"), \
        "no snapshot is produced from a missing db"


def test_never_leaves_a_partial_snapshot_behind(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    _run(db, out)
    # no .partial-* temp files survive a successful run
    assert not list((out / "hourly").glob(".partial*")), "temp files cleaned up"


def test_timer_fires_hourly():
    t = (REPO / "backup" / "family-hub-backup.timer").read_text()
    assert "OnCalendar=hourly" in t, "the timer must fire hourly (not the old daily schedule)"


def test_service_is_oneshot_and_loads_boxside_env():
    s = (REPO / "backup" / "family-hub-backup.service").read_text()
    assert "Type=oneshot" in s
    # box paths / FH_REMOTE come from an optional env file, never the public unit
    assert "EnvironmentFile=-/etc/family-hub-backup.env" in s
    # box settings come via the env file, never hardcoded into the public unit:
    # no active `Environment=FH_...` directive bakes a real path/NAS target in.
    # (Illustrative `#   FH_DB=...YOUR-USER...` comment lines are fine.)
    assert "Environment=FH_" not in s


def test_remote_failure_exits_2_but_local_snapshot_survives(tmp_path):
    # The headline contract: a NAS outage is loud (distinct exit 2) yet the
    # local snapshot is already committed and valid — deploy.sh gates on the
    # 1-vs-2 distinction (1 = no local snapshot, abort; 2 = local OK, NAS down).
    db = tmp_path / "hub.db"
    _make_db(db, rows=4)
    out = tmp_path / "backup"
    blocker = tmp_path / "blocker"
    blocker.write_text("x")            # a regular file...
    remote = blocker / "nas"           # ...so rsync can't create a dir under it
    r = _run(db, out, remote=remote, check=False)
    assert r.returncode == 2, f"remote failure must exit 2 (got {r.returncode}): {r.stderr}"
    hourly = _snaps(out, "hourly")
    assert hourly, "the local snapshot is committed before the remote step"
    c = sqlite3.connect(hourly[0])
    assert c.execute("select count(*) from todos").fetchone()[0] == 4
    c.close()


def test_local_failure_exits_1_not_2(tmp_path):
    out = tmp_path / "backup"
    r = _run(tmp_path / "nope.db", out, check=False)
    assert r.returncode == 1, "a hard local failure is exit 1, distinct from remote's 2"


def test_corrupt_source_fails_loud_and_leaves_nothing(tmp_path):
    # A present-but-not-a-database source must never yield a file that LOOKS
    # like a good backup — and must leave no snapshot and no partial behind.
    bad = tmp_path / "hub.db"
    bad.write_bytes(b"not a sqlite database " * 500)   # > 8192 bytes, not sqlite
    out = tmp_path / "backup"
    r = _run(bad, out, check=False)
    assert r.returncode != 0, "a corrupt source must fail loudly, never report OK"
    assert not _snaps(out, "hourly"), "no snapshot is produced from a corrupt source"
    assert not list((out / "hourly").glob(".partial*")), "the failed run leaves no partial"


def test_page_corrupt_source_is_caught_not_backed_up(tmp_path):
    # A file with a valid SQLite header but garbage pages must not sail through
    # as a 'good' backup — integrity_check (or the backup read itself) rejects it.
    db = tmp_path / "hub.db"
    _make_db(db, rows=200)
    raw = bytearray(db.read_bytes())
    for i in range(100, len(raw)):     # scribble over everything past the header
        raw[i] = 0xFF
    db.write_bytes(raw)
    out = tmp_path / "backup"
    r = _run(db, out, check=False)
    assert r.returncode != 0, "a page-corrupt source must not produce a 'good' backup"
    assert not _snaps(out, "hourly")
    assert not list((out / "hourly").glob(".partial*"))


def test_zero_keep_is_rejected_not_silently_emptying(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    r = _run(db, out, extra_env={"HOURLY_KEEP": "0"}, check=False)
    assert r.returncode != 0, "KEEP=0 must fail, not empty the tier while printing OK"
    r2 = _run(db, out, extra_env={"DAILY_KEEP": "notanumber"}, check=False)
    assert r2.returncode != 0, "a non-numeric keep count is rejected"


def test_leading_zero_keep_count_prunes_as_base_10_not_octal(tmp_path):
    # '02' must mean 2 (base 10), not trigger an octal-arithmetic error that
    # silently skips pruning for that tier while the run still prints OK.
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    for hh in ("0900", "1000", "1100"):
        r = _run(db, out, now=f"20260818{hh}", extra_env={"HOURLY_KEEP": "02"}, check=False)
        assert r.returncode == 0, f"a leading-zero keep count must not error: {r.stderr}"
    hourly = _snaps(out, "hourly")
    assert len(hourly) == 2, "HOURLY_KEEP=02 keeps 2 (base 10) and pruning still runs"
    assert hourly[-1].name == "hub-20260818-1100.db", "the newest is kept"


def test_weekly_pruning_across_a_year_boundary(tmp_path):
    # ISO week is the one stamp format where chronological != obvious lexical
    # order across a year roll — pin that sort -r still treats 2026-W01 as newer
    # than 2025-W52. Mondays of W52-2025, W01-2026, W02-2026.
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    for now in ("202512220800", "202512290800", "202601050800"):
        _run(db, out, now=now, extra_env={"WEEKLY_KEEP": "2"})
    weekly = [w.name for w in _snaps(out, "weekly")]
    assert len(weekly) == 2, "weekly pruned to WEEKLY_KEEP"
    assert "hub-2025-W52.db" not in weekly, "the older year's week is pruned, not the newer"
    assert "hub-2026-W02.db" in weekly, "the newest week survives"


def test_offbox_replicated_copy_is_a_valid_db(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db, rows=7)
    out = tmp_path / "backup"
    remote = tmp_path / "nas"
    _run(db, out, remote=remote)
    rs = _snaps(remote, "hourly")
    assert rs, "snapshot replicated off-box"
    c = sqlite3.connect(rs[0])
    assert c.execute("select count(*) from todos").fetchone()[0] == 7, \
        "the off-box copy is a complete, valid db (not a truncated mirror)"
    c.close()


def test_stale_partial_is_swept_on_next_run(tmp_path):
    db = tmp_path / "hub.db"
    _make_db(db)
    out = tmp_path / "backup"
    (out / "hourly").mkdir(parents=True)
    (out / "hourly" / ".partial-20260101-0000.db").write_bytes(b"stale from a hard kill")
    _run(db, out)
    assert not list((out / "hourly").glob(".partial*")), \
        "a prior hard-kill's orphaned partial is swept on the next run"


def test_offbox_rsync_does_not_preserve_owner_group_perms():
    # A NAS export squashes client uids to one account, so `rsync -a` (owner/
    # group/perms) fails with EPERM (exit 23) even though the data copies —
    # which would false-trip the exit-2 REMOTE FAIL every run. Pin that the
    # off-box mirror copies content + mtimes only and lets the target own perms.
    import re
    s = SCRIPT.read_text()
    m = re.search(r'rsync\b[^\n]*"\$OUT/"\s+"\$REMOTE/"', s)
    assert m, "off-box rsync line not found"
    line = m.group(0)
    assert " -a" not in line, "off-box rsync must not use -a on a squashed NAS target"
    assert "--no-owner" in line and "--no-group" in line, \
        "off-box rsync must skip owner/group (NAS squash forbids chown/chgrp)"


def test_writes_backup_heartbeat_into_kv(tmp_path):
    # When hub.db has the app's kv table, a successful backup records a
    # 'backup_status' heartbeat the dashboard reads. (A db with NO kv table is a
    # silent no-op — exercised by every other test's todos-only source db.)
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.execute("create table todos (id integer primary key, title text)")
    c.execute("insert into todos (title) values ('t')")
    c.execute("create table kv (key text primary key, value text not null)")
    c.commit()
    c.close()
    _run(db, tmp_path / "out")
    c = sqlite3.connect(db)
    row = c.execute("select value from kv where key='backup_status'").fetchone()
    c.close()
    assert row is not None, "a successful backup must record a backup_status heartbeat"
    rec = json.loads(row[0])
    assert "at" in rec and rec["bytes"] > 0 and rec["snapshot"].startswith("hub-")


def test_heartbeat_absent_kv_table_is_silent_noop(tmp_path):
    # The common case (source db without a kv table) must still succeed cleanly:
    # no failure, and nothing on stderr from the heartbeat step.
    db = tmp_path / "hub.db"
    _make_db(db)
    r = _run(db, tmp_path / "out")
    assert r.returncode == 0
    assert "backup_status" not in r.stderr and "Traceback" not in r.stderr


def test_heartbeat_not_written_when_backup_fails(tmp_path):
    # The ordering guarantee: a FAILED backup must never advance the heartbeat,
    # or the dashboard would show green over a broken backup — the exact silent
    # failure this feature exists to catch. Source is a valid db with a kv table
    # holding an OLD backup_status; force the run to fail (OUT under a file, so
    # mkdir can't create the tiers even as root) and assert the row is untouched.
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.execute("create table kv (key text primary key, value text not null)")
    c.execute("insert into kv (key, value) values ('backup_status', ?)",
              (json.dumps({"at": "2000-01-01T00:00:00+00:00", "snapshot": "old"}),))
    c.commit()
    c.close()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                 # a regular file...
    r = _run(db, blocker / "out", check=False)   # ...so mkdir under it fails -> backup fails
    assert r.returncode != 0, "an unwritable OUT must fail the backup"
    c = sqlite3.connect(db)
    val = json.loads(c.execute(
        "select value from kv where key='backup_status'").fetchone()[0])
    c.close()
    assert val["at"] == "2000-01-01T00:00:00+00:00", \
        "a failed backup must not advance the heartbeat (no false-green badge)"
