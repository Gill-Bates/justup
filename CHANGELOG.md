## [1.0.3] - 2026-11-04
- `New` Settings > General has a **Public hostname** field with an auto-detect button. Alert mails (HTML and plain text) and Signal messages link to the target at `https://<hostname>/ui/targets/<id>`.
- `Fix` The green "Open in justUp!" button was missing in real alert mails because no instance URL could be set. It now appears as soon as a public hostname is saved; without one, mails have no button and Signal messages no link.
- `Fix` The plain-text part of alert mails shows the URL of a link instead of dropping it.

## [Unreleased]
- `New` Admins can re-detect the services of a target (**Detect services**): a preview shows the changes and nothing is stored until the admin confirms.
- `Change` **Detect services** lists service changes and version-only changes in separate groups, and **Apply changes** asks in the shared confirmation dialog before anything is stored. A second detection for the same target is refused (409) while one is running.
- `Change` A host change resets the observed port service data (per protocol) and the target status.
- `New` The sidebar has an About entry, and the About page shows the Nmap version.
- `Change` The PDF downtime log and downtime KPI use the recorded downtime periods; gaps without measurements no longer count as downtime.
- `Fix` PDF downtime minutes in the KPI and the downtime log now use the same whole-second durations as the dashboard and the downtime history.
- `Fix` UDP scans no longer fall back to the TCP host; they use the target host.
- `Fix` The PDF report no longer fails when the downtime query errors; it falls back to the sampled data.
- `Fix` The Nmap scan worker returns proper errors for an empty (EOF) or invalid result instead of a 500 or leaked validation text.
- `Fix` The service-identity comparison respects the stored length limits, so long product or version strings no longer show a false change.
- `Fix` Zooming the combined chart keeps the downtime bands on targets without HTTP checks.
- `Fix` Hover and background tints written with invalid `rgb()` syntax now apply.
- `Fix` The slider thumb reduced-motion and forced-colors rules now apply.
- `Fix` The first dashboard paint for admins no longer overlaps the tiles.
- `Fix` The Delivery queue tile can no longer be resized below 4 of 12 columns; at 3 columns its metrics were clipped.
- `Change` The factory dashboard layout is now: the four status tiles on top, Monitor quality, Delivery queue (admins) and Alerts as one row of equal cards, and the Downtime overview across the full width below. Without the queue card, Quality and Alerts share the row. Your saved layout is not changed; Reset layout applies the new default.
- `Security` The bundled Nmap now uses the Debian PCRE2 and zlib packages, so the image scan covers them, and it no longer contains libssh2 (only used by the NSE scripts, which are not built). The remaining bundled libraries (libpcap, libdnet, liblinear) are listed with their versions in the image provenance and the release notes; the image scan cannot see them.
- `Security` The release image build now fails when its Nmap is not linked against the Debian zlib and PCRE2 (the earlier negative banner check never failed the build), when the Adoptium signing key is anything but the one expected key (exact fingerprint instead of a text search), or when the Nmap libraries of the builder and the runtime stage differ.
- `Fix` A discovery scan that expired while still unwinding no longer frees the address reserved by its successor: only the job that holds the reservation releases it, so a second scan of the same target cannot start in parallel.
- `Fix` UDP targets with a mix of open and uncertain ports (for example open plus `open|filtered`) no longer write an "up" point to the history or the PDF report: the UDP result is only recorded when it is known (all ports open, or one confirmed closed). A confirmed outage now outranks an unknown result of another check in the target status.
- `Fix` The PDF report no longer treats a configured `http://` address as HTTPS: no certificate or TLS scan is run for it, the report states "HTTP, no TLS scan" instead of a TLS section, and the cover shows only the host name (no credentials, query or fragment of the URL).
- `Fix` Imported PoP locations with invalid coordinates (NaN, infinite or outside the valid latitude/longitude range) are skipped instead of being clamped to a pole or the date line.
- `Fix` A hanging DNS resolver can no longer stall a check indefinitely: every ping, HTTP, certificate and TCP check has an overall deadline (3x its timeout plus 10 s) and then fails as an ordinary timeout. System DNS lookups use their own bounded thread pool.
- `New` Traceroute now uses Nmap through the worker instead of `mtr` (up to 30 hops, one round-trip time per hop). The Loss, Avg, Best and Worst columns are gone, silent hops show "No reply", and the Docker image no longer contains `mtr`. A host name that cannot be resolved is not traced.
- `Change` The downtime card of the target details is compact: one figure row `Downtimes` / `Data gaps`, events in at most three lines with short dates (`4 Nov · 18:57–21:50`), exact times on hover, and compact empty states. The explanatory paragraph and info icon are gone from the card (the terms are explained in the documentation); a data gap is never shown as uptime.
- `Fix` The duration in the downtime list could be one minute short of the shown times (2 h 52 m for 18:57:06 to 21:50:06); it now follows the displayed seconds.
- `Fix` The notification queue no longer freezes the web UI and monitoring while the database is busy at the first start of the spooler; its initialisation runs off the event loop.
- `Fix` The PoP data refresh no longer retries immediately when a server answers HTTP 429 with a negative or invalid `Retry-After`; it waits with the normal backoff, and the header may also be an HTTP date.
- `Fix` In the PDF report the Availability Timeline (and the Data Coverage bar) now start where the plot area of the Response Time, Ping and TCP charts starts, so bars and curves sit on the same time axis instead of the bars running left over the Y-axis labels.
- `Fix` Zooming into the availability chart no longer draws green or red lines over the axis labels or past the plot area; lines are cut at the edge of the plot.
- `Fix` Zooming into the response-time chart no longer paints downtime bands and the status strip over the axes and past the plot area; they are cut at the edge of the plot like the lines.
- `Change` The target details no longer carry explanatory sentences under the panel titles ("Latency, HTTP status codes and outages ...", "Per-check state ...") nor the "no HTTP status codes" part of the chart summary line.
- `Fix` Cards keep their rounded border at the corners when their content is collapsed (for example the check cards in the target form): the inner radius now follows the card radius.
- `Fix` The chart of a target without an HTTP check no longer shows the note "No HTTP status codes: the HTTP check is not enabled."
- `Fix` The tab bar of Edit Target (General, HTTP / TLS, Network, Alerts & SLA) no longer scrolls vertically by one pixel (ghost scroll under the tabs).
- `Fix` Native select fields show a hand cursor while operable and `not-allowed` while disabled.
- `Fix` On the About page the cards no longer push the page sideways on very narrow screens (320 px).
- `Fix` A mouse click on a Settings tab could be lost: pressing the tab scrolled the tab bar under the pointer before the button was released. The bar now scrolls to a tab only on keyboard focus.
- `Fix` "Insert link" in the mail template editor no longer opens a browser popup: the address is typed into a field of the editor, and a blocked address (for example `javascript:`) is shown as an error at that field.
- `Fix` "Reset metrics" and "Delete target" ask in the application's own dialog (naming the target and the consequence) instead of the browser's native popup.
- `Fix` The target preview suggests HTTPS only when the final settings really check an https:// URL; an `http://` override or a switched-off HTTP check no longer gets the suggestion.
- `Fix` The target page lists the configured TCP and UDP endpoints in "Network endpoints" ("Waiting for first measurement" until the first result) instead of "No port measurements yet", and the empty band between that card and the toolbar is gone.
- `Fix` The Delivery Queue Log shows one card per entry on narrow screens (with the channel name next to its icon) instead of a sideways-scrolling table.
- `Fix` The dashboard alert counts (resolved and acknowledged in 24 h) no longer read every resolved alert: a schema upgrade (version 9) adds two range indexes; with 100,000 alerts a poll drops from about 300 ms to under 1 ms. The upgrade takes about 0.1 s once.
- `New` The target wizard tile "TCP service" is now "TCP/UDP service"; its TCP and UDP port fields stand side by side (stacked on narrow screens) and the address uses the full width.
- `New` Port discovery: the wizard and target editor scan a host with Nmap (TCP and/or UDP, Quick or Extended) and add the chosen ports. Targets can monitor UDP ports; uncertain UDP results stay unknown.
- `Fix` The target wizard asks for TCP and UDP ports only once, on the address step. The checks step shows them as a summary with an "Edit ports" button instead of a second input.
- `Fix` Port discovery no longer fails with "Nmap scan worker is unavailable" when justUp! runs outside Docker (`python run.py`): the app now starts its own unprivileged Nmap worker (Nmap must be installed). If the worker is down, Scan ports is disabled with the reason shown on the form and re-enabled once it is back.
- `Fix` The alert email header shows only the logo; the "Monitoring alert" label with bell and divider is gone. Templates you saved yourself are not changed.
- `Fix` The Targets list no longer flickers on live updates: only changed cells are rewritten, so rows, status icons, focus, open groups and the search stay as they are.
- `Fix` The route map is easier to read: the route is drawn between the markers with an outline, estimated sections are dashed, the basemap is calmer, only a few place names remain and the legend names every kind of node and link.
- `Fix` Resetting the response-time chart zoom (double-click or "Reset zoom") now restores the load view of all axes, series and peaks through one shared function and ignores a late zoom-detail response.
- `New` Settings > Recipients has a search field that filters by name, email or Signal number while you type.
- `Fix` Settings > Delivery Queue shows the stored status `sent` as "Sent" (it read "Accepted by server"); the status carries no tooltip, the meaning is documented.
- `Fix` The Delivery Queue Log shows the channel as an icon only; the name stays available as tooltip and for screen readers.
- `New` The Docker image contains the latest stable Nmap release at build time. It is built from the PGP-signature-verified source for amd64 and arm64 (same version and source hash), with build data in `/usr/share/doc/nmap/PROVENANCE.txt` and the Nmap Public Source License notice in the image. See "Network monitoring with Nmap" in the documentation.
- `New` UDP and TCP ports are monitored and shown per endpoint (protocol and port, such as `TCP:53` and `UDP:53`). UDP `open` is up, `closed` is down after two confirming scans; `open|filtered`, `filtered` and failed scans are unknown, never up and never downtime. UDP scans run at most every 5 minutes.
- `Fix` Downtime no longer includes stretches without measurements: a gap between a down and a later up measurement is shown as "No measurements", unknown results as "Unknown", and neither counts as downtime or uptime.
- `Security` UDP scans run in a separate Nmap worker with only the `NET_RAW` capability; the web process gets no raw-socket rights, Docker `privileged` stays off and `no-new-privileges` stays on. Scans are admin-only and accept just a host, protocols and a scope, with no free Nmap arguments, ranges or host lists.
- `New` Settings > Recipients has a **Re-check Signal** button per recipient with a number. It asks signal-cli again instead of using the 24 h cached answer; the result replaces the status icon. Limited to 5 checks per minute, one lookup per number at a time and two at once overall.

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
