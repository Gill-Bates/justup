#!/usr/bin/env python3
#
# app/services/traceroute_service.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared traceroute service for the web UI and the PDF report.

One call resolves the target to ONE concrete IP, asks the privileged Nmap worker for the
route and returns the tool-independent ``Route`` together with the hop dictionaries the
route analysis, timeline and geolocation code works on. A failed trace is a controlled
result (``error_code``), never an exception, so a missing worker cannot break a page or a PDF.
"""

from __future__ import annotations

import asyncio
import atexit
import ipaddress
import logging
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from app.services import nmap_client
from app.services.nmap_scanner import NmapScannerError
from app.services.route_model import Route
from app.utils.constants import HopStatus

_log = logging.getLogger(__name__)

# Highest TTL the worker probes; a route that ends here without the destination may be cut off.
TRACE_HOP_LIMIT = 30
DEFAULT_TIMEOUT_S = 30

HOSTNAME_DISPLAY_MAX_LENGTH = 50
HOSTNAME_TRUNCATION_SUFFIX = "\u2026"

# Hop dictionary states; traceroute_geo and route_interpolator import them from here.
STATUS_OK = HopStatus.OK
STATUS_PARTIAL = "partial"  # not a HopStatus member (aggregate concept); synthetic destination row only
STATUS_TIMEOUT = HopStatus.TIMEOUT

# Controlled failure codes handed to the UI and the PDF.
ERROR_UNAVAILABLE = "unavailable"
ERROR_PERMISSION = "permission_denied"
ERROR_TIMEOUT = "timeout"
ERROR_NO_TRACE = "no_trace"
ERROR_INVALID_TARGET = "invalid_target"
ERROR_FAILED = "failed"

ERROR_MESSAGES = {
    ERROR_UNAVAILABLE: "The Nmap worker is not available",
    ERROR_PERMISSION: "The Nmap worker lacks the network capability needed for a traceroute",
    ERROR_TIMEOUT: "The traceroute timed out",
    ERROR_NO_TRACE: "No route could be traced to the destination",
    ERROR_INVALID_TARGET: "The target cannot be traced",
    ERROR_FAILED: "The traceroute failed",
}

_KNOWN_CODES = {
    "unavailable": ERROR_UNAVAILABLE,
    "permission_denied": ERROR_PERMISSION,
    "timeout": ERROR_TIMEOUT,
    "no_trace": ERROR_NO_TRACE,
    # The worker reports a rejected or unresolvable host as "invalid_host".
    "invalid_host": ERROR_INVALID_TARGET,
}


@dataclass(frozen=True)
class TracerouteOutcome:
    """Result of one trace: either a route or a controlled error code."""

    host: str
    target_ip: str | None
    route: Route | None = None
    hops: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.route is not None and self.error_code is None

    @property
    def error_message(self) -> str | None:
        return ERROR_MESSAGES.get(self.error_code) if self.error_code else None


def resolve_target_ip(host: str) -> str | None:
    """Resolve a target to ONE concrete IP (IPv4 or IPv6) that scan and evaluation both use."""
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            return sockaddr[0]
    return None


def _shorten_hostname_for_display(hostname: str, max_length: int = HOSTNAME_DISPLAY_MAX_LENGTH) -> str:
    """Shorten hostnames for UI/PDF display (the full hostname stays available for POP inference)."""
    if not hostname or len(hostname) <= max_length:
        return hostname
    return hostname[: max_length - len(HOSTNAME_TRUNCATION_SUFFIX)] + HOSTNAME_TRUNCATION_SUFFIX


_dns_executor: ThreadPoolExecutor | None = None
_dns_executor_lock = threading.Lock()


def _get_dns_executor() -> ThreadPoolExecutor:
    global _dns_executor
    if _dns_executor is None:
        with _dns_executor_lock:
            if _dns_executor is None:
                _dns_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolver")
                atexit.register(_dns_executor.shutdown, wait=False)
    return _dns_executor


def _resolve_sync(ip: str) -> str | None:
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
    except OSError:
        return None
    return hostname.strip().strip(".") or None


def _collect_resolved(futures: dict[str, Future], budget_s: float) -> dict[str, str | None]:
    """Collect finished lookups within one shared time budget; the rest resolve to None."""
    done, pending = wait(futures.values(), timeout=budget_s)
    for fut in pending:
        fut.cancel()
    resolved: dict[str, str | None] = {}
    for ip, fut in futures.items():
        try:
            resolved[ip] = fut.result(timeout=0) if fut in done else None
        except Exception:  # noqa: BLE001 - reverse DNS is best effort
            resolved[ip] = None
    return resolved


def route_to_hops(route: Route) -> list[dict[str, Any]]:
    """Turn a Route into hop dictionaries; a silent hop keeps its number and carries no value."""
    hops: list[dict[str, Any]] = []
    for hop in route.hops:
        if hop.status == "ok":
            hops.append(
                {
                    "hop": hop.hop_number,
                    "ip": hop.ip,
                    "hostname": hop.hostname,
                    "hostname_display": _shorten_hostname_for_display(hop.hostname) if hop.hostname else None,
                    "rtt_ms": hop.rtt_ms,
                    "status": STATUS_OK,
                }
            )
        else:
            hops.append(
                {
                    "hop": hop.hop_number,
                    "ip": "*",
                    "hostname": None,
                    "hostname_display": None,
                    "rtt_ms": None,
                    "status": STATUS_TIMEOUT,
                }
            )
    return hops


def _add_reverse_dns(hops: list[dict[str, Any]]) -> None:
    ips = {h["ip"] for h in hops if h["status"] == STATUS_OK and not h.get("hostname")}
    if not ips:
        return
    executor = _get_dns_executor()
    futures = {ip: executor.submit(_resolve_sync, ip) for ip in ips}
    resolved = _collect_resolved(futures, 1.5 if any(":" in ip for ip in ips) else 0.5)
    for hop in hops:
        name = resolved.get(hop["ip"]) if hop["status"] == STATUS_OK else None
        if name and not hop.get("hostname"):
            hop["hostname"] = name
            hop["hostname_display"] = _shorten_hostname_for_display(name)


def _failure(host: str, target_ip: str | None, code: str) -> TracerouteOutcome:
    return TracerouteOutcome(host=host, target_ip=target_ip, error_code=code)


async def trace(host: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> TracerouteOutcome:
    """Trace ``host`` through the Nmap worker; failures come back as ``error_code``."""
    target_ip = await asyncio.to_thread(resolve_target_ip, host)
    if target_ip is None:
        return _failure(host, None, ERROR_INVALID_TARGET)
    try:
        route = await nmap_client.request_traceroute(target_ip, timeout=timeout_s)
    except NmapScannerError as exc:
        code = _KNOWN_CODES.get(exc.error.code, ERROR_FAILED)
        _log.info("Traceroute to %s failed: %s", target_ip, exc.error.code)
        return _failure(host, target_ip, code)
    except (OSError, ValueError) as exc:
        _log.info("Traceroute to %s failed: %s", target_ip, type(exc).__name__)
        return _failure(host, target_ip, ERROR_FAILED)
    # The scan and the evaluation must name the same address.
    if route.destination_ip != target_ip:
        _log.warning("Traceroute destination %s differs from resolved %s", route.destination_ip, target_ip)
        return _failure(host, target_ip, ERROR_FAILED)
    hops = route_to_hops(route)
    if not hops:
        return _failure(host, target_ip, ERROR_NO_TRACE)
    await asyncio.to_thread(_add_reverse_dns, hops)
    return TracerouteOutcome(host=host, target_ip=target_ip, route=route, hops=hops)


def trace_sync(host: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> TracerouteOutcome:
    """Blocking wrapper for synchronous code (PDF build); never raises, never nests a loop."""
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(trace(host, timeout_s=timeout_s))
        # Called on a thread that already runs a loop: use a private thread with its own loop.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="traceroute-sync") as pool:
            return pool.submit(lambda: asyncio.run(trace(host, timeout_s=timeout_s))).result(timeout_s + 10)
    except Exception as exc:  # noqa: BLE001 - a failed trace must never break the PDF
        _log.warning("Traceroute for %s failed: %s", host, type(exc).__name__)
        return _failure(host, None, ERROR_FAILED)


def analyze_route(hops: list[dict[str, Any]], target_ip: str | None = None) -> dict[str, Any]:
    """Judge destination reachability and silent-hop blocks of a hop list.

    Returns ``destination_reached``, ``destination_ip``, ``blocked_range``, ``last_responding_hop``,
    ``needs_interpolation`` and ``timeout_blocks`` (list of (start_hop, end_hop)).
    """
    if not hops:
        return {
            "destination_reached": False,
            "destination_ip": target_ip,
            "blocked_range": None,
            "last_responding_hop": 0,
            "needs_interpolation": False,
            "timeout_blocks": [],
        }

    destination_idx = -1
    if target_ip:
        for i, h in enumerate(hops):
            if h.get("ip") == target_ip and h.get("status") in (STATUS_OK, STATUS_PARTIAL):
                destination_idx = i
                break
    destination_reached = destination_idx >= 0

    timeout_blocks: list[tuple[int, int]] = []
    last_responding_hop = 0
    last_before_first_block = 0
    block_start = None
    for i, h in enumerate(hops):
        hop_num = h.get("hop", i + 1)
        if h.get("status", STATUS_TIMEOUT) == STATUS_TIMEOUT:
            if block_start is None:
                block_start = hop_num
            continue
        if block_start is not None:
            timeout_blocks.append((block_start, hops[i - 1]["hop"] if i > 0 else block_start))
            block_start = None
        if not timeout_blocks:
            last_before_first_block = hop_num
        last_responding_hop = hop_num
    if block_start is not None:
        timeout_blocks.append((block_start, hops[-1].get("hop", len(hops))))

    blocked_start = blocked_end = None
    if destination_reached:
        dest_hop = hops[destination_idx].get("hop", destination_idx + 1)
        for start, end in timeout_blocks:
            if start < dest_hop:
                blocked_start = start if blocked_start is None else min(blocked_start, start)
                blocked_end = end if blocked_end is None else max(blocked_end, end)
    needs_interpolation = blocked_start is not None
    return {
        "destination_reached": destination_reached,
        "destination_ip": target_ip,
        "blocked_range": (blocked_start, blocked_end) if blocked_start else None,
        "last_responding_hop": last_before_first_block if blocked_start else last_responding_hop,
        "needs_interpolation": needs_interpolation,
        "timeout_blocks": timeout_blocks,
    }
