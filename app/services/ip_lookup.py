#!/usr/bin/env python3
#
# app/services/ip_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared HTTPS lookup for public IP metadata used by reports and traceroutes."""

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
from threading import Lock
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
    path = quote(ip, safe=":")
    if source == "ip-api-pro":
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
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        _log.debug("%s lookup for %s failed (%s)", source, ip, type(exc).__name__)
        return None


def _normalize(source: str, ip: str, data: dict[str, Any]) -> IPInfo | None:
    if source == "ip-api-pro":
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
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest() if source == "ip-api-pro" else ""
    key = (source, ip, fingerprint, ttl_seconds)
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            _cache.move_to_end(key)
            return dict(entry[1]) if entry[1] else None  # type: ignore[return-value]
        if entry:
            del _cache[key]

    payload = _fetch_json(source, ip, api_key)
    result = _normalize(source, ip, payload) if payload else None
    expiry = now + (ttl_seconds if result else min(ttl_seconds, _NEGATIVE_TTL_SECONDS))
    with _cache_lock:
        _cache[key] = (expiry, result)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return dict(result) if result else None  # type: ignore[return-value]


def _complete_geo(info: IPInfo | None) -> bool:
    return bool(info and info["lat"] is not None and info["lon"] is not None and info["city"] and info["country_code"])


def _complete_network(info: IPInfo | None) -> bool:
    return bool(info and info["asn"] and info["provider"])


def _sources(*values: str | None) -> str | None:
    unique = dict.fromkeys(value for value in values if value)
    return " + ".join(unique) if unique else None


def lookup_public_ip(
    ip: str,
    *,
    api_key: str = "",
    need_geo: bool = True,
    need_network: bool = True,
    ttl_seconds: int = PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
) -> IPInfo | None:
    """Prefer ip-api Pro when configured, then fill missing groups from ipwho.is."""
    normalized_ip = public_ip(ip)
    if normalized_ip is None:
        return None
    ttl_seconds = max(PROVIDER_LOOKUP_CACHE_TTL_MIN, min(PROVIDER_LOOKUP_CACHE_TTL_MAX, ttl_seconds))
    primary_source = "ip-api-pro" if api_key else "ipwho.is"
    primary = _source_lookup(primary_source, normalized_ip, api_key, ttl_seconds)
    fallback = None
    if api_key and ((need_geo and not _complete_geo(primary)) or (need_network and not _complete_network(primary))):
        fallback = _source_lookup("ipwho.is", normalized_ip, "", ttl_seconds)
    if primary is None and fallback is None:
        return None

    merged = dict(primary or fallback or _empty(normalized_ip))
    if primary and fallback:
        geo_sources = [primary["geo_source"]]
        network_sources = [primary["network_source"]]
        # Coordinates and city are treated as one location to avoid a false combination.
        if (not _complete_geo(primary) and _complete_geo(fallback)) or (
            primary["lat"] is None and fallback["lat"] is not None
        ):
            for field in ("lat", "lon", "city", "country", "country_code"):
                merged[field] = fallback[field]
            geo_sources = [fallback["geo_source"]]
        for field in ("asn", "provider"):
            if not merged[field] and fallback[field]:
                merged[field] = fallback[field]
                network_sources.append(fallback["network_source"])
        merged["geo_source"] = _sources(*geo_sources)
        merged["network_source"] = _sources(*network_sources)
    return merged
