# Users & Roles

justUp! is multi-user capable. Every access requires a sign-in unless an admin
disables authentication under *Settings > Users*
(see [Settings](../configuration/settings.md)).

## Roles

| Role | May |
| --- | --- |
| **Viewer** (signed in) | read dashboard, targets, metrics, alerts, and reports; change own password and theme |
| **Alert handler** (`can_close_alerts`) | additionally acknowledge alerts |
| **Admin** | everything, including targets, groups, recipients, users, settings, backup, and Signal account |

Permissions are defined declaratively per route. Write access to groups and
recipients is reserved for admins.

## User management

- User CRUD is performed by admins.
- Admins cannot lock themselves out and cannot remove the last admin.
- Per user, the theme (light, dark, system) and sign-in credentials are stored.
- Admins can revoke another user's active sessions from **Settings > Users**;
  the affected user must sign in again.

## Sign-in

- Login with username and password.
- After too many failed attempts, a lockout takes effect – **per IP and per
  username**.
- Login consumes constant time, even for unknown users, to prevent user
  enumeration.

Details on the security model: [Authentication](../security/authentication.md).

## Reset password (CLI)

```bash
# a specific user
justup-cli reset password <username>

# all admins
justup-cli reset password
```

!!! note "Initial admin password"
    The first admin password is generated randomly on the first start and printed
    to stdout on every start until it is changed. It is not sent to the
    application log handlers; a private file in the data directory holds it
    until the change so that it can be shown again.
    The first sign-in leads to `/ui/change-password`. A password reset by an
    admin through the web UI also forces a change at the next sign-in; the CLI
    reset revokes active sessions and prints a new password, but does not set
    the forced-change flag.
