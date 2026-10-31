# Traceroute & Maps

On the target detail page, justUp! shows an interactive traceroute map with
geographic hops. The map helps make the network path to a host and possible
bottlenecks visible.

The view is located in the target under the **Network diagnostics** tab. The
previous dashboard stays in the **Dashboard** tab; the diagnostic data is loaded
only when the network tab is opened.

Successful traceroutes are cached in the app process's memory for five minutes
per target and host. A reload within this time shows the result again, without
running MTR again. The permission is rechecked on every request; the browser does
not cache the fragment.

## Network diagnostics in the target

The **Network diagnostics** tab shows a source-destination overview, metrics,
the hop table, and the map. The metrics use measured destination data;
unknown values appear as `—` and not as invented zero values. The hop count also
accounts for aggregated ICMP-blocked hops.

The sequence bar follows the hop order. Non-geolocated hops stay visible but
receive no invented map position. Map markers carry the hop numbers, arrows show
the direction of travel; repeated locations combine their hop numbers into a
single marker.

**Run again** forces a new measurement and refreshes the cache. The button shows
the loading, success, or error state; simultaneous or too-frequent manual
measurements are rate-limited. There are still no periodic traceroutes.

## When traceroute runs

MTR/traceroute runs **only on demand** – via the detail page or as part of a PDF
report. There is no periodic execution.

## Map rendering

!!! info "Local map rendering"
    The map is rendered exclusively client-side with the locally served
    **Leaflet**. The backend delivers only geo and route data (hops with
    coordinates and segment type). The base map consists of locally served
    Natural Earth country outlines (1:110m, public domain). There is no external
    map, tile, or static-map API and no server-side map rendering. The optional
    online lookup described below supplies geolocation data, not map tiles.

Flags next to the hops come as SVG from jsDelivr (`flag-icons` 7.3.2). If the
CDN request fails or is blocked, the table shows the two-letter country code. The
base map itself is loaded locally.

## Geo data

- **POP-code detection** from hostnames is preferred when a known network POP is
  identified (provider-specific parsers, token
  matching, prefix match, embedded fallback table). The data basis comes from
  PeeringDB and UN/LOCODE with a monthly refresh; an admin view shows status and
  runs.
- **GeoLite2-City** supplies offline locations. A missing database does not stop
  traceroute. Hops without a reliable location remain off the map. A city name
  and coordinates alone do not prove an exact router location. justUp uses the
  MaxMind accuracy radius and plots GeoLite2 city points only when that radius is
  at most 25 km. Wider or missing radii remain approximate.
- **Online Geo Lookup** is off by default. When enabled, it sends public hop IPs
  over HTTPS to ip-api Pro if a key is configured, with ipwho.is as a fallback.
  Without a key, it uses ipwho.is. Private and reserved addresses stay local.
  A complete online city can fill the hop table when GeoLite2 has no city.
  Online coordinates have no published accuracy radius and are not plotted as
  precise route points, so the map may remain empty without GeoLite2 or POP data.
  The online providers do not publish comparable independent accuracy scores,
  and their underlying datasets can overlap; agreement between them is not
  treated as independent confirmation.
- **Provider lookup** supplies ASN/ISP metadata separately and can be disabled
  independently. It uses the same HTTPS provider order and a bounded cache. For
  report locations, GeoLite2 wins when its city has a radius of at most 25 km;
  a complete online result fills a missing or country-only location. Reports show
  the selected data source and the accuracy radius when available.

!!! warning "License of the geo database"
    GeoLite2 is obtained only from a license-compliant source (MaxMind account
    with a license key) and verified against a checksum. The external geo and
    provider lookups can be disabled individually – see
    [Settings](../configuration/settings.md).
