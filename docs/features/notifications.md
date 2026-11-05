# Notifications

justUp! notifies by **email** (SMTP) and **Signal**. Messages go through a
persistent delivery queue with retry and backoff.

## Recipients

A recipient has a name, an email address and/or a Signal number, an active flag,
and the targets it is assigned to. Only admins manage recipients.

**Enable for all Targets:** this switch (create and edit dialog) assigns the recipient to every
existing target when saved on, and removes it from every target when saved off after it was on.
New targets automatically get all recipients with the switch on. It only drives these bulk
actions: single targets can still be changed manually, and bulk assignments send no welcome message.
When switching it off for a recipient with assigned targets, saving asks for confirmation because
all assignments, including manual ones, will be removed. Cancel keeps the edits open without saving.

**Channels in the dialog:** Signal and Email each have a switch. A switched-off channel shows only its
switch; phone number, hint and Send test appear when it is on. Values you typed stay while the dialog
is open. On save, a switched-off channel is cleared (the recipient keeps only the channels that are on),
and at least one channel must be on.
**Search:** the field above the recipients list filters while you type, like the Targets search. It matches
name, email address and Signal number (case-insensitive, ignoring accents and extra spaces) and runs only in
the browser; the text is not stored or sent anywhere. Escape or the clear button resets it. Without a hit the
list shows "No recipients match ..." with a *Show all recipients* button. The query stays active after
saving, pausing or deleting; creating a new recipient clears it so the new entry is visible.

**Test messages:** the paper-plane button in the recipients list tests all saved
channels; the Edit Recipient dialog tests Signal and email separately. Tests use the
saved values, work for disabled recipients, and are not retried on failure.

**Signal status:** the Signal column shows whether the number is registered with Signal. The
answer is cached for 24 hours. The *Re-check Signal* button (circular arrows, in the row actions)
asks signal-cli again for that recipient and replaces the cached answer (a number checked in the
last 10 seconds is answered from the cache); admins only, at most
5 checks per minute. Two checks of the same number at the same time share one lookup, and at most
two lookups run at once. The number is read from the stored recipient and appears neither in the
request address nor in the log. These limits and the cache apply per application process.

## Delivery

- **Restarts:** the queue is stored in the database. After a restart or container restart, pending
  messages are sent as soon as they are due. A message that was being sent when the process died is
  picked up again after 10 minutes.
- **Temporary failures** (network down, DNS, timeout, SMTP 4xx, Signal unavailable): retried with
  backoff 1, 2, 4, 8, then every 15 minutes (±25 %). They never use up the retry budget, so an outage
  of hours is survived.
- **Validity:** an undelivered message expires after 24 hours and is shown as *failed* with
  `[EXPIRED]` in the log. A day-old alert is no longer current.
- **Errors a retry cannot fix** (invalid SMTP settings, TLS or certificate problem, rejected
  credentials, unknown errors): 5 further attempts over about 30 minutes, which leaves time to correct
  the setting, then *failed*. A TLS error never leads to an unencrypted send. An invalid recipient or a
  rejected message fails at once.
- **Pacing:** at least 2.5 s between two email sends (at most 4 per scheduler run) and at least 10 s
  between two Signal sends. The last attempt time is stored, so the gaps also hold after a restart.
  Waiting for the gap is not a failure.
- **Provider limits:** when Signal reports a rate limit, all Signal messages of that sender wait for the
  time the provider named (up to 24 h; 1 h if none was given), including new ones. An SMTP answer
  421, 450, 451 or 452 that mentions a rate or quota limit pauses email for 15 minutes. Other channels
  and senders keep sending, and waiting messages keep their retry count.
- **Order:** a *resolved* message waits until the older *open* message of the same alert, recipient and
  channel is delivered or finished, so "back online" never arrives before "offline".
- **Duplicates:** if the connection breaks after the data was handed over, it is unknown whether the
  provider accepted the message. It is retried (at least once), marked `[UNCERTAIN]` in the log, with the
  same Message-ID so mail systems can drop the copy.
- Throttling lowers the risk of being rated as spam but does not ensure delivery; SPF, DKIM, DMARC and
  sender reputation are configured outside justUp!.
- Sent and failed items are cleaned up after 7 days.
### Delivery states: what "sent" means

| Stored status | Shown as | Meaning |
| --- | --- | --- |
| `pending` | Waiting for handoff | Queued; waiting for its turn or a scheduled retry |
| `pending` with retries | Retrying handoff | The last SMTP handoff failed; another attempt is scheduled |
| `sent` | Sent | Accepted by the configured SMTP server (`RCPT TO` and `DATA`) or by signal-cli. Delivery to the recipient is not verifiable |
| `failed` | Handoff failed | Rejected, retry budget used up, or not handed over within 24 h |

`sent` is the stored status of a message that the SMTP server accepted. It never means that the
message reached a mailbox. **Later bounces cannot be detected:** justUp! has no IMAP, POP3, webhook or
DSN return channel, so an undeliverable message that the server first accepted stays `sent`.
Only the immediate SMTP answer is evaluated:

- A recipient rejected with 5xx, or a message rejected after `DATA` with 5xx, fails at once. A 4xx
  answer is retried with backoff.
- A queue entry has exactly one recipient address; an entry with several addresses is not sent. Should a
  server still accept only part of the recipients, the entry fails without retry (a retry would send a
  copy to the recipients that already received it); the log shows the count and codes, not addresses.
- A lost connection before the message content was sent is retried as "not accepted". A timeout,
  disconnect or TLS record error while the content or the final answer is outstanding leaves the
  acceptance unknown: it is retried and marked `[UNCERTAIN]`. The phase is taken from the `DATA`
  exchange; an interruption between the `DATA` command and the first answer is counted as "not accepted".
- Once the server answered 250 after `DATA`, a failing `QUIT` or close cannot change the result.
- No `VRFY`, no MX lookup of recipients and no DSN request is used.

### Alerts in the Notification Center

Admins see these entries (category *delivery*); they are stored in the database, so they appear even
when email itself is down, and they never trigger another email:

- **Email handoff failing:** an email that failed at least twice has been unsent for more than 15
  minutes. One entry per continuous outage with the failure category, the number of waiting messages and
  the next retry. It is refreshed in place at most every 30 minutes, never repeated. Normal waiting,
  the 2.5 s pacing and a cooldown without errors do not alert. No error text, address, credential or
  mail content is stored.
- **Email handoff failed:** the existing per-message failure entry (exactly one per message); an open
  outage entry is switched to the final state instead of adding a second warning.
- **Email handoff recovered:** the first accepted message after a reported outage creates one entry
  stating that the SMTP handoff succeeded again. It does not say that mail was delivered. A restart
  neither loses an open outage nor creates a recovery: the state is stored in the `settings` table.

On the dashboard and in Settings, failed handoffs of the last 24 hours (`failed_recent`) mark the queue
as failed even when nothing is waiting; older failures only appear in the log. Flush removes failed
entries. Settings also shows the note that a successful handoff confirms only the server's acceptance.

Settings > Delivery Queue shows statistics, per channel the waiting count, next attempt, oldest
message, active cooldown and failures, and the log. You can flush the queue or clear the log.

### Background service alerts

The service watchdog reports to the same inbox (category *activity*, admins only), so a failing
background service is visible although the web server stays healthy. The alert is written with its own
short database connection and does not depend on the monitoring scheduler or the SMTP queue; it never
sends an email. If the database is briefly unreachable, the alert is queued in memory (at most 50) and
stored as soon as it is back, as long as the process runs.

| Entry | When | Severity |
| --- | --- | --- |
| `<Service> crashed` | The service crashed or hung and is restarted automatically. | warning |
| `<Service> degraded` | 5 crashes within 5 minutes, 3 hangs within 24 hours, or a failure series (below). | error |
| `<Service> recovered` | The service ran 60 seconds without a crash and, after a failure series, completed an iteration and stayed clean for 60 seconds. | success |

There is one entry per continuous disturbance and service. Repeated crashes refresh it in place at most
every 30 minutes (a counter is kept); the escalation to *degraded* rewrites it and marks it unread again.
The first stable run after a container start also closes an entry a previous process left open. Only the
service name, time, state, crash count and the exception class name are stored, never an error message.
Orderly shutdown and a backup restore are not crashes and raise nothing.

Services that catch errors inside their own loop report each iteration to the watchdog. They count as
*degraded* after consecutive failed iterations that also span at least 2 minutes:

| Service | Threshold |
| --- | --- |
| Monitoring scheduler | 5 failed iterations (a scheduler that fails is no longer counted as alive for the 30 minute hang check) |
| PDF cleanup | 3 failed cycles |
| POP location refresh, release check | 3 failed attempts (they run rarely) |

Limits: `/healthz` keeps answering `200` with `degraded: true`, and Docker restarts the container only
when the process exits, not when it turns *unhealthy*. A complete container or host outage cannot be
reported by the inbox itself; use an external availability monitor on `/healthz`. The PDF worker reports
crashes of its task but not single failed jobs (the requester gets those). The event bus (SSE) is not
supervised: it runs no background task of its own (keep-alives belong to each client stream), so there is nothing to restart or to report a heartbeat.

## Email (SMTP)

Configure host, port, STARTTLS or implicit TLS, and credentials under Settings >
SMTP. Credentials are stored encrypted. Subject and HTML template are editable
with a fixed set of placeholders. A test email is queued like any other.

Transport security (encrypted by default):

- **STARTTLS is on by default.** With no stored choice the effective value is on, on a new
  installation and on an existing one that never saved it. `smtp_enabled` is a separate
  setting and is never switched on by this default.
- **Port 465 always uses implicit TLS** (SMTPS). The STARTTLS switch has no effect there; the UI
  shows it locked on and labelled "Implicit TLS (SMTPS)".
- **Fail closed.** If the server does not offer STARTTLS, the handshake fails or the certificate
  or host name check fails, nothing is sent and the queue entry fails. There is no fallback to
  cleartext, and the username and password are only sent after TLS is established.
- **Compatibility exception.** Unencrypted sending needs an administrator to switch **Use
  STARTTLS** off on purpose (stored as `smtp_use_tls=false`) *and* neither username nor
  password to be set. Use it only for a trusted relay. With a username or password, sending is
  refused while TLS is off.
- **Incomplete or unreadable credentials are an error.** A username without a password (or the
  reverse), or a stored password that cannot be decrypted (for example after a key change), stops
  the send with a configuration error. It never turns into an anonymous send. Re-enter the
  password to fix it.
- Only a stored `false` counts as "off". A missing, `null` or otherwise invalid value is read as
  "on" (invalid values are logged). `PATCH /settings` accepts only the JSON values `true` and
  `false` for `smtp_use_tls`; `null`, strings such as `"off"` and numbers are rejected with 422.

!!! warning "Behavior change in 1.0.1"
    Earlier versions treated a missing choice as "off". If you relied on that for a plaintext
    relay without credentials, open Settings > SMTP and switch **Use STARTTLS** off once;
    otherwise mail now needs STARTTLS from your server. An already saved "off" is kept as is.

## Links in notifications

Links need the **Public hostname** (Settings > General, see [Settings](../configuration/settings.md)).
With it set, notifications link to `https://<hostname>/ui/targets/<id>`; without it they
carry no link, and the settings page shows a hint.

- **Email:** the green **Open in justUp!** button only renders when the instance URL is a
  valid `http(s)` URL, so a missing public hostname means no button. The plain-text part
  shows the URL as text. The default template contains the button; a saved custom template
  keeps its own markup and needs the `{{#if cta}}` block around the button to show it.
- **Signal:** the default DOWN and UP templates end with a `Link:` line. Add
  `{{#if cta}}{{system.url}}{{/if cta}}` to a saved custom template to include the link.
- **Welcome messages** link to the start page.

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
