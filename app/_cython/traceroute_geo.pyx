# cython: language_level=3
# Safety checks controlled by setup_cython.py (_SAFETY_CRITICAL list)
# Do NOT add boundscheck/wraparound/cdivision directives here!

"""
Geolocation service for traceroute hops using MaxMind GeoLite2.

This module provides functionality to geolocate IP addresses from traceroute
results. The actual map visualization is handled by:
- traceroute_worldmap.py / traceroute_routezoom.py (Cartopy-based server renderers)
- Client-side Canvas/Chart.js (for Web UI)
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any, Optional, TypedDict

# Import shared constants (single source of truth)
from app.utils.constants import HopStatus, SegmentType

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
CONFIDENCE_GEOIP = "geoip"              # MaxMind/GeoIP database
CONFIDENCE_HOSTNAME = "hostname"         # Inferred from rDNS hostname (most accurate for backbone)
CONFIDENCE_INTERPOLATED = "interpolated" # Forward-filled from previous hop
CONFIDENCE_DESTINATION = "destination"   # Injected destination point
CONFIDENCE_FALLBACK = "fallback"         # Last known location used as fallback
CONFIDENCE_ONLINE = "online"             # Online API fallback (ip-api.com, privacy implications)

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
    return ip[:max_length - len(IPV6_TRUNCATION_SUFFIX)] + IPV6_TRUNCATION_SUFFIX


# --- Online fallback config ---
# SECURITY/PRIVACY NOTICE:
# - ip-api.com uses HTTP (no TLS) - IP addresses are transmitted in plaintext
# - External third-party service - IPs are shared with ip-api.com
# - Rate-limited to 45 req/min (free tier)
# RECOMMENDATION: Disable in hardened/privacy-sensitive deployments
ONLINE_GEO_ENABLED = os.getenv("UPMON_ONLINE_GEO_ENABLED", "false").lower() == "true"
ONLINE_GEO_API_URL = "http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,lat,lon"
ONLINE_GEO_TIMEOUT = 0.5  # 500ms max per lookup
ONLINE_GEO_CACHE_SIZE = 1000  # Cache up to 1000 IPs

# Import POP code lookup from external module (SQL-based, no dicts)
from app.services.pop_codes import lookup_pop_code

# Try to import geoip2 - if not available, geolocation will be disabled
try:
    import geoip2.database
    _HAS_GEOIP = True
except ImportError:
    _log.info("geoip2 not installed, geolocation disabled")
    _HAS_GEOIP = False

# Path for the MaxMind City database
# Single location: data_dir/GeoLite2-City.mmdb (Docker: /app/data/, local dev: ./data/)
_GEOIP_DB_NAME = "GeoLite2-City.mmdb"

def _get_geoip_path() -> Path:
    """Get the GeoIP database path from env or default data directory."""
    # Explicit override
    if os.getenv("UPMON_GEOIP_DB_PATH"):
        return Path(os.getenv("UPMON_GEOIP_DB_PATH"))
    
    # Use JUSTUP_DATA_DIR if set (Docker/production)
    # Falls back to data/ relative to app module (local dev)
    data_dir = os.getenv("JUSTUP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / _GEOIP_DB_NAME
    
    # Local dev fallback: data/ relative to app module
    app_dir = Path(__file__).resolve().parent.parent.parent
    return app_dir / "data" / _GEOIP_DB_NAME

# Global reader instance (lazy-initialized, thread-safe)
_reader: Optional[Any] = None
# One-time logging flag (avoids log spam, but allows retry when DB appears)
_db_not_found_logged = False
# Lock for thread-safe reader initialization
_reader_lock = Lock()
# Lock for thread-safe reader queries (MaxMind C extension may not be thread-safe)
_reader_query_lock = Lock()


def _get_reader() -> Optional[Any]:
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
                    "GeoLite2-City.mmdb not found at %s. Geolocation disabled. "
                    "Will retry on next request.", db_path
                )
            return None
        
        try:
            _reader = geoip2.database.Reader(str(db_path))
            _log.info("Initialized GeoIP reader from %s", db_path)
            # Clear the manual geo cache to invalidate any stale None results
            # (from lookups that failed before the DB was available)
            _geo_cache.clear()
        except Exception as e:
            _log.warning("Failed to initialize GeoIP reader from %s: %s", db_path, e)
            return None
    
    return _reader


# TypedDict for geolocation result (better type hints)
class GeoLocation(TypedDict):
    lat: float
    lon: float
    city: Optional[str]
    country: Optional[str]


# Manual cache for geolocate_ip (replaces @lru_cache to avoid caching when reader unavailable)
_geo_cache: dict[str, Optional[GeoLocation]] = {}
_geo_cache_lock = Lock()  # Thread-safe cache access (mirrors _online_geo_cache pattern)
_geo_cache_max_size = 512


def geolocate_ip(ip: str) -> Optional[GeoLocation]:
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
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
        ):
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
            city_name = r.city.name if r.city else None
            country_iso = r.country.iso_code if r.country else None
        
        # ── Lock released - only work with pure Python values below this line ──
        
        if not lat or not lon:
            # IPv6 FALLBACK: If no lat/lon but we have country, log it for awareness
            # We still return None here because maps need coordinates
            # Frontend/PDF can use country-only data from PTR/ASN if available
            if country_iso:
                _log.debug(
                    "GeoIP lookup for %s: country=%s but no coordinates (common for IPv6)",
                    ip, country_iso
                )
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
            "city": city_name,
            "country": country_iso,
        }
        
        # Cache the positive result
        with _geo_cache_lock:
            if len(_geo_cache) < _geo_cache_max_size:
                _geo_cache[ip] = result
        
        return result
    except Exception as e:
        _log.debug("GeoIP lookup failed for %s: %s", ip, e)
        # Cache the negative result for failed lookups
        with _geo_cache_lock:
            if len(_geo_cache) < _geo_cache_max_size:
                _geo_cache[ip] = None
        return None


# --- Online Geo API Fallback (ip-api.com) ---
# Cached to avoid rate limits (45 req/min free tier)
_online_geo_cache: dict[str, Optional[dict[str, Any]]] = {}
_online_geo_cache_lock = Lock()  # Thread-safe cache access


def _geolocate_online_fallback(ip: str) -> Optional[dict[str, Any]]:
    """
    Query online geo API as last resort when MMDB has no city/coords.
    
    Uses ip-api.com (free, no API key, 45 req/min limit).
    Results are cached in-memory to reduce API calls.
    
    SECURITY: Disabled by default (UPMON_ONLINE_GEO_ENABLED=true to enable).
    See module-level comments for privacy implications.
    
    Returns:
        Dict with lat, lon, city, country or None on failure/disabled.
    """
    # Feature flag check - disabled by default for privacy
    if not ONLINE_GEO_ENABLED:
        return None
    
    # Validate IP format and reject private IPs (SSRF protection)
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback:
            return None
        # Use validated string representation
        validated_ip = str(ip_obj)
    except ValueError:
        _log.warning("Invalid IP format for online geo lookup: %s", ip)
        return None
    
    # Import httpx with error handling (optional dependency)
    try:
        import httpx
    except ImportError:
        _log.warning(
            "httpx not installed — online geo fallback unavailable. "
            "Install with: pip install httpx"
        )
        return None
    
    # Check cache first (thread-safe)
    with _online_geo_cache_lock:
        if ip in _online_geo_cache:
            return _online_geo_cache[ip]
    
    # Limit cache size (simple LRU-like: clear oldest half when full)
    # Thread-safe eviction
    with _online_geo_cache_lock:
        if len(_online_geo_cache) >= ONLINE_GEO_CACHE_SIZE:
            keys_to_remove = list(_online_geo_cache.keys())[:ONLINE_GEO_CACHE_SIZE // 2]
            for k in keys_to_remove:
                del _online_geo_cache[k]
    
    # Perform HTTP request outside lock (avoid blocking cache access)
    try:
        url = ONLINE_GEO_API_URL.format(ip=validated_ip)
        resp = httpx.get(url, timeout=ONLINE_GEO_TIMEOUT)
        if resp.status_code != 200:
            with _online_geo_cache_lock:
                _online_geo_cache[ip] = None
            return None
        
        data = resp.json()
        if data.get("status") != "success":
            with _online_geo_cache_lock:
                _online_geo_cache[ip] = None
            return None
        
        # Validate coordinate types (API could return strings, lists, etc.)
        lat, lon = data.get("lat"), data.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            _log.warning("Online geo API returned invalid types for %s: lat=%r, lon=%r", ip, lat, lon)
            with _online_geo_cache_lock:
                _online_geo_cache[ip] = None
            return None
        
        # Validate coordinate ranges (defense against malicious/buggy API)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            _log.warning("Online geo API returned out-of-range coords for %s: lat=%s, lon=%s", ip, lat, lon)
            with _online_geo_cache_lock:
                _online_geo_cache[ip] = None
            return None
        
        result = {
            "lat": float(lat),
            "lon": float(lon),
            "city": data.get("city"),
            "country": data.get("countryCode"),
        }
        
        _log.debug("Online geo for %s -> %s [ip-api]", ip, result.get("city"))
        with _online_geo_cache_lock:
            _online_geo_cache[ip] = result
        return result
        
    except Exception as e:
        _log.debug("Online geo lookup failed for %s: %s", ip, e)
        with _online_geo_cache_lock:
            _online_geo_cache[ip] = None
        return None


def infer_location_from_hostname(hostname: Optional[str]) -> Optional[dict[str, Any]]:
    """
    Infer geographic location from rDNS hostname using known POP codes.
    Uses pop_codes.py for multi-stage carrier POP code mappings.
    """
    loc = lookup_pop_code(hostname)
    if loc:
        confidence = loc.get("confidence", "unknown")
        source = loc.get("source", "unknown")
        _log.debug(
            "Hostname '%s' matched POP -> %s (%s confidence, %s)",
            hostname, loc.get("city"), confidence, source
        )
        return {**loc, META_CONFIDENCE: CONFIDENCE_HOSTNAME}
    return None


def geolocate_with_hostname_hint(
    ip: str,
    hostname: Optional[str] = None,
) -> Optional[dict[str, Any]]:
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

    last_known_loc: Optional[dict[str, Any]] = None
    
    for h in hops:
        status = h.get("status", STATUS_TIMEOUT)
        ip = h.get("ip", "*")
        hostname = h.get("hostname")  # rDNS hostname if available
        hop_num = h.get("hop", 0)
        
        # Handle interpolated hops separately - NEVER geolocate, only forward-fill
        if status == STATUS_INTERPOLATED:
            if forward_fill and last_known_loc:
                points.append({
                    "hop": hop_num,
                    "ip": ip,
                    **last_known_loc,
                    META_FORWARD_FILLED: True,
                    META_INTERPOLATED: True,
                })
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
        points.append({
            "hop": hop_num,
            "ip": ip,
            **loc,
        })
    
    # Deduplicate by (lat, lon) to avoid overlapping markers,
    # but keep first and last to preserve route endpoints
    # Use consistent GEO_DEDUP_DECIMALS (~11m precision)
    if len(points) > 2:
        points = _deduplicate_geo_points(points)
        
    return points


def mtr_hops_to_geo_points(
    hops: list[dict],
    destination_ip: Optional[str] = None,
    destination_geo: Optional[dict] = None,
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
    
    last_known_loc: Optional[dict[str, Any]] = None
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
        if not loc:
            # Private IP - use last known or skip
            if last_known_loc:
                points.append({
                    "hop": hop_num,
                    "ip": ip,
                    **last_known_loc,
                    "segment_to_next": SEGMENT_SOLID,
                    META_PRIVATE_IP: True,
                    META_IS_PUBLIC: False,
                })
            continue
        
        last_known_loc = loc
        _log.debug(
            "Geo resolved hop %s (%s) -> %s [%s]",
            hop_num, ip, loc.get("city"), loc.get(META_CONFIDENCE)
        )
        
        # Reset to solid after adding a real geolocated point
        points.append({
            "hop": hop_num,
            "ip": ip,
            **loc,
            "segment_to_next": SEGMENT_SOLID,  # Default, may be updated by next hop
            META_IS_PUBLIC: True,
        })
        prev_segment_type = SEGMENT_SOLID
    
    # Add destination marker - multiple strategies:
    # 1. If last hop IP matches destination -> mark it as destination (in-place)
    # 2. If destination_geo has coords -> inject destination point
    # 3. If destination reached but no geo -> use last known location with fallback marker
    
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
        has_destination = any(
            p.get(META_DESTINATION)
            for p in points
        )
        
        if not has_destination:
            last_hop = max((h.get("hop", 0) for h in hops), default=0)
            
            # Check if last hop in original data is the destination (reached)
            last_hop_data = next((h for h in reversed(hops) if h.get("status") in ("ok", "partial")), None)
            destination_reached = last_hop_data and last_hop_data.get("ip") == destination_ip
            
            # If previous segment was dashed (interpolated block), mark it
            if prev_segment_type == SEGMENT_DASHED and points:
                points[-1]["segment_to_next"] = SEGMENT_DASHED
            
            if destination_geo and destination_geo.get("lat") and destination_geo.get("lon"):
                # Strategy 1: We have valid geo coordinates for destination
                # Note: No segment_to_next - destination is terminal node
                points.append({
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
                })
            elif points and last_known_loc:
                # Strategy 2: No geo for destination, but we have last known location
                # Create a fallback marker at last known position
                # This prevents "open-ended" routes when destination is reached but not geolocatable
                # Note: No segment_to_next - destination is terminal node
                # Note: destination_geo may be None here - ternary handles this safely
                country_only = destination_geo.get("country") if destination_geo else None
                city_label = f"Destination ({country_only})" if country_only else "Destination (reached)"
                
                points.append({
                    "hop": last_hop + 1,
                    "ip": destination_ip,
                    "lat": last_known_loc.get("lat"),
                    "lon": last_known_loc.get("lon"),
                    "city": city_label,
                    "country": country_only,
                    META_DESTINATION: True,
                    META_IS_TERMINAL: True,
                    META_IS_PUBLIC: True,
                    META_GEO_UNAVAILABLE: True,
                    META_CONFIDENCE: CONFIDENCE_FALLBACK,
                })
    
    # Deduplicate points with same coordinates for cleaner map display.
    # Uses a single-pass approach: group by coordinates, then reduce.
    if len(points) > 2:
        points = _deduplicate_geo_points(points)
    
    return points


def _deduplicate_geo_points(points: list[dict]) -> list[dict]:
    """
    Deduplicate geo points by coordinates.
    
    Single-pass grouping followed by linear reduction:
    1. Group all points by (lat, lon) rounded to 4 decimals (~11m)
    2. Keep first and last points always (route endpoints)
    3. For intermediate groups, keep first occurrence and add hop range
    4. Preserve segment_to_next=dashed from later points in the group
    
    Note: Creates shallow copies to avoid mutating the input list.
    
    Returns:
        Deduplicated list with _hop_range annotations where applicable.
    """
    if len(points) <= 2:
        return points
    
    # Step 1: Group points by coordinates
    coord_groups: dict[tuple[float, float], list[dict]] = {}
    for p in points:
        key = (round(p.get("lat", 0), GEO_DEDUP_DECIMALS), round(p.get("lon", 0), GEO_DEDUP_DECIMALS))
        coord_groups.setdefault(key, []).append(p)
    
    # Step 2: Build deduplicated list
    deduped: list[dict] = []
    seen_coords: set[tuple[float, float]] = set()
    
    for i, p in enumerate(points):
        key = (round(p.get("lat", 0), GEO_DEDUP_DECIMALS), round(p.get("lon", 0), GEO_DEDUP_DECIMALS))
        is_first = (i == 0)
        is_last = (i == len(points) - 1)
        
        # Always include first and last (endpoints)
        # For intermediate points, skip if already seen
        if not is_first and not is_last and key in seen_coords:
            continue
        
        # Create a copy to avoid mutating the original
        point = dict(p)
        
        # Add hop range annotation if multiple hops share this location
        group = coord_groups.get(key, [])
        if len(group) > 1:
            hop_nums = [pt.get("hop", 0) for pt in group]
            point[META_HOP_RANGE] = f"{min(hop_nums)}-{max(hop_nums)}"
            
            # Preserve segment_to_next=dashed from any point in the group
            # (if later hops in the same location have dashed segments,
            # the entire group should have a dashed segment to next)
            for group_point in group:
                if group_point.get("segment_to_next") == SEGMENT_DASHED:
                    point["segment_to_next"] = SEGMENT_DASHED
                    break
        
        deduped.append(point)
        seen_coords.add(key)
    
    return deduped
