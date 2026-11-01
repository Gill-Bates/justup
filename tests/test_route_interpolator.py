#!/usr/bin/env python3
#
# tests/test_route_interpolator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route interpolation: only timeouts between measured hops are interpolated."""

from __future__ import annotations

from app.services.route_interpolator import (
    collapse_interpolated_hops,
    ensure_destination_hop,
    interpolate_route,
)
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _hop(n: int, status: str = "ok", avg: float | None = 10.0) -> dict:
    lat = {"avg": avg} if avg is not None else None
    return {"hop": n, "ip": f"10.0.0.{n}", "status": status, "latency_ms": lat}


def _timeout(n: int) -> dict:
    return _hop(n, "timeout", None)


def test_trailing_timeouts_are_not_interpolated() -> None:
    out = interpolate_route([_hop(1), _hop(2, avg=20.0), _timeout(3), _timeout(4)])
    assert [h["status"] for h in out] == ["ok", "ok", "timeout", "timeout"]
    assert out[2]["latency_ms"] is None
    assert not out[2].get("_interpolated")


def test_timeout_between_responding_hops_is_still_interpolated() -> None:
    hops = [_hop(1), _timeout(2), _hop(3, avg=30.0)]
    out = interpolate_route(hops)
    assert out[1]["status"] == "interpolated"
    assert out[1]["_interpolated"] is True
    assert out[1]["latency_ms"]["avg"] == 20.0
    assert hops[1]["status"] == "timeout"  # input is not mutated


def test_middle_block_interpolated_and_trailing_block_left_alone() -> None:
    out = interpolate_route([_hop(1), _timeout(2), _hop(3), _timeout(4)])
    assert [h["status"] for h in out] == ["ok", "interpolated", "ok", "timeout"]


def test_latency_is_linear_across_a_multi_hop_block() -> None:
    out = interpolate_route([_hop(1, avg=10.0), _timeout(2), _timeout(3), _hop(4, avg=40.0)])
    assert [h["latency_ms"]["avg"] for h in out[1:3]] == [20.0, 30.0]


def test_leading_block_starts_from_zero_latency() -> None:
    out = interpolate_route([_timeout(1), _hop(2, avg=20.0)])
    assert out[0]["status"] == "interpolated"
    assert out[0]["latency_ms"]["avg"] == 10.0


def test_route_without_timeouts_is_returned_unchanged() -> None:
    hops = [_hop(1), _hop(2)]
    assert interpolate_route(hops) == hops
    assert interpolate_route([]) == []


def test_destination_geo_hint_is_attached_to_interpolated_hops_only() -> None:
    geo = {"lat": 1.0, "lon": 2.0}
    out = interpolate_route([_hop(1), _timeout(2), _hop(3)], destination_geo=geo)
    assert out[1]["_geo_hint"] == geo
    assert "_geo_hint" not in out[0]


def test_ensure_destination_hop_appends_a_synthetic_partial_hop() -> None:
    geo = {"lat": 1.0, "lon": 2.0, "city": "X", "country": "DE"}
    out = ensure_destination_hop(
        [_hop(1), _hop(2)],
        destination_ip="198.51.100.9",
        destination_hostname="dest.example",
        destination_geo=geo,
        final_latency_ms=12.0,
    )
    dest = out[-1]
    assert (dest["hop"], dest["ip"], dest["status"]) == (3, "198.51.100.9", "partial")
    assert dest["_synthetic_destination"] is True
    assert dest["latency_ms"] is None
    assert dest["_estimated_latency_ms"] == 12.0
    assert (dest["city"], dest["country"]) == ("X", "DE")


def test_ensure_destination_hop_keeps_a_reached_destination_and_handles_empty_input() -> None:
    reached = [_hop(1), {**_hop(2), "ip": "198.51.100.9"}]
    assert ensure_destination_hop(reached, destination_ip="198.51.100.9") is reached
    assert ensure_destination_hop([], destination_ip="198.51.100.9") == []
    assert ensure_destination_hop(reached, destination_ip=None) is reached


def test_collapse_summarises_consecutive_interpolated_hops() -> None:
    hops = interpolate_route([_hop(1), _timeout(2), _timeout(3), _hop(4), _timeout(5), _hop(6)])
    rows = collapse_interpolated_hops(hops)
    assert [r.get("hop_display", r.get("hop")) for r in rows] == [1, "2 - 3", 4, "5", 6]
    assert rows[1]["hostname_display"] == "(2 hops interpolated)"
    assert rows[3]["hostname_display"] == "No reply"
    assert [h["hop"] for h in rows[1]["_raw_hops"]] == [2, 3]
    assert collapse_interpolated_hops([]) == []
