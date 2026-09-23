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
# mount, a second disk, an rsync-over-ssh module) and every run copies the
# whole tiered tree there (adding files only, then pruning each tier there by
# the same keep counts; it never mirrors deletions, so a new or empty local
# tree can't wipe the off-box history). A NAS host/credentials are the
# operator's to configure on the box -- they never live in this public repo.
# A remote failure exits 2 AFTER the local snapshot is safely committed, so a
# NAS outage is loud but never costs the local backup.
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
# glob only matches hub-*.db, so partials would otherwise accumulate forever).
# That includes the -wal/-shm sidecars a killed integrity check can leave: the
# snapshot starts out in the live db's WAL mode (see below).
for tier in hourly daily weekly monthly; do
  rm -f "$OUT/$tier"/.partial-*.db "$OUT/$tier"/.partial-*.db-wal \
        "$OUT/$tier"/.partial-*.db-shm 2>/dev/null
done

# One WAL-safe online-backup snapshot, then VERIFY it opens and passes
# integrity_check before trusting it. Temp file -> size-check -> atomic move.
# The online backup copies the live db's WAL journal mode, so opening the copy
# makes -wal/-shm sidecars next to it. It is switched to a plain rollback
# journal before it is closed: that folds everything into the one file, leaves
# no sidecar behind (some SQLite builds keep an empty -wal after close), and a
# restore copies one plain file.
TMP="$OUT/hourly/.partial-$HOURLY.db"
drop_tmp() { rm -f "$TMP" "$TMP-wal" "$TMP-shm"; }
python3 - "$DB" "$TMP" <<'PY' || { drop_tmp; fail "snapshot or integrity check failed"; }
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
src.close()
row = dst.execute("PRAGMA integrity_check").fetchone()
if not row or row[0] != "ok":
    dst.close()
    sys.stderr.write("integrity_check: %r\n" % (row,))
    sys.exit(3)
mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
dst.close()
if not mode or mode[0].lower() != "delete":
    sys.stderr.write("could not take the snapshot out of WAL mode: %r\n" % (mode,))
    sys.exit(4)
PY
# Some SQLite builds (Apple's) keep the -shm, or an empty -wal, after close
# even in rollback mode. With the copy closed and out of WAL they hold
# nothing, so they go; a -wal with content would mean frames were not folded
# in, and that snapshot is not trusted.
[ ! -s "$TMP-wal" ] || { drop_tmp; fail "snapshot left a non-empty WAL behind"; }
rm -f "$TMP-wal" "$TMP-shm"

BYTES=$(wc -c < "$TMP")
[ "$BYTES" -ge 8192 ] || { drop_tmp; fail "undersized snapshot ($BYTES bytes)"; }
mv "$TMP" "$OUT/hourly/hub-$HOURLY.db" || { drop_tmp; fail "atomic move failed"; }
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
#
# The same record carries the OFF-BOX outcome (remote_ok, remote_at = last
# attempt, remote_ok_at = last good copy), filled in by record_remote below
# once the mirror has run. This local write keeps whatever remote result is
# already there (a FH_SKIP_REMOTE run says nothing about the NAS), except when
# no remote is configured at all: then the remote fields are dropped, so an
# old NAS result can't go stale and nag forever.
if [ -z "$REMOTE" ] && [ "$SKIP_REMOTE" != "1" ]; then REMOTE_MODE=clear; else REMOTE_MODE=keep; fi
if ! hb_err="$(python3 - "$DB" "hub-$HOURLY.db" "$BYTES" "$REMOTE_MODE" 2>&1 <<'PY'
import sqlite3, sys, json, datetime as dt
db, name, nbytes, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
c = sqlite3.connect(db, timeout=5)
row = c.execute("SELECT value FROM kv WHERE key='backup_status'").fetchone()
try:
    old = json.loads(row[0]) if row else {}
except ValueError:
    old = {}
rec = {k: v for k, v in (old if isinstance(old, dict) else {}).items()
       if k.startswith("remote_") and mode == "keep"}
rec.update({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "snapshot": name, "bytes": nbytes})
c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('backup_status', ?)",
          (json.dumps(rec),))
c.commit(); c.close()
PY
)"; then
  echo "family-hub-backup: heartbeat write skipped (snapshot OK): $(printf '%s' "$hb_err" | tail -1)"
fi

# Record the off-box outcome into the heartbeat. Best-effort like the write
# above: it never changes the run's exit code, but a failure to record is
# printed, not swallowed.
record_remote() {  # <true|false>
  local err
  if ! err="$(python3 - "$DB" "$1" 2>&1 <<'PY'
import sqlite3, sys, json, datetime as dt
db, ok = sys.argv[1], sys.argv[2] == "true"
c = sqlite3.connect(db, timeout=5)
row = c.execute("SELECT value FROM kv WHERE key='backup_status'").fetchone()
try:
    rec = json.loads(row[0]) if row else {}
except ValueError:
    rec = {}
if not isinstance(rec, dict):
    rec = {}
now = dt.datetime.now(dt.timezone.utc).isoformat()
rec["remote_ok"] = ok
rec["remote_at"] = now
if ok:
    rec["remote_ok_at"] = now
c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('backup_status', ?)",
          (json.dumps(rec),))
c.commit(); c.close()
PY
)"; then
    echo "family-hub-backup: remote status write skipped: $(printf '%s' "$err" | tail -1)"
  fi
}

# Prune one tier of the off-box copy to its keep count, by the same rule as
# prune() above: newest-first by the stamp in the name. rsync lists the tier
# and deletes the extras, so this works the same for a mounted path and a
# 'host:/path' target. Only hub-<stamp>.db files are ever touched; anything
# else the operator keeps there stays. rsync deletes a file on the target
# that is --include'd but absent from the (empty) source, so the include list
# is exactly the files to remove.
remote_prune() {  # <tier> <keep>
  local tier="$1" keep="$2" listing extra empty rc f
  listing="$(rsync --list-only "$REMOTE/$tier/")" || return 1
  extra="$(printf '%s\n' "$listing" | awk '{print $NF}' \
    | grep -E '^hub-[0-9W-]+\.db$' | sort -r | tail -n +$(( keep + 1 )))"
  [ -n "$extra" ] || return 0
  empty="$(mktemp -d)" || return 1
  set --
  while IFS= read -r f; do set -- "$@" "--include=$f"; done <<< "$extra"
  rsync -r --delete "$@" --exclude='*' "$empty/" "$REMOTE/$tier/"
  rc=$?
  rmdir "$empty"
  return "$rc"
}

# Off-box copy of the whole tiered tree, unless suppressed (FH_SKIP_REMOTE=1,
# used by the pre-deploy snapshot which only needs a local restore point).
#
# ADD, THEN PRUNE -- never `rsync --delete` from the local tree. With --delete
# a new or empty local tree (a replaced disk, a changed FH_OUT) would mirror
# its emptiness and wipe every snapshot on the NAS on its first run, the one
# moment the off-box copy is all that is left. So the copy only adds files,
# and each off-box tier is then pruned to the same keep count as the local
# one (remote_prune): a fresh local tree adds to the NAS history and the old
# snapshots age out at the normal pace. .partial-* temp files never leave.
#
# NOT `rsync -a`: a NAS export (the intended target) commonly squashes client
# uids to one account, so preserving owner/group/perms fails with EPERM (rsync
# exit 23) even though the data copies fine -- which would false-trip the exit-2
# REMOTE FAIL every run. We only need content + mtimes off-box, so copy those
# and let the target own the perms: -r -t, and explicitly --no-owner/group/perms.
#
# MOUNT CHECK first: if the NAS mount drops, the mountpoint is just an empty
# folder on this box's own disk, and rsync would "mirror" into it and report
# OK while nothing left the box. FH_REMOTE_MOUNT names the mountpoint the
# target must sit under; unset (or "auto"), the script finds the mount the
# target lives on and refuses the root filesystem, which is never off-box.
# "none" turns the check off. A 'host:/path' rsync target has no local mount
# and is not checked. A failed check counts as a failed remote copy.
if [ -n "$REMOTE" ] && [ "$SKIP_REMOTE" != "1" ]; then
  case "$REMOTE" in
    /*|./*|../*) remote_is_local=1 ;;
    *:*) remote_is_local=0 ;;
    *) remote_is_local=1 ;;
  esac
  if [ "$remote_is_local" = "1" ] && ! mount_err="$(python3 - "$REMOTE" "${FH_REMOTE_MOUNT:-auto}" 2>&1 <<'PY'
import os, sys
remote, mode = os.path.realpath(sys.argv[1]), sys.argv[2]
if mode == "none":
    sys.exit(0)
if mode == "auto":
    mnt = remote
    while not os.path.ismount(mnt):
        mnt = os.path.dirname(mnt)
    if mnt == "/":
        sys.exit("target is on the root filesystem, not a mounted share")
    sys.exit(0)
mnt = os.path.realpath(mode)
if not os.path.ismount(mnt):
    sys.exit(f"{mode} is not a mountpoint")
if not (remote + "/").startswith(mnt.rstrip("/") + "/"):
    sys.exit(f"target is not under {mode}")
PY
)"; then
    echo "family-hub-backup REMOTE FAIL (not mounted: $mount_err): $REMOTE $(date -u +%FT%TZ)" >&2
    record_remote false
    exit 2
  fi
  if ! rsync -rt --no-owner --no-group --no-perms --exclude='.partial-*' "$OUT/" "$REMOTE/"; then
    echo "family-hub-backup REMOTE FAIL: $REMOTE $(date -u +%FT%TZ)" >&2
    record_remote false
    exit 2
  fi
  for spec in "hourly $HOURLY_KEEP" "daily $DAILY_KEEP" \
              "weekly $WEEKLY_KEEP" "monthly $MONTHLY_KEEP"; do
    # shellcheck disable=SC2086  # two words on purpose: <tier> <keep>
    if ! prune_err="$(remote_prune $spec 2>&1)"; then
      echo "family-hub-backup REMOTE FAIL (prune ${spec%% *}: $prune_err): $REMOTE $(date -u +%FT%TZ)" >&2
      record_remote false
      exit 2
    fi
  done
  record_remote true
  echo "family-hub-backup REMOTE OK: $REMOTE"
fi
