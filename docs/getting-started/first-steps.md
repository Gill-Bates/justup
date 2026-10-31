# First Steps

After the [installation](installation.md), set up your monitoring. This page walks
through the most important first steps.

## 1. Change the admin password

Sign in as `admin`. Change the initial password immediately in your user profile.

The initial password is generated at the first start and printed to the console
(`docker logs justup`). It is printed again on **every** start until the admin has signed
in and changed it; after that it is never shown again. To make this possible, the plaintext
is kept in `<data dir>/.bootstrap-admin-credentials` (mode `0600`, service user only, outside
the backed-up `sqlite`, `tsdb` and `pdf_reports` directories) and deleted when the password
changes, whether through the UI, the API, or `justup-cli`. Earlier banners stay in the
container log history until the log is rotated, so change the password promptly.

!!! tip "Forgot your password?"
    The admin password can be reset via the CLI:

    ```bash
    docker exec -it justup justup-cli reset password admin
    # or for all admins:
    docker exec -it justup justup-cli reset password
    ```

## 2. Create your first target

Select **New Target**. The wizard first asks for the purpose:
website, API/health endpoint, server/host, TCP service, or a custom profile.
Then you enter the URL or host, adjust the suggested checks, and review the full
summary before the start.

For a domain without a scheme, the wizard suggests HTTPS. An explicitly entered
`http://` address stays HTTP. For TCP you must specify the ports yourself; the
wizard does not guess them. Exact duplicates are shown and blocked before
creation. After creating, **Edit** opens the full editor directly, not the wizard
again.

Details on profiles, checks, and duplicates: [Monitoring & Checks](../features/monitoring.md).

## 3. Set up notifications

1. Under **Recipients**, create at least one entry with an email and/or a Signal
   phone number.
2. Assign recipients to the targets they concern.
3. Configure the delivery path: SMTP for email, signal-cli for Signal.

See [Notifications](../features/notifications.md).

## 4. Understand the Quality Gate

justUp! continuously measures its own connectivity. If the network situation is
poor, due checks are suspended and affected targets go to `unknown` instead of
producing false alerts. The state is visible in the dashboard. Details:
[Quality Gate](../features/quality-gate.md).

## 5. Generate a report

On a target's detail page, choose **Range** first, then start a PDF report.
The report uses that selected time range. Generation runs as an
asynchronous job; progress and download appear once the job is finished. See
[Reports](../features/reports.md).
