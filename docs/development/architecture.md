# Architecture

justUp! is a FastAPI application that runs as **one Uvicorn process** (`python -m app`)
in the container. Scheduler, rate limiter, maintenance mode, and queues live in
that process, so do not run several app processes against one data directory.

| Area | Technology |
| --- | --- |
| Runtime | Python 3.13, Uvicorn, FastAPI |
| Storage | SQLite (`data/sqlite/justup.db`), time series in `data/tsdb` |
| Frontend | Bootstrap 5, HTMX, plain JavaScript without a build step |
| Maps and charts | Leaflet, Chart.js (served locally) |
| PDF | ReportLab, matplotlib |
| Notifications | SMTP, signal-cli |

A watchdog restarts crashed background services; their state is part of the
`/healthz` response. The current API is described by `/openapi.json` and the
[API overview](../api/overview.md).
