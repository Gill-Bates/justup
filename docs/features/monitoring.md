# Monitoring & Checks

A **target** is a monitored host with a name, optional group, active flag,
interval (5-86,400 s), retention (1-3650 days), maximum retries (0-10), and
individually switchable checks.

A group can only be deleted when no target uses it (HTTP `409` otherwise). Targets are
never moved to "Ungrouped" implicitly; reassign or delete them first.

## Creating targets

A four-step wizard suggests checks by profile and shows a summary before saving.

| Profile | Suggested checks |
| --- | --- |
| Website | HTTP GET, success codes 200-399; TLS expiry for HTTPS |
| API / health endpoint | HTTP GET, success codes 200-299; TLS expiry for HTTPS |
| Server / host | ICMP ping |
| TCP/UDP service | TCP or UDP to the ports you enter |
| Custom | Any combination and advanced settings |

Defaults: 60 s interval, two retries, 365 days retention, auto-close. You can
change them before creation. A domain without scheme suggests HTTPS; an explicit
`http://` stays HTTP. The summary does not contact the target.
In the checks step, settings appear only for active checks: the TCP port field with TCP, URL, method and success codes with HTTP, and the TLS option "Ignore TLS certificate errors" with HTTP or certificate expiry. Interval, retries, retention and auto-close are always shown. The summary lists only active checks.

Exact duplicates are blocked. Different services on the same host are allowed;
partially overlapping checks show a warning. **Edit** opens the full editor.

## Check types

=== "Ping"
    ICMP via system `ping`. Result: reachable and latency.

=== "HTTP(S)"
    GET or HEAD, optional basic auth, success codes (default `200-399`), port
    override, optional certificate ignoring. Redirects are not followed. Result:
    reachable, status code, latency.

=== "TCP"
    Several ports per target; status and latency per port.

=== "Certificate"
    Remaining days of the TLS certificate. A configured certificate host and port always win.
    Without them the host comes from the HTTP URL (then the target host) and the port from
    the HTTP URL, then the HTTP port override, then 443 (80 for an `http` URL).

## Host rules

- Hosts must be valid hostnames or IP addresses.
- Checks block loopback, link-local, and metadata addresses (SSRF protection).
  Private RFC 1918 addresses are allowed.
- DNS is resolved once; the check connects to exactly that address.

## Scheduling

Checks run aligned to the interval with a small per-target jitter. Failed checks
are retried with backoff. A slow check does not delay other targets. Changes
apply without a restart.

## Searching targets

The search field on the Targets page filters while you type. It matches a part of the
name, host, HTTP URL, TCP port or group name, ignoring case and accents. Groups without
a hit disappear, groups with hits open while you search, and the badge shows hits
against the total (`2 / 5`). Clearing the field (the clear button or `Esc`) restores the
groups as they were. Without a match the page says `No targets match "..."`.

## Status and SLA

Each target has a status per service and an overall status (`up`, `down`,
`degraded`, `unknown`), plus uptime for 24 h and 30 days. Uptime follows the overall
status: a check cycle counts as up only if every enabled check (HTTP, ping, TCP)
is up. Admins can reset a target's measurements and send a test alert.

SLA targets (availability %, HTTP response time, ping latency) are set per target.
They affect charts and reports only and never raise alerts. Without an
availability target, charts assume 99.9 %. See [Reports](reports.md).

## Dashboard

The dashboard shows online, offline, disabled, and total targets, the connection
quality widget, and the **Downtime Overview (24h)**. It refreshes every 30 s and
has a refresh button.

On desktop, **Edit dashboard** moves, resizes, and hides widgets per user. **Done**
saves the layout; **Reset layout** restores the default. Keyboard: arrow keys move a
focused widget handle, Shift+arrow keys resize.

## Live updates

Open pages receive change hints over a Server-Sent Events stream (`/ui/events`)
and reload the affected parts. If the stream fails, the page polls every 15 s. A
**Server offline** overlay appears only when `/health` fails twice in a row.
Proxy requirements: [Reverse proxy](../configuration/reverse-proxy.md).

Retention and rollups: [Storage & retention](../configuration/storage.md).
