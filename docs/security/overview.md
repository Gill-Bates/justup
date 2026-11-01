# Security Overview

Details: [Authentication](authentication.md), [Rate limiting](rate-limiting.md),
[Best practices](best-practices.md).

## Sessions

- A [session token](../api/authentication.md#session-token) is stored in an `HttpOnly`, `SameSite=Lax` cookie
  (`Secure` behind HTTPS). API clients send the same token as a bearer token.
- The session lasts 1-7 days (setting) and also expires after six hours of
  inactivity.
- CSRF protection applies to browser routes. See [Authentication](authentication.md).
- An admin can disable authentication; every request then runs as administrator.
  Keep it off on any reachable instance.

## Secrets and passwords

- Secrets such as SMTP credentials are stored encrypted with a key from
  `SECRET_KEY` (or `SECRET_KEYS`). The key is never stored in the repository.
- Passwords are hashed with Argon2id or PBKDF2-SHA256. Sign-in takes constant time
  for unknown users.
- The initial admin password is random, printed once per start until changed, and
  must be changed at first sign-in.

## Network

- Outbound checks block loopback, link-local, and metadata addresses. Private
  RFC 1918 addresses are allowed.
- Public IPs of traceroute hops may be sent over HTTPS to ipwho.is (and ip-api Pro with
  a key), only when GeoLite2 is not precise enough and the online fallback is on; the daily release check contacts api.github.com; flag images load from
  jsDelivr in the web UI and from flagcdn.com in PDF reports (country codes only; optional). See [Best practices](best-practices.md#external-services).
- justUp! sets no `Content-Security-Policy` header; see [Best practices](best-practices.md#content-security-policy).

## Container

The app runs as an unprivileged user; the entrypoint drops root with `gosu`. See
[Docker](../getting-started/docker.md).

## Accessibility

justUp! targets WCAG 2.2 AA. Full conformance needs manual testing with assistive
technology and expert review.
