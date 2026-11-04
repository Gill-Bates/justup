#!/usr/bin/env python3
#
# tests/test_traceroute_route_model.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Contracts for the shared traceroute route model and the two geo-correctness fixes.

Covers the mandatory review test matrix at the level these pure functions reach:
unknown intermediate hops are not placed on the map and not given a fabricated
precise link; a destination without real coordinates is never synthesized onto
the last known position; the four geo states (precise / hostname / coarse /
missing) stay distinguishable; hop order and original hop numbers are preserved.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import traceroute_geo
from app.services.route_timeline import mapped_hop_numbers
from app.services.traceroute_geo import (
    RM_ACCURACY,
    RM_DESTINATION,
    RM_GEO_SOURCE,
    RM_HOP,
    RM_LOCATION,
    RM_MAPPABLE,
    RM_SYNTHETIC,
    build_route_model,
    mtr_hops_to_geo_points,
)
from app.utils.constants import GeoAccuracy, GeoSource
from tests import _support  # noqa: F401 - imported for its sys.path side effect

CONF = traceroute_geo.META_CONFIDENCE


def _geo(lat: float | None, lon: float | None, city: str | None, country: str, confidence: str) -> dict[str, Any]:
    return {"lat": lat, "lon": lon, "city": city, "country": country, CONF: confidence, "accuracy_radius_km": 10}


def _patch_geo(monkeypatch: pytest.MonkeyPatch, table: dict[str, dict[str, Any] | None]) -> None:
    """Resolve each IP to a fixed location (or None), bypassing GeoIP/network."""
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_with_hostname_hint",
        lambda ip, _hostname=None: table.get(ip),
    )


def _only_node(monkeypatch: pytest.MonkeyPatch, ip: str, loc: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve one hop to ``loc`` and return its route-model node."""
    _patch_geo(monkeypatch, {ip: loc})
    return build_route_model([{"hop": 1, "ip": ip, "status": "ok"}])[0]


@pytest.mark.parametrize("status", [None, "", "bogus"])
def test_missing_or_unknown_hop_status_is_inferred_from_the_ip_in_both_views(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    hops = [{"hop": 1, "ip": "10.0.0.1", "status": status}, {"hop": 2, "ip": "*", "status": status}]
    nodes = build_route_model(hops)
    assert [n["status"] for n in nodes] == [traceroute_geo.STATUS_OK, traceroute_geo.STATUS_TIMEOUT]
    assert [p["hop"] for p in mtr_hops_to_geo_points(hops)] == [1]


# --- The four geolocation states, verified through the public route model -----


def test_precise_from_geoip_with_city(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _only_node(monkeypatch, "10.0.0.1", _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP))
    assert node[RM_GEO_SOURCE] == GeoSource.GEOIP
    assert node[RM_ACCURACY] == GeoAccuracy.PRECISE
    assert node[RM_MAPPABLE] is True


def test_large_geoip_radius_is_not_mappable_even_with_city(monkeypatch: pytest.MonkeyPatch) -> None:
    coarse = _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)
    coarse["accuracy_radius_km"] = 100
    node = _only_node(monkeypatch, "10.0.0.1", coarse)
    assert node[RM_GEO_SOURCE] == GeoSource.GEOIP_COARSE
    assert node[RM_ACCURACY] == GeoAccuracy.APPROXIMATE
    assert node[RM_MAPPABLE] is False
    assert mtr_hops_to_geo_points([{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]) == []


def test_online_city_without_accuracy_radius_is_not_mappable(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_ONLINE)
    online["accuracy_radius_km"] = None
    node = _only_node(monkeypatch, "10.0.0.1", online)
    assert node[RM_GEO_SOURCE] == GeoSource.ONLINE
    assert node[RM_ACCURACY] == GeoAccuracy.APPROXIMATE
    assert node[RM_MAPPABLE] is False
    assert mtr_hops_to_geo_points([{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]) == []


def test_coarse_geoip_still_queries_online_but_keeps_evidenced_local_location(monkeypatch: pytest.MonkeyPatch) -> None:
    local = {"lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE", "accuracy_radius_km": 100}
    calls: list[str] = []
    online = {"lat": 51.2, "lon": 7.2, "city": "Wuppertal", "country_code": "DE", "geo_source": "ipwho.is"}
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda _ip: local)
    monkeypatch.setattr(traceroute_geo, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(traceroute_geo, "get_runtime_bool_fail_closed", lambda *_args: True)
    monkeypatch.setattr(traceroute_geo, "_online_ttl_seconds", lambda: 600)
    monkeypatch.setattr(
        traceroute_geo,
        "lookup_online_sources",
        lambda ip, **_kw: (calls.append(ip), (("ipwho.is",), {"ipwho.is": online}))[1],
    )
    result = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert calls == ["8.8.8.8"]
    assert result["city"] == "Dortmund"
    assert result["lat"] == 51.5
    assert result[traceroute_geo.META_CONFIDENCE] == traceroute_geo.CONFIDENCE_GEOIP


def test_geolite_zero_coordinates_are_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    record = SimpleNamespace(
        location=SimpleNamespace(latitude=0.0, longitude=0.0, accuracy_radius=10),
        city=SimpleNamespace(name="Null Island"),
        country=SimpleNamespace(iso_code="GH"),
        subdivisions=None,
    )
    reader = SimpleNamespace(city=lambda _ip: record)
    monkeypatch.setattr(traceroute_geo, "_get_reader", lambda: reader)
    monkeypatch.setattr(traceroute_geo, "_geo_cache", {})
    location = traceroute_geo.geolocate_ip("8.8.8.8")
    assert location["lat"] == location["lon"] == 0.0
    assert location["accuracy_radius_km"] == 10


def test_hostname_inference_is_precise_but_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _only_node(monkeypatch, "10.0.0.1", _geo(50.1, 8.6, "Frankfurt", "DE", traceroute_geo.CONFIDENCE_HOSTNAME))
    assert node[RM_GEO_SOURCE] == GeoSource.HOSTNAME
    assert node[RM_ACCURACY] == GeoAccuracy.PRECISE


def test_coords_without_city_is_coarse_approximate(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _only_node(monkeypatch, "10.0.0.1", _geo(52.0, 9.0, None, "DE", traceroute_geo.CONFIDENCE_GEOIP))
    assert node[RM_GEO_SOURCE] == GeoSource.GEOIP_COARSE
    assert node[RM_ACCURACY] == GeoAccuracy.APPROXIMATE
    assert node[RM_MAPPABLE] is False


def test_no_coords_is_coarse_approximate(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _only_node(monkeypatch, "10.0.0.1", _geo(None, None, None, "DE", traceroute_geo.CONFIDENCE_GEOIP))
    assert node[RM_GEO_SOURCE] == GeoSource.GEOIP_COARSE
    assert node[RM_ACCURACY] == GeoAccuracy.APPROXIMATE


def test_missing_location_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _only_node(monkeypatch, "10.0.0.1", None)
    assert node[RM_GEO_SOURCE] == GeoSource.NONE
    assert node[RM_ACCURACY] == GeoAccuracy.UNKNOWN
    assert node[RM_LOCATION] is None


# --- build_route_model: order, numbers, and the four states -------------------


def test_route_model_preserves_hop_order_and_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(
        monkeypatch,
        {
            "10.0.0.5": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP),
            "10.0.0.6": _geo(50.1, 8.6, "Frankfurt", "DE", traceroute_geo.CONFIDENCE_HOSTNAME),
        },
    )
    hops = [
        {"hop": 1, "ip": "192.168.0.1", "status": "ok"},  # private: unknown
        {"hop": 2, "ip": "*", "status": "timeout"},
        {"hop": 5, "ip": "10.0.0.5", "status": "ok"},
        {"hop": 6, "ip": "10.0.0.6", "status": "ok"},
    ]
    nodes = build_route_model(hops)
    assert [n[RM_HOP] for n in nodes] == [1, 2, 5, 6]
    # Private/timeout hops present but not mappable.
    assert nodes[0][RM_MAPPABLE] is False and nodes[0][RM_LOCATION] is None
    assert nodes[1][RM_MAPPABLE] is False
    # Real geolocations are mappable and keep their source.
    assert nodes[2][RM_MAPPABLE] is True and nodes[2][RM_GEO_SOURCE] == GeoSource.GEOIP
    assert nodes[3][RM_GEO_SOURCE] == GeoSource.HOSTNAME


def test_route_model_coarse_geoip_not_mappable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(monkeypatch, {"10.0.0.9": _geo(52.0, 9.0, None, "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    nodes = build_route_model([{"hop": 3, "ip": "10.0.0.9", "status": "ok"}])
    node = nodes[0]
    assert node[RM_ACCURACY] == GeoAccuracy.APPROXIMATE
    assert node[RM_MAPPABLE] is False
    assert node[RM_LOCATION] == {"lat": None, "lon": None, "city": None, "country": "DE"}


def test_route_model_destination_reached_without_location(monkeypatch: pytest.MonkeyPatch) -> None:
    # Destination answered MTR but has no resolvable location.
    _patch_geo(monkeypatch, {"203.0.113.9": None})
    hops = [{"hop": 1, "ip": "203.0.113.9", "status": "ok"}]
    nodes = build_route_model(hops, destination_ip="203.0.113.9")
    dest = next(n for n in nodes if n[RM_DESTINATION])
    assert dest[RM_MAPPABLE] is False
    assert dest[RM_LOCATION] is None
    assert dest[RM_SYNTHETIC] is False


def test_route_model_synthetic_destination_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Destination never replied: a synthetic terminal node is appended, unlocated.
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    hops = [{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]
    nodes = build_route_model(hops, destination_ip="203.0.113.9")
    dest = next(n for n in nodes if n[RM_DESTINATION])
    assert dest[RM_SYNTHETIC] is True
    assert dest[RM_MAPPABLE] is False
    assert dest[RM_HOP] == 2  # last_hop + 1, number preserved/extended


# --- mtr_hops_to_geo_points: the two correctness fixes ------------------------


def test_unknown_hop_not_placed_at_previous_coordinate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hop 2 is private/unknown between two located hops; it must not get a marker
    # at hop 1's coordinate, and the gap must render as a dashed segment.
    _patch_geo(
        monkeypatch,
        {
            "10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP),
            "192.168.0.1": None,
            "10.0.0.3": _geo(50.1, 8.6, "Frankfurt", "DE", traceroute_geo.CONFIDENCE_GEOIP),
        },
    )
    hops = [
        {"hop": 1, "ip": "10.0.0.1", "status": "ok"},
        {"hop": 2, "ip": "192.168.0.1", "status": "ok"},
        {"hop": 3, "ip": "10.0.0.3", "status": "ok"},
    ]
    points = mtr_hops_to_geo_points(hops)
    hop_nums = [p["hop"] for p in points]
    assert 2 not in hop_nums  # unknown hop produces no map point
    # No two points share hop 1's coordinate (no forward-fill duplicate).
    dortmund = [p for p in points if (round(p["lat"], 4), round(p["lon"], 4)) == (51.5, 7.4)]
    assert len(dortmund) == 1
    # The segment leaving the last point before the gap is dashed, not solid.
    first = next(p for p in points if p["hop"] == 1)
    assert first.get("segment_to_next") == traceroute_geo.SEGMENT_DASHED


def test_destination_without_geo_is_not_synthesized_on_map(monkeypatch: pytest.MonkeyPatch) -> None:
    # Destination answered but is unlocatable: no marker at the last known coord.
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    hops = [
        {"hop": 1, "ip": "10.0.0.1", "status": "ok"},
        {"hop": 2, "ip": "203.0.113.9", "status": "ok"},  # dest, no geo
    ]
    points = mtr_hops_to_geo_points(hops, destination_ip="203.0.113.9", destination_geo=None)
    # Only the one located hop is on the map; nothing fabricated at its coordinate.
    coords = [(round(p["lat"], 4), round(p["lon"], 4)) for p in points]
    assert coords == [(51.5, 7.4)]
    assert not any(p.get("ip") == "203.0.113.9" for p in points)


def test_destination_with_real_geo_still_placed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    dest_geo = {"lat": 48.1, "lon": 11.6, "city": "Munich", "country": "DE", "accuracy_radius_km": 10}
    hops = [{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]
    points = mtr_hops_to_geo_points(hops, destination_ip="203.0.113.9", destination_geo=dest_geo)
    dest = next(p for p in points if p.get("ip") == "203.0.113.9")
    assert (dest["lat"], dest["lon"]) == (48.1, 11.6)
    assert dest.get(traceroute_geo.META_DESTINATION) is True


def test_approximate_city_destination_is_plotted_with_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    dest_geo = {
        "lat": 48.1,
        "lon": 11.6,
        "city": "Munich",
        "position_city": "Munich",
        "country": "DE",
        "accuracy_radius_km": 60,
        "accuracy_class": "approximate",
    }
    hops = [{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]

    points = mtr_hops_to_geo_points(hops, destination_ip="203.0.113.9", destination_geo=dest_geo)

    dest = next(p for p in points if p.get("ip") == "203.0.113.9")
    assert (dest["city"], dest["loc_precision"], dest["_approximate"]) == ("Munich", "approximate", True)
    assert dest["accuracy_radius_km"] == 60
    assert points[0]["segment_to_next"] == traceroute_geo.SEGMENT_DASHED


def test_deduplicated_map_point_retains_approximate_accuracy() -> None:
    points = [
        {"hop": 1, "lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE"},
        {
            "hop": 2,
            "lat": 51.5,
            "lon": 7.4,
            "city": "Dortmund",
            "country": "DE",
            "_approximate": True,
            "accuracy_class": "approximate",
            "accuracy_radius_km": 60,
        },
        {"hop": 3, "lat": 50.1, "lon": 8.6, "city": "Frankfurt", "country": "DE"},
    ]

    merged = traceroute_geo._deduplicate_geo_points(points)  # noqa: SLF001

    assert merged[0]["_approximate"] is True
    assert merged[0]["accuracy_class"] == "approximate"
    assert merged[0]["loc_precision"] == "approximate"
    assert merged[0]["accuracy_radius_km"] == 60


def test_same_country_center_run_preserves_every_hop_and_keeps_destination_node() -> None:
    points = [
        {
            "hop": hop,
            "lat": 51.16,
            "lon": 10.45,
            "city": None,
            "country": "DE",
            "map_role": traceroute_geo.MAP_ROLE_ESTIMATE,
            "_country_center": True,
            "segment_to_next": "dashed",
        }
        for hop in range(1, 5)
    ]
    points[-1]["_destination"] = True

    merged = traceroute_geo._deduplicate_geo_points(points)  # noqa: SLF001

    assert [(point["hop"], point.get("_hop_range")) for point in merged] == [(1, "1-3"), (4, None)]
    assert mapped_hop_numbers(merged) == {1, 2, 3, 4}
    assert merged[-1]["_destination"] is True


def test_country_only_location_has_the_same_estimate_role_in_route_model_and_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_geo(monkeypatch, {"8.8.8.8": _geo(None, None, None, "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    hops = [{"hop": 1, "ip": "8.8.8.8", "status": "ok"}]

    node = build_route_model(hops)[0]
    point = mtr_hops_to_geo_points(hops)[0]

    assert node["map_role"] == point["map_role"] == traceroute_geo.MAP_ROLE_ESTIMATE
    assert point["_country_center"] is True


def test_private_country_only_location_does_not_get_a_synthetic_map_center(monkeypatch: pytest.MonkeyPatch) -> None:
    country_only = _geo(None, None, None, "DE", traceroute_geo.CONFIDENCE_GEOIP)
    _patch_geo(monkeypatch, {"10.0.0.1": country_only})
    hops = [{"hop": 1, "ip": "10.0.0.1", "status": "ok"}]

    node = build_route_model(hops)[0]

    assert node["map_role"] == traceroute_geo.MAP_ROLE_NONE
    assert mtr_hops_to_geo_points(hops) == []


def test_zero_geolocated_hops_yields_no_points(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(monkeypatch, {"192.168.0.1": None, "192.168.0.2": None})
    hops = [
        {"hop": 1, "ip": "192.168.0.1", "status": "ok"},
        {"hop": 2, "ip": "192.168.0.2", "status": "ok"},
    ]
    assert mtr_hops_to_geo_points(hops) == []


def test_model_and_geo_points_agree_on_destination_hop_number(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared _destination_disposition keeps the injected destination hop
    # number identical between the route model and the map points.
    _patch_geo(monkeypatch, {"10.0.0.1": _geo(51.5, 7.4, "Dortmund", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    dest_geo = {"lat": 48.1, "lon": 11.6, "city": "Munich", "country": "DE", "accuracy_radius_km": 10}
    hops = [{"hop": 7, "ip": "10.0.0.1", "status": "ok"}]
    model = build_route_model(hops, destination_ip="203.0.113.9", destination_geo=dest_geo)
    points = mtr_hops_to_geo_points(hops, destination_ip="203.0.113.9", destination_geo=dest_geo)
    model_dest = next(n for n in model if n[RM_DESTINATION])
    point_dest = next(p for p in points if p.get("ip") == "203.0.113.9")
    assert model_dest[RM_HOP] == point_dest["hop"] == 8


def test_model_and_geo_points_agree_destination_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    # When the last responding hop IS the destination, neither path injects a
    # synthetic node and both mark the existing node as the destination.
    _patch_geo(monkeypatch, {"203.0.113.9": _geo(48.1, 11.6, "Munich", "DE", traceroute_geo.CONFIDENCE_GEOIP)})
    hops = [{"hop": 1, "ip": "203.0.113.9", "status": "ok"}]
    model = build_route_model(hops, destination_ip="203.0.113.9")
    points = mtr_hops_to_geo_points(hops, destination_ip="203.0.113.9")
    assert sum(1 for n in model if n[RM_DESTINATION]) == 1
    assert [n[RM_HOP] for n in model if n[RM_DESTINATION]] == [1]
    assert [p["hop"] for p in points if p.get(traceroute_geo.META_DESTINATION)] == [1]


def test_location_view_names_a_city_only_for_the_candidate_that_owns_the_position() -> None:
    view = traceroute_geo.location_view
    precise = {"lat": 50.1, "lon": 8.7, "city": "Frankfurt", "country": "DE", "accuracy_class": "precise"}
    assert view(precise) == {"city": "Frankfurt", "country": "DE", "precision": "precise"}
    # ``city`` is a text-only upgrade from another source: the position's own place is unknown.
    upgraded = {"lat": 50.6, "lon": 9.2, "city": "Kettenhausen", "country": "DE", "accuracy_class": "approximate"}
    assert view({**upgraded, "position_city": None}) == {"city": None, "country": "DE", "precision": "country"}
    assert view({**upgraded, "position_city": "Fulda"})["precision"] == "approximate"
    assert view(None) == {"city": None, "country": None, "precision": "none"}


# --- Map chain: every located hop is a node; estimated ends and unknown stretches are dashed---

FRANKFURT = (50.11, 8.68)
KOTTENHEIM = (50.38, 7.33)
WUPPERTAL = (51.27, 7.20)
RATINGEN = (51.30, 6.85)
AMSTERDAM = (52.37, 4.90)
ROLE = traceroute_geo.META_MAP_ROLE


def _place(coords: tuple[float, float], city: str, accuracy: str, *, radius: float = 10, source: str = "geoip") -> dict:
    return {
        "lat": coords[0],
        "lon": coords[1],
        "city": city,
        "position_city": city,
        "country": "DE",
        "accuracy_radius_km": radius,
        "accuracy_class": accuracy,
        CONF: source,
    }


def _hops(*entries: tuple[int, str | None]) -> list[dict[str, Any]]:
    """(hop, ip) pairs; ``None`` is a silent hop."""
    return [
        {"hop": n, "ip": ip or "*", "hostname": None, "status": "ok" if ip else "timeout", "latency_ms": {"avg": n}}
        for n, ip in entries
    ]


def _segments(points: list[dict]) -> list[tuple[int, int, str]]:
    """The chain as (from hop, to hop, style) read the way the web map and the PDF read it."""
    return [(a["hop"], b["hop"], a["segment_to_next"]) for a, b in itertools.pairwise(points)]


def _owner_route(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict], dict, list[dict]]:
    """Frankfurt, Kottenheim (approx.), silent, Frankfurt, Wuppertal, silent, Ratingen (approx. destination)."""
    ratingen = _place(RATINGEN, "Ratingen", "approximate", radius=60)
    table = {
        "a7": _place(FRANKFURT, "Frankfurt", "precise"),
        "a8": _place(KOTTENHEIM, "Kottenheim", "approximate", radius=100),
        "a10": _place(FRANKFURT, "Frankfurt", "precise"),
        "a11": _place(WUPPERTAL, "Wuppertal", "precise"),
        "a13": ratingen,
    }
    _patch_geo(monkeypatch, table)
    hops = _hops((7, "a7"), (8, "a8"), (9, None), (10, "a10"), (11, "a11"), (12, None), (13, "a13"))
    return hops, ratingen, mtr_hops_to_geo_points(hops, destination_ip="a13", destination_geo=ratingen)


def test_approximate_hop_is_a_chain_node_with_dashed_links(monkeypatch: pytest.MonkeyPatch) -> None:
    _hops_in, _dest, points = _owner_route(monkeypatch)
    assert [(p["hop"], p[ROLE]) for p in points] == [
        (7, "route"),
        (8, "estimate"),  # Kottenheim is a hollow node of the chain
        (10, "route"),
        (11, "route"),
        (13, "route"),
    ]
    # One chain in hop order, nothing left out; the detour via Kottenheim is visible but marked as estimated.
    assert _segments(points) == [(7, 8, "dashed"), (8, 10, "dashed"), (10, 11, "solid"), (11, 13, "dashed")]
    assert points[1]["segment_to_next"] == traceroute_geo.SEGMENT_DASHED
    # Skipped hops get no coordinates; the segment names them for the tooltip.
    assert points[1][traceroute_geo.META_SEGMENT_NOTE] == "Hops 9 not on the map"
    assert points[3][traceroute_geo.META_SEGMENT_NOTE] == "Hops 12 not on the map"
    assert traceroute_geo.META_SEGMENT_NOTE not in points[2]
    note = points[1][traceroute_geo.META_LOC_NOTE]
    assert "GeoIP" in note and "100" in note
    assert points[1][traceroute_geo.META_MAP_RADIUS_KM] == 100.0


def test_silent_hops_invent_no_points_and_the_destination_stays_approximate(monkeypatch: pytest.MonkeyPatch) -> None:
    hops, _dest, points = _owner_route(monkeypatch)
    assert {9, 12}.isdisjoint(p["hop"] for p in points)
    dest = points[-1]
    assert dest[traceroute_geo.META_DESTINATION] is True and dest["_approximate"] is True
    assert dest[ROLE] == "route" and "segment_to_next" not in dest
    assert (dest["lat"], dest["lon"]) == RATINGEN
    # Hop numbers and measured values of the input stay untouched.
    assert [h["hop"] for h in hops] == [7, 8, 9, 10, 11, 12, 13]
    assert [h["latency_ms"]["avg"] for h in hops] == [7, 8, 9, 10, 11, 12, 13]


def test_route_model_keeps_every_hop_in_order_with_the_same_role(monkeypatch: pytest.MonkeyPatch) -> None:
    hops, dest, points = _owner_route(monkeypatch)
    nodes = build_route_model(hops, destination_ip="a13", destination_geo=dest)
    assert [n[RM_HOP] for n in nodes] == [7, 8, 9, 10, 11, 12, 13]
    assert [n["ip"] for n in nodes] == [h["ip"] for h in hops]
    by_hop = {n[RM_HOP]: n for n in nodes}
    assert by_hop[8][RM_ACCURACY] == GeoAccuracy.APPROXIMATE
    assert [by_hop[n][traceroute_geo.RM_MAP_ROLE] for n in (7, 8, 9, 12, 13)] == [
        "route",
        "estimate",
        "none",
        "none",
        "route",
    ]
    assert by_hop[13][RM_DESTINATION] is True
    # Both views derive the role from one rule.
    assert {p["hop"]: p[ROLE] for p in points} == {n: by_hop[n][traceroute_geo.RM_MAP_ROLE] for n in (7, 8, 10, 11, 13)}


def test_a_real_geographic_back_jump_between_reliable_hops_stays_on_the_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(
        monkeypatch,
        {
            "a1": _place(FRANKFURT, "Frankfurt", "precise"),
            "a2": _place(AMSTERDAM, "Amsterdam", "precise"),
            "a3": _place(FRANKFURT, "Frankfurt", "precise"),
            "a4": _place(WUPPERTAL, "Wuppertal", "precise"),
        },
    )
    points = mtr_hops_to_geo_points(_hops((1, "a1"), (2, "a2"), (3, "a3"), (4, "a4")))
    assert [(p["city"], p[ROLE]) for p in points] == [
        ("Frankfurt", "route"),
        ("Amsterdam", "route"),
        ("Frankfurt", "route"),
        ("Wuppertal", "route"),
    ]
    assert _segments(points) == [(1, 2, "solid"), (2, 3, "solid"), (3, 4, "solid")]


def test_silent_hop_between_reliable_hops_makes_the_link_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(
        monkeypatch,
        {
            "a1": _place(FRANKFURT, "Frankfurt", "precise"),
            "a4": _place(WUPPERTAL, "Wuppertal", "precise"),
            "a5": _place(AMSTERDAM, "Amsterdam", "precise"),
        },
    )
    hops = _hops((1, "a1"), (2, None), (3, None), (4, "a4"), (5, "a5"))
    hops[2]["status"] = traceroute_geo.STATUS_INTERPOLATED  # an already interpolated gap behaves the same
    points = mtr_hops_to_geo_points(hops)
    assert _segments(points) == [(1, 4, "dashed"), (4, 5, "solid")]


def test_missing_hop_number_and_unlocated_hop_leave_the_link_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(
        monkeypatch,
        {
            "a1": _place(FRANKFURT, "Frankfurt", "precise"),
            "a2": {**_place(FRANKFURT, "x", "precise"), "lat": None, "lon": None},  # coordinates missing
            "a3": _place(WUPPERTAL, "Wuppertal", "precise"),
            "a5": _place(AMSTERDAM, "Amsterdam", "precise"),
        },
    )
    points = mtr_hops_to_geo_points(_hops((1, "a1"), (2, "a2"), (3, "a3"), (5, "a5")))  # hop 4 absent
    assert [p["hop"] for p in points] == [1, 3, 5]
    assert _segments(points) == [(1, 3, "dashed"), (3, 5, "dashed")]


def test_consecutive_approximate_hops_are_chained_with_dashed_links(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geo(
        monkeypatch,
        {
            "a1": _place(FRANKFURT, "Frankfurt", "precise"),
            "a2": _place(KOTTENHEIM, "Kottenheim", "approximate", radius=100),
            "a3": _place(AMSTERDAM, "Amsterdam", "approximate", radius=80, source="online"),
            "a4": _place(WUPPERTAL, "Wuppertal", "precise"),
        },
    )
    points = mtr_hops_to_geo_points(_hops((1, "a1"), (2, "a2"), (3, "a3"), (4, "a4")))
    assert [(p["hop"], p[ROLE]) for p in points] == [(1, "route"), (2, "estimate"), (3, "estimate"), (4, "route")]
    assert _segments(points) == [(1, 2, "dashed"), (2, 3, "dashed"), (3, 4, "dashed")]
    assert "online lookup" in points[2][traceroute_geo.META_LOC_NOTE]


def test_colocated_consecutive_reliable_hops_merge_but_an_estimate_keeps_its_own_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_geo(
        monkeypatch,
        {
            "a1": _place(FRANKFURT, "Frankfurt", "precise"),
            "a2": _place(FRANKFURT, "Frankfurt", "precise"),
            "a3": _place(FRANKFURT, "Frankfurt", "approximate", radius=90),
            "a4": _place(WUPPERTAL, "Wuppertal", "precise"),
            "a5": _place(AMSTERDAM, "Amsterdam", "precise"),
        },
    )
    points = mtr_hops_to_geo_points(_hops((1, "a1"), (2, "a2"), (3, "a3"), (4, "a4"), (5, "a5")))
    assert [(p["hop"], p.get(traceroute_geo.META_HOP_RANGE), p[ROLE]) for p in points] == [
        (1, "1-2", "route"),
        (3, None, "estimate"),
        (4, None, "route"),
        (5, None, "route"),
    ]
    assert _segments(points) == [(1, 3, "dashed"), (3, 4, "dashed"), (4, 5, "solid")]


def test_hostname_hint_and_geoip_sources_are_named_in_the_estimate_note() -> None:
    note = traceroute_geo.location_note
    assert note({CONF: "hostname", "accuracy_radius_km": None}) == "hostname hint estimate"
    assert note({CONF: "geoip", "accuracy_radius_km": 40}) == "GeoIP estimate, radius about 40 km"
    assert note(None) is None
    assert traceroute_geo.map_role(None) == traceroute_geo.MAP_ROLE_NONE
