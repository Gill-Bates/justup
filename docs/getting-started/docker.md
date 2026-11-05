# Docker

## Image

`giiibates/justup` on [Docker Hub](https://hub.docker.com/r/giiibates/justup),
multi-arch for `linux/amd64` and `linux/arm64`.

## Docker Compose

`docker/docker-compose.yml` pulls the published image. Excerpt:

```yaml
services:
  justup:
    image: ${JUSTUP_IMAGE:-giiibates/justup:latest}
    container_name: justup
    init: true
    restart: unless-stopped
    stop_grace_period: 60s
    environment:
      PUID: 1000
      PGID: 1000
      LOG_LEVEL: INFO
      SECRET_KEY: ${SECRET_KEY:?Set a persistent key for justUp}
      SECRET_KEYS: ${SECRET_KEYS:-}
    volumes:
      - ./:/app/data
    tmpfs:
      - /app/data/pdf_reports:size=256m,mode=0700,uid=1000,gid=1000
```

The file also sets log rotation (50 MB x 5), a health check on `/healthz`, and
the hardening options below.

- `./` is relative to the Compose file, so data lands in the repository's
  `docker/` directory. Change the mount for another location. Starting the same
  stack from another directory (a copied Compose file, a re-imported stack) mounts an
  empty data directory: the database, users and sessions seem gone.
- Compose reads `.env` from the directory of the Compose file (`docker/`), not
  from the directory you run the command in. Use `--env-file` to be explicit.
- PDF reports live in a 256 MiB `tmpfs` and are gone after a restart.
- The file publishes no port. Add `ports: ["8000:8000"]` under `services.justup`
  or connect the service to a reverse proxy.

```bash
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

!!! warning "SECRET_KEY is required"
    The start aborts without it. Keep the key stable; otherwise stored secrets
    cannot be decrypted and all sessions end, because the session-token hashes
    are keyed with it. Mount the data volume persistently as well.

!!! warning "Behind a reverse proxy, set TRUSTED_PROXIES"
    Add `TRUSTED_PROXIES` with the proxy address as the container sees it.
    Without it, the login rate limit and audit log record the proxy IP for every
    client. See [Reverse proxy](../configuration/reverse-proxy.md).

## Shutdown

`docker stop` triggers an orderly shutdown (schedulers, PDF worker, Signal,
database). `stop_grace_period: 60s` keeps Docker from killing the container
after 10 s. After a hard kill, SQLite recovers its WAL on the next start.

## Ping and traceroute

`ping` needs `cap_net_raw`; the image sets it on the binary. Traceroute runs in the
Nmap worker with `--privileged` and raw sockets, which also need `NET_RAW`. The Compose
file adds `NET_RAW` back to the capability set.

## UDP scans and the Nmap worker

UDP monitoring and port discovery use Nmap, which is built into the image. A separate worker
process holds `NET_RAW` for it; the web process does not. See
[Network monitoring with Nmap](../features/network-monitoring.md#linux-capabilities).

## Container hardening

- The entrypoint starts as root only to set up `PUID`/`PGID` and the data
  directory, then drops privileges with `gosu`.
- Missing data subdirectories are created as the service user. A recursive
  `chown` runs only when the owner of `/app/data` differs from `PUID:PGID`.
- The Compose file drops all capabilities and adds back `CHOWN`, `FOWNER`,
  `SETUID`, `SETGID`, and `NET_RAW`. `no-new-privileges` is set.
- `/healthz` is public and checks liveness only. It returns `ok` and a timestamp and,
  when the service supervisor is available, `degraded` and the state of each background
  service. Error details and crash counters are not exposed.

## signal-cli

The image bundles Java 25 (Temurin) and signal-cli for Signal notifications.
Tune memory with `JAVA_XMS` and `JAVA_XMX`. See [Notifications](../features/notifications.md).

## Logs

```bash
docker logs -f justup
```

Uvicorn access lines appear only at `LOG_LEVEL=DEBUG`. The initial admin password
is printed to the container output, so restrict access to the Docker logs.
