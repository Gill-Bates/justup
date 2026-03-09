# cython: language_level=3
# cython: boundscheck=False, wraparound=False

"""Provider info lookup (IP, ASN, ISP, rough location).

Used by PDF reports and other "nice-to-have" metadata sections.

Notes:
- This module may call external services. It is configurable and can be disabled.
- Results are cached with a TTL to reduce rate-limit issues during batch runs.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from typing import Any

# httpx is optional - degrade gracefully if not installed
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False

from app.services.mtr_run import resolve_target_ip

_log = logging.getLogger(__name__)

if not _HAS_HTTPX:
    _log.info("httpx not installed, provider lookup disabled (nice-to-have metadata)")

_PROVIDER_LOOKUP_ENABLED = os.getenv("JUSTUP_PROVIDER_LOOKUP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

# Safe parsing of TTL env var - crash on invalid config is unfriendly
_DEFAULT_CACHE_TTL = 1800  # 30 min
try:
    _PROVIDER_LOOKUP_CACHE_TTL_SECONDS = int(os.getenv("JUSTUP_PROVIDER_LOOKUP_CACHE_TTL_SECONDS", str(_DEFAULT_CACHE_TTL)))
except ValueError:
    _log.warning("Invalid JUSTUP_PROVIDER_LOOKUP_CACHE_TTL_SECONDS, using default %d", _DEFAULT_CACHE_TTL)
    _PROVIDER_LOOKUP_CACHE_TTL_SECONDS = _DEFAULT_CACHE_TTL

_PROVIDER_LOOKUP_CACHE_MAX_SIZE = 2048

# Negative cache TTL: avoid hammering external APIs when they're down
# Use positive TTL capped at 5 minutes to balance freshness vs. spam
_NEGATIVE_CACHE_TTL_SECONDS = min(300, _PROVIDER_LOOKUP_CACHE_TTL_SECONDS)

# ip-api.com: HTTPS requires Pro endpoint + API key (https://pro.ip-api.com/)
# Note: API key is passed as URL query param (ip-api doesn't support header auth).
# Be aware that DEBUG-level httpx logging may expose the key.
_IP_API_KEY = os.getenv("JUSTUP_IP_API_KEY", "").strip()

# Optional explicit opt-in: allow unencrypted free ip-api endpoint (discouraged).
_ALLOW_INSECURE_IP_API_HTTP = os.getenv("JUSTUP_ALLOW_INSECURE_IP_API_HTTP", "false").strip().lower() in {"1", "true", "yes", "on"}

# Cache: ip -> (data, expires_at, inserted_at)
# NOTE: Uses threading.Lock (not asyncio.Lock) because all HTTP calls are synchronous.
# This is intentional for simplicity - provider lookup is best-effort metadata,
# not critical path. If async context needed, wrap calls in asyncio.to_thread().
_provider_info_cache: dict[str, tuple[dict[str, str], float, float]] = {}
_provider_info_cache_lock = threading.Lock()


def _cache_get_provider_info(ip: str) -> dict[str, str] | None:
    now = time.monotonic()
    with _provider_info_cache_lock:
        entry = _provider_info_cache.get(ip)
        if not entry:
            return None
        value, expires_at, _ = entry
        if now < expires_at:
            return value
        # Expired -> drop
        _provider_info_cache.pop(ip, None)
        return None


def _cache_set_provider_info(ip: str, value: dict[str, str], *, ttl_seconds: int) -> None:
    now = time.monotonic()
    with _provider_info_cache_lock:
        # Sweep expired entries first (cheap, avoids evicting valid ones)
        expired = [k for k, (_, exp, _) in _provider_info_cache.items() if now >= exp]
        for k in expired:
            del _provider_info_cache[k]

        if len(_provider_info_cache) >= _PROVIDER_LOOKUP_CACHE_MAX_SIZE:
            # Evict the oldest 25% (dict preserves insertion order in Python 3.7+)
            keys = list(_provider_info_cache.keys())
            evict_count = max(1, len(keys) // 4)
            for k in keys[:evict_count]:
                _provider_info_cache.pop(k, None)

        _provider_info_cache[ip] = (value, now + ttl_seconds, now)


def _validate_ip_for_external_lookup(ip: str) -> str | None:
    """
    Validate IP address before sending to external APIs.
    
    Returns normalized IP string, or None if invalid/private.
    Rejects:
    - Invalid IP formats (prevents URL manipulation/SSRF)
    - Private/loopback IPs (no point querying external APIs, leaks topology)
    """
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        _log.debug("Invalid IP format for provider lookup: %s", ip)
        return None
    
    # Reject private/loopback/reserved IPs - external APIs can't resolve them
    # and sending them leaks internal network topology
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
    ):
        _log.debug("Skipping external lookup for non-public IP: %s", ip)
        return None
    
    return str(ip_obj)


def _fetch_json(
    url: str,
    params: dict[str, str] | None = None,
    *,
    label: str,
) -> dict[str, Any] | None:
    """
    Common HTTP GET + JSON parse with unified error handling.
    
    Returns parsed JSON dict, or None on any failure.
    Does NOT follow redirects (SSRF mitigation for API endpoints).
    """
    if not _HAS_HTTPX:
        return None
    
    try:
        resp = httpx.get(
            url,
            params=params,
            timeout=3.0,
            follow_redirects=False,  # SSRF mitigation: don't follow redirects from geo APIs
        )
        if resp.status_code == 429:
            _log.warning("%s rate limited (429)", label)
            return None
        if resp.status_code != 200:
            _log.warning("%s returned status %s", label, resp.status_code)
            return None
        return resp.json()
    except httpx.TimeoutException:
        _log.warning("%s request timed out", label)
    except httpx.RequestError as e:
        _log.warning("%s request failed: %s", label, e)
    except Exception as e:
        _log.warning("%s lookup failed: %s", label, e)
    return None


def _fetch_provider_info_from_ip_api(ip_address: str, *, use_http: bool) -> dict[str, str] | None:
    out = {"asn": "-", "provider": "-", "location": "-", "country_code": ""}

    if not ip_address:
        return None
    
    # Validate IP before sending to external API
    validated_ip = _validate_ip_for_external_lookup(ip_address)
    if not validated_ip:
        return None

    if not use_http and not _IP_API_KEY:
        # ip-api free endpoint is HTTP-only (no TLS). Without a key, skip.
        return None

    base = "http://ip-api.com/json" if use_http else "https://pro.ip-api.com/json"
    params: dict[str, str] = {
        "fields": "status,message,country,countryCode,city,isp,as",
    }
    if not use_http:
        params["key"] = _IP_API_KEY

    data = _fetch_json(f"{base}/{validated_ip}", params, label=f"ip-api.com/{validated_ip}")
    if data is None:
        return None
    
    if data.get("status") != "success":
        _log.warning("ip-api.com returned error for %s: %s", validated_ip, data.get("message", "unknown"))
        return None

    as_info = data.get("as", "")
    if as_info:
        parts = str(as_info).split(" ", 1)
        out["asn"] = parts[0] if parts else str(as_info)

    out["provider"] = str(data.get("isp", "-") or "-")
    out["country_code"] = str(data.get("countryCode", "") or "").strip().upper()

    city = data.get("city", "")
    country = data.get("country", "")
    if city and country:
        out["location"] = f"{city}, {country}"
    elif country:
        out["location"] = str(country)

    return out


def _fetch_provider_info_from_ipwhois(ip_address: str) -> dict[str, str] | None:
    out = {"asn": "-", "provider": "-", "location": "-", "country_code": ""}

    if not ip_address:
        return None
    
    # Validate IP before sending to external API
    validated_ip = _validate_ip_for_external_lookup(ip_address)
    if not validated_ip:
        return None

    data = _fetch_json(f"https://ipwho.is/{validated_ip}", label=f"ipwho.is/{validated_ip}")
    if data is None:
        return None
    
    if not data.get("success", False):
        _log.warning("ipwho.is returned error for %s: %s", validated_ip, data.get("message", "unknown"))
        return None

    cc = data.get("country_code") or data.get("countryCode") or ""
    if isinstance(cc, str):
        out["country_code"] = cc.strip().upper()

    city = data.get("city") or ""
    country = data.get("country") or ""
    if city and country:
        out["location"] = f"{city}, {country}"
    elif country:
        out["location"] = str(country)

    connection = data.get("connection") or {}
    if isinstance(connection, dict):
        asn = connection.get("asn")
        if asn:
            # Normalize to "AS1234" format (some APIs return just the number)
            asn_s = str(asn).strip()
            out["asn"] = asn_s if asn_s.upper().startswith("AS") else f"AS{asn_s}"

        provider = connection.get("isp") or connection.get("org") or "-"
        out["provider"] = str(provider).strip() if provider else "-"

    return out


def lookup_provider_info(host: str) -> dict[str, str]:
    """Resolve host IP and fetch provider info (ASN/Provider/Location).

    Returns dict with keys: ip, asn, provider, location, country_code
    """
    result = {
        "ip": "-",
        "asn": "-",
        "provider": "-",
        "location": "-",
        "country_code": "",
    }

    if not host:
        return result

    try:
        # Use same resolution logic as MTR for consistency.
        # This ensures provider info matches the actual traced path.
        ip = resolve_target_ip(host)
        if not ip:
            return result
        result["ip"] = ip

        if not _PROVIDER_LOOKUP_ENABLED:
            return result
        
        if not _HAS_HTTPX:
            # httpx not installed - gracefully skip provider lookup
            return result

        cached = _cache_get_provider_info(ip)
        if cached:
            result.update(cached)
            return result

        provider_info = None

        # Prefer TLS-only lookups. ip-api free endpoint is HTTP-only (discouraged).
        if _IP_API_KEY:
            provider_info = _fetch_provider_info_from_ip_api(ip, use_http=False)
        elif _ALLOW_INSECURE_IP_API_HTTP:
            _log.warning(
                "Using unencrypted ip-api.com HTTP endpoint for provider lookup "
                "(JUSTUP_ALLOW_INSECURE_IP_API_HTTP enabled)."
            )
            provider_info = _fetch_provider_info_from_ip_api(ip, use_http=True)

        # Fallback to HTTPS provider if ip-api is not configured / fails.
        if provider_info is None:
            provider_info = _fetch_provider_info_from_ipwhois(ip)

        if provider_info is None:
            # Negative cache to avoid spamming external services on batch runs.
            # Uses scaled TTL (capped at 5min) to balance freshness vs. API abuse.
            _cache_set_provider_info(
                ip,
                {"asn": "-", "provider": "-", "location": "-", "country_code": ""},
                ttl_seconds=_NEGATIVE_CACHE_TTL_SECONDS,
            )
            return result

        _cache_set_provider_info(ip, provider_info, ttl_seconds=_PROVIDER_LOOKUP_CACHE_TTL_SECONDS)
        result.update(provider_info)
        return result
    except Exception as e:
        # Use .exception() to get traceback - helps debug programming errors
        _log.exception("Provider lookup failed for %s: %s", host, e)
        return result

