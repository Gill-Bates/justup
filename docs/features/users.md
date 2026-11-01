# Users & Roles

Every access requires sign-in, unless an admin disables authentication under
Settings > Users (see [Settings](../configuration/settings.md)).

## Roles

| Role | May |
| --- | --- |
| **Viewer** | read dashboard, targets, metrics, alerts, reports; change own password and theme |
| **Alert handler** (`can_close_alerts`) | additionally acknowledge alerts |
| **Admin** | everything, including targets, groups, recipients, users, settings, backup, Signal |

## User management

- Admins create, change, deactivate, and delete users.
- An admin cannot lock themselves out or remove the last admin.
- Each user has a theme (light, dark, system).
- Admins can revoke another user's sessions in Settings > Users.

## Sign-in

After repeated failed sign-ins, the IP and the username are locked out with
growing delay (up to one hour). See [Authentication](../security/authentication.md).

## Reset a password

```bash
docker exec -it justup justup-cli reset password <username>
docker exec -it justup justup-cli reset password admin   # all admins
```

The CLI prints a new password and revokes the user's sessions.

!!! note "Initial admin password"
    On first start justUp! prints a random one-time password to the container
    output, on every start until it is changed. The first sign-in leads to
    `/ui/change-password`. A reset by an admin in the web UI also forces a change.
    The CLI reset does not.
