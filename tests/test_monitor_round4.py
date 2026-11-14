#!/usr/bin/env python3
#
# tests/test_monitor_round4.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Dual-stack probe fallback and alert deferral after a failed status write."""

from __future__ import annotations

import asyncio
import socket
from datetime import timedelta
from unittest import mock

import pytest

from app.db import sqlite as sqlite_db
from app.monitor import checks, scheduler
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_scheduler_alert_state import _sched, _target

V4, V6 = "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"


def _infos(*ips: str):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def _resolve(system: list[str], first: str | None):
    """Patch DNS: public answer ``first`` (AAAA answered first) and the system resolver ``system``."""
    return (
        mock.patch.object(checks, "_is_host_blocked_async", mock.AsyncMock(return_value=False)),
        mock.patch.object(checks, "_resolve_host_via_public_dns_first_answer", mock.AsyncMock(return_value=first)),
        mock.patch("socket.getaddrinfo", return_value=_infos(*system)),
        mock.patch.object(checks, "resolve_validated", return_value=None),
    )


def _tcp(system, first, reachable: set[str]):
    tried: list[str] = []

    def connect(addr, **_kw):
        tried.append(addr[0])
        if addr[0] not in reachable:
            raise OSError(113, "no route")
        return mock.MagicMock()

    p = _resolve(system, first)
    with p[0], p[1], p[2], p[3], mock.patch.object(socket, "create_connection", side_effect=connect):
        return asyncio.run(checks.tcp_connect("dual.example", 443, timeout_seconds=2)), tried


def test_ipv6_first_but_unreachable_falls_back_to_ipv4():
    result, tried = _tcp([V6, V4], V6, {V4})
    assert result.ok and tried == [V6, V4]


def test_ipv6_only_and_ipv4_only_targets():
    assert _tcp([V6], V6, {V6})[0].ok
    assert _tcp([V4], V4, {V4})[0].ok


def test_both_families_unreachable_is_down():
    result, tried = _tcp([V6, V4], V6, set())
    assert not result.ok and set(tried) == {V4, V6}


def test_blocked_addresses_are_never_used():
    result, tried = _tcp(["127.0.0.1", "::1"], "127.0.0.1", {"127.0.0.1", "::1"})
    assert not result.ok and result.detail == "blocked host" and tried == []


def test_blocked_fallback_address_is_dropped():
    result, tried = _tcp([V6, "169.254.169.254"], V6, {"169.254.169.254"})
    assert not result.ok and "169.254.169.254" not in tried


def test_ping_falls_back_to_second_family():
    seen: list[str] = []

    def run(cmd, **_kw):
        seen.append(cmd[-1])
        code = 0 if cmd[-1] == V4 else 1
        return mock.Mock(returncode=code, stdout="time=1.5 ms", stderr="")

    p = _resolve([V6, V4], V6)
    with p[0], p[1], p[2], p[3], mock.patch("subprocess.run", side_effect=run):
        result = asyncio.run(checks.ping("dual.example", timeout_seconds=4))
    assert result.ok and seen == [V6, V4]


def test_status_write_failure_defers_alert(db_path, tmp_path):
    tid = _target(db_path, enable_ping=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    sched._rt[tid].down_since_by_service["ping"] = utcnow() - timedelta(minutes=30)
    failed = scheduler.checks.CheckResult(ok=False, detail="timeout")
    alert = mock.AsyncMock()
    sched._maybe_alert = alert
    with (
        mock.patch.object(scheduler.checks, "ping", mock.AsyncMock(return_value=failed)),
        mock.patch.object(scheduler.tsdb_db, "append_points"),
        mock.patch.object(sqlite_db, "set_target_status_snapshot", side_effect=OSError("disk")),
    ):
        asyncio.run(sched._run_target(tid, utcnow()))
    alert.assert_not_called()
    assert (tid, "status") in sched._persistence_failures

    with (
        mock.patch.object(scheduler.checks, "ping", mock.AsyncMock(return_value=failed)),
        mock.patch.object(scheduler.tsdb_db, "append_points"),
    ):
        asyncio.run(sched._run_target(tid, utcnow()))
    alert.assert_called_once()


@pytest.mark.parametrize("name", ["BLOCKED_NETWORKS"])
def test_unused_blocked_networks_constant_is_gone(name):
    assert not hasattr(checks, name)
