# Backup & Restore

justUp! can back up its entire state into an archive and restore from it. Backup
and restore are integrity-checked.

## Backup

A backup bundles the database, measurement data, and reports into an archive with
an integrity check (HMAC-SHA256 over the full digest). The key for this comes from
the environment; it is not in the archive.

The SQLite databases go into the archive as a **consistent snapshot** (SQLite Online Backup API),
not as a copy of the running file; concurrent writes do not disturb the backup
(`-wal`/`-shm` files are not in the archive). Each measurement series (`tsdb`: the active `.jsonl` file together with its
compressed `.jsonl.gz` segments) is opened under one file lock and copied as of that moment: it is never half-written,
never shows a day twice or not at all, and does not delay the monitoring writes; lock and temp files are not in the archive. Backups made
before the compressed format (plain `.jsonl` only) restore unchanged and are compressed in the
background afterwards. Because the segments are already gzip-compressed, the bzip2 archive of a
backup is larger than before for the same data (see the benchmark in
[Database maintenance](../development/database-maintenance.md)) but is created and restored faster.
Reports (`pdf_reports`) are taken over as files.

!!! note "What the backup guarantees – and what it does not"
    - Guaranteed: each SQLite database is a transaction-consistent snapshot, each measurement series a
      complete, untorn state of that series.
    - Not guaranteed: a common point in time across all files. Database and series are read sequentially;
      measurement points created in the meantime may be contained in one series and missing from
      another (or from the database). Monitoring operation is deliberately not paused for the backup.
    - HMAC and `quick_check` prove immutability and readable databases, not the consistency of the
      measurement data among itself.
    - A backup does not run concurrently with a restore: it then responds with 503 and `Retry-After`.

The file name carries the full HMAC-SHA256 (`justup_backup_<date>_<time>_<64 hex>.tar.bz2`).
The signing key is derived domain-separated from `SECRET_KEY` (or `SECRET_KEYS`) or provided via
`JUSTUP_BACKUP_HMAC_SECRET`. A provided secret must have at least 32 characters with
at least 8 distinct characters (e.g. `openssl rand -hex 32`), otherwise backup and restore reject
with a configuration error (the value is never printed). Before extraction, the number and total size of
entries are limited; only regular files and directories under `data/sqlite`, `data/tsdb`, and
`data/pdf_reports` are allowed (no links, devices, or paths outside).

!!! danger "Breaking change in 1.0.0"
    Backups of older versions (16 hex characters, key in the database) are rejected with an error message
    and must be recreated.

!!! warning "Keep the key separate"
    A backup is not fully usable without the matching `SECRET_KEY`, because the
    contained secrets are Fernet-encrypted. Keep the key separate from the backup.

### Key rotation

- With `SECRET_KEYS` (rotation), **all** configured keys are checked (in a single read pass,
  without early abort); backups signed with an earlier key remain restorable
  as long as that key is in `SECRET_KEYS`. New backups are signed by the first key.
- `JUSTUP_BACKUP_HMAC_SECRET` supports the same rotation as a comma-separated list `NEW,OLD`: the first
  key signs, all verify (likewise in a single read pass without early abort). Each entry
  is validated individually (min. 32 characters, min. 8 distinct); empty or duplicate entries are rejected
  with a configuration error, without printing the value. A key removed without replacement thereby makes
  backups signed with it unverifiable – keep the old value in the list until then.
- Session tokens are hashed with a pepper derived from the **first** key. If the
  primary key changes, all users sign in again (encrypted data and backups stay readable
  as long as the old key remains in `SECRET_KEYS`).

## Restore

The restore runs in this order:

```mermaid
graph LR
  Upload[Upload] --> Hmac[Verify HMAC]
  Hmac --> Extract[Extract & check structure]
  Extract --> Check[SQLite quick_check per database]
  Check --> Quiesce[Maintenance mode & quiescing]
  Quiesce --> Apply[Swap directories]
  Apply --> Restart[Restart]
```

A valid HMAC only proves that the bytes are unchanged, not that they yield an intact database;
therefore every contained `.db` must pass `PRAGMA quick_check` before anything is touched.
First, **maintenance mode** is activated: scheduler runs (checks, TSDB writes, rollups,
notification spooler), PDF jobs including the cleanup run, and backups no longer start
again; work already running is awaited for up to 30 s and never aborted. Only then does quiescing follow.
The scheduler and the PDF worker pause during this time and resume on their own afterwards if the restore
aborts. Maintenance mode applies per process; the container image starts exactly one app process. A
`WORKERS` environment variable is not supported.

During quiescing, justUp no longer accepts new database connections (requests receive 503 with
`Retry-After`) and waits up to 30 s for active users to close their connections themselves; foreign
connections are never closed. If neither maintenance mode nor quiescing is reached in time,
the restore aborts with 503 and `Retry-After` without changing anything, and both locks are released
again. This also applies to any other error before or during the apply. If the apply fails and
is rolled back, operation is immediately free again. After success (or if the rollback also fails),
the database and maintenance mode stay locked until the process is restarted.

## Storage statistics and purge

Admins see a storage statistic and can clean up selectively:

- **Purge** of alerts and measurement data
- Resetting a single target's measurement data

See also [Storage & Retention](../configuration/storage.md).
