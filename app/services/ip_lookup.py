#!/usr/bin/env python3
#
# app/services/ip_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared HTTPS lookup for public IP metadata used by reports and traceroutes.

Each online provider is queried independently with its own throttle state, so a
throttled or failing provider never disables the other. Geo selection is NOT done
here (see geo_resolver); this module only delivers per-provider results.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import re
import ssl
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Event, Lock
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from app.utils.constants import (
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    PROVIDER_LOOKUP_CACHE_TTL_MAX,
    PROVIDER_LOOKUP_CACHE_TTL_MIN,
)

_log = logging.getLogger(__name__)
_CACHE_SIZE = 2048
_NEGATIVE_TTL_SECONDS = 300
_TIMEOUT_SECONDS = 3.0
_MAX_RESPONSE_BYTES = 65536
# Wall-clock budget for the parallel online phase: provider timeout plus 1 s margin.
ONLINE_BUDGET_SECONDS = _TIMEOUT_SECONDS + 1.0
_RETRY_AFTER_DEFAULT = 60.0  # lock period for a 429 without a usable Retry-After
_RETRY_AFTER_MAX = 900.0
_BACKOFF_BASE = 15.0  # transient errors: 15 s, 30 s, ... capped
_BACKOFF_MAX = 300.0
SOURCE_IP_API_PRO = "ip-api-pro"
SOURCE_IPWHO = "ipwho.is"
_ASN_RE = re.compile(r"^(?:AS)?(\d+)$", re.IGNORECASE)


class IPInfo(TypedDict):
    ip: str
    lat: float | None
    lon: float | None
    city: str
    country: str
    country_code: str
    asn: str
    provider: str
    geo_source: str | None
    network_source: str | None


_cache: OrderedDict[tuple[str, str, str, int], tuple[float, IPInfo | None]] = OrderedDict()
_cache_lock = Lock()
_inflight: dict[tuple[str, str, str, int], Event] = {}  # single-flight: one request per key
_breaker: dict[tuple[str, str], tuple[float, int]] = {}  # (source, key fingerprint) -> (blocked_until, failures)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="geo-online")


class ProviderThrottledError(Exception):
    """The provider answered HTTP 429; ``retry_after`` is the validated lock period in seconds."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("provider throttled")
        self.retry_after = retry_after


def reset_provider_state() -> None:
    """Drop caches, in-flight markers and breaker state (tests and key rotation)."""
    with _cache_lock:
        _cache.clear()
        _breaker.clear()


def _fingerprint(source: str, api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16] if source == SOURCE_IP_API_PRO else ""


def _parse_retry_after(value: str | None) -> float | None:
    """Accept delta-seconds or an HTTP date; anything else is ignored (default lock applies)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = (
            float(value) if value.isdigit() else (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return min(max(seconds, 0.0), _RETRY_AFTER_MAX)


def _blocked(state_key: tuple[str, str], now: float) -> bool:
    with _cache_lock:
        entry = _breaker.get(state_key)
    return bool(entry and entry[0] > now)


def _record_outcome(state_key: tuple[str, str], now: float, *, ok: bool, retry_after: float | None = None) -> None:
    with _cache_lock:
        if ok:
            _breaker.pop(state_key, None)
        elif retry_after is not None:
            _breaker[state_key] = (now + retry_after, _breaker.get(state_key, (0.0, 0))[1])
        else:
            failures = _breaker.get(state_key, (0.0, 0))[1] + 1
            _breaker[state_key] = (now + min(_BACKOFF_BASE * 2 ** (failures - 1), _BACKOFF_MAX), failures)


def public_ip(value: str | None) -> str | None:
    """Return a canonical globally routable IP, never a hostname or internal address."""
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return None
    return str(address) if address.is_global else None


def _empty(ip: str) -> IPInfo:
    return {
        "ip": ip,
        "lat": None,
        "lon": None,
        "city": "",
        "country": "",
        "country_code": "",
        "asn": "",
        "provider": "",
        "geo_source": None,
        "network_source": None,
    }


def _text(value: Any) -> str:
    return " ".join(value.split())[:256] if isinstance(value, str) else ""


def _asn(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    match = _ASN_RE.match(str(value).split(" ", 1)[0].strip()) if value is not None else None
    return f"AS{int(match.group(1))}" if match and int(match.group(1)) > 0 else ""


def _coordinates(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    if (
        isinstance(lat, bool)
        or isinstance(lon, bool)
        or not isinstance(lat, (int, float))
        or not isinstance(lon, (int, float))
    ):
        return None, None
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return float(lat), float(lon)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _fetch_json(source: str, ip: str, api_key: str) -> dict[str, Any] | None:
    """Return the JSON payload, None on a transient failure; raise ProviderThrottledError on HTTP 429."""
    path = quote(ip, safe=":")
    if source == SOURCE_IP_API_PRO:
        query = urlencode({"fields": "status,country,countryCode,city,lat,lon,isp,as", "key": api_key})
        url = f"https://pro.ip-api.com/json/{path}?{query}"
    else:
        url = f"https://ipwho.is/{path}"

    # urllib does not emit request URLs through the httpx INFO logger, which could expose the key.
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    try:
        with opener.open(
            Request(url, headers={"Accept": "application/json"}),  # noqa: S310 - fixed HTTPS host, validated IP
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            if response.status != 200:
                return None
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(body)
        return payload if isinstance(payload, dict) else None
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise ProviderThrottledError(retry_after) from exc
        _log.debug("%s lookup for %s failed (HTTP %s)", source, ip, exc.code)
        return None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        _log.debug("%s lookup for %s failed (%s)", source, ip, type(exc).__name__)
        return None


def _normalize(source: str, ip: str, data: dict[str, Any]) -> IPInfo | None:
    if source == SOURCE_IP_API_PRO:
        if data.get("status") != "success":
            return None
        lat, lon = _coordinates(data.get("lat"), data.get("lon"))
        asn = _asn(data.get("as"))
        provider = _text(data.get("isp"))
        country_code = _text(data.get("countryCode")).upper()
    else:
        if data.get("success") is not True:
            return None
        lat, lon = _coordinates(data.get("latitude"), data.get("longitude"))
        connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
        asn = _asn(connection.get("asn"))
        provider = _text(connection.get("isp") or connection.get("org"))
        country_code = _text(data.get("country_code")).upper()

    result = _empty(ip)
    result.update(
        lat=lat,
        lon=lon,
        city=_text(data.get("city")),
        country=_text(data.get("country")),
        country_code=country_code,
        asn=asn,
        provider=provider,
        geo_source=source if lat is not None or lon is not None or country_code else None,
        network_source=source if asn or provider else None,
    )
    return result if result["geo_source"] or result["network_source"] or result["country_code"] else None


def _source_lookup(source: str, ip: str, api_key: str, ttl_seconds: int) -> IPInfo | None:
    fingerprint = _fingerprint(source, api_key)
    key = (source, ip, fingerprint, ttl_seconds)
    state_key = (source, fingerprint)
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            _cache.move_to_end(key)
            return dict(entry[1]) if entry[1] else None  # type: ignore[return-value]
        if entry:
            del _cache[key]
        waiter = _inflight.get(key)
        if waiter is None:
            _inflight[key] = Event()
    if waiter is not None:
        # Coalesce an identical concurrent request: wait for the leader, then read its cache entry.
        waiter.wait(_TIMEOUT_SECONDS + 1.0)
        with _cache_lock:
            entry = _cache.get(key)
        return dict(entry[1]) if entry and entry[1] else None  # type: ignore[return-value]

    try:
        if _blocked(state_key, now):
            return None  # locked: no request and no negative geo result is cached
        try:
            payload = _fetch_json(source, ip, api_key)
        except ProviderThrottledError as throttled:
            lock = _RETRY_AFTER_DEFAULT if throttled.retry_after is None else throttled.retry_after
            _record_outcome(state_key, now, ok=False, retry_after=lock)
            _log.warning("%s throttled (HTTP 429); paused for %.0f s", source, lock)
            return None
        if payload is None:
            _record_outcome(state_key, now, ok=False)  # transient: bounded backoff, nothing cached
            return None
        _record_outcome(state_key, now, ok=True)
        result = _normalize(source, ip, payload)
        expiry = now + (ttl_seconds if result else min(ttl_seconds, _NEGATIVE_TTL_SECONDS))
        with _cache_lock:
            _cache[key] = (expiry, result)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
        return dict(result) if result else None  # type: ignore[return-value]
    finally:
        with _cache_lock:
            done = _inflight.pop(key, None)
        if done is not None:
            done.set()


def lookup_online_sources(
    ip: str,
    *,
    api_key: str = "",
    ttl_seconds: int = PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    budget_seconds: float = ONLINE_BUDGET_SECONDS,
) -> tuple[tuple[str, ...], Mapping[str, IPInfo | None]]:
    """Query every available provider in parallel; return (attempted sources, results by source).

    ip-api Pro is only attempted with a key. Results are collected, never ranked by arrival;
    a provider still running when the budget ends counts as no result (it still fills the cache).
    """
    normalized_ip = public_ip(ip)
    if normalized_ip is None:
        return (), {}
    ttl_seconds = max(PROVIDER_LOOKUP_CACHE_TTL_MIN, min(PROVIDER_LOOKUP_CACHE_TTL_MAX, ttl_seconds))
    sources = ((SOURCE_IP_API_PRO,) if api_key else ()) + (SOURCE_IPWHO,)
    futures = {
        source: _executor.submit(
            _source_lookup, source, normalized_ip, api_key if source == SOURCE_IP_API_PRO else "", ttl_seconds
        )
        for source in sources
    }
    wait(list(futures.values()), timeout=budget_seconds)
    results: dict[str, IPInfo | None] = {}
    for source, future in futures.items():
        try:
            results[source] = future.result(timeout=0) if future.done() else None
        except Exception as exc:  # noqa: BLE001 - one provider failing must not discard the others
            _log.debug("%s lookup for %s failed (%s)", source, normalized_ip, type(exc).__name__)
            results[source] = None
    return sources, results


def _sources(*values: str | None) -> str | None:
    unique = dict.fromkeys(value for value in values if value)
    return " + ".join(unique) if unique else None


def lookup_public_ip(
    ip: str,
    *,
    api_key: str = "",
    ttl_seconds: int = PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
) -> IPInfo | None:
    """ASN/ISP metadata merged field-by-field in provider priority (ip-api Pro, then ipwho.is).

    The geo fields mirror the highest-priority provider that has a position; they are NOT the
    selected location (geo_resolver decides that), so ASN display never alters it.
    """
    _attempted, results = lookup_online_sources(ip, api_key=api_key, ttl_seconds=ttl_seconds)
    ordered = [info for info in results.values() if info]
    if not ordered:
        return None
    merged = dict(ordered[0])
    network_sources = [ordered[0]["network_source"]]
    for info in ordered[1:]:
        for field in ("asn", "provider"):
            if not merged[field] and info[field]:
                merged[field] = info[field]
                network_sources.append(info["network_source"])
    if merged["lat"] is None:
        geo = next((info for info in ordered if info["lat"] is not None), None)
        if geo:
            for field in ("lat", "lon", "city", "country", "country_code", "geo_source"):
                merged[field] = geo[field]
    merged["network_source"] = _sources(*network_sources)
    return merged  # type: ignore[return-value]
