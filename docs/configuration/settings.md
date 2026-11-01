# Settings

Runtime settings are stored in the database and managed by admins in the web UI.
Every control saves automatically. Changes apply without a restart. Bootstrap
values are [environment variables](environment.md).

## Tabs

- **General**: display (UTC times, PDF paper size) and the Down Alert Delay.
- **Network**: connection quality, provider lookup, geolocation, GeoLite2 auto-update.
- **Storage**: statistics, purge permission, data deletion; see [Storage & retention](storage.md).
- **Backup**: backup and restore; see [Backup & restore](../features/backup.md).
- **SMTP**: email delivery.
- **Users**: access and sessions (authentication switch, login rate limit,
  session lifetime) and accounts; see [Users & roles](../features/users.md).
- **Recipients**: notification recipients.
- **Delivery Queue**: outbound delivery queue, statistics, and log. The in-app
  inbox is a separate page (`Manage > Notifications`).
- **Signal**: signal-cli account and message templates.

## Reference

- **Down Alert Delay** (`metrics_timeout_minutes`, 1-10 min, default 5): how long
  a failure must last before a DOWN alert is raised.
- **Allow Metric Purge** (`purge_enabled`): permits "Delete all Metrics". The
  action and **Clear Alert History** require the admin password.
- **Login Rate Limit** (`rate_limit_auth`, default `10/minute`): maximum login
  requests per client IP and window. Units: `second`, `minute`, `hour`, `day`.
- **Session Lifetime** (`session_max_days`, 1-7, default 1): absolute lifetime
  from sign-in. It applies to new sign-ins only.
- **Probe Targets** (`quality_targets`): hosts probed for connection quality, at
  least 3. Separate entries with comma, Tab, or Enter. Defaults: `https://1.1.1.1`,
  `8.8.8.8`, `9.9.9.9`, `https://www.de-cix.net`, `https://cloudflare.com`.
  **Reset to defaults** restores them. Invalid entries and fewer than 3 targets
  are not saved.
- **Minimum Score** (`quality_min_score`, default 70) and **RTT Threshold**
  (`quality_rtt_threshold_ms`, default 500): see [Quality gate](../features/quality-gate.md).
- **Provider Lookup**: resolves public target IPs to ASN and ISP. With an ip-api
  Pro key (stored encrypted; leave the field untouched to keep it, use **Remove
  stored key** to clear it) that service is preferred; otherwise ipwho.is is used.
  **Lookup Cache TTL** is stored in seconds.
- **Online fallback** (`online_geo_fallback_enabled`, Geolocation card): allows
  public hop IPs to be sent over HTTPS to ipwho.is (and ip-api Pro when a key is
  stored), but only when GeoLite2 and verified POP data are not precise enough.
  Private addresses never are sent. A new installation starts with it on; switching
  it off is stored and kept. Pre-release databases are not carried over (see
  [Database baseline](../development/database-maintenance.md#schema-baseline-and-versioning)):
  set the switch again after a reset. If the setting cannot be read, no online
  request is made. Provider Lookup (ASN/ISP) is independent. See
  [Traceroute & maps](../features/traceroute.md#online-fallback).
- **GeoLite DB auto-update** (`geoip_auto_update`): checks for a newer MaxMind
  GeoLite2 database at start.
- **Disable Authentication** (Users tab): every page and API call then runs as
  administrator. It needs the admin password and writes a CRITICAL log line. Use
  it only on a network you fully trust.

Credentials such as the SMTP password are stored encrypted with a key derived from
`SECRET_KEY`.
