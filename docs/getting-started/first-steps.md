# First Steps

## 1. Change the admin password

Sign in as `admin` with the initial password from `docker logs justup`. The
password is printed on every start until it is changed. Change it at once;
earlier output stays in the container log until the log is rotated.

!!! tip "Forgot the password?"
    ```bash
    docker exec -it justup justup-cli reset password admin   # all admins
    docker exec -it justup justup-cli reset password <username>
    ```

## 2. Create a target

Select **New Target**. Choose a profile (website, API/health endpoint,
server/host, TCP/UDP service, or custom), enter the URL or host, adjust the
suggested checks, and review the summary. For TCP or UDP, enter the ports or discover them with a port scan (admins).
Exact duplicates are blocked. **Edit** opens the full editor.

See [Monitoring & checks](../features/monitoring.md).

## 3. Set up notifications

1. Create at least one recipient with an email address and/or a Signal number.
2. Assign the recipient to targets.
3. Configure SMTP and/or signal-cli.

See [Notifications](../features/notifications.md).

## 4. Know the quality gate

If justUp! detects that its own connectivity is poor, it pauses checks and sets
targets to `unknown` instead of alerting. See [Quality gate](../features/quality-gate.md).

## 5. Create a report

On the target page, select the **Range**, then start the PDF report. See
[Reports](../features/reports.md).
