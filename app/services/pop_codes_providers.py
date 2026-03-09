#!/usr/bin/env python3
#
# app/services/pop_codes_providers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Provider-specific hostname parsers for POP code extraction.

Each carrier uses a distinct hostname convention. Instead of trying to 
handle all cases with generic regex, we dispatch to provider-specific 
parsers based on domain suffix matching.

Architecture:
- Registry of (domain_pattern, parser_function) pairs
- Parsers return extracted POP code + confidence
- Falls back to generic extraction if no provider matches

References for hostname conventions:
- https://github.com/activecm/espy (crowd-sourced ISP patterns)
- NANOG mailing list archives
- Personal observation from traceroute data
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass
class ProviderPOP:
    """Result from a provider-specific parser."""
    pop_code: str       # Extracted POP code (lowercase, e.g., "fra")
    provider: str       # Provider identifier
    confidence: float   # 0.0-1.0
    role: str = ""      # Optional: "core", "edge", "ix", "peer"


# ---------------------------------------------------------------------------
# Provider parser registry
# ---------------------------------------------------------------------------

_PROVIDER_PARSERS: list[tuple[str, re.Pattern, callable]] = []


def _register(provider: str, domain_pattern: str):
    """Decorator to register a provider-specific parser."""
    compiled = re.compile(domain_pattern, re.IGNORECASE)
    
    def decorator(func):
        _PROVIDER_PARSERS.append((provider, compiled, func))
        return func
    return decorator


def parse_provider_hostname(hostname: str) -> Optional[ProviderPOP]:
    """
    Try all registered provider parsers.
    Returns the first successful match, or None.
    """
    if not hostname:
        return None
    
    h = hostname.lower().strip()
    for provider, pattern, parser in _PROVIDER_PARSERS:
        if pattern.search(h):
            try:
                result = parser(h)
                if result:
                    return result
            except Exception as e:
                _log.debug("Provider parser %s failed: %s", provider, e)
    return None


# ---------------------------------------------------------------------------
# Deutsche Telekom / DTAG
# Format: ae1.cr-frankfurt1.de.net.dtag.de
#         cr-berlin1.de.net.dtag.de
# POP is in the city name after cr-/ar-/pr-
# ---------------------------------------------------------------------------
@_register("dtag", r"\.dtag\.de$")
def _parse_dtag(hostname: str) -> Optional[ProviderPOP]:
    m = re.search(r"[.-](?:cr|ar|pr|br)-([a-z]+)\d*\.", hostname)
    if m:
        return ProviderPOP(
            pop_code=m.group(1)[:8],
            provider="dtag",
            confidence=0.9,
            role="core" if "cr-" in hostname else "edge",
        )
    return None


# ---------------------------------------------------------------------------
# Telia Carrier
# Format: s-bb4-link.telia.net  → Stockholm (s)
#         hbg-b1.telia.net      → Hamburg (hbg)
#         fra-b12.telia.net     → Frankfurt
#         nyk-bb1-link.telia.net → New York
# POP code is the first segment
# ---------------------------------------------------------------------------
@_register("telia", r"\.telia\.net$")
def _parse_telia(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    if parts:
        first = parts[0]
        m = re.match(r"^([a-z]{2,6})", first)
        if m:
            code = m.group(1)
            if code not in {"link", "peer", "cust"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="telia",
                    confidence=0.85,
                )
    return None


# ---------------------------------------------------------------------------
# Lumen / CenturyLink / Level3
# Format: ae28-100.bar2.Frankfurt1.Level3.net
#         ae1.3112.edge4.Frankfurt1.level3.net
#         ae1.3602.edge4.Berlin1.Level3.net
# POP is the city name segment
# ---------------------------------------------------------------------------
@_register("level3", r"\.level3\.net$")
def _parse_level3(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{4,15})\d*$", part)
        if m:
            city = m.group(1)
            if city not in {"level3", "edge", "core", "bar", "net"}:
                return ProviderPOP(
                    pop_code=city,
                    provider="level3",
                    confidence=0.85,
                )
    return None


# ---------------------------------------------------------------------------
# GTT Communications
# Format: ae3.cr2.fra6.ip.gtt.net
# POP code is IATA+number segment
# ---------------------------------------------------------------------------
@_register("gtt", r"\.gtt\.net$")
def _parse_gtt(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{3})\d+$", part)
        if m:
            code = m.group(1)
            if code not in {"ip", "net", "gtt"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="gtt",
                    confidence=0.9,
                )
    return None


# ---------------------------------------------------------------------------
# NTT / Verio / GIN
# Format: ae-5.r24.frnkge17.de.bb.gin.ntt.net
#         xe-0-0-3.r00.tokyjp09.jp.bb.gin.ntt.net
# POP code is the "city+country" segment (frnkge = Frankfurt Germany)
# First 3-4 chars are the POP code
# ---------------------------------------------------------------------------
@_register("ntt", r"\.ntt\.net$")
def _parse_ntt(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        # NTT uses 6-10 char city codes: frnkge17, tokyjp09, londen14
        m = re.match(r"^([a-z]{4,6})[a-z]{2}\d{2}$", part)
        if m:
            return ProviderPOP(
                pop_code=m.group(1),
                provider="ntt",
                confidence=0.9,
            )
    return None


# ---------------------------------------------------------------------------
# Cogent Communications
# Format: be3593.ccr21.fra03.atlas.cogentco.com
#         be3282.agr21.ams01.atlas.cogentco.com
# POP code is IATA+number (fra03, ams01, ber01)
# ---------------------------------------------------------------------------
@_register("cogent", r"\.cogentco\.com$")
def _parse_cogent(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{3})\d{2}$", part)
        if m:
            code = m.group(1)
            if code not in {"ccr", "agr", "bbr"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="cogent",
                    confidence=0.9,
                )
    return None


# ---------------------------------------------------------------------------
# Zayo
# Format: ae3.mpr1.lax11.us.zip.zayo.com
#         ae1.cs1.fra6.de.zip.zayo.com
# POP = IATA+number segment
# ---------------------------------------------------------------------------
@_register("zayo", r"\.zayo\.com$")
def _parse_zayo(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{3})\d+$", part)
        if m:
            code = m.group(1)
            if code not in {"zip", "mpr", "cs"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="zayo",
                    confidence=0.9,
                )
    return None


# ---------------------------------------------------------------------------
# Arelion (formerly Telia Carrier International)
# Format: prs-bb4-link.ip4.tinet.net
# ---------------------------------------------------------------------------
@_register("tinet", r"\.tinet\.net$")
def _parse_tinet(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    if parts:
        m = re.match(r"^([a-z]{3})", parts[0])
        if m:
            code = m.group(1)
            if code not in {"ip4", "ip6"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="tinet",
                    confidence=0.85,
                )
    return None


# ---------------------------------------------------------------------------
# Hurricane Electric
# Format: 10ge16-1.core1.fra1.he.net
#         100ge3-1.core1.ams1.he.net
# POP = segment matching [a-z]{3}\d
# ---------------------------------------------------------------------------
@_register("he", r"\.he\.net$")
def _parse_he(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{3})\d+$", part)
        if m:
            code = m.group(1)
            if code not in {"he", "net", "core"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="he",
                    confidence=0.9,
                )
    return None


# ---------------------------------------------------------------------------
# Colt Technology Services
# Format: ae2.3603.edge4.ber1.neo.colt.net
#         te0-0-0-21.ccr21.ber01.atlas.cogentco.com
# POP = IATA+number segment
# ---------------------------------------------------------------------------
@_register("colt", r"\.colt\.net$")
def _parse_colt(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        m = re.match(r"^([a-z]{3})\d*$", part)
        if m:
            code = m.group(1)
            if code not in {"neo", "colt", "net", "edge"}:
                return ProviderPOP(
                    pop_code=code,
                    provider="colt",
                    confidence=0.85,
                )
    return None


# ---------------------------------------------------------------------------
# DE-CIX (German Internet Exchange)
# Format: ae0.core1.fra.de-cix.net
# ---------------------------------------------------------------------------
@_register("de-cix", r"\.de-cix\.net$")
def _parse_decix(hostname: str) -> Optional[ProviderPOP]:
    parts = hostname.split(".")
    for part in parts:
        # DE-CIX uses plain IATA codes
        if len(part) == 3 and part.isalpha():
            if part not in {"net", "cix"}:
                return ProviderPOP(
                    pop_code=part,
                    provider="de-cix",
                    confidence=0.95,
                )
    return None


# ---------------------------------------------------------------------------
# AMS-IX (Amsterdam Internet Exchange)
# Format: routeserver.ams-ix.net
# ---------------------------------------------------------------------------
@_register("ams-ix", r"\.ams-ix\.net$")
def _parse_amsix(hostname: str) -> Optional[ProviderPOP]:
    return ProviderPOP(
        pop_code="ams",
        provider="ams-ix",
        confidence=0.95,
    )


# ---------------------------------------------------------------------------
# LINX (London Internet Exchange)
# Format: *.linx.net
# ---------------------------------------------------------------------------
@_register("linx", r"\.linx\.net$")
def _parse_linx(hostname: str) -> Optional[ProviderPOP]:
    return ProviderPOP(
        pop_code="lon",
        provider="linx",
        confidence=0.95,
    )


# ---------------------------------------------------------------------------
# CLLI Code Parser (US/Canada Telcos)
#
# Common Language Location Identifier – used by AT&T, Lumen, Verizon etc.
# Format: NYCMNY01 = New York City, Manhattan, NY, office 01
#         DLLSTX09 = Dallas, TX, office 09
#         CHCGIL09 = Chicago, IL, office 09
#
# Structure: [4 city][2 state][2 sequence]
# ---------------------------------------------------------------------------

# Top ~50 US CLLI city codes
_CLLI_CITY_CODES: dict[str, tuple[str, str, float, float]] = {
    "nycm": ("New York", "US", 40.7128, -74.0060),
    "lsaj": ("Los Angeles", "US", 34.0522, -118.2437),
    "chcg": ("Chicago", "US", 41.8781, -87.6298),
    "dlls": ("Dallas", "US", 32.7767, -96.7970),
    "dllx": ("Dallas", "US", 32.7767, -96.7970),
    "hstx": ("Houston", "US", 29.7604, -95.3698),
    "snjp": ("San Jose", "US", 37.3382, -121.8863),
    "snfr": ("San Francisco", "US", 37.7749, -122.4194),
    "sttl": ("Seattle", "US", 47.6062, -122.3321),
    "dnvr": ("Denver", "US", 39.7392, -104.9903),
    "atlg": ("Atlanta", "US", 33.7490, -84.3880),
    "miam": ("Miami", "US", 25.7617, -80.1918),
    "bstn": ("Boston", "US", 42.3601, -71.0589),
    "phla": ("Philadelphia", "US", 39.9526, -75.1652),
    "wash": ("Washington DC", "US", 38.9072, -77.0369),
    "asbn": ("Ashburn", "US", 39.0438, -77.4874),
    "ptld": ("Portland", "US", 45.5152, -122.6784),
    "mnps": ("Minneapolis", "US", 44.9778, -93.2650),
    "kcmo": ("Kansas City", "US", 39.0997, -94.5786),
    "slkc": ("Salt Lake City", "US", 40.7608, -111.8910),
    "tamp": ("Tampa", "US", 27.9506, -82.4572),
    "phnx": ("Phoenix", "US", 33.4484, -112.074),
    # Canada
    "toab": ("Toronto", "CA", 43.6532, -79.3832),
    "toro": ("Toronto", "CA", 43.6532, -79.3832),
    "mtrl": ("Montreal", "CA", 45.5017, -73.5673),
    "vnvr": ("Vancouver", "CA", 49.2827, -123.1207),
}

_CLLI_PATTERN = re.compile(r"\b([a-z]{4})([a-z]{2})\d{2}\b")


def parse_clli_code(hostname: str) -> Optional[ProviderPOP]:
    """
    Attempt to parse a hostname containing a CLLI code.
    Returns ProviderPOP if the 4-char city prefix is known.
    """
    h = hostname.lower()
    
    for m in _CLLI_PATTERN.finditer(h):
        city_code = m.group(1)
        if city_code in _CLLI_CITY_CODES:
            _, _, _, _ = _CLLI_CITY_CODES[city_code]
            return ProviderPOP(
                pop_code=city_code,
                provider="clli",
                confidence=0.85,
            )
    return None


# Register CLLI parser for common US carriers
@_register("att", r"\.(att|sbc|sbcglobal)\.net$")
def _parse_att(hostname: str) -> Optional[ProviderPOP]:
    return parse_clli_code(hostname)


@_register("verizon", r"\.verizon\.net$")
def _parse_verizon(hostname: str) -> Optional[ProviderPOP]:
    return parse_clli_code(hostname)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

__all__ = [
    "ProviderPOP",
    "parse_provider_hostname",
    "parse_clli_code",
]
