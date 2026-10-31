# Troubleshooting

Common problems and their causes. Check the logs first:

```bash
docker logs -f justup
```

## The container does not start

??? failure "`SECRET_KEY` not set"
    justUp! does not start without `SECRET_KEY` – there is no default. In Compose,
    `${SECRET_KEY:?...}` aborts the start. Set a Fernet key:

    ```bash
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ```

    See [Environment Variables](configuration/environment.md).

??? question "Rate limits or audit logs show the proxy IP instead of the client IP"
    Enter the direct address of the reverse proxy in
    `UPMON_TRUSTED_PROXIES`. Invalid entries are ignored; until a
    valid proxy is trusted, justUp! uses the direct peer address.
    See [Reverse Proxy](configuration/reverse-proxy.md).

??? failure "Privilege-drop tool missing"
    The entrypoint aborts if the tool for dropping root rights is missing.
    This is intentional – the application process must never run as root.

## Sign-in fails

??? question "Admin password unknown"
    Reset it via the CLI:

    ```bash
    docker exec -it justup justup-cli reset password admin
    ```

??? question "Account locked"
    After too many failed attempts, a lockout per IP and username takes effect. Wait
    out the lockout period. See [Authentication](security/authentication.md).

## No notifications

??? question "Email does not arrive"
    Check the SMTP settings and send a test email from the
    settings. The spooler retries up to 5 times with backoff; the spooler UI
    shows the log.

??? question "Signal does not send"
    Check whether the signal-cli account is registered or linked, and send
    a test message. See [Notifications](features/notifications.md).

## Everything shows `unknown`

??? info "Quality Gate has suspended checks"
    When the instance's own connectivity is poor, the
    [Quality Gate](features/quality-gate.md) suspends the checks and marks targets
    as `unknown` instead of producing false alerts. Check the gate widget in the
    dashboard and the reference targets (Settings > Network).

## Ping or traceroute does not work

??? failure "Missing capabilities"
    `ping` and `mtr` need `cap_net_raw`. In the official image this is set via
    `setcap`. In customized environments the capability may be missing – see
    [Docker](getting-started/docker.md).

## Target status looks stale

??? question "Status does not update live"
    Pages receive change hints over one Server-Sent Events stream (`/ui/events`)
    and reload the affected parts through normal requests. In the browser's
    network tab, check that `/ui/events` stays open and delivers a heartbeat every
    15 s. If a reverse proxy buffers or cuts the stream, the page falls back to
    polling every 15 s; see [Reverse Proxy](configuration/reverse-proxy.md). Also check
    the `/ui/fragments/*` responses, the rate-limit logs, and whether the proxy caches
    error responses or HTML fragments.

??? question "Server offline overlay or redirect to sign-in"
    The overlay appears after two failed `/health` requests, so the server or
    network is really unreachable; it closes by itself once the server answers. A
    redirect to the sign-in page means the session ended: it expired, was revoked
    (Settings > Users), or the account was deactivated or changed.

## No geo data in the traceroute map

??? info "GeoLite2 missing"
    Without the GeoLite2 database file, justUp! continues to run, but without the geo feature.
    Obtain the file in a license-compliant way (MaxMind account). See
    [Traceroute & Maps](features/traceroute.md).

## Background services

??? question "A background service keeps crashing or is `degraded`"
    Every background service runs under a watchdog that logs one line per event in the form
    `service=<name> event=<event> state=<state> ...`. Events: `started`, `crashed` (with
    traceback and exception type), `restarting` (attempt and backoff), `recovered` (stable for
    60 s), `degraded` (5 crashes within 5 minutes, or 3 detected hangs within 24 hours; then one retry
    every 5 minutes; hang-based degradation ends after one hour of error-free running), `paused` /
    `resumed` (maintenance), `interrupted`, and `stopped`. Find them with:

    ```bash
    docker logs justup 2>&1 | grep "event=crashed\|event=degraded"
    ```

    `GET /healthz` lists the state of each service (admins get crash counters from the
    notification-queue stats endpoint); `ok` stays `true`
    because restarting the container would not fix a failing service. Fix the cause named in
    the traceback; the service recovers on its next retry.
