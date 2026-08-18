#!/bin/bash
#
# family-hub-backup.sh -- consistent, verified, tiered snapshots of the
# family-hub SQLite db into a local backup tree, optionally mirrored off-box.
#
# WHY: chores/people/todos/completions history is family memory and the ONLY
# state that ORIGINATES in family-hub -- code and config are in git and calendar
# events re-sync, but a lost hub.db loses the family's lists, streaks and
# history for good. This is the cheap insurance, and it is why a deploy takes a
# fresh snapshot first (deploy.sh calls this with FH_SKIP_REMOTE=1).
#
# HOW: one WAL-safe snapshot per run via SQLite's online-backup API (python3
# stdlib), VERIFIED with PRAGMA integrity_check (a byte count can't tell a good
# backup from a large corrupt one), size-checked, atomically moved into place,
# then promoted -- also atomically -- into hourly/daily/weekly/monthly tiers so
# frequent runs don't balloon storage and an old-but-important state is never
# pruned away. Each coarser tier keeps the latest snapshot for its period.
#
# OFF-BOX: set FH_REMOTE to a path that survives losing the data dir/box (a NAS
# mount, a second disk, an rsync-over-ssh module) and every run mirrors the
# whole tiered tree there. A NAS host/credentials are the operator's to
# configure on the box -- they never live in this public repo. A remote failure
# exits 2 AFTER the local snapshot is safely committed, so a NAS outage is loud
# but never costs the local backup.
#
# FAIL-LOUD: a missing/corrupt source, a failed integrity check, an
# undersized/partial file, or a bad keep-count all abort non-zero and never
# masquerade as a good backup. Local failures exit 1; a remote-mirror failure
# exits 2 (local snapshot already safe).
#
# RESTORE: stop the web container, remove any stale WAL sidecars, copy the
# chosen snapshot over data/hub.db, start the container. See
# docs/backup-and-restore.md.
#
# All inputs are env so the script is deterministic under test:
#   FH_DB, FH_OUT, FH_REMOTE, FH_SKIP_REMOTE, FH_NOW ('YYYYmmddHHMM' clock),
#   and HOURLY_KEEP/DAILY_KEEP/WEEKLY_KEEP/MONTHLY_KEEP.
set -u

DB="${FH_DB:-$HOME/family-hub/data/hub.db}"
OUT="${FH_OUT:-/srv/backup/family-hub}"
REMOTE="${FH_REMOTE:-}"
SKIP_REMOTE="${FH_SKIP_REMOTE:-}"
NOW="${FH_NOW:-}"

HOURLY_KEEP="${HOURLY_KEEP:-48}"
DAILY_KEEP="${DAILY_KEEP:-14}"
WEEKLY_KEEP="${WEEKLY_KEEP:-8}"
MONTHLY_KEEP="${MONTHLY_KEEP:-12}"

fail() { echo "family-hub-backup FAIL: $1 $(date -u +%FT%TZ)" >&2; exit 1; }

[ -r "$DB" ] || fail "db missing/unreadable: $DB"

# Keep counts must be positive integers -- a fat-fingered env (KEEP=0, or a
# non-number) would otherwise prune away the snapshot just taken and the run
# would still print OK over an empty tier.
for kv in "$HOURLY_KEEP" "$DAILY_KEEP" "$WEEKLY_KEEP" "$MONTHLY_KEEP"; do
  case "$kv" in ''|*[!0-9]*) fail "keep counts must be positive integers (got '$kv')";; esac
  [ "$kv" -ge 1 ] || fail "keep counts must be >= 1 (got '$kv')"
done
# Normalize to base 10 so a leading-zero value ('08', '010') doesn't get read as
# octal in the prune arithmetic below (which would error and silently skip that
# tier's pruning). Runs in the main shell -- not a subshell -- so the fail()
# guards above still abort the whole script.
HOURLY_KEEP=$((10#$HOURLY_KEEP))
DAILY_KEEP=$((10#$DAILY_KEEP))
WEEKLY_KEEP=$((10#$WEEKLY_KEEP))
MONTHLY_KEEP=$((10#$MONTHLY_KEEP))

# Period stamps from FH_NOW ('YYYYmmddHHMM') or the current time. python keeps
# this portable across the box (GNU date) and a dev mac (BSD date), and the
# stamps sort chronologically as plain strings, which is what pruning relies on.
STAMPS=$(python3 - "$NOW" <<'PY'
import sys, datetime as dt
now = sys.argv[1]
t = dt.datetime.strptime(now, "%Y%m%d%H%M") if now else dt.datetime.now()
iso = t.isocalendar()
print(t.strftime("%Y%m%d-%H%M"), t.strftime("%Y%m%d"),
      "%04d-W%02d" % (iso[0], iso[1]), t.strftime("%Y%m"))
PY
) || fail "could not compute time stamps"
read -r HOURLY DAILY WEEKLY MONTHLY <<< "$STAMPS"
[ -n "$HOURLY" ] && [ -n "$DAILY" ] && [ -n "$WEEKLY" ] && [ -n "$MONTHLY" ] \
  || fail "incomplete time stamps: '$STAMPS'"

mkdir -p "$OUT/hourly" "$OUT/daily" "$OUT/weekly" "$OUT/monthly" \
  || fail "cannot mkdir under $OUT"

# Sweep orphaned temp files from a previous hard-kill / power loss (the prune
# glob only matches hub-*.db, so partials would otherwise accumulate forever
# and get mirrored off-box).
rm -f "$OUT"/hourly/.partial-*.db "$OUT"/daily/.partial-*.db \
      "$OUT"/weekly/.partial-*.db "$OUT"/monthly/.partial-*.db 2>/dev/null

# One WAL-safe online-backup snapshot, then VERIFY it opens and passes
# integrity_check before trusting it. Temp file -> size-check -> atomic move.
TMP="$OUT/hourly/.partial-$HOURLY.db"
python3 - "$DB" "$TMP" <<'PY' || { rm -f "$TMP"; fail "snapshot or integrity check failed"; }
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
src.close()
row = dst.execute("PRAGMA integrity_check").fetchone()
dst.close()
if not row or row[0] != "ok":
    sys.stderr.write("integrity_check: %r\n" % (row,))
    sys.exit(3)
PY

BYTES=$(wc -c < "$TMP")
[ "$BYTES" -ge 8192 ] || { rm -f "$TMP"; fail "undersized snapshot ($BYTES bytes)"; }
mv "$TMP" "$OUT/hourly/hub-$HOURLY.db" || { rm -f "$TMP"; fail "atomic move failed"; }
SNAP="$OUT/hourly/hub-$HOURLY.db"

# Promote into the coarser tiers -- ATOMICALLY (temp copy -> rename). Each tier
# keeps the LATEST snapshot for its period, so these exact slots are rewritten
# on every run; a crash/ENOSPC mid-copy must never truncate the previous good
# copy, so we never cp in place over a live restore point.
promote() {  # <tier-dir> <stamp>
  cp -f "$SNAP" "$1/.partial-$2.db" && mv "$1/.partial-$2.db" "$1/hub-$2.db"
}
promote "$OUT/daily"   "$DAILY"   || fail "daily promote failed"
promote "$OUT/weekly"  "$WEEKLY"  || fail "weekly promote failed"
promote "$OUT/monthly" "$MONTHLY" || fail "monthly promote failed"

# Prune each tier to its keep count. Ordering is by the chronological stamp in
# the NAME (sort -r = newest first), not mtime -- so pruning is deterministic
# even when many runs land in the same second, and portable (tail -n +N works
# on BSD and GNU; head -n -N does not). Keep counts are >= 1 (guarded above), so
# a tier is never emptied.
prune() {
  ls -1 "$1"/hub-*.db 2>/dev/null | sort -r | tail -n +$(( $2 + 1 )) \
    | while IFS= read -r f; do rm -f "$f"; done
}
prune "$OUT/hourly"  "$HOURLY_KEEP"
prune "$OUT/daily"   "$DAILY_KEEP"
prune "$OUT/weekly"  "$WEEKLY_KEEP"
prune "$OUT/monthly" "$MONTHLY_KEEP"

echo "family-hub-backup OK: hub-$HOURLY.db ($BYTES bytes) $(date -u +%FT%TZ)"

# Record a heartbeat in the live hub.db so the dashboard header can show backup
# health -- a STALE heartbeat also catches "backups stopped running at all"
# (timer disabled, box asleep), which nothing else surfaces. Best-effort: a
# good, verified snapshot must NEVER fail over this telemetry write, so it runs
# after the OK line and can only exit 0. But a PERSISTENT write failure would
# show a false 'stale' badge, so surface the cause on STDOUT (the backup's own
# log) instead of swallowing it -- while keeping stderr clean and rc 0. A fresh
# install whose hub.db has no kv table yet reads as this same skipped note.
# python3 (already required above) keeps it dependency-free (no sqlite3 CLI).
if ! hb_err="$(python3 - "$DB" "hub-$HOURLY.db" "$BYTES" 2>&1 <<'PY'
import sqlite3, sys, json, datetime as dt
db, name, nbytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
c = sqlite3.connect(db, timeout=5)
c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('backup_status', ?)",
          (json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "snapshot": name, "bytes": nbytes}),))
c.commit(); c.close()
PY
)"; then
  echo "family-hub-backup: heartbeat write skipped (snapshot OK): $(printf '%s' "$hb_err" | tail -1)"
fi

# Off-box mirror of the whole tiered tree, unless suppressed (FH_SKIP_REMOTE=1,
# used by the pre-deploy snapshot which only needs a local restore point).
#
# NOT `rsync -a`: a NAS export (the intended target) commonly squashes client
# uids to one account, so preserving owner/group/perms fails with EPERM (rsync
# exit 23) even though the data copies fine -- which would false-trip the exit-2
# REMOTE FAIL every run. We only need content + mtimes off-box, so copy those
# and let the target own the perms: -r -t, and explicitly --no-owner/group/perms.
if [ -n "$REMOTE" ] && [ "$SKIP_REMOTE" != "1" ]; then
  rsync -rt --delete --no-owner --no-group --no-perms "$OUT/" "$REMOTE/" \
    || { echo "family-hub-backup REMOTE FAIL: $REMOTE $(date -u +%FT%TZ)" >&2; exit 2; }
  echo "family-hub-backup REMOTE OK: $REMOTE"
fi
