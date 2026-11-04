#!/usr/bin/env python3
#
# app/services/nmap_local.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Supervises the Nmap worker when justUp runs outside the container image.

In the image docker/entrypoint.sh starts the worker under its own UID with CAP_NET_RAW and
exports JUSTUP_NMAP_SOCKET; nothing here runs then. Started directly (``python run.py``) no
entrypoint exists, so the app launches the same ``app.services.nmap_worker`` module as a
separate child process with the web process's own rights and a scrubbed environment. It gains
no capability: TCP scans work, UDP scans need CAP_NET_RAW and report ``permission_denied``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from app.services import nmap_client, nmap_scanner
from app.utils.time import utcnow

_log = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOT_INSTALLED = "Nmap is not installed on this host. Install the nmap package and restart justUp!"


def external_worker_configured() -> bool:
    """True when something else (the container entrypoint) owns the worker socket."""
    return bool(os.environ.get("JUSTUP_NMAP_SOCKET"))


class LocalWorker:
    """Start the worker, wait for its socket, restart it with bounded backoff."""

    def __init__(
        self,
        *,
        ready_timeout: float = 20.0,
        max_backoff: float = 60.0,
        max_failures: int = 8,
        stable_after: float = 120.0,
    ) -> None:
        self._ready_timeout = ready_timeout
        self._max_backoff = max_backoff
        self._max_failures = max_failures
        self._stable_after = stable_after
        self._directory: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._restarts = 0

    async def start(self) -> None:
        # The private 0700 directory keeps the socket and the status file away from other local users.
        self._directory = Path(tempfile.mkdtemp(prefix="justup-nmap-"))
        nmap_client.SOCKET_PATH = str(self._directory / "nmap.sock")
        nmap_client.WORKER_STATUS_PATH = self._directory / "status.json"
        if nmap_scanner.locate_binary() is None:
            self._status("failed", "nmap binary not found", NOT_INSTALLED)
            _log.warning("%s Port discovery stays unavailable.", NOT_INSTALLED)
            return
        self._task = asyncio.create_task(self._supervise(), name="nmap-local-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._terminate()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)

    def _status(self, state: str, detail: str = "", message: str = "") -> None:
        if self._directory is None:
            return
        payload = {
            "state": state,
            "detail": detail,
            "message": message,
            "restarts": self._restarts,
            "since": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        target = self._directory / "status.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(target)

    def _environment(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONPATH": str(_PROJECT_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "JUSTUP_NMAP_SOCKET": nmap_client.SOCKET_PATH,
            "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
        }
        if os.environ.get("NMAPDIR"):
            env["NMAPDIR"] = os.environ["NMAPDIR"]
        return env

    async def _launch(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.services.nmap_worker",
            env=self._environment(),
            cwd=str(_PROJECT_ROOT),
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    async def _wait_ready(self) -> str | None:
        """Return None when ready, else the reason."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        while loop.time() < deadline:
            if self._process is None or self._process.returncode is not None:
                return "worker exited during startup"
            if await nmap_client.probe_socket(timeout=1):
                return None
            await asyncio.sleep(0.2)
        return f"worker not ready within {self._ready_timeout:g}s"

    async def _supervise(self) -> None:
        failures, delay = 0, 1.0
        loop = asyncio.get_running_loop()
        while True:
            self._status("starting")
            started = loop.time()
            reason: str
            try:
                await self._launch()
                reason = await self._wait_ready() or ""
                if not reason:
                    self._status("ready")
                    _log.info("Nmap worker ready (pid %s, socket %s)", self._process.pid, nmap_client.SOCKET_PATH)
                    code = await self._process.wait()
                    reason = f"worker exited with status {code}"
                    if loop.time() - started >= self._stable_after:
                        failures, delay = 0, 1.0
            except OSError as exc:
                reason = f"worker could not be launched ({type(exc).__name__})"
            await self._terminate()
            failures += 1
            if failures >= self._max_failures:
                self._status("failed", f"{reason}; gave up after {failures} consecutive failures")
                _log.error("Nmap worker: %s; giving up, port discovery stays unavailable", reason)
                return
            self._status("restarting", f"{reason}; retry in {delay:g}s")
            _log.warning("Nmap worker: %s; restarting in %gs", reason, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_backoff)
            self._restarts += 1


_worker: LocalWorker | None = None


async def start_local_worker() -> None:
    """Start the supervised local worker unless the container entrypoint owns it."""
    global _worker
    if external_worker_configured() or _worker is not None:
        return
    _worker = LocalWorker()
    await _worker.start()


async def stop_local_worker() -> None:
    global _worker
    worker, _worker = _worker, None
    if worker is not None:
        await worker.stop()
