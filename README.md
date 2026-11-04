<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/static/img/justup_1c_white.svg">
    <img src="app/static/img/justup_1c.svg" width="320" alt="justUp! Logo">
  </picture>
</p>

<p align="center">
  <strong>Self-hosted uptime monitoring with network diagnostics built in.</strong><br>
  Fewer false alarms. Clearer diagnostics. Reports you can share. All in one container.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/justup/releases"><img src="https://img.shields.io/github/v/release/Gill-Bates/justup?logo=github&logoColor=white" alt="GitHub Release"></a>
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Supported Platforms"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

<p align="center">
  <a href="#getting-started"><img src="https://img.shields.io/badge/Quick%20Start-168B75?style=for-the-badge&logo=docker&logoColor=white" alt="Quick Start"></a>
  <a href="https://gill-bates.github.io/justup/"><img src="https://img.shields.io/badge/Documentation-34495E?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation"></a>
</p>

---

justUp! brings availability monitoring, network diagnostics, and PDF reporting
together. Check hosts over ping, HTTP(S), TCP, and UDP, discover open ports with Nmap, and track certificate expiry.
When something goes wrong, inspect the network route and share the findings
without switching tools.

Everything runs in **one container**, with application state and measurement
history stored locally. No agents to install on monitored hosts, no separate
Grafana instance, and no external database to maintain.

## Why justUp!

| Why it matters | What you get |
| --- | --- |
| 🛡️ Fewer false alarms | The Quality Index checks justUp!'s own connectivity. When it falls below the configured minimum, due checks pause and affected targets show `unknown` rather than triggering misleading DOWN alerts. Checks resume when connectivity recovers. |
| 🧭 Diagnose beyond up or down | Run traceroute on demand, inspect hop details, and explore the route on a map without leaving the target view. |
| 📄 Reports ready to share | Generate print-ready PDFs per target with availability, SLA attainment, performance, TLS results, and network route details. |
| 📡 One container, no monitoring stack | Guided setup, checks, dashboard, and alerting together. SQLite and the built-in time-series database keep state and measurements local. |
| 📈 History beyond the latest check | Explore measurement charts and long-term rollups without running a separate metrics database. |
| 🔔 Alerts that keep trying | Receive email and Signal alerts, with queued delivery and retries when delivery fails. |

## Getting started

Requires Docker. Generate a secret key using the image, then start justUp!:

```bash
export SECRET_KEY="$(docker run --rm --entrypoint python giiibates/justup:latest -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
docker run -d \
  --name justup \
  --init \
  -p 127.0.0.1:8000:8000 \
  -e SECRET_KEY="$SECRET_KEY" \
  -v justup-data:/app/data \
  giiibates/justup:latest
```

Open <http://127.0.0.1:8000> and sign in as `admin`. Find the initial password
with `docker logs justup`; justUp! requires you to change it at first sign-in.
Keep `SECRET_KEY` safe and reuse it when recreating the container so stored
secrets remain readable.

For Compose, reverse proxies, configuration, backups, and operations, see the
[justUp! documentation](https://gill-bates.github.io/justup/).

Licensed under the [MIT License](LICENSE).
