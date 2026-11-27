# Backups & restore

`hub.db` (SQLite) holds the only state that originates in family-hub — chores,
people, to-dos, completions, streaks. Code and config live in git; calendar
events re-sync; **a lost `hub.db` is gone for good.** So it is snapshotted on a
schedule, before every deploy, and (optionally) mirrored off the box.

Two things can take those snapshots, with the same names, checks and keep
counts:

- **The hub itself** (`src/family_hub/backups.py`), hourly, into whatever
  folder is mounted at `/backups`. Nothing to install. It also saves the
  settings files, encrypted, once a backup password is set. This is the
  default for anyone who installs from GitHub.
- **The box script** (`backup/family-hub-backup.sh` with its systemd timer),
  for a box that wants a host-side job, a pre-deploy snapshot, or an rsync
  mirror. Unchanged; it can run beside the hub's own job.

Both write the hub's `backup_status` record, so the wall's "Backup stale"
badge reads whichever ran last.

## The hub's own backup

**Where.** `docker-compose.yml` mounts `${BACKUP_DIR:-./backups}` at
`/backups`. `./backups` ships in the repo (an empty folder with a
placeholder), so docker never creates it owned by root. For a NAS, mount the
share on the box and set `BACKUP_DIR` in `.env`:

```sh
BACKUP_DIR=/mnt/nas/family-hub
```

then `docker compose up -d web`. The hub mounts nothing itself: the
container drops every capability and runs unprivileged, so mounting SMB or
NFS is the box's job (the fstab example under "Enable off-box mirroring"
below works here too, with `uid=` set to `HUB_UID`). Outside docker, set
`BACKUPS_PATH` to any writable folder.

**When.** A few minutes after the hub starts, then every hour. It is off in
the demo and under `DISABLE_SYNC=1` (tests), where Back up now still works.
The work runs in a thread, one run at a time, so a slow NAS never holds up
the wall. A failed run is logged with the reason, shown on the Settings card
and recorded in `backup_status.in_app_error`; it never stops the next run.

**It is off, and says why, when** the Backups switch (Features &
integrations) is off, the folder is missing, the default `/backups` is not a
mount (a folder baked into the container would vanish with it), or the hub's
user can't write to it.

**Layout:**

```
<BACKUP_DIR>/db/{hourly,daily,weekly,monthly}/hub-<stamp>.db
<BACKUP_DIR>/settings/settings-<stamp>.fhb
```

The database copies follow the box script exactly: SQLite's online backup,
`PRAGMA integrity_check`, out of WAL mode, a size floor, a temp file and an
atomic rename, promotion into the coarser tiers by temp copy and rename, and
pruning by the stamp in the name to 48 / 14 / 8 / 12, never emptying a tier.
Leftover `.partial-*` files from a killed run are swept first. Other files in
the folder are never touched.

**A dropped NAS mount.** From inside the container a bind mount always looks
mounted, so the hub can't ask whether the share is there. Instead the first
good run writes `.family-hub-backups` into the folder, holding a random id the
hub also keeps (kv `backups.marker`). If the share drops, the box's mount point
is an empty folder on its own disk, the file is missing, and the hub refuses
to back up there: the card says "The backup folder isn't the one backups went
to before. Is the NAS mounted?", the heartbeat's `in_app_error` says the same,
and `at` stays put, so the wall's stale badge fires on time. Remount the share
and the next run goes through. If you moved backups to a new folder on
purpose, tap **Use this folder** on the card (it needs the parent PIN when one
is set) to write a fresh id there.

**Stuck or skipped runs.** Each time the reason an hourly run is skipped
changes (switched off, no folder, a run already going), the log says so
once. A run that has held the lock for more than 20 minutes (a hung NAS) is
recorded as "Backup stuck" on the card and in `in_app_error`. If Back up now
copies the database but can't write its record, it answers with that error
instead of a stale "ok". `/health/full` lists the job under
`notes.backups_in_app` (information only, never a failing status).

**A settings file that disappears.** If a file that was in the last settings
bundle is gone now (a deleted `.env`, a lost `pin.key`), no smaller bundle is
written over the good one. The card names the file and the log says so once.
Put it back, or set the backup password again to start fresh.

### The monthly restore test

A backup nobody has opened is a hope, not a backup. Once a month the hub
proves its newest copy really restores, and the card says "Last restore test
passed November 3" (or that it failed, and why).

**What it checks.** It takes the newest database copy (the newest hourly one,
or the newest in the next tier when the hourly folder is empty), copies it
into a private temp folder (mode 0700, removed afterwards), and opens that
copy read-only. Then:

1. `PRAGMA integrity_check` answers `ok`.
2. The tables a hub needs are there: `kv`, `people`, `chores`, `completions`,
   `occurrence_log`, `ledger` and `todos`.
3. The row counts of `people`, `chores`, `occurrence_log` (chore history) and
   `ledger` (the wallet) hold up against the live database: a table with rows
   live must have rows in the copy, and at least 90% as many. A copy far
   behind fails as "old or cut short". A table may be one row short with no
   failure, so a person added since the last hourly copy does not fail it.
4. With a backup password set, the newest settings bundle is decrypted in
   memory with the key in `data/backup.key` and must hold every file the last
   backup recorded (`backups.in_app.settings.files`). Nothing decrypted is
   written anywhere. A bundle made with an older password fails with that
   reason; the next backup writes a new one. When the bundle can't be checked
   (no backup password set, a key that can't be used, no settings backup
   yet), the test still passes on the database, but the card says so: "Database
   restored on November 27. Settings weren't checked: no backup password set."

It never touches `data/hub.db` or the files in the backup folder. If the temp
folder can't be removed afterwards, the log names it: it holds a full copy of
the database, so delete it by hand.

**When.** From the hourly run, right after its copy, so it tests a copy
minutes old: the first test a day after the first good backup
(`backups.in_app.first_ok_at`), then again once the last passed test is 30
days old. A failed test is tried again each day until it passes. **Test
restore now** on the card runs it at once. It shares the backup lock, so it
is refused while a backup runs, and it needs a backup to exist first.

**What a failure does.** The result goes in kv `backups.restore_test`
(`at`, `ok`, `snapshot`, `reason`, `settings_file`), and the card shows the
reason in plain words. The log says so once for each new reason. It never
moves the backup heartbeat (`backup_status`), so the wall's stale badge stays
about whether copies are being made. `/health/full` lists the last result
under `notes.backups_restore_test` (information only, never a failing status).
A test that could not run at all (the live database unreadable, say) is saved
as failed too, so it shows and retries in a day.

**Overdue.** If no test has passed in 35 days while backups are on and the
hourly job runs, the card says "No restore test has passed in over 35 days"
and the note carries `overdue: true`, so a schedule that never fires can't
look like quiet success.

### The settings bundle and backup.key

With a backup password set (Settings → Backups, at least 8 characters, and
the parent PIN when one is set), each run also saves whichever of these
exist:

| In the bundle | From | Restores to |
|---|---|---|
| `config.json` | `config.json` | the project folder |
| `env` | `.env` (mounted read-only at `/app/.env`) | `.env` in the project folder |
| `pin.key` | `data/pin.key` | `data/` |
| `wyze.env` | `data/wyze.env` | `data/` |
| `token.json` | `data/token.json` | `data/` |

The files are packed in a tar and encrypted with AES-256-GCM. The key is
scrypt(password, 16-byte salt, n=2^15, r=8, p=1). A bundle is the bytes `FHB1`,
the salt, a 12-byte nonce, then the ciphertext and tag; the first 32 bytes
are the GCM associated data, so a changed header is refused too. A new bundle
is written only when a file changed (or the password did); the last 14 are
kept.

The password itself is never stored. `data/backup.key` (mode 0600, written
to a temp file and renamed, like `pin.key`) holds the salt and the key made
from it, so the hourly job can encrypt with nobody typing anything, while a
copy of the backup folder alone can't be opened. If `backup.key` goes missing
or is damaged after a password was set, the card says so and bundles stop
until the password is set again. Changing the password makes a new key:
bundles saved before still need the old password.

**`.env` must exist.** compose mounts it with the long bind form and
`create_host_path: false`. The short form (`./.env:/app/.env`) would turn a
missing `.env` into an empty folder owned by root, which the owner then can't
replace with the file. With the long form a missing `.env` stops
`docker compose up` with "bind source path does not exist" instead. The quick
start creates it (`cp .env.example .env`); on an older install without one,
run `cp .env.example .env` (or `touch .env`) once before `up`.

### Restore the settings files

```sh
cd ~/family-hub                       # the project folder
docker compose run --rm web python -m family_hub.backups --list
mkdir -p restored
docker compose run --rm -v "$PWD/restored:/restored" web \
  python -m family_hub.backups restore-settings \
  /backups/settings/settings-20260817-0900.fhb /restored
```

It asks for the backup password, then writes the files into `./restored`
under their real names (mode 0600) and says where each one goes. A wrong
password or a damaged file writes nothing, and it never overwrites a file, so
point it at an empty folder. Move the files into place, then
`docker compose up -d`.

On a new box, restore the settings files first (so `data/pin.key` comes back
and the parent PIN keeps working), then the database as below. Set the backup
password again in Settings afterwards: `backup.key` is not in the bundle.

## The box script

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
gocryptfs vault on the homelab server):

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
`FH_REMOTE_MOUNT` (the mountpoint `FH_REMOTE` must sit under; by default the
script refuses a target on the root filesystem, which is what a dropped NAS
mount leaves behind; `none` turns the check off),
`HOURLY_KEEP`/`DAILY_KEEP`/`WEEKLY_KEEP`/`MONTHLY_KEEP`, and `FH_NOW`
(`YYYYmmddHHMM`, for tests).

## Restore

Any snapshot is a plain SQLite file, from either job. The hub's own copies
are under `<BACKUP_DIR>/db/<tier>/`; the box script's under `FH_OUT`. Pick the
tier and time you want:

```sh
cd ~/docker-services/family-hub
docker compose stop web
rm -f data/hub.db-wal data/hub.db-shm      # drop stale WAL sidecars first...
cp /srv/backup/family-hub/daily/hub-20260817.db data/hub.db   # ...then restore
docker compose start web
curl -s http://127.0.0.1:8138/health      # {"status":"ok"}
```

**The parent PIN on a new box.** The PIN's secret key lives in `data/pin.key`,
beside `hub.db` but not inside it, so a copy of the database alone cannot be
used to guess the PIN. The database snapshots copy only `hub.db`, so the key
survives a restore only when the box's `data/` folder did, or when you put it
back from a settings bundle (above). Restoring onto a
new box (or after losing `data/`) leaves the chore wallet's money actions
locked, and the wall and the log say why. Clear the PIN (and set a new one
in Settings if you want one):

```sh
docker compose exec web python -m family_hub.parent_pin --clear
```

The same command is the way back if someone at the wall sets a PIN nobody
knows. With no PIN set, anyone at the wall can pay out, change amounts, and
also set a PIN; that is the trade the optional PIN makes.

Clearing `hub.db-wal`/`hub.db-shm` before the copy is essential: the live db
runs in WAL mode, and leaving an old write-ahead log next to the restored file
lets SQLite replay stale frames onto it on the next open — silently reverting or
corrupting the restore. The snapshot itself is one self-contained file with no
sidecars: the online backup copies the live db's WAL mode into it, so the
script switches it to a plain rollback journal (folding everything into the
file) before closing it, and sweeps any `.partial-*.db-wal`/`-shm` a killed run
left behind. Snapshots taken before that change are still marked WAL in their
header; that is harmless (they were fully checkpointed, and SQLite simply starts
a fresh log on open). Only the destination's sidecars need clearing.

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
   written, so it never costs you the local backup. Each run also records the
   NAS result in the hub's backup heartbeat, and the wall header shows an
   "Off-box backup failing" (or "stale") badge until a copy succeeds again.

The off-box copy only ever **adds** files; it never mirrors deletions from the
local tree. Each NAS tier is then pruned to the same keep count as the local
one (newest by the stamp in the name), touching only `hub-<stamp>.db` files. So
if the local tree is ever new or empty (a replaced disk, a changed `FH_OUT`),
the first run adds to the NAS history instead of wiping it, and the old
snapshots age out at the normal pace. A failed prune counts as a failed remote
copy (exit 2).
