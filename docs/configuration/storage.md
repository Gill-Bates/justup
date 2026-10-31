# Storage & retention

justUp! keeps its state locally in one data directory (`JUSTUP_DATA_DIR`). This
page describes where the data lives, how it is retained, and how to free storage.

## Data locations

- **Database** — configuration, users, targets, monitoring alerts, and notification
  events in SQLite using WAL mode.
- **Measurements** — time series per target and metric: an uncompressed file for the
  current day plus compressed segments for earlier days, with rollups.
- **Reports** — generated PDF files.
- **signal-cli** — account and state data.

## Rollups and retention

Measurements are downsampled to keep long-term storage bounded. This is automatic
and has no setting:

| Resolution | Retention | Aggregation |
| --- | --- | --- |
| Raw | Up to 30 days, capped by target retention | — |
| 5 minutes | Up to 180 days, capped by target retention | Median (minimum for binary metrics) |
| 1 hour | Up to the target retention, maximum 3650 days | Median (minimum for binary metrics) |

- Binary availability metrics use the minimum, so an outage inside a bucket never
  disappears from the history.
- `http_status` is categorical and is not rolled up; its status distribution covers
  the raw 30 days and the UI reports that coverage.
- The per-target **Retention** value (1–3650 days) is the upper bound for all
  resolutions. For a 45-day target, raw measurements are kept for at most 30 days,
  5-minute rollups for at most 45 days, and hourly rollups for up to 45 days.
- Rollups, pruning, and compression run during the scheduler's daily housekeeping.

Queries select a resolution by range: up to 30 days uses raw points, up to 180 days
uses 5-minute points, and older ranges use hourly points. A shorter target retention
means no measurements exist outside that configured range.

## Automatic compression

Only the current UTC day of a series stays in a plain, append-only file, so writes
stay fast. Housekeeping compresses closed days with gzip into segments (raw data per
ISO week, 5-minute rollups per month, hourly rollups per year). There is nothing to
configure, queries read only the segments that overlap the requested range, and
charts, reports, availability, and downtime views return the same values as before.

- Typical saving is about 80 % of the measurement storage. The Storage view shows the
  volume saved by compression.
- After an upgrade, existing data is compressed gradually in the background: a few
  seconds per target and run, repeated every five minutes until done. Monitoring and
  the UI stay responsive and all data remains readable meanwhile. Restarting the
  container does not repeat finished work.
- A segment file that cannot be read is skipped with a warning from the point of damage
  on. When new data has to be added to it, the readable part is kept, the damaged file
  is set aside as `*.corrupt-*` and deleted after 30 days.

!!! warning "Downgrading"
    Releases before the compressed format ignore `*.jsonl.gz` segments: they keep
    running but do not see older history. Restore a backup or upgrade again to get it
    back. Details: [Database maintenance](../development/database-maintenance.md#rollback-and-downgrade).

## Freeing storage

Administrators can view storage statistics and perform targeted cleanup:

- Purge monitoring alerts.
- Purge time-series data.
- Reset the measurements for one target.

!!! tip "Choose retention per target"
    Use shorter retention for rarely used targets and longer retention when you
    need more historical data. Rollups and compression limit the growth of retained
    data.

For backups and restores, see [Backup & restore](../features/backup.md).
