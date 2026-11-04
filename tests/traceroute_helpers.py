"""Test helpers: feed hop fixtures through the real traceroute service via a fake Nmap client."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import nmap_client, traceroute_service
from app.services.route_model import Route, RouteHop


def route_from_hops(hops: list[dict[str, Any]], destination_ip: str) -> Route:
    """Build a Route from hop dicts; ip None/'*' is a silent hop, rtt_ms or latency_ms.avg is the RTT."""
    route_hops = []
    for h in hops:
        ip = h.get("ip")
        if not ip or ip in ("*", "???") or h.get("status") == "timeout":
            route_hops.append(RouteHop(hop_number=h["hop"], status="no_reply"))
            continue
        rtt = h.get("rtt_ms")
        lat = h.get("latency_ms")
        if rtt is None and isinstance(lat, dict):
            rtt = lat.get("avg")
        route_hops.append(
            RouteHop(hop_number=h["hop"], ip=ip, hostname=h.get("hostname"), rtt_ms=rtt or None, status="ok")
        )
    start = datetime.now(UTC) - timedelta(milliseconds=300)
    return Route(
        destination_ip=destination_ip,
        address_family="ipv6" if ipaddress.ip_address(destination_ip).version == 6 else "ipv4",
        started_at=start,
        completed_at=start + timedelta(milliseconds=300),
        duration_ms=300,
        destination_reached=bool(route_hops) and route_hops[-1].ip == destination_ip,
        complete=True,
        hops=route_hops,
    )


def patch_trace(
    monkeypatch: pytest.MonkeyPatch,
    hops_for: Callable[[str], list[dict[str, Any]]] | list[dict[str, Any]],
    *,
    resolved: str | None = None,
    calls: list[str] | None = None,
) -> None:
    """Replace name resolution and the worker client; ``calls`` records the address that was traced."""

    async def fake_request(host: str, **_kwargs: Any) -> Route:
        if calls is not None:
            calls.append(host)
        hops = hops_for(host) if callable(hops_for) else hops_for
        return route_from_hops([dict(h) for h in hops], host)

    monkeypatch.setattr(traceroute_service, "resolve_target_ip", lambda _h: resolved)
    monkeypatch.setattr(traceroute_service, "_add_reverse_dns", lambda _hops: None)
    monkeypatch.setattr(nmap_client, "request_traceroute", fake_request)
