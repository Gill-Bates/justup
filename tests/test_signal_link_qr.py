#!/usr/bin/env python3
#
# tests/test_signal_link_qr.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001 - white-box access to the link process state
"""Signal device linking: the URI read honours one deadline even when a line is cut off."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from app.services.notifications import signal_cli_backend as mod


def _fake_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "signal-cli"
    script.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


def _backend(tmp_path: Path, script: Path, monkeypatch: pytest.MonkeyPatch) -> mod.SignalCliBackend:
    backend = mod.SignalCliBackend(cli_path=str(script), data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(backend, "_resolve_cli", lambda: str(script))
    return backend


def test_partial_uri_without_newline_times_out_releases_lock_and_kills_child(tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    script = _fake_cli(
        tmp_path,
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('sgnl://linkdevice?uuid=abc')\nsys.stdout.flush()\ntime.sleep(60)",
    )
    monkeypatch.setattr(mod, "QR_TIMEOUT", 0.6)
    backend = _backend(tmp_path, script, monkeypatch)

    t0 = time.monotonic()
    result = backend.get_link_qr()
    elapsed = time.monotonic() - t0

    assert result == (None, None)
    assert elapsed < 5
    assert backend._state_lock.acquire(blocking=False), "lock must be free after the timeout"
    backend._state_lock.release()
    assert backend._link_process is None
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "child process must not outlive the timeout"


def test_uri_split_across_writes_is_assembled_within_the_deadline(tmp_path, monkeypatch):
    script = _fake_cli(
        tmp_path,
        "sys.stdout.write('sgnl://linkdevice?'); sys.stdout.flush(); time.sleep(0.3)\n"
        "sys.stdout.write('uuid=abc\\n'); sys.stdout.flush(); time.sleep(30)",
    )
    monkeypatch.setattr(mod, "QR_TIMEOUT", 5)
    backend = _backend(tmp_path, script, monkeypatch)
    try:
        png, session_id = backend.get_link_qr()
        assert png is not None and png.startswith(b"\x89PNG")
        assert session_id
    finally:
        backend.cancel_link()


def test_non_uri_output_is_rejected(tmp_path, monkeypatch):
    script = _fake_cli(tmp_path, "print('error: nope'); sys.stdout.flush(); time.sleep(30)")
    monkeypatch.setattr(mod, "QR_TIMEOUT", 5)
    backend = _backend(tmp_path, script, monkeypatch)
    assert backend.get_link_qr() == (None, None)
    assert backend._link_process is None
