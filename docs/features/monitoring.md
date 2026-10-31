# Monitoring & Checks

A **target** is a monitored host. Each target has a name, an optional group,
an active flag, an interval (5–86,400 s), a retention (1–3650 days),
maximum retries (0–10), and independently toggleable checks.

## Creating targets

New targets are created through a four-step wizard. The profiles suggest checks
server-side; before saving, a preview shows the host, URL or ports, settings,
group, and alert recipients.

| Profile | Suggested checks |
| --- | --- |
| Website | HTTP GET, success codes 200–399; TLS expiry for HTTPS |
| API / health endpoint | HTTP GET, success codes 200–299; TLS expiry for HTTPS |
| Server / host | ICMP ping |
| TCP service | TCP to the explicitly chosen ports |
| Custom | Combinable checks and advanced settings |

All profiles start with a 60-second interval, two retries, 365 days of retention,
and auto-close. The values can be changed before creation. Ping is not
automatically enabled for websites. A domain without a scheme suggests HTTPS;
the scheme, port, path, and query of a full URL are preserved.

The server-side duplicate check blocks identical monitoring endpoints.
Different services on the same host remain allowed; partially overlapping checks
appear as a warning. When editing, **Edit** leads directly to the full editor and
does not apply new profile suggestions.

The preview reads existing targets from the database and does not connect to the
entered destination. Network checks begin only once you start monitoring.

## Check types

=== "Ping"

    ICMP via the system `ping` with a hard process timeout. Result: reachable
    (yes/no) and latency.

=== "HTTP(S)"

    GET or HEAD, optional basic auth, configurable success codes
    (default `200-399`), port override, and optional certificate ignoring.
    Redirects are **not** followed automatically. Result: reachable,
    status code, latency.

=== "TCP"

    Multiple ports per target; status and latency are captured per port.

=== "Certificate"

    Remaining days until the TLS certificate expires. Host and port can be
    derived from the HTTP URL.

## Host validation and SSRF protection

- Hosts must be validated as a hostname (RFC 1123) or as an IP literal.
  Free text is not allowed.
- Outbound checks are protected against SSRF: loopback, link-local, and
  metadata addresses are blocked. RFC 1918 addresses remain allowed, because
  internal monitoring is an explicit use case.
- DNS resolution happens once, and exactly that address is used for the check and
  the connection.

!!! info "Subprocesses"
    External tools (`ping`, `mtr`) are invoked exclusively as an argument list,
    with `--` before the positional host and with a hard timeout. No shell is
    used.

## Scheduler

- Checks run aligned to the interval, with stable jitter per target (0–10 s)
  and retries with exponential backoff (max. 30 s).
- Each target is scheduled independently – a slow check must not delay other
  targets. Concurrency is bounded by a global limit.
- Configuration changes are applied without a restart (at the latest after 30 s
  or immediately via a signal from the API).

## Measurement data

Captured metrics include `ping_up`, `ping_ms`, `http_up`, `http_ms`,
`http_status`, `tcp_up`, `tcp_ms`, `tcp_up_<port>`, `tcp_ms_<port>`, and
`cert_days_left`.

The store compacts the data into rollups:

| Resolution | Retention |
| --- | --- |
| raw | 30 days |
| 5-min median | 180 days |
| 1-h median | per-target retention (max. 3650 days) |

Binary metrics are compacted using the minimum instead of the median;
`http_status` is not compacted. The per-target retention caps every resolution.
Queries select the resolution automatically by time range (≤ 30 days raw,
≤ 180 days 5 min, otherwise 1 h). Details: [Storage & retention](../configuration/storage.md).

## SLA targets

Each target can carry its own SLA targets (availability %, HTTP response time,
ping latency) in the target editor. They are per target; there is no global SLA
setting. They affect reports and charts only and never raise alerts. Without a
configured availability target, the target detail charts assume 99.9 %. See [Reports](reports.md).

## Status

Per target there is a current status per service and an overall status
(`up`, `down`, `degraded`, `unknown`) as well as the uptime over 24 h and 30
days. An admin can reset a target's measurement data and trigger a test alert.

## Dashboard

The dashboard shows online, offline, disabled, and total target counts, the
connection-quality widget, and a **Downtime Overview (24h)**. The downtime
table distinguishes HTTP, ping, and TCP failures; when no measurements exist,
it shows a separate empty state instead of claiming that all systems are up.
The dashboard refreshes every 30 seconds and also has a manual refresh button.
Status and list views additionally update from the live event channel (below).

On desktop, **Edit dashboard** lets each user move or resize widgets and choose
which ones to show. **Done** saves the layout to that user's account; **Reset
layout** restores the default arrangement. Focus a widget handle and use arrow
keys to move it or Shift+arrow keys to resize it.

## Live updates

Each open browser tab keeps one Server-Sent Events connection (`/ui/events`). It
carries only hints that something changed (target status and measurements, alerts,
notifications, target changes); the page then reloads the affected parts through
the normal endpoints. The server sends a heartbeat every 15 s and re-checks the
session every 10 s, so a revoked or expired session ends the stream and the browser
returns to the sign-in page.

If the stream drops or stays silent for 45 s, the browser reconnects with backoff.
After repeated failures it polls every 15 s instead; a full resync also runs every
5 minutes. The **Server offline** overlay appears only when two consecutive
requests to `/health` fail, not merely because the stream is blocked. Proxy
requirements: [Reverse proxy](../configuration/reverse-proxy.md).
