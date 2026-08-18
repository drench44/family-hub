# Backups & restore

`hub.db` (SQLite) holds the only state that originates in family-hub — chores,
people, to-dos, completions, streaks. Code and config live in git; calendar
events re-sync; **a lost `hub.db` is gone for good.** So it is snapshotted on a
schedule, before every deploy, and (optionally) mirrored off the box.

## What runs

`backup/family-hub-backup.sh` takes one WAL-safe snapshot (SQLite online-backup
API — safe against the live database), size-checks it, atomically moves it into
place, then promotes it into four tiers and prunes each:

| Tier    | Cadence      | Kept | Covers        |
|---------|--------------|------|---------------|
| hourly  | every hour   | 48   | ~2 days       |
| daily   | 1×/day       | 14   | ~2 weeks      |
| weekly  | 1×/ISO week  | 8    | ~2 months     |
| monthly | 1×/month     | 12   | ~1 year       |

Layout under `FH_OUT` (default `/srv/backup/family-hub`, already an encrypted
gocryptfs vault on the garage box):

```
/srv/backup/family-hub/{hourly,daily,weekly,monthly}/hub-<stamp>.db
```

Pruning only ever deletes *older snapshots within a tier* — it never touches the
live database, and the monthly tier keeps ~a year of restore points.

The `systemd` timer (`backup/family-hub-backup.timer`) fires hourly; `deploy.sh`
also takes a fresh **local** snapshot before it touches the box and refuses to
deploy if that fails.

## Environment (box-side, never in this public repo)

The service reads `/etc/family-hub-backup.env`:

```sh
FH_DB=/home/<user>/docker-services/family-hub/data/hub.db
FH_OUT=/srv/backup/family-hub
# FH_REMOTE=/mnt/nas/family-hub    # uncomment once the NAS is mounted (below)
```

Other knobs (all optional): `FH_REMOTE`, `FH_SKIP_REMOTE=1` (local only),
`HOURLY_KEEP`/`DAILY_KEEP`/`WEEKLY_KEEP`/`MONTHLY_KEEP`, and `FH_NOW`
(`YYYYmmddHHMM`, for tests).

## Restore

Any snapshot is a plain SQLite file — pick the tier/timestamp you want:

```sh
cd ~/docker-services/family-hub
docker compose stop web
rm -f data/hub.db-wal data/hub.db-shm      # drop stale WAL sidecars first...
cp /srv/backup/family-hub/daily/hub-20260817.db data/hub.db   # ...then restore
docker compose start web
curl -s http://127.0.0.1:8138/health      # {"status":"ok"}
```

Clearing `hub.db-wal`/`hub.db-shm` before the copy is essential: the live db
runs in WAL mode, and leaving an old write-ahead log next to the restored file
lets SQLite replay stale frames onto it on the next open — silently reverting or
corrupting the restore. The snapshot itself has no sidecars (it's a fresh
default-journal db), so only the destination's need clearing.

## Enable off-box mirroring to a NAS

Off-box replication stays **off** until `FH_REMOTE` points somewhere that
survives losing the box/disk. Credentials and the NAS address are yours to
configure on the box — they never belong in this repo.

1. Mount the NAS share on the box (one time). For SMB, e.g. in `/etc/fstab`:
   ```
   //NAS-HOST/family-hub  /mnt/nas/family-hub  cifs  credentials=/etc/nas.cred,uid=<user>,_netdev,nofail  0 0
   ```
   with `/etc/nas.cred` (root-only, `chmod 600`) holding `username=…` /
   `password=…`. Then `sudo mount /mnt/nas/family-hub`. (NFS works too — mount
   it wherever you like and point `FH_REMOTE` there.)
2. Set `FH_REMOTE=/mnt/nas/family-hub` in `/etc/family-hub-backup.env`.
3. Verify: `sudo systemctl start family-hub-backup.service` then check the NAS
   path holds the tiered tree. A NAS outage later exits the run non-zero (loud
   in `journalctl -u family-hub-backup`) **after** the local snapshot is safely
   written, so it never costs you the local backup.
