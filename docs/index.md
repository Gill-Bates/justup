---
hide:
  - navigation
---

<p align="center">
  <img src="assets/justup-logo.svg" width="320" alt="justUp! Logo">
</p>

<p align="center">
  <strong>Uptime monitoring – done right!</strong>
</p>

<p align="center">
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform"></a>
  <a href="https://github.com/Gill-Bates/justup/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

## What is justUp!?

justUp! is a self-hosted uptime monitor for small teams. A guided wizard sets up
matching ping, HTTP(S), TCP, and certificate checks. justUp! stores the
measurement series locally, alerts via email and Signal, and generates
print-optimized PDF reports with embedded fonts.

justUp! runs as **a single container** with an embedded SQLite database and
time-series store: no external database, agents, or Grafana stack.

## Core features

<div class="grid cards" markdown>

-   :material-radar:{ .lg .middle } **Monitoring & Checks**

    ---

    The four-step wizard suggests website, API, server, TCP, or combined
    profiles. Suggestions, duplicates, and settings are shown before the start;
    existing targets stay in the full editor.

    [:octicons-arrow-right-24: Monitoring & Checks](features/monitoring.md)

-   :material-bell-ring:{ .lg .middle } **Alerts with a lifecycle**

    ---

    DOWN alerts with cooldown, immediate recovery notification, certificate
    warning, acknowledgment, and optional auto-close – open, acknowledged, closed.

    [:octicons-arrow-right-24: Alerts](features/alerts.md)

-   :material-message-alert:{ .lg .middle } **Email & Signal**

    ---

    Persistent delivery queue with retry and backoff. Email over SMTP
    (STARTTLS/TLS) and Signal over a local signal-cli.

    [:octicons-arrow-right-24: Notifications](features/notifications.md)

-   :material-file-pdf-box:{ .lg .middle } **PDF reports**

    ---

    Server-side RGB PDF reports with KPIs, SLA attainment, charts, TLS summary,
    and a traceroute route schematic and table, generated as an asynchronous job.

    [:octicons-arrow-right-24: Reports](features/reports.md)

-   :material-map-marker-path:{ .lg .middle } **Traceroute & Maps**

    ---

    Interactive traceroute map in the web UI (Leaflet, served locally), with
    GeoLite2 lookup and POP-code detection per hop.

    [:octicons-arrow-right-24: Traceroute & Maps](features/traceroute.md)

-   :material-shield-check:{ .lg .middle } **Quality Gate**

    ---

    justUp! measures its own connectivity. If conditions are poor, checks are
    suspended instead of sending false alerts.

    [:octicons-arrow-right-24: Quality Gate](features/quality-gate.md)

-   :material-account-group:{ .lg .middle } **Users & Roles**

    ---

    Multi-user operation with viewer, alert handler, and admin. Login, password
    change, and a personal theme per user.

    [:octicons-arrow-right-24: Users & Roles](features/users.md)

-   :material-backup-restore:{ .lg .middle } **Backup & Restore**

    ---

    Backup as an archive with integrity check; restore with maintenance mode,
    database quiescing, and rollback. Storage statistics and purge included.

    [:octicons-arrow-right-24: Backup & Restore](features/backup.md)

</div>

## Quick start

```bash
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
# Set SECRET_KEY in .env (Fernet key, see Quick Start).
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

!!! warning "Mandatory: SECRET_KEY"
    Without `SECRET_KEY`, justUp! does not start. There is no default.

!!! info "First access"
    The supplied Compose file does not publish a port. Add `ports: ["8000:8000"]`
    under `services.justup` before starting for direct access at
    `http://localhost:8000`. Sign in as `admin`; the initial password appears in
    `docker logs justup` until it has been changed, and must be changed on the
    first sign-in.

[:material-rocket-launch: Installation](getting-started/installation.md){ .md-button .md-button--primary }
[:material-book-open-page-variant: Quick Start](getting-started/quick-start.md){ .md-button }

## Security

Authentication, secret management, CSRF and rate-limiting behavior, and the
container hardening are described in the [Security Overview](security/overview.md).

## Contribute & Support

<div class="grid" markdown>

[:fontawesome-brands-github: GitHub repository](https://github.com/Gill-Bates/justup){ .md-button }
[:fontawesome-brands-docker: Docker Hub](https://hub.docker.com/r/giiibates/justup){ .md-button }
[:material-bug: Report a bug](https://github.com/Gill-Bates/justup/issues){ .md-button }

</div>

## License

justUp! is released under the [MIT license](https://github.com/Gill-Bates/justup/blob/main/LICENSE).

