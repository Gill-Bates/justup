#!/usr/bin/env python3
#
# app/services/provider_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Best-effort provider metadata for PDF reports (ASN/ISP from providers, location from the shared resolver)."""

from __future__ import annotations

import logging

from app.services import geo_resolver as gr
from app.services.ip_lookup import lookup_public_ip
from app.services.mtr_run import resolve_target_ip
from app.services.traceroute_geo import resolve_location
from app.utils.constants import (
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    PROVIDER_LOOKUP_CACHE_TTL_MAX,
    PROVIDER_LOOKUP_CACHE_TTL_MIN,
    SettingKeys,
)
from app.utils.runtime_settings import get_ip_api_key, get_runtime_bool_fail_closed, get_runtime_int

_log = logging.getLogger(__name__)


def _source_label(value: str | None) -> str:
    names = {"ip-api-pro": "ip-api Pro", "ipwho.is": "ipwho.is"}
    return " + ".join(names.get(part, part) for part in value.split(" + ")) if value else ""


_GEO_LABELS = {
    gr.SRC_GEOLITE2: "GeoLite2",
    gr.SRC_POP: "POP-Location-DB",
    gr.SRC_IP_API_PRO: "ip-api Pro",
    gr.SRC_IPWHO: "ipwho.is",
}


def _accuracy_text(decision: gr.GeoDecision) -> str:
    """Describe only evidenced accuracy; never invent a radius."""
    chosen = decision.selected
    if chosen is None:
        return ""
    if decision.accuracy_class != gr.ACCURACY_PRECISE and decision.contradictions:
        return "Approximate; sources disagree"
    if chosen.source == gr.SRC_GEOLITE2 and chosen.accuracy_radius_km is not None:
        return f"{chosen.accuracy_radius_km:g} km radius (67% confidence)"
    if chosen.source == gr.SRC_GEOLITE2:
        return "Approximate; radius unavailable"
    if chosen.source == gr.SRC_POP:
        return "POP/hostname mapping" if decision.accuracy_class == gr.ACCURACY_PRECISE else "Approximate; POP mapping"
    return "Approximate; provider does not publish an accuracy radius"


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
        # Sends the target IP to third-party providers: an unreadable store must read as disabled.
        if not get_runtime_bool_fail_closed(SettingKeys.PROVIDER_LOOKUP_ENABLED, True):
            return result

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

        # ASN/ISP comes from the providers; the location is the shared resolver decision.
        decision, _extras = resolve_location(ip, host)
        chosen = decision.selected
        if chosen:
            result["location"] = gr.format_place(decision.place_name, chosen.country_code) or "-"
            result["country_code"] = chosen.country_code or ""
            geo_label = _GEO_LABELS[chosen.source]
            if decision.place:  # position stays local; only the place name comes from the online source
                geo_label += f", place name {_GEO_LABELS[decision.place.source]}"
            result["source"] = f"{network_source} (ASN/ISP), {geo_label} (location)" if network_source else geo_label
            result["location_accuracy"] = _accuracy_text(decision)
        elif network_source:
            result["source"] = f"{network_source} (ASN/ISP)"
        return result
    except Exception as exc:  # noqa: BLE001 - optional report metadata must never abort report generation
        _log.warning("Provider lookup failed for %s (%s)", host, type(exc).__name__)
        return result
