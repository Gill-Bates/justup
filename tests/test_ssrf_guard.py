#!/usr/bin/env python3
#
# tests/test_ssrf_guard.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared SSRF guard used by the WhatWeb and SSLyze scanners (resolver mocked, no network)."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from unittest import mock

import pytest

from app.utils import ssrf, whatweb_parser
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _fake_getaddrinfo(*addresses: str):
    def fake(host, port, family=0, type_=0, *args, **kwargs):  # noqa: ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]

    return fake


@pytest.mark.parametrize(
    "literal",
    ["127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "fe80::1", "::ffff:127.0.0.1"],  # noqa: S104
)
def test_always_blocked_even_when_private_is_allowed(literal):
    assert ssrf.resolve_validated(literal, allow_private=True) is None


@pytest.mark.parametrize("literal", ["10.0.0.5", "172.16.1.1", "192.168.1.1", "100.64.0.1", "fd00::1"])
def test_private_only_passes_when_allowed(literal):
    assert ssrf.resolve_validated(literal, allow_private=False) is None
    assert ssrf.resolve_validated(literal, allow_private=True) == [ipaddress.ip_address(literal)]


def test_public_literal_passes():
    assert ssrf.resolve_validated("93.184.216.34", allow_private=False) == [ipaddress.ip_address("93.184.216.34")]


def test_hostname_with_any_blocked_answer_is_rejected():
    with mock.patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34", "127.0.0.1")):
        assert ssrf.resolve_validated("rebind.example", allow_private=True) is None


def test_hostname_resolves_once_and_returns_addresses():
    calls = []
    fake = _fake_getaddrinfo("93.184.216.34")

    def counting(*args, **kwargs):
        calls.append(args)
        return fake(*args, **kwargs)

    with mock.patch("socket.getaddrinfo", counting):
        assert ssrf.resolve_validated("example.org", allow_private=False) == [ipaddress.ip_address("93.184.216.34")]
    assert len(calls) == 1


def test_unresolvable_and_localhost_fail_closed():
    with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert ssrf.resolve_validated("nx.example", allow_private=True) is None
    assert ssrf.resolve_validated("localhost", allow_private=True) is None
    assert ssrf.resolve_validated("", allow_private=True) is None


class TestWhatWeb:
    def _run(self, url, *addresses):
        seen = {}

        def fake_run(cmd, timeout):  # noqa: ARG001
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            mock.patch("socket.getaddrinfo", _fake_getaddrinfo(*addresses)),
            mock.patch.object(whatweb_parser, "_find_whatweb_binary", return_value="/usr/bin/whatweb"),
            mock.patch.object(whatweb_parser, "_run_capped", fake_run),
        ):
            whatweb_parser.run_whatweb(url, aggression=1, timeout=5)
        return seen.get("cmd")

    def test_blocked_targets_are_refused(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/", "http://0.0.0.0/"):
            with pytest.raises(ValueError, match="SSRF"):
                whatweb_parser.run_whatweb(url, aggression=1, timeout=5)

    def test_dns_failure_is_refused(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror), pytest.raises(ValueError, match="SSRF"):
            whatweb_parser.run_whatweb("http://nx.example/", aggression=1, timeout=5)

    def test_rebinding_answer_set_is_refused(self):
        with (
            mock.patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34", "127.0.0.1")),
            pytest.raises(ValueError, match="SSRF"),
        ):
            whatweb_parser.run_whatweb("http://rebind.example/", aggression=1, timeout=5)

    def test_http_scan_is_pinned_to_validated_ip_with_host_header(self):
        cmd = self._run("http://example.org:8080/x", "93.184.216.34")
        assert cmd[-1] == "http://93.184.216.34:8080/x"
        assert cmd[cmd.index("-H") : cmd.index("-H") + 2] == ["-H", "Host:example.org:8080"]
        assert "--follow-redirect=never" in cmd

    def test_rfc1918_target_is_allowed_and_pinned(self):
        cmd = self._run("http://intranet.example/", "10.1.2.3")
        assert cmd[-1] == "http://10.1.2.3/"

    def test_ip_literal_is_scanned_as_is_on_https(self):
        cmd = self._run("https://192.168.1.10/", "192.168.1.10")
        assert cmd[-1] == "https://192.168.1.10/"

    def test_https_hostname_fails_closed_without_sni_pinning(self):
        with (
            mock.patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")),
            pytest.raises(ValueError, match="SNI"),
        ):
            whatweb_parser.run_whatweb("https://example.org/", aggression=1, timeout=5)


class TestSslScan:
    def test_loopback_and_metadata_targets_are_not_scanned(self):
        from app.utils import tls_scanner
        from app.utils.tls_model import ScanStatus

        with mock.patch.object(tls_scanner, "Scanner") as scanner:
            for target in ("https://127.0.0.1", "https://169.254.169.254"):
                assert tls_scanner.scan_tls(target).status is ScanStatus.FAILED
            with mock.patch("socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")):
                assert tls_scanner.scan_tls("https://rebind.example").status is ScanStatus.FAILED
        scanner.assert_not_called()

    def test_validated_address_is_pinned(self):
        from app.utils import tls_scanner

        with (
            mock.patch("socket.getaddrinfo", _fake_getaddrinfo("10.1.2.3")),
            mock.patch.object(tls_scanner, "Scanner") as scanner,
            mock.patch.object(tls_scanner, "ServerNetworkLocation") as location,
        ):
            scanner.return_value.get_results.return_value = []
            tls_scanner.scan_tls("https://internal.example")
        assert location.call_args.kwargs["ip_address"] == "10.1.2.3"
