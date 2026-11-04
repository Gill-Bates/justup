#!/usr/bin/env python3
#
# app/services/nmap_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded Nmap discovery and selected-port scans with typed XML results."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import signal
from datetime import datetime
from typing import Literal

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, Field

from app.models.targets import validate_host
from app.utils.ssrf import resolve_validated
from app.utils.tcp_ports import parse_port_list
from app.utils.time import utcnow

Protocol = Literal["tcp", "udp"]
ScanState = Literal["open", "closed", "filtered", "open|filtered", "unfiltered", "closed|filtered", "unknown"]

QUICK_TCP_PORTS = (21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 3306, 5432, 8080, 8443)
QUICK_UDP_PORTS = (53, 67, 68, 123, 161, 162, 500, 514, 1900)
EXTENDED_TCP_PORTS = (*QUICK_TCP_PORTS, 23, 111, 135, 139, 389, 445, 636, 1521, 2375, 27017)
EXTENDED_UDP_PORTS = (*QUICK_UDP_PORTS, 69, 137, 138, 389, 4500, 5353)
MAX_PORTS = 128
MAX_OUTPUT_BYTES = 1_000_000
MAX_CONCURRENT_SCANS = 2
NMAP_BINARY = "/usr/bin/nmap"

_scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
_target_locks: dict[str, asyncio.Lock] = {}


class NmapServiceInfo(BaseModel):
    name: str | None = None
    product: str | None = None
    version: str | None = None
    method: Literal["probed", "table"] | None = None
    confidence: int | None = Field(default=None, ge=0, le=10)


class NmapPortResult(BaseModel):
    protocol: Protocol
    port: int = Field(ge=1, le=65535)
    state: ScanState
    reason: str | None = None
    service: NmapServiceInfo | None = None


class NmapHostResult(BaseModel):
    address: str
    status: str | None = None
    ports: list[NmapPortResult] = Field(default_factory=list)


class NmapScanResult(BaseModel):
    hosts: list[NmapHostResult] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    complete: bool


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
    ]
    message: str


class NmapScannerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error = NmapScanError(code=code, message=message)


def parse_nmap_xml(data: bytes, *, started_at: datetime, completed_at: datetime) -> NmapScanResult:
    """Parse bounded Nmap XML without expanding external entities or DTDs."""
    if len(data) > MAX_OUTPUT_BYTES or b"\x00" in data:
        raise NmapScannerError("invalid_xml", "Nmap returned an invalid or oversized XML document")
    try:
        text = data.decode("utf-8")
        root = ElementTree.fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (UnicodeError, ElementTree.ParseError, DefusedXmlException) as exc:
        raise NmapScannerError("invalid_xml", "Nmap returned invalid XML") from exc
    if root.tag != "nmaprun":
        raise NmapScannerError("invalid_xml", "Nmap returned an unexpected XML document")
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
                    confidence=int(confidence) if confidence and confidence.isdigit() else None,
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


async def _resolve(host: str) -> str:
    try:
        validated = validate_host(host)
    except ValueError as exc:
        raise NmapScannerError("invalid_host", "Invalid target host") from exc
    if not validated:
        raise NmapScannerError("invalid_host", "Target host is required")
    addresses = await asyncio.to_thread(resolve_validated, validated, allow_private=True)
    if not addresses:
        raise NmapScannerError("invalid_host", "Target host has no permitted address")
    return str(addresses[0])


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
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


async def _run_locked(command: list[str], timeout: float) -> NmapScanResult:
    async with _scan_slots:
        started = utcnow()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
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
            raise NmapScannerError("timeout", "Nmap scan timed out") from exc
        except BaseException:
            await _stop_process(proc)
            raise
        if proc.returncode != 0:
            detail = "Nmap could not complete the scan"
            if b"requires root privileges" in stderr.lower() or b"permission denied" in stderr.lower():
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
