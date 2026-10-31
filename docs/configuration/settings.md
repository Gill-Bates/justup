# Settings

In addition to [environment variables](environment.md), justUp! has runtime
settings stored in the **SQLite database** (`settings` table) and managed in the
web UI. They survive restarts and do not require a container restart.

## Server configuration and runtime settings

| | Server configuration | Runtime settings |
| --- | --- | --- |
| Source | Environment variables | `settings` table |
| Examples | `SECRET_KEY`, `JUSTUP_PORT`, `LOG_LEVEL` | SMTP credentials, Signal account, thresholds |
| Change | Restart required | Managed in the UI |
| Owner | Operations/deployment | Administrator |

## Settings areas

Every control saves automatically (`PATCH /settings`); there are no save buttons.
The nine tabs form one horizontally scrollable strip on every screen size.

- **General** — display (UTC times, PDF paper size) and alerting delay.
- **Network** — connection quality (probe targets, minimum score, RTT threshold),
  provider lookup, geolocation, and GeoLite auto-update. The POP location data
  refreshes itself and has no settings; its state is shown under
  `GET /api/admin/pop/status`.
- **Storage** — statistics, purge permission, and data deletion; see [Storage & retention](storage.md).
- **Backup** — backup and restore; see [Backup & restore](../features/backup.md).
- **SMTP** — email delivery.
- **Users** — access & sessions (authentication switch, login rate limit, session
  lifetime) and accounts; see [Users & roles](../features/users.md).
- **Recipients** — notification recipients.
- **Delivery Queue** — spooler queue, statistics, and log. This is the outbound
  delivery log, not the in-app notification page (`/ui/notifications`).
- **Signal** — signal-cli account and message templates.

### Setting reference

Short field labels in the UI carry units and placeholders; details are here.

- **Down Alert Delay** (`metrics_timeout_minutes`, 1-10 min) is how long a service
  failure must continue before a DOWN alert is raised. A successful check resolves
  the alert.
- Metric downsampling has no setting: rollups always run (see
  [Storage & retention](storage.md)). `downsample_enabled` was removed from the
  settings API; a stored legacy value is deleted at the next start and a client that
  still sends it receives a 422.
- **Allow Metric Purge** (`purge_enabled`) permits the "Delete all Metrics" action,
  which still requires the administrator's password. **Clear Alert History** deletes
  the stored alert history and also requires the password.
- **Login Rate Limit** (`rate_limit_auth`) is the maximum number of login/auth
  requests per client IP and window, for example `10/minute` (units: second, minute,
  hour, day). It takes effect after the runtime settings cache refreshes.
- **Session Lifetime** (`session_max_days`, 1-7 days) is the absolute lifetime from
  sign-in. Changes apply to new logins.
- **Probe Targets** (`quality_targets`) lists the hosts or URLs probed for
  connection quality (at least 3). Add one with comma, Tab or Enter (pasted comma- or
  line-separated lists work too), remove it with its x button or with Backspace in the
  empty field. Without a custom list the built-in defaults are shown: `https://1.1.1.1`,
  `8.8.8.8`, `9.9.9.9`, `https://www.de-cix.net`, `https://cloudflare.com`. An
  unchanged default list is stored as empty, so it keeps following the defaults;
  **Reset to defaults** restores it. Invalid entries are highlighted and, like fewer
  than 3 targets, are not saved until fixed.
- **Minimum Score** (`quality_min_score`) marks connectivity as degraded when at
  least two-thirds of probes respond but the smoothed score falls below this
  threshold. Target checks pause while the quality gate is degraded.
- **RTT Threshold** (`quality_rtt_threshold_ms`) is the round-trip time at which the
  latency part of the score reaches zero.
- **Lookup Cache TTL** is stored in seconds; the UI shows a readable duration.
- **Provider Lookup** resolves public target IPs to ASN, ISP, and a rough location.
  With an ip-api Pro key it tries that HTTPS service first, then ipwho.is over
  HTTPS if the service fails or lacks requested fields. Without a key it uses
  ipwho.is. A local GeoLite2 location with a small accuracy radius takes
  precedence over an unmeasured online point. The PDF identifies the source and
  displays the radius when the source provides one. Provider lookup can be
  disabled separately from traceroute geolocation.
- **GeoLite DB auto-update** checks for a newer MaxMind GeoLite2 database on start.
- **Disable Authentication** (Users tab, `POST /settings/auth/toggle`) makes every
  page and API call run as an administrator. It requires the admin password, is
  rate-limited, and writes a CRITICAL log line with the user and client IP. Leave it
  off unless the instance is reachable only from a trusted network.
- **Online Geo Lookup** sends public hop IP addresses over HTTPS only when enabled.
  Both switches show a one-line warning in the UI. The former insecure ip-api HTTP
  setting is no longer accepted.
- A stored ip-api key remains unchanged if the field is untouched. Entering a new
  value replaces it; the separate **Remove stored key** action clears it.

!!! info "Secrets are encrypted at rest"
    Credentials in settings (for example, the SMTP password) are encrypted with
    Fernet/MultiFernet. The key is derived from `SECRET_KEY`; see
    [Security overview](../security/overview.md).
