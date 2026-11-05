# Database maintenance

SQLite stores application state; the time-series store (TSDB) stores measurements.
Retention for each is separate. Both are maintained automatically.

## Process model

Maintenance coordination (backup, restore, housekeeping) is per process. justUp! is
supported as a single process; do not use `--workers` or several instances on one
data directory.

## SQLite

SQLite runs in WAL mode with `synchronous=FULL`, so committed data survives a
system crash. Never delete WAL or SHM files by hand.

The scheduler runs SQLite cleanup about hourly:

| Data | Kept |
| --- | --- |
| Expired authentication tokens | removed |
| Login attempts | 24 hours (active lockouts stay) |
| Quality samples | 30 days |
| Resolved monitoring alerts | 730 days (open alerts stay) |
| Delivery queue | sent/failed 7 days (pending stay) |
| POP import runs | 90 days (at least the latest 20) |
| Unreviewed PoP candidates and observations | 365 days, deleted together |
| Reviewed PoP candidates and review audit | retained; no automatic deletion |
| Inbox notifications | 180 days, read or unread |

### PoP candidate review

PeeringDB facilities are grouped by known city **and country**. A group whose
coordinates disagree by more than 50 km, or disagree with a curated city anchor,
is skipped. A curated seed/manual position wins; otherwise the deterministic
facility centroid is stored in `pop_city_evidence` with its source and facility
count. This table is not read by hostname lookups. The importer neither creates
nor refreshes productive router aliases, and it does not retire existing aliases.
Seed, manual and historical learned rows are not overwritten. Repeated identical
snapshots leave city evidence unchanged.
Import status counts accepted facilities as `processed` and city-evidence rows
as `inserted` or `updated`; productive alias counts are unchanged.

Traceroute candidates are stored separately in `pop_alias_candidates` and
`pop_candidate_observations`. Route fingerprints deduplicate repeats; an IPv4
`/24` or IPv6 `/48` identifies an independent network context. Provider/parser
context is recorded separately and cannot make the same network independent. At most
5,000 candidates and 20 observations per candidate are retained. Quality score
is 0 without a local GeoLite2 proposal, 40 with one, and 70 only with at least
two independent, non-conflicting contexts. A country conflict or positions more
than 250 km apart blocks approval. These scores do not change production lookup
confidence. A matching city/country may suggest a curated canonical code, marked
`geolite2-city+curated-code`; the suggestion never confirms the alias. There is
**no automatic promotion**.

Inspect candidates and historical `source='learned'` rows read-only:

```bash
python tools/pop_candidate_review.py /path/to/justup.db inspect
```

After reviewing independent route evidence and confirming a city-level position,
an operator may explicitly approve or reject a code. Approval requires two
independent contexts, no recorded conflict, and a reviewed country agreeing with
the GeoLite2 proposal. A reviewed alias has source `learned`, confidence 70 and
priority 10; it never replaces an existing active alias. `--canonical` may
attach the alias to the inspected suggestion only when a nearby curated
seed/manual anchor exists; without it, the candidate code is its own canonical.
The actor, evidence,
action and UTC time are retained in `pop_candidate_reviews`.

```bash
python tools/pop_candidate_review.py /path/to/justup.db approve abc \
  --actor operator --evidence 'ticket and independent route review' \
  --canonical fra --city Frankfurt --country DE --lat 50.1 --lon 8.6
python tools/pop_candidate_review.py /path/to/justup.db reject abc \
  --actor operator --evidence 'country conflict'
python tools/pop_candidate_review.py /path/to/justup.db revoke abc \
  --actor operator --evidence 'new contradictory evidence'
```

Approval and revocation write to the specified database; take a verified backup
and review the `inspect` output before using them on an installation. Revocation
retires only the row linked to that approval, without deleting it. Historical
learned rows without a matching review remain in their previous active/retired
state and are shown with no review status; cleanup or migration of those rows
requires a separate authorised operation.

Imports never overwrite manual or seed rows or an active reviewed `learned` row; after a
revocation the import may reclaim the alias. An import record with invalid coordinates
(NaN, infinite or outside latitude -90..90 / longitude -180..180) is skipped, not clamped:
clamping would store a position that no source reported.

Inbox notifications are removed automatically after 180 days, unread ones included;
their read and hidden marks go with them.

The WAL is checkpointed regularly and truncated when it exceeds 64 MiB. Databases created by
this release use `auto_vacuum=INCREMENTAL`: the scheduler returns free pages in small
steps after the cleanup. A full `VACUUM` is never run automatically. To shrink the file
further, compare `freelist_bytes` with `db_bytes`
in the database health view, make sure free disk space for a second copy exists,
take and verify a backup, stop writers, and run `VACUUM` once.

### Schema baseline and versioning

The schema of v1.0.0 is the baseline (`PRAGMA user_version` 1). The current schema version is
**9**. The SQLite layer is the package `app/db/sqlite/` (`from app.db import sqlite` still exposes
every function). `SCHEMA_VERSION` and `_create_baseline_schema()` live in `schema.py`
(notification DDL in `schema_notifications.py`), the ordered `MIGRATIONS` registry,
`check_schema_compatibility()` and `init_schema()` in `migrations.py`, and the layout comparison
behind the compatibility check in `layout.py`:

| Version | Step | Change |
| --- | --- | --- |
| 1 | baseline (v1.0.0) | all tables, foreign keys, CHECK constraints and indexes |
| 2 | `1 -> 2` | `notification_queue.limited_attempts`, `notification_channel_state` |
| 3 | `2 -> 3` | `alerts.active_incident`, `alert_dispatch_outbox` |
| 4 | `3 -> 4` | `dashboard_layouts.height_mode` |
| 5 | `4 -> 5` | `recipients.enable_for_all_targets` (existing recipients: off) |
| 6 | `5 -> 6` | PoP candidate observations, city evidence, and explicit review metadata |
| 7 | `6 -> 7` | `targets.enable_udp_scan`, `targets.udp_ports`, table `target_network_ports`; existing TCP ports are copied as manual rows |
| 8 | `7 -> 8` | `target_network_ports.raw_state`, `service_identity`, `identity_changed_at`, `latency_ms` |
| 9 | `8 -> 9` | range indexes for the alert summary and `ANALYZE alerts` (bounded sample) |

A new database is created directly at the current version. A database at an older version is
upgraded forward, step by step, on the next start. Migrations are forward-only: there is no
downgrade, and a database written by a newer release is refused. Releases that change the
schema bump `SCHEMA_VERSION` and append one `Migration` that upgrades from the previous version;
`_create_baseline_schema()` must create the same end state for new databases.

#### Startup behaviour

At start the application opens an existing database only if it is compatible:

| State of the file | Result |
| --- | --- |
| No tables, `user_version` 0 (new or empty file) | created at the current version |
| `user_version` 1–8 with a migration path | upgraded in one transaction, then opened |
| Current `user_version` and matching layout | opened |
| Tables but `user_version` 0 (unversioned pre-release database) | refused (`unversioned_database`) |
| `user_version` above the supported version | refused (`future_schema`) |
| `user_version` without a complete migration path | refused (`missing_migration_path`) |
| Current `user_version` but a table, column, type, constraint or index differs | refused (`schema_drift`) |
| Not a readable SQLite file | refused (`corrupt_database`) |
| A migration or the check of its result fails | rolled back (`migration_failed` / `schema_drift`) |

All migration steps and the validation of the resulting layout run in one `BEGIN IMMEDIATE`
transaction. If any step fails, schema changes and `user_version` roll back together, and the
file stays at the old version with all rows intact. Each applied migration is logged once
(`Schema migration 2 -> 3 (...): ok`); a failure is logged with the same prefix. Repeating the
start is idempotent. The compatibility check itself is read-only.

A refused database is never modified or deleted; the application exits with a message naming
the file, the stored and supported schema versions and the differing tables, columns or
indexes (names and definitions only, never row contents).

#### Compatibility validation

The check compares, per table of the current schema: column names, declared types, `NOT NULL`,
defaults and primary-key membership, foreign keys (target, columns, `ON DELETE`/`ON UPDATE`),
`UNIQUE` constraints, normalised `CHECK` expressions, and the required indexes (table, columns,
uniqueness, partial). It reads `PRAGMA table_info`, `foreign_key_list`, `index_list`,
`index_xinfo` and the stored `CREATE TABLE` text. Tables and indexes unknown to the schema are ignored.

**Column order is not compared.** `ALTER TABLE ... ADD COLUMN` always appends the new column,
so a correctly migrated database orders columns differently from a new one (for example
`alerts.active_incident` sits last after the 2 -> 3 migration, but between `status` and
`started_at` in a new database). Comparing positions would reject valid upgraded databases on
their next restart. Nothing in the application depends on column positions.

Two explicit, known variants are accepted although they differ from a new database:

- `alerts.active_incident` may carry `DEFAULT 0` (upgraded) or `DEFAULT 1` (new). SQLite cannot
  change a default without rebuilding the table, and no code path relies on it: every writer sets
  the column explicitly (new alert `1`; resolved, acknowledged or deactivated alert `0`). Any
  other default is reported as drift.
- `idx_recipients_phone` may still be the old non-unique index while duplicate phone numbers exist
  (see below); only its table and columns are compared.

#### Backup and recovery

Take a backup before every upgrade: stop the app (or use Settings > Backup, which uses the
SQLite Backup API for a consistent live snapshot) and keep `data/sqlite/` and `data/tsdb/` together.
Never copy `justup.db` alone while the app runs: committed data may still sit in `justup.db-wal`. A backup is
validated before a restore replaces anything: signature, archive paths, the backup manifest
(`data/backup-manifest.json`: release, `PRAGMA user_version`, schema fingerprint), version compatibility,
integrity check, and the same schema compatibility check as at start.

A backup with an older schema is migrated during the restore, in the staging directory under
`data/.restore_staging_*`, with the same `MIGRATIONS` and `init_schema()` as at start. The staged
database must then pass `integrity_check`, `foreign_key_check`, and the layout check; its WAL is
checkpointed and the connection closed, and no side file may remain, before `apply_restore()` swaps it
in. The live database is never migrated in place. A newer release or schema, a missing migration path,
a missing or contradicting manifest, triggers or views, or a failed migration reject the backup and
leave existing data unchanged. See [Backup & Restore](../features/backup.md) for the compatibility
table.

A new `Migration` also defines which old backups stay restorable: keep the chain from the oldest
supported schema version contiguous. The manifest fingerprint hashes the definitions of the archived
file and is only compared with that same file, so a migrated database (whose `ALTER TABLE` text and
column order differ from a new one) is never rejected because of it.

If a start fails, nothing was changed. Read `PRAGMA user_version;` and `PRAGMA table_info(alerts);`
of the file (read-only), compare with the message, and restore the pre-upgrade backup or start
the release that matches the file. Do not edit `user_version` by hand and do not delete the
database or TSDB files.

The 3 → 4 layout rebuild has no down migration. To return to an older release after a successful
upgrade, stop the app, recover the complete pre-upgrade SQLite and TSDB data offline, verify its
integrity and schema version, then run the matching older release. Do not use the newer release's
restore endpoint for a downgrade: it migrates older backups forward. A post-migration backup
cannot recover the original version-3 layout semantics.

#### Unsupported pre-release databases

A database with tables but no schema version (before v1.0.0) is not migrated. To start over
with such a file:
1. Back up: stop the app, then copy `data/sqlite/justup.db` (with `-wal`/`-shm` if present)
   and the whole `data/tsdb/` directory to a safe place. Note the stored settings, for example
   `SELECT key, value FROM settings;` (`online_geo_fallback_enabled`, or the former
   `online_geo_enabled`).
2. Move the old file aside: `mv data/sqlite/justup.db data/sqlite/justup.db.old`
   (and the `-wal`/`-shm` files). Move `data/tsdb/` aside as well
   (`mv data/tsdb data/tsdb.old`).
3. Start the app. It creates a new database and prints the new admin password.
4. Set the settings again (Settings > Network for the online geo fallback; fresh
   installs default to on). History in `data/tsdb.old/` is not
   reused: the TSDB stores series by numeric target ID (`target_1`, ...), and a new
   database assigns IDs again, so old measurements would show up under the wrong targets.
   Reuse the old series only with an explicitly verified mapping from old to new IDs.

For tests and smoke runs, `tools/make-dummy-dbs.py` creates throw-away baseline or
pre-squash databases with sample rows.

## Measurements (TSDB)

Each target and metric has its own series. The current UTC day is a plain file;
closed days are gzip segments (`*.jsonl.gz`). Daily housekeeping creates the
5-minute and hourly rollups, applies retention, and compresses closed days. See
[Storage & retention](../configuration/storage.md) for the retention table.

Deleting a target removes its TSDB directory before the SQLite commit, inside the same
request. If files remain, the request fails with 500 and the row is rolled back; if the
commit itself fails after the files are gone, the target stays with an empty history and the
failure is logged. Both cases converge on repeating the delete, and no orphaned TSDB
directory can result, so retention needs no sweep for deleted targets.

Group deletes check emptiness and delete in one immediate write transaction. A group that
still has targets is refused with 409; group names are unique (exact match), and a clash
that appears at write time also returns 409.


#### Duplicate recipient phone numbers

Baseline databases enforce a unique phone number per recipient (`idx_recipients_phone`).
If an earlier v1 database already holds duplicates, `init_schema()` logs a warning and keeps
the old non-unique index; it never deletes recipients. In this state the API still refuses
every new duplicate: creating a recipient, or changing a phone number to one that another
recipient owns, fails with HTTP 400 ("Phone number already exists"). The check runs in the
same `BEGIN IMMEDIATE` transaction as the write, so parallel requests cannot add duplicates.
Existing duplicate recipients stay editable as long as their own number is unchanged.

To clean up, find the duplicates, edit or delete all but one recipient per number
(Settings > Recipients), then restart the app, which creates the unique index:

```sql
SELECT phone, COUNT(*) FROM recipients
WHERE phone IS NOT NULL AND phone != '' GROUP BY phone HAVING COUNT(*) > 1;
```

### Rollback and downgrade

Releases before the compressed format ignore `*.jsonl.gz`. After a downgrade the old
release:

- keeps writing the active file but does not see older history, so charts and
  reports show a shorter range;
- does not touch the segments, and its backups omit them.

Nothing is corrupted. Upgrading again makes the history visible. Take a backup
before a downgrade; a backup from this release restores the full range only on this
release or newer.
