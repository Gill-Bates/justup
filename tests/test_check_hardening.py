#!/usr/bin/env python3
#
# tests/test_check_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Host contract (FR-06) and subprocess argument hygiene (NFR-07)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import subprocess
import unittest
from unittest import mock

import httpx
import pytest

from app.models.targets import TargetCreate, TargetUpdate, validate_host
from app.monitor import checks
from tests import _support  # noqa: F401 - imported for its sys.path side effect


class HostValidationTests(unittest.TestCase):
    def test_accepts_hostnames_and_ip_literals(self):
        for value in ("example.com", "a-b.example.org.", "localhost", "10.0.0.1", "2001:db8::1"):
            with self.subTest(value=value):
                self.assertEqual(validate_host(value), value)

    def test_rejects_free_text_and_option_like_hosts(self):
        for value in ("-oProxyCommand=x", "exa mple.com", "http://example.com", "a..b", "-a.example.com", "x;y"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_host(value)

    def test_models_apply_the_contract(self):
        with self.assertRaises(ValueError):
            TargetCreate(name="t", host="not a host")
        with self.assertRaises(ValueError):
            TargetUpdate(ping_host="-c")
        self.assertEqual(TargetCreate(name="t", host="example.com").host, "example.com")


class PingInvocationTests(unittest.TestCase):
    def test_ping_ends_options_before_host_and_has_hard_timeout(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="time=1.5 ms", stderr="")

        with mock.patch("subprocess.run", fake_run):
            result = asyncio.run(checks.ping("192.0.2.1", timeout_seconds=3, skip_ssrf_check=True))
        self.assertTrue(result.ok)
        self.assertEqual(seen["cmd"][-2:], ["--", "192.0.2.1"])
        self.assertIsNotNone(seen["kwargs"].get("timeout"))

    def test_hard_timeout_is_reported_as_timeout(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with mock.patch("subprocess.run", fake_run):
            result = asyncio.run(checks.ping("192.0.2.1", timeout_seconds=1, skip_ssrf_check=True))
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "timeout")


# --- http_head: connections are bound to the addresses that passed the SSRF check -------------------------


def _answers(*per_call: str):
    """getaddrinfo stand-in: the n-th call returns the n-th address, the last one repeats."""
    calls = {"n": 0}

    def fake(host, port, family=0, type_=0, *args, **kwargs):  # noqa: ARG001
        address = per_call[min(calls["n"], len(per_call) - 1)]
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    return fake


def _run_http_head(url: str, getaddrinfo, *, public_answer: str | None = None, **kwargs):
    """Run http_head with name resolution mocked and every outgoing request recorded (no network)."""
    seen: list[tuple[str, dict]] = []
    public_dns = mock.AsyncMock(return_value=public_answer)

    @contextlib.asynccontextmanager
    async def fake_stream(self, method, url, **kw):  # noqa: ARG001
        seen.append((str(url), kw))
        raise httpx.ConnectError("refused")
        yield  # pragma: no cover - makes this an async generator

    with (
        mock.patch("socket.getaddrinfo", getaddrinfo),
        mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", public_dns),
        mock.patch.object(httpx.AsyncClient, "stream", fake_stream),
    ):
        result = asyncio.run(checks.http_head(url, **kwargs))
    return result, seen


def test_http_head_returns_after_the_headers_and_never_reads_an_endless_body():
    """The monitor needs status and headers only; a huge or endless body must not hold the check."""
    requests: list[str] = []

    async def handle(reader, writer):
        requests.append((await reader.readuntil(b"\r\n\r\n")).split(b" ", 1)[0].decode())
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            while True:
                writer.write(b"400\r\n" + b"x" * 1024 + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def scenario():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await asyncio.wait_for(
                checks.http_head(
                    f"http://127.0.0.1:{port}/", timeout_seconds=5, skip_ssrf_check=True, prefer_head=False
                ),
                timeout=3,
            )
        finally:
            server.close()

    with mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value=None)):
        result = asyncio.run(scenario())
    assert result.ok and result.value == {"status": 200}
    assert requests == ["GET"]


@pytest.mark.parametrize("public_answer", [None, "93.184.216.35"])
@pytest.mark.parametrize("allowed_lookups", [1, 2, 3])
def test_http_head_never_connects_to_name_or_address_that_changed_after_the_check(allowed_lookups, public_answer):
    # The name resolves to a public address for the first N lookups and to loopback afterwards (DNS rebinding).
    answers = ["93.184.216.34"] * allowed_lookups + ["127.0.0.1"]
    result, seen = _run_http_head("https://rebind.example/", _answers(*answers), public_answer=public_answer)
    assert not result.ok
    for url, kw in seen:
        host = httpx.URL(url).host
        assert not ipaddress.ip_address(host).is_loopback, url  # literal IP only: never the name, never loopback
        assert kw["headers"]["Host"] == "rebind.example"
        assert kw["extensions"] == {"sni_hostname": "rebind.example"}


def test_http_head_pins_every_validated_address_and_keeps_host_header():
    result, seen = _run_http_head("http://monitor.example:8080/x", _answers("93.184.216.34"))
    assert not result.ok
    assert [httpx.URL(u).host for u, _ in seen] == ["93.184.216.34"] * len(seen) and seen
    assert all(kw["headers"]["Host"] == "monitor.example:8080" and not kw.get("extensions") for _, kw in seen)


@pytest.mark.parametrize(
    ("hostname", "port", "scheme", "expected"),
    [
        ("93.184.216.34", None, "http", "93.184.216.34"),
        ("93.184.216.34", 80, "http", "93.184.216.34"),
        ("93.184.216.34", 8080, "http", "93.184.216.34:8080"),
        ("93.184.216.34", 443, "https", "93.184.216.34"),
        ("93.184.216.34", 8443, "https", "93.184.216.34:8443"),
        ("monitor.example", None, "https", "monitor.example"),
        ("monitor.example", 443, "https", "monitor.example"),
        ("monitor.example", 443, "http", "monitor.example:443"),
        ("monitor.example", 8443, "https", "monitor.example:8443"),
        ("2606:4700:4700::1111", None, "https", "[2606:4700:4700::1111]"),
        ("2606:4700:4700::1111", 443, "https", "[2606:4700:4700::1111]"),
        ("2606:4700:4700::1111", 80, "http", "[2606:4700:4700::1111]"),
        ("2606:4700:4700::1111", 8443, "https", "[2606:4700:4700::1111]:8443"),
        ("fe80::1%eth0", 8080, "http", "[fe80::1]:8080"),
    ],
)
def test_host_header_brackets_ipv6_and_omits_the_default_port(hostname, port, scheme, expected):
    assert checks._host_header(hostname, port, scheme) == expected  # noqa: SLF001 - unit test of the builder


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://[2606:4700:4700::1111]/", "[2606:4700:4700::1111]"),
        ("https://[2606:4700:4700::1111]:8443/x", "[2606:4700:4700::1111]:8443"),
    ],
)
def test_http_head_sends_a_bracketed_host_header_for_ipv6_literals(url, expected):
    result, seen = _run_http_head(url, _answers("93.184.216.34"))
    assert not result.ok
    assert seen
    assert all(kw["headers"]["Host"] == expected for _, kw in seen)


def test_http_head_does_not_send_ip_literals_to_the_public_dns_resolvers():
    with mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock()) as public_dns:
        result, seen = _run_http_head("http://93.184.216.34/", _answers("93.184.216.34"))
    public_dns.assert_not_called()
    assert not result.ok
    assert [httpx.URL(url).host for url, _ in seen] == ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://169.254.169.254/latest/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_http_head_blocks_loopback_and_metadata_notations_without_connecting(url):
    result, seen = _run_http_head(url, _answers("127.0.0.1"))
    assert not result.ok
    assert seen == []


# --- Overall per-check deadline ---------------------------------------------------------------------------------


def _block_until(release):
    def _blocked(*_args, **_kwargs):
        release.wait(10)
        return True

    return _blocked


@pytest.mark.parametrize("name", ["ping", "http_head", "cert_expiration", "tcp_connect"])
def test_blocking_resolver_returns_timeout_result_and_next_call_recovers(name, monkeypatch):
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(checks, "CHECK_DEADLINE_FACTOR", 0.0)
    monkeypatch.setattr(checks, "CHECK_DEADLINE_MARGIN_SECONDS", 0.3)
    monkeypatch.setattr(checks, "_is_host_blocked", _block_until(release))
    args = {
        "ping": ("example.com",),
        "http_head": ("https://example.com/",),
        "cert_expiration": ("example.com", 443),
        "tcp_connect": ("example.com", 80),
    }[name]
    func = getattr(checks, name)

    started = time.monotonic()
    try:
        result = asyncio.run(func(*args))
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert result.ok is False
    assert "timeout" in result.detail
    assert checks.is_retryable_detail(result.detail)

    # Recovery: the next call is not affected by the abandoned worker thread.
    monkeypatch.setattr(checks, "_is_host_blocked", lambda _host: True)
    again = asyncio.run(func(*args))
    assert again.ok is False
    assert "timeout" not in (again.detail or "")


def test_deadline_does_not_mask_inner_timeout_errors():
    @checks._with_deadline  # noqa: SLF001 - the decorator is the unit under test
    async def fn(timeout_seconds: float = 1.0):
        raise TimeoutError(f"inner {timeout_seconds}")

    with pytest.raises(TimeoutError):
        asyncio.run(fn())


def test_public_dns_resolver_runs_on_the_dns_executor(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    names: list[tuple[str, str]] = []

    class Recording(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            names.append((self._thread_name_prefix, fn.__name__))
            return super().submit(fn, *args, **kwargs)

    answer = mock.Mock(address="192.0.2.9")
    resolver = mock.Mock()
    resolver.resolve.return_value = [answer]
    dns_pool, check_pool = Recording(1, thread_name_prefix="dns"), Recording(1, thread_name_prefix="chk")
    monkeypatch.setattr(checks, "_DNS_EXECUTOR", dns_pool)
    monkeypatch.setattr(checks, "_EXECUTOR", check_pool)
    try:
        with mock.patch("dns.resolver.Resolver", return_value=resolver):
            result = asyncio.run(
                checks._resolve_host_via_public_dns_first_answer("dns-executor.example")  # noqa: SLF001 - executor boundary under test
            )
    finally:
        dns_pool.shutdown()
        check_pool.shutdown()
    assert result == "192.0.2.9"
    assert names and {prefix for prefix, _ in names} == {"dns"}


# ── TCP ports of one target: one validated resolution, bounded parallel connects ──


def _stub_resolution(monkeypatch, answer: str | None, counter: list[str]):
    async def first_answer(host, **_kwargs):
        counter.append(host)
        return answer

    async def not_blocked(_host):
        return False

    monkeypatch.setattr(checks, "_resolve_host_via_public_dns_first_answer", first_answer)
    monkeypatch.setattr(checks, "_is_host_blocked_async", not_blocked)


def test_tcp_ports_resolve_once_and_connect_to_the_pinned_ip(monkeypatch):
    import time

    lookups: list[str] = []
    _stub_resolution(monkeypatch, "203.0.113.7", lookups)
    seen: list[tuple[str, int]] = []

    def fake_connect(address, **_kwargs):
        seen.append(address)
        time.sleep(0.05 * (address[1] % 10))  # distinct per-port latency, wide steps against scheduler jitter
        return contextlib.nullcontext()

    monkeypatch.setattr(checks.socket, "create_connection", fake_connect)
    ports = [1, 2, 3, 4, 5]
    results = asyncio.run(checks.tcp_connect_ports("mon.example", ports))

    assert lookups == ["mon.example"]  # one lookup for five ports
    assert sorted(seen) == [("203.0.113.7", p) for p in ports]  # nothing re-resolves after the check
    assert all(r.ok for r in results)
    latencies = [r.latency_ms for r in results]
    assert latencies == sorted(latencies) and latencies[-1] - latencies[0] > 50  # measured per port, in port order


@pytest.mark.parametrize("answer", ["127.0.0.1", "::1", "169.254.169.254"])
def test_tcp_ports_of_a_blocked_resolution_are_all_blocked_without_connecting(monkeypatch, answer):
    lookups: list[str] = []
    _stub_resolution(monkeypatch, answer, lookups)
    connect = mock.Mock()
    monkeypatch.setattr(checks.socket, "create_connection", connect)

    results = asyncio.run(checks.tcp_connect_ports("rebind.example", [22, 80, 443]))

    assert [r.detail for r in results] == ["blocked host"] * 3 and not any(r.ok for r in results)
    assert lookups == ["rebind.example"] and not connect.called


def test_tcp_ports_with_unvalidated_fallback_answer_are_blocked(monkeypatch):
    lookups: list[str] = []
    _stub_resolution(monkeypatch, None, lookups)  # public DNS has no answer: the pinned fallback decides
    monkeypatch.setattr(checks, "resolve_validated", lambda *_a, **_k: None)  # mixed records with a blocked address
    connect = mock.Mock()
    monkeypatch.setattr(checks.socket, "create_connection", connect)

    results = asyncio.run(checks.tcp_connect_ports("mixed.example", [22, 80]))

    assert [r.detail for r in results] == ["blocked host"] * 2 and not connect.called


def test_tcp_ports_run_at_most_three_at_once_and_a_hanging_port_only_costs_its_own_deadline(monkeypatch):
    import threading
    import time

    _stub_resolution(monkeypatch, "203.0.113.7", [])
    monkeypatch.setattr(checks, "CHECK_DEADLINE_FACTOR", 0.0)
    monkeypatch.setattr(checks, "CHECK_DEADLINE_MARGIN_SECONDS", 0.4)
    release = threading.Event()
    active = peak = 0
    guard = threading.Lock()

    def fake_connect(address, **_kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            if address[1] == 1:
                release.wait(5)  # hangs past the per-check deadline
            else:
                time.sleep(0.1)
        finally:
            with guard:
                active -= 1
        return contextlib.nullcontext()

    monkeypatch.setattr(checks.socket, "create_connection", fake_connect)
    started = time.monotonic()
    try:
        results = asyncio.run(checks.tcp_connect_ports("mon.example", [1, 2, 3, 4, 5, 6]))
    finally:
        release.set()

    assert time.monotonic() - started < 2.0  # the hanging port is cut at its own deadline, not waited for
    assert [r.ok for r in results] == [False, True, True, True, True, True]
    assert results[0].detail == "timeout"
    assert peak <= checks.MAX_CONCURRENT_TCP_PER_TARGET == 3


def test_tcp_ports_hung_resolver_times_out_every_port_with_one_lookup(monkeypatch):
    import time

    lookups: list[str] = []

    async def hung(host, **_kwargs):
        lookups.append(host)
        await asyncio.sleep(30)

    async def not_blocked(_host):
        return False

    monkeypatch.setattr(checks, "_resolve_host_via_public_dns_first_answer", hung)
    monkeypatch.setattr(checks, "_is_host_blocked_async", not_blocked)
    monkeypatch.setattr(checks, "CHECK_DEADLINE_FACTOR", 0.0)
    monkeypatch.setattr(checks, "CHECK_DEADLINE_MARGIN_SECONDS", 0.3)

    started = time.monotonic()
    results = asyncio.run(checks.tcp_connect_ports("slow.example", [1, 2, 3]))

    assert time.monotonic() - started < 3
    assert [r.detail for r in results] == ["timeout"] * 3 and lookups == ["slow.example"]
