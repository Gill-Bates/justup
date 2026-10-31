# Alerts

Alerts bundle outages into a traceable lifecycle: **open**, **acknowledged**,
**closed**.

## When an alert is created

- A **DOWN alert** is created when a service is down for longer than
  `metrics_timeout_minutes` (1–10, default 5).
- The `unknown` status **never** alerts – for example when the Quality Gate
  suspends checks.
- DOWN notifications have a 5 min cooldown; a **RECOVERY** is reported
  immediately.

## Certificate warning

A certificate alert is created at ≤ 7 remaining days (fixed threshold). It is
delivered through the dispatcher like other alerts, appears in the notification
inbox as `monitoring.certificate.warning`, and resolves automatically after
renewal.

## Lifecycle and fields

Each alert carries a UUID and the following details:

| Field | Content |
| --- | --- |
| Target / service | affected target and check type |
| Start / end | timestamps of the outage |
| Duration | computed outage duration |
| Recipients | notified recipients |
| Acknowledgment | who acknowledged and when |

Auto-close can be toggled per target.

## Acknowledging

Alerts can be acknowledged individually or in bulk. This is allowed for admins and
users with the `can_close_alerts` permission (alert handler role).
See [Users & Roles](users.md).

!!! tip "No false alert when your own situation is poor"
    If connectivity of the justUp! instance itself fails, the
    [Quality Gate](quality-gate.md) suspends the checks and creates no
    DOWN alerts. This prevents false alerts caused by your own network problems.
