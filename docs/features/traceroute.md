# Traceroute & Maps

The **Network diagnostics** tab of a target shows the route to the host: a route
timeline, metrics, a hop table, and an interactive map. The data loads when you
open the tab.

Traceroute runs through the Nmap worker (`nmap --traceroute`) only on demand, from the tab or as part of a PDF report.
Three layers decide whether a new measurement happens:

| Layer | Lifetime | Purpose |
| --- | --- | --- |
| Browser cache | 24 hours | Reload, new navigation and browser restart show the stored result without any request to the traceroute endpoint |
| Server cache | 5 minutes | Protects against repeated traces for the same target and host |
| New scan | on demand | The refresh icon (**Run new scan**) always measures again |

- **Automatic scan.** A stored result is reused for 24 hours after the actual
  measurement, not after the last display. Once it has expired, the next visit
  starts exactly one new scan. An open page does the same when it becomes
  visible again; a hidden tab never scans, and switching between Dashboard and
  Network diagnostics never reloads. Several browser tabs coalesce into one scan
  where the browser supports Web Locks or `localStorage` leases.
- **Run new scan.** The refresh icon starts a scan at any time and bypasses both
  caches. The server still allows one scan per target and host at a time and
  rejects a repeat within 15 seconds (HTTP 429, honoured by the page).
- **Failures.** A failed or rejected scan keeps the previous result and its time.
  After a failed automatic scan the page shows *Outdated – new scan failed* and
  does not retry on its own for five minutes.
- **Time shown.** *Last scanned* is the measurement time reported by the server
  (`X-Measured-At`, epoch milliseconds, also on a server-cache hit). It follows the
  *Show dashboard times in UTC* setting. A stamp more than five minutes ahead of
  the browser clock is replaced by the browser time, so a wrong client clock
  shifts the 24 hours by that offset at most.

Only successful measurements are stored. The browser cache is separated by user
and target, and by the target's route destination: editing the host or re-creating
a target under a reused id starts a new entry. Entries are kept as rendered
fragment HTML in `localStorage` (at most 12 entries of 256 KiB; expired ones are
dropped after 7 days) and inserted like a live response. Without usable storage
the page works as before and keeps the result in memory only.

!!! note "Privacy"
    A stored route can contain internal IP addresses and host names. It stays in
    the local browser profile only, holds no tokens or credentials, and is
    removed on logout and when another user opens a target on the same browser.
    Entries of deleted targets are removed by the size and age limits.

## Method and limits

- **Probe order.** The first attempt is Nmap host discovery with its default probes
  (`-sn --traceroute`). Only if that yields no trace (no trace found or time-out) does
  one fallback run with a single port probe (default TCP 443) follow. A TCP or UDP probe
  requested by the caller runs first, alone. There is never a third attempt.
- **Privileges.** Nmap runs with `--privileged` and needs `NET_RAW`. The worker
  receives only that capability; without it the page reports a permission error.
- **Limits.** At most 30 hops. One shared deadline covers name resolution and all
  attempts. Two traces run at a time; a further request is refused as busy.
- **Values.** Each hop has a single round-trip time as reported by Nmap. There are no
  loss, average, best or worst statistics. Hops that do not answer are shown as
  *No reply*. IPv4 and IPv6 are supported; a host name is resolved once and that
  address is traced.
- **Without Docker.** A local worker started by the application has the same limits.
  It needs an `nmap` binary and raw-socket rights (root or `cap_net_raw`); otherwise
  traceroute is unavailable.

## Measured behaviour
Measured in a three-router lab (network namespaces, IPv4 and IPv6) with Nmap 7.993 from the
runtime image under the Compose capability profile, against `mtr --json -n -c 3 -m 20` 0.95
(the removed tool). 10 runs per cell (5 for the last two rows), one trace at a time, p50 in
seconds. The lab is a clean, low-latency path; absolute times on the internet differ.

| Scenario | MTR p50 | Nmap p50 | Route found |
| --- | --- | --- | --- |
| Clean path, 3 hops | 7.8 | 0.2 (IPv6), 0.6 (IPv4)¹ | both: all 3 hops |
| Router without *time exceeded*, target drops ping | 8.6 | 2.1 | Nmap: 2 hops, destination reached. MTR: 1 hop, destination not reached |
| Target also drops all TCP and UDP | 8.6 | 2.1 (IPv4), 3.1 (IPv6, no trace) | IPv4: Nmap reached the destination; IPv6: the first attempt found no trace, the fallback (through the worker) found 1 hop after 11.2 s |
| Target down | 9.0 | 3.1 (no trace), 11.2 with the fallback | MTR shows the routers up to the failing one; plain Nmap often shows nothing |

¹ Back-to-back IPv4 runs hit the kernel's ICMP rate limit (one *time exceeded* per second), so
some Nmap runs missed a hop and were retried; the IPv6 runs did not.

- **Faster** on a healthy path (about 0.2–0.6 s against 7.8 s), because Nmap sends its probes
  once instead of three rounds per hop. **Slower** when the target stays silent: after the
  first attempt returns nothing, the fallback needs up to about 11 s; MTR always needs 8–9 s.
- **More robust** when a router or the target blocks ping: the discovery probes still found
  the destination where MTR's ICMP probes did not.
- **Probe volume.** Per trace Nmap sent 15–27 packets and MTR 18–59 (counter delta on the
  client interface, includes unrelated background traffic of the container).
- **Concurrency.** Two traces run in parallel; the third is refused as *busy* by design (5 of
  15 requests in a three-parallel run). Nmap is one short process per trace (about 27 MB peak
  RSS).
- **Lost compared with MTR:** per-hop loss and average, best and worst round-trip time. A
  hop's single RTT is one sample and says nothing about loss or stability.
- **Worker overhead:** about 0–0.5 s over a plain Nmap call (socket, validation, XML parsing).
- **Not measured:** public IPv6 (the Docker bridge has no IPv6 route to the internet), arm64,
  and IPv6 with three parallel traces.

If the silent-target case matters, a tuning option would be a shorter budget for the first
attempt (it currently takes 40 % of the request time); this was not implemented.

## Route timeline

First hop, intermediate hops, and destination appear once each, in hop order. The
first displayed hop is called **First hop**; it is not necessarily the machine that
ran the measurement. Unknown values show `—`, never invented zeros.

- On narrow containers the timeline is vertical; on wide ones it is a single
  horizontal track that scrolls.
- Three or more consecutive hops without reply, or consecutive hops without map
  location, collapse into one node such as `2–4` ("3 hops without reply").
  Activate the node to expand it. The hop count comes from the measured hop
  numbers, not from an estimate.
- Select a hop (click, tap, Enter, Space) to highlight its table row and map
  marker and show its details.

| State | Meaning |
| --- | --- |
| Measured | The hop replied |
| No reply | The hop did not answer. This includes silent hops between measured hops (for example ICMP blocked). No address, host name, latency or location is shown, and nothing is estimated for it |
| Not reached | Synthetic destination, because the target never answered |

A hop without map location is not a failed hop. It is marked "Location unknown"
and has no map marker.

## Map

The map is drawn in the browser with Leaflet and a base map served by justUp!. No
external tile service is used. Country flags load from jsDelivr; if that is blocked,
the table shows the two-letter country code.

The map shows one continuous chain from the first mappable hop to the destination,
in hop order. Every located hop is a node, including approximately located hops
(hollow marker with "~"). A link between two reliably located hops is a solid line.
A link is dashed when at least one end is estimated or approximate, or when hops in
between did not answer or have no location; the hop numbers it skips are named in
its tooltip. Nothing is estimated for a skipped hop: it gets no marker and no
coordinates. A real geographic back-jump stays visible, and a back-jump caused by an
approximate position is recognizable by its dashed link. The PDF report draws the
same chain from the same data.

## Location sources

justUp! picks one location per hop. Local sources come first; online services are
only a fallback (see [Online fallback](#online-fallback)). Sources:

- **POP detection** from curated hostname-code mappings and explicitly reviewed
  learned aliases. The PeeringDB refresh stores city-level facility evidence but
  never creates a hostname alias from a facility alone. Refresh status is at
  `GET /api/admin/pop/status`; UN/LOCODE import remains a placeholder.
- **GeoLite2-City** (offline). Without the database, traceroute still works, but
  such hops stay off the map. A GeoLite2 point is plotted only when its accuracy
  radius is at most 25 km; otherwise the location stays approximate.
- **ipwho.is** and, with a key, **ip-api Pro** (online fallback only). Their
  coordinates have no published accuracy and are never plotted as precise points.

### Online fallback

GeoLite2 is queried first. Its result counts as high quality when it has valid
coordinates, a real city (not only a region), a country code and an accuracy radius
of at most 25 km. The 25 km limit is a justUp! product decision
(`MAX_CITY_RADIUS_KM`), not an official MaxMind limit: MaxMind's radius only means
that the true location lies inside it with roughly 67 % confidence. It is to be
revisited with real traceroute data.

| GeoLite2 result | Quality | Online call |
| --- | --- | --- |
| City + country, radius up to 25 km (25.0 included) | high | no |
| City + country, radius above 25 km | limited | yes |
| City + country, no radius | not evidenced | yes |
| Region/state only | coarse | yes |
| Country only | very coarse | yes |
| No valid coordinates | unusable | yes |

A POP/CLLI location with verified confidence counts as high-quality local evidence
and also avoids the online call. A city only guessed from a hostname does not.

Unknown codes seen on on-demand traceroutes are stored as review candidates, not
used for location lookup. The local GeoLite2 city/country suggestion is coarse
evidence, not an exact router position. Reprocessing the same route does not add
another observation, and addresses in one provider/network context do not count
as independent confirmations. Candidate capture adds no external geolocation
request. See [PoP candidate review](../development/database-maintenance.md#pop-candidate-review)
for the explicit review workflow.

Only public IPs are ever sent to ipwho.is / ip-api Pro (HTTPS), subject to cache and
backoff; private and non-global addresses never are. **Online fallback**
(`online_geo_fallback_enabled`, Settings > Network > Geolocation) switches the
fallback off completely. It is read on every lookup, so a change applies at once
and needs no restart.

An online result has no evidenced accuracy, so it never replaces a local position.
For a coarse local result (region or country only) it may supply the place name
when it agrees on the country and lies near the local position; coordinates, source
and accuracy class stay local (`place_source` in the decision marks this text-only
use). Without any local position, the online position is used and stays
approximate. Contradicting sources are reported and the hop stays approximate.

The online lookups of one route run concurrently (4 workers, one request per
distinct IP, 10 s budget per route). Offline results are available immediately;
IPs not answered in time keep their local result. Provider lookup (ASN/ISP) is
independent of this fallback.

Without a usable source the location stays unknown; justUp! does not guess. If two
well-evidenced sources are more than 250 km apart, the hop is shown as
approximate. Provider lookup (ASN/ISP) is separate and does not change the location.
A provider that rate-limits or fails is skipped for a while; GeoLite2 remains the
local fallback.

!!! warning "GeoLite2 license"
    The automatic update downloads `GeoLite2-City.mmdb` from the P3TERX GitHub
    mirror (`P3TERX/GeoLite.mmdb`), not from MaxMind. Make sure your use complies with the
    MaxMind GeoLite2 license. See [Settings](../configuration/settings.md).
