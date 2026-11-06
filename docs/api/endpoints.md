# Endpoints

This overview lists the most important JSON endpoints. FastAPI publishes the
complete, field-level list at `http(s)://<justup-host>/openapi.json` and
`http(s)://<justup-host>/docs`. Most paths live directly at the root,
without a general `/api` prefix; exceptions are `/api/meta` and
`/api/admin/pop/*`. Authentication: [API Authentication](authentication.md).

## Health and sign-in

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz`, `/health` | public liveness check without a database check |
| `GET` | `/api/meta` | app name and version |
| `GET` | `/ui/events` | Server-Sent Events stream with UI change hints (cookie or bearer; 401 when not signed in) |
| `GET` | `/ui/events/session` | cheap session probe for the stream client (401 = sign in again) |
| `POST` | `/login` | check username/password, issue bearer token |
| `POST` | `/logout` | revoke session token |
| `GET` | `/me` | read the signed-in user |
| `POST` | `/me/password` | change own password |
| `POST` | `/me/complete-required-change` | complete a forced password change |
| `GET`, `PATCH` | `/me/theme` | read/change personal theme |
| `GET`, `PUT`, `DELETE` | `/me/dashboard-layout` | read, save, reset own dashboard layout |

## Targets and monitoring

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/targets` | list / create targets |
| `GET` | `/targets/profiles` | available wizard profiles |
| `POST` | `/targets/preview` | check profile data and existing conflicts, without a network probe |
| `GET`, `PATCH`, `DELETE` | `/targets/{target_id}` | read, edit, delete target (history is removed first; on a failure the target stays and the request can be repeated) |
| `POST` | `/targets/{target_id}/reset` | reset measurement data |
| `GET`, `PUT` | `/targets/{target_id}/alert-users` | read / replace alert recipients |
| `GET` | `/targets/{target_id}/notification-status/{task_id}` | delivery status of new recipients |
| `POST` | `/targets/{target_id}/test-alert` | trigger a test alert |
| `GET` | `/targets/{target_id}/metrics/{metric}` | query one metric series |
| `GET` | `/dashboard/uptime`, `/dashboard/downtime` | dashboard time series |
| `GET` | `/network-scans/status` | whether the Nmap worker accepts scans (admin): `available`, `state`, `message`, `detail` |
| `POST` | `/network-scans` | start a port discovery scan (admin; `host`, `protocols` `tcp`/`udp`, `profile` `quick`/`extended`); `202`, `429` when busy, `503` with `detail.code` `unavailable` when the worker is down |
| `GET`, `DELETE` | `/network-scans/{scan_id}` | read status and result / cancel your own scan |

Exact target duplicates respond with `409 Conflict`; different services on
the same host remain allowed. Preview also classifies overlapping checks.
Create, Preview, and the independent PATCH edit use the same
server-side validation.

## Alerts and master data

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/alerts`, `/alerts/{alert_id}` | list alerts / read details |
| `POST` | `/alerts/{alert_id}/acknowledge` | acknowledge alert (admin or alert handler) |
| `POST` | `/alerts/acknowledge-all` | acknowledge open alerts in bulk |
| `GET` | `/ui/notifications/summary` | unread counter, ID of the newest unread event, preview (UI polling, cookie or bearer) |
| `GET` | `/ui/notifications/items` | notifications of the signed-in user (filters `category`, `unread`, `severity`, pagination) |
| `POST` | `/ui/notifications/{id}/read`, `/ui/notifications/read-all` | mark as read (only for the caller; changes no alert) |
| `GET`, `POST` | `/groups` | list / create groups |
| `GET`, `PATCH`, `DELETE` | `/groups/{group_id}` | read, change, delete group (`409` if the name exists or targets still use the group, `404` if unknown) |
| `GET`, `POST` | `/recipients` | list / create recipients (`409` if the phone number already exists); `enable_for_all_targets` assigns the recipient to every target |
| `GET`, `PATCH`, `DELETE` | `/recipients/{recipient_id}` | read, change, delete recipient (`PATCH` of `enable_for_all_targets`: `true` assigns to all targets, `false` after `true` removes from all) |
| `POST` | `/recipients/verify-signal` | verify Signal number |
| `POST` | `/recipients/{recipient_id}/test` | queue a test message to the saved Signal/email channel (`channel`: `signal`, `email`, `all`; admin; 30 s cooldown per recipient and channel) |
| `GET` | `/recipients/{recipient_id}/test/{queue_id}` | delivery state of a test message: `pending`, `sent` (accepted by the SMTP server; final delivery is not verifiable), `failed` (admin) |
| `GET`, `POST` | `/users` | list / create users (admin) |
| `PUT`, `DELETE` | `/users/{user_id}` | change / delete user (admin) |
| `POST` | `/users/{user_id}/active` | set user status (admin) |
| `POST` | `/users/{user_id}/revoke-sessions` | revoke another user's active sessions (admin) |

## Settings, reports, and services

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `PATCH` | `/settings` | read / change runtime settings (admin); the response includes the non-secret SMTP values, the effective `smtp_use_tls` and `smtp_password_set`, never the password |
| `POST` | `/settings/backup` | create and download a backup |
| `POST` | `/settings/restore` | restore a backup with admin password confirmation |
| `POST` | `/settings/auth/toggle` | enable or disable authentication (`disable`, admin `password`; admin) |
| `GET` | `/settings/storage`, `/settings/db-health` | storage and database status |
| `POST` | `/reports/targets/{target_id}/pdf/request` | start an asynchronous RGB PDF report |
| `GET` | `/reports/jobs/{job_id}` | read report status |
| `GET` | `/reports/jobs/{job_id}/download` | download a finished report |
| `GET` | `/reports/targets/{target_id}/pdf` | request a report directly |
| `GET` | `/quality/current`, `/quality/history` | Quality Gate state and history |
| various | `/signal/*`, `/settings/signal/*` | signal-cli account, messages, and templates |
| various | `/settings/smtp/*`, `/settings/notification-queue/*` | email, queue, and delivery log; `GET /settings/notification-queue/stats` returns `failed` (all stored) and `failed_recent` (last 24 h); `sent` means accepted by the server, not delivered |
| `GET`, `POST` | `/api/admin/pop/*` | POP data and import runs (admin) |

Backup, restore, purge, and Signal operations are additionally rate-limited.

## Notifications

The notification endpoints live under `/ui/` (cookie auth and CSRF protection),
respond with `Cache-Control: private, no-store`, and return 404 for IDs not visible.
`/ui/alerts` redirects to `/ui/notifications?category=monitoring`. Report jobs
(`/reports/jobs/{id}`, `/download`) are accessible to the requester and admins;
expired downloads return `410`.
