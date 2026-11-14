# Alerts

An alert has the status **open** or **resolved**. Acknowledgment is recorded
separately (who and when). Acknowledging an open alert also sets it to resolved;
auto-close resolves it when the target recovers.

## When an alert is created

- A **DOWN alert** is created when a service has been down for the target's
  Down Alert Delay (`down_alert_delay_minutes`, 1-10 min, default 5). Set it per
  target in the wizard's advanced settings or in the editor. The alert is raised
  at the first check after the delay has passed, so it can come up to one check
  interval late. The delay does not depend on the retries.
- The `unknown` status never alerts, for example while the
  [quality gate](quality-gate.md) pauses checks.
- DOWN notifications have a 5-minute cooldown. A **RECOVERY** is sent immediately.
- A certificate alert is created at 7 or fewer remaining days and resolves after
  renewal. It appears in the inbox as `monitoring.certificate.warning`.

## Fields

| Field | Content |
| --- | --- |
| Target / service | affected target and check type |
| Start / end, duration | outage timestamps and length |
| Recipients | notified recipients |
| Acknowledgment | who and when |

Auto-close can be switched per target.

## Acknowledging

Acknowledge alerts one by one or in bulk. This is allowed for admins and users
with the alert-handler permission (`can_close_alerts`). See [Users & roles](users.md).
