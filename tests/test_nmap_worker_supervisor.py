#!/usr/bin/env python3
#
# tests/test_nmap_worker_supervisor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""docker/nmap-worker.sh: readiness handshake, bounded restarts and the published status."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "nmap-worker.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

# Stand-in worker: binds the Unix socket like the real one, lives LIFETIME seconds, then exits.
FAKE_WORKER = """
import os, socket, sys, time
if os.environ.get("FAKE_BIND") == "1":
    server = socket.socket(socket.AF_UNIX)
    server.bind(os.environ["JUSTUP_NMAP_SOCKET"])
time.sleep(float(os.environ["FAKE_LIFETIME"]))
"""


def run_supervisor(tmp_path: Path, *, bind: bool, lifetime: float, handshake_ok: bool = True, **limits):
    status = tmp_path / "status.json"
    launches = tmp_path / "launches"
    env = {
        **os.environ,
        "NMAP_WORKER_STATUS_FILE": str(status),
        "JUSTUP_NMAP_SOCKET": str(tmp_path / "nmap.sock"),
        "NMAP_WORKER_READY_TIMEOUT": str(limits.get("ready_timeout", 3)),
        "NMAP_WORKER_MAX_FAILURES": str(limits.get("max_failures", 3)),
        "NMAP_WORKER_MAX_BACKOFF": "1",
        "FAKE_BIND": "1" if bind else "0",
        "FAKE_LIFETIME": str(lifetime),
        "FAKE_PYTHON": sys.executable,
        "FAKE_LAUNCHES": str(launches),
    }
    script = f"""
source '{SCRIPT}'
launch_worker() {{
    rm -f "$JUSTUP_NMAP_SOCKET"
    echo x >> "$FAKE_LAUNCHES"
    "$FAKE_PYTHON" -c "$FAKE_WORKER" &
}}
handshake() {{ return {0 if handshake_ok else 1}; }}
supervise
"""
    result = subprocess.run(  # noqa: S603 # fixed local script under test
        ["bash", "-c", script],
        env={**env, "FAKE_WORKER": FAKE_WORKER},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    count = len(launches.read_text().split()) if launches.exists() else 0
    return result, json.loads(status.read_text()), count


def test_worker_that_dies_at_startup_is_retried_then_reported_failed(tmp_path):
    result, status, launches = run_supervisor(tmp_path, bind=False, lifetime=0)
    assert result.returncode == 1
    assert launches == 3  # bounded: no endless fast restart loop
    assert status["state"] == "failed"
    assert "exited during startup" in status["detail"]
    assert status["restarts"] == 2


def test_socket_that_never_becomes_ready_times_out(tmp_path):
    result, status, launches = run_supervisor(tmp_path, bind=False, lifetime=30, ready_timeout=1, max_failures=2)
    assert result.returncode == 1
    assert launches == 2
    assert status["state"] == "failed"
    assert "not ready within 1s" in status["detail"]


def test_failing_handshake_is_not_ready_even_with_a_socket(tmp_path):
    result, status, _ = run_supervisor(
        tmp_path, bind=True, lifetime=30, handshake_ok=False, ready_timeout=1, max_failures=1
    )
    assert result.returncode == 1
    assert "not ready" in status["detail"]
    assert "worker ready" not in result.stdout


def test_worker_that_ends_after_being_ready_is_restarted_with_backoff_then_given_up(tmp_path):
    result, status, launches = run_supervisor(tmp_path, bind=True, lifetime=0.6)
    assert launches == 3
    assert result.stdout.count("worker ready") == 3
    assert "exited with status" in status["detail"]
    assert status["state"] == "failed"
    assert status["restarts"] == 2


def test_status_detail_is_sanitised_and_file_is_world_readable(tmp_path):
    _, status, _ = run_supervisor(tmp_path, bind=False, lifetime=0, max_failures=1)
    assert set(status) == {"state", "detail", "restarts", "since"}
    assert (tmp_path / "status.json").stat().st_mode & 0o044 == 0o044
