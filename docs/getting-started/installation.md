# Installation

justUp! runs as a single container with an embedded SQLite database.

## Deployment

=== "Docker Compose (recommended)"

    ```bash
    git clone https://github.com/Gill-Bates/justup.git
    cd justup
    cp .env.example .env
    # Set SECRET_KEY in .env and add ports: ["8000:8000"] under
    # services.justup in docker/docker-compose.yml.
    docker compose --env-file .env -f docker/docker-compose.yml up -d
    ```

    See [Docker](docker.md) for the Compose file.

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

## SECRET_KEY

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SECRET_KEY` is required. All variables: [Environment variables](../configuration/environment.md).

## Data directory

All persistent data is in `/app/data` (`JUSTUP_DATA_DIR`). Mount it as a volume
or bind mount.

| Content | Location |
| --- | --- |
| Database | `sqlite/` |
| Measurements | `tsdb/` |
| PDF reports | `pdf_reports/` |
| Signal account | `signal/` |
| GeoLite2 City and ASN databases | `geoip/` |

The entrypoint sets the owner of the data directory to `PUID`/`PGID` (default
`1000`) and then runs the app as that user, never as root. The supplied Compose
file bind-mounts its own `docker/` directory as the data directory and keeps PDF
reports in a `tmpfs`; see [Docker](docker.md) before you change this.

For HTTPS, use a [reverse proxy](../configuration/reverse-proxy.md).

## Updating

```bash
docker compose --env-file .env -f docker/docker-compose.yml pull
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

justUp! refuses to start on a database whose schema it does not support (for example one
written by a newer release or by a pre-release build). Back up before you upgrade
([Backup & restore](../features/backup.md)); see [Database baseline](../development/database-maintenance.md#schema-baseline-and-versioning).
