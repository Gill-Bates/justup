#!/usr/bin/env python3
#
# tests/test_nmap_traceroute.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Traceroute through the Nmap worker: XML parser, command strategy, typed request and socket round trip.

The XML samples follow real Nmap 7.993 output captured in a local router chain (see
.agents/tasks/traceroute-nmap/progress.md); the stand-in executable replaces the privileged binary.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import shutil
import stat
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import nmap_client, nmap_scanner, nmap_worker
from app.services.nmap_scanner import NmapScannerError, parse_traceroute_xml

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)


def _xml(address: str, trace: str, *, family: str = "ipv4", exit_state: str = "success") -> bytes:
    return (
        f'<?xml version="1.0"?><nmaprun><host><status state="up"/><address addr="{address}" addrtype="{family}"/>'
        f'{trace}</host><runstats><finished exit="{exit_state}"/></runstats></nmaprun>'
    ).encode()


GAP_TRACE = (
    '<trace port="443" proto="tcp"><hop ttl="1" ipaddr="172.17.0.1" rtt="0.25"/>'
    '<hop ttl="3" ipaddr="10.10.0.1" rtt="10.00" host="edge.example.net"/>'
    '<hop ttl="6" ipaddr="1.1.1.1" rtt="11.08"/></trace>'
)


def _parse(data: bytes, destination: str = "1.1.1.1"):
    return parse_traceroute_xml(data, destination_ip=destination, started_at=T0, completed_at=T1)


class TestParser:
    def test_missing_ttls_become_explicit_unknown_hops(self):
        route = _parse(_xml("1.1.1.1", GAP_TRACE))
        assert [(h.hop_number, h.status) for h in route.hops] == [
            (1, "ok"), (2, "no_reply"), (3, "ok"), (4, "no_reply"), (5, "no_reply"), (6, "ok"),
        ]  # fmt: skip
        silent = route.hops[1]
        assert (silent.ip, silent.rtt_ms, silent.hostname) == (None, None, None)
        assert (route.probe_protocol, route.probe_port, route.duration_ms) == ("tcp", 443, 2000)
        assert route.destination_reached and route.complete and route.address_family == "ipv4"
        assert route.hops[2].hostname == "edge.example.net"

    def test_destination_not_reached_when_last_answer_is_a_router(self):
        trace = '<trace proto="icmp"><hop ttl="1" ipaddr="10.77.1.2" rtt="0.3"/></trace>'
        route = _parse(_xml("1.1.1.1", trace))
        assert not route.destination_reached and route.probe_protocol == "icmp"

    def test_ipv6_trace_without_proto_attribute_keeps_probe_unknown(self):
        trace = '<trace><hop ttl="1" ipaddr="fd77:1::2" rtt="0.2"/><hop ttl="2" ipaddr="FD77:3::2" rtt="0.4"/></trace>'
        route = _parse(_xml("fd77:3::2", trace, family="ipv6"), "fd77:3::2")
        assert route.address_family == "ipv6" and route.probe_protocol is None
        assert route.destination_reached and route.hops[1].ip == "fd77:3::2"

    @pytest.mark.parametrize("rtt", ["nan", "inf", "-1", "abc", "999999", ""])
    def test_invalid_rtt_stays_unknown_instead_of_being_invented(self, rtt):
        trace = f'<trace><hop ttl="1" ipaddr="1.1.1.1" rtt="{rtt}"/></trace>'
        hop = _parse(_xml("1.1.1.1", trace)).hops[0]
        assert hop.status == "ok" and hop.rtt_ms is None

    @pytest.mark.parametrize(
        "hops",
        [
            '<hop ttl="2" ipaddr="10.0.0.1"/><hop ttl="2" ipaddr="10.0.0.2"/>',
            '<hop ttl="3" ipaddr="10.0.0.1"/><hop ttl="2" ipaddr="10.0.0.2"/>',
            '<hop ttl="0" ipaddr="10.0.0.1"/>',
            '<hop ttl="x" ipaddr="10.0.0.1"/>',
            '<hop ttl="65" ipaddr="10.0.0.1"/>',
            '<hop ttl="1" ipaddr="not-an-ip"/>',
            '<hop ttl="1" ipaddr="fd00::1"/>',
        ],
    )
    def test_invalid_hops_are_rejected(self, hops):
        with pytest.raises(NmapScannerError) as exc:
            _parse(_xml("1.1.1.1", f"<trace>{hops}</trace>"))
        assert exc.value.error.code == "invalid_xml"

    @pytest.mark.parametrize("trace", ["", "<trace/>"])
    def test_missing_or_empty_trace_is_its_own_error(self, trace):
        with pytest.raises(NmapScannerError) as exc:
            _parse(_xml("1.1.1.1", trace))
        assert exc.value.error.code == "no_trace"

    def test_other_host_in_output_is_not_taken_for_the_destination(self):
        with pytest.raises(NmapScannerError) as exc:
            _parse(_xml("9.9.9.9", GAP_TRACE))
        assert exc.value.error.code == "no_trace"

    @pytest.mark.parametrize(
        "data",
        [
            b"<nmaprun",
            b"\x00",
            b"<other/>",
            b'<!DOCTYPE x [<!ENTITY a "b">]><nmaprun>&a;</nmaprun>',
            b"<nmaprun>" + b"x" * (nmap_scanner.MAX_OUTPUT_BYTES + 1) + b"</nmaprun>",
        ],
    )
    def test_broken_hostile_or_oversized_xml_is_rejected(self, data):
        with pytest.raises(NmapScannerError) as exc:
            _parse(data)
        assert exc.value.error.code == "invalid_xml"


class TestCommand:
    def test_discovery_attempt_is_a_pure_host_discovery_and_never_connect_scan(self):
        command = nmap_scanner._trace_command("10.0.0.5", budget=10, probe=None)
        assert "-sn" in command and "--traceroute" in command and "-p" not in command
        assert "-sT" not in command and command[-1] == "10.0.0.5"

    @pytest.mark.parametrize(("protocol", "flag"), [("tcp", "-sS"), ("udp", "-sU")])
    def test_fallback_attempt_probes_exactly_one_port_without_connect_scan(self, protocol, flag):
        command = nmap_scanner._trace_command("fd00::5", budget=10, probe=(protocol, 53))
        assert flag in command and "-Pn" in command and "-sT" not in command and "-sn" not in command
        assert command[command.index("-p") + 1] == "53" and "-6" in command
        assert not {"-A", "-sV", "-sC", "--script"} & set(command)


class TestRequest:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "traceroute", "host": "h", "args": ["-A"]},
            {"kind": "traceroute", "host": "h", "probe_port": 443},
            {"kind": "traceroute", "host": "h", "probe_protocol": "tcp"},
            {"kind": "traceroute", "host": "h", "probe_protocol": "icmp", "probe_port": 1},
            {"kind": "traceroute", "host": "h", "probe_protocol": "tcp", "probe_port": 0},
            {"kind": "traceroute", "host": "h", "timeout": 4},
            {"kind": "traceroute", "host": "h", "timeout": 121},
            {"kind": "traceroute", "host": ""},
            {"kind": "scan", "host": "h"},
        ],
    )
    def test_rejects_invalid_requests(self, payload):
        with pytest.raises(ValidationError):
            nmap_worker.TracerouteRequest.model_validate(payload)

    def test_scan_requests_still_reject_the_traceroute_kind(self):
        with pytest.raises(ValidationError):
            nmap_worker.WorkerRequest.model_validate({"kind": "traceroute", "host": "h", "tcp_ports": [22]})


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp(prefix="jt"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _fake_nmap(directory: Path, body: str) -> Path:
    path = directory / "nmap"
    path.write_text(f"#!{sys.executable}\nimport os, sys, time\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


XML_NO_TRACE = (
    'sys.stdout.write(\'<nmaprun><host><status state="down"/><address addr="10.0.0.5" addrtype="ipv4"/></host>'
    '<runstats><finished exit="success"/></runstats></nmaprun>\')'
)
XML_TRACE = (
    'sys.stdout.write(\'<nmaprun><host><status state="up"/><address addr="10.0.0.5" addrtype="ipv4"/>'
    '<trace port="443" proto="tcp"><hop ttl="1" ipaddr="10.0.0.1" rtt="0.5"/><hop ttl="3" ipaddr="10.0.0.5" rtt="2.5"/>'
    '</trace></host><runstats><finished exit="success"/></runstats></nmaprun>\')'
)


async def _roundtrip(sock_dir: Path, **kwargs):
    sock = sock_dir / "w.sock"
    server = await asyncio.start_unix_server(nmap_worker._handle, path=str(sock), limit=nmap_worker.MAX_REQUEST_BYTES)
    async with server:
        return await nmap_client.request_traceroute("10.0.0.5", **kwargs)


class TestWorkerRoundTrip:
    def _setup(self, sock_dir, monkeypatch, body):
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, body)))
        monkeypatch.setattr(nmap_client, "SOCKET_PATH", str(sock_dir / "w.sock"))

    def test_trace_crosses_the_socket_and_runs_discovery_only_once(self, sock_dir, monkeypatch):
        self._setup(
            sock_dir, monkeypatch, f"open({str(sock_dir / 'argv')!r}, 'a').write(' '.join(sys.argv)+'\\n')\n{XML_TRACE}"
        )
        route = asyncio.run(_roundtrip(sock_dir, timeout=10))
        assert [h.status for h in route.hops] == ["ok", "no_reply", "ok"] and route.destination_reached
        calls = (sock_dir / "argv").read_text().splitlines()
        assert len(calls) == 1 and "-sn" in calls[0] and "--traceroute" in calls[0] and "-sT" not in calls[0]

    def test_bounded_second_attempt_uses_the_requested_probe_port_only_without_a_first_trace(
        self, sock_dir, monkeypatch
    ):
        body = f"if '-sn' in sys.argv:\n    {XML_NO_TRACE}\nelse:\n    {XML_TRACE}"
        self._setup(sock_dir, monkeypatch, body)
        route = asyncio.run(_roundtrip(sock_dir, probe_protocol="udp", probe_port=53, timeout=10))
        assert route.destination_reached

    def test_two_attempts_without_trace_report_no_trace(self, sock_dir, monkeypatch):
        self._setup(sock_dir, monkeypatch, XML_NO_TRACE)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(_roundtrip(sock_dir, timeout=10))
        assert exc.value.error.code == "no_trace"

    def test_missing_privilege_is_reported_as_permission_denied(self, sock_dir, monkeypatch):
        self._setup(
            sock_dir, monkeypatch, "sys.stderr.write('Traceroute has to be run as root QUITTING!')\nsys.exit(1)"
        )
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(_roundtrip(sock_dir, timeout=10))
        assert exc.value.error.code == "permission_denied"

    def test_hung_nmap_is_killed_at_the_time_limit(self, sock_dir, monkeypatch):
        self._setup(sock_dir, monkeypatch, "time.sleep(60)")
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(_roundtrip(sock_dir, timeout=5))
        assert exc.value.error.code == "timeout"

    def test_loopback_destinations_are_not_traced(self, sock_dir, monkeypatch):
        self._setup(sock_dir, monkeypatch, XML_TRACE)

        async def scenario():
            sock = sock_dir / "w.sock"
            server = await asyncio.start_unix_server(
                nmap_worker._handle, path=str(sock), limit=nmap_worker.MAX_REQUEST_BYTES
            )
            async with server:
                await nmap_client.request_traceroute("127.0.0.1", timeout=5)

        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scenario())
        assert exc.value.error.code == "invalid_host"


# --- Review fixes: probe choice, shared deadline, DNS bound, global Nmap cap, request type, route model -----

_LOG_ARGV = "open({log!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"


def _trace_for(sock_dir, monkeypatch, body: str) -> Path:
    log = sock_dir / "argv"
    monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, _LOG_ARGV.format(log=str(log)) + body)))
    return log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


class TestProbeChoiceAndDeadline:
    @pytest.mark.parametrize(("protocol", "flag"), [("udp", "-sU"), ("tcp", "-sS")])
    def test_requested_probe_runs_first_and_alone(self, sock_dir, monkeypatch, protocol, flag):
        log = _trace_for(sock_dir, monkeypatch, XML_TRACE)
        asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=10, probe_protocol=protocol, probe_port=53))
        calls = _calls(log)
        assert len(calls) == 1 and flag in calls[0].split() and "-sn" not in calls[0].split()

    def test_requested_probe_is_not_widened_by_a_second_attempt(self, sock_dir, monkeypatch):
        log = _trace_for(sock_dir, monkeypatch, XML_NO_TRACE)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=10, probe_protocol="udp", probe_port=53))
        assert exc.value.error.code == "no_trace" and len(_calls(log)) == 1

    def test_default_trace_starts_with_discovery(self, sock_dir, monkeypatch):
        log = _trace_for(sock_dir, monkeypatch, XML_TRACE)
        asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=10))
        assert "-sn" in _calls(log)[0].split()

    def test_timeout_of_discovery_falls_back_once_within_one_deadline(self, sock_dir, monkeypatch):
        log = _trace_for(sock_dir, monkeypatch, f"if '-sn' in sys.argv:\n    time.sleep(60)\nelse:\n    {XML_TRACE}")
        started = time.monotonic()
        route = asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=12, probe_port=443, probe_protocol=None))
        calls = _calls(log)
        assert route.destination_reached and len(calls) == 2
        assert "-sn" in calls[0].split() and "-sS" in calls[1].split()
        assert time.monotonic() - started < 12 + 1

    def test_two_timeouts_stay_inside_the_deadline_and_never_make_a_third_attempt(self, sock_dir, monkeypatch):
        log = _trace_for(sock_dir, monkeypatch, "time.sleep(60)")
        started = time.monotonic()
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=12))
        assert exc.value.error.code == "timeout" and len(_calls(log)) == 2
        assert time.monotonic() - started < 12 + 1

    def test_no_fallback_when_too_little_budget_is_left(self, sock_dir, monkeypatch):
        log = _trace_for(sock_dir, monkeypatch, "time.sleep(60)")
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=5))
        assert exc.value.error.code == "timeout" and len(_calls(log)) == 1

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ("sys.stderr.write('Traceroute has to be run as root')\nsys.exit(1)", "permission_denied"),
            ("sys.stderr.write('boom')\nsys.exit(2)", "process_failed"),
            ("sys.stdout.write('<html/>')", "invalid_xml"),
        ],
    )
    def test_permission_and_internal_errors_are_not_masked_by_a_second_attempt(self, sock_dir, monkeypatch, body, code):
        log = _trace_for(sock_dir, monkeypatch, body)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_scanner.trace_route("10.0.0.5", timeout=10))
        assert exc.value.error.code == code and len(_calls(log)) == 1


class TestDnsBound:
    def test_slow_dns_is_bounded_and_reported_as_timeout(self, monkeypatch):
        monkeypatch.setattr(nmap_scanner, "resolve_validated", lambda *a, **k: time.sleep(2) or [])
        monkeypatch.setattr(nmap_scanner, "DNS_TIMEOUT", 0.3, raising=False)
        started = time.monotonic()
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(nmap_scanner.trace_route("slow.example.net", timeout=10))
        assert exc.value.error.code == "timeout" and time.monotonic() - started < 1.9

    def test_concurrent_resolutions_are_capped_even_after_a_caller_gave_up(self, monkeypatch):
        state = {"now": 0, "peak": 0}

        def slow(*_a, **_k):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.4)
            state["now"] -= 1
            return []

        monkeypatch.setattr(nmap_scanner, "resolve_validated", slow)
        monkeypatch.setattr(nmap_scanner, "DNS_TIMEOUT", 0.1, raising=False)

        async def scenario():
            monkeypatch.setattr(nmap_scanner, "_resolve_slots", asyncio.Semaphore(2), raising=False)
            monkeypatch.setattr(nmap_scanner, "MAX_CONCURRENT_RESOLUTIONS", 2, raising=False)
            await asyncio.gather(
                *(nmap_scanner._resolve(f"h{n}.example.net") for n in range(6)), return_exceptions=True
            )

        asyncio.run(scenario())
        assert state["peak"] <= 2


_CONCURRENCY_BODY = """
run = os.path.join({root!r}, 'run')
os.makedirs(run, exist_ok=True)
mark = os.path.join(run, str(os.getpid()))
open(mark, 'w').close()
open(os.path.join({root!r}, 'peak'), 'a').write(str(len(os.listdir(run))) + '\\n')
time.sleep(0.8)
os.remove(mark)
addr = sys.argv[-1]
if '--traceroute' in sys.argv:
    sys.stdout.write('<nmaprun><host><status state="up"/><address addr="%s" addrtype="ipv4"/>'
                     '<trace><hop ttl="1" ipaddr="10.0.0.1" rtt="0.5"/><hop ttl="2" ipaddr="%s" rtt="1.5"/></trace>'
                     '</host><runstats><finished exit="success"/></runstats></nmaprun>' % (addr, addr))
else:
    sys.stdout.write('<nmaprun><host><status state="up"/><address addr="%s" addrtype="ipv4"/><ports/></host>'
                     '<runstats><finished exit="success"/></runstats></nmaprun>' % addr)
"""


class TestGlobalNmapCap:
    def _setup(self, sock_dir, monkeypatch):
        body = _CONCURRENCY_BODY.format(root=str(sock_dir))
        monkeypatch.setattr(nmap_scanner, "NMAP_BINARY", str(_fake_nmap(sock_dir, body)))

    @staticmethod
    def _peak(sock_dir) -> int:
        return max(int(x) for x in (sock_dir / "peak").read_text().split())

    @staticmethod
    def _fresh_limits(monkeypatch):
        monkeypatch.setattr(nmap_scanner, "_scan_slots", asyncio.Semaphore(nmap_scanner.MAX_CONCURRENT_SCANS))
        monkeypatch.setattr(nmap_scanner, "_trace_slots", asyncio.Semaphore(nmap_scanner.MAX_CONCURRENT_TRACEROUTES))
        monkeypatch.setattr(
            nmap_scanner, "_nmap_slots", asyncio.Semaphore(nmap_scanner.MAX_CONCURRENT_NMAP), raising=False
        )

    def test_scans_and_traces_share_one_process_cap_and_neither_kind_is_starved(self, sock_dir, monkeypatch):
        self._setup(sock_dir, monkeypatch)
        assert nmap_scanner.MAX_CONCURRENT_NMAP < (
            nmap_scanner.MAX_CONCURRENT_SCANS + nmap_scanner.MAX_CONCURRENT_TRACEROUTES
        )

        async def scenario():
            self._fresh_limits(monkeypatch)
            scans = [
                asyncio.create_task(nmap_scanner.scan_selected_ports(f"10.0.1.{n}", [22], [], timeout=10))
                for n in (1, 2)
            ]
            await asyncio.sleep(0.3)
            traces = await asyncio.gather(
                *(nmap_scanner.trace_route(f"10.0.2.{n}", timeout=10) for n in (1, 2)), return_exceptions=True
            )
            return await asyncio.gather(*scans, return_exceptions=True), traces

        scans, traces = asyncio.run(scenario())
        assert self._peak(sock_dir) <= nmap_scanner.MAX_CONCURRENT_NMAP
        assert all(not isinstance(item, BaseException) for item in scans)
        assert sum(isinstance(item, nmap_scanner.Route) for item in traces) >= 1

    def test_busy_traces_cannot_starve_a_scan(self, sock_dir, monkeypatch):
        self._setup(sock_dir, monkeypatch)

        async def scenario():
            self._fresh_limits(monkeypatch)
            traces = [asyncio.create_task(nmap_scanner.trace_route(f"10.0.2.{n}", timeout=10)) for n in (1, 2)]
            await asyncio.sleep(0.3)
            started = time.monotonic()
            scan = await nmap_scanner.scan_selected_ports("10.0.1.1", [22], [], timeout=10)
            waited = time.monotonic() - started
            await asyncio.gather(*traces)
            return scan, waited

        _scan, waited = asyncio.run(scenario())
        assert waited < 0.7 + 0.5 and self._peak(sock_dir) <= nmap_scanner.MAX_CONCURRENT_NMAP


class TestRequestType:
    def test_client_worker_and_scanner_offer_the_same_probe_protocols(self):
        import typing

        def args(func_or_model, name):
            hints = typing.get_type_hints(func_or_model)
            return set(typing.get_args(hints[name]) or ()) - {type(None)}

        client = args(nmap_client.request_traceroute, "probe_protocol")
        scanner = args(nmap_scanner.trace_route, "probe_protocol")
        worker = {
            arg
            for arg in typing.get_args(nmap_worker.TracerouteRequest.model_fields["probe_protocol"].annotation)
            if arg is not type(None)
        }
        assert client == scanner == worker == {"tcp", "udp"}

    def test_route_result_may_still_report_an_icmp_probe(self):
        route = _parse(_xml("1.1.1.1", '<trace proto="icmp"><hop ttl="1" ipaddr="1.1.1.1" rtt="1.0"/></trace>'))
        assert route.probe_protocol == "icmp" and route.probe_port is None


def _route_kwargs(**over):
    base = {
        "destination_ip": "10.0.0.5",
        "address_family": "ipv4",
        "probe_protocol": "tcp",
        "probe_port": 443,
        "started_at": T0,
        "completed_at": T1,
        "duration_ms": 2000,
        "destination_reached": True,
        "complete": True,
        "hops": [
            {"hop_number": 1, "ip": "10.0.0.1", "rtt_ms": 0.5, "status": "ok"},
            {"hop_number": 2, "status": "no_reply"},
            {"hop_number": 3, "ip": "10.0.0.5", "rtt_ms": 2.5, "status": "ok"},
        ],
    }
    base.update(over)
    return base


class TestRouteModel:
    def test_consistent_route_is_accepted(self):
        assert nmap_scanner.Route(**_route_kwargs()).destination_reached

    @pytest.mark.parametrize(
        "over",
        [
            {"destination_ip": "not-an-ip"},
            {"address_family": "ipv6"},
            {"hops": [{"hop_number": 1, "ip": "999.1.1.1", "status": "ok"}], "destination_reached": False},
            {"hops": [{"hop_number": 1, "ip": "fd00::1", "status": "ok"}], "destination_reached": False},
            {
                "hops": [
                    {"hop_number": 2, "ip": "10.0.0.1", "status": "ok"},
                    {"hop_number": 2, "ip": "10.0.0.5", "status": "ok"},
                ]
            },
            {
                "hops": [
                    {"hop_number": 3, "ip": "10.0.0.1", "status": "ok"},
                    {"hop_number": 1, "ip": "10.0.0.5", "status": "ok"},
                ]
            },
            {"completed_at": datetime(2025, 12, 31, tzinfo=UTC)},
            {"duration_ms": 5},
            {"probe_protocol": "icmp", "probe_port": 443},
            {"probe_protocol": None, "probe_port": 443},
            {"probe_protocol": "udp", "probe_port": None},
            {"destination_reached": True, "hops": [{"hop_number": 1, "ip": "10.0.0.1", "status": "ok"}]},
        ],
    )
    def test_inconsistent_routes_are_rejected(self, over):
        with pytest.raises(ValidationError):
            nmap_scanner.Route(**_route_kwargs(**over))

    def test_parser_drops_a_port_without_a_port_protocol(self):
        route = _parse(_xml("1.1.1.1", '<trace port="443"><hop ttl="1" ipaddr="1.1.1.1" rtt="1.0"/></trace>'))
        assert route.probe_protocol is None and route.probe_port is None
