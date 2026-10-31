# Database maintenance

This page describes the implemented SQLite and TSDB maintenance. SQLite retention
and per-target measurement retention are separate policies: SQLite stores
application state; the TSDB stores metric samples and rollups.

## Process model

Maintenance coordination (drain and stop of scheduler, PDF worker and database
connections during backup, restore and housekeeping) is kept per process. justUp is
supported as a single process: `python -m app` starts one uvicorn server with an
application object, which cannot be combined with `--workers`. Do not put several
processes in front of the same data directory.

## SQLite

### Journal mode and durability

SQLite uses WAL mode. Each connection enables foreign keys, applies the configured
busy timeout, and uses `synchronous=FULL`. `FULL` remains the production default
because a successful commit should remain durable after a system crash. `NORMAL` is
faster in WAL mode, but can lose the latest committed transactions after a system
crash.

A local comparison using `tools/db-bench.py sync --tmpdir /opt/justup --writes 1500
--seconds 4 --runs 3` ran on ext4 against a temporary database. Median results from
that run:

| Metric | FULL | NORMAL |
| --- | ---: | ---: |
| Single-commit transactions per second | 553 | 7519 |
| Single-commit p50 latency | 1.32 ms | 0.07 ms |
| Single-commit p95 latency | 3.74 ms | 0.11 ms |
| Concurrent-load commits per second | 165 | 395 |
| Peak WAL size | 8.59 MB | 20.81 MB |

This is one local benchmark, not a performance guarantee for production hardware.
The speed-up from `NORMAL` does not justify its weaker durability guarantee here;
`SYNCHRONOUS_MODE` therefore remains `FULL`.

### Retention and schedule

The scheduler runs SQLite maintenance about once an hour and retries incomplete
work after five minutes. Deletes run in batches of 1000 rows, each in a short write
transaction, with a 10 ms pause between full batches. A run starts new batches for
at most ten seconds; remaining rows are handled by a later run.

| Data | Retention / condition |
| --- | --- |
| Expired authentication tokens | `expires_at` is in the past |
| Login attempts | At most 24 hours; active lockouts are preserved |
| Monitoring quality samples | 30 days |
| Monitoring alerts | Resolved alerts only, 730 days from start; open alerts are preserved |
| Delivery queue (`notification_queue`) | Sent/failed items: 7 days; pending/processing items are preserved |
| Scheduler runs | Heartbeat older than 300 seconds |
| Target locks | After lock expiry |
| POP import runs | 90 days, while keeping at least the latest 20 runs |
| POP alias candidates | Reviewed: 90 days; unreviewed: 365 days |

Notification inbox events (`notifications`) and per-user read state
(`notification_reads`) are separate from the delivery queue. The current retention
job does not delete them; the 7-day queue policy does not apply to inbox history.

### WAL, query planning, and VACUUM

The maintenance job first runs a `PASSIVE` WAL checkpoint. It runs a `TRUNCATE`
checkpoint only when the WAL file exceeds 64 MiB. Active readers can prevent a full
checkpoint; the result and file sizes are available in maintenance statistics.

`PRAGMA optimize` runs after schema initialization and in the daily maintenance
pass. It refreshes planner statistics when SQLite considers them stale. A separate
scheduled `ANALYZE` is not required.

Automatic `VACUUM` is intentionally disabled. It rewrites the database file, needs
temporary free space, and blocks more than batched retention. Before vacuuming,
compare `freelist_bytes` with `db_bytes` and ensure there is enough free disk space
for a second database copy. Then schedule a maintenance window, create and verify a
SQLite backup, stop application writers, and run `VACUUM` once. Never remove WAL or
SHM files manually.

### Target users and delivery recipients

The two target mappings have different jobs and are not duplicates:

- `target_alert_users` maps internal application users to targets. It controls who
  can see target-level monitoring state and target-audience inbox events.
- `target_recipients` maps external delivery recipients to targets. The dispatcher
  uses these contacts for email and Signal delivery.

An internal user mapping does not replace an external contact, or vice versa.

### Indexes

Indexes are created in `app/db/sqlite.py`. The current decision is to keep indexes
used by application queries and retention; they avoid table scans in scheduler
lookups, notification visibility, and expiry cleanup.

| Index group | Purpose |
| --- | --- |
| `auth_tokens` expiry and user indexes | Token lookup, revocation, and expiry cleanup |
| `notification_queue` status/retry/time indexes | Delivery scheduling and queue retention |
| `notifications` audience/category/recipient/ID indexes | Inbox visibility, filtering, and newest-first listing |
| `notification_reads` user/event index | Unread counts and per-user read state |
| `alerts` target/status/start-time indexes | Monitoring views and resolved-alert retention |
| Target/recipient mapping indexes | Delivery selection and target visibility |
| POP lookup/import indexes | Alias, canonical-code, source, and import queries |

`idx_pop_alias_prio` and `idx_pop_canonical` remain until a focused write-cost and
`EXPLAIN QUERY PLAN` comparison proves that removing them is safe. An index is not
removed based on one query-plan observation.

## Per-target TSDB retention

The target's **Retention** value is the upper bound for its retained history (1–3650
days). Resolutions also have their own maximums:

| Resolution | Effective retention |
| --- | --- |
| Raw data | `min(30 days, target retention)` |
| 5-minute rollups | `min(180 days, target retention)` |
| Hourly rollups | Target retention, up to 3650 days |

Daily housekeeping always converts raw data just before the 30-day boundary into
5-minute buckets, then converts 5-minute data just before the 180-day boundary into
hourly buckets. A shorter target retention moves these handoffs to the target's
limit. Only complete buckets are written, and a run converts everything between the
last existing rollup point and the handoff, so a missed housekeeping day is caught up
instead of leaving a gap. Binary availability metrics (`ping_up`, `http_up`,
`tcp_up`, `tcp_up_<port>`) use the minimum so an outage never disappears; other
numeric metrics use the median. `http_status` is categorical: it is never rolled up,
and its history is the raw 30 days, which the status distribution reports as its
coverage. Rollups of `http_status` written by earlier releases are still read and
age out by retention.

Measurements are stored per target and metric (see [TSDB storage format](#tsdb-storage-format)).
A backup opens each series (active file and segments) under its file lock and copies it as of
that moment, so it never captures a partially written series.

## TSDB storage format

Per target (`data/tsdb/target_<id>/`) and series (metric, `@5m_median` and `@1h_median`
rollups are separate series):

| File | Content |
| --- | --- |
| `<series>.jsonl` | Active file: the current UTC day, uncompressed, append-only. Older lines only exist before their first compression. |
| `<series>.<period>.jsonl.gz` | Closed days, one gzip member per day. Period is the ISO week (`2026W44`) for raw data, the month (`202610`) for 5-minute rollups and the year (`2026`) for hourly rollups. |
| `<series>.lock` | Series lock (`flock`): shared for readers, exclusive for appends, pruning and the short publish step of a compression. Readers hold it only while they open the files, never while they decompress. |
| `.housekeeping.lock` | Per-target non-blocking lock; a second housekeeping run (or worker) skips the target. |
| `.*.tmp`, `*.corrupt-<epoch>-<id>` | Temp files of a running pass (deleted after one hour if orphaned) and damaged segments that were set aside (deleted after 30 days). |

A line is still `{"ts": "...", "value": ...}`. Readers (`query`, `query_auto_resolution`,
`query_display_series`, availability and downtime) merge segments and active file
and open only the segments whose period overlaps the requested range, so a 24 hour query
never touches a segment. A reader takes the shared lock only to open the files; because a
handle keeps the file it was opened on, it reads one consistent state even if a compaction
publishes meanwhile, and a long read never delays the appends of the monitoring loop. The
newest lines of a segment are cached per file identity, so polling the latest points right
after midnight does not decompress a week of data per request.

### Compression pass

Housekeeping per target runs rollups, then pruning, then compression, in that order.
Compression of one series:

1. Skip check: read the first valid line of the active file. If it is from today, the
   series is done (one small read, no state file).
2. Without a lock, stream the contiguous closed prefix of the active file into one
   temp gzip member per period. Appends only extend the file, so closed lines cannot
   change; RAM stays at a few buffers.
3. Build the replacement segment: the old segment bytes plus the new member (no
   recompression). If the new lines are not strictly newer than the segment (crash
   leftover, late line), merge by timestamp instead, which removes duplicates.
4. Verify by decompressing the result: line count and newest timestamp must match.
5. Copy the not yet closed bytes of the active file (the current day) into a durable
   temp file, still without the lock.
6. Under the exclusive series lock (milliseconds), check that the active file is the
   same inode and only grew and that no segment changed, append the few bytes written
   since the scan to the temp file, `rename` the segments in, `fsync` the directory, and
   `rename` the temp file over the active file.

The originals stay untouched until step 5, so a failure or kill at any point loses
nothing. A kill between the segment rename and the active-file replacement leaves the
days in both places; the next pass merges them without duplicates (a read in between
may see those days twice). A race with a concurrent writer discards the attempt and
reports it as pending.

Limits: one series at a time, a three second budget per target and run
(`COMPACT_BUDGET_SECONDS`), a pause of at most half a second after each series, and a
stop request on shutdown or maintenance. An incomplete target is retried after five
minutes (`HOUSEKEEPING_RETRY_SECONDS`), a target with an I/O error after one hour
(`HOUSEKEEPING_ERROR_RETRY_SECONDS`). One failing series does not stop the pruning or
compression of the others.

A damaged segment is never rewritten in place. When new days must be added to it, the
readable part is merged with them, the damaged file is first copied aside as
`*.corrupt-*` and only then replaced, so a kill at any point still leaves both versions.

### Migration of existing data

There is no migration step at start. An existing install keeps up to 30 days of raw
and up to 180 or 3650 days of rollup lines in the active files, which stay readable.
The same compression pass moves them into segments over several runs. The layout is
the progress marker, so a restart continues where it stopped and a finished series
costs one small read. A backup from before this format restores as it is and migrates
the same way.

### Rollback and downgrade

Segments use the suffix `.jsonl.gz`, which earlier releases do not list (they read
`*.jsonl`). After a downgrade the old release therefore:

- keeps writing and reading the active file, but does not see older history, and its
  charts and reports show a shorter range;
- does not delete, open or modify segments, and its backup omits them;
- would not roll up the days that sit in segments.

Nothing is corrupted. Upgrading again makes the history visible; rollup buckets that
were due while the older release ran are caught up, because every complete bucket after
the last rollup is converted before pruning removes the raw segments. Take a backup
before a downgrade; a backup made by this release contains the segments and restores
only on this release or newer to the full range. The SQLite schema version is not
changed by this feature, and the removed `downsample_enabled` row is deleted at start.

## TSDB storage benchmark and format decision

`tools/tsdb-bench.py` generates synthetic steady-state data (no external services) and
measures size (logical bytes and `st_blocks`), write throughput, query time, housekeeping,
backup and restore. Values mirror the scheduler: full-precision float latencies, `0`/`1`
availability, HTTP status codes. Run it on a real disk, not tmpfs:

```bash
.venv/bin/python tools/tsdb-bench.py codecs    --tmpdir /var/tmp/bench
.venv/bin/python tools/tsdb-bench.py segments  --tmpdir /var/tmp/bench
.venv/bin/python tools/tsdb-bench.py scenarios --tmpdir /var/tmp/bench --json result.json
```

### Baseline (uncompressed JSONL, one file per series)

One target per scenario. Housekeeping is the second, idempotent run.

| Scenario (interval / retention) | Points | Files | Disk (`st_blocks`) | Backup (tar.bz2) | Housekeeping | Query 24 h / 30 d / 365 d | Restore |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Ping 60 s / 365 d | 181,684 | 6 | 10.93 MB | 1.29 MB | 1.2 s | 98 / 267 / 448 ms | 0.7 s |
| HTTP + Ping 60 s / 365 d | 454,210 | 15 | 26.82 MB | 2.85 MB | 2.8 s | 84 / 287 / 459 ms | 0.9 s |
| HTTP + Ping + Cert 30 s / 365 d | 804,252 | 18 | 47.66 MB | 5.14 MB | 4.9 s | 185 / 301 / 538 ms | 1.3 s |
| TCP ports (4) 60 s / 365 d | 1,090,104 | 36 | 65.56 MB | 7.75 MB | 6.8 s | 89 / 280 / 435 ms | 2.2 s |
| Intensive 5 s / 365 d | 2,830,210 | 15 | 174.12 MB | 22.67 MB | 11.0 s (80 MB RSS) | 985 / 286 / 401 ms | 4.2 s |
| Long term 60 s / 3650 d | 339,364 | 6 | 19.95 MB | 2.11 MB | 1.7 s | 74 / 246 / 509 ms | 0.4 s |

Two baseline findings shape the design: even an idempotent housekeeping run rewrites
every series file (pruning reads and rewrites the whole file), and a 24 h chart query
reads the entire 30-day raw file.

### Result with gzip segments

Same data and commands as the baseline, measured after the segmented store compressed
it (the first pass is the migration of the legacy files; the second pass shows the
steady state). Disk use is `st_blocks`, which includes file-system block rounding.

| Scenario | Disk before | Disk after | Saving | Files |
| --- | ---: | ---: | ---: | ---: |
| Ping 60 s / 365 d | 10.93 MB | 1.97 MB | 82.0 % | 6 -> 32 |
| HTTP + Ping 60 s / 365 d | 26.82 MB | 4.44 MB | 83.4 % | 15 -> 79 |
| HTTP + Ping + Cert 30 s / 365 d | 47.66 MB | 8.00 MB | 83.2 % | 18 -> 95 |
| TCP ports (4) 60 s / 365 d | 65.56 MB | 11.82 MB | 82.0 % | 36 -> 192 |
| Intensive 5 s / 365 d | 174.12 MB | 33.39 MB | 80.8 % | 15 -> 79 |
| Long term 60 s / 3650 d | 19.95 MB | 3.26 MB | 83.7 % | 6 -> 50 |

| Scenario | Backup (tar.bz2) | Housekeeping, idempotent run | Migration (first run) | Query 24 h / 30 d / 365 d (ms) | Write (points/s) | Backup / restore time |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Ping 60 s | 1.29 -> 1.71 MB | 1.16 -> 0.01 s | 1.5 s | 98/267/448 -> 14/152/293 | 16.4k -> 31.5k | 0.78/0.66 -> 0.15/0.08 s |
| HTTP + Ping 60 s | 2.85 -> 3.74 MB | 2.76 -> 0.02 s | 3.8 s | 84/287/459 -> 15/144/286 | 18.1k -> 27.7k | 2.12/0.94 -> 0.32/0.17 s |
| HTTP + Ping + Cert 30 s | 5.14 -> 6.72 MB | 4.89 -> 0.03 s | 6.4 s | 185/301/538 -> 29/206/382 | 20.6k -> 28.6k | 3.67/1.28 -> 0.58/0.34 s |
| TCP ports (4) 60 s | 7.75 -> 10.18 MB | 6.76 -> 0.07 s | 9.5 s | 89/280/435 -> 13/158/312 | 17.2k -> 28.3k | 4.65/2.20 -> 0.86/0.49 s |
| Intensive 5 s | 22.67 -> 29.39 MB | 11.0 -> 1.5 s | 22.2 s | 984/286/401 -> 155/185/339 | 22.2k -> 29.4k | 10.0/4.19 -> 2.34/1.48 s |
| Long term 60 s / 3650 d | 2.11 -> 2.77 MB | 1.72 -> 0.01 s | 2.6 s | 74/246/509 -> 16/165/346 | 22.1k -> 27.6k | 1.26/0.43 -> 0.30/0.17 s |

Housekeeping peak RSS fell from 28-80 MB to 19-28 MB, because pruning no longer loads
whole files.

Reading the numbers:

- **Target met.** Time-series storage shrinks by 81-84 %, inside the 70-85 % goal. Raw
  latency floats are close to incompressible digits, so latency series compress about
  4x while availability and status series compress 7-15x; the mix gives the result.
- **No slower where it matters.** The append path is unchanged and writes to a small plain
  file; the rates above are equal or higher, but the baseline run shared the machine with
  other work, so read them as "not slower", not as a speed-up. A 24 hour chart query reads
  only today's file and is 5-6x faster; 30 day and 365 day queries decompress the
  overlapping segments and are still faster than reading the uncompressed files.
- **The backup archive grows by about 30 %.** The backup was already a bzip2 tarball,
  which compresses the plain JSONL (about 8:1) better than gzip does (5-6:1), so
  a gzip-compressed series cannot shrink further inside it. The backup is created and
  restored 3-6x faster and the data directory is far smaller. Switching the segments to
  bzip2 would equalize the archive but cost 4-8x slower reads (see the codec table), so
  it was not done.
- **More, smaller files.** A series at full retention is one active file plus about 5
  weekly raw segments, up to 7 monthly 5-minute segments and one yearly hourly segment
  (about 13 files). Roughly 600 series (50 targets with 12 series each) stay under 8,000
  files. Daily segments would have tripled this for 1.6 % less compressed size.
- **Migration** of one target takes seconds of CPU and is spread over several budgeted
  runs. Its peak RSS stays below 30 MB.

### gzip versus Zstandard

Synthetic `ping_ms` (full-precision floats, 3.1 MB, 43,200 lines) and `ping_up`
(0/1, 2.4 MB). Zstandard was measured with the optional `zstandard` 0.25.0 module in a
scratch environment; it is not an application dependency.

| Codec | `ping_ms` ratio | `ping_up` ratio | Compress | Decompress |
| --- | ---: | ---: | ---: | ---: |
| gzip -1 | 3.65 | 6.65 | 153 MB/s | 339 MB/s |
| gzip -6 | 4.02 | 7.62 | 49 MB/s | 323 MB/s |
| gzip -9 | 4.15 | 8.09 | 14 MB/s | 299 MB/s |
| zstd -3 | 3.85 | 7.81 | 242 MB/s | 712 MB/s |
| zstd -10 | 4.25 | 9.06 | 35 MB/s | 673 MB/s |
| zstd -19 | 5.13 | 12.18 | 1.9 MB/s | 616 MB/s |
| bz2 -9 | 5.46 | 11.07 | 19 MB/s | 40 MB/s |
| xz -6 | 5.50 | 15.18 | 2.0 MB/s | 82 MB/s |

Decision: **gzip level 6 from the standard library**. At comparable CPU cost zstd
compresses no better than gzip (zstd -3 has a lower ratio than gzip -6, zstd -10 gains
about 6 %), and its faster decompression is irrelevant for segments of a few hundred
KB. Only zstd -19 compresses clearly better, at 1.9 MB/s, and it would add a native
dependency to the image and to every platform wheel. bz2 and xz were rejected because
they decompress 4-8 times slower, and xz needs 60-95 MB of RAM to compress. The raw
floats dominate the entropy, so the achievable ratio is about 4-5x for latencies and
7-15x for availability and status series.

### Segment length

`ping_ms` raw, 30 days of 60 s checks, gzip level 6:

| Segmentation | Files | Compressed | With 4 KiB block rounding |
| --- | ---: | ---: | ---: |
| Daily | 31 | 781 KB | 860 KB |
| Weekly | 5 | 771 KB | 774 KB |
| Monthly | 2 | 769 KB | 770 KB |

Compression barely depends on segment length, but daily files multiply the file count
by six. The format therefore uses one file per ISO week for raw data, per month for
5-minute rollups and per year for hourly rollups. Each closed day is appended as its own
gzip member (concatenated members are a valid gzip stream and cost 1.6 % over a single
stream), so only the current UTC day stays uncompressed.

## Tools

- `tools/db-bench.py plans` compares query plans for representative data volumes.
- `tools/db-bench.py sync` compares `FULL` and `NORMAL` in a temporary database; it
  does not change the production PRAGMA.
- The in-app storage view reports database, WAL, freelist, and queue statistics.
