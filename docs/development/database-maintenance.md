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
| Inbox notifications | 180 days, read or unread |

Inbox notifications are removed automatically after 180 days, unread ones included;
their read and hidden marks go with them.

The WAL is checkpointed regularly and truncated when it exceeds 64 MiB. Databases created by
this release use `auto_vacuum=INCREMENTAL`: the scheduler returns free pages in small
steps after the cleanup. A full `VACUUM` is never run automatically. To shrink the file
further, compare `freelist_bytes` with `db_bytes`
in the database health view, make sure free disk space for a second copy exists,
take and verify a backup, stop writers, and run `VACUUM` once.

### Schema baseline and versioning

The schema of v1.0.0 is the baseline: `PRAGMA user_version` is `1`, and `init_schema()`
creates all tables, foreign keys, CHECK constraints and indexes in one step on a new
database. There are no migrations before the first release. A future release bumps
`SCHEMA_VERSION` and registers an ordered step in `MIGRATIONS` (`app/db/sqlite.py`).

At start the application opens an existing database only if it is compatible:

| State of the file | Result |
| --- | --- |
| No tables, `user_version` 0 (new or empty file) | initialised as baseline |
| `user_version` equals the baseline and tables, columns and indexes match it | opened |
| Tables but `user_version` 0 (unversioned pre-release database) | refused |
| `user_version` differs from the baseline (and no migration path) | refused |
| Baseline `user_version` but a table, column or index differs | refused |

A refused database is never modified or deleted; the application exits with a message
naming the file. To start over with a pre-release database:

1. Back up: stop the app, then copy `data/sqlite/justup.db` (with `-wal`/`-shm` if present)
   and the whole `data/tsdb/` directory to a safe place. Note the stored settings, for example
   `SELECT key, value FROM settings;` (`online_geo_fallback_enabled`, or the former
   `online_geo_enabled`).
2. Move the old file aside: `mv data/sqlite/justup.db data/sqlite/justup.db.old`
   (and the `-wal`/`-shm` files). Move `data/tsdb/` aside as well
   (`mv data/tsdb data/tsdb.old`).
3. Start the app. It creates a new baseline database and prints the new admin password.
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
