# Best Practices

## Secrets

- Keep `SECRET_KEY` only in the environment, never in the repository. If you lose
  it, stored secrets cannot be read.
- Rotate with `SECRET_KEYS` (first = primary); see
  [Environment variables](../configuration/environment.md).
- Store backups and keys separately. A backup needs the matching key.
- Backups are HMAC-signed but not encrypted. Keep them access-protected and encrypt
  them (`age`, GPG, disk encryption) before external storage; see
  [Backup & Restore](../features/backup.md#confidentiality).

!!! danger "Rotate leaked secrets"
    If a `SECRET_KEY` or admin password was ever committed, rotate it and clean the
    Git history.

## HTTPS

Terminate TLS at a reverse proxy and enter its direct address in
`TRUSTED_PROXIES`. See [Reverse proxy](../configuration/reverse-proxy.md).

## Users

- Give admin rights sparingly.
- Change the initial admin password at once.
- Use **Disable Authentication** only on an isolated network.

## External services

justUp! contacts these services:

| Service | Purpose | Can be disabled |
| --- | --- | --- |
| ipwho.is, ip-api Pro (with key) | Hop geolocation fallback when GeoLite2 is not precise enough (public IPs only, HTTPS) | Yes (`online_geo_fallback_enabled`) |
| Provider lookup | ASN and ISP of target IPs | Yes (Settings > Network) |
| icanhazip.com, ipify.org | Public outbound IPs card (Settings > Network): the services see the server's public address; run on tab open and Refresh, not stored | No (no request while the Network tab is closed) |
| github.com (P3TERX GeoLite.mmdb mirror) | GeoLite2 database download | Yes (`geoip_auto_update`) |
| api.github.com | Daily release check | No |
| jsDelivr | Country flag images in the web UI | No (falls back to country codes) |
| flagcdn.com | Country flag images in PDF reports (optional, PNG over HTTPS) | No (falls back to the country code) |

PDF reports request only `https://flagcdn.com/48x36/<code>.png` for the countries shown. flagcdn.com sees the
server's public IP and which countries are requested, never target or hop addresses. Flags are not cached
and are fetched again for every report; if the service is unreachable the report shows the country code only.

Block outbound access at the network level if a service is not acceptable.
The GeoLite2 download needs no MaxMind account or license key; check the GeoLite2 license terms for your use.

## Port scans

Only administrators can start Nmap scans. Scan hosts you are authorized to scan; scans can
trigger alerts at the target. See
[Network monitoring with Nmap](../features/network-monitoring.md#security-and-operations).

## Updates

Pull the current image regularly (`docker compose pull`) and read the
`CHANGELOG.md` in the [repository](https://github.com/Gill-Bates/justup) for breaking
changes before upgrading.

## Content Security Policy

justUp! enforces only `Content-Security-Policy: frame-ancestors 'self'` (plus
`X-Frame-Options: SAMEORIGIN`), because its pages use inline scripts and styles. A
second, report-only policy lists the sources the pages actually use; violations are
logged as `CSP_VIOLATION`. Do not enforce a stricter policy at the proxy without
testing every page. See [Reverse proxy](../configuration/reverse-proxy.md#response-security-headers).
