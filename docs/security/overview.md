# Security – Overview

This page describes the security model of justUp! in the target version v1.0.0.
The individual topics go deeper into [authentication](authentication.md), the
[rate limiting](rate-limiting.md), and the [best practices](best-practices.md).

## Principles

- **One session token, two transport paths.** Browser pages use an HttpOnly cookie;
  JSON API calls send the same token as a bearer. An admin can disable authentication
  in the settings (password-confirmed and logged); keep it off on reachable instances.
- **Local and standalone.** App code, fonts, base map, and most assets are served
  locally; the only external UI asset is the flag SVGs from jsDelivr in the
  traceroute table. On failure, the country code appears. Inline scripts are
  blocked by the Content Security Policy.
- **Secrets only from the environment.** No default, no key in the repository.

## Session and CSRF

- The session is an **HMAC-signed token** in an `HttpOnly` cookie with
  `SameSite=Lax`. Behind TLS, `Secure` is set. Behind a proxy, justUp!
  trusts `X-Forwarded-Proto` only if the direct peer is in
  `UPMON_TRUSTED_PROXIES`. The absolute lifetime is 1–7 days; in addition,
  a six-hour inactivity expiry applies.
- JSON API routes require `Authorization: Bearer <access_token>`. Cookie auth is
  limited to browser pages and selected self-service routes.
- **CSRF protection following the double-submit pattern** applies to unsafe
  methods on `/ui/*`, `/login`, and the cookie-protected self-service routes
  `/me/theme`, `/me/complete-required-change`, and `/me/dashboard-layout`. An
  existing `Origin` header is checked. The rest of the bearer API lies outside
  the cookie CSRF protection.

## Secrets at rest

Secrets (e.g. SMTP credentials) are stored encrypted with **Fernet/MultiFernet**.
The key comes exclusively from `SECRET_KEY` (or `SECRET_KEYS` for rotation). See
[Environment Variables](../configuration/environment.md).

## Passwords

Passwords are hashed with **argon2id** or **PBKDF2-SHA256** (at least 600,000
iterations). Login consumes constant time, even for unknown users, to prevent
user enumeration. User objects for templates never contain the password hash.

## Network and outbound checks

- Outbound checks are protected against **SSRF** (loopback, link-local, and
  metadata addresses blocked; RFC 1918 allowed).
- Subprocesses (`ping`, `mtr`, `whatweb`, `signal-cli`, `sslyze`) are invoked only
  as an argument list with `--` before positional hosts and with a hard timeout.

## Rate limiting

justUp! uses SlowAPI with a process-local limiter store. In addition, a
login lockout per IP and per username applies. Details:
[Rate Limiting](rate-limiting.md).

## Container

The application process never runs as root. The entrypoint drops privileges before
the start via `gosu`; if the tool is missing, it aborts. See
[Docker](../getting-started/docker.md).

## TLS check and dependency chain

`cryptography` is pinned to 50.0.2 (fixes CVE-2026-69247, PKCS#7 decryption).
`sslyze` 6.3.1 from PyPI limits `cryptography<47`; the upstream branch `release` raises
the limit in one line (`<51`), without a release existing. justUp! therefore ships
its own, once-built wheel `sslyze-6.3.1+justup.1-py3-none-any.whl` from
`docker/vendor/wheels/` (pure Python, identical for amd64 and arm64). The source is the
upstream commit `e064c2c8e7d251e5bc5db1a7beb21add5e12a0bc` (tag 6.3.1 plus two commits)
with exactly one patch (`docker/vendor/sslyze/patches/`: only the version number). The
requirement stays `cryptography>=43,<51`. Origin, hash, and toolchain are in
`docker/vendor/PROVENANCE.json`; the license (AGPL-3.0) is in the wheel.

The complete dependency state (Python 3.13, Linux amd64/arm64, hashes of all packages,
wheels only) is in `docker/requirements.lock`; the SSLyze entry carries the SHA-256 of the
stored wheel. Docker and CI install with `--require-hashes`,
`--only-binary=:all:` and `--find-links docker/vendor/wheels` and then check
`pip check`. A missing wheel, a wrong hash, or a wrong platform aborts the
installation; SSLyze is never built from source. For vulnerabilities there is no
Trivy exception: `.trivyignore` stays empty.

The only exception in the source scan is the misconfiguration `DS-0002` (image starts as
root), entered in `.trivyignore.yaml` only for `docker/Dockerfile` and with an expiry date.
The entrypoint needs root only for PUID/PGID and the volume permissions and starts the
application afterwards via `exec gosu justup` without privileges. It is not a global disable.

Rebuilding (controlled): `docker/vendor/sslyze/build-wheel.sh` fetches the pinned commit,
checks the commit ID, applies the patches, and builds in an isolated venv with a
hash-pinned toolchain (`build-requirements.lock`), without build isolation and without an index.
`build-wheel.sh --check` proves a byte-identical rebuild. Afterwards, run
`python tools/relock-runtime.py --write` and adjust `PROVENANCE.json`. The
stored wheel remains the authoritative artifact. Once an official `sslyze` release
allows `cryptography>=50`, switch back to a PyPI requirement and remove the wheel together with
`docker/vendor/`.

The TLS check (`app/utils/tls_scanner.py`) returns `secure`, `insecure`, or
`unknown` per check. A failed or incomplete scan appears in the PDF as
"failed" or "unknown / not tested", never as passed. The destination is resolved
once and checked against the SSRF protection; the scan connects to exactly that address.

!!! note "Accessibility"
    justUp! targets WCAG 2.2 AA (keyboard operation, visible focus, contrast,
    `prefers-reduced-motion`). Full WCAG conformance requires a manual
    check with assistive technologies and an expert review.
