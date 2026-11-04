#!/usr/bin/env python3
#
# app/services/nmap_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Narrow Unix-socket service that alone executes privileged Nmap scans."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import stat
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.nmap_scanner import MAX_PORTS, NmapScannerError, discover_ports, scan_selected_ports, trace_route
from app.utils.tcp_ports import parse_port_list

_log = logging.getLogger(__name__)
SOCKET_PATH = Path(os.environ.get("JUSTUP_NMAP_SOCKET", "/run/justup/nmap.sock"))
MAX_REQUEST_BYTES = 8192


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    tcp_ports: list[int] | None = None
    udp_ports: list[int] | None = None
    protocols: set[str] | None = None
    profile: str = Field(default="quick", pattern="^(quick|extended)$")
    timeout: float = Field(default=90, ge=1, le=300)
    detect_services: bool = False

    @model_validator(mode="after")
    def validate_mode(self):
        selected = self.tcp_ports is not None or self.udp_ports is not None
        if selected == (self.protocols is not None):
            raise ValueError("Specify either selected ports or discovery protocols")
        if self.protocols is not None and (not self.protocols or not self.protocols <= {"tcp", "udp"}):
            raise ValueError("Invalid discovery protocols")
        if selected:
            tcp = parse_port_list(self.tcp_ports)
            udp = parse_port_list(self.udp_ports)
            if (not tcp and not udp) or len(tcp) + len(udp) > MAX_PORTS:
                raise ValueError("Invalid selected-port count")
        return self


class TracerouteRequest(BaseModel):
    """Typed traceroute request: a host and an optional TCP/UDP probe port, never free Nmap arguments."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["traceroute"]
    host: str = Field(min_length=1, max_length=255)
    probe_protocol: Literal["tcp", "udp"] | None = None
    probe_port: int | None = Field(default=None, ge=1, le=65535)
    timeout: float = Field(default=30, ge=5, le=120)

    @model_validator(mode="after")
    def validate_probe(self):
        if (self.probe_protocol is None) != (self.probe_port is None):
            raise ValueError("Probe protocol and port belong together")
        return self


class _NoReplyError(Exception):
    """Leave the request handler without an answer; the connection is still closed by its `finally`."""


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    response: dict | None = None
    scan_task: asyncio.Task | None = None
    disconnect_task: asyncio.Task | None = None
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        if raw == b"":  # availability probe: the peer connected and left without a request
            raise _NoReplyError
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Invalid request size")
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("kind") == "traceroute":
            trace = TracerouteRequest.model_validate(payload)
            request = None
            scan_task = asyncio.create_task(
                trace_route(
                    trace.host, timeout=trace.timeout, probe_protocol=trace.probe_protocol, probe_port=trace.probe_port
                )
            )
        else:
            request = WorkerRequest.model_validate_json(raw)
        if request is None:
            pass
        elif request.protocols is not None:
            scan_task = asyncio.create_task(
                discover_ports(request.host, request.protocols, timeout=request.timeout, profile=request.profile)
            )
        else:
            scan_task = asyncio.create_task(
                scan_selected_ports(
                    request.host,
                    request.tcp_ports or [],
                    request.udp_ports or [],
                    timeout=request.timeout,
                    detect_services=request.detect_services,
                )
            )
        disconnect_task = asyncio.create_task(reader.read(1))
        done, _ = await asyncio.wait({scan_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        if disconnect_task in done:
            scan_task.cancel()
            with suppress(asyncio.CancelledError):
                await scan_task
            raise _NoReplyError  # the client is gone, nothing to answer
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task
        result = scan_task.result()
        response = {"result": result.model_dump(mode="json")}
    except _NoReplyError:
        pass
    except NmapScannerError as exc:
        response = {"error": exc.error.model_dump()}
    except (ValueError, json.JSONDecodeError) as exc:
        _log.info("Rejected malformed Nmap worker request: %s", type(exc).__name__)
        response = {"error": {"code": "invalid_host", "message": "Invalid scan request"}}
    except Exception:  # noqa: BLE001 - worker boundary returns a safe error to the caller
        _log.exception("Nmap worker scan failed")
        response = {"error": {"code": "process_failed", "message": "Nmap scan failed"}}
    try:
        if response is not None:
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        if disconnect_task is not None and not disconnect_task.done():
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def _prepare_socket() -> None:
    SOCKET_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        if not stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
            raise RuntimeError("Nmap socket path is not a socket")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(SOCKET_PATH))
        except ConnectionRefusedError:
            SOCKET_PATH.unlink()
        else:
            raise RuntimeError("Nmap worker is already running")
        finally:
            probe.close()


async def _exit_with_parent(parent_pid: int) -> None:
    """Stop when the supervising web process is gone, so a killed server leaves no worker behind."""
    while os.getppid() == parent_pid:  # noqa: ASYNC110 - a parent's death has no event to wait for
        await asyncio.sleep(5)
    _log.info("Supervising process ended, stopping the Nmap worker")
    os.kill(os.getpid(), signal.SIGTERM)


async def main() -> None:
    _prepare_socket()
    parent = os.environ.get("JUSTUP_NMAP_PARENT_PID", "")
    watcher = asyncio.create_task(_exit_with_parent(int(parent))) if parent.isdigit() else None
    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH), limit=MAX_REQUEST_BYTES)
    await asyncio.to_thread(SOCKET_PATH.chmod, 0o660)
    try:
        async with server:
            await server.serve_forever()
    finally:
        if watcher is not None:
            watcher.cancel()
        await asyncio.to_thread(SOCKET_PATH.unlink, missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
