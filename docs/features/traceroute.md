# Traceroute & Maps

The **Network diagnostics** tab of a target shows the route to the host: a route
timeline, metrics, a hop table, and an interactive map. The data loads when you
open the tab.

Traceroute (`mtr`) runs only on demand, from the tab or as part of a PDF report.
Three layers decide whether a new measurement happens:

| Layer | Lifetime | Purpose |
| --- | --- | --- |
| Browser cache | 24 hours | Reload, new navigation and browser restart show the stored result without any request to the traceroute endpoint |
| Server cache | 5 minutes | Protects against repeated `mtr` runs for the same target and host |
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

## Route timeline

First hop, intermediate hops, and destination appear once each, in hop order. The
first displayed hop is called **First hop**; it is not necessarily the machine that
ran the measurement. Unknown values show `—`, never invented zeros.

- On narrow containers the timeline is vertical; on wide ones it is a single
  horizontal track that scrolls.
- Three or more consecutive hops without map location, or consecutive estimated
  hops, collapse into one node such as `2–4`. Activate the node to expand it.
- Select a hop (click, tap, Enter, Space) to highlight its table row and map
  marker and show its details.

| State | Meaning |
| --- | --- |
| Measured | The hop replied (partial loss is stated) |
| No reply | Timeout |
| Estimated | ICMP-blocked hop between measured hops; values are interpolated |
| Not reached | Synthetic destination, because the target never answered |

A hop without map location is not a failed hop. It is marked "Location unknown"
and has no map marker.

## Map

The map is drawn in the browser with Leaflet and a base map served by justUp!. No
external tile service is used. Country flags load from jsDelivr; if that is blocked,
the table shows the two-letter country code.

## Location sources

justUp! picks one location per hop. Local sources come first; online services are
only a fallback (see [Online fallback](#online-fallback)). Sources:

- **POP detection** from the hostname (data from PeeringDB and UN/LOCODE, refreshed
  automatically; status at `GET /api/admin/pop/status`).
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
