<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/justUp/main/static/img/justup-white.svg" width="200">
<br>
Uptime Monitor – done right!
</p>

[![Docker Hub](https://img.shields.io/docker/v/giiibates/justup?label=Docker%20Hub&logo=docker&logoColor=white)](https://hub.docker.com/r/giiibates/justup)
[![Docker Pulls](https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white)](https://hub.docker.com/r/giiibates/justup)
[![Docker Image Size](https://img.shields.io/docker/image-size/giiibates/justup/latest?logo=docker&logoColor=white)](https://hub.docker.com/r/giiibates/justup)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-linux%2Famd64%20|%20linux%2Farm64-lightgrey?logo=linux&logoColor=white)](https://hub.docker.com/r/giiibates/justup)


Seriously – why another monitoring tool?

**Because I wanted something that actually fits.**

justUp is a self-contained, Docker-first monitoring solution for websites and endpoints, designed to be simple to operate, efficient in storage, and professional in output — without external databases, agents, or Grafana stacks.

---

## What makes justUp different

<img src="https://github.com/Gill-Bates/justUp/blob/main/static/img/justup_1.png?raw=true" alt="Screenshot" width="800">
<br><br>

- **Truly self-contained**  
  No Postgres, no InfluxDB, no Redis. justUp ships with its own SQLite + resampled TSDB and runs as a single container.

- **Performance-critical paths compiled with Cython**  
  12 core modules (~8500 LOC) compiled to native binaries: auth, crypto, monitoring engine, network analysis, rate limiting. Combines Python flexibility with C-level performance and code obfuscation.

- **Modern Python 3.13 runtime**  
  FastAPI backend with async/await throughout. Minimal dependencies, security-hardened with selective bounds checking on sensitive modules.

- **Server-rendered UI (no SPA)**  
  HTMX-based, fast, predictable, and SEO/ops-friendly. No React/Vue overhead.

- **Meaningful metrics, not noise**  
  One primary uptime signal per target, with auxiliary checks for context.

- **Professional reporting**  
  High-quality PDF reports with charts, downtime tables, and traceroute maps.

---