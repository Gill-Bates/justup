# Best Practices

Recommendations for a secure operation of justUp!.

## Secrets

- **Generate and store `SECRET_KEY` securely.** Only in the environment, never in
  the repository. Loss means that encrypted secrets can no longer be read.
- **Rotate keys** via `SECRET_KEYS` (first = primary). See
  [Environment Variables](../configuration/environment.md).
- **Keep backups and keys separate.** A backup is not fully usable without the
  matching `SECRET_KEY`.

!!! danger "Rotate leaked secrets"
    If a `SECRET_KEY` or an admin password was ever committed to a shared
    repository, rotate the value and clean up the Git history. The
    example file `.env.example` must not contain a real secret.

## Operation behind HTTPS

- Place justUp! behind a reverse proxy and terminate TLS there.
- Enter the direct proxy address in `UPMON_TRUSTED_PROXIES`. justUp!
  trusts `X-Forwarded-Proto` and client-IP headers only from these peers; this
  sets HTTPS cookies with `Secure`.

See [Reverse Proxy](../configuration/reverse-proxy.md).

## Users and roles

- Grant admin rights sparingly. Most users need only read rights
  or the permission to acknowledge alerts.
- Change the initial admin password immediately after the first login.
- Do not use **Disable Authentication** except on an isolated network.

## External services

The external lookups (provider lookup, online geo, GeoIP update, and POP refresh)
can be disabled individually in the settings. Enable only what you
really need. The daily release check (api.github.com) has no switch. For GeoLite2, a license-compliant MaxMind source is required;
traceroute flags are loaded via jsDelivr and fall back to the
country code on failure.

## Updates

Keep the image up to date (`docker compose pull`). The runtime dependency state
is pinned with hashes in `docker/requirements.lock`; system tools and the base image
are set per release. Check the release notes for breaking changes
and required steps before the upgrade.
