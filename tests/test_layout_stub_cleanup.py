#
# tests/test_layout_stub_cleanup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""The layout app stub must not outlive a test worker that was killed without cleanup."""

from __future__ import annotations

import base64
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

STUB = Path(__file__).resolve().parent / "layout" / "app_stub.py"

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="uses PR_SET_PDEATHSIG")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_stub_exits_when_its_parent_is_killed(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
        "JUSTUP_DATA_DIR": str(tmp_path / "data"),
        "JUSTUP_HOST": "127.0.0.1",
        "JUSTUP_PORT": str(port),
        "LOG_LEVEL": "WARNING",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
    }
    pidfile = tmp_path / "stub.pid"
    parent = subprocess.Popen(
        ["sh", "-c", f'"$0" "$1" & echo $! > "{pidfile}"; wait', sys.executable, str(STUB)],
        env=env,
        cwd=STUB.parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stub_pid = 0
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if pidfile.exists() and pidfile.read_text().strip():
                stub_pid = int(pidfile.read_text())
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    pass
            time.sleep(0.2)
        else:
            pytest.fail("stub did not start listening")
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _alive(stub_pid):
            time.sleep(0.2)
        assert not _alive(stub_pid), "stub must stop when its parent dies"
    finally:
        if stub_pid and _alive(stub_pid):
            os.kill(stub_pid, signal.SIGKILL)
        parent.poll()
