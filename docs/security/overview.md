# Security Overview

Details: [Authentication](authentication.md), [Rate limiting](rate-limiting.md),
[Best practices](best-practices.md).

## Sessions

- A [session token](../api/authentication.md#session-token) is stored in an `HttpOnly`, `SameSite=Lax` cookie
  (`Secure` behind HTTPS). API clients send the same token as a bearer token.
- The session lasts the configured 1-7 days (setting `session_max_days`, default 1),
  counted from sign-in with no separate inactivity timeout. See
  [Authentication](authentication.md).
- CSRF protection applies to browser routes. See [Authentication](authentication.md).
- An admin can disable authentication; every request then runs as administrator.
  Keep it off on any reachable instance.

## Secrets and passwords

- Secrets such as SMTP credentials are stored encrypted with a key from
  `SECRET_KEY` (or `SECRET_KEYS`). The key is never stored in the repository.
- Passwords are hashed with PBKDF2-HMAC-SHA256. Sign-in takes constant time
  for unknown users.
- The initial admin password is random, printed once per start until changed, and
  must be changed at first sign-in.

## Network

- Outbound checks block loopback, link-local, and metadata addresses. Private
  RFC 1918 addresses are allowed.
- Public IPs of traceroute hops may be sent over HTTPS to ipwho.is (and ip-api Pro with
  a key), only when GeoLite2 is not precise enough and the online fallback is on; the daily release check contacts api.github.com; flag images load from
  jsDelivr in the web UI and from flagcdn.com in PDF reports (country codes only; optional). See [Best practices](best-practices.md#external-services).
- Settings > Network > Public outbound IPs connects directly (no proxy) to icanhazip.com and ipify.org over HTTPS, which sees the server's public address; nothing is stored. See [Network monitoring](../features/network-monitoring.md#public-outbound-ips).
- justUp! enforces only `Content-Security-Policy: frame-ancestors 'self'` and sends a report-only policy for the rest; see [Best practices](best-practices.md#content-security-policy).

## Container

The app runs as an unprivileged user; the entrypoint drops root with `gosu`. See
[Docker](../getting-started/docker.md).

Release images are scanned with Trivy. Nmap and the libraries compiled into it are invisible to Trivy, so the release also checks Nmap and libpcap against NVD and blocks on CRITICAL CVEs; libdnet and liblinear have no NVD entry and are not checked. Details and limits: [Release image](../development/release-image.md#nvd-gate-for-the-compiled-components).

## Accessibility

justUp! targets WCAG 2.2 AA. Full conformance needs manual testing with assistive
technology and expert review.
