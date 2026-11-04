"""Shared traceroute service: pinned resolution, controlled failures, sync wrapper, web == PDF hops."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import nmap_client, pdf_report, traceroute_geo, traceroute_service
from app.services.nmap_scanner import NmapScannerError
from tests.test_traceroute_pipeline import HOST, ROUTES, TARGET_IP, _geo, _web_context
from tests.traceroute_helpers import patch_trace, route_from_hops


def test_service_traces_exactly_the_resolved_address(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    patch_trace(monkeypatch, ROUTES["reached"], resolved=TARGET_IP, calls=seen)
    outcome = asyncio.run(traceroute_service.trace(HOST))
    assert outcome.ok and seen == [TARGET_IP] and outcome.target_ip == TARGET_IP
    assert outcome.route is not None and outcome.route.destination_ip == TARGET_IP


def test_silent_hops_keep_their_number_and_carry_no_value(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_trace(monkeypatch, ROUTES["blocked_middle"], resolved=TARGET_IP)
    hops = asyncio.run(traceroute_service.trace(HOST)).hops
    assert [h["hop"] for h in hops] == [1, 2, 3, 4]
    assert hops[1] == {
        "hop": 2,
        "ip": "*",
        "hostname": None,
        "hostname_display": None,
        "rtt_ms": None,
        "status": "timeout",
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unavailable", "unavailable"),
        ("permission_denied", "permission_denied"),
        ("timeout", "timeout"),
        ("no_trace", "no_trace"),
        ("process_failed", "failed"),
    ],
)
def test_worker_errors_become_controlled_outcomes(monkeypatch: pytest.MonkeyPatch, code: str, expected: str) -> None:
    patch_trace(monkeypatch, [], resolved=TARGET_IP)

    async def fail(*_a: Any, **_k: Any):
        raise NmapScannerError(code, "boom")

    monkeypatch.setattr(nmap_client, "request_traceroute", fail)
    outcome = asyncio.run(traceroute_service.trace(HOST))
    assert not outcome.ok and outcome.error_code == expected and outcome.error_message


def test_unresolvable_host_is_never_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    patch_trace(monkeypatch, ROUTES["reached"], resolved=None, calls=seen)
    outcome = asyncio.run(traceroute_service.trace(HOST))
    assert outcome.error_code == "invalid_target" and seen == []


def test_destination_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_trace(monkeypatch, [], resolved=TARGET_IP)

    async def other(*_a: Any, **_k: Any):
        return route_from_hops(ROUTES["reached"], "198.51.100.77")

    monkeypatch.setattr(nmap_client, "request_traceroute", other)
    assert asyncio.run(traceroute_service.trace(HOST)).error_code == "failed"


def test_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_trace(monkeypatch, [], resolved=TARGET_IP)

    async def slow(*_a: Any, **_k: Any):
        await asyncio.sleep(30)

    monkeypatch.setattr(nmap_client, "request_traceroute", slow)

    async def run() -> None:
        task = asyncio.create_task(traceroute_service.trace(HOST))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_sync_wrapper_works_outside_and_inside_a_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_trace(monkeypatch, ROUTES["reached"], resolved=TARGET_IP)
    assert traceroute_service.trace_sync(HOST).ok

    async def inside() -> bool:
        return traceroute_service.trace_sync(HOST).ok

    assert asyncio.run(inside())


def test_pdf_route_survives_a_failing_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_trace(monkeypatch, [], resolved=TARGET_IP)

    async def fail(*_a: Any, **_k: Any):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(nmap_client, "request_traceroute", fail)
    assert pdf_report._run_traceroute_with_interpolation(HOST) == ([], [], False)  # noqa: SLF001


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_web_and_pdf_render_the_same_hop_sequence_from_one_route(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    patch_trace(monkeypatch, ROUTES[route], resolved=TARGET_IP)
    for module in (traceroute_geo, pdf_report):
        monkeypatch.setattr(module, "geolocate_with_hostname_hint", _geo, raising=False)
    web = _web_context(monkeypatch)
    pdf_hops, _points, _interp = pdf_report._run_traceroute_with_interpolation(HOST)  # noqa: SLF001

    def sequence(hops: list[dict]) -> list[tuple]:
        numbers: list[tuple] = []
        for h in hops:
            raw = h.get("_raw_hops")
            numbers.extend((r["hop"], r["ip"]) for r in raw) if raw else numbers.append((h["hop"], h["ip"]))
        return numbers

    pdf_seq = sequence(pdf_hops)
    web_seq = sequence(web["hops"])
    assert web_seq == pdf_seq[: len(web_seq)] and [n for n, _ in web_seq] == sorted(n for n, _ in web_seq)
    assert all("loss_pct" not in h and "latency_ms" not in h for h in pdf_hops)
