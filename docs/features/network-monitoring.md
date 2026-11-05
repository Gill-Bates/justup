# Network monitoring with Nmap

justUp! monitors network services without agents and without any change on the target.
You enter a host, let [Nmap](https://nmap.org/) discover its ports, pick the ones to watch
or type them in, and justUp! checks exactly those endpoints from then on.

Nmap does the generic network work. justUp! does configuration, scheduling, status
evaluation, history, alerts and the UI. There are no service-specific health checks for
SMTP, FTP, SSH, DNS, Redis and so on.

## What is checked

| Check | How | Needs |
| --- | --- | --- |
| ICMP | The existing ping check (system `ping`), not Nmap. | `NET_RAW` (already required for ping) |
| TCP | A plain connect to each configured port, no Nmap and no extra rights. | nothing |
| UDP | A targeted Nmap UDP scan of exactly the configured ports. | The Nmap worker with `CAP_NET_RAW` |

ICMP, TCP and UDP can be combined on one target and are independent of the HTTP and
certificate checks. A port is identified by protocol **and** number: `TCP:53` and `UDP:53`
are two endpoints with separate status and history.

UDP is off for every target by default, including targets created before UDP support
existed. Existing TCP ports and ping settings are unchanged by the upgrade.

## Setting up ports

There are two ways, and you can mix them. A scan is never required to create a target.

### Automatic discovery

In the target wizard and the target editor, **Discover ports with Nmap** (collapsed by
default) opens the scan options. **Scan ports** starts an Nmap scan of the target host with TCP, UDP or both and one of two scopes:

| Scope | TCP ports | UDP ports |
| --- | --- | --- |
| Quick | 16 common ports (21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 3306, 5432, 8080, 8443) | 9 common ports (53, 67, 68, 123, 161, 162, 500, 514, 1900) |
| Extended | Quick plus 23, 111, 135, 139, 389, 445, 636, 1521, 2375, 27017 | Quick plus 69, 137, 138, 389, 4500, 5353 |

There is no full 65,535-port scan, no NSE script, no OS fingerprinting and no traceroute.
Discovery uses service detection (`-sV --version-light`) so a result can show the
service Nmap recognised.

The scan runs as a background job. The page stays usable and shows an activity indicator,
not a progress percentage, because Nmap gives no reliable progress. A scan can be
cancelled; a cancelled or expired job never delivers late results.

After a scan a summary counts the results (for example "16 ports found: 1 confirmed open,
2 closed, 13 unclear") and the results fall into two groups:

- **Confirmed open** lists the ports Nmap found open (`open`). They are selected for you,
  but not added yet. Change the selection if you like and choose **Add selected ports (N)**.
- **Other results** (for example "15 other results – not confirmed open") stay collapsed.
  They hold ports Nmap found closed and ports whose state it could not determine
  (`filtered`, `open|filtered`, `closed|filtered`, `unfiltered`). Each result shows its Nmap
  state in words. These ports are never selected automatically; you can tick them yourself.
- A service marked "(table hint)" is the usual service of that port number, not a detection.
- **Add selected ports** merges the choice into the port list without duplicates and keeps
  ports you typed by hand. Nothing is stored until you save the form.
- If no port is confirmed open, the page says so and does not claim that the other ports
  are closed.

Which service Nmap named (and whether it was identified by a probe or only guessed from the
port table) is stored with the port when you save. The server takes this from its own
completed scan job, not from the browser, so it cannot be forged by a client.

### Manual configuration

In the wizard and the editor, one input takes single ports and lists in the range 1-65535,
separated by commas or spaces. A port is TCP unless you write `udp:53` or `53/udp`. The
ports appear as removable chips (`TCP 22`, `UDP 53`) below the input. A manually entered port has no service name and is monitored like any other.

## Public outbound IPs

Settings > Network > **Public outbound IPs** shows the IPv4 and IPv6 address that external
services observe when justUp! connects out. It answers "which address do my monitors and
the services I probe see?", for example to allow-list the server elsewhere.

This is not the address you use to reach justUp!. The incoming server address depends on
`JUSTUP_HOST`, published ports and your reverse proxy. The outgoing address is chosen by the
routing of the container and its host.

### How it is determined

justUp! opens one HTTPS connection per address family and provider and reads the plain-text
answer. Nothing is taken from local interfaces, from the incoming request or from proxy headers.

| Family | Providers | Operator |
| --- | --- | --- |
| IPv4 | `ipv4.icanhazip.com`, `api4.ipify.org` | Cloudflare, ipify |
| IPv6 | `ipv6.icanhazip.com`, `api6.ipify.org` | Cloudflare, ipify |

Why these services: HTTPS with certificate and host-name verification, separate host names
that resolve only to A or only to AAAA records (so a family cannot silently fall back to the
other), a bare-text answer that is parsed only as an IP address, and two unrelated operators
that can be compared. The list is fixed; no URL is configurable.

Rules the lookup follows:

- The family is forced per lookup. A failed IPv6 lookup never shows an IPv4 address.
- The host name is resolved once, every answer must be a public address, and the connection
  goes to that validated address. Redirects are not followed.
- An answer must be a public address of the requested family. Private (RFC 1918), CGNAT, ULA,
  link-local, loopback, documentation, IPv4-mapped and NAT64 addresses are discarded, so a
  Docker bridge address is never shown as a public address.
- The answer is limited to 256 bytes and the whole lookup to about 8 seconds. Resolver and
  connection work runs in small bounded pools; while earlier resolver calls are still hanging
  new lookups fail fast with "Earlier lookups are still running" instead of queueing.
- The lookup is direct. `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY` are not used; if one is set,
  the card says so, because HTTP monitors that use it may leave via another address.
- Results are held in memory for 300 seconds, never stored. Concurrent requests share one
  lookup. **Refresh** asks for a new measurement, but within 10 seconds of the last one the
  cached result is returned and labelled "Cached result, not re-measured".

### States

| State | Meaning | Address shown | Copy |
| --- | --- | --- | --- |
| Available | One or both providers agree | yes | yes |
| Stale | Not confirmed now; the last confirmed address (up to 24 hours old) with the time it was confirmed | yes | no |
| Inconsistent | Providers report different addresses (for example multi-WAN or policy routing) | all observed values as text | no |
| Unavailable | No route or no DNS record for this family (for example no IPv6) | no | no |
| Lookup failed | Timeout, TLS or answer error, resolver busy | no | no |
| Checking | A lookup is running; a previous address stays visible | previous, if any | no |

IPv4 and IPv6 are independent: an IPv6 failure does not hide the IPv4 address. If the
request to justUp! itself fails (HTTP error, timeout), the last address shown is kept as
*Stale* with its time and an error text.

### Privacy

To find the addresses, icanhazip.com and ipify.org see the public egress address of the
server and nothing else. justUp! sends no host name, user data or target. The check runs when
the Network tab opens and on **Refresh**; there is no polling and nothing is stored. The
endpoint `GET /settings/network/public-egress` is admin-only, limited to 12 requests per
minute and sent with `Cache-Control: private, no-store`.

### Limits of the result

The card shows what these providers observe now. NAT, Docker bridge networking, IPv6
routing, policy routing, multiple WAN links, VPNs and proxies can give other destinations a
different address. IPv6 may be missing on a Docker network by default (see
[Docker](../getting-started/docker.md#ipv4-and-ipv6)).

## Status values

Each endpoint has a **transport status**:

| Value | Meaning |
| --- | --- |
| `PORT_UP` (`up`) | The port was reached (TCP connect succeeded, UDP state `open`). |
| `PORT_DOWN` (`down`) | The port was confirmed unreachable (TCP refused or timed out after the retries, UDP state `closed` in two consecutive scans). |
| `PORT_UNKNOWN` (`unknown`) | No reliable result: an uncertain UDP state, a failed scan engine, a stale or missing result. |

The wire and database values are lower case; the UI shows `Up`, `Down` and `Unknown` with
different icon shapes, so colour is never the only cue.

The **service identity** is a separate dimension and never changes the transport status:

| Value | Meaning |
| --- | --- |
| `SERVICE_IDENTIFIED` | A probe identified the service (name from an active Nmap probe). |
| `SERVICE_UNKNOWN` | Not identified. A name that only comes from the port table (`nmap-services`) is shown as a hint and does **not** count as identified. Manual ports are unknown. |
| `SERVICE_CHANGED` | An explicit re-detection found a different service name or product than the stored one. Re-detection only proposes the change; a confirmed change becomes the new `SERVICE_IDENTIFIED` base. A changed version string alone is a note, not a change. |

A changed service never turns a port down and never raises a DOWN alert.

### Service re-detection

An administrator can run a fresh service detection (`-sV`) for a target with **Detect services**
in the network diagnostics of the target page. Nothing is stored by the scan itself.

1. **Preview** (`POST /api/network-scans/services/{id}/preview`) scans the configured ports and
   lists what would change, grouped as **Service changed** (new or changed service name or product)
   and **Version only**. Nothing is stored. Only one detection per target runs at a time; a second
   request meanwhile answers `409`.
2. **Apply changes** asks in the shared confirmation dialog; Cancel and Escape send nothing and keep
   the preview. **Confirm** (`POST /api/network-scans/services/{id}/confirm`) stores the previewed result. A new
   service or a changed service name or product becomes the stored identity (a change then
   becomes the new `SERVICE_IDENTIFIED` base). A version-only change only updates the stored version.

The confirmation token is single-use, bound to the administrator and the target, and valid for
5 minutes. Confirm answers `409` if the target or a stored service changed since the preview or the
preview expired, and `404` if the token is unknown, already used or no longer stored. Preview allows 5 requests per minute per IP and 2 per minute per user,
confirm 10 per minute per IP and 5 per minute per user.

UDP ports are scanned only against the central host (`host`), never against a separate TCP host.

### Host change

When the scan host of a target changes, the stored port observations no longer describe it.
TCP rows are reset when the effective TCP host (`tcp_host`, or `host` if empty) changes; UDP rows
are reset when `host` changes. A change of `host` also resets the target status.

### Overall target status

A target is `down` if at least one enabled endpoint is confirmed down. Otherwise it is
`unknown` if at least one endpoint is unknown, otherwise `up`. Unknown endpoints do not
cause DOWN alerts and do not send recovery messages. Endpoints that were measured keep
their own status on the target page even when the target as a whole is `unknown`.
The UDP aggregate follows the same rule: one confirmed `down` port makes it `down`, all ports `up` make it
`up`, anything else is `unknown`. History and reports get a `udp_up` point only when the aggregate is known;
an unknown cycle leaves a gap, not downtime.

## Uncertain UDP results

UDP has no handshake. Silence can mean an open port that does not answer, a filtering
firewall or a lost packet. Nmap reports this honestly, and justUp! maps the Nmap state
like this (the original Nmap state is stored unchanged next to the status):

| Nmap state | Status | Downtime |
| --- | --- | --- |
| `open` | `up` | no |
| `closed` | `down`, once confirmed by two consecutive scans | yes, after confirmation |
| `open\|filtered` | `unknown` | never |
| `filtered` | `unknown` | never |
| `closed\|filtered`, `unfiltered`, unknown state, port missing in the result | `unknown` | never |

`open|filtered` is **never** treated as up and never as down. Typical open UDP ports of
services that ignore Nmap's empty probe, such as many DNS or NTP servers, therefore often
stay `unknown`. A single `closed` scan keeps the previous status (or `unknown`) until the
next scan confirms it.

A failure of the scan engine (worker not running, missing privileges, crash, invalid
result, timeout) says nothing about the target. All UDP ports of that run become
`unknown`, never `down`, and the scheduler carries on.

## Downtime, gaps and unknown

- **Downtime** counts only from confirmed `down` measurements. An outage ends at the next
  `up` measurement; when measurements are missing in between, it ends at its last `down`
  measurement. A gap is never downtime and never uptime.
- A **gap** is a distance of more than three expected check intervals between two
  measurements of the same endpoint (for example after a restart or a long pause).
  UDP series are spaced by the UDP interval.
- **Unknown** phases are neither downtime nor uptime. The downtime history of the target
  page shows three kinds, told apart by text and icon: *Downtime*, *No measurements* and
  *Unknown*. Only *Downtime* counts.
- The uptime percentage is based on the measurements that exist. Gaps and unknown phases
  carry no samples and are excluded from numerator and denominator.
- Outages shorter than 60 s are treated as transient and not listed as downtime.
- Endpoint history is kept per protocol and port. Disabled checks and removed ports create
  no downtime.
- Alerts follow the existing rules (cooldown, confirmation window). UDP needs two
  consecutive `closed` scans before it can alert, and an unknown result resets the pending
  confirmation, so single UDP timeouts do not flood your recipients. The alert text names
  the failing endpoint (for example `udp:123, tcp:25`).

## Intervals and limits

| Item | Value |
| --- | --- |
| TCP check | Every target interval (5 s to 24 h), plain connect. |
| UDP scan | Every `max(target interval, 300 s)`. A scan never overlaps the previous one of the same target; a skipped cycle is logged. |
| UDP result age | A result older than two UDP intervals counts as `unknown`. |
| UDP scan timeout | The smaller of 60 s and half the UDP interval (at least 5 s). |
| Parallel UDP scans | 2 across all targets. A slow scan never delays the other checks of its target. |
| Monitoring scan | Only the configured ports, no service detection, no extra ports. |
| Discovery scan (API) | At most 128 ports; 90 s (Quick) or 180 s (Extended) timeout; 2 Nmap runs at once; 1 active scan per address; 4 active jobs per administrator and 16 in total. |
| Scan job lifetime | 15 min, then `expired` for 5 more minutes, then removed. |
| Rate limit | 10 scan starts per minute per IP and 3 per minute per user. |

Service detection (`-sV`) runs during discovery and on explicit administrator re-detection, never in a monitoring cycle.

## Limits compared with application health checks

An open port proves **transport reachability** and nothing more.

- *Port up* means the operating system accepted the connection (TCP) or Nmap saw an
  `open` answer (UDP). The application behind it can still be hung, misconfigured or
  returning errors.
- *Service identity* from Nmap is additional evidence of what answered when the port was
  scanned. It is not a function test.
- justUp! does not log in, send mail, resolve names or run queries. If you need that, use
  the HTTP check or a dedicated probe next to this feature.
- A middlebox, proxy or load balancer can accept connections on behalf of a dead service.
- UDP results are inherently weaker than TCP results (see above).

## IPv4 and IPv6 in checks

Established by reading the code and by running checks on a host without a global IPv6
route (only `fe80::/64`):

- IPv6 literals work: TCP and certificate checks report `network unreachable`, ping `unreachable`
  and HTTP `All connection attempts failed`; the `Host` header brackets the literal and drops a
  zone id. Nmap and traceroute add `-6` for an IPv6 target.
- IPv4-mapped IPv6 addresses (`::ffff:a.b.c.d`) are judged by their IPv4 part in the SSRF
  guard, so `::ffff:10.0.0.1` is treated like `10.0.0.1`.
- For a host name, the public-DNS lookup prefers A before AAAA (queried over the IPv4
  resolvers 1.1.1.1 and 9.9.9.9), followed by the system resolver's addresses. Ping, TCP and
  TLS use the first pinned address; HTTP tries the candidates in order.
- Observation, not changed: only `ENETUNREACH` (101) is labelled `network unreachable` and
  retried as transient. `EHOSTUNREACH` (113) appears as `[Errno 113] No route to host` and is not
  treated as retryable. No failing behaviour was reproduced for it.

## Docker image and Nmap version

- The image contains the **latest stable Nmap release at build time**. There is no
  pinned version number in the repository; a new Nmap release means a new image build.
  Betas, nightlies, Debian's `nmap` package and third-party downloads are not used, and
  Nmap is not updated inside a running container.
- The release workflow resolves the version once (`tools/nmap-release.py latest`),
  downloads the source archive, verifies its PGP signature against a fixed Nmap signing-key
  fingerprint and records the SHA-256 of the archive. A failed verification stops the build.
  Both architectures are then built from that same version and archive hash, which the
  Dockerfile checks again (`NMAP_VERSION`, `NMAP_SOURCE_SHA256`, both mandatory and part of
  the BuildKit cache key, so an old cached binary can not be reused).
- `linux/amd64` and `linux/arm64` are both **compiled natively from the verified source**
  with identical configure options. The official x86-64 RPM is deliberately not used: it is
  a statically linked vendor build with its own OpenSSL, which would make the two
  architectures differ. Compiler and build tools exist only in a builder stage; the
  runtime image gets the `nmap` binary and the four data files (`nmap-services`,
  `nmap-service-probes`, `nmap-protocols`, `nmap-rpc`) through three explicit `COPY`
  lines. There is no Zenmap, Ncat, Nping or NSE.
- Each image carries build metadata in `/usr/share/doc/nmap/PROVENANCE.txt`:
  `nmap_version`, `source_url`, `source_sha256`, `signing_key_fingerprint`,
  `architecture`, `build_method` (`verified-source`), `binary_sha256` and
  `runtime_dependencies`. Read it with:

  ```bash
  docker run --rm --entrypoint cat giiibates/justup:<version> /usr/share/doc/nmap/PROVENANCE.txt
  ```

- The release gates check, per architecture, that `nmap --version` equals the resolved
  version, that the provenance matches, that TCP and UDP scans produce parseable XML, that
  the worker runs with the Compose capability set and that both architectures report the
  same version and source hash. See [Release image](../development/release-image.md).

!!! warning "Not verified here"
    The `linux/arm64` build and its tests run only in the release workflow on a native
    arm64 runner; they were not run on the development host. Trivy's vulnerability scan
    does not see the Nmap binary (it has no package database entry); it covers only the
    base libraries.

## Linux capabilities

- UDP scans send raw packets and need `CAP_NET_RAW`. The web process (`uvicorn`) does **not**
  get that capability and never starts Nmap.
- In the Docker image, the entrypoint starts a separate **Nmap worker** under its own user
  (`justup-nmap`, UID/GID 10001) with only `CAP_NET_RAW` as effective, permitted and ambient
  capability. The web process talks to it over a Unix socket (`/run/justup/nmap.sock`).
  If the worker dies, a small supervisor restarts it with growing delays and gives up
  after 8 consecutive failures (the web server keeps running).
- The Compose file keeps `cap_drop: ALL` and adds only `CHOWN`, `FOWNER`, `SETUID`,
  `SETGID` and `NET_RAW`. `NET_RAW` was already needed for ping and traceroute.
  `no-new-privileges` stays on and there is no `privileged: true`.
- UDP scans run with Nmap's `--privileged` flag in a fixed argument list so Nmap uses the
  capability instead of checking for UID 0. TCP needs no extra rights.
- Without `NET_RAW` in the container, UDP scans fail with `permission_denied` and the UDP
  ports show `unknown`.
- Outside Docker (`python run.py`) justUp! starts its own unprivileged worker. TCP
  discovery works there; UDP needs raw-socket rights that the process does not have.

## Security and operations

- **Only administrators** can start scans. Jobs belong to the administrator who started
  them; other users get `404`.
- **Authorized hosts only.** Scan only hosts you are allowed to scan. Port scans can
  look like an attack to the target's owner and to intrusion detection.
- The host passes the same validation as a check target (loopback, link-local and cloud
  metadata addresses are blocked; private RFC 1918 ranges are allowed). The name is
  resolved once and Nmap receives only the validated IP address, so DNS rebinding between
  validation and scan is not possible.
- **No free Nmap arguments.** The API accepts only a host, the protocols and the scope. It
  rejects extra fields, ports, options, ranges, CIDR blocks and host lists. The command
  line is built on the server from a fixed list and started without a shell.
- Nmap's XML output is parsed with `defusedxml` (entities and external references
  refused, 1 MB output cap). Nmap texts are shown as plain text, never as HTML.
- Nmap's raw output and error text are not echoed into normal logs or API errors.
- A scan-engine failure never crashes the application and never marks a port down.

## Troubleshooting

??? failure "Scan ports is disabled: the Nmap scan worker is unavailable"
    The form shows the reason. Common causes: `nmap` is not installed (outside Docker:
    `apt install nmap`), the worker is restarting, or it gave up after repeated failures.
    Check the container log for the supervisor messages and restart the container. Ports can
    still be entered by hand. The form re-checks when the page regains focus.

??? failure "UDP scans report permission_denied"
    The worker lacks `CAP_NET_RAW`. Keep `NET_RAW` in `cap_add` and do not remove it with
    a stricter `cap_drop` or a custom security profile. Outside Docker, the process cannot
    send raw packets.

??? question "A UDP port shows Unknown"
    Nmap saw `open|filtered`, `filtered` or no answer. This is expected for many UDP
    services and is never treated as downtime. The target page shows the Nmap state next to
    the status. If you need a definite answer, check the service with a protocol-aware tool
    or add an HTTP check.

??? question "A scan says busy (429)"
    A scan for this host is already running, or the limits above are reached. Wait a
    moment or cancel the running scan.

??? question "The scan timed out or produced too much output"
    Use the Quick scope, scan only TCP or only UDP, or enter the ports manually. UDP
    scans are slow by design.

??? question "The scan result expired"
    Results are kept for 15 minutes. Scan again, then adopt the ports before saving.

??? question "The downtime list shows 'No measurements' or 'Unknown'"
    The scheduler was paused or restarted, a scan could not run, or the result was
    uncertain. These periods are neither downtime nor uptime.

??? question "Image check: which Nmap version is inside?"
    Run the `docker run ... cat /usr/share/doc/nmap/PROVENANCE.txt` command above, or
    `docker exec <container> nmap --version`.

## Nmap licence

Nmap is distributed under the [Nmap Public Source License (NPSL)](https://nmap.org/npsl/)
and is a registered trademark of Nmap Software LLC. Nmap is **not** covered by justUp!'s MIT
licence.

The justUp! image redistributes the unmodified upstream Nmap build described above. The
licence text and a notice are included in the image in `/usr/share/doc/nmap/LICENSE` and
`/usr/share/doc/nmap/NOTICE.txt`. The notice names the exact release and its upstream
source archive, for example `https://nmap.org/dist/nmap-<version>.tar.bz2`; the
`source_url` and `source_sha256` in `PROVENANCE.txt` identify the same archive.
The build applies no patches, so the source offered by Nmap is the corresponding source.

!!! note "Open decision"
    The licence gate for redistributing Nmap in the public image was cleared by the
    project owner. Whether the in-image notice plus the upstream source link is enough for
    the project's own source-availability policy (for example mirroring the archive next
    to the image) is not decided here.
