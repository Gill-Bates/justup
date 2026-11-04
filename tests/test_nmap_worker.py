#!/usr/bin/env python3
#
# tests/test_nmap_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Nmap worker socket protocol and client: request validation, errors, disconnect cancellation.

A stand-in executable replaces Nmap; the Unix socket lives in a private temporary directory.
"""

# ruff: noqa: SLF001, ARG001  # white-box access to the worker handler and scanner internals
from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import nmap_client, nmap_scanner, nmap_worker
from app.services.nmap_scanner import NmapScannerError

OK_BODY = """
sys.stdout.write(
    '<?xml version="1.0"?><nmaprun><host><status state="up"/><address addr="10.0.0.5" addrtype="ipv4"/>'
    '<ports><port protocol="udp" portid="53"><state state="open|filtered" reason="no-response"/></port></ports>'
    '</host><runstats><finished exit="success"/></runstats></nmaprun>'
)
"""


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp(prefix="jn"))  # short path: AF_UNIX limits the length
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _fake_nmap(directory: Path, body: str) -> Path:
    path = directory / "nmap"
    path.write_text(f"#!{sys.executable}\nimport os, sys, time\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestWorkerRequest:
    @pytest.mark.parametrize(
        "payload",
        [
            {"host": "10.0.0.5"},
            {"host": "10.0.0.5", "protocols": ["tcp"], "tcp_ports": [22]},
            {"host": "10.0.0.5", "protocols": []},
            {"host": "10.0.0.5", "protocols": ["icmp"]},
            {"host": "10.0.0.5", "tcp_ports": []},
            {"host": "10.0.0.5", "tcp_ports": [0]},
            {"host": "10.0.0.5", "udp_ports": [65536]},
            {"host": "10.0.0.5", "tcp_ports": list(range(1, 130))},
            {"host": "10.0.0.5", "tcp_ports": [22], "args": ["-A"]},
            {"host": "10.0.0.5", "tcp_ports": [22], "timeout": 0},
            {"host": "10.0.0.5", "tcp_ports": [22], "timeout": 301},
            {"host": "10.0.0.5", "protocols": ["tcp"], "profile": "full"},
            {"host": "", "tcp_ports": [22]},
        ],
    )
    def test_rejects_invalid_requests(self, payload):
        with pytest.raises(ValidationError):
            nmap_worker.WorkerRequest.model_validate(payload)

    def test_accepts_discovery_and_selected_port_requests(self):
        assert nmap_worker.WorkerRequest.model_validate({"host": "h", "protocols": ["tcp", "udp"]})
        assert nmap_worker.WorkerRequest.model_validate({"host": "h", "udp_ports": [53], "tcp_ports": []})


async def _serve(sock: Path):
    return await asyncio.start_unix_server(nmap_worker._handle, path=str(sock), limit=nmap_worker.MAX_REQUEST_BYTES)


async def _raw(sock: Path, data: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(data)
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=10)
    writer.close()
    return json.loads(line)


class TestWorkerProtocol:
    def test_end_to_end_selected_ports_keep_unsure_udp_state(self, sock_dir, monkeypatch):
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, OK_BODY)))
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

        async def scenario():
            server = await _serve(sock_dir / "w.sock")
            async with server:
                return await nmap_client.request_scan("10.0.0.5", tcp_ports=[], udp_ports=[53], timeout=5)

        port = asyncio.run(scenario()).hosts[0].ports[0]
        assert (port.protocol, port.port, port.state) == ("udp", 53, "open|filtered")

    def test_scan_errors_cross_the_socket_with_their_code(self, sock_dir, monkeypatch):
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, "sys.exit(3)")))
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

        async def scenario():
            server = await _serve(sock_dir / "w.sock")
            async with server:
                await nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=5)

        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scenario())
        assert exc.value.error.code == "process_failed"

    @pytest.mark.parametrize(
        "data", [b"\n", b"not json\n", b'{"host": "10.0.0.5"}\n', b'{"host": "127.0.0.1", "tcp_ports": [22]}\n']
    )
    def test_malformed_or_forbidden_requests_get_an_error_and_the_worker_lives(self, sock_dir, data, monkeypatch):
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, OK_BODY)))

        async def scenario():
            server = await _serve(sock_dir / "w.sock")
            async with server:
                first = await _raw(sock_dir / "w.sock", data)
                second = await _raw(
                    sock_dir / "w.sock", b'{"host":"10.0.0.5","tcp_ports":[],"udp_ports":[53],"timeout":5}\n'
                )
            return first, second

        first, second = asyncio.run(scenario())
        assert "error" in first and "result" not in first
        assert "result" in second

    def test_oversized_request_is_refused(self, sock_dir):
        async def scenario():
            server = await _serve(sock_dir / "w.sock")
            async with server:
                reader, writer = await asyncio.open_unix_connection(str(sock_dir / "w.sock"))
                writer.write(b"x" * (nmap_worker.MAX_REQUEST_BYTES * 2) + b"\n")
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    return {"error": {}}
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                return json.loads(line) if line else {"error": {}}

        assert "error" in asyncio.run(scenario())

    def test_client_disconnect_kills_the_nmap_process_group(self, sock_dir, monkeypatch):
        pids = sock_dir / "pids"
        body = f"""
import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')
time.sleep(60)
"""
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, body)))
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

        async def scenario():
            server = await _serve(sock_dir / "w.sock")
            async with server:
                task = asyncio.create_task(nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=30))
                for _ in range(100):
                    if pids.exists() and pids.read_text().count(" "):
                        break
                    await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                for _ in range(60):
                    if not nmap_scanner._target_locks:
                        break
                    await asyncio.sleep(0.05)
                assert nmap_scanner._target_locks == {}

        asyncio.run(scenario())
        from tests.test_nmap_scanner import _assert_gone

        for pid in map(int, pids.read_text().split()):
            _assert_gone(pid)


class TestClient:
    def test_missing_socket_is_unavailable_not_a_crash(self, sock_dir, monkeypatch):
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "absent.sock"))
        monkeypatch.setattr(nmap_client, "WORKER_STATUS_PATH", sock_dir / "status")
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=5))
        assert exc.value.error.code == "unavailable"

    def test_supervisor_status_is_reflected_in_the_message(self, sock_dir, monkeypatch):
        status = sock_dir / "status"
        status.write_text(json.dumps({"state": "restarting", "detail": "exit 1"}))
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "absent.sock"))
        monkeypatch.setattr(nmap_client, "WORKER_STATUS_PATH", status)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=5))
        assert "restarting" in exc.value.error.message

    @pytest.mark.parametrize(
        "reply",
        [
            b"garbage\n",
            b"[1]\n",
            b'{"error": "x"}\n',
            b'{"result": {"hosts": "no"}}\n',
            b'{"error": {"code": "timeout", "message": "m"}}\n',
        ],
    )
    def test_invalid_worker_answers_become_structured_errors(self, sock_dir, monkeypatch, reply):
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

        async def answer(reader, writer):
            await reader.readline()
            writer.write(reply)
            await writer.drain()
            writer.close()

        async def scenario():
            server = await asyncio.start_unix_server(answer, path=str(sock_dir / "w.sock"))
            async with server:
                await nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=5)

        with pytest.raises((NmapScannerError, ValidationError)) as exc:
            asyncio.run(scenario())
        if isinstance(exc.value, NmapScannerError):
            assert exc.value.error.code in {"process_failed", "timeout"}

    def test_silent_worker_times_out(self, sock_dir, monkeypatch):
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

        async def silent(reader, writer):
            await reader.readline()
            await reader.read()

        async def scenario():
            server = await asyncio.start_unix_server(silent, path=str(sock_dir / "w.sock"))
            async with server:
                started = time.monotonic()
                with pytest.raises(NmapScannerError) as exc:
                    await nmap_client.request_scan("10.0.0.5", tcp_ports=[22], timeout=0.2)
                assert exc.value.error.code == "timeout"
                assert time.monotonic() - started < 10

        asyncio.run(scenario())
