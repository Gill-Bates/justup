# Rate Limiting

justUp! limits request rates per client with a small built-in, in-memory sliding-window
limiter (`app/utils/rate_limit.py`). It has no external dependency, no Redis, and no
background service. Exceeding a limit answers HTTP `429` with a `Retry-After` header.

## How it works

- Every rate-limited route has its own counter. Two routes with the same limit string
  count separately.
- A limit is a FastAPI dependency (`dependencies=[Depends(rate_limit("5/minute"))]`). It
  runs **before** authentication and body validation, so unauthenticated or invalid
  requests count against the client's budget and cannot bypass it. The `429` also happens
  before the handler body, so no expensive work (backup, PDF, mail, Signal) starts.
- Limits use `N/second|minute|hour|day`. A malformed limit fails at startup.
- Rejected requests are not recorded, so a client that backs off is admitted again as soon as
  enough earlier requests have left the window.

## Client identity

The bucket key is the client IP from `resolve_trusted_client_ip`: `X-Forwarded-For`,
`X-Real-IP`, or `Forwarded` are honoured only when the direct sender is in
`TRUSTED_PROXIES`; see [Reverse proxy](../configuration/reverse-proxy.md). Forged
headers from other peers create no new buckets.

- IPv6 clients share one counter per `/64`; IPv4-mapped IPv6 addresses use their IPv4 bucket.
- Requests without a usable client address share one bucket.

## Limits

| Route | Method | Limit | Key |
|---|---|---|---|
| `/login` | POST | Login limit (below) | IP |
| `/me/password` | POST | Login limit | IP |
| `/me/complete-required-change` | POST | Login limit | IP |
| `/ui/fragments/target-info-cards/{target_id}` | GET | `60/minute` | IP |
| `/ui/fragments/status-summary` | GET | `30/minute` | IP |
| `/ui/fragments/targets-table` | GET | `30/minute` | IP |
| `/ui/fragments/target-status/{target_id}` | GET | `60/minute` | IP |
| `/ui/fragments/traceroute/{target_id}` | GET | `10/minute` | IP |
| `/reports/targets/{target_id}/pdf` | GET | `5/minute` | IP |
| `/reports/targets/{target_id}/pdf/request` | POST | `5/minute` | IP |
| `/settings/tsdb/purge` | POST | `10/minute` | IP |
| `/settings/alerts/purge` | POST | `10/minute` | IP |
| `/settings/auth/toggle` | POST | `5/minute` | IP |
| `/settings/signal/test` | POST | `5/minute` | IP |
| `/settings/signal/status` | GET | `30/minute` | IP |
| `/settings/signal/unregister` | POST | `3/minute` | IP |
| `/settings/signal/unlink` | POST | `5/minute` | IP |
| `/settings/network/public-egress` | GET | `12/minute` (also with `refresh=true`) | IP |
| `/settings/backup` | POST | `10/minute` | IP |
| `/settings/restore` | POST | `10/minute` | IP |
| `/settings/smtp/test-mail` | POST | `5/minute` | IP |
| `/signal/send` | POST | `5/minute` | user |
| `/signal/qr` | GET | `3/hour` | user |
| `/signal/link/confirm` | POST | `5/hour` | user |
| `/signal/link/cancel` | POST | `10/hour` | user |
| `/signal/register` | POST | `3/hour` | user |
| `/signal/verify` | POST | `10/hour` | user |
| `/targets/{target_id}/test-alert` | POST | `5/minute` | IP |
| `/recipients/verify-signal` | POST | `10/minute` | IP |
| `/recipients/{recipient_id}/test` | POST | `10/minute` | IP |
| `/recipients/{recipient_id}/test/{queue_id}` | GET | `60/minute` | IP |
| `/me/dashboard-layout` | GET, PUT, DELETE | `60/minute` each | IP |
| `/api/admin/pop/refresh` | POST | `10/minute` | IP |
| `/api/network-scans/services/{target_id}/preview` | POST | `5/minute` and `2/minute` | IP and user |
| `/api/network-scans/services/{target_id}/confirm` | POST | `10/minute` and `5/minute` | IP and user |
| `/csp-report` | POST | `30/minute` | IP |

A regression test (`tests/test_rate_limit.py`) pins this table, including that no route is
counted twice.

## Login limit and lockout

The limit for `/login`, `/me/password`, and `/me/complete-required-change` is the setting
`rate_limit_auth` (Settings > Users, default `10/minute`). It is read on every request, so
a change applies without a restart. An invalid stored value falls back to `10/minute`. Each
of the three routes keeps one counter: changing the limit keeps the request history, so
lowering it never hands out a fresh budget, and raising it takes effect immediately.
Changing the time unit (for example `10/minute` to `100/hour`) judges the kept history by the
new window.

The sign-in lockout per IP (exponential backoff after failed logins; see
[Authentication](authentication.md)) is separate. It keeps its own `429` and its own
`Retry-After`.

## Signal budgets

The six Signal routes are limited per authenticated user, not per token: all tokens of one
user share one budget, so minting new tokens gains nothing, and another user stays independent.
The user budget is only charged after authentication succeeded. Before authentication every
Signal request also passes a shared per-IP guard (`60/minute`), so an unauthenticated flood is
limited by IP and cannot use up any user's budget. Clients behind the same IP as a flooder
share that guard.

## HTTP 429

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 30
X-RateLimit-Limit: 5
Content-Type: application/json

{"detail": "Too many requests. Please try again later."}
```

`Retry-After` is a whole number of seconds, at least `1`. Clients should wait that long.
The login page counts down from it; htmx fragments are not swapped on a non-2xx answer.

## Memory bound and failure behaviour

- Each counter tracks at most 10,000 client buckets. Expired buckets are removed when the
  table is full; active buckets are never evicted to make room.
- If the table is full of active buckets, new clients are rejected with `429` (`Retry-After`
  up to 60 s) until buckets expire. Known clients keep their counters.
- Any internal error also rejects the request with `429`. The cause is logged (at most once
  per minute) and never sent to the client.

## Limits of the single-process model

Counters live in the memory of the app process and reset on restart. justUp! supports one app
process per data directory, so these counters are authoritative; there is no shared counter
store. Running several app processes against one data directory is not supported and would
multiply every limit.
