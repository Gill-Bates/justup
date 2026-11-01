#!/usr/bin/env python3
#
# tests/test_traceroute_pipeline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The web fragment and the PDF report must build the same traceroute from the same MTR run."""

# ruff: noqa: SLF001
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import frontend
from app.services import geo_resolver as gr
from app.services import mtr_run, pdf_report, traceroute_geo
from tests import _support  # noqa: F401 - imported for its sys.path side effect

TARGET_IP = "198.51.100.9"
HOST = "dest.example"


def _hop(n: int, ip: str | None, ms: float | None = 5.0) -> dict[str, Any]:
    if ip is None:
        return {"hop": n, "ip": "*", "hostname": None, "status": "timeout", "latency_ms": None, "loss_pct": 100.0}
    lat = {"avg": ms, "best": ms, "worst": ms} if ms is not None else None
    return {"hop": n, "ip": ip, "hostname": None, "status": "ok", "latency_ms": lat, "loss_pct": 0.0}


ROUTES = {
    "reached": [_hop(1, "192.0.2.1"), _hop(2, "192.0.2.2"), _hop(3, TARGET_IP)],
    "blocked_middle": [_hop(1, "192.0.2.1"), _hop(2, None), _hop(3, None), _hop(4, TARGET_IP, 20.0)],
    "trailing_timeout": [_hop(1, "192.0.2.1"), _hop(2, "192.0.2.2"), _hop(3, None), _hop(4, None)],
}


def _geo(ip: str | None, _hostname=None):
    if not ip or ip == "*":
        return None
    last = int(ip.rsplit(".", 1)[1])
    return {
        "lat": 50.0 + last / 100,
        "lon": 8.0,
        "city": f"City{last}",
        "country": "DE",
        "_confidence": "geoip",
        "accuracy_class": "precise",
    }


def _patch_sources(monkeypatch: pytest.MonkeyPatch, hops: list[dict]) -> None:
    def run(*_a, **_k):
        return [dict(h) for h in hops]

    # The web fragment imports lazily from the service modules, the PDF module binds names at import.
    for module in (mtr_run, pdf_report):
        monkeypatch.setattr(module, "resolve_target_ip", lambda _h: TARGET_IP, raising=False)
        monkeypatch.setattr(module, "run_mtr", run, raising=False)
    for module in (traceroute_geo, pdf_report):
        monkeypatch.setattr(module, "geolocate_with_hostname_hint", _geo, raising=False)


def _web_context(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def render(_request, _name, ctx):
        captured.update(ctx)
        return SimpleNamespace(body=b"", headers={})

    monkeypatch.setattr(frontend, "templates", SimpleNamespace(TemplateResponse=render))
    monkeypatch.setattr(frontend, "_cache_traceroute_fragment", lambda *_a, **_k: None)
    asyncio.run(frontend._run_traceroute_fragment(SimpleNamespace(), 1, HOST, "ns"))
    return captured


def _view(hops: list[dict]) -> list[tuple]:
    return [(h.get("hop_display", h.get("hop")), h.get("ip"), h.get("status"), h.get("city")) for h in hops]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_web_and_pdf_build_the_same_route(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    _patch_sources(monkeypatch, ROUTES[route])
    web = _web_context(monkeypatch)
    pdf_hops, pdf_points, pdf_interp = pdf_report._run_mtr_with_interpolation(HOST)
    # Only the web view strips trailing timeouts itself; the PDF strips them later, in _get_mtr_data.
    while pdf_hops and pdf_hops[-1].get("status") == "timeout" and not pdf_hops[-1].get("_interpolated"):
        pdf_hops.pop()
    assert _view(web["hops"]) == _view(pdf_hops)
    assert [p["ip"] for p in web["geo_points"]] == [p["ip"] for p in pdf_points]
    assert web["has_interpolation"] == pdf_interp


def test_blocked_middle_is_collapsed_and_flagged_in_both_views(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, ROUTES["blocked_middle"])
    web = _web_context(monkeypatch)
    pdf_hops, _points, pdf_interp = pdf_report._run_mtr_with_interpolation(HOST)
    assert web["has_interpolation"] is True
    assert pdf_interp is True
    for hops in (web["hops"], pdf_hops):
        collapsed = [h for h in hops if h.get("_collapsed")]
        assert [h["hop_display"] for h in collapsed] == ["2 - 3"]


def test_trailing_timeouts_stay_timeouts_in_the_pdf_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, ROUTES["trailing_timeout"])
    hops, _points, interpolated = pdf_report._run_mtr_with_interpolation(HOST)
    assert interpolated is False
    assert [h["status"] for h in hops if h["hop"] in (3, 4)] == ["timeout", "timeout"]


def test_unreached_destination_gets_one_synthetic_final_hop_in_both_views(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, ROUTES["trailing_timeout"])
    pdf_hops, _points, _interp = pdf_report._run_mtr_with_interpolation(HOST)
    web = _web_context(monkeypatch)
    for hops in (web["hops"], pdf_hops):
        synthetic = [h for h in hops if h.get("_synthetic_destination")]
        assert [(h["ip"], h["status"]) for h in synthetic] == [(TARGET_IP, "partial")]
        assert hops[-1] is synthetic[0]


def test_empty_mtr_result_yields_an_empty_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, [])
    assert pdf_report._run_mtr_with_interpolation(HOST) == ([], [], False)
    web = _web_context(monkeypatch)
    assert web["hops"] == []
    assert web["has_interpolation"] is False


def test_web_view_keeps_trailing_timeouts_when_no_destination_can_be_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, ROUTES["trailing_timeout"])
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _h: None)  # no synthetic destination hop
    web = _web_context(monkeypatch)
    assert [(h["hop"], h["status"]) for h in web["hops"]][-2:] == [(3, "timeout"), (4, "timeout")]


# ── Table and map tell the same story (owner report: table lists more cities than the map shows) ──

DEST = "84.116.200.1"


def _gl2(lat: float, lon: float, city: str | None, radius: float | None, *, region: bool = False):
    return gr.build_candidate(
        gr.SRC_GEOLITE2, lat, lon, city=city, country_code="DE", accuracy_radius_km=radius, city_is_region=region
    )


# Real resolver on fake candidates: only radius <= 25 km (Frankfurt, Wuppertal) is precisely mappable.
_CANDIDATES = {
    "212.227.121.182": [_gl2(49.49, 8.47, "Mannheim", 100)],
    "212.227.112.116": [_gl2(51.0, 9.0, None, None)],
    "212.227.113.1": [_gl2(50.11, 8.68, "Frankfurt", 10)],
    # coarse region position; another source only supplies a place NAME ("Kettenhausen")
    "92.79.239.89": [
        _gl2(50.6, 9.2, "Hessen", 200, region=True),
        gr.build_candidate(gr.SRC_IPWHO, 50.9, 9.5, city="Kettenhausen", country_code="DE"),
    ],
    "84.116.191.213": [_gl2(50.11, 8.68, "Frankfurt", 10)],
    "84.116.197.21": [_gl2(51.27, 7.2, "Wuppertal", 10)],
    DEST: [_gl2(51.3, 6.85, "Ratingen", 60)],
}
_OWNER_ROUTE = [
    _hop(1, "10.30.0.1"),
    _hop(2, "169.254.0.2"),
    _hop(3, "212.227.121.182"),
    _hop(5, "212.227.112.116"),
    _hop(7, "212.227.113.1"),
    _hop(8, "92.79.239.89"),
    _hop(9, None),
    _hop(10, "84.116.191.213"),
    _hop(11, "84.116.197.21"),
    _hop(12, None),
    _hop(13, DEST, 19.3),
]


def _owner_route_context(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(traceroute_geo, "_online_fallback_enabled", lambda: False)
    monkeypatch.setattr(
        traceroute_geo, "_collect_candidates", lambda ip, _h: (list(_CANDIDATES.get(ip, [])), [gr.SRC_GEOLITE2], {})
    )
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _h: DEST)
    monkeypatch.setattr(mtr_run, "run_mtr", lambda *_a, **_k: [dict(h) for h in _OWNER_ROUTE])
    return _web_context(monkeypatch)


def test_table_and_map_agree_on_places_pins_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _owner_route_context(monkeypatch)
    rows = {h["hop"]: h for h in ctx["hops"] if not h.get("_collapsed")}
    plotted = {}
    for point in ctx["geo_points"]:
        first, last = (
            (int(v) for v in point["_hop_range"].split("-")) if point.get("_hop_range") else (point["hop"],) * 2
        )
        for number in range(first, last + 1):
            plotted[number] = point
    # Every hop on the map shows exactly the city and country of its marker and carries the pin flag.
    assert set(plotted) == {number for number, row in rows.items() if row["on_map"]} == {7, 10, 11}
    for number, point in plotted.items():
        assert (rows[number]["city"], rows[number]["country"]) == (point["city"], point["country"])
    # The subtitle count is exactly the number of plotted hops (no range claims hop 8 or 9).
    assert ctx["mapped_hop_count"] == len(plotted) == 3
    # No city without a position of its own: the name from another source ("Kettenhausen") is not shown.
    assert (rows[8]["city"], rows[8]["country"], rows[8]["loc_precision"]) == (None, "DE", "country")
    assert rows[5]["city"] is None and rows[5]["loc_precision"] == "country"
    # Approximate cities stay visible but are flagged; private hops have no location claim.
    assert (rows[3]["city"], rows[3]["loc_precision"], rows[3]["on_map"]) == ("Mannheim", "approximate", False)
    assert rows[1]["city"] is None and rows[1]["loc_precision"] == "none"


def test_unmapped_destination_is_not_replaced_by_the_last_mapped_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _owner_route_context(monkeypatch)
    assert ctx["destination_on_map"] is False
    assert not any(point.get("_destination") for point in ctx["geo_points"])
    dest_row = next(h for h in ctx["hops"] if h.get("ip") == DEST)
    assert (dest_row["city"], dest_row["on_map"]) == ("Ratingen", False)
    assert ctx["kpis"]["city"] == "Ratingen"


def test_hops_split_by_an_unmapped_hop_are_never_merged_into_a_range(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _owner_route_context(monkeypatch)
    frankfurt = [p for p in ctx["geo_points"] if p["city"] == "Frankfurt"]
    assert [(p["hop"], p.get("_hop_range")) for p in frankfurt] == [(7, None), (10, None)]


def test_mapped_destination_marker_matches_the_destination_row(monkeypatch: pytest.MonkeyPatch) -> None:
    precise = {DEST: [_gl2(51.3, 6.85, "Ratingen", 10)]}
    monkeypatch.setitem(_CANDIDATES, DEST, precise[DEST])
    ctx = _owner_route_context(monkeypatch)
    point = next(p for p in ctx["geo_points"] if p.get("_destination"))
    row = next(h for h in ctx["hops"] if h.get("ip") == DEST)
    assert ctx["destination_on_map"] is True and row["on_map"] is True
    assert (point["city"], point["country"]) == (row["city"], row["country"]) == ("Ratingen", "DE")
    assert (point["lat"], point["lon"]) == (51.3, 6.85)
    assert ctx["mapped_hop_count"] == 4


def test_destination_ip_has_one_location_decision_even_when_the_last_hop_resolves_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hops = [_hop(1, "192.0.2.1"), _hop(2, TARGET_IP)]
    resolved = {"lat": 51.3, "lon": 6.85, "city": "Ratingen", "country": "DE", "_confidence": "geoip"}
    other = {"lat": 50.0, "lon": 8.0, "city": "Elsewhere", "country": "DE", "_confidence": "geoip"}
    monkeypatch.setattr(traceroute_geo, "geolocate_with_hostname_hint", lambda *_a: other)
    monkeypatch.setattr(traceroute_geo, "_classify_location", lambda _loc: (None, traceroute_geo.GeoAccuracy.PRECISE))
    points = traceroute_geo.mtr_hops_to_geo_points(hops, destination_ip=TARGET_IP, destination_geo=resolved)
    dest = next(p for p in points if p.get("_destination"))
    assert (dest["city"], dest["lat"], dest["lon"]) == ("Ratingen", 51.3, 6.85)


def test_mtr_traces_the_resolved_ip_not_the_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch, ROUTES["reached"])
    seen: list[tuple] = []
    monkeypatch.setattr(mtr_run, "run_mtr", lambda *a, **k: seen.append((a, k)) or [dict(h) for h in ROUTES["reached"]])
    _web_context(monkeypatch)
    assert seen[0][0] == (TARGET_IP,) and seen[0][1]["force_ipv6"] is False
    # IPv6 resolution keeps the address family; an unresolvable host falls back to the name.
    monkeypatch.setattr(traceroute_geo, "geolocate_with_hostname_hint", lambda *_a: None)
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _h: "2001:db8::9")
    _web_context(monkeypatch)
    assert seen[1][0] == ("2001:db8::9",) and seen[1][1]["force_ipv6"] is True
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _h: None)
    _web_context(monkeypatch)
    assert seen[2][0] == (HOST,)


def test_concurrent_cache_misses_share_one_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.responses import HTMLResponse

    calls: list[int] = []

    async def measure(_request, target_id, _host, _namespace):
        calls.append(target_id)
        await asyncio.sleep(0.05)
        return HTMLResponse(b"<p>route</p>", headers={"Cache-Control": "private, no-store", "X-Measured-At": "42"})

    monkeypatch.setattr(frontend, "_run_traceroute_fragment", measure)

    async def burst():
        return await asyncio.gather(*(frontend._single_flight_traceroute(None, 1, HOST, "ns") for _ in range(10)))

    responses = asyncio.run(burst())
    assert calls == [1]
    assert {r.body for r in responses} == {b"<p>route</p>"}
    assert {r.headers["x-measured-at"] for r in responses} == {"42"}
    assert frontend._traceroute_inflight == {}
    asyncio.run(burst())  # the reservation is released: a later miss measures again
    assert len(calls) == 2


def test_failed_measurement_releases_the_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a):
        raise RuntimeError("mtr failed")

    monkeypatch.setattr(frontend, "_run_traceroute_fragment", boom)

    async def run():
        with pytest.raises(RuntimeError):
            await frontend._single_flight_traceroute(None, 1, HOST, "ns")
        await asyncio.sleep(0)
        return dict(frontend._traceroute_inflight)

    assert asyncio.run(run()) == {}


@pytest.mark.parametrize("route", ["reached", "blocked_middle", "trailing_timeout"])
def test_kpi_destination_verdict_is_the_route_analysis_verdict(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    _patch_sources(monkeypatch, ROUTES[route])
    web = _web_context(monkeypatch)
    reached = mtr_run.analyze_route(ROUTES[route], TARGET_IP)["destination_reached"]
    assert web["kpis"]["destination_reached"] is reached
    if not reached:  # a synthetic destination never supplies latency or loss
        assert web["kpis"]["latency_display"] is None and web["kpis"]["loss_display"] is None


def test_kpis_follow_an_explicit_verdict_over_hop_flags() -> None:
    hops = [{"hop": 1, "ip": TARGET_IP, "status": "ok", "latency_ms": {"avg": 5.0}, "loss_pct": 0.0}]
    assert frontend._destination_kpis(hops, TARGET_IP, None, True)["latency_display"] == "5.0 ms"
    unreached = frontend._destination_kpis(hops, TARGET_IP, None, False)
    assert unreached["destination_reached"] is False and unreached["latency_display"] is None
