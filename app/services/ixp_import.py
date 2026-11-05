#!/usr/bin/env python3
#
# app/services/ixp_import.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# app/services/ixp_import.py  # noqa: ERA001 - canonical file header, not code
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Build a validated IXP prefix snapshot from PeeringDB tables (pure, no I/O).

Inputs are the raw ``data`` lists of PeeringDB ``ix``, ``ixlan``, ``ixpfx``, ``ixfac`` and ``fac``.
Everything is validated before anything is stored: inactive, malformed, inconsistent and contradicting
records are excluded and counted. A prefix proves membership of an exchange LAN; the exchange region is
derived only from facility evidence and never from the exchange's city name alone.
"""

from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..db.ixp_prefixes import (
    LOC_CONFLICTING,
    LOC_DISTRIBUTED,
    LOC_METRO,
    LOC_UNKNOWN,
    IxpExchange,
    IxpPrefix,
    IxpSnapshot,
)

# An IXP LAN is never broader than this; anything shorter would capture unrelated address space.
MIN_PREFIX_LENGTH = {4: 16, 6: 32}
# Facilities must lie within this distance of their centroid to count as ONE metro region.
METRO_MAX_RADIUS_KM = 50.0
# The drawn region radius never shrinks below this: a centroid of few facilities is not a point fix.
METRO_MIN_RADIUS_KM = 15.0
MAX_NAME_LENGTH = 128
MAX_CITY_LENGTH = 64

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PROTOCOL_VERSION = {"IPv4": 4, "IPv6": 6}


@dataclass(slots=True)
class IxpBuildResult:
    snapshot: IxpSnapshot
    excluded: dict[str, int] = field(default_factory=dict)


def _int_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub("", value).strip()
    return cleaned if 0 < len(cleaned) <= limit else None


def _country(value: object) -> str | None:
    code = value.strip().upper() if isinstance(value, str) else ""
    return code if len(code) == 2 and code.isalpha() and code.isascii() else None


def _active(row: object) -> bool:
    return isinstance(row, dict) and row.get("status") == "ok"


def _norm_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def _names_agree(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    return bool(na and nb and (na in nb or nb in na))


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742.0 * math.asin(min(1.0, math.sqrt(h)))


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Mean on the unit sphere, so points on both sides of the antimeridian average correctly."""
    x = y = z = 0.0
    for lat, lon in points:
        la, lo = math.radians(lat), math.radians(lon)
        x, y, z = x + math.cos(la) * math.cos(lo), y + math.cos(la) * math.sin(lo), z + math.sin(la)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _facilities(rows: Iterable[Any]) -> dict[int, tuple[str, str, float, float]]:
    """fac id -> (city, country, lat, lon) for active facilities with a plausible position."""
    result: dict[int, tuple[str, str, float, float]] = {}
    seen: set[int] = set()
    dropped: set[int] = set()
    for row in rows:
        fac_id = _int_id(row.get("id")) if isinstance(row, dict) else None
        if fac_id is None or not _active(row):
            continue
        lat, lon = row.get("latitude"), row.get("longitude")
        city, country = _text(row.get("city"), MAX_CITY_LENGTH), _country(row.get("country"))
        coords_ok = all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
        if fac_id in seen:
            dropped.add(fac_id)  # two active records with one id contradict each other
            continue
        seen.add(fac_id)
        # (0, 0) is the usual placeholder of a missing position, not a facility.
        if not (city and country and coords_ok) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            continue
        result[fac_id] = (city, country, float(lat), float(lon))
    for fac_id in dropped:
        result.pop(fac_id, None)
    return result


def classify_exchange(
    ix_id: int, name: str, city: str | None, country: str | None, facilities: list[tuple[str, str, float, float]]
) -> IxpExchange:
    """Decide what the facility evidence proves about the exchange region (see module docstring)."""

    def exchange(location_class: str, **kwargs: Any) -> IxpExchange:
        return IxpExchange(ix_id, name, city, country, location_class, facility_count=len(facilities), **kwargs)

    if not facilities:
        return exchange(LOC_UNKNOWN)
    countries = {f[1] for f in facilities}
    if len(countries) > 1:
        return exchange(LOC_DISTRIBUTED)
    fac_country = next(iter(countries))
    if country and country != fac_country:
        return exchange(LOC_CONFLICTING)
    points = [(f[2], f[3]) for f in facilities]
    center = _centroid(points)
    spread = max(_distance_km(p, center) for p in points)
    if spread > METRO_MAX_RADIUS_KM:
        return exchange(LOC_DISTRIBUTED, ref_country=fac_country)
    if city and not any(_names_agree(city, f[0]) for f in facilities):
        return exchange(LOC_CONFLICTING, ref_country=fac_country)
    ref_city = city or Counter(f[0] for f in facilities).most_common(1)[0][0]
    return exchange(
        LOC_METRO,
        ref_city=ref_city,
        ref_country=fac_country,
        ref_lat=round(center[0], 4),
        ref_lon=round(center[1], 4),
        ref_radius_km=round(max(METRO_MIN_RADIUS_KM, math.ceil(spread)), 1),
    )


def _unique_by_id(rows: Iterable[Any], excluded: Counter, key: str) -> dict[int, dict]:
    """Active rows by id; an id used by two active rows is contradictory and dropped entirely."""
    found: dict[int, dict] = {}
    bad: set[int] = set()
    for row in rows:
        if not _active(row):
            excluded["inactive_or_malformed"] += 1
            continue
        row_id = _int_id(row.get("id"))
        if row_id is None:
            excluded[f"{key}_invalid_id"] += 1
        elif row_id in found:
            bad.add(row_id)
        else:
            found[row_id] = row
    for row_id in bad:
        found.pop(row_id, None)
        excluded[f"{key}_duplicate_id"] += 1
    return found


def build_snapshot(
    ix_rows: Iterable[Any],
    ixlan_rows: Iterable[Any],
    ixpfx_rows: Iterable[Any],
    ixfac_rows: Iterable[Any],
    fac_rows: Iterable[Any],
) -> IxpBuildResult:
    """Validate the PeeringDB tables and derive exchanges, prefixes and exchange regions."""
    excluded: Counter = Counter()
    exchanges = {}
    for ix_id, row in _unique_by_id(ix_rows, excluded, "ix").items():
        ix_name = _text(row.get("name"), MAX_NAME_LENGTH)
        if ix_name is None:
            excluded["ix_invalid_name"] += 1
            continue
        exchanges[ix_id] = (ix_name, _text(row.get("city"), MAX_CITY_LENGTH), _country(row.get("country")))

    ixlan_to_ix: dict[int, int] = {}
    for lan_id, row in _unique_by_id(ixlan_rows, excluded, "ixlan").items():
        ix_id = _int_id(row.get("ix_id"))
        if ix_id in exchanges:
            ixlan_to_ix[lan_id] = ix_id
        else:
            excluded["ixlan_unknown_ix"] += 1

    # Prefix candidates keyed by canonical string; a prefix claimed by two exchanges is contradictory.
    claims: dict[str, list[tuple[int, int, int, int]]] = {}  # prefix -> [(ix_id, ixlan_id, version, pfx_id)]
    for row in ixpfx_rows:
        if not _active(row):
            excluded["pfx_inactive_or_malformed"] += 1
            continue
        pfx_id, lan_id = _int_id(row.get("id")), _int_id(row.get("ixlan_id"))
        version = _PROTOCOL_VERSION.get(row.get("protocol"))
        text = row.get("prefix")
        if pfx_id is None or lan_id not in ixlan_to_ix or version is None or not isinstance(text, str):
            excluded["pfx_inconsistent"] += 1
            continue
        try:
            network = ipaddress.ip_network(text.strip(), strict=True)
        except ValueError:
            excluded["pfx_invalid_network"] += 1
            continue
        if network.version != version or network.prefixlen < MIN_PREFIX_LENGTH[version] or not network.is_global:
            excluded["pfx_rejected_network"] += 1
            continue
        claims.setdefault(str(network), []).append((ixlan_to_ix[lan_id], lan_id, version, pfx_id))

    prefixes: list[IxpPrefix] = []
    for text, items in sorted(claims.items()):
        if len({item[0] for item in items}) > 1:
            excluded["pfx_conflicting_exchanges"] += 1
            continue
        ix_id, lan_id, version, _pfx = min(items, key=lambda item: (item[3], item[1]))  # deterministic
        prefixes.append(IxpPrefix(text, version, ipaddress.ip_network(text).prefixlen, ix_id, lan_id))

    used = {p.ix_id for p in prefixes}
    facilities = _facilities(fac_rows)
    per_ix: dict[int, set[int]] = {}
    for row in ixfac_rows:
        ix_id, fac_id = (_int_id(row.get("ix_id")), _int_id(row.get("fac_id"))) if _active(row) else (None, None)
        if ix_id in used and fac_id in facilities:
            per_ix.setdefault(ix_id, set()).add(fac_id)

    result_exchanges = tuple(
        classify_exchange(ix_id, *exchanges[ix_id], [facilities[f] for f in sorted(per_ix.get(ix_id, ()))])
        for ix_id in sorted(used)
    )
    return IxpBuildResult(IxpSnapshot(result_exchanges, tuple(prefixes)), dict(excluded))
