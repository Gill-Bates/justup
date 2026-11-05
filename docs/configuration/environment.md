# Environment Variables

Environment variables hold bootstrap values. Everything else is a runtime
setting in the web UI; see [Settings](settings.md). Changing a variable needs a
container restart.

## Secrets

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SECRET_KEY` | yes | – | Fernet key; encrypts stored secrets, keys the session-token hashes and signs backups; see [session token](../api/authentication.md#session-token) |
| `SECRET_KEYS` | no | – | Comma-separated keys for rotation (first = primary) |
| `JUSTUP_BACKUP_HMAC_SECRET` | no | derived from `SECRET_KEY(S)` | Dedicated backup signing key; `NEW,OLD` for rotation |

`SECRET_KEY` is a urlsafe-base64 32-byte Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

There is no default. Store the key only in the environment.

### Key rotation

Put the new key first. The first key encrypts; the others only decrypt existing
data.

```bash
SECRET_KEYS=<new-primary-key>,<old-key>
```

Session tokens depend on the first key only: after you change it, all users sign in
again, and an old key in second place does not keep sessions valid (it still decrypts
stored secrets). The shipped `docker/docker-compose.yml` forwards `SECRET_KEYS` to the
container; a Compose file without that line ignores the variable.

### Backup key

`JUSTUP_BACKUP_HMAC_SECRET` entries need at least 32 characters with at least 8
distinct characters (for example `openssl rand -hex 32`). Weak, empty, or duplicate
entries make backup and restore fail with a configuration error. New backups are
signed with the first entry; all entries verify. Keep an old entry until no backup
signed with it is needed. See [Backup & restore](../features/backup.md).

## Server and network

| Variable | Default | Purpose |
| --- | --- | --- |
| `JUSTUP_HOST` | `0.0.0.0` | Bind address in the container |
| `JUSTUP_PORT` | `8000` | Bind port in the container; an invalid value falls back to `8000` with a warning |
| `TRUSTED_PROXIES` | empty | Proxy addresses trusted for `X-Forwarded-For`, `X-Real-IP`, and `X-Forwarded-Proto` |
| `LOG_LEVEL` | `INFO` | Log level (`CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`); access lines appear only at `DEBUG`. An invalid value falls back to `INFO` with a warning |

justUp! supports exactly one app process per data directory. There is no
`WORKERS` variable.

The supplied Compose file publishes no port. Add `ports: ["8000:8000"]` or use a
reverse proxy; adjust the mapping if you change `JUSTUP_PORT`.

## Data and user mapping

| Variable | Default | Purpose |
| --- | --- | --- |
| `JUSTUP_DATA_DIR` | `/app/data` in the container | Data directory |
| `PUID`, `PGID` | `1000` | Owner of the data directory and the app process |

## signal-cli

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIGNAL_CLI_PATH` | image default | Path to the signal-cli binary |
| `SIGNAL_DATA_DIR` | `/app/data/signal` | signal-cli data directory |
| `JAVA_XMS`, `JAVA_XMX` | `64m`, `256m` | Java heap for signal-cli |

See [Notifications](../features/notifications.md).

## Runtime settings

These are not variables. Admins change them in the UI without a restart:

| Setting | Default |
| --- | --- |
| `purge_enabled` | on |
| `pdf_page_size` | `a4` |
| `metrics_timeout_minutes` | 5 (1-10) |
| `session_max_days` | 1 (1-7) |
| `geoip_auto_update` | on |
| `rate_limit_auth` | `10/minute` |
| `quality_targets` | built-in list (min. 3) |
| `quality_min_score` | 70 (0-100) |
| `quality_rtt_threshold_ms` | 500 (1-10000) |
| `provider_lookup_enabled` | on |
| `online_geo_fallback_enabled` | on until you switch it off; an opt-out is stored and kept |
| `provider_lookup_cache_ttl_seconds` | 1800 (60-86400) |
| `ip_api_key` (encrypted) | empty |
| `signal_api_sender_number` | empty |
