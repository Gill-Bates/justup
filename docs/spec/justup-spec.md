# justUp! – Historical specification for the structured rebuild

| | |
| --- | --- |
| Status | Historical draft 0.2 |
| As of | 2026-10-27 |
| Target version | v1.0.0 (project reset) |
| Basis | Current-state analysis of the repository `/opt/justup` (static reading, Ruff run), reference stack `/opt/fetchly` |
| Goal of the draft | Original basis for the product scope, architecture, and acceptance criteria of the v1.0.0 rebuild |

Requirements carry IDs (`FR-…` functional, `NFR-…` non-functional, `ARC-…` architecture, `DEC-…` open decision). "MUST", "SHOULD", and "MAY" were normative for the planned rebuild. The section [Current defects](#9-current-defects-and-acceptance-criteria) is a snapshot taken on 2026-10-27.

!!! warning "Historical planning document – not a current implementation contract"
    This specification captures the draft before the structured rebuild. Parts
    of it were implemented, changed, or discarded; in particular API paths,
    authentication, runtime structure, dependency pinning, and PDF export now
    diverge. For the current state, the feature and security pages apply,
    [API Overview](../api/overview.md), [Architecture](../development/architecture.md),
    and the API reference generated at `/openapi.json`. The following requirements
    and audit findings are kept as a historical decision basis.

---

## 1. Product

justUp is a self-hosted uptime monitor for small teams. It checks hosts via ping, HTTP(S), TCP, and certificate lifetime, stores measurement series locally, alerts via email and Signal, and generates reports. It runs as a single container without an external database.

### 1.1 In scope

| Area | Content |
| --- | --- |
| Monitoring | Targets with ping, HTTP, TCP, and certificate checks, interval, retries, SLA goals, groups |
| Quality Gate | Measure the instance's own connectivity; suspend checks in a poor situation instead of sending false alerts |
| Status and alerts | Status per target and service, alert lifecycle (open, acknowledged, closed), auto-close, certificate warning |
| Measurement data | Time series with rollups (raw, 5 min, 1 h) and retention per target |
| Visualization | Dashboard, target list, detail page with charts, downtime history, interactive traceroute map (Leaflet, web UI only) |
| Reports | Server-side PDF report per target (asynchronous job) |
| Notification | Recipients, assignment to targets, persistent delivery queue, email (SMTP), Signal (signal-cli) |
| Users | Multi-user operation with roles, login, password change, theme |
| Operation | Backup and restore, storage statistics, purge, CLI for password reset |
| Geo | GeoLite2 lookup, POP-code detection in hostnames, provider lookup |

### 1.2 Not in scope (currently)

DNS check as its own check type, maintenance windows, public status page, multi-tenancy, horizontal scaling. See [DEC-01](#11-open-decisions) for possible inclusion.

### 1.4 Project reset

- **FR-00** The project is reset. The next release is **v1.0.0**; the version history 1.1.x does not continue.
- The current code serves as a functional reference, not as a compatibility contract. Schema, environment variables, API paths, and data formats may change without a transition layer.
- `CHANGELOG.md` starts anew with the section `1.0.0`.
- Whether data from a 1.1.x installation must be importable is decided by [DEC-13](#11-open-decisions).

### 1.5 Versioning principle: Latest is greatest

- **FR-00a** Versions are defined exclusively in `pyproject.toml`; the project version is there as well. There is no `VERSION` file and no `requirements.txt`.
- Runtime dependencies in `pyproject.toml` stay unpinned (like fetchly); only the dev and docs tools are pinned, so that a new Ruff or MkDocs version does not break unchanged code.
- Base image, system packages, Java, signal-cli, and vendor assets follow the respective latest state. The Dockerfile contains no version or digest pins; integrity is secured via signatures or published checksums of the source, not via hard-coded hashes.
- So that both architectures of a release are identical, the release pipeline resolves the rolling states once per run (base digest, signal-cli version, dependency constraints) and passes them through as build arguments. These values are not written into the repository.

### 1.3 Roles

| Role | May |
| --- | --- |
| Viewer (signed in) | read dashboard, targets, metrics, alerts, reports; change own password and theme |
| Alert handler (`can_close_alerts`) | additionally acknowledge alerts |
| Admin | everything, including targets, groups, recipients, users, settings, backup, Signal account |

---

## 2. Functional requirements

### 2.1 Targets and checks

- **FR-01** A target has a name, an optional group, an active flag, an interval (5–86,400 s), retention (1–3650 days), max. retries (0–10), and independently toggleable checks.
- **FR-02 Ping**: ICMP via the system `ping`, with a process timeout. Result: reachable, latency.
- **FR-03 HTTP**: GET or HEAD, optional basic auth, configurable success codes (default `200-399`), port override, optional certificate ignoring, no automatic following of redirects. Result: reachable, status code, latency.
- **FR-04 TCP**: multiple ports per target, status and latency per port.
- **FR-05 Certificate**: remaining days until expiry; host/port derivable from the HTTP URL.
- **FR-06** Hosts MUST be validated as a hostname (RFC 1123) or an IP literal; free text is not allowed.
- **FR-07** Outbound checks MUST be protected against SSRF: loopback, link-local, and metadata addresses are blocked; RFC 1918 remains allowed (internal monitoring is a use case). DNS resolution happens once, and exactly that address is used for the check and the connection.
- **FR-08** MTR/traceroute runs only on demand (detail page, PDF), not periodically.
- **FR-09** An admin can reset a target's measurement data and trigger a test alert.

### 2.2 Scheduler

- **FR-10** Checks run aligned to the interval, with stable jitter per target (0–10 s), retries with exponential backoff (max. 30 s).
- **FR-11** Each target is scheduled independently; a slow check MUST NOT delay other targets. Concurrency is bounded by a global limit.
- **FR-12** Configuration changes to targets are applied without a restart (at the latest after 30 s or immediately via a signal from the API).
- **FR-13** After a crash, the scheduler resumes cleanly; heartbeat and target locks remain if more than one process is conceivable, otherwise they are dropped (see ARC-04).

### 2.3 Quality Gate

- **FR-14** Every 30 s the instance measures at least 5 reference targets (configurable) and computes a smoothed score (EMA). If it falls below the threshold, due checks are suspended, affected targets go to `unknown`, and no DOWN alerts are created.
- **FR-15** The gate state is visible in the dashboard and available as a history.

### 2.4 Status and alerts

- **FR-16** Per target there is a current status per service, an overall status (`up`, `down`, `degraded`, `unknown`), as well as uptime over 24 h and 30 days.
- **FR-17** A DOWN alert is created when a service is down for longer than `metrics_timeout_minutes` (1–10, default 5); `unknown` never alerts. DOWN notifications have a 5 min cooldown, RECOVERY is reported immediately.
- **FR-18** Certificate alert at ≤ 7 remaining days (threshold configurable, SHOULD), resolution after renewal.
- **FR-19** Alerts have a UUID, target, service, start, end, duration, notified recipients, and acknowledgment (who, when). Auto-close toggleable per target.
- **FR-20** Acknowledgment individually or in bulk by admin or alert handler.

### 2.5 Measurement data

- **FR-21** Metrics: `ping_up`, `ping_ms`, `http_up`, `http_ms`, `http_status`, `tcp_up`, `tcp_ms`, `tcp_up_<port>`, `tcp_ms_<port>`, `cert_days_left`.
- **FR-22** Rollups: raw 30 days, 5-min median 180 days, 1-h median 365 days; binary metrics with minimum instead of median; `http_status` is not compacted. Retention additionally bounded per target.
- **FR-23** Queries select the resolution automatically by time range (≤ 30 days raw, ≤ 180 days 5 min, otherwise 1 h) and return availability series and downtime periods.
- **FR-24** Admin can delete all measurement data or that of one target.

### 2.6 Interface

- **FR-25** Dashboard with a status overview and a quality widget, live refresh.
- **FR-26** Target list with pagination (50 per page), group filter, and live status.
- **FR-27** Target detail: charts with zoom and time-range selection, availability, downtime history, recipient assignment, interactive traceroute map with geo hops.
- **FR-27a** Maps are rendered exclusively client-side with the locally served Leaflet; the backend delivers only geo/route data (hops with coordinates, segment type). The base map is locally served Natural Earth country outlines (1:110m, public domain, `app/static/vendor/geo/`). There is no external map, tile, or static-map API and no server-side map rendering.
- **FR-28** Forms for targets, groups, recipients, users, and settings (General, Storage, Backup, SMTP, Users, Recipients, Spooler, Signal).
- **FR-29** About page with version, component versions, and a rendered CHANGELOG.
- **FR-30** Theme light, dark, system, stored per user.

### 2.7 Reports

- **FR-31** PDF report per target (A4 or Letter): status banner, KPIs, SLA attainment, charts, provider info, tech stack (WhatWeb), TLS summary (SSLyze), traceroute table with location details per hop. The report contains no world map.
- **FR-32** Generation exclusively as an asynchronous job with progress, download, and expiry; the synchronous endpoint is dropped. At start, orphaned jobs are marked as failed.
- **FR-33** Client-side PDF export (jsPDF/html2canvas) is dropped in favor of FR-31, unless [DEC-05](#11-open-decisions) decides otherwise.

### 2.8 Notifications

- **FR-34** Recipients have a name, phone (Signal), and/or email (at least one), an active flag, and are assigned to targets. The legacy mapping `target_alert_users` is dropped.
- **FR-35** The dispatcher creates only queue entries; only the spooler sends.
- **FR-36** Spooler: persistent queue, max. 5 attempts, backoff 60 s to 1 h with jitter, processing timeout, cleanup after 7 days. Signal throttled (1 message, 2 s apart), email in batches.
- **FR-37** Email via SMTP with STARTTLS or implicit TLS, editable subject and HTML template with a placeholder whitelist, test email.
- **FR-38** Signal via local signal-cli: registration (SMS/call, captcha), verification, linking as a secondary device via QR, unlinking, profile name, editable DOWN/UP templates, test message.
- **FR-39** Spooler UI: statistics, log, flush, clear log.

### 2.9 Users, sign-in, administration

- **FR-40** User CRUD by admins; admins cannot lock themselves out or remove the last admin.
- **FR-41** Login with username and password; lockout after failed attempts per IP **and** per username.
- **FR-42** Backup as an archive of the database, measurement data, and reports with an integrity check; restore as a background job with a maintenance state (scheduler and spooler paused), then a controlled restart of the services.
- **FR-43** Storage statistics, purge of alerts and measurement data.
- **FR-44** CLI `justup-cli reset-password <username|--all-admins>`.

### 2.10 Geo and POP

- **FR-46** GeoLite2-City for traceroute hops. Obtained only from a license-compliant source (MaxMind account, see DEC-06) with a checksum; if the file is missing, the app continues to run without the geo feature.
- **FR-47** POP-code detection from hostnames (provider-specific parsers, token matching, prefix match, embedded fallback table), data basis from PeeringDB and UN/LOCODE with a monthly refresh, admin view for status and runs.
- **FR-48** Provider lookup (ASN/ISP) with a cache, disableable.

### 2.11 Telemetry

- **FR-49** Telemetry is **opt-in**, off by default, and fail-closed (an error reading the setting means off). First transmission at the earliest after explicit consent. No secret hidden in the source. See DEC-07.

---

## 3. Non-functional requirements

### 3.1 Security

- **NFR-01** One authentication model: HMAC-signed session in the HttpOnly cookie, `SameSite=Lax`, `Secure` behind HTTPS (via an environment variable, not via an unverified header).
- **NFR-02** CSRF protection (double-submit) for **all** unsafe methods on all routes, including `/api/*`. Origin check with a Referer fallback; if both are missing, it is rejected.
- **NFR-03** Authorization per route declarative; write access to groups and recipients only for admins.
- **NFR-04** No global "auth off" switch. If needed, only as a read-only guest access (DEC-02).
- **NFR-05** Password hashing with argon2id or PBKDF2-SHA256 with at least 600,000 iterations; login consumes constant time even for unknown users.
- **NFR-06** Secrets at rest with Fernet/MultiFernet (key rotation). The key comes only from the environment; there is no default and no key in the repository.
- **NFR-07** Subprocesses (`ping`, `mtr`, `whatweb`, `signal-cli`, `sslyze`) only as an argument list, with `--` before positional hosts and with a hard timeout.
- **NFR-08** Output in the frontend without `innerHTML` for user data; a shared escaper. Markdown rendering is sanitized (nh3).
- **NFR-09** User objects for templates never contain `password_hash`.
- **NFR-10** Content Security Policy without inline scripts; all assets served locally.
- **NFR-11** Rate limits without SlowAPI or another third-party library, as a dedicated hardening following the fetchly pattern (`app/auth/rate_limit.py`):
  - Sliding-window limiter per route as a FastAPI dependency `rate_limit("10/minute")`; units `second`, `minute`, `hour`.
  - Storage bounded per route (fetchly: `MAX_KEYS = 10_000`), expired keys first, then the least recently used.
  - Client IP only from `X-Forwarded-For` when the direct sender is in `FORWARDED_ALLOW_IPS`. Invalid entries or `*` abort the start with an error.
  - Response `429` with `Retry-After`.
  - Limits for login, expensive endpoints (PDF, traceroute, restore, test messages), and SSE connections per client.
  - State lives in the process; this requires `WORKERS=1` (ARC-04).
  - In addition, a login lockout per IP and user (FR-41) as well as an invisible anti-bot token in the login form as in fetchly.

### 3.2 Robustness and data

- **NFR-12** No blocking call in the event loop: SQLite, file I/O, subprocesses, SMTP, archives run in threads or processes (Ruff `ASYNC` without a finding).
- **NFR-13** Database connections are opened via a context manager and always closed; no global registry. `journal_mode=WAL`, `busy_timeout` ≥ 5 s, `foreign_keys=ON` per connection.
- **NFR-14** Read requests (in particular session checks and polling) create no write transaction; session extension at most once per minute.
- **NFR-15** One schema with versioned migrations, each migration in an explicit transaction together with its version entry. Fresh installation and upgrade yield the same schema (the test compares both).
- **NFR-16** Measurement-data writes are crash-safe (atomic compaction, fsync of file and directory or a SQLite transaction).
- **NFR-17** Exceptions are never silently swallowed; broad `except` only with a reason and logging (Ruff `BLE`, `S110` without a finding).
- **NFR-18** All timestamps UTC-aware; invalid timestamps are an error, not epoch 0.

### 3.3 Performance

- **NFR-19** Goal: 500 targets at a 60-s interval on 1 vCPU / 1 GB RAM without interval drift over 2 s (measured in a load test, confirm the value in DEC-08).
- **NFR-20** Metric queries for 30 days of a target under 300 ms; query time does not grow linearly with the total retention.
- **NFR-21** Memory usage stable over 7 days of continuous operation (no growth from connections, caches, or task state).

### 3.4 Privacy and operation

- **NFR-22** External services (provider lookup, online geo, GeoIP update, POP refresh, telemetry) are individually disableable and listed in the documentation.
- **NFR-23** The initial admin password is generated randomly on the first start (pattern `Tujoza47!`), printed to stdout on every start until it was changed, never logged or stored in the database (only in a private 0600 file outside the backups that is deleted on the change); `must_change_password` forces the change, and until then a gate blocks all other paths with HTTP 423 (DEC-09).
- **NFR-24** Logs structured, with a request ID, without secrets.
- **NFR-25** Accessibility: keyboard operation, visible focus, contrast per WCAG 2.2 AA, `prefers-reduced-motion`. Full WCAG conformance requires a manual check with assistive technologies.

---

## 4. Architecture and tech stack

The stack follows `/opt/fetchly`. Deviations are justified and marked.

### 4.1 Stack

| Layer | Decision | Like fetchly |
| --- | --- | --- |
| Language | Python ≥ 3.13 | yes |
| Web | FastAPI, Starlette, Pydantic for request validation | yes |
| Server | Gunicorn with `uvicorn-worker`, **`WORKERS=1`** (scheduler and queues live in the process) | yes |
| Persistence | stdlib `sqlite3`, hand-written SQL, `sqlite3.Row`, no ORM | yes |
| Measurement data | see DEC-03 (recommendation: a dedicated SQLite file `metrics.db` instead of JSONL) | deviation, fetchly has no time series |
| Migrations | versioned migrations from baseline `0001` in v1.0.0 | **deviation**: fetchly has none; justUp needs them for upgrades from v1.0.0 |
| Templates | Jinja2, server-side rendered, macros and `_` partials | yes |
| Frontend | hand-written ES modules without a build step, Bootstrap 5 vendored under `app/static/vendor/` | yes; whether HTMX stays is decided by DEC-04 |
| Realtime | Server-Sent Events (`/api/events`, `EventSource`) instead of polling | yes |
| Charts/maps | Chart.js (+ zoom, annotation, date-fns) and Leaflet, both vendored. Leaflet is the only map renderer (web UI, FR-27a), but without a tile server: base map from local Natural Earth GeoJSON | extension |
| PDF | ReportLab, matplotlib | extension |
| Session | HMAC-SHA256-signed tokens (`app/auth/session.py`) | yes |
| CSRF | double-submit middleware (`middleware/csrf.py`) | yes |
| Rate limit | dedicated hardening in `app/auth/rate_limit.py` (NFR-11); **no SlowAPI** | yes |
| Lint/format | Ruff, `line-length = 120`, `target-version = "py313"`, rule set like fetchly | yes |
| Tests | pytest with unittest classes (`tests/_support.py`), `node:test` for JS | yes |
| Frontend lint | ESLint, Stylelint, source-contract check under `tools/` | yes |
| Docs | MkDocs Material, configuration in `docs/mkdocs.yml`, deployment via `docs-build.yml` (section 8.3) | yes |
| Container | `docker/Dockerfile`, `docker/entrypoint.sh`, `docker/docker-compose.yml`, build context repo root | yes |
| CI/release | the same workflows as fetchly: `ci.yml`, `docker-build.yml`, `docs-build.yml`, `ui-audit.yml` (section 8.2) | yes |
| Versioning | `pyproject.toml` as the single source for version and dependencies; `app/utils/version.py` reads `[project].version` from it; no `VERSION`, no `requirements.txt`; start at 1.0.0 | yes |
| Pinning | "Latest is greatest": pins only in `pyproject.toml` (section 1.5) | yes |

### 4.2 Directory structure (target picture)

```text
app/
  main.py                 # app factory, middleware, lifespan, router registration
  config.py               # environment variables, read once, typed
  db/                     # connection, schema, migrations, repositories per domain
    connection.py
    migrations/           # 0001_baseline.sql, 0002_….py
    targets.py users.py alerts.py notifications.py settings.py pop.py reports.py
  metrics/                # time series: writing, rollups, queries, retention
  monitor/                # scheduler, checks (ping, http, tcp, cert), Quality Gate, alert logic
  notifications/          # dispatcher, spooler, email, Signal, templates
  reports/                # PDF jobs and rendering
  geo/                    # GeoIP, POP codes, provider lookup, traceroute
  auth/                   # session.py, credentials.py, rate_limit.py, permissions.py
  routes/                 # pages.py, events.py, targets.py, alerts.py, metrics.py, users.py,
                          # groups.py, recipients.py, settings.py, signal.py, reports.py, pop.py, health.py
  runtime/                # background services, lifecycle, maintenance state
  utils/                  # version, changelog, time, template filters
  static/  js/ css/ img/ fonts/ vendor/
  templates/  base.html, pages, macros/, _partials
middleware/csrf.py
cli/justup_cli.py
tests/  _support.py, test_*.py, js/*.test.mjs
tools/  eslint.config.mjs, stylelint.config.mjs, ui-lint/
docker/ Dockerfile, entrypoint.sh, docker-compose.yml
docs/   mkdocs.yml, user and operations documentation
```

- **ARC-01** Routes know no SQL statements; they call repository functions in `app/db/`. Monitor and notifications import no routes.
- **ARC-02** No module over ca. 600 lines and no function over ca. 80 lines without a justified exception (guideline, not a CI gate).
- **ARC-03** No function-local imports to work around circular dependencies.
- **ARC-04** One process, one scheduler. Background services (scheduler, spooler, Quality Gate, housekeeping, POP refresh, PDF worker, optional telemetry) are started centrally in `app/runtime/`, monitored (done callback, restart with backoff), and stopped in a fixed order.
- **ARC-05** Compute-intensive or blocking work: checks and DB in the thread pool, PDF and restore in a dedicated process.
- **ARC-06** All API routes under `/api/…`, pages under `/…`, SSE under `/api/events`, health under `/healthz` (public, without details).
- **ARC-07** No test-code path in production modules (`TESTING`, `PYTEST_CURRENT_TEST` are dropped).

### 4.3 Configuration

Runtime settings live in the table `settings`; server configuration comes from the environment. Application-specific settings consistently use the `JUSTUP_` prefix. Because of the project reset, there is no transition support for the old prefixes `UPMON_`. Exempt from the prefix are deliberately defined infrastructure and ecosystem names (`PORT`, `WORKERS`, `FORWARDED_ALLOW_IPS`, `LOG_LEVEL`, `SIGNAL_CLI_PATH`, `JAVA_XMS`, `JAVA_XMX`, `PUID`, `PGID`) as well as the one key/HMAC name `SECRET_KEY` (or `SECRET_KEYS` for rotation), which the application reads without a prefix.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SECRET_KEY` | yes | – | key for Fernet encryption and HMAC signature (session, CSRF token) |
| `SECRET_KEYS` | no | – | comma-separated key list for rotation (first = primary) |
| `JUSTUP_DATA_DIR` | no | `data/` locally, `/app/data` in the container | single data directory |
| `PORT`, `WORKERS` | no | `8000`, `1` | `WORKERS` MUST stay 1 |
| `FORWARDED_ALLOW_IPS` | no | `127.0.0.1,::1` | trusted proxies |
| `JUSTUP_BEHIND_HTTPS` | no | `0` | `Secure` cookies |
| `LOG_LEVEL` | no | `INFO` | logging |
| `JUSTUP_QUALITY_TARGETS`, `…_MIN_SCORE`, `…_RTT_THRESHOLD_MS` | no | built-in, 70, 500 | Quality Gate |
| `JUSTUP_PROVIDER_LOOKUP`, `JUSTUP_ONLINE_GEO`, `JUSTUP_GEOIP_UPDATE`, `JUSTUP_POP_REFRESH` | no | see DEC-07 | external services individually switchable |
| `SIGNAL_CLI_PATH`, `JAVA_XMS`, `JAVA_XMX` | no | image default | signal-cli |
| `PUID`, `PGID` | no | `1000` | user mapping in the entrypoint |

`docs/configuration/environment.md` is the authoritative reference; this table is transferred there.

---

## 5. Data model (target picture)

Changes compared to the current schema are in bold.

| Table | Essential columns | Change |
| --- | --- | --- |
| `users` | id, username UNIQUE, password_hash, is_admin, is_active, can_close_alerts, theme, last_login_at, last_login_ip, created_at | **theme moved here from `settings`** |
| `sessions` | id, user_id FK CASCADE, token_hash UNIQUE, expires_at, max_expires_at, last_seen_at, created_at | **replaces `auth_tokens`** |
| `login_attempts` | key (ip or user), failed_count, locked_until, last_attempt_at | **key IP and user** |
| `settings` | key PK, value JSON, updated_at | keys only from an enum |
| `groups` | id, name UNIQUE, description, timestamps | – |
| `targets` | as current, plus **`group_id` FK** instead of `group_name` | **foreign key** |
| `target_status` | as current | – |
| `recipients`, `target_recipients` | as current | **`target_alert_users` is dropped** |
| `alerts` | as current | – |
| `notification_queue` | as current including `target_id` | **only defined in the schema, never in the spooler** |
| `monitor_quality`, `monitor_quality_latest` | as current | – |
| `report_jobs` | as current | – |
| `pop_locations`, `pop_alias_candidates`, `pop_import_runs` | as current | – |
| `schema_migrations` | version, applied_at | replaces `_migrations` |
| `scheduler_runs`, `target_locks` | – | **dropped** with one process (ARC-04) |

### 5.1 Implemented schema (shipped v1.0.0)

The table above is the planned target picture. The schema that ships is created by `init_schema()` in
`app/db/sqlite.py` (`SCHEMA_VERSION = 3`, stamped in `PRAGMA user_version`; WAL journal, `synchronous=FULL`).

| Area | Tables |
| --- | --- |
| Identity | `users`, `auth_tokens` (session tokens, kept under this name), `login_attempts` |
| Configuration | `settings`, `groups`, `targets`, `recipients`, `target_recipients`, `target_alert_users`, `dashboard_layouts` |
| Monitoring state | `target_status`, `alerts`, `monitor_quality`, `monitor_quality_latest`, `scheduler_runs`, `target_locks` |
| Notifications | `notification_queue` (idempotent delivery queue), `notifications`, `notification_reads` (inbox) |
| Reports | `report_jobs` |
| Geo and POP | `pop_locations`, `pop_alias_candidates`, `pop_import_runs` |

Forward-only migrations (`MIGRATIONS`): v2 adds `report_jobs.user_id` and the notification inbox tables;
v3 removes the obsolete POP refresh interval settings. Measurement series are stored outside SQLite in the
file-based time-series store (`app/db/tsdb*.py`).

Measurement data (DEC-03, recommendation): a dedicated file `metrics.db` with tables `samples_raw(target_id, metric, ts, value)`, `samples_5m`, `samples_1h`, primary key `(target_id, metric, ts)`, `WITHOUT ROWID`. Rollups and retention as a nightly job in transactions.

---

## 6. Interfaces

- **API**: JSON under `/api`, resources as in the current state (targets, metrics, alerts, groups, recipients, users, settings, Signal, reports, POP, quality). Pydantic models for input and output. Errors uniformly as `{"detail": …, "code": …}`.
- **Pages**: `/`, `/login`, `/targets`, `/targets/{id}`, `/targets/{id}/edit`, `/alerts`, `/groups`, `/settings`, `/about`.
- **SSE** `/api/events`: events `target.status`, `alert.opened`, `alert.closed`, `quality.changed`, `report.progress`, `restore.progress`.
- **Public without sign-in**: only `/login`, `/healthz`, static assets, `robots.txt`.
- **Bearer token** for automation: optional as a per-user API key (DEC-10); without this decision, cookie sessions only.
- `docs/api/endpoints.md` becomes the binding endpoint list.

---

## 7. Quality assurance

| Gate | Command | Required in CI |
| --- | --- | --- |
| Python lint | `ruff check .` | yes |
| Format | `ruff format --check .` | yes |
| Python tests | `pytest` | yes |
| JS/CSS lint | `npm run lint` | yes |
| JS tests | `npm test` | yes |
| Container | `docker build --check -f docker/Dockerfile .`, `shellcheck docker/entrypoint.sh`, `actionlint` | yes |
| UI audit | `npm run ui-lint` (Playwright, axe) | separate workflow |

Required test areas (each with its own test module by contract, not by ticket):

1. Auth: a cookie without a CSRF token is rejected on POST/PUT/PATCH/DELETE to `/api/*`; role matrix per route; lockout per IP and user.
2. Migrations: fresh installation and upgrade from the last current schema yield an identical schema; an abort in the middle of a migration leaves the old state.
3. Scheduler: a slow check does not delay other targets; the Quality Gate suspends checks and does not alert.
4. Alert logic: threshold, cooldown, recovery, auto-close, certificate.
5. Spooler: retry, backoff, timeout, no double sending.
6. Checks: host validation, SSRF including DNS rebinding (mocked resolver), subprocess arguments with `--`.
7. Measurement data: rollups, retention, resolution selection.
8. Backup/restore: the integrity check fails on tampering; restore runs in the maintenance state.

---

## 8. Container, release, and documentation

### 8.1 Container

- **OPS-01** `docker/Dockerfile`, multi-stage build on `ARG PYTHON_BASE=python:3.13-slim` (current Debian of the image series), dependencies from `pyproject.toml`. No digest in the Dockerfile; the release pipeline sets the digest per run.
- **OPS-02** Runtime binaries: `iputils-ping`, `mtr-tiny`, `whatweb`, a jlink-trimmed Temurin 25 LTS runtime (newest 25.x patch from the Adoptium repository, GPG-verified, built in a separate stage; no JDK in the image), and the latest signal-cli version, verified against the SHA-256 digest that GitHub publishes for the release asset. `cap_net_raw` via `setcap` on `ping` and `mtr`, no SUID.
- **OPS-03** The application process never runs as root. A minimal entrypoint MAY start as root exclusively for UID/GID and filesystem setup (PUID/PGID mapping) and MUST drop privileges before the app start (e.g. via `gosu`). If the privilege-drop tool is missing, the entrypoint aborts. No recursive `chown` on every start (only on owner mismatch of the root directory).
- **OPS-04** The build works from a fresh clone; GeoLite2 and build metadata are not mandatory files on the build host.
- **OPS-05** Healthcheck on `/healthz`; Compose example without a private registry, without fixed IPs, with `LOG_LEVEL=INFO`.
- **OPS-06** Version in the image only via the co-copied `pyproject.toml`; `BUILD_INFO` is optional.

### 8.2 Release pipeline (identical to fetchly)

`.github/workflows/` adopts the fetchly workflows and adjusts only names, image, smoke tests, and rolling tools:

| Workflow | Trigger | Task |
| --- | --- | --- |
| `ci.yml` | push, pull request, manual | PR gate: Ruff (`check`, `format --check`), pytest, ESLint, Stylelint, source contracts, `node --test`, `docker build --check`, Shellcheck, Actionlint; manifest check (version only in `pyproject.toml`, no `VERSION`, no `requirements.txt`) |
| `docker-build.yml` | tag `v*`, manual | check metadata (tag = `pyproject.toml` version), resolve rolling states once (base digest of `python:3.13-slim`, signal-cli version, dependency constraints for amd64 and arm64), build per architecture, smoke test (`/healthz`, privilege drop, no build artifacts in the image, tool versions), Trivy scan, push, multi-arch manifest with version equality of both architectures, cleanup of old tags (only `latest` and the current version remain), GitHub release with the Trivy finding |
| `docs-build.yml` | push to `main` with docs changes, pull request, manual, weekly | build MkDocs and publish to GitHub Pages; weekly Trivy scan of the docs dependencies |
| `ui-audit.yml` | manual, pull request | Playwright UI audit with axe |

- **OPS-07** Release flow as in fetchly: set the version in `pyproject.toml`, add to `CHANGELOG.md`, push the tag `vX.Y.Z`; everything else is done by `docker-build.yml`. Note on the current situation: the branch is called `feature/v1.0.0`, but `pyproject.toml` carries `1.1.14`. This does not yet violate anything as long as no tag is created, but MUST be resolved before the first release — per OPS-08 the goal is `v1.0.0`, so `pyproject.toml` is to be set to `1.0.0` before the tag (tag and `pyproject.toml` version identical).
- **OPS-08** The first release is `v1.0.0`. Beforehand, the old 1.1.x tags in the target registry must be removed or a new repository used, because the fetchly pipeline sets `latest` only for the highest published version (DEC-15).
- **OPS-09** The target registry is Docker Hub; the private registry from `setup.conf` is dropped (DEC-16).

### 8.3 Documentation (MkDocs)

- **DOC-01** User and operations documentation lives as a MkDocs Material site under `docs/`, configuration `docs/mkdocs.yml`, dependencies in the `docs` extra of `pyproject.toml`, publication via `docs-build.yml`.
- **DOC-02** Navigation modeled on fetchly: Getting Started (Quick Start, Installation, First Steps, Docker), Features (Checks, Alerts, Notifications, Reports, Traceroute), Configuration (Environment Variables as the authoritative reference, Settings, Reverse Proxy, Storage and Retention), Security (Overview, Authentication, Rate Limiting, Best Practices), API Reference, Development (Setup, Architecture, Contributing), Troubleshooting; the workflow copies `CHANGELOG.md` and `LICENSE` into the site.
- **DOC-03** The root README is a short landing page with a link to the site; `README_DOCKER.md` moves into the documentation.
- **DOC-04** This specification is filed under `docs/development/` after its acceptance (intended in the historical draft; the file still lives under `docs/spec/`).

---

## 9. Current defects and acceptance criteria

Findings from the static analysis on 2026-10-27 (not reproduced at runtime). Each finding counts as resolved in the rebuild when the criterion is met.

| ID | Severity | Current finding (evidence) | Acceptance criterion in the rebuild |
| --- | --- | --- | --- |
| D-01 | high | Cookie auth applies to all paths (`app/api/auth.py:31`, prefix `"/"`), CSRF only for `/ui/` and `/login` (`app/middleware/csrf.py:23`) | NFR-01/02; test 7.1 |
| D-02 | high | The connection registry holds every SQLite connection (`app/db/sqlite.py:59`), almost never removed | NFR-13, NFR-21 |
| D-03 | high | Restore blocks the event loop and swaps data under running connections (`app/api/settings.py:770-950`) | FR-42, NFR-12; test 7.8 |
| D-04 | high | Encryption key and admin password (`admin`) were public on `main` in `setup.conf` and are therefore still findable in the Git history. On `feature/v1.0.0`, `setup.conf` is now a versioned developer cheat sheet with a **declared dummy dev secret** (`SECRET_KEY`, not a real production secret) and without dead references; the file stays tracked and is **not** ignored via `.gitignore` (a file cannot be tracked and ignored at the same time). Evidence: `git log -- setup.conf`, history; current file content. | NFR-06; every real key ever committed as well as the admin password are to be treated as compromised, rotated, and removed from the Git history (DEC-11). For the dummy value there is no rotation obligation. `.env.example` contains no real secret. |
| D-05 | high | CI workflows expect the fetchly layout and fail; the Docker build needs ignored files | section 7 and OPS-04 |
| D-06 | high | Telemetry on by default, fail-open, obfuscated key in the code (`app/services/telemetry.py:51-91,556-575`) | FR-49 |
| D-07 | high | No tests, no lint setup | section 7 |
| D-08 | medium | Partially fixed: `mtr` now has a hard process timeout (`subprocess.run(..., timeout=timeout_s)`). Still open: a missing `--` before the positional host for `ping`/`mtr`, for `ping` only the OS-side `-W` timeout instead of a hard process timeout, and FR-06 is unmet because `TargetBase.host` validates only the length but no RFC-1123/IP-literal contract (`app/monitor/checks.py:419`, `app/services/mtr_run.py`, `app/models/targets.py`) | FR-06 (host contract), NFR-07 (`--` before host, hard timeout also for `ping`) |
| D-09 | medium | Partially fixed: ping and HTTP resolve DNS and connect to the determined IP. Still open is a residual path in the HTTP check: a fallback to the original URL can resolve again by hostname, so that FR-07 ("resolve once and use exactly that address") is not guaranteed (`app/monitor/checks.py:285-300,559-601`) | FR-07 (no fallback to hostname resolution); test 7.6 (DNS rebinding including the HTTP fallback path) |
| D-10 | medium | Scheduler: synchronous DB in the loop, batch `gather` blocks on the slowest check (`app/monitor/scheduler.py:368-430`) | FR-11, NFR-12; test 7.3 |
| D-11 | medium | Every authenticated request writes (`app/db/sqlite.py:1043`) | NFR-14 |
| D-12 | medium | TSDB without fsync, queries read whole files (`app/db/tsdb.py:291-296,393-396`) | NFR-16, NFR-20 |
| D-13 | medium | Migrations not atomic, schema defined twice (`app/utils/migration.py:741`, `app/db/sqlite.py:166`) | NFR-15; test 7.2 |
| D-14 | medium | Fresh installation: `notification_queue` without `target_id`, the spooler uses the column (`app/services/notifications/spooler.py:226,352`) | data model section 5; test 7.5 |
| D-15 | medium | Backup HMAC truncated to 64 bit, comparison with `!=`, key lies in the backup (`app/api/settings.py:87-128`) | FR-42: full HMAC-SHA256, `compare_digest`, key from `SECRET_KEY` |
| D-16 | medium | "Auth off" makes every caller an admin (`app/utils/auth_helpers.py:149-157`) | NFR-04 |
| D-17 | medium | Groups and recipients writable by every user (`app/api/groups.py`, `app/api/recipients.py`) | NFR-03 |
| D-18 | medium | GeoLite2 from an unofficial mirror without a checksum (`app/utils/bootstrap.py:30`) | FR-46 |
| D-19 | medium | 180 broad `except`, 34 `except: pass` | NFR-17 |
| D-20 | medium | God modules (e.g. `pdf_report.py` 3159, `sqlite.py` 2138, `scheduler.py` 1765 lines), triply duplicated client-IP logic, four JS escapers | ARC-01 to ARC-03 |
| D-21 | medium | Mixed indentation (tabs and spaces) | `ruff format` |
| D-22 | medium | Two data-directory variables (`JUSTUP_DATA_DIR`, `DATA_DIR`) | section 4.3 |
| D-23 | low | Cookie expires after 1 h, token slides up to 24 h | cookie lifetime = session lifetime |
| D-24 | low | `Secure` flag follows an unverified `X-Forwarded-Proto` | NFR-01 |
| D-25 | low | User enumeration via login timing; PBKDF2 300,000 iterations | NFR-05 |
| D-26 | low | `password_hash` in the template context (`app/db/sqlite.py:1050`) | NFR-09 |
| D-27 | low | Generated admin password in the container log (fixed: stdout once, never log/file, forced change) | NFR-23 |
| D-28 | low | CHANGELOG rendered unfiltered as `| safe` | NFR-08 |
| D-29 | low | FastAPI version `0.1.0` deviates from the release version | version only from `pyproject.toml` |
| D-30 | low | Old obfuscation remnants (short names, XOR keys) from the Cython era | remove without replacement |

Not yet checked: `pdf_report.py`, the notification modules in detail, `traceroute_*`, `route_interpolator`, `sslyze_scanner.py`, and the majority of the templates and JS logic. Before the start of the respective phase, a dedicated analysis is due there.

---

## 10. Approach

Rebuild in the existing repository with the goal v1.0.0. The current code is replaced module by module and serves as a reference until then. Each phase ends with a green CI gate and a runnable container; only v1.0.0 is published.

| Phase | Content | Result |
| --- | --- | --- |
| 0 Foundation | rotate secrets, `pyproject.toml` as the single source (version 1.0.0), Ruff configuration, `ruff format`, `docker/` layout, fetchly workflows, `tests/_support.py`, MkDocs scaffold, `CHANGELOG.md` anew | green gate |
| 1 Security | `/api` prefix, one session model, CSRF everywhere, a dedicated rate limiter instead of SlowAPI, role matrix, lockout per user, remove "auth off", telemetry opt-in | D-01, D-04, D-06, D-16, D-17, D-23 to D-27 |
| 2 Persistence | connection context manager, one schema plus migrations, `notification_queue` corrected, repositories per domain | D-02, D-11, D-13, D-14 |
| 3 Runtime | `app/runtime/`, scheduler per target, checks hardened, restore as a job | D-03, D-08 to D-10 |
| 4 Measurement data | implement decision DEC-03 | D-12, NFR-20 |
| 5 Frontend | SSE instead of polling, vendor folder, one escaper, CSP, ESLint/Stylelint, `node:test` | D-28, NFR-08, NFR-10 |
| 6 Modules | split `pdf_report`, notifications, geo into `app/reports/`, `app/notifications/`, `app/geo/` | D-20 |
| 7 Docs and release | MkDocs content complete, release dry run, tag `v1.0.0` | sections 8.2, 8.3 |

Upgrade path: as of v1.0.0, upgrades MUST run without data loss via the migrations. For installations of version 1.1.x there is no automatic upgrade path, as long as DEC-13 defines nothing else.

---

## 11. Open decisions

| ID | Question | Recommendation |
| --- | --- | --- |
| DEC-01 | Include DNS check, maintenance window, public status page? | Maintenance window yes (most common need), the rest later |
| DEC-02 | Guest access without login needed? | No; if so, only read-only and only its own status page |
| DEC-03 | Measurement data: keep JSONL or SQLite `metrics.db`? | SQLite: transactions, indexes, one backup format, no file lock |
| DEC-04 | Keep HTMX or, like fetchly, pure ES modules? | Pure ES modules with SSE, to follow the fetchly stack; HTMX only if rebuilding the fragments is too expensive |
| DEC-05 | Keep client-side PDF export? | No, one report path (server-side) |
| DEC-06 | GeoLite2 source: MaxMind account with a license key or without geo? | MaxMind account, optional via an environment variable |
| DEC-07 | Keep telemetry? Defaults of the external lookups? | Drop telemetry or make it opt-in; external lookups off by default |
| DEC-08 | Target load (number of targets, smallest interval)? | 500 targets at 60 s as a baseline |
| DEC-09 | Handling of the initial admin password | Random initial password to stdout on every start until changed (plaintext only in a 0600 file `.bootstrap-admin-credentials` in the data root, deleted on the change, not part of backups), forced change (`must_change_password`), gate (HTTP 423); no environment variable |
| DEC-10 | API key for automation? | Yes, per user, stored hashed only |
| DEC-11 | Clean the Git history because of leaked secrets? | Yes, if the repository is shared; rotation in any case |
| DEC-12 | License: justUp is MIT (`LICENSE`, `pyproject.toml`) | MIT, as fetchly. The vendored SSLyze wheel stays AGPL-3.0 (see `docker/vendor/PROVENANCE.json`) |
| DEC-13 | One-time import of 1.1.x data (targets, users, recipients, optionally measurement data)? | Configuration only as an import tool, not measurement data |
| DEC-14 | GitHub Actions: fetchly pins actions to the commit SHA (supply-chain protection). Does "Latest is greatest" apply there too? | Keep SHA pins as in fetchly and update via Renovate; otherwise pins only in `pyproject.toml` |
| DEC-15 | Old 1.1.x image tags: delete or a new Docker Hub repository? | Delete old tags before v1.0.0 |
| DEC-16 | Keep serving the private registry `docker.cirrio.de`? | No, only Docker Hub as in fetchly |
| DEC-17 | Keep server-side map rendering for the PDF report? | **Decided 2026-10-27: no.** The server-side map library together with its geo-data dependencies, the associated Docker stage, and the geo-data directory is dropped. Maps only interactive in the web UI (Leaflet, FR-27a); the PDF report shows the traceroute as a table (FR-31). matplotlib stays exclusively for PDF charts. A later server-side map requires a new decision and a change to FR-31 |
