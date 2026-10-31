# API Authentication

The interface and the JSON API use the same signed session token, but
on different transport paths:

- Browser pages (`/`, `/ui/*`) use the HttpOnly cookie `token`.
- JSON API calls use `Authorization: Bearer <access_token>`.
- There is no permanent API key system. For automation, the
  same token as at login is used.

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
`must_change_password: true`. The browser also receives an HttpOnly `token` cookie.

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

Most writing API routes require a bearer token but no
CSRF header: they do not accept cookie authentication. CSRF-protected are
`/ui/*`, `/login`, `/me/theme`, `/me/complete-required-change`, and
`/me/dashboard-layout`. `/me/theme` and `/me/complete-required-change` allow
cookie authentication; `/me/dashboard-layout` requires a bearer token,
but additionally the CSRF header. `POST /login` is also CSRF-protected.

## Token lifetime

The absolute lifetime is determined by `session_max_days` (1–7 days, default: 1 day).
In addition, a sliding expiry after six hours of inactivity applies.
Expired or revoked tokens return `401`. A pending mandatory
password change blocks other API calls with `428`.
