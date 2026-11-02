# API Authentication

The interface and the JSON API use the same session token, but
on different transport paths:

- The browser UI uses the HttpOnly session cookie (`token`, or `__Host-token` over HTTPS) together with the CSRF header. It stores no token in `localStorage` and sends no bearer header.
- External API clients use `Authorization: Bearer <access_token>`.
- There is no permanent API key system. For automation, the
  same token as at login is used.

## Session token

The session token is a random, opaque string (`secrets.token_urlsafe(32)`), not a
signed or self-describing token. justUp! stores only an HMAC-SHA256 hash of it in
SQLite, keyed with a key derived from `SECRET_KEY`, so a database copy does not
reveal usable tokens. The CSRF token is also random and is checked by comparison
with the `X-CSRF-Token` header.

## Request a token

First fetch the login page. This gives you the CSRF cookie that the
`POST /login` endpoint requires:

```bash
curl -c cookies.txt -b cookies.txt http://localhost:8000/ui/login
```

Read `csrf_token` from `cookies.txt` and send it as a header:

```bash
curl -c cookies.txt -b cookies.txt \
  -H 'Content-Type: application/json' \
  -H 'X-CSRF-Token: <csrf-token-from-cookies.txt>' \
  -d '{"username":"admin","password":"<password>"}' \
  http://localhost:8000/login
```

The response contains `access_token` and `expires_at`. For a bootstrap password
that has not yet been changed (it is printed at every start until then), it may additionally contain
`must_change_password: true`. The browser also receives the HttpOnly session cookie (`token`; `__Host-token` when the request is HTTPS).

## API calls

Send the `access_token` as a bearer token on API calls:

```bash
curl -H 'Authorization: Bearer <access-token>' \
  http://localhost:8000/targets
```

Example for the target preview in the wizard:

```bash
curl -X POST http://localhost:8000/targets/preview \
  -H 'Authorization: Bearer <access-token>' \
  -H 'Content-Type: application/json' \
  -d '{"profile":"website","address":"https://example.com"}'
```

An `Authorization` header always takes precedence: an invalid, expired or revoked bearer token
answers `401` even when a valid session cookie is sent too. A cookie is used only when no
`Authorization` header is present.

Every unsafe request (`POST`, `PUT`, `PATCH`, `DELETE`) that is authenticated by the cookie needs the
`X-CSRF-Token` header (value of the `csrf_token` cookie) and a matching `Origin` header; otherwise it
answers `403`. `/ui/*`, `/login`, `/me/theme`, `/me/complete-required-change`, `/me/dashboard-layout` and
`/logout` require the CSRF header in every case. Bearer requests do not need it. The `csrf_token` cookie
lives as long as the longest session (7 days) and is renewed after each checked request.

## Token lifetime

The lifetime is determined by `session_max_days` (1–7 days, default: 1 day) and
counts from sign-in, with or without activity. There is no shorter inactivity
expiry: the cookie `max_age` and the server-side token expiry both equal the
configured lifetime. Expired or revoked tokens return `401`. A pending mandatory
password change blocks other API calls with `428`.
