#!/usr/bin/env python3
#
# tests/test_check_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Host contract (FR-06) and subprocess argument hygiene (NFR-07)."""

from __future__ import annotations

import asyncio
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


if __name__ == "__main__":
    unittest.main()


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

    async def fake_request(self, url, **kw):  # noqa: ARG001
        seen.append((str(url), kw))
        raise httpx.ConnectError("refused")

    with (
        mock.patch("socket.getaddrinfo", getaddrinfo),
        mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", public_dns),
        mock.patch.object(httpx.AsyncClient, "head", fake_request),
        mock.patch.object(httpx.AsyncClient, "get", fake_request),
    ):
        result = asyncio.run(checks.http_head(url, **kwargs))
    return result, seen


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
