#!/usr/bin/env python3
#
# app/services/traceroute_geo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Geolocation service for traceroute hops using MaxMind GeoLite2.

This module provides functionality to geolocate IP addresses from traceroute
results. It only produces geo/route data; there is no server-side map
rendering. The Web UI draws the route client-side with the vendored Leaflet
(app/static/js/traceroute-map.js) on a local Natural Earth basemap, and the
PDF report shows the hops as a table and as an offline route map
(app/services/pdf_route_map.py).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import ipaddress
import itertools
import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

from app.services import geo_resolver as gr
from app.services import ixp_index
from app.services.geo_resolver import MAX_CITY_RADIUS_KM
from app.services.ip_lookup import lookup_online_sources, public_ip
from app.services.pop_codes import lookup_pop_code, observe_unknown_pop
from app.services.traceroute_service import STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT
from app.utils.constants import (
    DEFAULT_ONLINE_GEO_FALLBACK_ENABLED,
    GEOIP_DB_RELPATH,
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    PROVIDER_LOOKUP_CACHE_TTL_MAX,
    PROVIDER_LOOKUP_CACHE_TTL_MIN,
    GeoAccuracy,
    GeoSource,
    HopStatus,
    SegmentType,
    SettingKeys,
)
from app.utils.runtime_settings import get_ip_api_key, get_runtime_bool_fail_closed, get_runtime_int

_log = logging.getLogger(__name__)

# Re-export segment types for backward compatibility
# Use SegmentType.SOLID/.DASHED directly where possible
SEGMENT_SOLID = SegmentType.SOLID
SEGMENT_DASHED = SegmentType.DASHED

# Re-export hop status for backward compatibility
# Use HopStatus.OK/.TIMEOUT/.INTERPOLATED directly where possible
STATUS_INTERPOLATED = HopStatus.INTERPOLATED

# Metadata key constants (avoid magic strings)
META_PRIVATE_IP = "_private_ip"
META_IS_PUBLIC = "_is_public"
META_DESTINATION = "_destination"
META_INTERPOLATED = "_interpolated"
META_HOP_RANGE = "_hop_range"
META_CONFIDENCE = "_confidence"
META_GEO_UNAVAILABLE = "_geo_unavailable"  # Destination reached but no geo coords
META_IS_TERMINAL = "_is_terminal"  # Explicit end-of-route signal for renderer
META_IS_IPV6 = "_is_ipv6"  # Mark IPv6 addresses for special handling
META_MAP_ROLE = "map_role"  # how a renderer draws the point: MAP_ROLE_*
META_LOC_NOTE = "loc_note"  # source/uncertainty text of an approximate position, for tooltips
META_SEGMENT_NOTE = "segment_note"  # hops the segment to the next point crosses without a map position
META_MAP_RADIUS_KM = "map_radius_km"  # accuracy radius a renderer may draw around an approximate position
MAX_MAP_RADIUS_KM = 250.0  # larger radii would cover the map and are not drawn

# IPv6 specific constants
# IPv6 addresses are long and cause layout issues in PDFs/tables
# We shorten them for display while keeping the full address for tooltips
IPV6_DISPLAY_MAX_LENGTH = 24  # Optimal for PDF column width
IPV6_TRUNCATION_SUFFIX = "\u2026"  # Ellipsis character

# Confidence levels for geolocation
# These values indicate HOW a location was determined.
# UI can use these for: tooltips, legend, debug overlay, trust indicators.
# Higher precision: hostname > geoip > online > fallback > interpolated
CONFIDENCE_GEOIP = "geoip"  # MaxMind/GeoIP database
CONFIDENCE_HOSTNAME = "hostname"  # Inferred from rDNS hostname (most accurate for backbone)
CONFIDENCE_INTERPOLATED = "interpolated"  # Forward-filled from previous hop
CONFIDENCE_DESTINATION = "destination"  # Injected destination point
CONFIDENCE_FALLBACK = "fallback"  # Last known location used as fallback
CONFIDENCE_ONLINE = "online"  # HTTPS lookup; provider is carried in source
CONFIDENCE_IXP = "ixp"  # PeeringDB IXP prefix: exchange region (or membership only), never a router fix
META_DECISION = "_decision"  # explainable resolver decision (sources, winner, reason)

# Deduplication config
GEO_DEDUP_DECIMALS = 4  # 4 decimals ≈ 11m precision (configurable for campus/DC)

# IPv6 Helper Functions


def is_ipv6_address(ip: str) -> bool:
    """
    Check if an IP address is IPv6.

    Args:
        ip: IP address string

    Returns:
        True if IPv6, False if IPv4 or invalid
    """
    if not ip or ip in ("*", "-", "—"):
        return False
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address)
    except ValueError:
        return False


def shorten_ipv6_for_display(ip: str, max_length: int = IPV6_DISPLAY_MAX_LENGTH) -> str:
    """
    Shorten IPv6 addresses for display in tables/PDFs.

    IPv6 addresses can be very long (e.g., 2001:4860:4860::8888:8844)
    and cause layout issues. This function truncates them with an ellipsis.

    Examples:
        2001:4860:4860::8888:8844 -> 2001:4860:4860::8888:8…
        2001:db8::1 -> 2001:db8::1 (unchanged, under limit)
        192.168.1.1 -> 192.168.1.1 (IPv4, unchanged)

    Args:
        ip: IP address string
        max_length: Maximum display length before truncation

    Returns:
        Original IP (if short) or truncated version with ellipsis
    """
    if not is_ipv6_address(ip):
        return ip

    if len(ip) <= max_length:
        return ip

    return ip[: max_length - len(IPV6_TRUNCATION_SUFFIX)] + IPV6_TRUNCATION_SUFFIX


# Try to import geoip2 - if not available, geolocation will be disabled
try:
    import geoip2.database
    import geoip2.errors

    _HAS_GEOIP = True
    _AddressNotFoundError: type[Exception] = geoip2.errors.AddressNotFoundError
except ImportError:
    _log.info("geoip2 not installed, geolocation disabled")
    _HAS_GEOIP = False

    class _AddressNotFoundError(Exception):  # placeholder so the except clause below stays valid
        pass

# Path for the MaxMind City database
# Single location: data_dir/geoip/GeoLite2-City.mmdb (Docker: /app/data/, local dev: ./data/)


def _get_geoip_path() -> Path:
    """Get the GeoIP database path inside the data directory."""
    # Use JUSTUP_DATA_DIR if set (Docker/production)
    # Falls back to data/ relative to app module (local dev)
    data_dir = os.getenv("JUSTUP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / GEOIP_DB_RELPATH

    app_dir = Path(__file__).resolve().parent.parent.parent
    return app_dir / "data" / GEOIP_DB_RELPATH


# Global reader instance (lazy-initialized, thread-safe).
# Locking scheme: _reader_lock serialises (re)loading; _reader_query_lock serialises queries and
# the close of a replaced reader; _geo_cache_lock is a leaf lock that guards the cache AND the
# (reader, generation) pair, so a lookup can tell whether its reader was swapped meanwhile.
_reader: Any | None = None
_reader_generation = 0  # bumped (with the cache cleared) on every reader swap
_reader_stamp: tuple[int, int, int] | None = None  # (mtime_ns, inode, size) of the opened DB file
# One-time logging flag (avoids log spam, but allows retry when DB appears)
_db_not_found_logged = False
# Lock for thread-safe reader initialization
_reader_lock = Lock()
# Lock for thread-safe reader queries (MaxMind C extension may not be thread-safe)
_reader_query_lock = Lock()


def _db_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ino, st.st_size)


def _get_reader() -> Any | None:
    """
    Get or initialize the GeoIP reader (lazy-init pattern, thread-safe).

    Uses double-checked locking to avoid race conditions during initialization.
    This prevents issues with:
    - Memory leaks on reloads
    - Fork/worker problems (uvicorn/gunicorn)
    - Hot-swap of the database file

    Note: If DB is not found initially but appears later (e.g., volume mount,
    hot-reload), subsequent calls will detect and initialize the reader.
    The reader is reopened (and the result cache cleared) when the DB file is replaced.
    """
    global _reader, _reader_stamp, _reader_generation, _db_not_found_logged

    if not _HAS_GEOIP:
        return None
    # Fast path: reuse the reader while the database file is unchanged.
    db_path = _get_geoip_path()
    if _reader is not None and _db_stamp(db_path) == _reader_stamp:
        return _reader

    with _reader_lock:
        if _reader is not None and _db_stamp(db_path) == _reader_stamp:
            return _reader
        if not db_path.exists():
            # A deleted file keeps the already opened reader usable.
            if _reader is None and not _db_not_found_logged:
                _db_not_found_logged = True
                _log.info(
                    "GeoLite2-City.mmdb not found at %s. Geolocation disabled. Will retry on next request.", db_path
                )
            return _reader

        stamp = _db_stamp(db_path)
        try:
            fresh = geoip2.database.Reader(str(db_path))
        except Exception as e:  # noqa: BLE001 - geolocation boundary: any GeoIP/library failure degrades to no-result
            _log.warning("Failed to initialize GeoIP reader from %s: %s", db_path, e)
            return _reader  # keep serving from the previous reader if there is one
        previous = _reader
        # Swap reader, generation and cache atomically: results derived from the old database
        # (including cached misses) are stale after a swap and in-flight writers are rejected.
        with _geo_cache_lock:
            _reader, _reader_stamp = fresh, stamp
            _reader_generation += 1
            _geo_cache.clear()
        _log.info("Initialized GeoIP reader from %s", db_path)
        if previous is not None:
            # No thread may be mid-query on the reader being closed; closing is best effort.
            with _reader_query_lock, contextlib.suppress(Exception):
                previous.close()
    return _reader


class GeoLocation(TypedDict):
    lat: float
    lon: float
    city: str | None
    country: str | None
    accuracy_radius_km: float | None
    city_is_region: bool


# Manual cache for geolocate_ip (replaces @lru_cache to avoid caching when reader unavailable)
_geo_cache: dict[str, GeoLocation | None] = {}
_geo_cache_lock = Lock()  # Thread-safe cache access
_geo_cache_max_size = 512


class _ReaderSwappedError(Exception):
    """The reader fetched by a lookup was replaced before the lookup could use it."""


_MAX_READER_ATTEMPTS = 3


def _cache_put(ip: str, value: GeoLocation | None, generation: int) -> None:
    """Cache a result unless the database was swapped since it was read."""
    with _geo_cache_lock:
        if generation == _reader_generation and len(_geo_cache) < _geo_cache_max_size:
            _geo_cache[ip] = value


def _read_record(reader: Any, ip: str) -> tuple[int, dict[str, Any] | None]:
    """Query ``reader`` and copy the record to plain Python values; return (generation, record).

    The record is None for an address that is not in the database. The query lock also keeps a
    swapping thread from closing the reader mid-query, and the extraction must happen under it:
    the result object references C memory that another query could overwrite.
    """
    with _reader_query_lock:
        with _geo_cache_lock:
            if _reader is not None and reader is not _reader:
                raise _ReaderSwappedError
            generation = _reader_generation
        try:
            r = reader.city(ip)
            return generation, {
                "lat": r.location.latitude,
                "lon": r.location.longitude,
                "accuracy_radius": r.location.accuracy_radius,
                "city": r.city.name if r.city else None,
                "country": r.country.iso_code if r.country else None,
                # GeoLite2 often has coordinates and a region but no city (common for IPv6 and
                # smaller carriers); the subdivision then names the place instead of "Unknown".
                "subdivision": r.subdivisions.most_specific.name if r.subdivisions else None,
            }
        except _AddressNotFoundError:
            return generation, None


def _to_geo_location(ip: str, record: dict[str, Any]) -> GeoLocation | None:
    lat, lon = record["lat"], record["lon"]
    if lat is None or lon is None:
        # Country-only records have no coordinates; maps need coordinates.
        _log.debug("GeoIP lookup for %s returned no coordinates (country=%s)", ip, record["country"])
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        _log.warning("GeoIP returned invalid coordinate types for %s: lat=%r, lon=%r", ip, lat, lon)
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        _log.warning("GeoIP returned out-of-range coordinates for %s: lat=%s, lon=%s", ip, lat, lon)
        return None
    radius = record["accuracy_radius"]
    city, subdivision = record["city"], record["subdivision"]
    return {
        "lat": float(lat),
        "lon": float(lon),
        "city": city or subdivision,
        "country": record["country"],
        "accuracy_radius_km": (
            float(radius)
            if isinstance(radius, (int, float))
            and not isinstance(radius, bool)
            and math.isfinite(radius)
            and radius >= 0
            else None
        ),
        "city_is_region": city is None and subdivision is not None,
    }


def _not_in_geoip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Non-routable addresses (private incl. CGN, loopback, link-local, reserved, multicast) are never looked up."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def geolocate_ip(ip: str) -> GeoLocation | None:
    """
    Geolocate an IP address to lat/lon/city/country with the local GeoLite2 database.

    Results are cached (512 entries). The reader is validated first, so a replaced database
    file also invalidates cached results. Only definitive answers are cached (a hit, or an
    address that is not in the database); an unavailable reader, a swapped reader or a
    transient reader error is never cached.

    Skips non-routable IPs (private, loopback, link-local, reserved, multicast; CGN is private).

    Returns:
        Dict with keys lat, lon, city, country (ISO code), accuracy_radius_km, city_is_region,
        or None if the lookup failed or is unavailable. This function does NOT reverse-geocode.
    """
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        _log.debug("Invalid IP format for geolocation: %s", ip)
        return None
    if _not_in_geoip(ip_obj):
        return None

    for _attempt in range(_MAX_READER_ATTEMPTS):
        reader = _get_reader()  # also detects a replaced database file and drops stale cache entries
        if not reader:
            return None  # not cached: the database may appear later
        with _geo_cache_lock:
            if ip in _geo_cache:
                return _geo_cache[ip]
        try:
            generation, record = _read_record(reader, ip)
        except _ReaderSwappedError:
            continue  # fetch the current reader and retry
        except Exception as e:  # noqa: BLE001 - geolocation boundary: any GeoIP/library failure degrades to no-result
            _log.debug("GeoIP lookup failed for %s: %s", ip, e)
            return None  # transient: not cached
        result = _to_geo_location(ip, record) if record is not None else None
        _cache_put(ip, result, generation)
        return result
    return None


_ISO_COUNTRY_CODES = frozenset(
    re.findall(
        r"[A-Z]{2}",
        """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM
    FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS
    IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML
    MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN
    PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG
    TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW""",
    )
)


def geolocate_country(ip: str) -> str | None:
    """Return a local GeoLite2 country code even when the record has no coordinates."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if _not_in_geoip(address):
        return None
    for _attempt in range(_MAX_READER_ATTEMPTS):
        reader = _get_reader()
        if not reader:
            return None
        try:
            _, record = _read_record(reader, ip)
        except _ReaderSwappedError:
            continue
        except Exception as exc:  # noqa: BLE001 - GeoIP failure must not affect quality monitoring
            _log.debug("GeoIP country lookup failed for %s: %s", ip, exc)
            return None
        code = record.get("country") if record else None
        return code.upper() if isinstance(code, str) and code.upper() in _ISO_COUNTRY_CODES else None
    return None


def infer_location_from_hostname(hostname: str | None) -> dict[str, Any] | None:
    """
    Infer geographic location from rDNS hostname using known POP codes.
    Uses pop_codes.py for multi-stage carrier POP code mappings.
    """
    loc = lookup_pop_code(hostname)
    if loc:
        confidence = loc.get("confidence", "unknown")
        source = loc.get("source", "unknown")
        _log.debug("Hostname '%s' matched POP -> %s (%s confidence, %s)", hostname, loc.get("city"), confidence, source)
        return {**loc, META_CONFIDENCE: CONFIDENCE_HOSTNAME}
    return None


_SOURCE_CONFIDENCE = {
    gr.SRC_GEOLITE2: CONFIDENCE_GEOIP,
    gr.SRC_POP: CONFIDENCE_HOSTNAME,
    gr.SRC_IP_API_PRO: CONFIDENCE_ONLINE,
    gr.SRC_IPWHO: CONFIDENCE_ONLINE,
    gr.SRC_IXP: CONFIDENCE_IXP,
}


def _online_ttl_seconds() -> int:
    return get_runtime_int(
        SettingKeys.PROVIDER_LOOKUP_CACHE_TTL_SECONDS,
        PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
        minimum=PROVIDER_LOOKUP_CACHE_TTL_MIN,
        maximum=PROVIDER_LOOKUP_CACHE_TTL_MAX,
    )


_EXTRA_IXP = "_ixp"


def _ixp_of(extras: Mapping[str, Any]) -> gr.IxpEvidence | None:
    return (extras.get(_EXTRA_IXP) or {}).get("evidence")


def _local_candidates(
    ip: str, hostname: str | None
) -> tuple[list[gr.GeoCandidate], list[str], dict[str, dict[str, Any]]]:
    """Gather the local sources (GeoLite2, POP/CLLI) and validate each one independently.

    Returns (valid candidates, considered sources, raw extras per source for the legacy dict).
    """
    candidates: list[gr.GeoCandidate] = []
    considered: list[str] = [gr.SRC_GEOLITE2]
    extras: dict[str, dict[str, Any]] = {}

    local = geolocate_ip(ip)
    if local:
        candidate = gr.build_candidate(
            gr.SRC_GEOLITE2,
            local.get("lat"),
            local.get("lon"),
            city=local.get("city"),
            country_code=local.get("country"),
            accuracy_radius_km=local.get("accuracy_radius_km"),
            city_is_region=bool(local.get("city_is_region")),
        )
        if candidate:
            candidates.append(candidate)

    if hostname:
        considered.append(gr.SRC_POP)
        pop = infer_location_from_hostname(hostname)
        candidate = pop and gr.build_candidate(
            gr.SRC_POP,
            pop.get("lat"),
            pop.get("lon"),
            city=pop.get("city"),
            country_code=pop.get("country"),
            confidence=gr.normalize_pop_confidence(pop.get("confidence")),
            evidence=str(pop.get("source") or "") or None,
        )
        if candidate:
            candidates.append(candidate)
            extras[gr.SRC_POP] = pop

    # Verified prefix membership of an IXP peering LAN; offline, one in-memory probe, public IPs only.
    ixp = ixp_index.lookup(ip) if public_ip(ip) else None
    if ixp is not None:
        extras[_EXTRA_IXP] = {"evidence": ixp}  # not a source key, so it never leaks into a location dict
    return candidates, considered, extras


# Online fallback (only when the local sources are not precise enough)
OnlineResult = tuple[tuple[str, ...], Mapping[str, Any]]
_NO_ONLINE: OnlineResult = ((), {})
# Distinct IPs of one route are looked up concurrently; the whole online phase of a route is capped.
ROUTE_ONLINE_WORKERS = 4
ROUTE_ONLINE_BUDGET_SECONDS = 10.0
# Prefetched online results of the route being processed (set by _batched_online).
_route_online: ContextVar[dict[str, OnlineResult] | None] = ContextVar("route_online", default=None)


def _online_fallback_enabled() -> bool:
    """Read per lookup, so toggling applies without a restart; an unreadable store means off."""
    return get_runtime_bool_fail_closed(SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, DEFAULT_ONLINE_GEO_FALLBACK_ENABLED)


def _query_online(public: str, api_key: str, ttl_seconds: int) -> OnlineResult:
    """Ask the online providers about one public IP; a failure degrades to 'no online result'."""
    try:
        return lookup_online_sources(public, api_key=api_key, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - online sources only improve quality; GeoLite2 must survive
        _log.warning("Online geo sources failed for %s (%s); using local sources", public, type(exc).__name__)
        return _NO_ONLINE


def _online_for(public: str) -> OnlineResult:
    prefetched = _route_online.get()
    if prefetched is not None and public in prefetched:
        return prefetched[public]
    return _query_online(public, get_ip_api_key(), _online_ttl_seconds())


def _prefetch_online(hops: list[dict]) -> dict[str, OnlineResult]:
    """Look up the distinct public hop IPs that need the fallback, concurrently and time-boxed.

    Offline results are untouched; the ranking later reads this mapping by IP, so response
    order cannot influence it. IPs not answered within the route budget get no online result.
    """
    if not hops or not _online_fallback_enabled():
        return {}
    targets: dict[str, None] = {}
    for hop in hops:
        public = public_ip(hop.get("ip"))
        if public is None or public in targets or hop.get("status") in (STATUS_TIMEOUT, STATUS_INTERPOLATED):
            continue
        if hop.get(META_INTERPOLATED):
            continue
        local, _considered, local_extras = _local_candidates(public, hop.get("hostname"))
        if gr.needs_online_fallback(local, ixp=_ixp_of(local_extras)):
            targets[public] = None
    if len(targets) < 2:  # a single lookup is already bounded by the provider timeout
        return {}
    api_key, ttl_seconds = get_ip_api_key(), _online_ttl_seconds()
    pool = ThreadPoolExecutor(max_workers=min(ROUTE_ONLINE_WORKERS, len(targets)), thread_name_prefix="geo-route")
    futures = {pool.submit(_query_online, public, api_key, ttl_seconds): public for public in targets}
    done, pending = wait(futures, timeout=ROUTE_ONLINE_BUDGET_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)
    if pending:
        _log.warning(
            "Online geo budget of %.0f s exhausted; %d IP(s) use local sources",
            ROUTE_ONLINE_BUDGET_SECONDS,
            len(pending),
        )
    return {public: (future.result() if future in done else _NO_ONLINE) for future, public in futures.items()}


def _batched_online(func: Callable[..., Any]) -> Callable[..., Any]:
    """Run a route-level function with the online fallback of all its hops prefetched concurrently."""

    @functools.wraps(func)
    def wrapper(hops: list[dict], *args: Any, **kwargs: Any) -> Any:
        if _route_online.get() is not None:
            return func(hops, *args, **kwargs)
        token = _route_online.set(_prefetch_online(hops))
        try:
            return func(hops, *args, **kwargs)
        finally:
            _route_online.reset(token)

    return wrapper


def _collect_candidates(
    ip: str, hostname: str | None
) -> tuple[list[gr.GeoCandidate], list[str], dict[str, dict[str, Any]]]:
    """Local sources first; online sources only for a public IP without sufficient local quality."""
    candidates, considered, extras = _local_candidates(ip, hostname)
    public = public_ip(ip)  # private and non-global addresses never leave the host
    if public and gr.needs_online_fallback(candidates, ixp=_ixp_of(extras)) and _online_fallback_enabled():
        attempted, online = _online_for(public)
        considered.extend(attempted)
        for source, info in online.items():
            candidate = info and gr.build_candidate(
                source,
                info["lat"],
                info["lon"],
                city=info["city"],
                country_code=info["country_code"],
            )
            if candidate:
                candidates.append(candidate)
                extras[source] = {"source": info["geo_source"]}
    return candidates, considered, extras


def resolve_location(ip: str, hostname: str | None = None) -> tuple[gr.GeoDecision, dict[str, dict[str, Any]]]:
    """The single location decision for an IP, shared by traceroute, map, PDF and provider info."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return gr.select([], []), {}
    candidates, considered, extras = _collect_candidates(ip, hostname)
    return gr.select(candidates, considered, ixp=_ixp_of(extras)), extras


def geolocate_with_hostname_hint(
    ip: str,
    hostname: str | None = None,
) -> dict[str, Any] | None:
    """Legacy location dict of the resolver decision, or None without a usable position."""
    decision, extras = resolve_location(ip, hostname)
    chosen = decision.selected
    if chosen is None:
        if decision.ixp is None:
            return None
        # IXP membership without an evidenced position: the table may name the exchange. ``country`` is
        # the facility-evidenced one (None for a multi-country exchange), never the exchange's admin seat.
        return {
            "lat": None,
            "lon": None,
            "city": None,
            "position_city": None,
            "country": decision.ixp.country,
            "accuracy_radius_km": None,
            "city_is_region": False,
            META_CONFIDENCE: CONFIDENCE_IXP,
            "accuracy_class": gr.ACCURACY_UNKNOWN,
            "ixp": decision.ixp.as_dict(),
            META_DECISION: decision.as_dict(),
        }
    return {
        **extras.get(chosen.source, {}),
        "lat": chosen.latitude,
        "lon": chosen.longitude,
        "city": decision.place_name,
        # The position's own place name; ``city`` may come from a text-only upgrade candidate.
        "position_city": None if chosen.city_is_region else chosen.city,
        "country": chosen.country_code,
        "accuracy_radius_km": chosen.accuracy_radius_km,
        "city_is_region": chosen.city_is_region,
        "place_text_source": decision.place.source if decision.place else None,
        META_CONFIDENCE: _SOURCE_CONFIDENCE[chosen.source],
        "accuracy_class": decision.accuracy_class,
        "ixp": decision.ixp.as_dict() if decision.ixp else None,
        META_DECISION: decision.as_dict(),
    }


# Shared business route model (single source of truth for web UI and PDF)
# Node keys (stable contract consumed by frontend.py, pdf_report.py, and the
# map renderer). Confidence/source stay separate so the UI can distinguish real
# geolocation, hostname/POP inference, coarse GeoIP, and a missing location.
RM_HOP = "hop"
RM_IP = "ip"
RM_HOSTNAME = "hostname"
RM_STATUS = "status"
RM_LOCATION = "location"  # {lat, lon, city, country} or None
RM_GEO_SOURCE = "geo_source"  # GeoSource value
RM_ACCURACY = "accuracy"  # GeoAccuracy value
RM_SYNTHETIC = "is_synthetic"
RM_DESTINATION = "is_destination"
RM_MAPPABLE = "mappable"  # True only when accuracy is precise and coords are valid
RM_MAP_ROLE = "map_role"  # MAP_ROLE_* of the hop, from the same rule as the geo points

# How a located hop is drawn. Web map and PDF read this value; neither decides it again.
MAP_ROLE_ROUTE = "route"  # reliable position (or the destination): a solid, named hop
MAP_ROLE_ESTIMATE = "estimate"  # approximate position: a chain node drawn hollow ("~"), its links are dashed
MAP_ROLE_NONE = "none"  # nothing to draw (no usable position)

_SOURCE_LABEL = {
    CONFIDENCE_IXP: gr.IXP_SOURCE_LABEL,
    CONFIDENCE_GEOIP: "GeoIP",
    CONFIDENCE_HOSTNAME: "hostname hint",
    CONFIDENCE_ONLINE: "online lookup",
}

_CONFIDENCE_TO_SOURCE = {
    CONFIDENCE_HOSTNAME: GeoSource.HOSTNAME,
    CONFIDENCE_GEOIP: GeoSource.GEOIP,
    CONFIDENCE_ONLINE: GeoSource.ONLINE,
    CONFIDENCE_IXP: GeoSource.IXP,
    CONFIDENCE_DESTINATION: GeoSource.GEOIP,
}


def _coarse_source(source: GeoSource) -> GeoSource:
    return source if source in (GeoSource.ONLINE, GeoSource.IXP) else GeoSource.GEOIP_COARSE


def _has_coords(loc: dict[str, Any]) -> bool:
    lat, lon = loc.get("lat"), loc.get("lon")
    return (
        isinstance(lat, (int, float))
        and not isinstance(lat, bool)
        and isinstance(lon, (int, float))
        and not isinstance(lon, bool)
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def _classify_location(loc: dict[str, Any] | None) -> tuple[GeoSource, GeoAccuracy]:
    """Map a resolved location dict to a (source, accuracy) pair.

    A POP hint or a GeoLite2 city within a small accuracy radius is precise; other
    city candidates can still be plotted with their approximate accuracy retained.
    """
    if not loc:
        return GeoSource.NONE, GeoAccuracy.UNKNOWN

    has_coords = _has_coords(loc)
    source = _CONFIDENCE_TO_SOURCE.get(loc.get(META_CONFIDENCE), GeoSource.GEOIP)
    if source == GeoSource.IXP:
        return source, GeoAccuracy.APPROXIMATE  # an exchange region is never promoted to a router position

    if not has_coords:
        return _coarse_source(source), GeoAccuracy.APPROXIMATE
    if not loc.get("city") or loc.get("city_is_region"):
        return _coarse_source(source), GeoAccuracy.APPROXIMATE
    explicit = loc.get("accuracy_class")
    if explicit is not None:  # the resolver already weighed evidence and contradictions
        if explicit == gr.ACCURACY_PRECISE:
            return source, GeoAccuracy.PRECISE
        return (source if source == GeoSource.HOSTNAME else _coarse_source(source)), GeoAccuracy.APPROXIMATE
    if source == GeoSource.HOSTNAME:
        return source, GeoAccuracy.PRECISE
    radius = loc.get("accuracy_radius_km")
    if (
        source == GeoSource.GEOIP
        and isinstance(radius, (int, float))
        and not isinstance(radius, bool)
        and math.isfinite(radius)
        and 0 <= radius <= MAX_CITY_RADIUS_KM
    ):
        return source, GeoAccuracy.PRECISE
    return _coarse_source(source), GeoAccuracy.APPROXIMATE


LOC_PRECISE = "precise"  # city, country and map position share one evidenced candidate
LOC_APPROXIMATE = "approximate"  # city of the position's own candidate, accuracy not evidenced
LOC_COUNTRY = "country"  # country only: no city claimed
LOC_NONE = "none"


def location_view(loc: dict[str, Any] | None) -> dict[str, Any]:
    """The location text of a hop, derived from the SAME candidate as its map position.

    Single owner for the hop table and the map: a precise location carries its city; an approximate
    one only the city of the position's own candidate (never a text-only upgrade from another source);
    a region or country-only result names no city.
    """
    if not loc:
        return {"city": None, "country": None, "precision": LOC_NONE, "ixp": None}
    country = loc.get("country") or None
    ixp = loc.get("ixp")
    ixp_name = ixp.get("name") if isinstance(ixp, dict) else None
    if _classify_location(loc)[1] == GeoAccuracy.PRECISE:
        return {"city": loc.get("city") or None, "country": country, "precision": LOC_PRECISE, "ixp": ixp_name}
    city = loc.get("position_city") or None
    if city:
        return {"city": city, "country": country, "precision": LOC_APPROXIMATE, "ixp": ixp_name}
    return {"city": None, "country": country, "precision": LOC_COUNTRY if country else LOC_NONE, "ixp": ixp_name}


@functools.lru_cache(maxsize=256)
def country_center(country: str) -> tuple[float, float] | None:
    """Area centroid of the country's largest Natural Earth land polygon."""
    # Deferred import: route_map_rules imports this module at load time.
    from app.services.route_map_rules import BASEMAP_FILE

    world = json.loads(BASEMAP_FILE.read_text(encoding="utf-8"))
    for feature in world.get("features", []):
        if (feature.get("properties") or {}).get("iso") != country.upper():
            continue
        geometry = feature.get("geometry") or {}
        polygons = geometry.get("coordinates") or []
        if geometry.get("type") == "Polygon":
            polygons = [polygons]
        largest_area, center = 0.0, None
        for polygon in polygons:
            if not polygon or len(polygon[0]) < 3:
                continue
            ring = polygon[0]
            edges = list(itertools.pairwise([*ring, ring[0]]))
            cross = [start[0] * end[1] - end[0] * start[1] for start, end in edges]
            area = sum(cross)
            if abs(area) > largest_area:
                largest_area = abs(area)
                lon = sum((start[0] + end[0]) * weight for (start, end), weight in zip(edges, cross, strict=True)) / (
                    3 * area
                )
                lat = sum((start[1] + end[1]) * weight for (start, end), weight in zip(edges, cross, strict=True)) / (
                    3 * area
                )
                center = (lat, lon)
        return center
    return None


def _map_location(loc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not loc or location_view(loc)["precision"] != LOC_COUNTRY:
        return loc
    if loc.get(META_CONFIDENCE) == CONFIDENCE_IXP:
        return loc  # a country behind an IXP is a table fact, not a place to draw
    center = country_center(str(loc["country"]))
    if center is None:
        return loc
    return {
        **loc,
        "lat": center[0],
        "lon": center[1],
        "city": None,
        "position_city": None,
        "accuracy_class": gr.ACCURACY_APPROXIMATE,
        "accuracy_radius_km": None,
        "_country_center": True,
    }


def map_role(loc: dict[str, Any] | None, *, is_destination: bool = False) -> str:
    """The single rule for how a located hop is drawn (web map, PDF and route model share it).

    Every located hop is a node of the one route chain. A precise position is a reliable node. An
    approximate one with a city of its own is an estimate node (hollow "~" marker, dashed links);
    the destination stays the chain's end and carries its uncertainty. Country centers are estimates.
    """
    if not loc or not _has_coords(loc):
        return MAP_ROLE_NONE
    accuracy = _classify_location(loc)[1]
    if loc.get("_country_center"):
        return MAP_ROLE_ESTIMATE
    if accuracy == GeoAccuracy.PRECISE:
        return MAP_ROLE_ROUTE
    if accuracy == GeoAccuracy.APPROXIMATE and location_view(loc)["city"]:
        return MAP_ROLE_ROUTE if is_destination else MAP_ROLE_ESTIMATE
    return MAP_ROLE_NONE


def location_note(loc: dict[str, Any] | None) -> str | None:
    """Source and uncertainty of an approximate position ("GeoIP estimate, radius about 100 km")."""
    if not loc:
        return None
    if loc.get("_country_center"):
        return "Country center; exact location unknown"
    if loc.get(META_CONFIDENCE) == CONFIDENCE_IXP:
        ixp = loc.get("ixp") if isinstance(loc.get("ixp"), dict) else {}
        note = f"{ixp.get('name') or 'IXP'}, source {_SOURCE_LABEL[CONFIDENCE_IXP]}"
        radius = loc.get("accuracy_radius_km")
        if isinstance(radius, int | float) and not isinstance(radius, bool) and math.isfinite(radius) and radius > 0:
            note += f", exchange region radius about {radius:g} km"
        return note + "; exact router location unknown"
    note = f"{_SOURCE_LABEL.get(loc.get(META_CONFIDENCE), 'Location')} estimate"
    radius = loc.get("accuracy_radius_km")
    if isinstance(radius, int | float) and not isinstance(radius, bool) and math.isfinite(radius) and radius > 0:
        note += f", radius about {radius:g} km"
    return note


def _route_node(
    hop_num: int,
    ip: str | None,
    hostname: str | None,
    status: str,
    loc: dict[str, Any] | None,
    *,
    is_synthetic: bool = False,
    is_destination: bool = False,
) -> dict[str, Any]:
    """Build one route-model node with its geo classification."""
    source, accuracy = _classify_location(loc)
    location = None
    if accuracy == GeoAccuracy.PRECISE and loc:
        location = {
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "city": loc.get("city"),
            "country": loc.get("country"),
        }
    elif accuracy == GeoAccuracy.APPROXIMATE and loc:
        # Country/region only: carry the country for text display, never coords.
        location = {"lat": None, "lon": None, "city": None, "country": loc.get("country")}

    return {
        RM_HOP: hop_num,
        RM_IP: ip,
        RM_HOSTNAME: hostname,
        RM_STATUS: status,
        RM_LOCATION: location,
        RM_GEO_SOURCE: str(source),
        RM_ACCURACY: str(accuracy),
        RM_SYNTHETIC: is_synthetic,
        RM_DESTINATION: is_destination,
        RM_MAPPABLE: accuracy == GeoAccuracy.PRECISE,
        RM_MAP_ROLE: map_role(_map_location(loc) if public_ip(ip) else loc, is_destination=is_destination),
    }


def _normalize_hop_status(ip: str | None, status: str) -> str:
    """Return a known hop status; a missing or unknown one is inferred from the IP (broken parsers)."""
    if status in (STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT, STATUS_INTERPOLATED):
        return status
    return STATUS_OK if ip and ip not in ("*", "-", "???") else STATUS_TIMEOUT


def _destination_disposition(hops: list[dict], destination_ip: str | None) -> tuple[int, bool]:
    """Single owner of the destination reached/synthetic decision and hop number.

    Returns ``(last_hop_number, reached)`` where ``reached`` is True when the last
    responding hop's IP equals the destination. Both ``build_route_model`` and
    ``route_hops_to_geo_points`` use this so the web UI and PDF cannot disagree on
    whether a destination was reached.
    """
    last_hop = max((h.get("hop", 0) for h in hops), default=0)
    last_responding = next((h for h in reversed(hops) if h.get("status") in (STATUS_OK, STATUS_PARTIAL)), None)
    reached = bool(destination_ip and last_responding and last_responding.get("ip") == destination_ip)
    return last_hop, reached


@_batched_online
def build_route_model(
    hops: list[dict],
    destination_ip: str | None = None,
    destination_geo: dict | None = None,
) -> list[dict[str, Any]]:
    """Build the shared business route model consumed by the web UI and the PDF.

    One node per traced hop, in traceroute order, with original hop numbers preserved.
    Each node distinguishes real geolocation, hostname/POP inference, coarse
    country/region GeoIP, and a missing location (see ``_classify_location``).
    Unknown and approximate nodes carry no coordinates and are never mappable, so
    no renderer can place them or bridge a fabricated precise link across them.

    This model and ``route_hops_to_geo_points`` share the destination reached/
    synthetic decision via ``_destination_disposition`` so the sequence/PDF view
    and the map view cannot disagree. This function changes no measurement,
    hop status, or IP info; it is a read-only projection of the hops.

    Integration status: this is the explicit shared surface for the four-state
    geo classification (``GeoSource``/``GeoAccuracy``). The shipped web UI and PDF
    render from ``route_hops_to_geo_points`` and the enriched hops; both take the
    drawing role of a hop from ``map_role`` (``RM_MAP_ROLE`` here, ``map_role`` on
    the geo points), so a hop's role cannot differ between the two views.
    """
    nodes: list[dict[str, Any]] = []
    if not hops:
        return nodes

    for h in hops:
        status = h.get("status") or ""
        ip = h.get("ip", "*")
        hostname = h.get("hostname")
        hop_num = h.get("hop", 0)

        status = _normalize_hop_status(ip, status)

        # Interpolated / timeout hops stay in the sequence as explicit gaps with
        # no location. They are never geolocated and never placed on a map.
        if status == STATUS_INTERPOLATED or h.get(META_INTERPOLATED):
            nodes.append(_route_node(hop_num, ip, hostname, STATUS_INTERPOLATED, None))
            continue
        if status == STATUS_TIMEOUT or ip in ("*", "-", "???"):
            nodes.append(_route_node(hop_num, ip, hostname, STATUS_TIMEOUT, None))
            continue
        if status not in (STATUS_OK, STATUS_PARTIAL):
            nodes.append(_route_node(hop_num, ip, hostname, status, None))
            continue

        is_dest = bool(destination_ip and ip == destination_ip)
        loc = destination_geo if is_dest and destination_geo else geolocate_with_hostname_hint(ip, hostname)
        nodes.append(_route_node(hop_num, ip, hostname, status, loc, is_destination=is_dest))

    # Destination handling shares the reached/synthetic decision with
    # route_hops_to_geo_points via _destination_disposition.
    has_destination = any(n[RM_DESTINATION] for n in nodes)
    if destination_ip and not has_destination:
        last_hop, reached = _destination_disposition(hops, destination_ip)
        nodes.append(
            _route_node(
                last_hop + 1,
                destination_ip,
                None,
                STATUS_OK if reached else STATUS_TIMEOUT,
                destination_geo if destination_geo and destination_geo.get("lat") is not None else None,
                is_synthetic=not reached,
                is_destination=True,
            )
        )
    # When a hop already matched the destination IP, its node keeps its hop number
    # and geo classification; a reached-but-unlocated destination is non-mappable.

    return nodes


def _hop_ranges(hops: list[int]) -> str:
    """ "9–12, 14" for the hop numbers a segment crosses."""
    parts: list[list[int]] = []
    for hop in sorted(set(hops)):
        if parts and parts[-1][1] == hop - 1:
            parts[-1][1] = hop
        else:
            parts.append([hop, hop])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in parts)


def _link(tail: dict, gap: bool, next_approximate: bool, skipped: list[int]) -> None:
    """Style the segment leaving ``tail``: solid only between two reliable positions without a gap.

    Dashed means the path is estimated or unknown: an end is approximate, or hops in between have
    no map position (their numbers go into ``segment_note``). No coordinate is ever invented.
    """
    if gap or next_approximate or tail.get("_approximate"):
        tail["segment_to_next"] = SEGMENT_DASHED
    if skipped:
        tail[META_SEGMENT_NOTE] = f"Hops {_hop_ranges(skipped)} not on the map"


@_batched_online
def route_hops_to_geo_points(
    hops: list[dict],
    destination_ip: str | None = None,
    destination_geo: dict | None = None,
) -> list[dict]:
    """
    Convert traceroute hops to the geo points both the web map and the PDF draw.

    The points are the nodes of ONE chain from the first mappable hop to the destination, in hop
    order. Every point carries a ``map_role`` (``route`` = reliable, ``estimate`` = approximate, drawn
    hollow). A point's ``segment_to_next`` is ``dashed`` when the path to the next node is estimated
    or unknown: an end is approximate/estimated, or hops in between did not answer, were
    interpolated or have no usable position (listed in ``segment_note``); otherwise it is ``solid``.
    A real geographic back-jump stays visible; no coordinate is invented for a skipped hop.

    Args:
        hops: List of traceroute hop dicts (with status: ok/partial/timeout/interpolated)
        destination_ip: Target IP for destination matching
        destination_geo: Pre-resolved geo data for destination

    Returns:
        Geo points in hop order. Hops without a usable position produce no point and are never
        geolocated, interpolated or filled from a neighbour.
    """
    points: list[dict] = []
    if not hops:
        return points
    destination_geo = _map_location(destination_geo)

    route_key = hashlib.sha256(
        repr(
            tuple(
                (str(h.get("hop", ""))[:8], str(h.get("ip", ""))[:45], str(h.get("hostname", ""))[:253])
                for h in hops[:64]
            )
        ).encode()
    ).hexdigest()

    tail: dict | None = None  # last chain node: the one whose segment_to_next the next node decides
    skipped: list[int] = []  # hops without a map position between the tail and the next node
    gap = False  # an unmeasured stretch lies between the tail and the next node
    prev_hop: int | None = None

    for h in hops:
        status = h.get("status") or ""
        ip = h.get("ip", "*")
        hostname = h.get("hostname")  # rDNS hostname
        hop_num = h.get("hop", 0)

        normalized = _normalize_hop_status(ip, status)
        if normalized != status and normalized == STATUS_OK:
            _log.debug("Hop %s: Missing status, inferring 'ok' from valid IP %s", hop_num, ip)
        status = normalized

        # A missing hop number is an unmeasured stretch too.
        if isinstance(hop_num, int) and prev_hop is not None and hop_num > prev_hop + 1:
            gap = True
            skipped.extend(range(prev_hop + 1, hop_num))
        if isinstance(hop_num, int):
            prev_hop = hop_num

        if h.get(META_INTERPOLATED) or ip in ("*", "-", "???") or status not in (STATUS_OK, STATUS_PARTIAL):
            gap = True  # silent, interpolated or unusable hops never get a location
            if isinstance(hop_num, int):
                skipped.append(hop_num)
            continue

        # The destination IP has ONE location decision (destination_geo), also when it answered as a hop.
        is_destination_hop = bool(destination_ip and ip == destination_ip)
        if is_destination_hop and destination_geo:
            loc = destination_geo
        else:
            # Hostname hint takes priority for known carriers.
            loc = geolocate_with_hostname_hint(ip, hostname)
        if hostname:
            try:
                observe_unknown_pop(hostname, ip, route_key, lambda ip=ip: geolocate_ip(ip))
            except Exception:  # noqa: BLE001 - candidate persistence must not affect route monitoring
                _log.warning("PoP candidate observation failed", exc_info=True)
        is_public = public_ip(ip)
        map_loc = _map_location(loc) if is_public else loc
        # Match the map projection's country-center conversion before deciding the drawing role.
        # Otherwise country-only locations are reported as unlocated here while their map point
        # is correctly shown as an estimate at the country center.
        role = map_role(map_loc, is_destination=is_destination_hop)
        if role == MAP_ROLE_NONE:
            gap = True  # country-only and missing locations have no table city to place on the map
            if isinstance(hop_num, int):
                skipped.append(hop_num)
            continue

        loc = map_loc
        accuracy = _classify_location(loc)[1]
        view = location_view(loc)
        approximate = accuracy == GeoAccuracy.APPROXIMATE

        _log.debug("Geo resolved hop %s (%s) -> %s [%s]", hop_num, ip, view["city"], loc.get(META_CONFIDENCE))

        point = {
            "hop": hop_num,
            "ip": ip,
            **loc,
            "city": view["city"],
            "country": view["country"],
            META_IS_PUBLIC: True,
            "loc_precision": view["precision"],
            "_approximate": approximate,
            META_MAP_ROLE: role,
        }
        if is_destination_hop:
            point[META_DESTINATION] = True
            point[META_IS_TERMINAL] = True
        if approximate:
            point[META_LOC_NOTE] = location_note(loc)
        # Every located hop is a node of ONE chain; the quality of both ends decides how the link is drawn.
        point["segment_to_next"] = SEGMENT_SOLID  # decided by the next node
        if tail is not None:
            _link(tail, gap, approximate, skipped)
        tail, gap, skipped = point, False, []
        points.append(point)

    # Add destination marker:
    # 1. If the last route point is the destination -> mark it in-place.
    # 2. Else, if destination_geo has a usable position -> inject a destination point.
    # A destination without a usable position is NOT placed on the map.
    if tail is not None and destination_ip and tail.get("ip") == destination_ip:
        tail[META_DESTINATION] = True
        tail[META_IS_TERMINAL] = True  # Explicit end signal for renderer
        tail.pop("segment_to_next", None)  # destination = terminal node, no outgoing edge
        if META_CONFIDENCE not in tail:
            tail[META_CONFIDENCE] = CONFIDENCE_GEOIP
        if not tail.get("city"):
            tail[META_GEO_UNAVAILABLE] = True  # country-only resolution

    if destination_ip and not any(p.get(META_DESTINATION) for p in points):
        # Shared with build_route_model: one owner of reached/synthetic + hop number.
        last_hop, _reached = _destination_disposition(hops, destination_ip)
        destination_view = location_view(destination_geo)
        destination_approximate = (
            bool(destination_geo) and _classify_location(destination_geo)[1] == GeoAccuracy.APPROXIMATE
        )
        if map_role(destination_geo, is_destination=True) != MAP_ROLE_NONE:
            if tail is not None:
                _link(tail, gap, destination_approximate, skipped)
            # No segment_to_next: the destination is the terminal node.
            point = {
                "hop": last_hop + 1,
                "ip": destination_ip,
                "lat": destination_geo.get("lat"),
                "lon": destination_geo.get("lon"),
                "city": destination_view["city"],
                "country": destination_view["country"],
                "accuracy_class": destination_geo.get("accuracy_class"),
                "accuracy_radius_km": destination_geo.get("accuracy_radius_km"),
                "ixp": destination_geo.get("ixp"),
                META_DESTINATION: True,
                META_IS_TERMINAL: True,
                META_IS_PUBLIC: True,
                META_CONFIDENCE: CONFIDENCE_DESTINATION,
                "loc_precision": destination_view["precision"],
                "_approximate": destination_approximate,
                META_MAP_ROLE: map_role(destination_geo, is_destination=True),
                "_country_center": bool(destination_geo.get("_country_center")),
            }
            if destination_approximate:
                point[META_LOC_NOTE] = location_note(destination_geo)
            points.append(point)
        # No fabricated destination marker when geo is missing: placing the destination at the last
        # known coordinate would suggest a located target that was never resolved.

    # Merge co-located consecutive hops of the chain for a cleaner map (same role only).
    if len(points) > 2:
        points = _deduplicate_geo_points(points)
    for point in points:
        radius = point.get("accuracy_radius_km")
        if point.get("_approximate") and isinstance(radius, int | float) and 0 < radius <= MAX_MAP_RADIUS_KM:
            point[META_MAP_RADIUS_KM] = float(radius)
    return points


def route_map_features(points: list[dict]) -> dict[str, bool]:
    """What a map legend has to explain for these points; the one source for web map and PDF."""
    return {
        "has_unknown_link": any(p.get("segment_to_next") == SEGMENT_DASHED for p in points),
        "has_approximate": any(p.get("_approximate") for p in points),
    }


def _deduplicate_geo_points(points: list[dict]) -> list[dict]:
    """
    Merge hops that are consecutive (hop numbers n, n+1, ...) AND share a location (~11 m).

    A route that returns to an earlier location (A -> B -> A -> C) keeps the second A, and two
    hops at one place with an unmapped hop between them stay separate points, so a `_hop_range`
    never claims a hop it does not contain. Each merged run gets a _hop_range annotation and a
    dashed segment_to_next if any member had one. The last run is represented by the destination
    point itself. Shallow copies; the input is not mutated.
    """
    if len(points) <= 2:
        return points

    def _key(p: dict) -> tuple[float, float]:
        return (round(p.get("lat", 0), GEO_DEDUP_DECIMALS), round(p.get("lon", 0), GEO_DEDUP_DECIMALS))

    def _follows(run_last: dict, p: dict) -> bool:
        if p.get(META_MAP_ROLE) != run_last.get(META_MAP_ROLE):
            return False  # an estimate never merges into a reliable hop (or the reverse)
        try:
            return int(p.get("hop")) == int(run_last.get("hop")) + 1
        except (TypeError, ValueError):
            return False

    runs: list[list[dict]] = []
    for p in points:
        if runs and _key(runs[-1][0]) == _key(p) and _follows(runs[-1][-1], p):
            runs[-1].append(p)
        else:
            runs.append([p])

    if len(runs) == 1:
        # Same-location route: keep a grouped origin run plus the destination as separate nodes.
        # Retain the middle hop numbers in the range so plotted-hop counts remain truthful.
        runs = [points[:-1], [points[-1]]]

    deduped: list[dict] = []
    for idx, run in enumerate(runs):
        point = dict(run[-1] if idx == len(runs) - 1 else run[0])
        if len(run) > 1:
            hop_nums = [pt.get("hop", 0) for pt in run]
            point[META_HOP_RANGE] = f"{min(hop_nums)}-{max(hop_nums)}"
            # The merged node leaves the way its last member does (inner links join one place).
            leaving = run[-1].get("segment_to_next")
            if leaving is not None:
                point["segment_to_next"] = leaving
            if run[-1].get(META_SEGMENT_NOTE):
                point[META_SEGMENT_NOTE] = run[-1][META_SEGMENT_NOTE]
            approximate = [
                pt for pt in run if pt.get("_approximate") or pt.get("accuracy_class") == gr.ACCURACY_APPROXIMATE
            ]
            if approximate:
                point["_approximate"] = True
                point["accuracy_class"] = gr.ACCURACY_APPROXIMATE
                point["loc_precision"] = LOC_APPROXIMATE
                radii = [
                    pt["accuracy_radius_km"]
                    for pt in approximate
                    if isinstance(pt.get("accuracy_radius_km"), int | float)
                    and not isinstance(pt["accuracy_radius_km"], bool)
                    and math.isfinite(pt["accuracy_radius_km"])
                    and pt["accuracy_radius_km"] >= 0
                ]
                if radii:
                    point["accuracy_radius_km"] = max(radii)
        deduped.append(point)

    return deduped
