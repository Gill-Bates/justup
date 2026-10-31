# Reverse Proxy

For HTTPS and external reachability, run justUp! behind a reverse proxy. justUp!
itself does not terminate TLS.

## Trusted proxies and client IP

justUp! derives the client IP from `X-Forwarded-For` or `X-Real-IP` only when the
direct sender is listed in `UPMON_TRUSTED_PROXIES`. This is relevant for rate
limiting and login lockout.

!!! danger "Set UPMON_TRUSTED_PROXIES correctly"
    Enter **only** the address(es) of your proxy. Invalid entries and `*`
    are discarded; without a matching address, justUp! ignores proxy headers and
    uses the direct peer address.

    ```bash
    UPMON_TRUSTED_PROXIES=10.0.0.2
    ```

## HTTPS detection

Set the `X-Forwarded-Proto` header at the proxy and enter its direct address in
`UPMON_TRUSTED_PROXIES`. justUp! honors the header only from a trusted peer and
then sets the `Secure` flag on the cookies. An unverified `X-Forwarded-Proto`
header is not used for client-IP or HTTPS detection.

## Examples

=== "Nginx"

    ```nginx
    server {
        listen 443 ssl;
        server_name justup.example.com;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host              $host;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
    ```

    And in the justUp! environment:

    ```bash
    UPMON_TRUSTED_PROXIES=127.0.0.1
    ```

=== "Traefik (labels)"

    ```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.justup.rule=Host(`justup.example.com`)
      - traefik.http.routers.justup.entrypoints=websecure
      - traefik.http.routers.justup.tls.certresolver=le
      - traefik.http.services.justup.loadbalancer.server.port=8000
    ```

The interface receives live updates over one Server-Sent-Events stream per
tab (`/ui/events`, session cookie, a heartbeat every 15 s). The application sends
`X-Accel-Buffering: no`; for other proxies disable response buffering for
`/ui/events`. The default read timeout of 60 s of common proxies (for example
nginx) is sufficient because a heartbeat arrives every 15 s. If the stream is blocked, the
interface falls back to periodic HTTP refreshes and does not show the offline
dialog as long as `/health` stays reachable. For restore uploads, the proxy timeout should
match the file size and the duration of the apply; the 30-s quiescing limit
concerns only waiting for database and background work already running.
