<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-xx-xx

- Initial release of the project

### Added
- Compressed time-series storage: only the current UTC day of a metric series stays in a plain file; closed days move into gzip segments (`<series>.<period>.jsonl.gz`, one per ISO week for raw data, per month for 5-minute and per year for hourly rollups). Measured saving is 81-84 % of the time-series storage; reads, charts, reports, backup and restore are unchanged. Existing JSONL data is compressed gradually in the background (no migration at start). Releases before this format ignore the segments instead of reading them; see `docs/development/database-maintenance.md` (rollback). `tools/tsdb-bench.py` reproduces the measurements.
- Notification center: persistent per-user inbox (`notifications`, `notification_reads`) with an animated topbar bell, preview popover and `Manage > Notifications` (replaces the Alerts menu entry; `/ui/alerts` redirects to its Monitoring tab). Schema version 2 also adds `report_jobs.user_id`; PDF job status and download are limited to the requesting user and admins.
- TLS scanner rebuilt around a scanner-independent result model (`app/utils/tls_model.py`, `tls_target.py`, `tls_scanner.py`): every check is `secure`, `insecure` or `unknown`; failed or partial scans are shown explicitly in the PDF (previously the whole TLS section disappeared). New checks: hostname match, certificate chain order, key strength, expiry as a separate verdict, SAN list.
- Configurable session lifetime: new admin setting `session_max_days` (slider in Settings > General, 1-7 days, default 1 = previous fixed 24 h). Applies to newly issued tokens and cookies; existing sessions keep their expiry.
- Login honeypot: a hidden `website` field on the sign-in form. A filled field is rejected like a failed login and counts toward the existing exponential per-IP backoff lockout (2^n seconds, max. 1 hour); RFC 1918 addresses are never locked out. Every hit is logged as `LOGIN_HONEYPOT` warning.

### Changed
- **Breaking (API):** the `downsample_enabled` setting and its "Metric Downsampling" switch were removed; rollups and compression always run. `PATCH /settings` rejects the field with HTTP 422 and a stored legacy value is deleted at start. Rollups now convert only complete buckets and catch up from the last rolled-up bucket after a missed housekeeping run; `http_status` is no longer rolled up (a median of status codes is not a status). `GET /settings/storage` additionally returns `tsdb_saved_bytes`.
- **Breaking (API):** `POST /users/{id}/active` requires a boolean `is_active`; a missing or non-boolean value (e.g. `"false"`) now returns HTTP 422 instead of being coerced.
- `since` greater than `until` returns HTTP 400 on all metric, dashboard, report and downtime-history fragment endpoints.
- `PUT /targets/{id}/alert-users` answers HTTP 429 with `Retry-After` when 4 notification tasks are already running; the recipient assignment is left unchanged in that case.
- The login cookie lifetime now equals the configured absolute session lifetime (`session_max_days`), also at 1 day (was 1 hour); the server still enforces the 6 h idle expiry.
- The environment surface was reduced to bootstrap values (`SECRET_KEY(S)`, `LOG_LEVEL`, `UPMON_TRUSTED_PROXIES`, `JUSTUP_DATA_DIR`/`DATA_DIR`/`CACHE_DIR`, `JUSTUP_HOST`/`JUSTUP_PORT`, `JUSTUP_BACKUP_HMAC_SECRET`, `SIGNAL_CLI_PATH`/`SIGNAL_DATA_DIR`). Purge, PDF page size, metrics timeout, GeoIP auto-update, auth rate limit, quality probe targets/score/RTT, provider lookup (incl. encrypted ip-api key), online geo, POP refresh timing and the Signal sender are now settings (Settings > General > Advanced) and apply without restart.
- Signal: a configured sender (`signal_api_sender_number`) that is not a registered signal-cli account now raises an error instead of silently switching to the first account; the UI reads the sender from the database.
- **Breaking:** The initial admin password is no longer read from `UPMON_ADMIN_PASSWORD`. On first start a random one-time password (pattern `Tujoza47!`) is printed once to the console, never logged or stored, and must be changed on first sign-in (`/ui/change-password`); all other routes answer HTTP 423 until then. Admin password resets also force a change. Existing databases need the new `users.must_change_password` column.
- **Breaking:** The encryption/signing key is now read from `SECRET_KEY` (and `SECRET_KEYS` for rotation) instead of `JUSTUP_ENCRYPTION_KEY(S)`. Set `SECRET_KEY` in your environment or compose file.
- **Breaking:** The quality-probe targets variable is now `JUSTUP_QUALITY_TARGETS` instead of `UPMON_QUALITY_TARGETS`.
- **Breaking:** The default HTTP port is now `8000` (was `8080`). Override with `JUSTUP_PORT`; update any port mapping and health check accordingly.
- **Breaking:** The SQLite database file is now `justup.db` (was `app.sqlite3`) under `<data_dir>/sqlite/`. A fresh install creates it directly; there is no automatic rename of an existing file.
- The shipped Docker Compose file runs on the default bridge network and a named volume, with no site-specific registry, IP, or external network.
- The API version reported by the app now tracks `pyproject.toml` (1.0.0) automatically.
- The About page (`/ui/about`) was rebuilt on a three-card layout (Application Details, Project, Dependencies) plus a full-width changelog section, showing justUp content: version, Python and GeoIP build info, author and repository, license, resolved dependency versions, and the rendered changelog.
- The web UI now follows the Gentelella look and feel (MIT, v4.2.0): collapsible left sidebar with profile area and keyboard-operable submenu, top navbar, "x_panel" cards, teal accent with WCAG-adjusted contrast in the light and dark theme, Inter font. Gridstack and Chart.js are unchanged. The license text ships in `app/static/vendor/gentelella/`; `sb-admin-layout.css` was replaced by `gentelella-layout.css`.
- Country flags in the traceroute map now load at runtime from a public CDN (flag-icons on jsDelivr) instead of SVGs baked into the image. The flag SVGs are no longer copied into the container or served from `/static/vendor/images/flags/`; an environment with no outbound access to the CDN shows the two-letter country code as a text fallback.

- **Breaking:** Backups are signed with the full HMAC-SHA256 digest (64 hex characters in the file name, was 16). The key is derived (domain-separated) from `SECRET_KEY`/`SECRET_KEYS`, or taken from `JUSTUP_BACKUP_HMAC_SECRET`; it is no longer stored in the database or inside the backup. Backups created by earlier versions are rejected with a clear error and must be recreated.
- Restore validates the archive before extraction (entry count, total unpacked size, only regular files and directories under `data/{sqlite,tsdb,pdf_reports}`) and runs its blocking file work off the event loop.
- Report scans (WhatWeb, SSLyze) resolve the target once, validate it against the SSRF policy and connect to that pinned address; WhatWeb no longer follows redirects, and HTTPS hostnames are refused for WhatWeb because the address cannot be pinned with correct SNI.

### Removed
- **Breaking:** The server-side PDF world map and its cartopy/matplotlib-geo dependency. The interactive traceroute map stays in the web UI (vendored Leaflet); the PDF report shows the traceroute as a table.
- **Breaking:** The database migration framework. The schema is created in full on a fresh install (schema version 1); there is no in-place upgrade path.

### Added
- `run.py` as a developer/operator entry point that launches the app via uvicorn.

### Security
- Replaced the previously committed live key in `setup.conf` with a declared dummy dev placeholder and removed the dead registry/tooling references. `setup.conf` stays tracked as a developer cheat-sheet and is not gitignored (a file cannot be both tracked and ignored); it must never carry a real key. Any key ever committed to history must be treated as compromised, rotated, and purged from Git history separately.
- CVE-2026-69247 (cryptography): fixed by moving to `cryptography` 50.0.2. `sslyze` is installed unmodified from upstream commit `e064c2c8e7d251e5bc5db1a7beb21add5e12a0bc` (cap `cryptography<51`) because PyPI 6.3.1 caps `<47`; the hash-pinned set is `docker/requirements.lock`. No `.trivyignore` entry is needed.

</details>
