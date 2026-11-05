# Troubleshooting

Check the logs first:

```bash
docker logs -f justup
```

## The container does not start

??? failure "`SECRET_KEY` not set"
    justUp! does not start without `SECRET_KEY`. Generate a Fernet key:
    ```bash
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ```
    See [Environment variables](configuration/environment.md).

??? failure "Entrypoint aborts"
    The entrypoint exits if `PUID`/`PGID` are not numeric, are `0` or equal the reserved Nmap worker id
    `10001`, if `gosu`
    is missing, or if `/app/data` is not writable by that user.

## Wrong client IP in rate limits or audit log

??? question "The proxy IP appears instead of the client IP"
    Set `TRUSTED_PROXIES` to the proxy address as the container sees it.
    See [Reverse proxy](configuration/reverse-proxy.md).

## Sign-in fails

??? question "Admin password unknown"
    ```bash
    docker exec -it justup justup-cli reset password admin
    ```

??? question "Account locked"
    Failed sign-ins lock out the IP and username with growing delay (up to one
    hour). Wait it out. See [Authentication](security/authentication.md).

## No notifications

??? question "Email does not arrive"
    Check the SMTP settings and send a test email. The delivery queue retries up
    to 5 times; Settings > Delivery Queue shows the log.

??? question "Signal does not send"
    Check that the signal-cli account is registered or linked and send a test
    message. See [Notifications](features/notifications.md).

## Everything shows `unknown`

??? info "The quality gate paused checks"
    Poor connectivity of the justUp! host pauses checks. Check the dashboard
    widget and the probe targets (Settings > Network). See [Quality gate](features/quality-gate.md).

## Ping or traceroute does not work

??? failure "Missing capability"
    `ping` and the Nmap-based traceroute need `cap_net_raw`. Keep `NET_RAW` in the container's
    capabilities. See [Docker](getting-started/docker.md).

## Port scan or UDP check does not work

??? failure "Nmap worker unavailable or permission_denied"
    The scan worker is down, or `NET_RAW` is missing. UDP results that are not clear stay
    `unknown` and are never downtime. See
    [Network monitoring with Nmap](features/network-monitoring.md#troubleshooting).

## Status does not update live

??? question "Live updates are delayed"
    The browser keeps one event stream (`/ui/events`) open. If a proxy buffers or
    cuts it, the page falls back to polling every 15 s. Disable response buffering
    for `/ui/events`; see [Reverse proxy](configuration/reverse-proxy.md).

??? question "Server offline overlay or redirect to sign-in"
    The overlay appears when `/health` fails twice in a row and closes on its own.
    A redirect to sign-in means the session expired, was revoked, or the account
    was deactivated.

## No geo data on the traceroute map

??? info "GeoLite2 database missing"
    Traceroute still works, but hops without another location source stay off the
    map. See [Traceroute & maps](features/traceroute.md).

## A background service keeps crashing

??? question "Service is `degraded`"
    A watchdog restarts crashed background services and logs
    `service=<name> event=<event> ...`. After repeated crashes a service is
    `degraded` and retried every 5 minutes.
    ```bash
    docker logs justup 2>&1 | grep "event=crashed\|event=degraded"
    ```
    `GET /healthz` lists the state of each service. Fix the cause named in the
    traceback; the service recovers on its next retry. Admins also get a
    [Notification Center entry](features/notifications.md#background-service-alerts)
    for a crash, for `degraded`, and for the recovery.
