#!/usr/bin/env python3
#
# app/services/provider_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Best-effort provider metadata for PDF reports."""

from __future__ import annotations

import logging

from app.services.ip_lookup import lookup_public_ip
from app.services.mtr_run import resolve_target_ip
from app.services.traceroute_geo import MAX_CITY_RADIUS_KM, geolocate_ip
from app.utils.constants import (
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    PROVIDER_LOOKUP_CACHE_TTL_MAX,
    PROVIDER_LOOKUP_CACHE_TTL_MIN,
    SettingKeys,
)
from app.utils.runtime_settings import get_ip_api_key, get_runtime_bool, get_runtime_int

_log = logging.getLogger(__name__)


def _source_label(value: str | None) -> str:
    names = {"ip-api-pro": "ip-api Pro", "ipwho.is": "ipwho.is"}
    return " + ".join(names.get(part, part) for part in value.split(" + ")) if value else ""


def _local_location_rank(location: dict | None) -> int:
    """Rank local GeoLite2 detail by its explicit accuracy metadata."""
    if not location:
        return 0
    city = location.get("city")
    if not city or location.get("city_is_region"):
        return 1
    radius = location.get("accuracy_radius_km")
    if isinstance(radius, (int, float)) and not isinstance(radius, bool):
        return 3 if 0 <= radius <= MAX_CITY_RADIUS_KM else 2
    return 1


def _online_location_rank(info: dict | None) -> int:
    """Online providers do not publish a comparable accuracy radius."""
    if not info:
        return 0
    if info.get("lat") is not None and info.get("lon") is not None and info.get("city"):
        return 2
    return 1 if info.get("city") or info.get("country") else 0


def lookup_provider_info(host: str) -> dict[str, str]:
    """Resolve an IP and return provider metadata with the best-supported location."""
    result = {
        "ip": "-",
        "asn": "-",
        "provider": "-",
        "location": "-",
        "country_code": "",
        "source": "",
        "location_accuracy": "",
    }
    if not host:
        return result
    try:
        ip = resolve_target_ip(host)
        if not ip:
            return result
        result["ip"] = ip
        if not get_runtime_bool(SettingKeys.PROVIDER_LOOKUP_ENABLED, True):
            return result

        local = geolocate_ip(ip)
        info = lookup_public_ip(
            ip,
            api_key=get_ip_api_key(),
            ttl_seconds=get_runtime_int(
                SettingKeys.PROVIDER_LOOKUP_CACHE_TTL_SECONDS,
                PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
                minimum=PROVIDER_LOOKUP_CACHE_TTL_MIN,
                maximum=PROVIDER_LOOKUP_CACHE_TTL_MAX,
            ),
        )
        network_source = ""
        if info:
            result["asn"] = info["asn"] or "-"
            result["provider"] = info["provider"] or "-"
            network_source = _source_label(info["network_source"])

        online_rank = _online_location_rank(info)
        local_rank = _local_location_rank(local)
        if local_rank >= online_rank and local:
            result["location"] = local.get("city") or local.get("country") or "-"
            result["country_code"] = local.get("country") or ""
            result["source"] = f"{network_source} (ASN/ISP), GeoLite2 (location)" if network_source else "GeoLite2"
            radius = local.get("accuracy_radius_km")
            if isinstance(radius, (int, float)) and not isinstance(radius, bool):
                result["location_accuracy"] = f"{radius:g} km radius (67% confidence)"
            else:
                result["location_accuracy"] = "Approximate; radius unavailable"
        elif online_rank and info:
            if info["city"] and info["country"]:
                result["location"] = f"{info['city']}, {info['country']}"
            elif info["city"]:
                result["location"] = info["city"]
            elif info["country"]:
                result["location"] = info["country"]
            result["country_code"] = info["country_code"]
            geo_source = _source_label(info["geo_source"])
            result["source"] = (
                f"{network_source} (ASN/ISP), {geo_source} (location)"
                if network_source and geo_source and network_source != geo_source
                else network_source or geo_source
            )
            result["location_accuracy"] = "Approximate; provider does not publish an accuracy radius"
        return result
    except Exception as exc:  # noqa: BLE001 - optional report metadata must never abort report generation
        _log.warning("Provider lookup failed for %s (%s)", host, type(exc).__name__)
        return result
