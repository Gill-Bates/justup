#!/usr/bin/env python3
#
# tests/test_nmap_local_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Outside Docker no entrypoint starts the Nmap worker: the app supervises its own unprivileged one."""

# ruff: noqa: SLF001, ASYNC240  # white-box checks of the private worker process and socket directory
from __future__ import annotations

import asyncio
import os
import signal
import stat
from pathlib import Path

import pytest

from app.services import nmap_client, nmap_local, nmap_scanner

REAL_EXTERNAL_CHECK = (
    nmap_local.external_worker_configured
)  # the autouse fixture in conftest.py patches the module attribute


@pytest.fixture
def local(monkeypatch, tmp_path):
    """Run the real local supervisor with short timings and a fake `nmap` on PATH."""
    monkeypatch.setattr(nmap_local, "external_worker_configured", lambda: False)
    fake = tmp_path / "nmap"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(nmap_scanner, "locate_binary", lambda: str(fake))
    monkeypatch.setattr(nmap_client, "SOCKET_PATH", "/nonexistent/before.sock")
    monkeypatch.setattr(nmap_client, "LOCAL_STATUS", None)
    return nmap_local.LocalWorker(ready_timeout=15, max_backoff=1)


async def _until(predicate, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached in time")


def test_container_supervisor_is_left_alone_when_the_socket_is_configured(monkeypatch):
    monkeypatch.setenv("JUSTUP_NMAP_SOCKET", "/run/justup/nmap.sock")
    assert REAL_EXTERNAL_CHECK() is True
    monkeypatch.delenv("JUSTUP_NMAP_SOCKET")
    assert REAL_EXTERNAL_CHECK() is False


@pytest.mark.asyncio
async def test_missing_nmap_binary_is_reported_with_the_prerequisite(monkeypatch, local):
    monkeypatch.setattr(nmap_scanner, "locate_binary", lambda: None)
    await local.start()
    try:
        availability = await nmap_client.availability()
        assert availability["available"] is False
        assert availability["state"] == "failed"
        assert "install" in availability["message"].lower() and "nmap" in availability["message"].lower()
        assert local._process is None
    finally:
        await local.stop()


@pytest.mark.asyncio
async def test_worker_is_started_ready_supervised_and_cleaned_up(local):
    await local.start()
    try:
        await _until(_ready)
        socket_dir = local._directory
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        assert str(socket_dir / "nmap.sock") == nmap_client.SOCKET_PATH
        status = nmap_client.worker_status()
        assert status["state"] == "ready" and status["restarts"] == 0
        # The child gets a scrubbed environment: no application secrets.
        environ = Path(f"/proc/{local._process.pid}/environ").read_bytes().split(b"\0")
        assert not any(item.startswith(b"SECRET_KEY=") for item in environ)
        # A crashed worker is restarted with backoff and becomes reachable again.
        first_pid = local._process.pid
        os.kill(first_pid, signal.SIGKILL)
        await _until(lambda: _restarted(local, first_pid))
        await _until(_ready)
        assert nmap_client.worker_status()["restarts"] >= 1
    finally:
        socket_dir = local._directory
        await local.stop()
    assert not socket_dir.exists()


async def _ready() -> bool:
    return nmap_client.worker_status()["state"] == "ready" and (await nmap_client.availability())["available"] is True


async def _restarted(worker, first_pid) -> bool:
    return worker._process is not None and worker._process.pid != first_pid
