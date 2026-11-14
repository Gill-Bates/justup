# Settings

Runtime settings are stored in the database and managed by admins in the web UI.
Every control saves automatically. Changes apply without a restart. Bootstrap
values are [environment variables](environment.md).

## Tabs

| Tab | Content |
| --- | --- |
| **General** | Display (UTC times), the Public hostname, the PDF card (PDF export switch, paper size) and the Access & Sessions card (Require Authentication, login rate limit, session lifetime). |
| **Network** | Connection quality, provider lookup, geolocation, GeoLite2 auto-update. |
| **Storage** | Statistics, purge permission, data deletion; see [Storage & retention](storage.md). |
| **Backup** | Backup and restore; see [Backup & restore](../features/backup.md). |
| **SMTP** | Email delivery. |
| **Users** | User management only; see [Users & roles](../features/users.md). |
| **Recipients** | Notification recipients. |
| **Delivery Queue** | Outbound delivery queue, statistics, and log. The in-app inbox is a separate page (`Manage > Notifications`). |
| **Signal** | signal-cli account and message templates. |

## Reference

- **Down Alert Delay**: no longer a global setting. It moved to each target; see
  [Monitoring & Checks](../features/monitoring.md). Sending `metrics_timeout_minutes` is rejected with `422`.
- **Public hostname** (`public_hostname`, General tab, empty by default): bare host
  name or IP address, no scheme, port, path or `@`. Notifications use it as
  `https://<hostname>` for links, see [Notifications](../features/notifications.md#links-in-notifications).
  The wand button fills in the address of the current browser tab. justUp! never
  derives the value from request headers. Invalid input is rejected with `422`.
- **Enable PDF Export** (`pdf_export_enabled`, General tab, PDF card, on by default; an
  installation that never stored a value keeps it on): while off, the PDF icon is missing
  on the target page and every PDF route (`/reports/...`, `/ui/reports/...`) answers
  `403` with `PDF export is disabled`. Queued report jobs are not rendered (they are
  marked failed), finished reports are not offered for download, and nothing is deleted.
  Switching it on again takes effect at once, without a restart. The **PDF Paper Size**
  (`pdf_page_size`) sits in the same card and can be changed while export is off.
- **Allow Metric Purge** (`purge_enabled`): permits "Delete all Metrics". The
  action and **Clear Alert History** require the admin password.
- **Login Rate Limit** (`rate_limit_auth`, General tab, Access & Sessions card, default `10/minute`): maximum login
  requests per client IP and window. Units: `second`, `minute`, `hour`, `day`.
- **Session Lifetime** (`session_max_days`, General tab, Access & Sessions card, 1-7, default 1): absolute lifetime
  from sign-in, regardless of activity. It applies to new sign-ins only.
- **Probe Targets** (`quality_targets`): hosts probed for connection quality, at
  least 3. A URL (`https://host:port/path`) is requested with HEAD; an entry
  without scheme must be a bare host name or IP address and is pinged, so
  `host:80` or `host/path` are rejected. Separate entries with comma, Tab, or Enter. Defaults: `https://1.1.1.1`,
  `8.8.8.8`, `9.9.9.9`, `https://www.de-cix.net`, `https://cloudflare.com`.
  **Reset to defaults** restores them. Invalid entries, fewer than 3 or more than 20
  distinct targets are not saved. A list saved before the limit existed is probed
  with its first 20 entries and logs a warning. One probe run stops after 20 s;
  unfinished targets count as failed.
- **Minimum Score** (`quality_min_score`, default 70) and **RTT Threshold**
  (`quality_rtt_threshold_ms`, default 500): see [Quality gate](../features/quality-gate.md).
- **Provider Lookup**: resolves public target IPs to ASN and ISP. With an ip-api
  Pro key (stored encrypted; leave the field untouched to keep it, use **Remove
  stored key** to clear it) that service is preferred; otherwise ipwho.is is used.
  **Lookup Cache TTL** is stored in seconds.
- **Enhanced Accuracy** (`online_geo_fallback_enabled`, Geolocation card): allows
  public hop IPs to be sent over HTTPS to ipwho.is (and ip-api Pro when a key is
  stored), but only when GeoLite2 and verified POP data are not precise enough.
  Enable it only if accurate measurement results matter to you and you agree to
  have public IP addresses shared with third parties.
  Private addresses never are sent. A new installation starts with it off; the
  choice you make is stored and kept. Pre-release databases are not carried over (see
  [Database baseline](../development/database-maintenance.md#schema-baseline-and-versioning)):
  set the switch again after a reset. If the setting cannot be read, no online
  request is made. The ASN is read from the local GeoLite2 ASN database first; this
  switch only gates the third-party lookup that fills what the local data lacks.
  Provider Lookup (ASN/ISP) is independent. See
  [Traceroute & maps](../features/traceroute.md#enhanced-accuracy-online-geolocation).
- **Public outbound IPs** (read-only card, no setting): shows the IPv4 and IPv6 address that
  external services see for this server. See
  [Public outbound IPs](../features/network-monitoring.md#public-outbound-ips).
- **GeoLite DB auto-update** (`geoip_auto_update`): at start, checks for newer GeoLite2
  City and GeoLite2 ASN databases (P3TERX GitHub mirror, no licence key). Both files
  are handled as one unit with the same updater: they are checked together, each is
  verified and replaced atomically, and a failed download of one keeps its old file
  and does not stop the other or the start. With the switch off, a missing file is
  still downloaded once. An existing installation gets the ASN file after a restart
  (so the new Python code loads); there is no separate update button or schedule.
  The ASN file is shown on the About page and needs no extra setting.
- **Require Authentication** (General tab, Access & Sessions card, on by default): while on,
  every access needs sign-in. Switching it off needs the admin password; every page and API
  call then runs as administrator, the UI shows "Your instance is publicly accessible!",
  and a CRITICAL log line is written. Switching it on again also asks for the password.
  Use it off only on a network you fully trust. The API still exposes the stored value as
  `auth_disabled` (`true` means Require Authentication is off); an installation that never
  stored it requires authentication, and an existing installation keeps its state.

Credentials such as the SMTP password are stored encrypted with a key derived from
`SECRET_KEY`. The UI never shows them: a stored secret appears as a fixed mask of dots (the
length does not match the secret). Leave the field untouched to keep it, type to replace it, or
use **Remove stored password** / **Remove stored key** to delete it.
