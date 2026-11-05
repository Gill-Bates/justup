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

The complete current SQLite layout is the new version-1 baseline (`PRAGMA user_version` 1).
`SCHEMA_VERSION` and `_create_baseline_schema()` live in `app/db/sqlite/schema.py`;
`check_schema_compatibility()`, `init_schema()`, and the empty forward `MIGRATIONS` registry live in
`migrations.py`. The layout comparison is in `layout.py`. A fresh database is created directly at
version 1 with all current tables, constraints, and indexes, including the network-port and IXP
prefix tables. The former version-2 through version-10 migration history has been consolidated.
A later schema change must bump `SCHEMA_VERSION` and add a forward migration from version 1.

#### Startup behaviour

| State of the file | Result |
| --- | --- |
| No tables, `user_version` 0 | Create the complete baseline and stamp version 1 |
| Version 1, matching layout | Open |
| Former current version 10, matching the complete baseline layout | Restamp to version 1 in one transaction; preserve every table and row |
| Versions 2–9 | Refuse without writes (`missing_migration_path`) |
| Tables but `user_version` 0 | Refuse without writes (`unversioned_database`) |
| Version above 10 | Refuse without writes (`future_schema`) |
| Version 1 or 10 with a differing table, column, constraint, or index | Refuse without writes (`schema_drift`) |
| Corrupt SQLite file | Refuse without writes (`corrupt_database`) |

The compatibility check is read-only. Creating a new database or restamping an exact version-10
layout uses one `BEGIN IMMEDIATE` transaction; any failure rolls back the schema and version
counter together. The application does not delete or rebuild existing tables during the restamp.
A version-9 database needs the last version-10 release to complete its 9 → 10 upgrade before this
baseline reset can accept it. The migration history for versions 2–9 is no longer shipped.

#### Compatibility validation

The check compares the current tables' columns, declared types, `NOT NULL`, defaults, primary
keys, foreign keys, unique constraints, normalized `CHECK` expressions, and required indexes.
Column order is ignored because previously migrated version-10 databases may have columns added
at the end. Unknown tables and indexes do not satisfy missing required objects.

Two known layout variants remain accepted: an upgraded `alerts.active_incident` may have
`DEFAULT 0` while a fresh table has `DEFAULT 1` (all application writers set the value), and
`idx_recipients_phone` may still be non-unique while duplicate phone numbers are being resolved.
Any other difference is reported as drift without exposing row values.

#### Backup and recovery

Take a consistent SQLite backup before a version change using the Backup UI or the SQLite Backup
API, and keep the matching `data/tsdb/` history. Copying only a live `.db` file can miss committed
WAL transactions. The restore checks the signed manifest, schema fingerprint, version, integrity,
and layout before replacing live data. An exact version-10 backup is restamped to version 1 in the
staging directory, then checked again; backups from versions 2–9 have no migration path. The
live database remains unchanged if validation or the staged restamp fails.

The manifest fingerprint is checked against the archived file's own schema, so the version-10
column order and accepted default/index variants do not prevent a valid restore. To return to an
older release after this reset, restore a backup made for that release; the older executable may
refuse a database stamped as version 1 with the new baseline layout.

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
