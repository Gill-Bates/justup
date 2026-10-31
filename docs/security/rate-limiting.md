# Rate Limiting

justUp! uses SlowAPI with a process-local storage structure and the
trusted client IP as the key. In addition, a login lockout per IP and
username applies.

## How it works

- Limits are set as a SlowAPI decorator per route. The login limit comes
  from the admin setting `rate_limit_auth` (default `10/minute`); heavy
  operations, PDF reports, and Signal actions have their own limits.
- On exceeding, the server responds with **`429`** and a
  `Retry-After` header.

## Client IP

The client IP is taken from `X-Forwarded-For` or `X-Real-IP` only
when the direct sender is in `UPMON_TRUSTED_PROXIES`. Invalid entries
or `*` are ignored (the connection is then evaluated with the actual
peer address). See [Reverse Proxy](../configuration/reverse-proxy.md).

## Where limits apply

| Area | Purpose |
| --- | --- |
| Login | protection against brute force (complemented by the lockout) |
| Expensive endpoints | PDF report, traceroute, restore, test messages |
| UI polling | limit for status, table, and dashboard fragments |

In addition, a login lockout per IP and per username applies (see
[Authentication](authentication.md)).

!!! warning "One app process per container"
    Limiter, scheduler, and maintenance lock are process-local. The container image
    starts one Uvicorn process; there is no supported `WORKERS` variable.
    Multiple app processes against the same data are not supported. See
    [Environment Variables](../configuration/environment.md).
