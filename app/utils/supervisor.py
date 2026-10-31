#!/usr/bin/env python3
#
# app/utils/supervisor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Central watchdog for the long-running asyncio services of the application.

Every service is a coroutine function run as one asyncio task under its own supervising task.
An exception, an unexpected return, or (optionally) a missed heartbeat counts as a crash and is
answered with a restart after exponential backoff with jitter. Too many crashes inside a window
put the service into the ``degraded`` state: one ERROR log, then a slow retry instead of a tight
loop. Repeated detected hangs (three within 24 h) also degrade the service; that history
survives a short stable run and clears only after a long error-free run. Shutdown
(``begin_shutdown``/``stop``) and maintenance (``utils.maintenance``) are intended
stops, never crashes: nothing is restarted while either is in effect.

The supervisor starts no threads. Log lines carry ``service=... event=...`` and the crash
traceback is logged once per crash; secrets and payloads are never part of the structured line.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from . import maintenance

_log = logging.getLogger(__name__)


class ServiceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    DEGRADED = "degraded"
    PAUSED = "paused"
    STOPPED = "stopped"


class ServiceExitedError(RuntimeError):
    """A service coroutine returned although nobody asked it to stop."""


class ServiceHungError(RuntimeError):
    """A service stopped reporting its heartbeat and was cancelled."""


@dataclass(frozen=True)
class SupervisionPolicy:
    """Restart tuning shared by all services; every time value is in seconds."""

    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0
    jitter: float = 0.25  # +/- fraction of the delay
    crash_window: float = 300.0
    crash_limit: int = 5  # crashes inside the window that mark the service degraded
    degraded_retry: float = 300.0
    stable_after: float = 60.0  # uptime after which a restart counts as recovered
    hang_window: float = 86400.0  # long window for repeated hangs (a short stable run does not clear it)
    hang_limit: int = 3  # detected hangs inside hang_window that mark the service degraded
    hang_recovery_after: float = 3600.0  # error-free uptime that clears the hang history and degraded state
    maintenance_poll: float = 0.5
    check_interval: float = 1.0  # granularity of the stability/heartbeat checks


@dataclass(frozen=True)
class ServiceSpec:
    """One supervised service.

    ``run`` must be re-callable and block until the service ends. ``request_stop`` asks it to
    end cleanly (usually sets its stop event). ``heartbeat_timeout`` enables hang detection for
    services that call ``ServiceSupervisor.heartbeat(name)`` regularly.
    """

    name: str
    run: Callable[[], Awaitable[None]]
    request_stop: Callable[[], None] | None = None
    heartbeat_timeout: float | None = None


@dataclass
class _Service:
    spec: ServiceSpec
    state: ServiceState = ServiceState.STARTING
    task: asyncio.Task | None = None  # supervising task
    attempt: int = 0  # consecutive unstable runs
    starts: int = 0
    crashes_total: int = 0
    crashes: deque[float] = field(default_factory=deque)
    hangs: deque[float] = field(default_factory=deque)  # long-term hang history, independent of crashes
    last_exc_type: str | None = None
    last_crash_at: datetime | None = None
    last_beat: float = 0.0
    needs_recovery_log: bool = False


class ServiceSupervisor:
    """Run, watch and restart named services; see the module docstring."""

    def __init__(
        self,
        policy: SupervisionPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._policy = policy or SupervisionPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter only, not security relevant
        self._services: dict[str, _Service] = {}
        self._stopping = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def add(self, spec: ServiceSpec) -> None:
        """Register a service and start supervising it (needs a running event loop)."""
        if spec.name in self._services:
            raise ValueError(f"Service already registered: {spec.name}")
        if self._stopping:
            raise RuntimeError("Supervisor is shutting down")
        svc = _Service(spec=spec, last_beat=self._clock())
        self._services[spec.name] = svc
        svc.task = asyncio.create_task(self._supervise(svc), name=f"supervisor:{spec.name}")

    def heartbeat(self, name: str) -> None:
        """Record liveness of a service (cheap; safe to call from its hot loop)."""
        svc = self._services.get(name)
        if svc is not None:
            svc.last_beat = self._clock()

    def begin_shutdown(self) -> None:
        """Forbid any further restart; running services are left to the owners' stop events."""
        self._stopping = True

    async def stop(self, timeout: float = 5.0, cancel_timeout: float = 5.0) -> None:
        """Ask every service to stop, wait up to ``timeout``, cancel the rest and wait ``cancel_timeout``.

        A task that ignores cancellation is logged and left behind instead of blocking shutdown. Idempotent.
        """
        self._stopping = True
        for svc in self._services.values():
            if svc.spec.request_stop is not None:
                try:
                    svc.spec.request_stop()
                except Exception:  # noqa: BLE001 - shutdown boundary: one failing hook must not block the others
                    _log.warning("service=%s event=stop_request_failed", svc.spec.name, exc_info=True)
        tasks = [s.task for s in self._services.values() if s.task is not None and not s.task.done()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            _, stuck = await asyncio.wait(pending, timeout=cancel_timeout)
            for task in stuck:
                name = next((n for n, sv in self._services.items() if sv.task is task), task.get_name())
                _log.error("service=%s event=stop_cancel_timeout waited=%.1fs", name, cancel_timeout)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a JSON-safe health summary per service (no exception messages)."""
        now = self._clock()
        out: dict[str, dict[str, Any]] = {}
        for name, svc in list(self._services.items()):
            hb = svc.spec.heartbeat_timeout is not None
            out[name] = {
                "state": svc.state.value,
                "starts": svc.starts,
                "crashes_total": svc.crashes_total,
                "hangs_in_window": self._recent_hangs(svc),
                "last_exception_type": svc.last_exc_type,
                "last_crash_at": svc.last_crash_at.isoformat() if svc.last_crash_at else None,
                "heartbeat_age_seconds": round(now - svc.last_beat, 1) if hb else None,
            }
        return out

    def is_degraded(self) -> bool:
        return any(s.state is ServiceState.DEGRADED for s in list(self._services.values()))

    # ─── Supervision ─────────────────────────────────────────────────────────

    def _event(self, level: int, svc: _Service, event: str, *, exc_info: Any = None, **fields: Any) -> None:
        parts = "".join(f" {k}={v}" for k, v in fields.items())
        _log.log(
            level,
            "service=%s event=%s state=%s%s",
            svc.spec.name,
            event,
            svc.state.value,
            parts,
            exc_info=exc_info,
            extra={"service": svc.spec.name, "service_event": event},
        )

    def _backoff(self, attempt: int) -> float:
        p = self._policy
        # Cap the exponent: a long crash loop must not overflow the float power.
        delay = min(p.backoff_max, p.backoff_initial * p.backoff_factor ** min(max(0, attempt - 1), 64))
        delay *= 1 + self._rng.uniform(-p.jitter, p.jitter)
        return max(0.0, min(p.backoff_max, delay))

    async def _wait_for_maintenance(self, svc: _Service) -> None:
        """Block while maintenance quiesces the app; a restart must not race a restore."""
        if self._stopping or not maintenance.is_active():
            return
        previous = svc.state
        svc.state = ServiceState.PAUSED
        self._event(logging.INFO, svc, "paused", reason="maintenance")
        while maintenance.is_active() and not self._stopping:
            await self._sleep(self._policy.maintenance_poll)
        if not self._stopping:
            svc.state = previous
            self._event(logging.INFO, svc, "resumed")

    async def _supervise(self, svc: _Service) -> None:
        try:
            while not self._stopping:
                await self._wait_for_maintenance(svc)
                if self._stopping:
                    break
                svc.starts += 1
                if svc.state is not ServiceState.DEGRADED:
                    svc.state = ServiceState.STARTING
                self._event(logging.INFO, svc, "started", attempt=svc.attempt + 1, starts=svc.starts)
                started = self._clock()
                svc.last_beat = started
                exc = await self._run_once(svc, started)
                if self._stopping:
                    break
                if isinstance(exc, maintenance.MaintenanceActiveError) or (exc is not None and maintenance.is_active()):
                    # Refused by a restore window: not a crash, wait it out and start again.
                    self._event(logging.INFO, svc, "interrupted", reason="maintenance")
                    continue
                self._record_crash(svc, exc, ran_for=self._clock() - started)
                delay = self._next_delay(svc)
                if self._stopping:
                    break
                self._event(logging.WARNING, svc, "restarting", attempt=svc.attempt, backoff=f"{delay:.1f}s")
                await self._sleep(delay)
        except Exception:
            self._event(logging.CRITICAL, svc, "supervisor_error", exc_info=True)
            raise
        finally:
            svc.state = ServiceState.STOPPED
            self._event(logging.INFO, svc, "stopped")

    async def _run_once(self, svc: _Service, started: float) -> BaseException | None:
        """Run the service until it ends; return the reason (exception) or None for a clean stop."""
        p = self._policy
        task = asyncio.create_task(svc.spec.run(), name=f"service:{svc.spec.name}")
        svc.state = ServiceState.RUNNING if svc.state is not ServiceState.DEGRADED else ServiceState.DEGRADED
        hb_timeout = svc.spec.heartbeat_timeout
        try:
            while True:
                watching = hb_timeout is not None or svc.needs_recovery_log or svc.attempt > 0 or bool(svc.hangs)
                done, _ = await asyncio.wait({task}, timeout=p.check_interval if watching else None)
                if done:
                    break
                now = self._clock()
                if (svc.attempt > 0 or svc.needs_recovery_log) and now - started >= p.stable_after:
                    self._mark_stable(svc)
                if svc.hangs and now - started >= p.hang_recovery_after:
                    self._clear_hangs(svc)
                if hb_timeout is not None and now - svc.last_beat > hb_timeout:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return ServiceHungError(f"no heartbeat for {hb_timeout:.0f}s")
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if task.cancelled():
            return None if self._stopping else asyncio.CancelledError()
        exc = task.exception()
        if exc is not None:
            return exc
        if self._stopping:
            return None
        return ServiceExitedError("service returned without a stop request")

    def _mark_stable(self, svc: _Service) -> None:
        svc.attempt = 0
        svc.crashes.clear()
        if svc.needs_recovery_log:
            svc.needs_recovery_log = False
            # Repeated hangs keep the service degraded until the long error-free window passes.
            if self._recent_hangs(svc) < self._policy.hang_limit:
                svc.state = ServiceState.RUNNING
                self._event(logging.INFO, svc, "recovered")

    def _recent_hangs(self, svc: _Service) -> int:
        now = self._clock()
        while svc.hangs and now - svc.hangs[0] > self._policy.hang_window:
            svc.hangs.popleft()
        return len(svc.hangs)

    def _clear_hangs(self, svc: _Service) -> None:
        svc.hangs.clear()
        if svc.state is ServiceState.DEGRADED and not svc.crashes:
            svc.state = ServiceState.RUNNING
        self._event(logging.INFO, svc, "recovered", reason="long_error_free_run")

    def _record_crash(self, svc: _Service, exc: BaseException | None, *, ran_for: float) -> None:
        p = self._policy
        now = self._clock()
        if ran_for >= p.stable_after:
            # It ran long enough before failing: an isolated failure, not part of a loop.
            svc.attempt = 0
            svc.crashes.clear()
        svc.attempt += 1
        svc.crashes_total += 1
        svc.last_exc_type = type(exc).__name__ if exc is not None else "Unknown"
        svc.last_crash_at = datetime.now(UTC)
        svc.crashes.append(now)
        if isinstance(exc, ServiceHungError):
            svc.hangs.append(now)
        while svc.crashes and now - svc.crashes[0] > p.crash_window:
            svc.crashes.popleft()
        svc.needs_recovery_log = True
        if svc.state is not ServiceState.DEGRADED:  # degraded is sticky until a run turns out stable
            svc.state = ServiceState.BACKOFF
        self._event(
            logging.ERROR,
            svc,
            "crashed",
            attempt=svc.attempt,
            exc_type=svc.last_exc_type,
            ran_for=f"{ran_for:.1f}s",
            exc_info=exc if exc is not None and exc.__traceback__ is not None else None,
        )

    def _next_delay(self, svc: _Service) -> float:
        p = self._policy
        hang_degraded = self._recent_hangs(svc) >= p.hang_limit
        if len(svc.crashes) >= p.crash_limit or hang_degraded or svc.state is ServiceState.DEGRADED:
            if svc.state is not ServiceState.DEGRADED:
                svc.state = ServiceState.DEGRADED
                self._event(
                    logging.ERROR,
                    svc,
                    "degraded",
                    crashes=len(svc.crashes),
                    hangs=len(svc.hangs),
                    window=f"{p.crash_window:.0f}s",
                    retry_in=f"{p.degraded_retry:.0f}s",
                )
            return p.degraded_retry
        return self._backoff(svc.attempt)


__all__ = [
    "ServiceExitedError",
    "ServiceHungError",
    "ServiceSpec",
    "ServiceState",
    "ServiceSupervisor",
    "SupervisionPolicy",
]
