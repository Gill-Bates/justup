# Backup & Restore

Admins back up the whole state into one signed archive (Settings > Backup) and
restore from it.

## Backup

The archive `justup_backup_<date>_<time>_<64 hex>.tar.bz2` contains the SQLite
databases, the measurements, PDF reports, and `data/backup-manifest.json`. The
64 hex characters are the HMAC-SHA256 signature over the complete archive,
including the manifest.

- Databases are consistent snapshots. Monitoring keeps running during a backup.
- Measurement series are copied as complete series. The files are not captured at a
  single common point in time; a few recent points can be in one series but not in
  another.
- A backup cannot run during a restore (HTTP 503 with `Retry-After`).

The signing key is derived from `SECRET_KEY(S)` or set with
`JUSTUP_BACKUP_HMAC_SECRET`; see [Environment variables](../configuration/environment.md).
It is not stored in the archive.

!!! warning "Keep the key separate"
    A restored backup needs the matching `SECRET_KEY`, because stored secrets are
    encrypted. Store the key apart from the backup.

!!! danger "Legacy backup format"
    Backups without a versioned manifest, including backups from before 1.0.0,
    are rejected. Create a new backup with a version that writes the manifest.

### Format and compatibility

The manifest records format version `1`, the application version and build ID,
the SQLite `user_version` read from the archived snapshot, a schema fingerprint,
and the UTC creation time. Its table, index, view, and trigger metadata is read
from that same snapshot; it contains object names and definition hashes, not
database rows. The fingerprint verifies the archived structure. The normal
SQLite compatibility check, not a comparison with a fresh-install fingerprint,
decides whether the target release supports that structure.

Application and schema versions are compared independently; the outcomes are listed under [Compatibility](#compatibility).

Versions must be numeric and comparable. Development builds are accepted only
when both sides report `dev` and their non-empty build IDs match exactly.
Missing, invalid, duplicate, inconsistent, or unsupported manifest metadata is
rejected. The application database `data/sqlite/justup.db` is required.

### Confidentiality

!!! warning "Backups are signed, not encrypted"
    The HMAC only proves integrity and origin. The archive holds user data, hashed
    passwords, encrypted settings, measurements, and reports in readable form.
    Restrict access to it, and encrypt it (for example with `age`, GPG, or an
    encrypted volume) before storing it outside the server. The server writes its
    temporary archive with mode `0600` and deletes it after the download.

### Key rotation

Backups signed with an earlier key stay restorable while that key remains in
`SECRET_KEYS` or in `JUSTUP_BACKUP_HMAC_SECRET` (`NEW,OLD`). New backups use the
first key. Changing the first `SECRET_KEY` signs all users out.

## Restore

1. Upload the archive and enter the admin password.
2. justUp! verifies the signature, then the archive paths and the manifest, and compares the
   versions (see below) before it extracts anything.
3. It extracts into a staging directory and checks the archived databases: the schema must match
   the manifest, the application database must contain no triggers or views, and every database
   must pass the integrity check.
4. A version-1 backup or an exact former-current version-10 backup is migrated to schema version 2 in
   the staging directory. The result must pass `integrity_check` and `foreign_key_check`, match the current schema,
   and leave no `-wal`/`-shm` file behind.
5. It enters maintenance mode: new checks, jobs, and backups stop, and running work
   gets up to 30 s to finish. New database connections get HTTP 503.
6. It swaps in the restored data.

A backup that fails step 2 to 4 is rejected with HTTP 400 and nothing is changed: the live data
stays in place, no restart is triggered, and the message names the cause. If the swap fails, it is
rolled back and operation resumes. After a successful restore, the instance stays in maintenance
mode until you restart the container. A migrated backup is already at the current schema, so the
restart runs no migration.

Set the proxy timeout so that the upload and the restore fit.

### Compatibility

Release number and database schema are compared separately.

| Backup release | Backup schema | Result |
|---|---|---|
| same or older | 2 (current) | Restored, no migration |
| same or older | 1, or exact former current version 10 | Migrated to version 2 in the staging directory if its layout matches; otherwise rejected |
| newer | any | Rejected: upgrade justUp! first |
| any | any other version | Rejected: no supported schema path |

There is no downgrade. Releases are ordered numerically (`1.0.10` is newer than `1.0.9`); a
pre-release (`rc1`, `beta2`, ...) is older than its final release. Versions that cannot be ordered
safely are rejected: `.postN` or other suffixes, and two different pre-releases of one version. A
development build (`dev`) only restores backups with an identical build ID.

### Failed migration

The migration of a version-1 or version-10 backup runs in one transaction on the staged copy. If it fails, the staged copy is
discarded and the response says that the migration failed. Details are in the server log. The
uploaded backup file is not modified.

### Limits

- A backup whose database has foreign key violations, or contains triggers or views (the schema
  defines none), is rejected. A backup of a database that already violates its own constraints
  cannot be restored.
- A restore needs the same `SECRET_KEY` (and signing key) as the instance that wrote the backup;
  encrypted settings are restored as they are.
- Measurements are restored as archived. Target IDs do not change in a migration, so series stay
  assigned to their targets. Databases and series are not captured at one common point in time.

## Storage and purge

Admins can purge alerts and measurements and reset a single target. See
[Storage & retention](../configuration/storage.md).
