---
hide:
  - navigation
---
<p align="center">
  <img class="justup-docs-logo" src="assets/justup-logo.svg" width="320" alt="justUp! Logo">
</p>
<p align="center">
  <strong>Uptime monitoring – done right!</strong>
</p>
<p align="center">
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform"></a>
  <a href="https://github.com/Gill-Bates/justup"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

## What is justUp!?

justUp! is a self-hosted uptime monitor for small teams. It checks hosts with
ping, HTTP(S), TCP, and certificate checks, alerts by email and Signal, and
creates PDF reports. It runs as a single container with an embedded SQLite
database. No external database or agent is needed.

## Features

- [Monitoring & checks](features/monitoring.md): setup wizard, check types, dashboard.
- [Alerts](features/alerts.md): DOWN, recovery, and certificate alerts.
- [Notifications](features/notifications.md): email, Signal, and the in-app inbox.
- [PDF reports](features/reports.md): availability, SLA, TLS summary, route.
- [Traceroute & maps](features/traceroute.md): route timeline and map.
- [Quality gate](features/quality-gate.md): no false alerts when your own network fails.
- [Users & roles](features/users.md): viewer, alert handler, admin.
- [Backup & restore](features/backup.md): signed archive.

## Quick start

```bash
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
# Set SECRET_KEY in .env (see Quick start).
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

!!! warning "SECRET_KEY is required"
    justUp! does not start without `SECRET_KEY`.

The supplied Compose file publishes no port. Add `ports: ["8000:8000"]` under
`services.justup`, open `http://localhost:8000`, and sign in as `admin`. The
initial password is in `docker logs justup`; you must change it at first sign-in.

[Installation](getting-started/installation.md){ .md-button .md-button--primary }
[Quick start](getting-started/quick-start.md){ .md-button }

## Links

[GitHub](https://github.com/Gill-Bates/justup){ .md-button }
[Docker Hub](https://hub.docker.com/r/giiibates/justup){ .md-button }
[Report a bug](https://github.com/Gill-Bates/justup/issues){ .md-button }

justUp! is released under the MIT license (see the `LICENSE` file in the [repository](https://github.com/Gill-Bates/justup)).
