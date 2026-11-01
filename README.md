<p align="center">
  <img src="app/static/img/justup_1c.svg" width="220" alt="justUp! Logo">
</p>
<p align="center">
  <strong>justUp! – self-hosted uptime monitor. Done right.</strong>
</p>
<p align="center">
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey"><img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

justUp! checks hosts via ping, HTTP(S), TCP, and certificate expiry. A guided
wizard suggests suitable checks for new targets. justUp! stores time series
locally, alerts by email and Signal, and generates print-optimized PDF reports
with embedded fonts and high-resolution charts.

It runs as **a single container** – no external database, no agents, no Grafana
stack. Persistence is an embedded SQLite database with compacted time-series
storage.

## Quick start (Docker)

Prerequisite: Docker with the Compose plugin. The bundled Compose file uses
`giiibates/justup:latest`. It publishes **no port** by default; for direct
access, add the following under `services.justup` before starting:

```yaml
ports:
  - "8000:8000"
```

```sh
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
# Generate a Fernet key and set it as SECRET_KEY in .env:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

With the port published, the UI is available at `http://localhost:8000`.
The random initial password for `admin` appears in `docker logs justup` and is
printed again on every restart until you change it. The first login forces a
password change. The Compose file stores data in the `docker/` directory and
keeps generated PDF files in an ephemeral `tmpfs`; see
[Docker](docs/getting-started/docker.md) for details.

## Documentation

The full user and operations documentation is the central reference:

**→ [Documentation](https://gill-bates.github.io/justup/)**

Quick links: [Quick start](docs/getting-started/quick-start.md) ·
[Installation](docs/getting-started/installation.md) ·
[Environment variables](docs/configuration/environment.md) ·
[Security](docs/security/overview.md)

For local development, see [Setup](docs/development/setup.md) and
[Contributing](docs/development/contributing.md).

## License

Licensed under the [MIT License](LICENSE).
