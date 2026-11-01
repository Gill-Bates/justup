# Authentication

justUp! uses one [session token](../api/authentication.md#session-token). Browser pages use it as the `HttpOnly`
cookie `token`; JSON API calls send it as a bearer token. There are no API keys.
See [API authentication](../api/authentication.md).

## Session

- `POST /login` returns `access_token` and `expires_at` and sets the cookie.
- The cookie is `SameSite=Lax`, and `Secure` behind HTTPS.
- Lifetime: `session_max_days` (1-7, default 1) plus a six-hour inactivity expiry.
- Expired, revoked, or inactive sessions are rejected. While a password change is
  pending, API calls answer HTTP 428.
- Cookie authentication works for browser pages and the self-service routes
  `/me/theme` and `/me/complete-required-change`. Other API routes accept only the
  bearer token.

## CSRF

Unsafe requests to `/ui/*`, `/login`, `/me/theme`, `/me/complete-required-change`,
and `/me/dashboard-layout` need the `csrf_token` cookie value in the
`X-CSRF-Token` header, and a matching `Origin` if present. Load `/ui/login` first to
receive the cookie. Other bearer API routes are not affected.

## Sign-in protection

- A hidden honeypot field rejects simple bots.
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
is shown once; reset a lost one with `justup-cli`.
