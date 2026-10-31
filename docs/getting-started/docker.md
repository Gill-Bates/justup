# Docker

justUp! ships as a single container image with an embedded SQLite database and
time-series store; no external database or agent is needed.

## Image

The official image is on Docker Hub: [`giiibates/justup`](https://hub.docker.com/r/giiibates/justup).
It is published for `linux/amd64` and `linux/arm64` as a multi-arch manifest.

## Docker Compose

The repository contains a Compose file at `docker/docker-compose.yml`. It pulls
the published image by default; it does not build the source tree:

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
    volumes:
      - ./:/app/data
    tmpfs:
      - /app/data/pdf_reports:size=256m,mode=0700,uid=1000,gid=1000
```

The excerpt omits log rotation (`json-file`, 50 MB x 5 files), the healthcheck,
and the hardening options listed below. The bind mount `./` is resolved relative
to the Compose file, so persistent application data goes to the repository's
`docker/` directory. Generated PDF files use a 256 MiB `tmpfs` and disappear on
container restart. Change the mounts if you need a different data location or
persistent report files.

The supplied file also has no `ports` mapping. Add the following under
`services.justup` to reach the UI directly at `http://localhost:8000`, or
connect it to your reverse proxy:

```yaml
ports:
  - "8000:8000"
```

Then start it:

```bash
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

!!! warning "SECRET_KEY is mandatory"
    Without `SECRET_KEY`, the start aborts (`${SECRET_KEY:?...}`). Generate the
    key once and keep it stable, otherwise stored secrets can no longer be
    decrypted.

## Orderly shutdown

`docker stop` sends SIGTERM; Uvicorn then runs the lifespan shutdown: background work is signaled,
schedulers (max. 20 s), POP refresh, PDF worker (max. 20 s), and the Signal backend are stopped, and only
afterwards are the SQLite connections closed. Each step is time-bounded, and errors are logged.
Open event streams (`/ui/events`) are cancelled after 5 s so that they do not hold up the shutdown.
`stop_grace_period: 60s` prevents Docker from aborting with SIGKILL after 10 s.

Manual check (not possible in the automated test environment):

```bash
docker compose --env-file .env -f docker/docker-compose.yml up -d
time docker stop <container>          # should stay well under 60 s
docker logs <container> | tail -n 20  # "Shutting down application..." ... "Shutdown complete"
docker inspect -f '{{.State.ExitCode}}' <container>   # 0, not 137 (SIGKILL)
```

After a hard abort (`docker kill`), SQLite starts in WAL mode without intervention and replays the WAL
on the next open.

## Capabilities for ping and MTR

The checks for ping and the traceroute feature use `ping` and `mtr`. In the image
these binaries receive `cap_net_raw` via `setcap` (no SUID). The application
process itself runs as an unprivileged user.

## Container hardening

- The entrypoint starts briefly as root only for the UID/GID and filesystem
  setup and drops privileges before the app start via `gosu`. If the
  privilege-drop tool is missing, the start aborts.
- Missing data subdirectories (`sqlite`, `tsdb`, `pdf_reports`, `signal`,
  `geoip`) are created as the service user (`PUID`/`PGID`). There is no
  recursive `chown` on every start – only when the owner of `/app/data` differs.
- The Compose file drops all capabilities (`cap_drop: ALL`) and adds back only
  `CHOWN`, `FOWNER`, `SETUID`, `SETGID`, and `NET_RAW`; `no-new-privileges` is set.
- Healthcheck on `/healthz` (public, without details).

## signal-cli in the container

For Signal notifications, the image bundles a trimmed Temurin 25 (LTS) Java runtime and signal-cli.
The image builds libsignal's native library for both supported architectures; it
does not download a prebuilt native binary from an unverified release. Memory
and paths can be controlled via `JAVA_XMS`, `JAVA_XMX`, and `SIGNAL_CLI_PATH`.
See [Notifications](../features/notifications.md).

## Logs

```bash
docker logs -f justup
```

Application logs include a request ID. Uvicorn's per-request access lines are
written only at `LOG_LEVEL=DEBUG`. The first-start admin
password is printed directly to container stdout until changed, so restrict
access to Docker logs and change that password promptly.
