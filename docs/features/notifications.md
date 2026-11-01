# Notifications

justUp! notifies by **email** (SMTP) and **Signal**. Messages go through a
persistent delivery queue with retry and backoff.

## Recipients

A recipient has a name, an email address and/or a Signal number, an active flag,
and the targets it is assigned to. Only admins manage recipients.

**Test messages:** the paper-plane button in the recipients list tests all saved
channels; the Edit Recipient dialog tests Signal and email separately. Tests use the
saved values, work for disabled recipients, and are not retried on failure.

## Delivery

- Up to 5 attempts per message, backoff from 60 s to 1 h.
- Signal is throttled to one message every 2 s.
- Sent and failed items are cleaned up after 7 days.

Settings > Delivery Queue shows statistics and the log and lets you flush the queue
or clear the log.

## Email (SMTP)

Configure host, port, STARTTLS or implicit TLS, and credentials under Settings >
SMTP. Credentials are stored encrypted. Subject and HTML template are editable
with a fixed set of placeholders. A test email is queued like any other.

Transport security: port 465 always uses implicit TLS. On any other port, keep
STARTTLS on so the connection is encrypted before login. Turn STARTTLS off only
for a trusted unauthenticated relay (no username/password); otherwise mail is
never sent in cleartext.

## Signal

Signal uses the signal-cli bundled in the container. Under Settings > Signal you can:

- register a number by SMS or call (with captcha) and verify it,
- link as a secondary device by QR code, or unlink,
- set the profile name, edit the DOWN/UP templates, and send a test message.

Set `JAVA_XMS`, `JAVA_XMX`, and `SIGNAL_CLI_PATH` as described in
[Environment variables](../configuration/environment.md).

## Notification center

The bell in the top bar and `Manage > Notifications` show a per-user inbox. Reading
a notification never acknowledges an alert.

- **Delete** a notification by swiping left or with the delete control on the row.
  It is hidden for you only.
- A new delivery in a grouped time window shows the group again.

| Event | Audience |
| --- | --- |
| Alert opened or resolved, certificate warning | admins and users assigned to the target |
| PDF report completed or failed | the requesting user |
| Admin password changed or reset, user created, target created | admins |
| New release available | admins |
| Email or Signal delivery sent or failed | admins |

Once a day justUp! asks the GitHub releases API for the latest stable release and
adds a notice for a newer version. There is no setting to turn this off. Successful
sign-ins are not inbox events. Notifications are deleted automatically 180 days after they were created, read or unread.
