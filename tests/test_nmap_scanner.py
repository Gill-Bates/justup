#!/usr/bin/env python3
#
# tests/test_nmap_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Nmap adapter: XML parsing, argument construction, bounded and cancellable process handling.

No real Nmap and no network: a stand-in executable replaces the binary.
"""

# ruff: noqa: ARG002, ARG005, SLF001  # pytest fixtures used for side effects; white-box registry checks
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services import nmap_scanner as scanner
from app.services.nmap_scanner import NmapScannerError, parse_nmap_xml

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _xml(hosts: str, *, exit_status: str = "success") -> bytes:
    return (
        '<?xml version="1.0"?><!DOCTYPE nmaprun><nmaprun scanner="nmap">'
        f'{hosts}<runstats><finished exit="{exit_status}"/></runstats></nmaprun>'
    ).encode()


def _parse(data: bytes):
    return parse_nmap_xml(data, started_at=T0, completed_at=T0)


def _host(address: str, ports: str, addrtype: str = "ipv4") -> str:
    ports_el = f"<ports>{ports}</ports>" if ports is not None else ""
    return f'<host><status state="up"/><address addr="{address}" addrtype="{addrtype}"/>{ports_el}</host>'


# --- XML parsing ---------------------------------------------------------------------------------


class TestParse:
    def test_tcp_probed_service(self):
        port = (
            '<port protocol="tcp" portid="25"><state state="open" reason="syn-ack"/>'
            '<service name="smtp" product="Postfix" version="3.9" method="probed" conf="10"/></port>'
        )
        result = _parse(_xml(_host("192.0.2.1", port)))
        found = result.hosts[0].ports[0]
        assert (found.protocol, found.port, found.state, found.reason) == ("tcp", 25, "open", "syn-ack")
        assert found.service.model_dump() == {
            "name": "smtp",
            "product": "Postfix",
            "version": "3.9",
            "method": "probed",
            "confidence": 10,
        }
        assert result.complete is True

    def test_method_table_is_kept_distinct_from_probed(self):
        port = (
            '<port protocol="udp" portid="53"><state state="open"/>'
            '<service name="domain" method="table" conf="3"/></port>'
        )
        service = _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].service
        assert service.method == "table"
        assert service.product is None and service.version is None

    def test_missing_service_name_confidence_and_unknown_method(self):
        port = '<port protocol="tcp" portid="9"><state state="open"/><service method="weird"/></port>'
        service = _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].service
        assert service.name is None and service.confidence is None and service.method is None

    def test_port_without_service_element(self):
        port = '<port protocol="tcp" portid="9"><state state="closed" reason="conn-refused"/></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].service is None

    @pytest.mark.parametrize("state", ["open", "closed", "filtered", "open|filtered", "unfiltered", "closed|filtered"])
    def test_all_nmap_states_are_preserved(self, state):
        port = f'<port protocol="udp" portid="123"><state state="{state}"/></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].state == state

    def test_unrecognised_state_becomes_unknown(self):
        port = '<port protocol="tcp" portid="1"><state state="bogus"/></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].state == "unknown"

    def test_missing_state_element_becomes_unknown(self):
        port = '<port protocol="tcp" portid="1"></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].state == "unknown"

    def test_ipv4_ipv6_and_several_hosts(self):
        port = '<port protocol="tcp" portid="443"><state state="open"/></port>'
        result = _parse(_xml(_host("192.0.2.1", port) + _host("2001:db8::1", port, "ipv6")))
        assert [h.address for h in result.hosts] == ["192.0.2.1", "2001:db8::1"]

    def test_host_without_port_list_or_address(self):
        no_address = '<host><status state="up"/><address addr="aa:bb" addrtype="mac"/></host>'
        result = _parse(_xml(_host("192.0.2.1", None) + no_address))
        assert len(result.hosts) == 1 and result.hosts[0].ports == []

    def test_empty_result(self):
        result = _parse(_xml(""))
        assert result.hosts == [] and result.complete is True

    def test_incomplete_run_is_flagged(self):
        assert _parse(_xml("", exit_status="error")).complete is False
        assert _parse(b'<?xml version="1.0"?><nmaprun/>').complete is False

    @pytest.mark.parametrize("port_id", ["0", "65536", "99999", "²", "1e3", "-1"])
    def test_out_of_range_or_non_ascii_port_is_skipped_not_crashing(self, port_id):
        port = f'<port protocol="tcp" portid="{port_id}"><state state="open"/></port>'
        port += '<port protocol="tcp" portid="22"><state state="open"/></port>'
        ports = _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports
        assert [p.port for p in ports] == [22]

    def test_non_ascii_confidence_is_ignored_not_crashing(self):
        port = '<port protocol="tcp" portid="22"><state state="open"/><service name="ssh" conf="²"/></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].service.confidence is None

    def test_out_of_range_confidence_is_ignored_not_crashing(self):
        port = '<port protocol="tcp" portid="22"><state state="open"/><service name="ssh" conf="99"/></port>'
        assert _parse(_xml(_host("192.0.2.1", port))).hosts[0].ports[0].service.confidence is None

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"not xml",
            b"<nmaprun><host></nmaprun>",
            b"<html/>",
            b"\xff\xfe\x00",
            b'<?xml version="1.0"?><nmaprun>\x00</nmaprun>',
        ],
    )
    def test_invalid_xml(self, data):
        with pytest.raises(NmapScannerError) as exc:
            _parse(data)
        assert exc.value.error.code == "invalid_xml"

    def test_entities_and_external_references_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE nmaprun [<!ENTITY a "x">]><nmaprun>&a;</nmaprun>'
        external = (
            b'<?xml version="1.0"?><!DOCTYPE nmaprun [<!ENTITY a SYSTEM "file:///etc/passwd">]><nmaprun>&a;</nmaprun>'
        )
        for data in (bomb, external):
            with pytest.raises(NmapScannerError) as exc:
                _parse(data)
            assert exc.value.error.code == "invalid_xml"

    def test_oversized_document_is_refused(self):
        with pytest.raises(NmapScannerError) as exc:
            _parse(b"<nmaprun>" + b" " * (scanner.MAX_OUTPUT_BYTES + 1) + b"</nmaprun>")
        assert exc.value.error.code == "invalid_xml"


# --- process handling ----------------------------------------------------------------------------


def _fake_nmap(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "nmap"
    args_file = tmp_path / "argv.json"
    path.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" + textwrap.dedent(body)
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _argv(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / "argv.json").read_text())


OK_BODY = """
sys.stdout.write(
    '<?xml version="1.0"?><nmaprun><host><status state="up"/><address addr="10.0.0.5" addrtype="ipv4"/>'
    '<ports><port protocol="tcp" portid="22"><state state="open" reason="syn-ack"/></port></ports></host>'
    '<runstats><finished exit="success"/></runstats></nmaprun>'
)
"""


@pytest.fixture
def use(monkeypatch, tmp_path):
    def _use(body: str) -> Path:
        path = _fake_nmap(tmp_path, body)
        monkeypatch.setattr(scanner, "NMAP_BINARY", str(path))
        return path

    return _use


def _scan(**kwargs):
    kwargs.setdefault("timeout", 5)
    return asyncio.run(
        scanner.scan_selected_ports("10.0.0.5", kwargs.pop("tcp", [22]), kwargs.pop("udp", []), **kwargs)
    )


class TestArguments:
    def test_tcp_monitoring_scan_is_light_and_pinned_to_the_validated_ip(self, use, tmp_path):
        use(OK_BODY)
        result = _scan(tcp=[80, 22])
        argv = _argv(tmp_path)
        assert result.hosts[0].ports[0].port == 22
        assert argv[-3:] == ["-p", "T:22,80", "10.0.0.5"]
        assert {"-sT", "-Pn", "-n", "-oX"} <= set(argv)
        assert argv[argv.index("-oX") + 1] == "-"
        assert "-sV" not in argv and "-sU" not in argv and "--privileged" not in argv
        assert not {"-A", "-O", "--script", "--traceroute", "-6"} & set(argv)

    def test_udp_uses_privileged_raw_scan_and_never_service_detection_by_default(self, use, tmp_path):
        use(OK_BODY)
        _scan(tcp=[], udp=[161, 53])
        argv = _argv(tmp_path)
        assert "-sU" in argv and "--privileged" in argv and "-sT" not in argv and "-sV" not in argv
        assert argv[-2:] == ["U:53,161", "10.0.0.5"]

    def test_mixed_ports_and_service_detection(self, use, tmp_path):
        use(OK_BODY)
        _scan(tcp=[25], udp=[53], detect_services=True)
        argv = _argv(tmp_path)
        assert {"-sT", "-sU", "-sV", "--version-light"} <= set(argv)
        assert argv[-2:] == ["T:25,U:53", "10.0.0.5"]

    def test_ipv6_target_adds_dash_6(self, use, tmp_path, monkeypatch):
        use(OK_BODY)
        asyncio.run(scanner.scan_selected_ports("2001:db8::1", [22], [], timeout=5))
        argv = _argv(tmp_path)
        assert "-6" in argv and argv[-1] == "2001:db8::1"

    def test_hostname_is_resolved_first_and_only_the_ip_reaches_nmap(self, use, tmp_path, monkeypatch):
        use(OK_BODY)
        import ipaddress

        monkeypatch.setattr(
            scanner, "resolve_validated", lambda host, allow_private: [ipaddress.ip_address("10.9.9.9")]
        )
        asyncio.run(scanner.scan_selected_ports("example.test", [22], [], timeout=5))
        argv = _argv(tmp_path)
        assert argv[-1] == "10.9.9.9" and "example.test" not in argv

    @pytest.mark.parametrize(
        "host",
        ["10.0.0.0/24", "10.0.0.1-5", "10.0.0.1 10.0.0.2", "-oN /tmp/x", "a,b", "--script=x", "", "127.0.0.1"],
    )
    def test_no_cidr_ranges_lists_options_or_blocked_targets(self, use, host):
        use(OK_BODY)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scanner.scan_selected_ports(host, [22], [], timeout=5))
        assert exc.value.error.code == "invalid_host"

    @pytest.mark.parametrize("ports", [[0], [65536], [-1], list(range(1, 130))])
    def test_invalid_port_numbers_and_counts(self, use, ports):
        use(OK_BODY)
        with pytest.raises(NmapScannerError) as exc:
            _scan(tcp=ports)
        assert exc.value.error.code == "invalid_ports"

    def test_no_ports_at_all(self, use):
        use(OK_BODY)
        with pytest.raises(NmapScannerError) as exc:
            _scan(tcp=[], udp=[])
        assert exc.value.error.code == "invalid_ports"

    @pytest.mark.parametrize("protocols", [set(), {"icmp"}, {"tcp", "sctp"}])
    def test_discovery_rejects_unknown_protocols(self, use, protocols):
        use(OK_BODY)
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scanner.discover_ports("10.0.0.5", protocols, timeout=5))
        assert exc.value.error.code == "invalid_ports"

    def test_discovery_profile_is_limited_and_detects_services(self, use, tmp_path):
        use(OK_BODY)
        asyncio.run(scanner.discover_ports("10.0.0.5", {"tcp", "udp"}, timeout=5))
        argv = _argv(tmp_path)
        assert {"-sT", "-sU", "-sV", "--version-light", "-Pn", "-n"} <= set(argv)
        ports = argv[argv.index("-p") + 1]
        tcp, udp = ports.split(",U:")
        assert len(tcp.split(",")) < 40 and len(udp.split(",")) < len(tcp.split(","))
        assert "65535" not in ports and "1-" not in ports
        assert not {"-A", "-O", "--script", "-sC", "--traceroute"} & set(argv)


class TestProcessFailures:
    def test_missing_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scanner, "NMAP_BINARY", str(tmp_path / "absent"))
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "unavailable"

    def test_not_executable(self, monkeypatch, tmp_path):
        path = tmp_path / "nmap"
        path.write_text("x")
        monkeypatch.setattr(scanner, "NMAP_BINARY", str(path))
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "permission_denied"

    def test_missing_privileges(self, use):
        use("sys.stderr.write('You requested a scan type which requires root privileges.\\n'); sys.exit(1)")
        with pytest.raises(NmapScannerError) as exc:
            _scan(udp=[53])
        assert exc.value.error.code == "permission_denied"

    @pytest.mark.parametrize(
        ("stderr", "code"),
        [
            ("Couldn't open a raw socket. Error: Operation not permitted (1)", "permission_denied"),
            ("OPERATION NOT PERMITTED", "permission_denied"),
            ("dnet: Failed to open device: Permission denied", "permission_denied"),
            ("Some unrelated failure", "process_failed"),
        ],
    )
    def test_scan_permission_errors_are_classified_without_raw_stderr(self, use, stderr, code):
        use(f"sys.stderr.write({stderr!r}); sys.exit(1)")
        with pytest.raises(NmapScannerError) as exc:
            _scan(udp=[53])
        assert exc.value.error.code == code
        assert stderr not in exc.value.error.message

    def test_unexpected_exit_code_hides_stderr(self, use):
        use("sys.stderr.write('SECRET-RAW-DETAIL'); sys.exit(2)")
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "process_failed"
        assert "SECRET-RAW-DETAIL" not in exc.value.error.message

    def test_killed_by_signal(self, use):
        use("os.kill(os.getpid(), 9)")
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "process_failed"

    def test_exit_zero_with_garbage_output(self, use):
        use("sys.stdout.write('hello')")
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "invalid_xml"

    def test_exit_zero_with_incomplete_xml(self, use):
        use("sys.stdout.write('<nmaprun><runstats><finished exit=\"error\"/></runstats></nmaprun>')")
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "process_failed"

    def test_oversized_stdout_is_cut_off_and_the_process_stopped(self, use, tmp_path):
        pid_file = tmp_path / "pid"
        use(
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "while True:\n    sys.stdout.write('x' * 65536); sys.stdout.flush()\n"
        )
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "output_limit"
        _assert_gone(int(pid_file.read_text()))

    def test_oversized_stderr_is_cut_off(self, use):
        use("while True:\n    sys.stderr.write('x' * 65536); sys.stderr.flush()\n")
        with pytest.raises(NmapScannerError) as exc:
            _scan()
        assert exc.value.error.code == "output_limit"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers signal 0
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _assert_gone(pid: int) -> None:
    for _ in range(50):
        if not _alive(pid):
            return
        import time

        time.sleep(0.1)
    raise AssertionError(f"process {pid} is still running")


HANG_BODY = """
import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open({pids!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')
time.sleep(60)
"""


class TestTraceProcessLifecycle:
    def test_missing_binary_is_unavailable(self, tmp_path):
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scanner._run_trace([str(tmp_path / "absent")], "10.0.0.5", 1))
        assert exc.value.error.code == "unavailable"

    def test_timeout_kills_the_whole_process_group(self, use, tmp_path):
        pids = tmp_path / "trace-pids"
        path = use(HANG_BODY.format(pids=str(pids)))
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scanner._run_trace([str(path)], "10.0.0.5", 1))
        assert exc.value.error.code == "timeout"
        for pid in map(int, pids.read_text().split()):
            _assert_gone(pid)


    @pytest.mark.parametrize(
        ("stderr", "code"),
        [
            ("Couldn't open a raw socket. Error: Operation not permitted (1)", "permission_denied"),
            ("Traceroute has to be run as root", "permission_denied"),
            ("sendto in send_ip_packet_sd: Permission denied", "permission_denied"),
            ("Some unrelated failure", "process_failed"),
        ],
    )
    def test_trace_permission_errors_are_classified_without_raw_stderr(self, use, stderr, code):
        path = use(f"sys.stderr.write({stderr!r}); sys.exit(1)")
        with pytest.raises(NmapScannerError) as exc:
            asyncio.run(scanner._run_trace([str(path)], "10.0.0.5", 5))
        assert exc.value.error.code == code
        assert stderr not in exc.value.error.message


class TestTimeoutAndCancel:
    def test_timeout_kills_the_whole_process_group(self, use, tmp_path):
        pids = tmp_path / "pids"
        use(HANG_BODY.format(pids=str(pids)))
        with pytest.raises(NmapScannerError) as exc:
            _scan(timeout=1)
        assert exc.value.error.code == "timeout"
        for pid in map(int, pids.read_text().split()):
            _assert_gone(pid)

    def test_cancellation_kills_the_whole_process_group_and_releases_the_target(self, use, tmp_path):
        pids = tmp_path / "pids"
        use(HANG_BODY.format(pids=str(pids)))

        async def scenario():
            task = asyncio.create_task(scanner.scan_selected_ports("10.0.0.5", [22], [], timeout=30))
            for _ in range(100):
                if pids.exists() and pids.read_text().count(" "):
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert scanner._target_locks == {}

        asyncio.run(scenario())
        for pid in map(int, pids.read_text().split()):
            _assert_gone(pid)

    def test_already_dead_group_does_not_raise_while_stopping(self, use, tmp_path):
        use("os.setsid() if False else None\nsys.stdout.write('')")

        async def scenario():
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", start_new_session=True)
            await proc.wait()
            await scanner._stop_process(proc)

        asyncio.run(scenario())

    def test_stubborn_process_is_sigkilled(self, use, tmp_path):
        pids = tmp_path / "pids"
        use(
            "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(pids)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
        )
        with pytest.raises(NmapScannerError) as exc:
            _scan(timeout=1)
        assert exc.value.error.code == "timeout"
        _assert_gone(int(pids.read_text()))


class TestLimits:
    def test_second_scan_for_the_same_address_is_refused_as_busy(self, use):
        use("time.sleep(1.5)\n" + OK_BODY)

        async def scenario():
            first = asyncio.create_task(scanner.scan_selected_ports("10.0.0.5", [22], [], timeout=10))
            await asyncio.sleep(0.3)
            with pytest.raises(NmapScannerError) as exc:
                await scanner.scan_selected_ports("10.0.0.5", [22], [], timeout=10)
            assert exc.value.error.code == "busy"
            await first

        asyncio.run(scenario())

    def test_global_parallelism_is_bounded(self, use, tmp_path, monkeypatch):
        marker = tmp_path / "running"
        marker.mkdir()
        use(
            f"p = os.path.join({str(marker)!r}, str(os.getpid()))\nopen(p, 'w').close()\n"
            f"open(os.path.join({str(tmp_path)!r}, 'peak'), 'a').write(str(len(os.listdir({str(marker)!r}))) + '\\n')\n"
            "time.sleep(0.5)\nos.remove(p)\n" + OK_BODY
        )
        monkeypatch.setattr(scanner, "MAX_CONCURRENT_SCANS", 2)

        async def scenario():
            monkeypatch.setattr(scanner, "_scan_slots", asyncio.Semaphore(2))
            await asyncio.gather(
                *(scanner.scan_selected_ports(f"10.0.0.{n}", [22], [], timeout=10) for n in range(1, 6))
            )

        asyncio.run(scenario())
        peaks = [int(x) for x in (tmp_path / "peak").read_text().split()]
        assert len(peaks) == 5 and max(peaks) <= 2

    @pytest.mark.parametrize("timeout", [0, 0.5, 301])
    def test_timeout_range_is_enforced(self, use, timeout):
        use(OK_BODY)
        with pytest.raises(ValueError):
            _scan(timeout=timeout)
