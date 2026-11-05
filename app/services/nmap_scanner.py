#!/usr/bin/env python3
#
# app/services/nmap_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded Nmap discovery and selected-port scans with typed XML results."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import shutil
import signal
from contextlib import suppress
from datetime import datetime
from functools import partial
from typing import Literal

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel

from app.models.network_scan import NmapHostResult, NmapPortResult, NmapScanResult, NmapServiceInfo, ScanState
from app.models.targets import validate_host
from app.services.route_model import MAX_HOPS, RequestedProbeProtocol, Route, RouteHop
from app.utils.ssrf import resolve_validated
from app.utils.tcp_ports import parse_port_list
from app.utils.time import utcnow

QUICK_TCP_PORTS = (21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 3306, 5432, 8080, 8443)
QUICK_UDP_PORTS = (53, 67, 68, 123, 161, 162, 500, 514, 1900)
EXTENDED_TCP_PORTS = (*QUICK_TCP_PORTS, 23, 111, 135, 139, 389, 445, 636, 1521, 2375, 27017)
EXTENDED_UDP_PORTS = (*QUICK_UDP_PORTS, 69, 137, 138, 389, 4500, 5353)
MAX_PORTS = 128
MAX_OUTPUT_BYTES = 1_000_000
MAX_CONCURRENT_SCANS = 2
# One cap for every Nmap process. It is below scans + traces, so neither kind can occupy all slots.
MAX_CONCURRENT_NMAP = 3
DNS_TIMEOUT = 5.0
MAX_CONCURRENT_RESOLUTIONS = 4
DEFAULT_NMAP_BINARY = "/usr/bin/nmap"


def locate_binary() -> str | None:
    """Return the Nmap executable: the image path first, else `nmap` from PATH (hosts running outside Docker)."""
    if os.access(DEFAULT_NMAP_BINARY, os.X_OK):
        return DEFAULT_NMAP_BINARY
    return shutil.which("nmap")


NMAP_BINARY = locate_binary() or DEFAULT_NMAP_BINARY

_scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
_nmap_slots = asyncio.Semaphore(MAX_CONCURRENT_NMAP)
_resolve_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_target_locks: dict[str, asyncio.Lock] = {}


class NmapScanError(BaseModel):
    code: Literal[
        "invalid_host",
        "invalid_ports",
        "busy",
        "unavailable",
        "permission_denied",
        "timeout",
        "output_limit",
        "process_failed",
        "invalid_xml",
        "no_trace",
    ]
    message: str


class NmapScannerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error = NmapScanError(code=code, message=message)


def parse_nmap_xml(data: bytes, *, started_at: datetime, completed_at: datetime) -> NmapScanResult:
    """Parse bounded Nmap XML without expanding external entities or DTDs."""
    root = _parse_root(data)
    hosts: list[NmapHostResult] = []
    for host in root.findall("host"):
        address_el = next((node for node in host.findall("address") if node.get("addrtype") in {"ipv4", "ipv6"}), None)
        if address_el is None or not address_el.get("addr"):
            continue
        ports: list[NmapPortResult] = []
        for node in host.findall("./ports/port"):
            protocol = node.get("protocol")
            number = node.get("portid")
            if protocol not in {"tcp", "udp"} or not number or not number.isascii() or not number.isdigit():
                continue
            if not 1 <= int(number) <= 65535:
                continue
            state_el = node.find("state")
            raw_state = state_el.get("state") if state_el is not None else "unknown"
            state: ScanState = (
                raw_state
                if raw_state in {"open", "closed", "filtered", "open|filtered", "unfiltered", "closed|filtered"}
                else "unknown"
            )  # type: ignore[assignment]
            service_el = node.find("service")
            service = None
            if service_el is not None:
                confidence = service_el.get("conf")
                service = NmapServiceInfo(
                    name=service_el.get("name"),
                    product=service_el.get("product"),
                    version=service_el.get("version"),
                    method=service_el.get("method") if service_el.get("method") in {"probed", "table"} else None,
                    confidence=int(confidence)
                    if confidence and confidence.isascii() and confidence.isdigit() and int(confidence) <= 10
                    else None,
                )
            ports.append(
                NmapPortResult(
                    protocol=protocol,
                    port=int(number),
                    state=state,
                    reason=state_el.get("reason") if state_el is not None else None,
                    service=service,
                )
            )
        status_el = host.find("status")
        hosts.append(
            NmapHostResult(
                address=address_el.get("addr", ""),
                status=status_el.get("state") if status_el is not None else None,
                ports=ports,
            )
        )
    finished = root.find("./runstats/finished")
    complete = finished is not None and finished.get("exit") == "success"
    return NmapScanResult(hosts=hosts, started_at=started_at, completed_at=completed_at, complete=complete)


def _normalize_ports(values: list[int]) -> list[int]:
    try:
        ports = parse_port_list(values)
    except ValueError as exc:
        raise NmapScannerError("invalid_ports", "Port numbers must be between 1 and 65535") from exc
    if len(ports) > MAX_PORTS:
        raise NmapScannerError("invalid_ports", f"At most {MAX_PORTS} ports may be scanned")
    return ports


def _port_argument(tcp_ports: list[int], udp_ports: list[int]) -> str:
    parts = []
    if tcp_ports:
        parts.append("T:" + ",".join(str(port) for port in tcp_ports))
    if udp_ports:
        parts.append("U:" + ",".join(str(port) for port in udp_ports))
    return ",".join(parts)


def _resolve_slots() -> asyncio.Semaphore:
    """Return the resolution limiter of the running loop (created lazily: a semaphore binds to one loop)."""
    global _resolve_gate
    loop = asyncio.get_running_loop()
    if _resolve_gate is None or _resolve_gate[0] is not loop:
        _resolve_gate = (loop, asyncio.Semaphore(MAX_CONCURRENT_RESOLUTIONS))
    return _resolve_gate[1]


def _release_when_done(slots: asyncio.Semaphore, future: asyncio.Future) -> None:
    """Free the slot only when the lookup thread ends: an abandoned lookup still occupies its thread."""
    if not future.cancelled():
        future.exception()  # mark retrieved
    slots.release()


async def _lookup(host: str, limit: float) -> list:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    slots = _resolve_slots()
    try:
        async with asyncio.timeout(limit):
            await slots.acquire()
    except TimeoutError as exc:
        raise NmapScannerError("timeout", "Name resolution timed out") from exc
    future = loop.run_in_executor(None, partial(resolve_validated, host, allow_private=True))
    future.add_done_callback(partial(_release_when_done, slots))
    done, _ = await asyncio.wait({future}, timeout=max(0.0, deadline - loop.time()))
    if not done:
        raise NmapScannerError("timeout", "Name resolution timed out")
    return future.result()


async def _resolve(host: str, *, timeout: float | None = None) -> str:
    """Resolve a validated host within DNS_TIMEOUT (or the smaller caller budget)."""
    try:
        validated = validate_host(host)
    except ValueError as exc:
        raise NmapScannerError("invalid_host", "Invalid target host") from exc
    if not validated:
        raise NmapScannerError("invalid_host", "Target host is required")
    addresses = await _lookup(validated, DNS_TIMEOUT if timeout is None else min(DNS_TIMEOUT, timeout))
    if not addresses:
        raise NmapScannerError("invalid_host", "Target host has no permitted address")
    return str(addresses[0])


def _signal_group(pid: int, sig: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, sig)


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate the scan's whole process group; the final SIGKILL also reaps stragglers."""
    if proc.returncode is None:
        _signal_group(proc.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)
    _signal_group(proc.pid, signal.SIGKILL)
    await proc.wait()


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks = bytearray()
    while chunk := await stream.read(16384):
        chunks.extend(chunk)
        if len(chunks) > MAX_OUTPUT_BYTES:
            raise NmapScannerError("output_limit", "Nmap output exceeded the size limit")
    return bytes(chunks)


async def scan_selected_ports(
    host: str,
    tcp_ports: list[int],
    udp_ports: list[int],
    *,
    timeout: float,
    detect_services: bool = False,
) -> NmapScanResult:
    """Run one bounded scan against the resolved address of a validated target host."""
    tcp = _normalize_ports(tcp_ports)
    udp = _normalize_ports(udp_ports)
    if not tcp and not udp:
        raise NmapScannerError("invalid_ports", "Select at least one TCP or UDP port")
    if len(tcp) + len(udp) > MAX_PORTS:
        raise NmapScannerError("invalid_ports", f"At most {MAX_PORTS} ports may be scanned")
    if not 1 <= timeout <= 300:
        raise ValueError("Scan timeout must be between 1 and 300 seconds")
    address = await _resolve(host)
    command = [
        NMAP_BINARY,
        "-Pn",
        "-n",
        "--reason",
        "--max-retries",
        "1",
        "--host-timeout",
        f"{int(timeout)}s",
        "-oX",
        "-",
    ]
    if tcp:
        command.append("-sT")
    if udp:
        command.extend(("-sU", "--privileged"))
    if detect_services:
        command.extend(("-sV", "--version-light"))
    if ipaddress.ip_address(address).version == 6:
        command.append("-6")
    command.extend(("-p", _port_argument(tcp, udp), address))
    lock = _target_locks.setdefault(address, asyncio.Lock())
    if lock.locked():
        raise NmapScannerError("busy", "A scan is already running for this target")
    await lock.acquire()
    try:
        return await _run_locked(command, timeout)
    finally:
        lock.release()
        if _target_locks.get(address) is lock:
            del _target_locks[address]


async def _exec_bounded(command: list[str], timeout: float, timeout_message: str) -> tuple[int, bytes, bytes]:
    """Run ``command`` in its own session with bounded output; stop the whole group on timeout or cancel."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True
        )
    except FileNotFoundError as exc:
        raise NmapScannerError("unavailable", "Nmap is not installed") from exc
    except PermissionError as exc:
        raise NmapScannerError("permission_denied", "Nmap is not executable") from exc
    try:
        async with asyncio.timeout(timeout + 2):
            stdout, stderr = await asyncio.gather(_read_bounded(proc.stdout), _read_bounded(proc.stderr))
            await proc.wait()
    except TimeoutError as exc:
        await _stop_process(proc)
        raise NmapScannerError("timeout", timeout_message) from exc
    except BaseException:
        await _stop_process(proc)
        raise
    return proc.returncode, stdout, stderr


_PERMISSION_MARKERS = (b"root privileges", b"as root", b"permission denied", b"operation not permitted")


def _is_permission_error(stderr: bytes) -> bool:
    """True when Nmap's stderr reports missing privileges; shared by scan and trace paths."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _PERMISSION_MARKERS)


async def _run_locked(command: list[str], timeout: float) -> NmapScanResult:
    async with _scan_slots, _nmap_slots:
        started = utcnow()
        returncode, stdout, stderr = await _exec_bounded(command, timeout, "Nmap scan timed out")
        if returncode != 0:
            detail = "Nmap could not complete the scan"
            if _is_permission_error(stderr):
                raise NmapScannerError("permission_denied", detail)
            raise NmapScannerError("process_failed", detail)
        result = parse_nmap_xml(stdout, started_at=started, completed_at=utcnow())
        if not result.complete:
            raise NmapScannerError("process_failed", "Nmap reported an incomplete scan")
        return result


async def discover_ports(host: str, protocols: set[str], *, timeout: float, profile: str = "quick") -> NmapScanResult:
    """Discover bounded common ports without scanning the full port range."""
    if not protocols or not protocols <= {"tcp", "udp"}:
        raise NmapScannerError("invalid_ports", "Select TCP, UDP, or both")
    if profile not in {"quick", "extended"}:
        raise NmapScannerError("invalid_ports", "Unknown scan profile")
    tcp = (QUICK_TCP_PORTS if profile == "quick" else EXTENDED_TCP_PORTS) if "tcp" in protocols else ()
    udp = (QUICK_UDP_PORTS if profile == "quick" else EXTENDED_UDP_PORTS) if "udp" in protocols else ()
    return await scan_selected_ports(host, list(tcp), list(udp), timeout=timeout, detect_services=True)


# Traceroute: `--traceroute` cannot be combined with `-sT` (a connect scan sends no raw probes), so a trace never
# reuses the port-scan command. Attempt 1 is host discovery only (`-sn`: nothing is port-scanned);
# attempt 2 runs only when attempt 1 produced no trace or timed out, as one bounded SYN (or UDP) probe port.
# A probe requested by the caller is the caller's decision: it runs first, alone and with the whole budget.
# DNS and all attempts share one monotonic deadline; permission, input and internal errors never retry.
MAX_CONCURRENT_TRACEROUTES = 2
DEFAULT_PROBE_PORT = 443
MAX_RTT_MS = 60_000.0
MIN_ATTEMPT_SECONDS = 3
KILL_GRACE_SECONDS = 2  # _run_trace stops the process this long after Nmap's own --host-timeout
FALLBACK_ERRORS = frozenset({"no_trace", "timeout"})
_trace_slots = asyncio.Semaphore(MAX_CONCURRENT_TRACEROUTES)
_trace_locks: dict[str, asyncio.Lock] = {}


def _parse_root(data: bytes):
    if len(data) > MAX_OUTPUT_BYTES or b"\x00" in data:
        raise NmapScannerError("invalid_xml", "Nmap returned an invalid or oversized XML document")
    try:
        root = ElementTree.fromstring(
            data.decode("utf-8"), forbid_dtd=False, forbid_entities=True, forbid_external=True
        )
    except (UnicodeError, ElementTree.ParseError, DefusedXmlException) as exc:
        raise NmapScannerError("invalid_xml", "Nmap returned invalid XML") from exc
    if root.tag != "nmaprun":
        raise NmapScannerError("invalid_xml", "Nmap returned an unexpected XML document")
    return root


def _parse_rtt(value: str | None) -> float | None:
    try:
        rtt = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return rtt if math.isfinite(rtt) and 0 <= rtt <= MAX_RTT_MS else None


def parse_traceroute_xml(data: bytes, *, destination_ip: str, started_at: datetime, completed_at: datetime) -> Route:
    """Parse Nmap trace XML into a Route; silent TTLs between answers become explicit ``no_reply`` hops."""
    destination = ipaddress.ip_address(destination_ip)
    root = _parse_root(data)
    host = None
    for candidate in root.findall("host"):
        for node in candidate.findall("address"):
            try:
                if ipaddress.ip_address(node.get("addr", "")) == destination:
                    host = candidate
            except ValueError:
                continue
    trace = host.find("trace") if host is not None else None
    if host is None or trace is None:
        raise NmapScannerError("no_trace", "Nmap returned no route data for the destination")
    hops: list[RouteHop] = []
    expected = 1
    for node in trace.findall("hop"):
        ttl_raw = node.get("ttl", "")
        if not ttl_raw.isascii() or not ttl_raw.isdigit() or not expected <= int(ttl_raw) <= MAX_HOPS:
            raise NmapScannerError("invalid_xml", "Nmap returned an invalid hop number")
        ttl = int(ttl_raw)
        try:
            address = ipaddress.ip_address(node.get("ipaddr", ""))
        except ValueError as exc:
            raise NmapScannerError("invalid_xml", "Nmap returned an invalid hop address") from exc
        if address.version != destination.version:
            raise NmapScannerError("invalid_xml", "Nmap returned a hop of the wrong address family")
        hops.extend(RouteHop(hop_number=number, status="no_reply") for number in range(expected, ttl))
        name = (node.get("host") or "").strip()
        hops.append(
            RouteHop(
                hop_number=ttl,
                ip=str(address),
                hostname=name if name and len(name) <= 255 and name.isprintable() else None,
                rtt_ms=_parse_rtt(node.get("rtt")),
                status="ok",
            )
        )
        expected = ttl + 1
    if not hops:
        raise NmapScannerError("no_trace", "Nmap returned no route data for the destination")
    port_raw = trace.get("port", "")
    proto = trace.get("proto")
    port = (
        int(port_raw)
        if proto in {"tcp", "udp"} and port_raw.isascii() and port_raw.isdigit() and 1 <= int(port_raw) <= 65535
        else None
    )
    finished = root.find("./runstats/finished")
    return Route(
        destination_ip=str(destination),
        address_family="ipv6" if destination.version == 6 else "ipv4",
        probe_protocol=proto if proto in {"icmp", "tcp", "udp"} else None,
        probe_port=port,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, int((completed_at - started_at).total_seconds() * 1000)),
        destination_reached=hops[-1].ip == str(destination),
        complete=finished is not None and finished.get("exit") == "success",
        hops=hops,
    )


def _trace_command(address: str, *, budget: int, probe: tuple[RequestedProbeProtocol, int] | None) -> list[str]:
    """Build the fixed trace command line; ``probe`` selects the bounded port variant instead of discovery."""
    command = [NMAP_BINARY, "--privileged", "-n", "--max-retries", "1", "--host-timeout", f"{budget}s", "-oX", "-"]
    if probe is None:
        command.append("-sn")
    else:
        protocol, port = probe
        command.extend(("-Pn", "-sS" if protocol == "tcp" else "-sU", "-p", str(port)))
    command.append("--traceroute")
    if ipaddress.ip_address(address).version == 6:
        command.append("-6")
    command.append(address)
    return command


async def _run_trace(command: list[str], address: str, timeout: float) -> Route:
    started = utcnow()
    returncode, stdout, stderr = await _exec_bounded(command, timeout, "Nmap traceroute timed out")
    if returncode != 0:
        if _is_permission_error(stderr):
            raise NmapScannerError("permission_denied", "Nmap lacks the privileges for a traceroute")
        raise NmapScannerError("process_failed", "Nmap could not complete the traceroute")
    route = parse_traceroute_xml(stdout, destination_ip=address, started_at=started, completed_at=utcnow())
    if not route.complete:
        raise NmapScannerError("process_failed", "Nmap reported an incomplete traceroute")
    return route


def _attempt_budget(remaining: float, *, share: float | None) -> int | None:
    """Nmap host-timeout for one attempt, or None when the deadline leaves no sensible attempt."""
    if remaining < MIN_ATTEMPT_SECONDS:
        return None
    if share is not None:
        return max(MIN_ATTEMPT_SECONDS, int(remaining * share))
    budget = int(remaining) - KILL_GRACE_SECONDS
    return budget if budget >= MIN_ATTEMPT_SECONDS else None


async def trace_route(
    host: str,
    *,
    timeout: float,
    probe_protocol: RequestedProbeProtocol | None = None,
    probe_port: int | None = None,
) -> Route:
    """Trace the path to the resolved address of a validated host within one deadline of ``timeout`` seconds.

    A requested probe runs once with the whole budget. Without one, host discovery gets 40 % and a single
    TCP/443 SYN probe the rest, but only after "no trace" or a timeout and only if enough time is left.
    """
    if not 5 <= timeout <= 120:
        raise ValueError("Traceroute timeout must be between 5 and 120 seconds")
    if (probe_protocol is None) != (probe_port is None) or probe_protocol not in {None, "tcp", "udp"}:
        raise NmapScannerError("invalid_ports", "Specify a TCP or UDP probe port together with its protocol")
    if probe_port is not None and not 1 <= probe_port <= 65535:
        raise NmapScannerError("invalid_ports", "Probe port must be between 1 and 65535")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    address = await _resolve(host, timeout=timeout)
    first: tuple[RequestedProbeProtocol, int] | None = None
    fallback: tuple[RequestedProbeProtocol, int] | None = None
    if probe_protocol is not None and probe_port is not None:
        first = (probe_protocol, probe_port)
    else:
        fallback = ("tcp", DEFAULT_PROBE_PORT)
    # Refuse before a lock entry exists, so refused requests cannot leave one behind per address.
    if _trace_slots.locked() or _nmap_slots.locked():
        raise NmapScannerError("busy", "A traceroute is already running for this target or the worker is busy")
    lock = _trace_locks.setdefault(address, asyncio.Lock())
    if lock.locked():
        raise NmapScannerError("busy", "A traceroute is already running for this target or the worker is busy")
    await lock.acquire()
    try:
        async with _trace_slots, _nmap_slots:
            budget = _attempt_budget(deadline - loop.time(), share=0.4 if fallback else None)
            if budget is None:
                raise NmapScannerError("timeout", "No time left for a traceroute")
            try:
                return await _run_trace(_trace_command(address, budget=budget, probe=first), address, budget)
            except NmapScannerError as exc:
                if fallback is None or exc.error.code not in FALLBACK_ERRORS:
                    raise
                first_error = exc
            budget = _attempt_budget(deadline - loop.time(), share=None)
            if budget is None:
                raise first_error
            return await _run_trace(_trace_command(address, budget=budget, probe=fallback), address, budget)
    finally:
        lock.release()
        if _trace_locks.get(address) is lock:
            del _trace_locks[address]
