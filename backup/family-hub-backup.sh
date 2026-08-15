#!/bin/bash
#
# family-hub-backup.sh -- nightly consistent snapshot of the family-hub SQLite
# into a backup directory (optional but recommended).
#
# WHY: chores/people/completions history is family memory and the ONLY state
# that ORIGINATES in family-hub -- code and config are in git and calendar events re-sync, but a lost hub.db loses the
# family's streaks and history. This is the cheap insurance.
#
# The snapshot uses SQLite's online-backup API (via python3's stdlib sqlite3),
# which is safe against the live WAL database. Point OUT somewhere that
# survives whatever happens to the data dir (a second disk, a NAS mount,
# an encrypted vault).
#
# FAIL-LOUD: partial or empty snapshots never masquerade as good ones -- the
# temp file is size-checked and only then atomically moved into place.
#
# RESTORE: stop the web container, copy a snapshot over data/hub.db,
# start the container. (Plain SQLite file; nothing else needed.)
set -u

DB="${FH_DB:-$HOME/family-hub/data/hub.db}"
OUT="${FH_OUT:-/srv/backup/family-hub}"
KEEP=14

fail() { echo "family-hub-backup FAIL: $1 $(date -Is)" >&2; exit 1; }

[ -r "$DB" ] || fail "db missing/unreadable: $DB"
mkdir -p "$OUT" || fail "cannot mkdir $OUT"

STAMP=$(date +%F)
TMP="$OUT/.partial-hub-$STAMP.db"

python3 - "$DB" "$TMP" <<'EOF' || fail "sqlite online backup failed"
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
EOF

BYTES=$(wc -c < "$TMP")
[ "$BYTES" -ge 8192 ] || fail "undersized snapshot ($BYTES bytes)"
mv "$TMP" "$OUT/hub-$STAMP.db" || fail "atomic move failed"

# prune to the newest $KEEP snapshots
ls -1t "$OUT"/hub-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

echo "family-hub-backup OK: hub-$STAMP.db ($BYTES bytes) $(date -Is)"
