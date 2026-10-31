# Authentication

justUp! uses a signed session token. Browser pages use it as the HttpOnly cookie
`token`; JSON API calls send it as a bearer token. There is no separate API key.
An admin can disable authentication in *Settings > Users*; every request then runs
as an administrator. Leave it off on any reachable instance.

## Session token

- `POST /login` checks username and password, returns `access_token` and
  `expires_at`, and sets the HttpOnly cookie `token`.
- JSON API calls use `Authorization: Bearer <access_token>`.
- The cookie is `SameSite=Lax`; behind HTTPS, `Secure` is set.
- The absolute lifetime equals `session_max_days` (1–7 days, default 1 day).
  In addition, the server enforces a sliding expiry after six hours of
  inactivity.
- Expired, revoked, or inactive sessions are rejected. A token is blocked with
  HTTP 428 when a password change is required.

Details on login and API calls are in the
[API authentication](../api/authentication.md).

## Cookie and bearer authentication

Cookie authentication is limited to browser pages and the self-service routes
`/me/theme` and `/me/complete-required-change`. The other API routes,
including `/me/dashboard-layout`, accept exclusively the bearer token;
an existing browser cookie is no API authentication substitute there.

## CSRF protection

The double-submit protection applies to unsafe methods on `/ui/*`, `/login`, and
`/me/theme`, `/me/complete-required-change`, and `/me/dashboard-layout`:

1. The `csrf_token` cookie must additionally be sent in the `X-CSRF-Token`
   header.
2. An existing `Origin` header must match the target host and the connection.
3. The token is rotated after a successful protected write.

The other JSON API routes use bearer authentication and lie outside the
cookie CSRF protection. The login itself is CSRF-protected; first load
`/ui/login` so that the browser receives the CSRF cookie.

## Login and lockout

- An invisible honeypot field hinders automated login attempts.
- Failed attempts lead to a lockout per IP and username with exponential
  backoff up to a maximum of one hour.
- The response time is constant even for unknown usernames, to hinder
  user enumeration.
- Passwords are hashed with Argon2id or PBKDF2-SHA256 (at least 600,000 iterations);
  password hashes never reach templates.

## First admin account

On the first start, justUp! generates a random initial password and prints it to the
console. It is not written to the log handlers, the database, or any API response. The first
sign-in must change the password; until then, the bootstrap gate blocks other requests with
HTTP 423.

To show the password again on every later start until it was changed, the plaintext is kept in
`<data dir>/.bootstrap-admin-credentials` (mode `0600`, service user only, outside the backed-up
`sqlite`, `tsdb` and `pdf_reports` directories). The file is deleted when the password changes
(UI, API or `justup-cli`) and is ignored and removed whenever it no longer matches the account's
password hash, is not private, or is a symbolic link. Earlier console banners remain in the
container log history until it is rotated. If the file could not be written, the password is shown only once; reset a lost one with
`justup-cli`.
