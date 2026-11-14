# Storage & Retention

All state is in one data directory (`JUSTUP_DATA_DIR`).

## Data locations

| Path | Content |
| --- | --- |
| `sqlite/` | Configuration, users, targets, alerts, notifications (SQLite, WAL mode) |
| `tsdb/` | Measurements per target and metric |
| `pdf_reports/` | Generated PDF reports |
| `signal/` | signal-cli account and state |
| `geoip/` | GeoLite2 City and ASN databases |

## Retention

Measurements are rolled up automatically. There is no setting.

| Resolution | Kept for |
| --- | --- |
| Raw | up to 30 days |
| 5 minutes | up to 180 days |
| 1 hour | up to 3650 days |

The per-target **Retention** (1-3650 days) is the upper bound for every
resolution. A 45-day target keeps raw data for 30 days and rollups for 45 days.
Charts and reports choose the resolution by range: up to 30 days raw, up to 180
days 5-minute, longer ranges hourly.

Availability metrics keep the minimum per bucket, so an outage never disappears.
`http_status` is not rolled up; its history is the raw 30 days.

Housekeeping runs daily. It also compresses closed days to save about 80 % of the
measurement storage. Compression of existing data runs in the background after an
upgrade; all data stays readable.

!!! warning "Downgrading"
    Releases before the compressed format ignore the compressed history and show
    a shorter range. Upgrade again or restore a backup to see it. Details:
    [Database maintenance](../development/database-maintenance.md#rollback-and-downgrade).

## Deleting a target

Deleting a target removes its measurement files first and the database row in the
same request. If the files cannot be removed completely, the row is kept and the
request fails with `500`; nothing is lost and a repeat succeeds.

!!! warning "Known limitation (1.0)"
    If the database commit fails after file removal, the target remains with an
    empty history. The request returns `500` and logs `delete failed at commit`;
    only a backup can restore the history. Repeating the delete removes the row.

## Freeing storage

Admins can view storage statistics (Settings > Storage) and:

- purge monitoring alerts,
- purge all measurements (needs **Allow Metric Purge** and the admin password),
- reset the measurements of one target.

Use shorter retention for rarely used targets. For backups, see
[Backup & restore](../features/backup.md).
