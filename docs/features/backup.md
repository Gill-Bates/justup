# Backup & Restore

Admins back up the whole state into one signed archive (Settings > Backup) and
restore from it.

## Backup

The archive `justup_backup_<date>_<time>_<64 hex>.tar.bz2` contains the SQLite
databases, the measurements, and the PDF reports. The 64 hex characters are the
HMAC-SHA256 signature.

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

!!! danger "Breaking change in 1.0.0"
    Backups from earlier versions are rejected and must be recreated.

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
2. justUp! verifies the signature and the archive structure, then checks every
   database. A backup whose application database has a schema this release does
   not support (for example from a pre-release build) is rejected with HTTP 400;
   nothing is changed.
3. It enters maintenance mode: new checks, jobs, and backups stop, and running work
   gets up to 30 s to finish. New database connections get HTTP 503.
4. It swaps in the restored data.

If a step fails before the swap or the swap is rolled back, nothing changes and
operation resumes. After a successful restore, the instance stays in maintenance
mode until you restart the container.

Set the proxy timeout so that the upload and the restore fit.

## Storage and purge

Admins can purge alerts and measurements and reset a single target. See
[Storage & retention](../configuration/storage.md).
