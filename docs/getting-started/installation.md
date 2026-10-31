# Installation

justUp! runs as a single container with an embedded SQLite database and
time-series store; it needs no additional service.

## Deployment variants

=== "Docker Compose (recommended)"

    ```bash
    git clone https://github.com/Gill-Bates/justup.git
    cd justup
    cp .env.example .env
    # Set SECRET_KEY in .env and add ports: ["8000:8000"] under
    # services.justup in docker/docker-compose.yml for direct browser access.
    docker compose --env-file .env -f docker/docker-compose.yml up -d
    ```

    Details on the Compose setup and the capabilities (`cap_net_raw` for ping and
    MTR) are in [Docker](docker.md). The bundled Compose file pulls the image
    and does not publish a port by default.

=== "docker run"

    ```bash
    docker run -d \
      --name justup \
      -p 8000:8000 \
      -e SECRET_KEY="<fernet-key>" \
      -e PUID=1000 -e PGID=1000 \
      -v justup-data:/app/data \
      giiibates/justup:latest
    ```

## Generate SECRET_KEY

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SECRET_KEY` is mandatory and is read without the `JUSTUP_` prefix. Full
reference of all variables: [Environment Variables](../configuration/environment.md).

## Data directory

All persistent data lives in a single directory. In the container that is
`/app/data` (variable `JUSTUP_DATA_DIR`). Mount it as a named volume or
bind mount so that data survives a restart.

| Content | Description |
| --- | --- |
| Database | Configuration, users, targets, alerts (SQLite) |
| Measurement data | Time series with rollups (raw, 5 min, 1 h) |
| Reports | Generated PDF files |
| signal-cli | Signal account and state (`/app/data/signal`) |

## User mapping (PUID/PGID)

The entrypoint starts briefly as root, only to set the owner and permissions of
the data directory (`PUID`/`PGID`, default `1000`), and then drops privileges via
`gosu`. The application process never runs as root. The supplied Compose file
bind-mounts its own `docker/` directory to `/app/data` and places PDF reports in
a volatile `tmpfs`; see [Docker](docker.md) before changing either mount.

!!! note "Reverse Proxy"
    For HTTPS and external reachability, place justUp! behind a reverse proxy.
    See [Reverse Proxy](../configuration/reverse-proxy.md).

## Updating

```bash
docker compose --env-file .env -f docker/docker-compose.yml pull
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

On start, justUp! applies pending schema migrations (forward-only, stored in
`PRAGMA user_version`) and refuses to start on a database written by a newer
release. Back up before upgrading ([Backup & restore](../features/backup.md)). A
database file named `app.sqlite3` from a pre-release build is not renamed to
`justup.db` automatically.
