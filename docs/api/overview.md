# API Reference – Overview

justUp! provides a JSON API for the same resources as the interface.
Most routes live directly at the root, e.g. `/targets` or `/settings`;
there is **no general `/api` prefix**. The exceptions are `/api/meta` and the
POP administration under `/api/admin/pop`.

## Authentication

The JSON API expects `Authorization: Bearer <access_token>` by default. The
token is provided by `POST /login`; the endpoint also sets an HttpOnly cookie for
browser navigation. The interface uses the cookie for pages and the
bearer token for API calls. Details and examples:
[API Authentication](authentication.md).

CSRF protection applies to writing browser routes under `/ui/` and `/login` as well as to
`/me/theme`, `/me/complete-required-change`, and `/me/dashboard-layout`. Most
JSON API routes, which use bearer authentication, lie outside this
cookie CSRF protection.

## Errors and limits

FastAPI returns validation and application errors in the JSON field `detail`.
Depending on the endpoint, `400`, `401`, `403`, `409`, `422`, `429`, and `503`
are used among others. Rate limits respond with `429` and, if available, `Retry-After`.

## Resources

| Resource | Route base | Purpose |
| --- | --- | --- |
| Targets | `/targets` | targets, profiles, preview, checks, and recipient assignment |
| Metrics | `/targets/{id}/metrics`, `/dashboard/*` | time series, uptime, and downtime |
| Alerts | `/alerts` | read alerts, acknowledge individually or in bulk |
| Groups, recipients, users | `/groups`, `/recipients`, `/users` | manage master data and roles |
| Self-service | `/me/*` | own password, theme, and dashboard layout |
| Settings | `/settings` | runtime settings, backup, restore, SMTP, and Signal |
| Signal | `/signal` | manage signal-cli account and messages |
| Reports | `/reports` | PDF reports and asynchronous jobs |
| Quality | `/quality/*` | state and history of the Quality Gate |
| POP administration | `/api/admin/pop` | POP data and import runs (admin) |
| Health and metadata | `/healthz`, `/api/meta` | liveness and app version |

The field-level specification is generated directly from FastAPI: `/openapi.json` or
`/docs`.
