#!/usr/bin/env python3
#
# app/services/geo_resolver.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure, deterministic selection of the best-evidenced location for one IP.

No I/O here: callers collect candidates (GeoLite2, POP/hostname, ip-api Pro, ipwho.is),
this module validates, ranks and explains. Best available evidence wins; GeoLite2 wins
ties; contradictory precise claims are downgraded to approximate instead of guessed.

Online sources have no evidenced accuracy, so they never replace a local (GeoLite2 or POP)
position. They are only queried when the local result is not good enough
(``needs_online_fallback``) and may then improve the place NAME of a coarse local result.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

SRC_GEOLITE2 = "geolite2"
SRC_POP = "pop"
SRC_IP_API_PRO = "ip-api-pro"
SRC_IPWHO = "ipwho.is"
SRC_IXP = "ixp"  # PeeringDB IXP prefix membership; its region is an estimate, never a router position

# Tie-breaker only (lower index wins); the quality key decides first.
SOURCE_PRIORITY = (SRC_GEOLITE2, SRC_POP, SRC_IP_API_PRO, SRC_IPWHO, SRC_IXP)

IXP_LOCATION_METRO = "metro"  # structured sources agree on one metro region

# PRODUCT DECISION, not an official MaxMind limit: MaxMind's accuracy_radius only states that the
# true location lies inside the circle with roughly 67 % confidence. A GeoLite2 city result is
# "high quality" (mappable, no online lookup) up to and including this radius. Revisit with real
# traceroute data. Single source of truth for resolver, route model and docs.
MAX_CITY_RADIUS_KM = 25
POP_VERIFIED_CONFIDENCE = 85  # POP/CLLI mappings at or above this value count as verified
CONTRADICTION_KM = 250  # precise claims further apart than this cannot both be trusted

ACCURACY_PRECISE = "precise"
ACCURACY_APPROXIMATE = "approximate"
ACCURACY_UNKNOWN = "unknown"

# Local source sets: only these can provide a position without online help.
LOCAL_SOURCES = (SRC_GEOLITE2, SRC_POP)

# GeoLite2 quality classes (see geolite2_quality); only QUALITY_HIGH avoids the online fallback.
QUALITY_HIGH = "high"  # city + country + radius <= MAX_CITY_RADIUS_KM
QUALITY_LIMITED = "limited"  # city + country, radius above the limit
QUALITY_UNEVIDENCED = "unevidenced"  # city + country, no accuracy radius
QUALITY_COARSE = "coarse"  # region/state only (or a city without country)
QUALITY_VERY_COARSE = "very_coarse"  # country only / centroid
QUALITY_UNUSABLE = "unusable"  # no valid coordinates

# Data level: complete city > region only > country only > nothing.
LEVEL_NONE, LEVEL_COUNTRY, LEVEL_REGION, LEVEL_CITY = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class GeoCandidate:
    """One self-consistent location claim; coordinates, city and country share an origin."""

    source: str
    latitude: float
    longitude: float
    city: str | None = None
    country_code: str | None = None
    accuracy_radius_km: float | None = None
    city_is_region: bool = False
    confidence: float | None = None  # POP mapping confidence (0-100); None for other sources
    evidence: str | None = None  # POP match origin, e.g. "provider_clli"


@dataclass(frozen=True, slots=True)
class IxpEvidence:
    """Verified membership of an address in an IXP peering LAN plus what is evidenced about the region.

    Membership is certain; the region is the exchange's, not the answering router's. Without
    ``location_class == "metro"`` there is no position (``country`` may still be evidenced).
    """

    ix_id: int
    name: str
    prefix: str
    location_class: str
    city: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None

    def position(self) -> GeoCandidate | None:
        """The exchange region as an approximate candidate; None unless the region is evidenced."""
        if self.location_class != IXP_LOCATION_METRO or not self.city or not self.country:
            return None
        return build_candidate(
            SRC_IXP,
            self.latitude,
            self.longitude,
            city=self.city,
            country_code=self.country,
            accuracy_radius_km=self.radius_km,
            evidence="peeringdb_ixp_prefix",
        )

    def as_dict(self) -> dict:
        return {
            "id": self.ix_id,
            "name": self.name,
            "prefix": self.prefix,
            "location_class": self.location_class,
            "city": self.city,
            "country": self.country,
        }


@dataclass(frozen=True, slots=True)
class GeoDecision:
    """The selected candidate plus the traceable reasoning behind it."""

    selected: GeoCandidate | None
    accuracy_class: str
    selection_reason: str
    is_fallback: bool
    sources_considered: tuple[str, ...]
    sources_succeeded: tuple[str, ...]
    contradictions: tuple[str, ...] = ()
    place: GeoCandidate | None = None  # text-only improvement of a coarse position (never moves it)
    ixp: IxpEvidence | None = None  # verified IXP peering-LAN membership of the address, if any

    @property
    def place_name(self) -> str | None:
        """City/place text to display: the text-only candidate's name, else the position's own."""
        if self.place is not None:
            return self.place.city
        return self.selected.city if self.selected else None

    def as_dict(self) -> dict:
        return {
            "selected_source": self.selected.source if self.selected else None,
            "place_source": self.place.source if self.place else None,
            "accuracy_class": self.accuracy_class,
            "selection_reason": self.selection_reason,
            "is_fallback": self.is_fallback,
            "sources_considered": list(self.sources_considered),
            "sources_succeeded": list(self.sources_succeeded),
            "contradictions": list(self.contradictions),
            "ixp": self.ixp.as_dict() if self.ixp else None,
        }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def build_candidate(
    source: str,
    lat: object,
    lon: object,
    *,
    city: str | None = None,
    country_code: str | None = None,
    accuracy_radius_km: object = None,
    city_is_region: bool = False,
    confidence: object = None,
    evidence: str | None = None,
) -> GeoCandidate | None:
    """Validate raw values; return None (candidate discarded) on unusable coordinates.

    0.0 is a valid coordinate. A negative or non-finite radius is dropped, not repaired.
    """
    la, lo = _number(lat), _number(lon)
    if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    radius = _number(accuracy_radius_km)
    return GeoCandidate(
        source=source,
        latitude=la,
        longitude=lo,
        city=(city or "").strip() or None,
        country_code=(country_code or "").strip().upper() or None,
        accuracy_radius_km=radius if radius is not None and radius >= 0 else None,
        city_is_region=bool(city_is_region),
        confidence=_number(confidence),
        evidence=evidence,
    )


_POP_LABELS = {"high": 90.0, "medium": 70.0, "low": 40.0}


def normalize_pop_confidence(value: object) -> float | None:
    """Map the mixed POP confidence encodings (labels, 0-1 floats, 0-100 numbers) to 0-100."""
    if isinstance(value, str):
        return _POP_LABELS.get(value.strip().lower())
    number = _number(value)
    if number is None or number < 0:
        return None
    return number * 100 if number <= 1 else number


def data_level(candidate: GeoCandidate) -> int:
    if candidate.city and not candidate.city_is_region and candidate.country_code:
        return LEVEL_CITY
    if candidate.city:
        return LEVEL_REGION
    return LEVEL_COUNTRY if candidate.country_code else LEVEL_NONE


def is_verified_pop(candidate: GeoCandidate) -> bool:
    return (
        candidate.source == SRC_POP
        and candidate.confidence is not None
        and candidate.confidence >= POP_VERIFIED_CONFIDENCE
        and data_level(candidate) == LEVEL_CITY
    )


def geolite2_quality(candidate: GeoCandidate | None) -> str:
    """Classify a GeoLite2 candidate for the online-fallback decision (None = no valid coordinates)."""
    if candidate is None:
        return QUALITY_UNUSABLE
    level = data_level(candidate)
    if level == LEVEL_CITY:
        radius = candidate.accuracy_radius_km
        if radius is None:
            return QUALITY_UNEVIDENCED
        return QUALITY_HIGH if radius <= MAX_CITY_RADIUS_KM else QUALITY_LIMITED
    return QUALITY_COARSE if level == LEVEL_REGION else QUALITY_VERY_COARSE


def needs_online_fallback(candidates: Iterable[GeoCandidate], ixp: IxpEvidence | None = None) -> bool:
    """True unless a high-quality GeoLite2 result or a verified POP/CLLI location is available.

    A city merely guessed from a hostname (unverified POP) is not equivalent evidence. An address of a
    recognized IXP peering LAN never asks online sources: they would only describe the registrant.
    """
    if ixp is not None:
        return False
    return not any(
        geolite2_quality(c) == QUALITY_HIGH if c.source == SRC_GEOLITE2 else is_verified_pop(c) for c in candidates
    )


def evidence_rank(candidate: GeoCandidate) -> int:
    """4 = evidenced precise, 3 = evidenced radius, 2 = unverified POP, 1 = no accuracy proof."""
    if candidate.source == SRC_POP:
        return 4 if is_verified_pop(candidate) else 2
    if candidate.source == SRC_GEOLITE2 and candidate.accuracy_radius_km is not None:
        return 4 if geolite2_quality(candidate) == QUALITY_HIGH else 3
    return 1  # online coordinates and GeoLite2 without a radius: accuracy unproven


def quality_key(candidate: GeoCandidate) -> tuple[int, int, int, int]:
    """Higher is better. Ordered: evidence, data level, source quality, then tie-breaker."""
    evidenced_geolite = candidate.source == SRC_GEOLITE2 and candidate.accuracy_radius_km is not None
    source_quality = 1 if evidenced_geolite or is_verified_pop(candidate) else 0
    return (
        evidence_rank(candidate),
        data_level(candidate),
        source_quality,
        -SOURCE_PRIORITY.index(candidate.source),
    )


def format_place(place: str | None, country_code: str | None) -> str:
    """Join place name and country code; a code the name already ends with is not repeated."""
    place, code = (place or "").strip(), (country_code or "").strip().upper()
    if not code or place == code or place.endswith(f", {code}"):
        return place or code
    return f"{place}, {code}" if place else code


def distance_km(a: GeoCandidate, b: GeoCandidate) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat, dlon = p2 - p1, math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _text_upgrade(winner: GeoCandidate, ranked: Sequence[GeoCandidate]) -> GeoCandidate | None:
    """Return a complete city candidate whose NAME may describe a coarse winner.

    It must agree on the country and not contradict the winner's position. Only its place name
    is used (``GeoDecision.place``); the winner's coordinates, source and accuracy stay.
    """
    if data_level(winner) >= LEVEL_CITY:
        return None
    for other in ranked:
        if other is winner or data_level(other) < LEVEL_CITY:
            continue
        if winner.country_code and other.country_code != winner.country_code:
            continue
        if distance_km(winner, other) > max(CONTRADICTION_KM, winner.accuracy_radius_km or 0):
            continue
        return other
    return None


def _select_for_ixp(
    candidates: list[GeoCandidate], considered: Sequence[str], ixp: IxpEvidence
) -> GeoDecision:
    """Decision for an address inside an IXP peering LAN.

    Prefix membership is evidence of its own. An evidenced exchange region beats ordinary GeoIP, which
    only describes the registrant, but stays APPROXIMATE: it is not the router's position. A verified
    POP/CLLI claim is independent router evidence and may still win. Without an evidenced region no
    GeoIP or online position is used at all, so a wrong country cannot pose as the exchange location.
    """
    considered_t = tuple(s for s in SOURCE_PRIORITY if s in considered or s == SRC_IXP)
    region = ixp.position()
    verified = [c for c in candidates if is_verified_pop(c)]
    ignored = [c for c in candidates if c not in verified]
    if verified:
        base = select(verified, [s for s in considered if s != SRC_IXP])
        reason = f"{base.selection_reason}; IXP membership {ixp.name} noted, router position from verified POP"
        contradictions = list(base.contradictions)
        accuracy = base.accuracy_class
        if region is not None and base.selected is not None:
            gap = distance_km(base.selected, region)
            if gap > CONTRADICTION_KM + (region.accuracy_radius_km or 0):
                contradictions.append(f"{base.selected.source} vs ixp: {gap:.0f} km apart")
                accuracy = ACCURACY_APPROXIMATE
                reason += "; POP and IXP region disagree, position downgraded to approximate"
        return replace(
            base,
            accuracy_class=accuracy,
            selection_reason=reason,
            contradictions=tuple(contradictions),
            sources_considered=considered_t,
            ixp=ixp,
        )
    succeeded = tuple(s for s in SOURCE_PRIORITY if s == SRC_IXP and region is not None)
    noted = tuple(
        f"{c.source} location {format_place(c.city, c.country_code) or 'unknown'} not used: "
        f"address belongs to IXP {ixp.name}"
        + (f" ({distance_km(c, region):.0f} km from its region)" if region is not None and c.latitude is not None else "")
        for c in ignored
    )
    if region is None:
        return GeoDecision(
            None,
            ACCURACY_UNKNOWN,
            f"IXP {ixp.name} ({ixp.location_class} location): no evidenced router or region position; "
            "GeoIP and online positions are not used",
            False,
            considered_t,
            succeeded,
            noted,
            ixp=ixp,
        )
    flagged = tuple(
        text
        for c, text in zip(ignored, noted, strict=True)
        if c.country_code != region.country_code or distance_km(c, region) > CONTRADICTION_KM
    )
    return GeoDecision(
        region,
        ACCURACY_APPROXIMATE,
        f"IXP prefix {ixp.prefix} of {ixp.name}: evidenced exchange region, not the router position",
        False,
        considered_t,
        succeeded,
        flagged,
        ixp=ixp,
    )


def select(
    candidates: Iterable[GeoCandidate], considered: Sequence[str], ixp: IxpEvidence | None = None
) -> GeoDecision:
    """Pick the best-evidenced candidate; the result is independent of input order."""
    if ixp is not None:
        local = sorted((c for c in candidates if c.source != SRC_IXP), key=quality_key, reverse=True)
        return _select_for_ixp(local, considered, ixp)
    ranked = sorted(candidates, key=quality_key, reverse=True)
    considered_t = tuple(s for s in SOURCE_PRIORITY if s in considered)
    succeeded = tuple(s for s in SOURCE_PRIORITY if any(c.source == s for c in ranked))
    if not ranked:
        return GeoDecision(None, ACCURACY_UNKNOWN, "no usable location from any source", False, considered_t, succeeded)

    # Online coordinates carry no evidenced accuracy: they never replace a local position.
    local = [c for c in ranked if c.source in LOCAL_SOURCES]
    pool = local or ranked
    winner = pool[0]
    reason = f"{winner.source} has the best-evidenced candidate"
    if len(pool) > 1 and quality_key(pool[1])[:3] == quality_key(winner)[:3]:
        reason = f"{winner.source} wins the tie on equal quality by source priority"
    elif local and len(local) < len(ranked):
        reason = f"{winner.source} kept: online sources have no evidenced accuracy"
    place = _text_upgrade(winner, ranked)
    if place is not None:
        reason += f"; place name from {place.source} (text only, position unchanged)"
    selected = winner

    contradictions = tuple(
        f"{selected.source} vs {other.source}: {distance_km(selected, other):.0f} km apart"
        for other in ranked
        if other is not selected and distance_km(selected, other) > CONTRADICTION_KM
    )
    # Only evidenced claims can contradict a precise position; unproven online data cannot.
    precise_rivals = [
        other
        for other in ranked
        if other is not selected and evidence_rank(other) >= 4 and distance_km(selected, other) > CONTRADICTION_KM
    ]
    if evidence_rank(selected) >= 4 and not precise_rivals:
        accuracy = ACCURACY_PRECISE
    else:
        accuracy = ACCURACY_APPROXIMATE
        if precise_rivals:
            reason += "; contradictory precise claims, position downgraded to approximate"
    missing_external = any(s != SRC_GEOLITE2 and s not in succeeded for s in considered_t)
    is_fallback = selected.source == SRC_GEOLITE2 and missing_external
    return GeoDecision(selected, accuracy, reason, is_fallback, considered_t, succeeded, contradictions, place)
