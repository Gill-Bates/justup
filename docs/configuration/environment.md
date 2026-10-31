# Environment Variables

This page is the **authoritative reference** for all environment variables of
justUp!. Runtime settings (such as SMTP credentials or target configuration), by
contrast, live in the database and are maintained through the interface – see
[Settings](settings.md).

## Naming convention

Application-specific settings consistently use the `JUSTUP_` prefix. There is
**no** permanent support for older `UPMON_` names. One existing exception is
`UPMON_TRUSTED_PROXIES` for the proxy address check.

Deliberately defined infrastructure and ecosystem names are exempt from the prefix
(`UPMON_TRUSTED_PROXIES`, `LOG_LEVEL`, `SIGNAL_CLI_PATH`, `SIGNAL_DATA_DIR`,
`JAVA_XMS`, `JAVA_XMX`, `PUID`, `PGID`) as well as the key/HMAC name `SECRET_KEY`
(or `SECRET_KEYS` for rotation), which the application reads without a prefix.

## Secrets

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SECRET_KEY` | **yes** | – | key for Fernet encryption and HMAC signature (session, CSRF token) |
| `SECRET_KEYS` | no | – | comma-separated key list for rotation (first = primary) |
| `JUSTUP_BACKUP_HMAC_SECRET` | no | derived from `SECRET_KEY(S)` | dedicated backup signing key; comma-separated `NEW,OLD` for rotation (first signs, all verify) |

`SECRET_KEY` is a urlsafe-base64-encoded 32-byte Fernet key. Generate:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

!!! danger "No default, no key in the repository"
    Without `SECRET_KEY`, justUp! does not start. There is deliberately no built-in
    default. Store the key only in the environment.

### Key rotation

For a rotation, use `SECRET_KEYS` as a comma-separated list. The **first** key is
the primary one and is used for encryption; the others serve only to decrypt
secrets already stored (MultiFernet).

```bash
SECRET_KEYS=<new-primary-key>,<old-key>
```

### Backup key

`JUSTUP_BACKUP_HMAC_SECRET` is optional. Each entry needs at least 32 characters with at least
8 distinct characters (e.g. `openssl rand -hex 32`). Empty and duplicate entries as well as weak
keys are rejected by backup and restore with a configuration error; the error message names only the
position, never the value. Rotation: `JUSTUP_BACKUP_HMAC_SECRET=<new>,<old>` – new backups are signed by the
first key, existing backups remain restorable with any key in the list. Remove the old key only
once no backups signed with it are needed anymore. Details:
[Backup & Restore](../features/backup.md).

## Server and network

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `JUSTUP_HOST` | no | `0.0.0.0` | address Uvicorn binds to in the container |
| `JUSTUP_PORT` | no | `8000` | port Uvicorn binds to in the container |
| `UPMON_TRUSTED_PROXIES` | no | empty | trusted proxies for `X-Forwarded-For` and `X-Real-IP` |
| `LOG_LEVEL` | no | `INFO` | logging level; Uvicorn access lines appear only at `DEBUG` |

The container starts exactly one Uvicorn process via `python -m app`. `WORKERS`
is not a supported environment variable. Do not start additional app processes
against the same data: scheduler, maintenance lock, the POP refresh lock, the
limit of 4 concurrent notification tasks (HTTP 429 with `Retry-After`), and other
runtime state are process-local.

!!! note "Compose port binding"
    The supplied `docker/docker-compose.yml` has no host port mapping. Add
    `ports: ["8000:8000"]` under `services.justup` for direct access, or connect
    the service to a reverse proxy. `JUSTUP_HOST` and `JUSTUP_PORT` control the
    listener in the container; adjust any mapping if you change its port.

## Data, users, and operation

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `JUSTUP_DATA_DIR` | no | `data/` locally, `/app/data` in the container | single data directory |
| `PUID`, `PGID` | no | `1000` | user/group mapping in the entrypoint |

## Settings in the interface

All remaining values do not exist as a variable; they are maintained under
*Settings* and stored in the database. Changes take effect without a restart.
Examples:

| Setting (key) | Default |
| --- | --- |
| `purge_enabled` | on |
| `pdf_page_size` | `a4` |
| `metrics_timeout_minutes` | 5 (1-10) |
| `geoip_auto_update` | on |
| `rate_limit_auth` | `10/minute` |
| `quality_targets` | built-in (min. 3) |
| `quality_min_score` | 70 (0-100) |
| `quality_rtt_threshold_ms` | 500 (1-10000) |
| `provider_lookup_enabled` | on |
| `provider_lookup_cache_ttl_seconds` | 1800 (60-86400) |
| `ip_api_key` (encrypted, never printed) | empty |
| `online_geo_enabled` | off |
| `signal_api_sender_number` among others | empty |

The POP location data (PeeringDB) refreshes itself: justUp decides when a refresh is due from the
timestamp of the last successful import (not the container start), keeps the data fresh for 30 days,
catches up overdue refreshes after a restart, and backs off on errors. There is no interval or
start-delay setting; the current state is visible under `GET /api/admin/pop/status`.

## signal-cli

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SIGNAL_CLI_PATH` | no | image default | path to the signal-cli binary (stays ENV: executable = security boundary) |
| `SIGNAL_DATA_DIR` | no | `/app/data/signal` | signal-cli data directory (bootstrap path) |
| `JAVA_XMS` | no | image default | Java initial heap for signal-cli |
| `JAVA_XMX` | no | image default | Java maximum heap for signal-cli |

See [Notifications](../features/notifications.md).
