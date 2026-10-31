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

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import traceroute_geo
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


@pytest.mark.parametrize("online", [None, {"city": "Wuppertal", "country": "DE", "source": "ipwho.is"}])
def test_coarse_geoip_consults_online_but_keeps_local_location(monkeypatch: pytest.MonkeyPatch, online) -> None:
    local = {"lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE", "accuracy_radius_km": 100}
    calls: list[str] = []
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda _ip: local)

    def online_lookup(ip: str):
        calls.append(ip)
        return online

    monkeypatch.setattr(traceroute_geo, "_geolocate_online_fallback", online_lookup)
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


def test_online_geo_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(traceroute_geo, "get_runtime_bool", lambda *_args: False)
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda _ip: None)
    monkeypatch.setattr(traceroute_geo, "lookup_public_ip", lambda *_args, **_kwargs: pytest.fail("unexpected request"))
    assert traceroute_geo.geolocate_with_hostname_hint("8.8.8.8") is None


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
