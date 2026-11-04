#!/usr/bin/env python3
#
# app/services/nmap_client.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Client for the privilege-separated local Nmap worker."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from app.services.nmap_scanner import NmapScannerError, NmapScanResult

SOCKET_PATH = os.environ.get("JUSTUP_NMAP_SOCKET", "/run/justup/nmap.sock")
MAX_RESPONSE_BYTES = 1_100_000
# Published by docker/nmap-worker.sh: {"state": starting|ready|restarting|failed, "detail", "restarts", "since"}
WORKER_STATUS_PATH = Path(os.environ.get("JUSTUP_NMAP_STATUS", "/run/justup-nmap.status"))


def worker_status() -> dict[str, object]:
    """Return the supervisor's worker state; ``{"state": "unknown"}`` when nobody publishes one."""
    try:
        status = json.loads(WORKER_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unknown", "detail": "", "restarts": 0}
    return status if isinstance(status, dict) and isinstance(status.get("state"), str) else {"state": "unknown"}


def _unavailable_message() -> str:
    status = worker_status()
    state, detail = status.get("state"), status.get("detail")
    if state in {"restarting", "failed", "starting"}:
        return f"Nmap scan worker is {state}" + (f": {detail}" if isinstance(detail, str) and detail else "")
    return "Nmap scan worker is unavailable"


async def request_scan(
    host: str,
    *,
    tcp_ports: list[int] | None = None,
    udp_ports: list[int] | None = None,
    protocols: set[str] | None = None,
    profile: str = "quick",
    timeout: float = 90,
    detect_services: bool = False,
) -> NmapScanResult:
    """Request a bounded scan; closing the connection cancels its child process."""
    payload = {
        "host": host,
        "tcp_ports": tcp_ports,
        "udp_ports": udp_ports,
        "protocols": sorted(protocols) if protocols is not None else None,
        "profile": profile,
        "timeout": timeout,
        "detect_services": detect_services,
    }
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH, limit=MAX_RESPONSE_BYTES)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError, OSError) as exc:
        raise NmapScannerError("unavailable", _unavailable_message()) from exc
    try:
        writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        try:
            async with asyncio.timeout(timeout + 5):
                raw = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            raise NmapScannerError("output_limit", "Nmap worker response exceeded the size limit") from exc
        except TimeoutError as exc:
            raise NmapScannerError("timeout", "Nmap scan worker timed out") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise NmapScannerError("output_limit", "Nmap worker response exceeded the size limit")
        try:
            response = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise NmapScannerError("process_failed", "Nmap worker returned invalid data") from exc
        if not isinstance(response, dict):
            raise NmapScannerError("process_failed", "Nmap worker returned invalid data")
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                raise NmapScannerError(error["code"], str(error.get("message") or "Nmap scan failed"))
            raise NmapScannerError("process_failed", "Nmap scan failed")
        return NmapScanResult.model_validate(response.get("result"))
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
