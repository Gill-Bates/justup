## [1.0.2] - 2026-11-03

- `New` The Targets page has a search field that filters by name, host, URL, TCP port or group while you type.
- `New` Recipients have an "Enable for all Targets" switch that assigns them to all existing and future targets.
- `New` The traceroute map and PDF route map show one continuous route; estimated sections are dashed.
- `New` The TLS report rates every certificate a server presents (e.g. ECDSA next to RSA), not only the first.
- `Fix` Check-type badges in alert emails and PDF reports are always neutral grey; the PDF uses the current justUp! logo.
- `Fix` Recipient dialogs hide the fields of switched-off channels; the target wizard shows only settings of active checks.
- `Fix` A database upgraded from 1.0.0 is no longer rejected as incompatible on start.
- `Fix` Uptime counts a check cycle as up only if all enabled checks are up, matching the overall status.
- `Fix` Slow checks keep their target lock, so a target is no longer measured twice.
- `Fix` Certificate checks use the configured host and port and work for IP targets; TCP, certificate and SMTP ports are validated strictly instead of being silently replaced.
- `Fix` HTTP checks send a correct `Host` header for IPv6 addresses.
- `Fix` Enabling Basic Auth via `PATCH` without credentials is rejected with `422`.
- `Fix` Traceroute hops without reply are labelled "No reply" instead of "Estimated".
- `Fix` Probe Targets are limited to 20 entries and 20 seconds per run.
- `Fix` Stale GeoIP databases are refreshed after 45 days; `JUSTUP_PORT` outside 1-65535 stops the start with a clear message.
- `Fix` Hung background services and Signal device linking no longer block the application.
- `Security` HTTP checks and probes connect only to SSRF-validated addresses (no DNS rebinding).
- `Security` Backup restore re-validates the archive while extracting it.
- `Security` The login redirect check rejects backslash and encoded bypasses.
- `Security` Email template debug logs no longer contain placeholder values.


<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.1] - 2026-11-02

- `New` Seven optional dashboard widgets (hidden by default): active alerts, delivery queue (admins only), 24h uptime, certificate expiry, slowest monitors, quality trend and stale checks.
- `New` The Notification Center reports crashed or hung background services and persistent email handoff problems as one admin warning each, followed by a "recovered" entry.
- `New` Settings > Delivery Queue shows per channel the waiting count, next attempt, oldest message, cooldown and failures. "Sent" now reads "Accepted by server".
- `New` The alert email template editor previews the template with sample data and switches between DOWN and RESOLVED examples.
- `New` Stored secrets (SMTP password, ip-api key) are shown as a fixed mask; a "Remove stored ..." button clears them.
- `Security` The browser session no longer lives in `localStorage`; the UI uses the HttpOnly session cookie with CSRF protection. Signed-in browsers must sign in once after the update. API clients keep `Authorization: Bearer`.
- `Security` Email is encrypted by default: STARTTLS is on unless explicitly saved as off, port 465 uses implicit TLS, and a failed TLS negotiation never falls back to cleartext. Operators of a plaintext relay must switch *Use STARTTLS* off once in Settings > SMTP.
- `Security` Incomplete or undecryptable SMTP credentials stop the send instead of sending anonymously.
- `Fix` Emails survive long outages: temporary failures are retried for up to 24 hours, and messages are paced to respect SMTP and Signal rate limits.
- `Fix` A mail accepted by the server is no longer reported as failed or sent twice because of a late connection error; a *resolved* message no longer overtakes a delayed *open* message.
- `Fix` Manual acknowledgment now clears the active incident state, and the dashboard no longer looks healthy when recent handoffs failed.
- `Fix` Session Lifetime fully governs new sign-ins; the hidden six-hour inactivity expiry is gone.
- `Fix` Settings reload correctly after a backup (SMTP fields and STARTTLS toggle) and `docker-compose.yml` forwards `SECRET_KEYS`.
- `Fix` The downtime list no longer loses recent outages on targets with very large data sets.
- `Fix` Probe Targets accept schemeless entries only as bare host names or IP addresses; entries like `host:80` are rejected.
- `Fix` The alert email logo displays in all common mail clients, and the test email shows a realistic sample outage with a plain-text part.
- `Fix` Modals close only via Escape or Cancel/Close, not by clicking outside.
- `Fix` A duplicate recipient phone number answers `409 Conflict` instead of `400`.
- `Fix` An invalid `JUSTUP_PORT` or `LOG_LEVEL` falls back to the default with a warning naming the ignored value.

## [1.0.0] - 2026-11-01
- `New` Initial release of the project
