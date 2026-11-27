# Backups that plug into a NAS: design

Approved by the owner 2026-11-27 ("That plan works").

## Why

The hub's only irreplaceable state is `data/hub.db`. Today it is backed up by
`backup/family-hub-backup.sh`, a host-side systemd timer the owner's box runs
hourly to a NAS. Someone who installs the hub from GitHub gets none of that
unless they install systemd units by hand, and nothing anywhere saves the
settings files (`config.json`, `.env`, `data/pin.key`, `data/wyze.env`), which
hold the calendar IDs, tokens and passwords a rebuilt box needs.

Goal: backups that work out of the box in the container, land on a NAS with one
line of setup, and also save the settings files safely.

## Goals

- The hub backs itself up from inside the container, hourly, with no host
  timer to install.
- One setting points backups at any folder; a NAS is just a folder the host has
  mounted.
- The database snapshot is verified and tiered exactly like the box script:
  WAL-safe online backup, `PRAGMA integrity_check`, out of WAL mode, atomic
  rename, hourly 48 / daily 14 / weekly 8 / monthly 12, pruned by the stamp in
  the name, a tier never emptied.
- The settings files are saved too, encrypted with a backup password the parent
  types once in Settings, so a NAS copy never exposes tokens.
- Settings > Backups shows the last good backup, where it went, how many are
  kept, a Back up now button, and lets a parent set the password.
- The wall's existing "Backup stale" badge covers the new job (same
  `backup_status` heartbeat).
- Restoring is documented and has a command for the settings bundle.

## Non-goals

- The hub mounting SMB/NFS itself. The container drops every capability and
  runs unprivileged; mounting is the host's job.
- Replacing the box's systemd backup. It keeps running unchanged.
- Restoring from the wall. Restore is a deliberate box-side step.
- Encrypting the database snapshots (they restore with no password, like
  today's box snapshots; the owner's NAS copy sits in an encrypted vault).

## Approaches considered

1. **In-container job writing to a mounted folder (chosen).** compose mounts
   `${BACKUP_DIR:-./backups}` at `/backups`. Works with any NAS the host can
   mount, keeps the container unprivileged.
2. Host systemd timer only, with an installer. Needs root on the host and does
   nothing on Docker Desktop or a NAS-hosted install.
3. The app pushes to the NAS over SMB/WebDAV itself. Needs NAS credentials in
   the app, a client library per protocol, and more ways to fail.

## Design

### Where

- `docker-compose.yml`: `- ${BACKUP_DIR:-./backups}:/backups` on `web`, and
  `- ./.env:/app/.env:ro` so the settings bundle can include it (optional: a
  missing `.env` is skipped, not an error).
- `.env.example` (or README) documents `BACKUP_DIR=/mnt/nas/family-hub`.
- Env `BACKUPS_PATH` (default `/backups`) inside the container.

### Job

- New module `src/family_hub/backups.py`, pure functions plus one `run_once(now)`.
- Runs on the app's loop as a lifespan task (like the laundry watcher): first
  run a few minutes after start, then hourly. Off in demo mode and when
  `DISABLE_SYNC` is set. On only when the Backups integration is on (default on
  when `/backups` is a writable directory that is not the container's own
  overlay, i.e. something was mounted there; the card says why when off).
- Each run: snapshot hub.db into `/backups/db/<tier>/hub-<stamp>.db` (same rules
  as the box script), then, if a password is set, write
  `/backups/settings/settings-<stamp>.fhb` (encrypted bundle, keep 14), then
  write the heartbeat. A failure is logged with the reason and recorded in the
  heartbeat (`in_app_error`), never crashes the task.
- The work runs in a thread (`asyncio.to_thread`) so a slow NAS never blocks
  the wall. One run at a time (a lock); Back up now uses the same lock.

### Settings bundle

- A tar of whichever exist: `config.json`, `.env`, `data/pin.key`,
  `data/wyze.env`, `data/token.json`, stored under short names.
- Encrypted with AES-256-GCM (`cryptography`, new pinned dependency). Key =
  scrypt(password, 16-byte salt, n=2**15, r=8, p=1). File = `FHB1` magic,
  salt, 12-byte nonce, ciphertext+tag. The header is the GCM associated data.
- The password is never stored. The derived key and its salt are stored in
  `data/backup.key` (0600), made once like `pin.key`. A stolen NAS copy alone
  is useless; restoring needs the password.
- Changing the password makes a new key; old bundles still need the old one
  (the card says so).
- `python -m family_hub.backups restore-settings <file> <out dir>` asks for the
  password and unpacks the files; `--list` shows the newest backups.

### Heartbeat

- Writes the same `backup_status` kv record the box script writes (`at`,
  `snapshot`, `bytes`), plus `source: "in-app"`, `dir`, `settings_at`,
  `in_app_error`. Whichever job ran last updates `at`, so the wall badge stays
  right on a box running both.

### API

- `GET /api/backups`: enabled, why off, dir, last good, last error, counts per
  tier, newest settings bundle, password set.
- `POST /api/backups/run`: Back up now (409 while one runs).
- `POST /api/backups/password` `{password}`: at least 8 characters; needs the
  parent PIN when one is set.

### Settings card

- New card `backups-card`, section "Backups" in the Settings menu.
- Shows: on/off and why, folder, last good backup (relative time), copies
  kept, last error in plain words, Back up now, set/change backup password with
  "Write this down. You need it to restore the settings files on a new box."
- A How it works topic for Backups.

### The owner's box

- The private deploy overlay mounts the NAS folder `/mnt/nas/family-hub/in-app`
  (or similar) as `BACKUP_DIR`. The systemd timer keeps running. The owner sets
  the backup password once.

## Left to the builder

- Exact card layout and copy within the house style.
- Test layout, following `docs/adding-a-feature.md`.
