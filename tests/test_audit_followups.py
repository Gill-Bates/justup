#!/usr/bin/env python3
#
# tests/test_audit_followups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Probe target resolution, error labels, traceroute feature cache and seed download hardening."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
from unittest import mock

import pytest

from app.api import signal as signal_api
from app.monitor import checks
from app.services.notifications.signal_errors import SignalUnavailable
from tests import _support


def _resolve(host: str, **kwargs):
    return asyncio.run(checks._resolve_probe_target(host, **kwargs))


def test_resolve_probe_target_blocked_host() -> None:
    with mock.patch.object(checks, "_is_host_blocked_async", mock.AsyncMock(return_value=True)):
        assert _resolve("10.0.0.1", skip_ssrf_check=False) == (None, False)


def test_resolve_probe_target_skip_ssrf_allows_literal_and_resolves_name() -> None:
    with mock.patch.object(
        checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value="192.0.2.7")
    ):
        assert _resolve("10.0.0.1", skip_ssrf_check=True) == ("10.0.0.1", False)
        assert _resolve("example.test", skip_ssrf_check=True) == ("192.0.2.7", True)


def test_resolve_probe_target_blocks_name_resolving_to_blocked_ip() -> None:
    with (
        mock.patch.object(checks, "_is_host_blocked_async", mock.AsyncMock(return_value=False)),
        mock.patch.object(
            checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value="127.0.0.1")
        ),
    ):
        assert _resolve("evil.test", skip_ssrf_check=False) == (None, True)


def test_resolve_probe_target_pins_system_answer_without_public_answer() -> None:
    pinned = [ipaddress.ip_address("93.184.216.34")]
    with (
        mock.patch.object(checks, "_is_host_blocked_async", mock.AsyncMock(return_value=False)),
        mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value=None)),
        mock.patch.object(checks, "resolve_validated", return_value=pinned),
    ):
        assert _resolve("example.test", skip_ssrf_check=False) == ("93.184.216.34", True)


def test_resolve_probe_target_never_returns_bare_name_when_system_answer_is_blocked() -> None:
    with (
        mock.patch.object(checks, "_is_host_blocked_async", mock.AsyncMock(return_value=False)),
        mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value=None)),
        mock.patch.object(checks, "resolve_validated", return_value=None),
    ):
        assert _resolve("example.test", skip_ssrf_check=False) == (None, True)


def test_resolve_probe_target_skip_ssrf_keeps_hostname_without_answer() -> None:
    with mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value=None)):
        assert _resolve("example.test", skip_ssrf_check=True) == ("example.test", True)


def test_network_unreachable_has_own_label_and_stays_retryable() -> None:
    assert checks._normalize_error_type(OSError(errno.ENETUNREACH, "x")) == "network unreachable"
    assert checks._normalize_error_type(OSError(errno.ETIMEDOUT, "x")) == "timeout"
    assert checks._normalize_error_type(OSError(errno.ECONNREFUSED, "x")) == "connection refused"
    assert checks.is_retryable_detail("network unreachable")
    assert checks.is_retryable_detail("Timeout")
    assert not checks.is_retryable_detail("connection refused")
    assert not checks.is_retryable_detail(None)


class _DownBackend:
    def status(self, **_kwargs):
        raise SignalUnavailable("signal-cli not found at /opt/secret/path")

    def list_accounts(self):
        raise SignalUnavailable("signal-cli not found at /opt/secret/path")


def test_signal_api_hides_backend_error_details(monkeypatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(signal_api, "get_signal_backend", _DownBackend)
    assert "secret" not in json.dumps(signal_api.signal_status(_user=None))
    with pytest.raises(HTTPException) as exc:
        signal_api.signal_accounts(None)
    assert exc.value.status_code == 503
    assert "secret" not in str(exc.value.detail)


def test_signal_qr_js_does_not_send_unused_number_param() -> None:
    src = (_support.ROOT / "app/static/js/settings.signal-registration.js").read_text(encoding="utf-8")
    assert "?number=" not in src
