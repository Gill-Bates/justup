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

<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-11-01
- `New` Initial release of the project
