# Reverse Proxy

justUp! does not terminate TLS. Run it behind a reverse proxy for HTTPS.

## Trusted proxies

justUp! reads the client IP from `X-Forwarded-For` or `X-Real-IP`, and the scheme
from `X-Forwarded-Proto`, only when the direct sender is listed in
`TRUSTED_PROXIES`. This matters for rate limiting, lockout, the audit log,
and the `Secure` cookie flag.

!!! danger "List only your proxy"
    Enter the proxy address as the container sees it. Invalid entries and `*`
    are ignored. Without a match, justUp! uses the direct peer address.
    ```bash
    UPMON_TRUSTED_PROXIES=10.0.0.2
    ```

The shipped `docker/docker-compose.yml` reads `UPMON_TRUSTED_PROXIES` from
your shell or `.env` file and passes it to the container as `TRUSTED_PROXIES`. The default is empty, so no proxy is trusted until you set
it. There is no built-in trusted address or Docker subnet.

With the variable unset, `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, and
`X-Forwarded-Proto` are ignored: the direct peer address is used for rate limiting,
lockout, and the audit log, and the `Secure` cookie flag follows the scheme of the
direct connection. A peer outside the list cannot spoof these values either. For
`X-Forwarded-For`, justUp! walks the chain from the right and stops at the first
address that is not trusted. `X-Forwarded-Proto` uses the rightmost value, the one
set by the closest proxy. IPv4-mapped IPv6 addresses (`::ffff:10.0.0.2`) match
IPv4 entries of `TRUSTED_PROXIES`.

!!! warning "Overwrite client-supplied headers"
    Configure the proxy to replace or append to `X-Forwarded-For` and to set
    `X-Forwarded-Proto` itself (see the examples below). Also make sure
    justUp! is reachable only through the proxy; a client that can connect
    directly is treated as an ordinary untrusted peer, which is safe, but it
    bypasses any filtering the proxy does.

## Authentication at the proxy

justUp! treats any `Authorization` header as an explicit login attempt and never
falls back to the session cookie for that request. A proxy that authenticates
users itself (HTTP basic auth, forward auth) must therefore not pass its
`Authorization` header on to justUp!, or page loads end at the sign-in page
although the session cookie is valid. Strip it, for example in Nginx with
`proxy_set_header Authorization "";` (this also blocks Bearer API clients on
that route).

## Response security headers

justUp! adds these headers to every response, including static files and error
responses:

| Header | Value |
| --- | --- |
| `X-Frame-Options` | `SAMEORIGIN` |
| `Content-Security-Policy` | `frame-ancestors 'self'` |
| `Content-Security-Policy-Report-Only` | same-origin policy with inline scripts and styles allowed; reports go to `/csp-report` |

Do not add the same headers at the proxy. Two `X-Frame-Options` headers with
different values make browsers ignore the header. Embedding justUp! in a page on
another origin is therefore not possible.

`/csp-report` accepts unauthenticated `POST` requests of up to 8 KiB, allows 30 per
minute per client, and only writes a `CSP_VIOLATION` log line without query strings.
It stores nothing. Allow `POST /csp-report` through the proxy.

## Request body limit

justUp! rejects a `POST /settings/restore` body above the backup size limit (10 GiB plus
1 MiB framing) with `413` while the body streams in, before authentication, also without
`Content-Length`. Set the proxy limit as well so oversized uploads never reach the app.
Allow the backup size for the restore path only and keep the proxy default elsewhere:

```nginx
location = /settings/restore {
    client_max_body_size 10g;
    proxy_request_buffering off;
    proxy_pass http://127.0.0.1:8000;
}
```

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

    Set `TRUSTED_PROXIES=127.0.0.1` in the justUp! environment.

=== "Traefik (labels)"

    ```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.justup.rule=Host(`justup.example.com`)
      - traefik.http.routers.justup.entrypoints=websecure
      - traefik.http.routers.justup.tls.certresolver=le
      - traefik.http.services.justup.loadbalancer.server.port=8000
    ```

## Live updates (SSE)

Each browser tab keeps one Server-Sent Events stream at `/ui/events` with a
heartbeat every 15 s. justUp! sends `X-Accel-Buffering: no`; for other proxies,
disable response buffering for `/ui/events`. The common 60 s read timeout is
enough. If the stream is blocked, the page polls instead.

For restore uploads, set the proxy timeout to cover the upload and the restore.

!!! warning "Not verified"
    The event stream behind nginx was not tested against a real nginx. Check in
    the browser that events arrive without delay.
