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
PDF report shows the hops as a table.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

# Import shared constants (single source of truth)
from app.services.ip_lookup import lookup_public_ip
from app.services.pop_codes import lookup_pop_code
from app.utils.constants import GEOIP_DB_RELPATH, GeoAccuracy, GeoSource, HopStatus, SegmentType, SettingKeys
from app.utils.runtime_settings import get_ip_api_key, get_runtime_bool

_log = logging.getLogger(__name__)

# --- Re-export segment types for backward compatibility ---
# Use SegmentType.SOLID/.DASHED directly where possible
SEGMENT_SOLID = SegmentType.SOLID
SEGMENT_DASHED = SegmentType.DASHED

# --- Re-export hop status for backward compatibility ---
# Use HopStatus.OK/.TIMEOUT/.INTERPOLATED directly where possible
STATUS_OK = HopStatus.OK
STATUS_PARTIAL = "partial"  # Not in HopStatus (aggregate concept)
STATUS_TIMEOUT = HopStatus.TIMEOUT
STATUS_INTERPOLATED = HopStatus.INTERPOLATED

# --- Metadata key constants (avoid magic strings) ---
META_PRIVATE_IP = "_private_ip"
META_IS_PUBLIC = "_is_public"
META_DESTINATION = "_destination"
META_INTERPOLATED = "_interpolated"
META_HOP_RANGE = "_hop_range"
META_FORWARD_FILLED = "_forward_filled"
META_CONFIDENCE = "_confidence"
META_GEO_UNAVAILABLE = "_geo_unavailable"  # Destination reached but no geo coords
META_IS_TERMINAL = "_is_terminal"  # Explicit end-of-route signal for renderer
META_IS_IPV6 = "_is_ipv6"  # Mark IPv6 addresses for special handling

# --- IPv6 specific constants ---
# IPv6 addresses are long and cause layout issues in PDFs/tables
# We shorten them for display while keeping the full address for tooltips
IPV6_DISPLAY_MAX_LENGTH = 24  # Optimal for PDF column width
IPV6_TRUNCATION_SUFFIX = "\u2026"  # Ellipsis character

# --- Confidence levels for geolocation ---
# These values indicate HOW a location was determined.
# UI can use these for: tooltips, legend, debug overlay, trust indicators.
# Higher precision: hostname > geoip > online > fallback > interpolated
CONFIDENCE_GEOIP = "geoip"  # MaxMind/GeoIP database
CONFIDENCE_HOSTNAME = "hostname"  # Inferred from rDNS hostname (most accurate for backbone)
CONFIDENCE_INTERPOLATED = "interpolated"  # Forward-filled from previous hop
CONFIDENCE_DESTINATION = "destination"  # Injected destination point
CONFIDENCE_FALLBACK = "fallback"  # Last known location used as fallback
CONFIDENCE_ONLINE = "online"  # Optional HTTPS lookup; provider is carried in source
MAX_CITY_RADIUS_KM = 25

# --- Deduplication config ---
GEO_DEDUP_DECIMALS = 4  # 4 decimals ≈ 11m precision (configurable for campus/DC)

# --- IPv6 Helper Functions ---


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
        # IPv4 or invalid - return as-is
        return ip

    if len(ip) <= max_length:
        # Short enough - no truncation needed
        return ip

    # Truncate and add ellipsis
    return ip[: max_length - len(IPV6_TRUNCATION_SUFFIX)] + IPV6_TRUNCATION_SUFFIX


# Try to import geoip2 - if not available, geolocation will be disabled
try:
    import geoip2.database

    _HAS_GEOIP = True
except ImportError:
    _log.info("geoip2 not installed, geolocation disabled")
    _HAS_GEOIP = False

# Path for the MaxMind City database
# Single location: data_dir/geoip/GeoLite2-City.mmdb (Docker: /app/data/, local dev: ./data/)


def _get_geoip_path() -> Path:
    """Get the GeoIP database path inside the data directory."""
    # Use JUSTUP_DATA_DIR if set (Docker/production)
    # Falls back to data/ relative to app module (local dev)
    data_dir = os.getenv("JUSTUP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / GEOIP_DB_RELPATH

    # Local dev fallback: data/ relative to app module
    app_dir = Path(__file__).resolve().parent.parent.parent
    return app_dir / "data" / GEOIP_DB_RELPATH


# Global reader instance (lazy-initialized, thread-safe)
_reader: Any | None = None
# One-time logging flag (avoids log spam, but allows retry when DB appears)
_db_not_found_logged = False
# Lock for thread-safe reader initialization
_reader_lock = Lock()
# Lock for thread-safe reader queries (MaxMind C extension may not be thread-safe)
_reader_query_lock = Lock()


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
    On successful init, the geolocate_ip cache is cleared to avoid stale None results.
    """
    global _reader, _db_not_found_logged

    if not _HAS_GEOIP:
        return None

    # Fast path: return cached reader if already initialized
    if _reader is not None:
        return _reader

    # Slow path: acquire lock and initialize
    with _reader_lock:
        # Double-check after acquiring lock (another thread may have initialized)
        if _reader is not None:
            return _reader

        # Get the single expected path
        db_path = _get_geoip_path()
        if not db_path.exists():
            # Log "not found" only once to avoid spam
            if not _db_not_found_logged:
                _db_not_found_logged = True
                _log.info(
                    "GeoLite2-City.mmdb not found at %s. Geolocation disabled. Will retry on next request.", db_path
                )
            return None

        try:
            _reader = geoip2.database.Reader(str(db_path))
            _log.info("Initialized GeoIP reader from %s", db_path)
            # Clear the manual geo cache to invalidate any stale None results
            # (from lookups that failed before the DB was available)
            _geo_cache.clear()
        except Exception as e:  # noqa: BLE001 - geolocation boundary: any GeoIP/library failure degrades to no-result
            _log.warning("Failed to initialize GeoIP reader from %s: %s", db_path, e)
            return None

    return _reader


# TypedDict for geolocation result (better type hints)
class GeoLocation(TypedDict):
    lat: float
    lon: float
    city: str | None
    country: str | None
    accuracy_radius_km: float | None
    city_is_region: bool


# Manual cache for geolocate_ip (replaces @lru_cache to avoid caching when reader unavailable)
_geo_cache: dict[str, GeoLocation | None] = {}
_geo_cache_lock = Lock()  # Thread-safe cache access (mirrors _online_geo_cache pattern)
_geo_cache_max_size = 512


def geolocate_ip(ip: str) -> GeoLocation | None:
    """
    Geolocate an IP address to lat/lon/city/country.

    Results are cached manually (512 entries) to avoid repeated lookups.
    Cache does NOT store results when the GeoIP reader is unavailable,
    preventing stale None values from persisting after DB becomes available.

    Skips non-routable IPs:
    - Private (RFC1918), Loopback, Link-local, Reserved
    - Carrier-Grade NAT (100.64.0.0/10) - covered by is_private
    - Multicast (224.0.0.0/4)

    Returns:
        Dict with keys: lat, lon, city, country (ISO code)
        or None if lookup failed or unavailable.

    Note: Some IPs resolve to coordinates (Lat/Lon) but lack a City name in the MaxMind DB.
    This typically indicates a generic ISP location or country-level accuracy.
    This function does NOT perform reverse geocoding (Lat/Lon -> City).
    """
    # Check cache first (thread-safe)
    with _geo_cache_lock:
        if ip in _geo_cache:
            return _geo_cache[ip]

    # Skip non-routable IPs (SSRF protection + they won't be in the DB anyway)
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
            # Cache the negative result for private IPs (they'll never resolve)
            with _geo_cache_lock:
                if len(_geo_cache) < _geo_cache_max_size:
                    _geo_cache[ip] = None
            return None
    except ValueError:
        _log.debug("Invalid IP format for geolocation: %s", ip)
        return None

    reader = _get_reader()
    if not reader:
        # Reader not available - DO NOT CACHE (it may appear later)
        return None

    # CRITICAL: Lock must cover ENTIRE result extraction, not just reader.city()!
    # The result object from maxminddb.extension references internal C memory/buffers.
    # If another thread calls reader.city() while we're accessing r.location.latitude,
    # the internal buffer can be overwritten -> SEGFAULT.
    # See: https://github.com/maxmind/MaxMind-DB-Reader-python#thread-safety
    try:
        with _reader_query_lock:
            # Query reader and extract ALL data to Python types BEFORE releasing lock
            r = reader.city(ip)

            # Extract C-referenced data to pure Python values (str/float/None)
            # Do NOT access result object `r` after releasing the lock!
            lat = r.location.latitude
            lon = r.location.longitude
            accuracy_radius = r.location.accuracy_radius
            city_name = r.city.name if r.city else None
            country_iso = r.country.iso_code if r.country else None
            # GeoLite2 often has coordinates and a region but no city (common for
            # IPv6 and smaller carriers). Fall back to the subdivision so the UI
            # shows the region instead of "Unknown".
            subdivision_name = r.subdivisions.most_specific.name if r.subdivisions else None

        # ── Lock released - only work with pure Python values below this line ──

        if lat is None or lon is None:
            # IPv6 FALLBACK: If no lat/lon but we have country, log it for awareness
            # We still return None here because maps need coordinates
            # Frontend/PDF can use country-only data from PTR/ASN if available
            if country_iso:
                _log.debug("GeoIP lookup for %s: country=%s but no coordinates (common for IPv6)", ip, country_iso)
            else:
                _log.debug("GeoIP lookup returned no coordinates for: %s", ip)
            # Cache the negative result
            with _geo_cache_lock:
                if len(_geo_cache) < _geo_cache_max_size:
                    _geo_cache[ip] = None
            return None

        # Defensive type check (MaxMind should always return floats, but be safe)
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            _log.warning("GeoIP returned invalid coordinate types for %s: lat=%r, lon=%r", ip, lat, lon)
            with _geo_cache_lock:
                if len(_geo_cache) < _geo_cache_max_size:
                    _geo_cache[ip] = None
            return None

        # Validate coordinate ranges
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            _log.warning("GeoIP returned out-of-range coordinates for %s: lat=%s, lon=%s", ip, lat, lon)
            with _geo_cache_lock:
                if len(_geo_cache) < _geo_cache_max_size:
                    _geo_cache[ip] = None
            return None

        result: GeoLocation = {
            "lat": float(lat),
            "lon": float(lon),
            "city": city_name or subdivision_name,
            "country": country_iso,
            "accuracy_radius_km": (
                float(accuracy_radius)
                if isinstance(accuracy_radius, (int, float))
                and not isinstance(accuracy_radius, bool)
                and math.isfinite(accuracy_radius)
                and accuracy_radius >= 0
                else None
            ),
            "city_is_region": city_name is None and subdivision_name is not None,
        }

        # Cache the positive result
        with _geo_cache_lock:
            if len(_geo_cache) < _geo_cache_max_size:
                _geo_cache[ip] = result

        return result
    except Exception as e:  # noqa: BLE001 - geolocation boundary: any GeoIP/library failure degrades to no-result
        _log.debug("GeoIP lookup failed for %s: %s", ip, e)
        # Cache the negative result for failed lookups
        with _geo_cache_lock:
            if len(_geo_cache) < _geo_cache_max_size:
                _geo_cache[ip] = None
        return None


def _geolocate_online_fallback(ip: str) -> dict[str, Any] | None:
    """Use the shared HTTPS lookup only after the operator enables online geo."""
    if not get_runtime_bool(SettingKeys.ONLINE_GEO_ENABLED, False):
        return None
    info = lookup_public_ip(ip, api_key=get_ip_api_key(), need_network=False)
    if not info or info["lat"] is None or info["lon"] is None:
        return None
    return {
        "lat": info["lat"],
        "lon": info["lon"],
        "city": info["city"] or None,
        "country": info["country_code"] or None,
        "source": info["geo_source"],
        "accuracy_radius_km": None,
    }


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


def geolocate_with_hostname_hint(
    ip: str,
    hostname: str | None = None,
) -> dict[str, Any] | None:
    """
    Geolocate IP with fallback chain: hostname POP > GeoIP > online API.
    """
    # Try hostname-based inference first (most accurate for backbone)
    if hostname:
        hostname_loc = infer_location_from_hostname(hostname)
        if hostname_loc:
            return hostname_loc

    # Fall back to GeoIP
    geoip_loc = geolocate_ip(ip)
    if geoip_loc:
        result = dict(geoip_loc)
        result[META_CONFIDENCE] = CONFIDENCE_GEOIP
        if _classify_location(result)[1] != GeoAccuracy.PRECISE:
            online_loc = _geolocate_online_fallback(ip)
            if (
                online_loc
                and (not result.get("country") or online_loc.get("country") == result["country"])
                and not result.get("city")
                and online_loc.get("city")
            ):
                # Use the online response as one location value. Never attach its city to
                # GeoLite2's country-centre coordinates, which may describe a different place.
                online_loc[META_CONFIDENCE] = CONFIDENCE_ONLINE
                return online_loc
        return result

    # Online fallback (disabled by default, privacy-sensitive)
    online_loc = _geolocate_online_fallback(ip)
    if online_loc:
        online_loc[META_CONFIDENCE] = CONFIDENCE_ONLINE
        return online_loc

    return None


def traceroute_to_geo_points(hops: list[dict], *, forward_fill: bool = True) -> list[dict]:
    """
    Enrich traceroute hops with geolocation data.

    Args:
        hops: List of hop dicts (with 'ip', 'hop', 'status')
        forward_fill: If True, interpolated hops inherit the last known geo position.
                      This creates a more meaningful route visualization.

    Returns:
        List of dicts with keys: hop, ip, lat, lon, city, country
        Deduplicated by (lat, lon) to avoid overlapping markers.

    Note: Interpolated hops are NEVER geolocated directly - they only use forward-fill.
    This prevents semantic confusion (interpolated ≠ real hop with IP).
    """
    points: list[dict] = []
    if not hops:
        return points

    last_known_loc: dict[str, Any] | None = None

    for h in hops:
        status = h.get("status", STATUS_TIMEOUT)
        ip = h.get("ip", "*")
        hostname = h.get("hostname")  # rDNS hostname if available
        hop_num = h.get("hop", 0)

        # Handle interpolated hops separately - NEVER geolocate, only forward-fill
        if status == STATUS_INTERPOLATED:
            if forward_fill and last_known_loc:
                points.append(
                    {
                        "hop": hop_num,
                        "ip": ip,
                        **last_known_loc,
                        META_FORWARD_FILLED: True,
                        META_INTERPOLATED: True,
                    }
                )
            continue

        # Skip failed hops or those without IP
        if status not in (STATUS_OK, STATUS_PARTIAL) or not ip or ip == "*":
            continue

        # Real hop with IP - attempt geolocation (hostname POP > GeoIP)
        loc = geolocate_with_hostname_hint(ip, hostname)
        if not loc:
            # Private IP or not in DB - skip silently
            # DESIGN DECISION: Do NOT forward-fill for private IPs.
            # Rationale: Private IPs (RFC1918, CGN) have no meaningful geographic
            # location. Forward-filling would show them at the previous hop's position,
            # which is technically correct for routing but semantically misleading.
            # Trade-off: NAT-heavy networks may show visual "gaps" in the route.
            # This is intentional - gaps indicate routing through non-public infrastructure.
            continue

        last_known_loc = loc
        points.append(
            {
                "hop": hop_num,
                "ip": ip,
                **loc,
            }
        )

    # Deduplicate by (lat, lon) to avoid overlapping markers,
    # but keep first and last to preserve route endpoints
    # Use consistent GEO_DEDUP_DECIMALS (~11m precision)
    if len(points) > 2:
        points = _deduplicate_geo_points(points)

    return points


# --- Shared business route model (single source of truth for web UI and PDF) ---
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

_CONFIDENCE_TO_SOURCE = {
    CONFIDENCE_HOSTNAME: GeoSource.HOSTNAME,
    CONFIDENCE_GEOIP: GeoSource.GEOIP,
    CONFIDENCE_ONLINE: GeoSource.ONLINE,
    CONFIDENCE_DESTINATION: GeoSource.GEOIP,
}


def _classify_location(loc: dict[str, Any] | None) -> tuple[GeoSource, GeoAccuracy]:
    """Map a resolved location dict to a (source, accuracy) pair.

    Only a POP hint or a GeoLite2 city within a small accuracy radius is mappable.
    Online coordinates without a published radius remain approximate.
    """
    if not loc:
        return GeoSource.NONE, GeoAccuracy.UNKNOWN

    lat, lon = loc.get("lat"), loc.get("lon")
    has_coords = (
        isinstance(lat, (int, float))
        and not isinstance(lat, bool)
        and isinstance(lon, (int, float))
        and not isinstance(lon, bool)
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )
    source = _CONFIDENCE_TO_SOURCE.get(loc.get(META_CONFIDENCE), GeoSource.GEOIP)

    if not has_coords:
        return (GeoSource.ONLINE if source == GeoSource.ONLINE else GeoSource.GEOIP_COARSE), GeoAccuracy.APPROXIMATE
    if not loc.get("city") or loc.get("city_is_region"):
        return (GeoSource.ONLINE if source == GeoSource.ONLINE else GeoSource.GEOIP_COARSE), GeoAccuracy.APPROXIMATE
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
    return (GeoSource.ONLINE if source == GeoSource.ONLINE else GeoSource.GEOIP_COARSE), GeoAccuracy.APPROXIMATE


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
    }


def _destination_disposition(hops: list[dict], destination_ip: str | None) -> tuple[int, bool]:
    """Single owner of the destination reached/synthetic decision and hop number.

    Returns ``(last_hop_number, reached)`` where ``reached`` is True when the last
    responding hop's IP equals the destination. Both ``build_route_model`` and
    ``mtr_hops_to_geo_points`` use this so the web UI and PDF cannot disagree on
    whether a destination was reached.
    """
    last_hop = max((h.get("hop", 0) for h in hops), default=0)
    last_responding = next((h for h in reversed(hops) if h.get("status") in (STATUS_OK, STATUS_PARTIAL)), None)
    reached = bool(destination_ip and last_responding and last_responding.get("ip") == destination_ip)
    return last_hop, reached


def build_route_model(
    hops: list[dict],
    destination_ip: str | None = None,
    destination_geo: dict | None = None,
) -> list[dict[str, Any]]:
    """Build the shared business route model consumed by the web UI and the PDF.

    One node per traced hop, in MTR order, with original hop numbers preserved.
    Each node distinguishes real geolocation, hostname/POP inference, coarse
    country/region GeoIP, and a missing location (see ``_classify_location``).
    Unknown and approximate nodes carry no coordinates and are never mappable, so
    no renderer can place them or bridge a fabricated precise link across them.

    This model and ``mtr_hops_to_geo_points`` share the destination reached/
    synthetic decision via ``_destination_disposition`` so the sequence/PDF view
    and the map view cannot disagree. This function changes no MTR measurement,
    hop status, or IP info; it is a read-only projection of the hops.

    Integration status: this is the explicit shared surface for the four-state
    geo classification (``GeoSource``/``GeoAccuracy``). The shipped web UI and PDF
    currently render from ``mtr_hops_to_geo_points`` and the enriched hops; moving
    those renderers onto this model (so ``mappable``/``geo_source``/``accuracy``
    reach the template and schematic) is the next step and is not wired yet.
    """
    nodes: list[dict[str, Any]] = []
    if not hops:
        return nodes

    for h in hops:
        status = h.get("status") or ""
        ip = h.get("ip", "*")
        hostname = h.get("hostname")
        hop_num = h.get("hop", 0)

        if not status or status not in (STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT, STATUS_INTERPOLATED):
            if ip and ip not in ("*", "-", "???", "", None):
                status = STATUS_OK
            else:
                status = STATUS_TIMEOUT

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

        loc = geolocate_with_hostname_hint(ip, hostname)
        is_dest = bool(destination_ip and ip == destination_ip)
        nodes.append(_route_node(hop_num, ip, hostname, status, loc, is_destination=is_dest))

    # Destination handling shares the reached/synthetic decision with
    # mtr_hops_to_geo_points via _destination_disposition.
    has_destination = any(n[RM_DESTINATION] for n in nodes)
    if destination_ip and not has_destination:
        last_hop, reached = _destination_disposition(hops, destination_ip)
        nodes.append(
            _route_node(
                last_hop + 1,
                destination_ip,
                None,
                STATUS_OK if reached else STATUS_TIMEOUT,
                destination_geo if destination_geo and destination_geo.get("lat") else None,
                is_synthetic=not reached,
                is_destination=True,
            )
        )
    # When a hop already matched the destination IP, its node keeps its hop number
    # and geo classification; a reached-but-unlocated destination is non-mappable.

    return nodes


def mtr_hops_to_geo_points(
    hops: list[dict],
    destination_ip: str | None = None,
    destination_geo: dict | None = None,
) -> list[dict]:
    """
    Convert MTR hops to geo points with interpolation support.

    This is the new unified function for both UI and PDF that handles:
    - Normal hops with geolocation
    - Interpolated hops (ICMP blocked segments)
    - Destination injection if not reached via MTR

    Args:
        hops: List of MTR hop dicts (with status: ok/partial/timeout/interpolated)
        destination_ip: Target IP for destination matching
        destination_geo: Pre-resolved geo data for destination

    Returns:
        List of geo points with segment_type for visualization:
        - segment_type: SEGMENT_SOLID (normal) or SEGMENT_DASHED (interpolated)

    Note: Interpolated hops ONLY affect line styling (dashed segments).
    They do NOT create markers and are NEVER geolocated directly.
    """
    points: list[dict] = []
    if not hops:
        return points

    # Track segment type to mark the final segment before destination injection.
    # This variable is updated throughout the loop but only used at the end:
    # if the route ends with interpolated hops, the segment to the injected
    # destination should be dashed. Reset to SOLID after each real geolocated point.
    prev_segment_type = SEGMENT_SOLID

    for h in hops:
        status = h.get("status") or ""  # Explicit empty check
        ip = h.get("ip", "*")
        hostname = h.get("hostname")  # rDNS hostname from MTR
        hop_num = h.get("hop", 0)

        # Infer status from IP if missing/empty (fixes broken parsers)
        # A valid IP without status is treated as ok
        if not status or status not in (STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT, STATUS_INTERPOLATED):
            if ip and ip not in ("*", "-", "???", "", None):
                status = STATUS_OK
                _log.debug("Hop %s: Missing status, inferring 'ok' from valid IP %s", hop_num, ip)
            else:
                status = STATUS_TIMEOUT

        # Handle interpolated hops - DON'T add as points, only mark segment
        # Interpolated hops should only affect line styling, not create markers
        if status == STATUS_INTERPOLATED or h.get(META_INTERPOLATED):
            # Mark the previous point's segment_to_next as dashed
            if points:
                points[-1]["segment_to_next"] = SEGMENT_DASHED
            prev_segment_type = SEGMENT_DASHED
            continue

        # Skip pure timeout hops (they have no usable data)
        if status == STATUS_TIMEOUT or ip in ("*", "-", "???"):
            continue

        # Include ok/partial hops - they have valid IPs
        if status not in (STATUS_OK, STATUS_PARTIAL):
            continue

        # Try geolocation with hostname hint (hostname takes priority for known carriers)
        loc = geolocate_with_hostname_hint(ip, hostname)
        if not loc or _classify_location(loc)[1] != GeoAccuracy.PRECISE:
            # No reliable location for this hop (private IP, or not in any source).
            # Do NOT place it at the previous hop's coordinate: that would invent a
            # position the router does not have. The hop stays off the map and the
            # gap is shown as a dashed (non-geographic) segment to the next point.
            if points:
                points[-1]["segment_to_next"] = SEGMENT_DASHED
            prev_segment_type = SEGMENT_DASHED
            continue

        _log.debug("Geo resolved hop %s (%s) -> %s [%s]", hop_num, ip, loc.get("city"), loc.get(META_CONFIDENCE))

        # Reset to solid after adding a real geolocated point
        points.append(
            {
                "hop": hop_num,
                "ip": ip,
                **loc,
                "segment_to_next": SEGMENT_SOLID,  # Default, may be updated by next hop
                META_IS_PUBLIC: True,
            }
        )
        prev_segment_type = SEGMENT_SOLID

    # Add destination marker:
    # 1. If the last hop IP matches the destination -> mark it in-place.
    # 2. Else, if destination_geo has real coords -> inject a destination point.
    # A destination without real coordinates is NOT placed on the map.

    # First: If last hop already reached destination, mark it semantically
    # This handles the case where destination is geolocated but not marked
    if points and destination_ip and points[-1].get("ip") == destination_ip:
        points[-1][META_DESTINATION] = True
        points[-1][META_IS_TERMINAL] = True  # Explicit end signal for renderer
        # Destination = terminal node, no outgoing edge
        points[-1].pop("segment_to_next", None)
        # Preserve existing confidence or set to geoip
        if META_CONFIDENCE not in points[-1]:
            points[-1][META_CONFIDENCE] = CONFIDENCE_GEOIP
        # Mark geo unavailable if no city (country-only resolution)
        if not points[-1].get("city"):
            points[-1][META_GEO_UNAVAILABLE] = True

    if destination_ip:
        # Only check explicit META_DESTINATION flag, not IP match
        # IP match alone doesn't mean it's semantically marked as destination
        has_destination = any(p.get(META_DESTINATION) for p in points)

        if not has_destination:
            # Shared with build_route_model: one owner of reached/synthetic + hop number.
            last_hop, _reached = _destination_disposition(hops, destination_ip)

            # If previous segment was dashed (interpolated block), mark it
            if prev_segment_type == SEGMENT_DASHED and points:
                points[-1]["segment_to_next"] = SEGMENT_DASHED

            if destination_geo and _classify_location(destination_geo)[1] == GeoAccuracy.PRECISE:
                # We have valid geo coordinates for the destination.
                # Note: No segment_to_next - destination is terminal node
                points.append(
                    {
                        "hop": last_hop + 1,
                        "ip": destination_ip,
                        "lat": destination_geo.get("lat"),
                        "lon": destination_geo.get("lon"),
                        "city": destination_geo.get("city"),
                        "country": destination_geo.get("country"),
                        META_DESTINATION: True,
                        META_IS_TERMINAL: True,
                        META_IS_PUBLIC: True,
                        META_CONFIDENCE: CONFIDENCE_DESTINATION,
                    }
                )
            # No fabricated destination marker when geo is missing: placing the
            # destination at the last known coordinate would suggest a located
            # target that was never resolved. The destination then stays off the
            # map and is communicated as off-map info (web) / a note (PDF) by the
            # route model below.

    # Deduplicate points with same coordinates for cleaner map display.
    # Uses a single-pass approach: group by coordinates, then reduce.
    if len(points) > 2:
        points = _deduplicate_geo_points(points)

    return points


def _deduplicate_geo_points(points: list[dict]) -> list[dict]:
    """
    Merge CONSECUTIVE hops that share a location (rounded to ~11 m).

    A route that returns to an earlier location (A -> B -> A -> C) keeps the
    second A. Each merged run gets a _hop_range annotation and a dashed
    segment_to_next if any member had one. The last run is represented by the
    destination point itself. Shallow copies; the input is not mutated.
    """
    if len(points) <= 2:
        return points

    def _key(p: dict) -> tuple[float, float]:
        return (round(p.get("lat", 0), GEO_DEDUP_DECIMALS), round(p.get("lon", 0), GEO_DEDUP_DECIMALS))

    runs: list[list[dict]] = []
    for p in points:
        if runs and _key(runs[-1][0]) == _key(p):
            runs[-1].append(p)
        else:
            runs.append([p])

    if len(runs) == 1:
        # Same-location route: keep origin and destination so consumers still get two points
        runs = [[points[0]], [points[-1]]]

    deduped: list[dict] = []
    for idx, run in enumerate(runs):
        point = dict(run[-1] if idx == len(runs) - 1 else run[0])
        if len(run) > 1:
            hop_nums = [pt.get("hop", 0) for pt in run]
            point[META_HOP_RANGE] = f"{min(hop_nums)}-{max(hop_nums)}"
            if any(pt.get("segment_to_next") == SEGMENT_DASHED for pt in run):
                point["segment_to_next"] = SEGMENT_DASHED
        deduped.append(point)

    return deduped
