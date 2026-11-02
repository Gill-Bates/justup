# Authentication

justUp! uses one [session token](../api/authentication.md#session-token). The browser UI uses it as the `HttpOnly`
cookie (`token`, `__Host-token` over HTTPS) and keeps it out of `localStorage`; JSON API clients send it as a bearer token. There are no API keys.
See [API authentication](../api/authentication.md).

## Session

- `POST /login` returns `access_token` and `expires_at` and sets the cookie.
- The cookie is `SameSite=Lax`, path `/`. Over HTTPS it is named `__Host-token` and `Secure`; over plain HTTP it is named `token` without the prefix, so HTTP installations keep working. HTTPS is detected as for the other cookies (`X-Forwarded-Proto` only from `UPMON_TRUSTED_PROXIES`).
- Lifetime: `session_max_days` (1-7, default 1), counted from sign-in; no separate inactivity expiry.
- Expired, revoked, or inactive sessions are rejected. While a password change is
  pending, API calls answer HTTP 428.
- Cookie authentication works on all routes the browser uses, if no `Authorization` header is sent. A present but invalid `Authorization` header is never replaced by the cookie (`401`).

## CSRF

Unsafe requests to `/ui/*`, `/login`, `/logout`, `/me/theme`, `/me/complete-required-change`,
and `/me/dashboard-layout`, and every unsafe request authenticated by the session cookie, need the `csrf_token` cookie value in the
`X-CSRF-Token` header. Cookie-authenticated requests need a matching `Origin` header as well. Load `/ui/login` first to
receive the cookie. Bearer API requests are not affected.

## Sign-in protection

- A hidden honeypot field rejects simple bots: a sign-in that fills it answers
  `401 Invalid credentials` without checking the password. A public client IP is
  then added to the same exponential lockout as a failed sign-in; private (RFC 1918)
  IPs are logged but never locked. It has no configuration.
- Failed sign-ins lock out IP and username with exponential delay, up to one hour.
- Sign-in takes constant time for unknown users.

## First admin account

On first start, justUp! prints a random one-time password to the container output.
It is not written to the log handlers, the database, or any API response. Until the
password is changed, browser pages redirect to `/ui/change-password` and API calls
answer HTTP 428 (`Password change required`).

To show the password again on later starts, justUp! keeps it in
`<data dir>/.bootstrap-admin-credentials` (mode `0600`, outside the backed-up
directories) and deletes the file when the password changes. Earlier output stays
in the container log until it is rotated. If the file cannot be written, the password
is shown once; reset a lost one with [`justup-cli`](../features/users.md#reset-a-password).
