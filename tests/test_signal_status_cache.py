#!/usr/bin/env python3
#
# tests/test_signal_status_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001 - exercise the link process lifecycle without a subprocess
"""Signal status cache expiry, concurrency, and mutation invalidation."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.notifications import signal_cli_backend as mod
from app.services.notifications.signal_types import SignalStatus


def _status(*, reachable: bool = True) -> SignalStatus:
    return SignalStatus(
        reachable=reachable,
        registered=reachable,
        linked=reachable,
        accounts=["+49****78"] if reachable else [],
        error=None if reachable else "offline",
    )


def test_status_cache_expiry_and_forced_refresh(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    now = [100.0]
    calls = []
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(backend, "_fetch_status_uncached", lambda: calls.append(1) or _status())

    assert backend.status() == backend.status()
    assert len(calls) == 1
    now[0] += 59
    backend.status()
    assert len(calls) == 1
    backend.status(force_refresh=True)
    assert len(calls) == 2
    now[0] += 60
    backend.status()
    assert len(calls) == 3


def test_failed_status_has_short_ttl(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    now = [100.0]
    calls = []
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(backend, "_fetch_status_uncached", lambda: calls.append(1) or _status(reachable=False))

    backend.status()
    now[0] += 9
    backend.status()
    assert len(calls) == 1
    now[0] += 1
    backend.status()
    assert len(calls) == 2


def test_concurrent_status_requests_share_one_fetch(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    started = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    calls = []
    original_wait_for = backend._status_condition.wait_for

    def wait_for(predicate):
        waiting.set()
        return original_wait_for(predicate)

    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return _status()

    monkeypatch.setattr(backend, "_fetch_status_uncached", fetch)
    monkeypatch.setattr(backend._status_condition, "wait_for", wait_for)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(backend.status)
        assert started.wait(3)
        others = [pool.submit(backend.status) for _ in range(2)]
        assert waiting.wait(3)
        release.set()
        assert first.result(timeout=3) == _status()
        assert all(future.result(timeout=3) == _status() for future in others)
    assert len(calls) == 1


def test_invalidation_discards_in_flight_result(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
            return _status(reachable=False)
        return _status()

    monkeypatch.setattr(backend, "_fetch_status_uncached", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(backend.status)
        assert started.wait(3)
        backend.invalidate_cache()
        release.set()
        assert future.result(timeout=3) == _status()
    assert len(calls) == 2
    assert backend.status() == _status()
    assert len(calls) == 2


def test_verify_invalidates_status(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    calls = []
    monkeypatch.setattr(backend, "_fetch_status_uncached", lambda: calls.append(1) or _status())
    monkeypatch.setattr(backend, "_run_signal_rpc", lambda *_args, **_kwargs: {"result": None})
    backend.status()
    backend.verify("+4917012345678", "123456")
    backend.status()
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["unlink_device", "unregister"])
def test_account_removal_invalidates_status(tmp_path, monkeypatch, operation):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    calls = []
    account = "+4917012345678"
    account_lists = iter([[account], [account], []] if operation == "unregister" else [[account], []])
    monkeypatch.setattr(backend, "_fetch_status_uncached", lambda: calls.append(1) or _status())
    monkeypatch.setattr(backend, "list_accounts", lambda: next(account_lists))
    monkeypatch.setattr(backend, "_run_signal_rpc", lambda *_args, **_kwargs: {"result": None})
    monkeypatch.setattr(backend, "_require_rpc_success", lambda *_args: None)

    backend.status()
    getattr(backend, operation)(account)
    backend.status()
    assert len(calls) == 2


def test_completed_link_invalidates_status(tmp_path, monkeypatch):
    backend = mod.SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path))
    calls = []

    class Process:
        def wait(self, *, timeout):
            assert timeout == 60
            return 0

    backend._link_process = Process()
    backend._link_session_id = "link-session"
    monkeypatch.setattr(mod, "_finish_pipe_drain", lambda *_args: None)
    monkeypatch.setattr(backend, "_fetch_status_uncached", lambda: calls.append(1) or _status())

    backend.status()
    assert backend.wait_for_link("link-session") is True
    backend.status()
    assert len(calls) == 2
