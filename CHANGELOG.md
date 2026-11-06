## [Unreleased]
- `Changed` Settings status labels (users, recipients, SMTP, Signal, delivery queue, public outbound IPs) and the recipient list in the target form show a coloured dot with the state word instead of a badge.

- `Changed` Dashboard widget status and notification severity show a coloured dot with the state word instead of a badge.

- `Changed` Status badges no longer show a check, cross or other icon; the state is carried by the badge colour and its word. Remaining web badges come from the JS helper `JuStatusBadge`; check and endpoint states are dots (see below).
- `Fix` On the Targets page, a refresh requested by an action (delete, enable/disable) or by the search could be dropped for two seconds after a live update; the list now always refreshes. Closing a group by hand during a search no longer reopens it on the next live update.
- `Fix` Mail subjects show `&`, `<` and quotes from the target name as typed instead of HTML entities such as `&amp;`; this covers alert mails, the test mail and the template preview.
- `Fix` The 24 h and 30 d uptime of a target is no longer erased by status refreshes within five minutes or by a connectivity-gate cycle.
- `Fix` A check switched off while failing no longer raises a DOWN alert that cannot resolve.
- `Fix` Deleting alerts or targets removes their pending notification events, and delivered events are pruned by retention.
- `Fix` The PDF "Estimated downtime" card names its real source (sum of measured outages) instead of "failed checks x interval".
- `Fix` Signal messages use the same check type and reason as alert mails. The UN/LOCODE refresh reports "not implemented" (HTTP 501) instead of a successful empty import.
- `Fix` The test mail links to your public hostname instead of the placeholder `justup.example.com`.
- `Changed` **Send Test Email** now sends a friendly justUp! welcome message in the usual mail design instead of a simulated "Service DOWN" alert. It shows the test recipient and send time, carries a TEST label, lists the next setup steps and ignores the alert template; the alert template preview is unchanged.
- `Changed` The link in alert mails and Signal messages opens the target with the time range of the event (30 minutes before and after). An unauthenticated visit goes through the sign-in and then continues to that page, also after a required password change.
- `Fix` Delivery queue log: the "No entries" / loading row spans the full table width again on wide screens.
- `Changed` All tab bars (Notifications, Edit Target, Target detail, Settings) now look the same: filled teal active tab and a thin teal scrollbar when the bar is wider than the screen.

- `Changed` Check and endpoint states (HTTP, Ping, TCP, UDP, certificate) on the Targets list and the target page are now a coloured dot in front of the label instead of a status badge. The state word stays available to screen readers and as tooltip; UNKNOWN is a hollow ring.
- `Changed` Traceroute hop results (OK, Partial, No reply) are shown as a coloured dot with the word instead of a badge; PDF unchanged.
- `Changed` The HTTP status donut lists each code with its count and share (for example `1,380 (97.2%)`); the separate class legend under the response-time chart is gone.
- `Fix` The response-time axis shows its unit (`ms`) also on narrow charts, and chart tooltips use the same unit format.
## [1.0.3] - 2026-11-05

- `New` UDP monitoring: UDP ports are checked next to TCP and shown per endpoint (for example `TCP:53`, `UDP:53`). `closed` counts as down only after two confirming scans; uncertain results stay unknown.
- `New` Port discovery with Nmap (TCP and/or UDP, Quick or Extended) in the target wizard and editor. Ports are entered once in one input with removable chips; confirmed open ports are listed first and preselected, other results stay collapsed. The wizard tile "TCP/UDP service" asks for ports on the address step.
- `New` Traceroute runs on Nmap instead of `mtr` (up to 30 hops). Silent hops show "No reply"; the Loss, Avg, Best and Worst columns are gone.
- `New` Traceroute recognizes internet-exchange (IXP) addresses by their PeeringDB prefix. The hop shows the exchange and, only when its facilities agree on one area, an estimated city and country; otherwise it names no location instead of a wrong one.
- `New` Admins can re-detect the services of a target (**Detect services**). A preview lists service and version changes; nothing is stored until you confirm.
- `New` Settings > General has a **Public hostname** field. Alert mails and Signal messages link to the target, and the "Open in justUp!" button now appears in mails.
- `New` Settings > Recipients has a search field and a **Re-check Signal** button per recipient (limited to 5 checks per minute).
- `New` The About page shows the Nmap version.
- `New` Settings > Network shows the public outbound IPv4 and IPv6 address of the server as seen by icanhazip.com and ipify.org (admins only, not stored, with a Refresh button). Stale addresses cannot be copied.
- `New` On the dashboard, a click on a target in "Downtime Overview (24h)" opens its page with the same 24 hours already selected. The whole row is the link.
- `Fix` **Save Target** in the target editor returns to the Targets overview instead of the target detail page, and a second save cannot start while the page changes.
- `Fix` Downtime no longer counts stretches without measurements. Gaps show as "No measurements", unknown results as "Unknown"; the dashboard, history and PDF report use the same durations.
- `Fix` UDP scans use the target host and no longer write an "up" point for mixed open and uncertain ports.
- `Fix` The PDF report no longer runs a TLS scan for `http://` targets, and its timeline lines up with the other charts.
- `Fix` The notification queue no longer freezes the UI and monitoring while the database is busy at start.
- `Fix` A hanging DNS resolver can no longer stall a check; every check has an overall deadline.
- `Fix` Zooming the availability and response-time charts no longer draws lines or downtime bands over the axes; resetting the zoom restores the full view.
- `Fix` Port discovery works outside Docker (`python run.py`) when Nmap is installed.
- `Fix` The Targets list updates live without flickering and keeps focus, open groups and the search.
- `Fix` "Reset metrics", "Delete target" and "Insert link" use the application's own dialogs instead of browser popups.
- `Fix` The Delivery Queue Log shows one card per entry on narrow screens, and the status `sent` reads "Sent".
- `Fix` The PoP data refresh waits with a normal backoff on HTTP 429 and skips locations with invalid coordinates.
- `Fix` IXP hops are located by their exchange shortly after an update; the first import no longer waits for the 30-day refresh interval, and cached traceroute results are rendered again with the new data without a new scan.
- `Fix` The Signal template preview resolves the optional `{{#if cta}}` blocks of the default templates and fills the status, badge and logo placeholders instead of showing the raw markers.
- `Fix` Signal registration recognizes "captcha required" and "number already registered" by a stable error code, so the matching hint appears whatever the wording of the message.
- `Security` Nmap scans run in a separate worker with only the `NET_RAW` capability; the web process gets no raw-socket rights and scans are admin-only.
- `Security` The bundled Nmap is built from PGP-verified source and uses the Debian PCRE2 and zlib packages, so the image scan covers them; the release build fails otherwise.


<details markdown="1">
<summary>Previous versions...</summary>

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
