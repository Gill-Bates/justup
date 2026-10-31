#!/usr/bin/env python3
#
# app/monitor/scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Async monitoring scheduler that runs target checks and updates cached status/metrics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from app.db import retention as db_retention
from app.db import sqlite as sqlite_db
from app.db import tsdb as tsdb_db
from app.monitor import checks
from app.monitor.quality_probe import QualityResult, run_quality_probe
from app.services import event_bus
from app.services.notifications import (
    AlertPayload,
    NotificationDispatcher,
    get_spooler,
    smtp_transport,
)
from app.services.notifications.signal_backend import get_signal_backend
from app.utils import maintenance
from app.utils.constants import SettingKeys
from app.utils.crypto import decrypt_secret
from app.utils.ssrf import redact_url
from app.utils.time import utcnow

_log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Status Constants (avoid magic strings)
# NOTE: Plain string constants instead of StrEnum.
# ─────────────────────────────────────────────────────────────────────────────


class TargetStatus:
    """Constants for target status values."""

    UP: str = "up"
    DOWN: str = "down"
    UNKNOWN: str = "unknown"
    DISABLED: str = "disabled"


class PortStatus:
    """Constants for port status values."""

    UP: str = "up"
    DOWN: str = "down"


class AlertStatus:
    """Constants for alert status values."""

    OPEN: str = "open"
    RESOLVED: str = "resolved"


class QualityState:
    """Constants for quality probe state."""

    OK: str = "ok"
    DEGRADED: str = "degraded"
    DOWN: str = "down"


MAX_JITTER_SECONDS = 10
TOKEN_CLEANUP_RETRY_SECONDS = 300
DB_MAINTENANCE_INTERVAL_SECONDS = 3600

# Housekeeping: daily rollups with 5 minutes jitter spread
HOUSEKEEPING_INTERVAL_SECONDS = 86400  # 24 hours
HOUSEKEEPING_RETRY_SECONDS = 300  # next try while a target's compression backlog is not drained
HOUSEKEEPING_ERROR_RETRY_SECONDS = 3600  # after an I/O error (e.g. disk full): do not hammer the disk
HOUSEKEEPING_JITTER_SECONDS = 300  # 5 minutes

# Maximum backoff for retries (prevents unbounded growth if max_retries is high)
MAX_BACKOFF_SECONDS = 30


# Epoch of the scheduler run a coroutine belongs to; inherited by its tasks and worker threads.
_RUN_EPOCH: ContextVar[int | None] = ContextVar("scheduler_run_epoch", default=None)


class StaleRunError(maintenance.MaintenanceActiveError):
    """Thread work of a scheduler run that the supervisor has already replaced was refused."""


def _run_in_maintenance_section(generation: int, callback, *args, **kwargs):
    """Keep synchronous worker-thread work registered until the thread exits."""
    with maintenance.work_section(generation):
        return callback(*args, **kwargs)


def _stable_housekeeping_jitter(target_id: int) -> int:
    """
    Calculate a stable jitter offset (0-300 seconds) for housekeeping tasks.

    Spreads rollup/pruning work across 5 minutes to avoid I/O spikes.
    """
    h = hashlib.md5(f"housekeeping:{target_id}".encode(), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % (HOUSEKEEPING_JITTER_SECONDS + 1)


def _stable_jitter_offset(target_id: int) -> int:
    """
    Calculate a stable jitter offset (0-10 seconds) for a target.

    Uses hash of target_id to ensure deterministic, evenly distributed offsets.
    This spreads load across the check interval and prevents thundering herd.
    """
    h = hashlib.md5(str(target_id).encode(), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % (MAX_JITTER_SECONDS + 1)


def _align_to_interval(now: datetime, interval_seconds: int, offset_seconds: int) -> datetime:
    """
    Calculate next aligned run time with jitter offset.

    Aligns to interval boundaries (e.g., every 60s starting at :00)
    then adds the stable offset for this target.

    Example with 60s interval, 7s offset:
      - now = 12:34:45 → next = 12:35:00 + 7s = 12:35:07
      - now = 12:35:08 → next = 12:36:00 + 7s = 12:36:07
    """
    epoch = now.timestamp()
    interval_start = ((epoch // interval_seconds) + 1) * interval_seconds
    next_ts = interval_start + offset_seconds
    return datetime.fromtimestamp(next_ts, tz=UTC)


def _decrypt_password(stored: str | None) -> str | None:
    """Decrypt http_password from database. Returns None on failure."""
    if not stored:
        return None
    try:
        return decrypt_secret(stored)
    except ValueError as e:
        _log.warning("Failed to decrypt http_password: %s", e)
        return None


def _parse_tcp_ports(value: str | int | None) -> list[int]:
    """
    Parse TCP ports from various input formats.

    Accepts:
      - None or empty → [80]
      - Integer → [int]
      - String "80" → [80]
      - String "80,443,8080" → [80, 443, 8080]
      - String "80 443 8080" → [80, 443, 8080]

    Returns sorted unique list of valid ports (1-65535).
    """
    if value is None:
        return [80]
    if isinstance(value, int):
        return [value] if 1 <= value <= 65535 else [80]
    value_str = str(value).strip()
    if not value_str:
        return [80]

    parts = re.split(r"[,\s]+", value_str)
    ports = set()
    for raw_p in parts:
        p = raw_p.strip()
        if not p:
            continue
        try:
            port = int(p)
            if 1 <= port <= 65535:
                ports.add(port)
        except ValueError:
            continue

    return sorted(ports) if ports else [80]


def _apply_http_port_override(url: str, port: int | None) -> str:
    """
    Apply custom port to URL if URL has no explicit port.

    This is a request-port override (separate from certificate port).
    Moved to module level for reusability and testability.

    Args:
            url: The original URL (e.g., "https://example.com/path")
            port: Optional port override (e.g., 8443)

    Returns:
            URL with port applied if no explicit port was present
    """
    if not url or not port:
        return url
    try:
        split = urlsplit(url)
        if split.scheme not in ("http", "https"):
            return url
        # If URL already contains a port, do not override.
        if split.port is not None:
            return url
        default_port = 443 if split.scheme == "https" else 80
        if int(port) == default_port:
            return url
        host = split.hostname
        if not host:
            return url
        # Preserve optional userinfo
        userinfo = ""
        if split.username:
            userinfo = split.username
            if split.password:
                userinfo += ":" + split.password
            userinfo += "@"
        # IPv6 host must be bracketed
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{userinfo}{host}:{int(port)}"
        return urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))
    except ValueError:
        return url


# Jitter between multi-port TCP checks (milliseconds)
# Base jitter + random component to avoid synchronized hammering
TCP_PORT_JITTER_BASE_MS = 50
TCP_PORT_JITTER_MAX_MS = 200


@dataclass
class TargetRuntime:
    """In-memory runtime state for a target across scheduler iterations."""

    next_due: datetime
    last_overall_ok: bool | None = None
    last_uptime_calc_at: datetime | None = None
    # Per-service downtime tracking (prevents cross-check reset)
    down_since_by_service: dict[str, datetime | None] = field(
        default_factory=lambda: {
            "http": None,
            "ping": None,
            "tcp": None,
            "cert": None,
        }
    )
    current_alert_id: str | None = None
    failed_services: set[str] = field(default_factory=set)
    cert_alert_sent: bool = False  # True if cert expiry alert was already sent
    cert_alert_id: str | None = None  # UUID of the cert expiry alert (for resolution)
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    # Bounded deque: maxlen prevents unbounded memory growth during extreme flapping
    fail_timestamps: deque = field(default_factory=lambda: deque(maxlen=FAIL_THRESHOLD * 2))
    # Logging noise reduction: track last logged failure state per service
    last_logged_failures: set[str] = field(default_factory=set)


# Connectivity issue cooldown: wait before retrying checks after connectivity loss
CONNECTIVITY_COOLDOWN_SECONDS = 30

# Certificate expiration alert: days threshold (alert fires once when reached)
CERT_EXPIRY_ALERT_DAYS = 7

# Quality probe interval: run quality probe at this interval
QUALITY_PROBE_INTERVAL_SECONDS = 30

# Alert cooldown: minimum seconds between alerts for the same target (prevents flapping spam)
ALERT_COOLDOWN_SECONDS = 300  # 5 minutes

# ─────────────────────────────────────────────────────────────────────────────
# Alert Stability Thresholds (Hysteresis) - DIAGNOSTIC CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: These constants are used for diagnostics and debugging only.
# The actual alert decision logic uses the metrics_timeout_minutes setting,
# the down_since timestamp, and the fail_timestamps sliding window.
#
# The FSM behavior:
#   - DOWN alert: fires after MIN_DOWN_SECONDS of continuous failure
#   - RECOVERY alert: fires immediately when target becomes UP
#   - Quality Gate UNKNOWN: does NOT trigger alerts (neither DOWN nor RECOVERY)
#     If target was DOWN, then Quality Gate activates (UNKNOWN), then UP:
#     → NO recovery alert is sent (by design, to avoid false positives)
# ─────────────────────────────────────────────────────────────────────────────
FAIL_THRESHOLD = 3  # Cycles to confirm DOWN (diagnostic reference)
RECOVERY_THRESHOLD = 2  # Cycles to confirm RECOVERY (diagnostic reference)
MIN_DOWN_SECONDS = 120  # Minimum duration before DOWN alert
FAIL_WINDOW_SECONDS = 300  # Sliding window: FAILs must occur within this time to count


class MonitorScheduler:
    """Periodic scheduler that runs checks, writes metrics, and updates cached status."""

    def __init__(self, *, get_conn: Callable[[], Any], tsdb_dir, refresh_seconds: int = 30):
        self._get_conn = get_conn
        self._tsdb_dir = tsdb_dir
        self._refresh_seconds = max(5, int(refresh_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._targets: dict[int, dict[str, Any]] = {}
        self._rt: dict[int, TargetRuntime] = {}
        self._inflight: set[int] = set()  # Targets currently being checked
        # After the first target load, a target without runtime state is new: check it right away.
        # Targets present at startup stay staggered by their jitter offset instead.
        self._initial_load_done = False
        self._refresh_requested = False
        self._last_refresh = 0.0
        # Housekeeping: track next rollup time per target
        self._housekeeping_due: dict[int, datetime] = {}
        # Connectivity tracking with caching
        self._connectivity_ok: bool = True
        self._connectivity_lost_at: datetime | None = None
        self._connectivity_checked_at: datetime | None = None
        # Quality probe tracking
        self._last_quality_probe_at: datetime | None = None
        self._quality_state: QualityState = QualityState.OK
        # Alert rate-limiting: track last alert time per target (DOWN alerts only)
        self._last_alert_at: dict[int, datetime] = {}
        # Cached global settings (updated in _refresh_targets)
        self._metrics_timeout_minutes: int = 5
        # Notification dispatcher (central notification system)
        self._notification_dispatcher: NotificationDispatcher | None = None
        # Notification spooler: process queue every 10 seconds
        # (immediate=True messages are picked up quickly, but still serialized)
        self._last_spooler_run_at: datetime | None = None
        self._spooler_interval_seconds: int = 10
        # Scheduler run ID for idempotency (Task 1: crash-safe scheduler)
        self._run_id: str = ""
        self._last_heartbeat_at: datetime | None = None
        self._last_db_maintenance: datetime | None = None
        self._last_db_optimize: datetime | None = None
        self._heartbeat_interval_seconds: int = 30
        # Liveness hook for the service supervisor; called once per loop iteration.
        self.on_heartbeat: Callable[[], None] | None = None
        # Run epoch: a supervisor restart (e.g. after a hang) bumps it, so worker threads that
        # belong to the abandoned run refuse to start work and the new run waits for them.
        self._run_epoch = 0
        self._epoch_lock = threading.Lock()
        self._threads_active = 0
        self._stale_logged_epoch = -1

    @contextmanager
    def _db_conn(self):
        """Context manager for database connections. Ensures proper cleanup."""
        conn = self._get_conn()
        try:
            yield conn
        finally:
            with suppress(Exception):
                sqlite_db.close_connection(conn)

    def _get_dispatcher(self, conn) -> NotificationDispatcher:
        """Get or create NotificationDispatcher.

        The dispatcher enqueues all notifications into the spooler.
        It does NOT send directly — the spooler is the single exit point.
        """
        return NotificationDispatcher(
            conn=conn,
            db_conn_factory=self._get_conn,
        )

    def _register_run(self) -> None:
        """Register this scheduler run for idempotency and clean up runs left by a crash."""
        self._run_id = uuid.uuid4().hex
        try:
            with self._db_conn() as conn:
                sqlite_db.register_scheduler_run(conn, self._run_id)
                # Cleanup stale runs from previous crashes
                stale_count = sqlite_db.cleanup_stale_scheduler_runs(conn, timeout_seconds=300)
                if stale_count > 0:
                    _log.info("Cleaned up %d stale scheduler runs", stale_count)
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Failed to register scheduler run: %s", e)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._register_run()
            self._task = asyncio.create_task(self._run_loop())

    def _epoch_for_thread(self) -> int:
        """Epoch of the run the calling coroutine belongs to, never the (newer) current one."""
        epoch = _RUN_EPOCH.get()
        return self._run_epoch if epoch is None else epoch

    def _thread_call(self, epoch: int, callback):
        """Wrap thread work so it is refused once its run was replaced and is tracked until it exits."""

        def call(*args, **kwargs):
            with self._epoch_lock:
                stale = epoch != self._run_epoch
                if stale:
                    first = self._stale_logged_epoch != self._run_epoch
                    self._stale_logged_epoch = self._run_epoch
                else:
                    self._threads_active += 1
            if stale:
                if first:
                    _log.warning("service=monitor_scheduler event=stale_run_discarded run_epoch=%d", epoch)
                raise StaleRunError("Scheduler run was replaced")
            try:
                return callback(*args, **kwargs)
            finally:
                with self._epoch_lock:
                    self._threads_active -= 1

        return call

    async def _drain_old_threads(self, timeout: float = 30.0) -> None:
        """Give worker threads of an abandoned run time to finish before the new run starts."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            with self._epoch_lock:
                active = self._threads_active
            if active == 0:
                return
            if asyncio.get_running_loop().time() >= deadline:
                _log.warning(
                    "service=monitor_scheduler event=old_threads_still_running count=%d waited=%.0fs", active, timeout
                )
                return
            await asyncio.sleep(0.2)

    async def run(self) -> None:
        """Run the scheduler loop in the calling task; restartable entry point for the supervisor."""
        self._task = asyncio.current_task()
        with self._epoch_lock:
            self._run_epoch += 1
            _RUN_EPOCH.set(self._run_epoch)
        await self._drain_old_threads()
        # A cancelled iteration never reached its cleanup, so drop stale in-flight markers.
        self._inflight.clear()
        self._register_run()
        try:
            await self._run_loop()
        finally:
            self._task = None

    def request_stop(self) -> None:
        """Ask the loop to end without waiting for it."""
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            # wait() never re-raises the task's outcome (the supervisor owns and logs it).
            await asyncio.wait({task})

    def request_refresh(self) -> None:
        """Pick up new or changed targets on the next loop tick instead of the next refresh interval."""
        self._refresh_requested = True

    def forget_target(self, target_id: int) -> None:
        self._targets.pop(target_id, None)
        self._rt.pop(target_id, None)
        self._inflight.discard(target_id)
        self._housekeeping_due.pop(target_id, None)
        self._last_alert_at.pop(target_id, None)

    async def _run_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            if self.on_heartbeat is not None:
                self.on_heartbeat()
            try:
                generation = maintenance.current_generation()
                await self._run_iteration(loop, generation)
            except maintenance.MaintenanceActiveError:
                await asyncio.sleep(0.5)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - loop resilience: one bad iteration must not kill the scheduler
                _log.exception("Scheduler iteration failed; continuing")
            await self._idle_sleep()

    async def _run_iteration(self, loop: asyncio.AbstractEventLoop, generation: int | None = None) -> None:
        generation = maintenance.current_generation() if generation is None else generation
        now = utcnow()
        # Production safety: explicit check instead of assert (assert is stripped in -O mode)
        offset = now.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise RuntimeError("Scheduler requires UTC timestamps with timezone info")
        if self._refresh_requested or loop.time() - self._last_refresh >= self._refresh_seconds:
            with maintenance.work_section(generation):
                self._refresh_requested = False
                self._refresh_targets(now)
                self._last_refresh = loop.time()

        # Heartbeat to keep run active
        if (
            not self._last_heartbeat_at
            or (now - self._last_heartbeat_at).total_seconds() >= self._heartbeat_interval_seconds
        ):
            try:
                with maintenance.work_section(generation):
                    with self._db_conn() as conn:
                        sqlite_db.update_scheduler_heartbeat(conn, self._run_id)
                    self._last_heartbeat_at = now
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Failed to update heartbeat: %s", e)

        # Run quality probe (at configured interval)
        await self._run_quality_probe(now, generation)

        # IMPORTANT: Snapshot quality_state to avoid race condition.
        # _run_quality_probe may update _quality_state asynchronously.
        # By caching here, we ensure consistent behavior within this loop iteration.
        # This is "best effort" gating - a target may start checking just as
        # quality degrades, which is acceptable (check will complete normally).
        quality_state_snapshot = self._quality_state

        # Only run targets that are due AND not already inflight
        due_ids = [tid for tid, rt in self._rt.items() if rt.next_due <= now and tid not in self._inflight]
        if due_ids:
            # QUALITY GATE: Check quality state before running checks
            if quality_state_snapshot != QualityState.OK:
                # Quality degraded or down -> skip target checks
                # Parallelized for scalability (only DB writes + reschedule, no semantic dependencies)
                await asyncio.gather(*(self._handle_connectivity_issue(tid, now, generation) for tid in due_ids))
            else:
                # Quality OK, run normal checks
                for tid in due_ids:
                    self._inflight.add(tid)
                results = await asyncio.gather(
                    *(self._run_target(tid, now, generation) for tid in due_ids),
                    return_exceptions=True,
                )
                # Log any exceptions and remove from inflight
                for tid, result in zip(due_ids, results, strict=False):
                    self._inflight.discard(tid)
                    if isinstance(result, Exception):
                        _log.error("Target %s check failed: %s", tid, result, exc_info=result)

        # Run housekeeping for targets that are due
        await self._run_housekeeping(now, generation)

        # Process notification spooler queue (retries for failed notifications)
        await self._process_notification_spooler(now, generation)

    async def _idle_sleep(self) -> None:
        # Adaptive sleep: wait until next target is due (min 0.5s, max 1.0s)
        # More efficient than fixed 1s polling for large target counts
        if self._rt:
            now_ts = utcnow()
            sleep_for = min(max((rt.next_due - now_ts).total_seconds(), 0.5) for rt in self._rt.values())
            await asyncio.sleep(min(sleep_for, 1.0))
        else:
            await asyncio.sleep(1.0)

    async def _run_quality_probe(self, now: datetime, generation: int | None = None) -> None:
        """
        Run the quality probe at configured intervals.

        Stores the result in SQLite and updates the in-memory quality state.
        """
        generation = maintenance.current_generation() if generation is None else generation
        # Check if probe is due
        if self._last_quality_probe_at is not None:
            elapsed = (now - self._last_quality_probe_at).total_seconds()
            if elapsed < QUALITY_PROBE_INTERVAL_SECONDS:
                return

        try:
            result: QualityResult = await run_quality_probe()

            with maintenance.work_section(generation):
                # Store the probe result only if its data generation is still current.
                with self._db_conn() as conn:
                    sqlite_db.insert_monitor_quality(
                        conn,
                        score=result.score,
                        median_latency_ms=result.median_latency_ms,
                        success_ratio=result.success_ratio,
                        state=result.state,
                    )
                    conn.commit()

                old_state = self._quality_state
                self._quality_state = result.state  # result.state is already a string
                self._last_quality_probe_at = now

            # Log state transitions
            if old_state != result.state:
                if result.state == QualityState.OK:
                    _log.info("Quality gate: state recovered to OK (score=%.1f)", result.score or 0)
                else:
                    _log.warning(
                        "Quality gate: state changed to %s (score=%s, success_ratio=%.0f%%)",
                        result.state,
                        result.score or "N/A",
                        result.success_ratio * 100,
                    )

            # Update connectivity tracking for backward compatibility
            self._connectivity_ok = result.state == QualityState.OK
            if not self._connectivity_ok and self._connectivity_lost_at is None:
                self._connectivity_lost_at = now
            elif self._connectivity_ok:
                self._connectivity_lost_at = None

        except maintenance.MaintenanceActiveError:
            raise
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Quality probe failed: %s", e)
            # On probe failure, assume degraded to be safe
            self._quality_state = QualityState.DEGRADED
            self._last_quality_probe_at = now

    async def _process_notification_spooler(self, now: datetime, generation: int | None = None) -> None:
        """
        Process the notification spooler queue (retries for failed notifications).

        Runs at _spooler_interval_seconds (default 10s) to avoid hammering.
        """
        generation = maintenance.current_generation() if generation is None else generation
        # Check if spooler run is due
        if self._last_spooler_run_at is not None:
            elapsed = (now - self._last_spooler_run_at).total_seconds()
            if elapsed < self._spooler_interval_seconds:
                return

        try:
            spooler = get_spooler(self._get_conn)

            # Build send functions for the spooler
            def send_signal(phone: str, message: str, sender: str) -> bool:
                """Send Signal message, returns True on success."""
                try:
                    backend = get_signal_backend()
                    backend.send_message(phone, message, sender)
                    return True
                except Exception as e:
                    _log.warning("Spooler: Signal send failed: %s", e)
                    raise

            def send_email(
                to_addr: str, subject: str, text_body: str, html_body: str, message_id_key: str | None = None
            ) -> bool:
                """Send email, returns True on success.

                Routes through the shared smtp_transport so the DECISIONS B6
                encryption rules apply identically to this spooler path and the
                EmailSender channel (implicit TLS on 465, mandatory STARTTLS
                otherwise, AUTH only over a protected connection).
                """
                from email.message import EmailMessage

                # Settings read is a short DB phase; the SMTP I/O below runs outside any section.
                with maintenance.work_section(generation), self._db_conn() as conn:
                    smtp_enabled = sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False)
                    if not smtp_enabled:
                        raise RuntimeError("SMTP not enabled")

                    smtp_host = sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST)
                    smtp_port = sqlite_db.get_setting(conn, SettingKeys.SMTP_PORT, 587)
                    smtp_tls = sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, False)
                    smtp_user = sqlite_db.get_setting(conn, SettingKeys.SMTP_USER)
                    smtp_pass_enc = sqlite_db.get_setting(conn, SettingKeys.SMTP_PASSWORD)
                    smtp_from = sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM)

                    if not smtp_host or not smtp_from:
                        raise RuntimeError("SMTP not configured")

                    smtp_pass = None
                    if smtp_pass_enc:
                        with suppress(Exception):
                            smtp_pass = decrypt_secret(smtp_pass_enc)

                try:
                    smtp_port = int(smtp_port)
                except (TypeError, ValueError):
                    smtp_port = 587

                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = smtp_from
                msg["To"] = to_addr
                if message_id_key:
                    # Same queue row -> same Message-ID on every retry, so receivers can drop duplicates.
                    domain = str(smtp_from).rpartition("@")[2].strip(" >") or "justup.invalid"
                    msg["Message-ID"] = f"<{message_id_key}@{domain}>"
                msg.set_content(text_body)
                if html_body:
                    msg.add_alternative(html_body, subtype="html")

                config = smtp_transport.SMTPConnection(
                    host=smtp_host,
                    port=smtp_port,
                    user=smtp_user,
                    password=smtp_pass,
                    use_tls=bool(smtp_tls),
                )
                smtp_transport.send_message(msg, config, timeout=30)

                return True

            # Process queue in a worker thread (SMTP/Signal I/O blocks). Only the claim and finalize
            # DB phases run in generation-tagged work sections; the network send runs outside, so a
            # restore never waits for it and a finalize after a restore is rejected as stale.
            processed, succeeded = await asyncio.to_thread(
                self._thread_call(self._epoch_for_thread(), spooler.process_queue_sync),
                send_signal,
                send_email,
                section=lambda: maintenance.work_section(generation),
                may_send=lambda: not maintenance.is_active() and maintenance.current_generation() == generation,
            )

            if processed > 0:
                _log.info(
                    "Spooler processed %d notifications (%d succeeded, %d failed/retried)",
                    processed,
                    succeeded,
                    processed - succeeded,
                )

            # Retention of sent/failed entries runs in the hourly DB maintenance, not on every spooler pass.

        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Notification spooler error: %s", e)

        self._last_spooler_run_at = now

    async def _handle_connectivity_issue(self, target_id: int, now: datetime, generation: int | None = None) -> None:
        """
        Handle a target check when connectivity is down.

        Does NOT record the target as "down" (that would be incorrect).
        Instead, sets status to "unknown" and reschedules.
        """
        generation = maintenance.current_generation() if generation is None else generation
        cfg = self._targets.get(target_id)
        if not cfg:
            return

        rt = self._rt.get(target_id)
        if not rt:
            return

        # Reschedule for after cooldown period
        rt.next_due = now + timedelta(seconds=CONNECTIVITY_COOLDOWN_SECONDS)

        # If we can't run checks due to connectivity/quality gate, do not let
        # "down since" timers accumulate (prevents false timeout-based alerts).
        if rt.current_alert_id is None:
            for k in rt.down_since_by_service:
                rt.down_since_by_service[k] = None
            rt.fail_timestamps.clear()
            rt.consecutive_failures = 0
            rt.consecutive_successes = 0

        if not cfg["is_enabled"]:
            return

        # Update status cache to reflect connectivity issue (NOT down!)
        try:
            with maintenance.work_section(generation), self._db_conn() as conn:
                existing = sqlite_db.get_target_status(conn, target_id)
                uptime_percent = existing["uptime_percent"] if (existing and "uptime_percent" in existing) else None

                sqlite_db.set_target_status_snapshot(
                    conn,
                    target_id,
                    ping_up=None,
                    ping_latency_ms=None,
                    http_up=None,
                    http_status=None,
                    http_latency_ms=None,
                    tcp_up=None,
                    tcp_latency_ms=None,
                    cert_days_left=None,
                    uptime_percent=uptime_percent,
                    overall_status=TargetStatus.UNKNOWN,  # Already str constant
                )
                conn.commit()
                event_bus.publish("target.status", {"target_id": target_id, "status": "unknown"})
        except maintenance.MaintenanceActiveError:
            return
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Failed to set unknown status for target %s (connectivity issue): %s", target_id, e)

    def _refresh_targets(self, now: datetime) -> None:
        with self._db_conn() as conn:
            rows = sqlite_db.list_targets(conn)
            try:
                mt = sqlite_db.get_setting(conn, SettingKeys.METRICS_TIMEOUT_MINUTES, 5) or 5
                self._metrics_timeout_minutes = max(1, min(10, int(mt)))
            except (ValueError, TypeError):
                self._metrics_timeout_minutes = 5

            # Load open alerts for all targets (idempotency: restore state on restart)
            # Use list to support multiple alerts per target (cert + availability)
            # NOTE: Use regular dict with setdefault().
            open_alerts: dict[int, list[dict]] = {}
            try:
                alert_rows = sqlite_db.list_open_alerts(conn)
                for row in alert_rows:
                    tid = row[0]
                    started_at = row[2]
                    # Handle both string and datetime types from SQLite
                    if started_at:
                        if isinstance(started_at, str):
                            started_at = datetime.fromisoformat(started_at).replace(tzinfo=UTC)
                        elif isinstance(started_at, datetime):
                            started_at = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at
                        else:
                            started_at = None
                    open_alerts.setdefault(tid, []).append(
                        {
                            "alert_id": row[1],
                            "started_at": started_at,
                            "failed_service": row[3],
                        }
                    )
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Failed to load open alerts: %s", e)

        seen: set[int] = set()
        for r in rows:
            tid = int(r["id"])
            seen.add(tid)
            interval = int(r["interval_seconds"])
            cfg = {
                "id": tid,
                "name": r["name"],
                "is_enabled": bool(r["is_enabled"]),
                "interval_seconds": interval,
                "retention_days": int(r["retention_days"]),
                "_updated_at": r["updated_at"],
                "enable_ping": bool(r["enable_ping"]),
                "enable_http_check": bool(r["enable_http_check"]),
                "enable_cert_expiration": bool(r["enable_cert_expiration"]),
                "enable_tcp_connect": bool(r["enable_tcp_connect"]),
                "ping_host": r["ping_host"],
                "http_url": r["http_url"],
                "http_username": r["http_username"],
                "http_password": _decrypt_password(r["http_password"]),
                "http_prefer_head": bool(r["http_prefer_head"]),
                "http_basic_auth_enabled": bool(r["http_basic_auth_enabled"]),
                "http_success_codes": r["http_success_codes"] or None,
                "http_port": int(r["http_port"]) if r["http_port"] else None,
                "cert_host": r["cert_host"],
                "cert_port": int(r["cert_port"]) if r["cert_port"] else None,
                "ignore_cert_errors": bool(r["ignore_cert_errors"]),
                "host": r["host"],
                "tcp_host": r["tcp_host"],
                "tcp_ports": _parse_tcp_ports(r["tcp_ports"] or "80"),
                "max_retries": int(r["max_retries"]),
                "auto_close_alert": bool(r["auto_close_alert"]),
                "_jitter_offset": _stable_jitter_offset(tid),
            }
            self._targets[tid] = cfg
            if tid not in self._rt:
                offset = cfg["_jitter_offset"]
                if self._initial_load_done:
                    # Created while running: first measurement right away, then the aligned schedule
                    next_due = now
                else:
                    # Startup: align to next interval boundary + jitter to spread the load
                    next_due = max(_align_to_interval(now, interval, offset), now)
                rt = TargetRuntime(next_due=next_due)

                # Restore open alert state from database (idempotency on restart)
                if tid in open_alerts:
                    for alert_info in open_alerts[tid]:
                        failed_service = alert_info["failed_service"] or ""

                        # Distinguish between cert alerts and availability alerts
                        if "CERT" in failed_service.upper():
                            # Cert expiry alert
                            rt.cert_alert_sent = True
                            rt.cert_alert_id = alert_info["alert_id"]
                            _log.info(
                                "Restored cert alert %s for target %d",
                                rt.cert_alert_id,
                                tid,
                            )
                        else:
                            # Availability alert
                            rt.current_alert_id = alert_info["alert_id"]
                            # Restore down_since for all services (conservative: we don't know
                            # which specific service was down, so mark overall start time)
                            started = alert_info["started_at"]
                            if started:
                                for k in rt.down_since_by_service:
                                    rt.down_since_by_service[k] = started
                            rt.failed_services = {failed_service} if failed_service else set()
                            # Compute earliest down time for logging
                            earliest_down = min(
                                (s for s in rt.down_since_by_service.values() if s),
                                default=None,
                            )
                            _log.info(
                                "Restored open alert %s for target %d (down since %s)",
                                rt.current_alert_id,
                                tid,
                                earliest_down,
                            )

                self._rt[tid] = rt
                _log.debug("Target %s scheduled at %s (offset=%ds)", tid, next_due, offset)

        self._initial_load_done = True

        # remove deleted
        for tid in list(self._targets.keys()):
            if tid not in seen:
                self.forget_target(tid)

    @staticmethod
    def _target_matches_snapshot(conn, target_id: int, cfg: dict[str, Any]) -> bool:
        current = sqlite_db.get_target(conn, target_id)
        if current is None:
            return False
        expected_updated_at = cfg.get("_updated_at")
        return expected_updated_at is None or current["updated_at"] == expected_updated_at

    async def _run_target(self, target_id: int, now: datetime, generation: int | None = None) -> None:
        generation = maintenance.current_generation() if generation is None else generation
        cfg = self._targets.get(target_id)
        if not cfg:
            return

        interval = max(5, int(cfg["interval_seconds"]))
        offset = cfg.get("_jitter_offset", 0)
        ttl_seconds = interval * 2

        rt = self._rt.get(target_id)
        if not rt:
            self._rt[target_id] = TargetRuntime(next_due=now)
            rt = self._rt[target_id]

        # Try to acquire lock (idempotency: prevent duplicate checks on restart)
        lock_acquired = False
        try:
            with maintenance.work_section(generation), self._db_conn() as conn:
                if not self._target_matches_snapshot(conn, target_id, cfg):
                    self._refresh_requested = True
                    return
                lock_acquired = sqlite_db.try_lock_target(conn, target_id, self._run_id, ttl_seconds)
        except maintenance.MaintenanceActiveError:
            return
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Failed to acquire lock for target %d: %s", target_id, e)

        if not lock_acquired:
            # Already locked by another scheduler or recent run
            _log.debug("Target %d is locked, skipping check", target_id)
            rt.next_due = _align_to_interval(utcnow(), interval, offset)
            return

        try:

            def _log_check_failure(
                service: str, message: str, attempt: int, max_retries: int, level: str = "warning"
            ) -> None:
                """
                Log check failure only on state change (reduces noise for persistent failures).

                Args:
                        service: Service identifier (e.g., "ping", "http", "tcp_443")
                        message: Log message
                        attempt: Current retry attempt (0-based)
                        max_retries: Maximum retries configured
                        level: Log level ("warning" or "debug")
                """
                # Add retry information to message
                retry_info = f"[{attempt + 1}/{max_retries + 1}]"
                message_with_retry = f"{retry_info} {message}"

                failure_key = f"{service}:{message}"
                is_new_failure = failure_key not in rt.last_logged_failures

                if is_new_failure:
                    # State change: log at warning level
                    if level == "warning":
                        _log.warning(message_with_retry)
                    else:
                        _log.debug(message_with_retry)
                    rt.last_logged_failures.add(failure_key)
                else:
                    # Repeated failure: log at debug only
                    _log.debug("%s (repeated)", message_with_retry)

            if not cfg["is_enabled"]:
                rt.next_due = _align_to_interval(utcnow(), interval, offset)
                return

            max_retries = int(cfg.get("max_retries", 5))
            cert_host: str | None = None

            snapshot: dict[str, Any] = {
                "ping_up": None,
                "ping_latency_ms": None,
                "http_up": None,
                "http_status": None,
                "http_latency_ms": None,
                "tcp_up": None,
                "tcp_latency_ms": None,
                "cert_days_left": None,
            }

            for attempt in range(max_retries + 1):
                # If target was deleted while this task is running, abort to avoid writing new metrics
                if target_id not in self._targets:
                    return

                availability_results: list[bool] = []
                timeout_seen = False

                # Ping
                if cfg["enable_ping"]:
                    p_host = str(cfg.get("ping_host") or cfg.get("host") or "")
                    res = None
                    if not p_host:
                        _log.warning("Target %s: ping enabled but no host configured", target_id)
                        ping_ok = False
                    else:
                        # Strip www. prefix for ICMP ping (many servers don't respond to www. pings)
                        if p_host.lower().startswith("www."):
                            p_host = p_host[4:]
                        res = await checks.ping(p_host)
                        ping_ok = bool(res.ok)
                        if not ping_ok:
                            if res and checks.is_retryable_detail(res.detail):
                                timeout_seen = True
                            _log_check_failure(
                                "ping",
                                f"PING failed for target {target_id} ({p_host}): {res.detail}",
                                attempt,
                                max_retries,
                            )
                        else:
                            # Clear logged failures on success
                            rt.last_logged_failures = {k for k in rt.last_logged_failures if not k.startswith("ping:")}
                    availability_results.append(ping_ok)
                    lat_ms = float(res.latency_ms) if (res and res.latency_ms is not None) else None
                    snapshot["ping_up"] = 1 if ping_ok else 0
                    snapshot["ping_latency_ms"] = lat_ms

                # HTTP Check
                if cfg["enable_http_check"]:
                    h_url = str(cfg.get("http_url") or "")
                    if not h_url and cfg.get("host"):
                        h_url = f"https://{cfg['host']}/"
                    # IMPORTANT: honor configured custom port (e.g. 8443) if URL didn't include it
                    h_url = _apply_http_port_override(h_url, cfg.get("http_port"))
                    res = None
                    if not h_url:
                        _log.warning("Target %s: http enabled but no URL configured", target_id)
                        http_ok = False
                    else:
                        basic_auth = None
                        if cfg.get("http_basic_auth_enabled") and cfg.get("http_username") and cfg.get("http_password"):
                            basic_auth = (str(cfg["http_username"]), str(cfg["http_password"]))
                        # Use HEAD by default (avoids affecting visitor counters), unless disabled
                        prefer_head = cfg.get("http_prefer_head", True)
                        res = await checks.http_head(
                            h_url,
                            basic_auth=basic_auth,
                            prefer_head=prefer_head,
                            success_status_codes=cfg.get("http_success_codes"),
                            ignore_cert_errors=cfg.get("ignore_cert_errors", False),
                            target_id=target_id,
                        )
                        http_ok = bool(res.ok) if res else False
                        if not http_ok:
                            if res and checks.is_retryable_detail(res.detail):
                                timeout_seen = True
                            status = None
                            if res and isinstance(res.value, dict) and "status" in res.value:
                                status = res.value.get("status")
                            detail = (
                                res.detail
                                if (res and res.detail)
                                else (f"HTTP {status}" if status is not None else "Unknown error")
                            )
                            _log_check_failure(
                                "http",
                                f"HTTP failed for target {target_id} ({redact_url(h_url)}): {detail}",
                                attempt,
                                max_retries,
                            )
                        else:
                            # Clear logged failures on success
                            rt.last_logged_failures = {k for k in rt.last_logged_failures if not k.startswith("http:")}
                    availability_results.append(http_ok)
                    lat_ms = float(res.latency_ms) if (res and res.latency_ms is not None) else None
                    http_code = None
                    if res and isinstance(res.value, dict) and "status" in res.value:
                        http_code = int(res.value["status"])
                    snapshot["http_up"] = 1 if http_ok else 0
                    snapshot["http_latency_ms"] = lat_ms
                    snapshot["http_status"] = http_code

                # Cert Expiration (informational only)
                snapshot["cert_days_left"] = None
                cert_host = None
                cert_port = 443
                if cfg["enable_cert_expiration"] and cfg.get("http_url"):
                    try:
                        parsed = urlparse(str(cfg["http_url"]))
                        cert_host = parsed.hostname
                        if parsed.port:
                            cert_port = parsed.port
                        elif cfg.get("http_port"):
                            cert_port = int(cfg["http_port"])
                        elif cfg.get("cert_port"):
                            # Legacy (deprecated): cert_port used to double as request port
                            cert_port = int(cfg["cert_port"])
                        else:
                            cert_port = 443 if parsed.scheme == "https" else 80
                    except (ValueError, KeyError) as e:
                        _log.warning("Target %s: failed to parse http_url for cert check: %s", target_id, e)
                if not cert_host and cfg["enable_cert_expiration"]:
                    cert_host = cfg.get("host")
                if cert_host:
                    res = await checks.cert_expiration(
                        cert_host,
                        cert_port,
                        ignore_cert_errors=cfg.get("ignore_cert_errors", False),
                    )
                    if isinstance(res.value, dict) and "remaining_days" in res.value:
                        days_left = int(res.value["remaining_days"])
                        snapshot["cert_days_left"] = days_left
                        _log.debug("Target %s: cert check OK, %d days left", target_id, days_left)
                    else:
                        _log.warning(
                            "Target %s: cert check failed for %s:%d - %s",
                            target_id,
                            cert_host,
                            cert_port,
                            res.detail if res else "no result",
                        )
                elif cfg["enable_cert_expiration"]:
                    _log.warning("Target %s: cert check enabled but no host could be determined", target_id)

                # TCP Connect (Multi-Port)
                if cfg["enable_tcp_connect"]:
                    t_host = str(cfg.get("tcp_host") or cfg.get("host") or "")
                    tcp_ports: list[int] = cfg.get("tcp_ports") or [80]

                    if not t_host:
                        _log.warning("Target %s: tcp enabled but no host configured", target_id)
                        for port in tcp_ports:
                            snapshot[f"tcp_up_{port}"] = 0
                            snapshot[f"tcp_ms_{port}"] = None
                        snapshot["tcp_port_status"] = json.dumps({str(port): "down" for port in tcp_ports})
                        snapshot["tcp_up"] = 0
                        snapshot["tcp_latency_ms"] = None
                        availability_results.append(False)
                    else:
                        all_ports_ok = True
                        total_latency_ms = 0.0
                        latency_count = 0
                        num_ports = len(tcp_ports)

                        for i, port in enumerate(tcp_ports):
                            if i > 0:
                                jitter_ms = TCP_PORT_JITTER_BASE_MS + random.randint(  # noqa: S311 - scheduling jitter, not security-sensitive
                                    0,
                                    min(TCP_PORT_JITTER_MAX_MS, 50 * num_ports),
                                )
                                await asyncio.sleep(jitter_ms / 1000.0)

                            res = await checks.tcp_connect(t_host, port)
                            port_ok = bool(res.ok)
                            lat_ms = float(res.latency_ms) if (res and res.latency_ms is not None) else None

                            snapshot[f"tcp_up_{port}"] = 1 if port_ok else 0
                            snapshot[f"tcp_ms_{port}"] = lat_ms

                            if not port_ok:
                                if res and checks.is_retryable_detail(res.detail):
                                    timeout_seen = True
                                all_ports_ok = False
                                _log_check_failure(
                                    f"tcp_{port}",
                                    f"TCP failed for target {target_id} ({t_host}:{port}): "
                                    f"{res.detail or 'connection failed'}",
                                    attempt,
                                    max_retries,
                                )
                            else:
                                _log.debug("Target %s: TCP port %d OK (%.1fms)", target_id, port, lat_ms or 0)
                                # Clear logged failures on success
                                rt.last_logged_failures = {
                                    k for k in rt.last_logged_failures if not k.startswith(f"tcp_{port}:")
                                }
                                if lat_ms is not None:
                                    total_latency_ms += lat_ms
                                    latency_count += 1

                        snapshot["tcp_port_status"] = json.dumps(
                            {
                                str(port): PortStatus.UP if snapshot.get(f"tcp_up_{port}") == 1 else PortStatus.DOWN
                                for port in tcp_ports
                            }
                        )
                        snapshot["tcp_up"] = 1 if all_ports_ok else 0
                        snapshot["tcp_latency_ms"] = (total_latency_ms / latency_count) if latency_count > 0 else None
                        availability_results.append(all_ports_ok)

                # Determine overall_ok explicitly: None if no checks, else all must pass
                if not availability_results:
                    # No checks enabled → will be marked as "disabled" downstream
                    overall_ok = None
                else:
                    overall_ok = all(availability_results)

                if overall_ok:
                    break
                # Only retry on timeouts (spec: timeout -> exponential retries)
                if attempt >= max_retries or not timeout_seen:
                    break

                backoff = min(2 ** (attempt + 1), MAX_BACKOFF_SECONDS)
                _log.debug(
                    "Target %s: timeout retry attempt %d/%d after %ds",
                    target_id,
                    attempt + 1,
                    max_retries,
                    backoff,
                )
                await asyncio.sleep(backoff)

            # If target was deleted while this task is running, abort to avoid writing new metrics
            if target_id not in self._targets:
                return

            # Write-once policy: persist at most one point per metric per interval
            try:
                with maintenance.work_section(generation):
                    with self._db_conn() as conn:
                        if not self._target_matches_snapshot(conn, target_id, cfg):
                            self._refresh_requested = True
                            return

                    def put(metric: str, value: int | float) -> None:
                        tsdb_db.append_point(
                            self._tsdb_dir,
                            target_id=target_id,
                            metric=metric,
                            value=value,
                            retention_days=int(cfg["retention_days"]),
                        )

                    if cfg["enable_ping"] and snapshot["ping_up"] is not None:
                        put("ping_up", int(snapshot["ping_up"]))
                        if snapshot["ping_latency_ms"] is not None:
                            put("ping_ms", float(snapshot["ping_latency_ms"]))

                    if cfg["enable_http_check"] and snapshot["http_up"] is not None:
                        put("http_up", int(snapshot["http_up"]))
                        if snapshot["http_latency_ms"] is not None:
                            put("http_ms", float(snapshot["http_latency_ms"]))
                        if snapshot["http_status"] is not None:
                            put("http_status", int(snapshot["http_status"]))

                    if cfg["enable_tcp_connect"] and snapshot["tcp_up"] is not None:
                        put("tcp_up", int(snapshot["tcp_up"]))
                        if snapshot["tcp_latency_ms"] is not None:
                            put("tcp_ms", float(snapshot["tcp_latency_ms"]))

                        for port in cfg.get("tcp_ports") or [80]:
                            port_up = snapshot.get(f"tcp_up_{port}")
                            port_ms = snapshot.get(f"tcp_ms_{port}")
                            if port_up is not None:
                                put(f"tcp_up_{port}", int(port_up))
                            if port_ms is not None:
                                put(f"tcp_ms_{port}", float(port_ms))

                    if cfg["enable_cert_expiration"] and snapshot["cert_days_left"] is not None:
                        put("cert_days_left", float(snapshot["cert_days_left"]))
            except maintenance.MaintenanceActiveError:
                return
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Failed to write TSDB metrics for target %s: %s", target_id, e)

            # Cert expiry alert (threshold and recovery handled in _maybe_cert_alert)
            if cfg["enable_cert_expiration"] and snapshot.get("cert_days_left") is not None and cert_host:
                await self._maybe_cert_alert(
                    target_id=target_id,
                    target_name=str(cfg["name"]),
                    days_left=int(snapshot["cert_days_left"]),
                    cert_host=str(cert_host),
                    generation=generation,
                )

            def _overall_status_from_snapshot() -> str:
                checks_for_status: list[int | None] = []
                if cfg["enable_http_check"]:
                    checks_for_status.append(snapshot.get("http_up"))
                if cfg["enable_ping"]:
                    checks_for_status.append(snapshot.get("ping_up"))
                if cfg["enable_tcp_connect"]:
                    checks_for_status.append(snapshot.get("tcp_up"))
                if not checks_for_status:
                    return TargetStatus.DISABLED  # Already str constant
                if any(v is None for v in checks_for_status):
                    return TargetStatus.UNKNOWN  # Already str constant
                if any(v != 1 for v in checks_for_status):
                    return TargetStatus.DOWN  # Already str constant
                return TargetStatus.UP  # Already str constant

            async def _compute_uptime_percent(hours: int = 24) -> float | None:
                """Compute uptime percentage for the given time window."""
                leading = None
                if cfg["enable_http_check"]:
                    leading = "http_up"
                elif cfg["enable_ping"]:
                    leading = "ping_up"
                elif cfg["enable_tcp_connect"]:
                    leading = "tcp_up"
                if not leading:
                    return None

                # Query all data points from the specified time window for a meaningful
                # uptime percentage.  The old code used only the last ~10 points
                # (5-minute window) which produced jumpy, implausible values
                # (100% → 90% → 100%) because a single failure in 10 checks
                # already equals 10%.  Using 24h/30d matches the UI labels
                # and gives smooth, accurate percentages.
                since_ts = now - timedelta(hours=hours)

                points = await asyncio.to_thread(
                    tsdb_db.query,
                    self._tsdb_dir,
                    target_id=target_id,
                    metric=leading,
                    since=since_ts,
                    limit=500_000,
                    latest=True,
                )
                if not points:
                    return None
                valid = [p for p in points if p.value in (0, 1)]
                if not valid:
                    return None
                total_checks = float(len(valid))
                up_checks = float(sum(1 for p in valid if p.value == 1))
                return (up_checks / total_checks * 100.0) if total_checks > 0 else None

            overall_status = _overall_status_from_snapshot()
            try:
                uptime_percent: float | None = None
                uptime_percent_30d: float | None = None
                calculate_uptime = rt.last_uptime_calc_at is None or (now - rt.last_uptime_calc_at) >= timedelta(
                    minutes=5
                )
                if calculate_uptime:
                    uptime_percent = await _compute_uptime_percent(hours=24)
                    uptime_percent_30d = await _compute_uptime_percent(hours=24 * 30)

                # --- per-service down-since tracking ---
                now_ts = now

                def _update_service(service: str, is_ok: int | None) -> None:
                    if is_ok == 1:
                        rt.down_since_by_service[service] = None
                    elif is_ok == 0 and rt.down_since_by_service[service] is None:
                        rt.down_since_by_service[service] = now_ts

                # Keep network checks and TSDB reads outside the restore guard. Revalidate the target
                # snapshot before writing so a target changed during the network checks is not updated
                # with a result from its old configuration.
                with maintenance.work_section(generation), self._db_conn() as conn:
                    if not self._target_matches_snapshot(conn, target_id, cfg):
                        self._refresh_requested = True
                        return
                    if not calculate_uptime:
                        row = sqlite_db.get_target_status(conn, target_id)
                        uptime_percent = row["uptime_percent"] if (row and "uptime_percent" in row) else None
                        uptime_percent_30d = (
                            row["uptime_percent_30d"] if (row and "uptime_percent_30d" in row) else None
                        )
                    if cfg["enable_http_check"]:
                        _update_service("http", snapshot.get("http_up"))
                    if cfg["enable_ping"]:
                        _update_service("ping", snapshot.get("ping_up"))
                    if cfg["enable_tcp_connect"]:
                        _update_service("tcp", snapshot.get("tcp_up"))
                    if cfg["enable_cert_expiration"] and snapshot.get("cert_days_left") is not None:
                        _update_service("cert", 1)
                    if calculate_uptime and uptime_percent is not None:
                        rt.last_uptime_calc_at = now
                    sqlite_db.set_target_status_snapshot(
                        conn,
                        target_id,
                        ping_up=snapshot["ping_up"],
                        ping_latency_ms=snapshot["ping_latency_ms"],
                        http_up=snapshot["http_up"],
                        http_status=snapshot["http_status"],
                        http_latency_ms=snapshot["http_latency_ms"],
                        tcp_up=snapshot["tcp_up"],
                        tcp_latency_ms=snapshot["tcp_latency_ms"],
                        tcp_port_status=snapshot.get("tcp_port_status"),
                        cert_days_left=snapshot["cert_days_left"],
                        uptime_percent=uptime_percent,
                        uptime_percent_30d=uptime_percent_30d,
                        overall_status=overall_status,
                    )
                    conn.commit()
                event_bus.publish("target.status", {"target_id": target_id, "status": str(overall_status)})
            except maintenance.MaintenanceActiveError:
                return
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Failed to update status cache for target %s: %s", target_id, e)

            # Points are already appended to the TSDB here, so charts are stale even if the status write failed.
            event_bus.publish("target.measurement", {"target_id": target_id})

            overall_ok = overall_status == TargetStatus.UP
            if overall_status != TargetStatus.DISABLED:
                await self._maybe_alert(
                    target_id=target_id,
                    target_name=str(cfg["name"]),
                    overall_ok=overall_ok,
                    overall_status=overall_status,
                    generation=generation,
                )

        finally:
            rt.next_due = _align_to_interval(utcnow(), interval, offset)
            await self._release_target_lock(target_id)

    async def _release_target_lock(self, target_id: int) -> None:
        """Release a check lock after maintenance, using the database's current generation."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 35.0
        while True:
            try:
                with maintenance.work_section(), self._db_conn() as conn:
                    sqlite_db.release_target_lock(conn, target_id, self._run_id)
                return
            except maintenance.MaintenanceActiveError:
                if loop.time() >= deadline:
                    _log.warning("Could not release target %d lock before maintenance wait expired", target_id)
                    return
                await asyncio.sleep(0.05)
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Failed to release lock for target %d: %s", target_id, e)
                return

    async def _maybe_alert(
        self,
        *,
        target_id: int,
        target_name: str,
        overall_ok: bool,
        overall_status: str,
        generation: int | None = None,
    ) -> None:
        """
        Alert state machine for DOWN/RECOVERY detection.

        Behavior (per spec):
        - Checks run on the configured interval (default: 60s).
        - Within the global metrics timeout window (default: 5m), at least one full OK
          (all enabled checks OK) must occur to avoid an alarm.
        - If no full OK occurs within that window, a DOWN alert is emitted once.
        - The first subsequent full OK emits RESOLVED and monitoring starts over.

        Notes:
        - Never alert on 'unknown' (quality gate / connectivity issues).
        - A lightweight sliding window is kept for counters/debugging only.
        """
        generation = maintenance.current_generation() if generation is None else generation
        with maintenance.work_section(generation):
            rt = self._rt.get(target_id)
            if not rt:
                self._rt[target_id] = TargetRuntime(next_due=utcnow(), last_overall_ok=overall_ok)
                rt = self._rt[target_id]

            # Never alert on 'unknown' status (connectivity/quality gate).
            if overall_status == TargetStatus.UNKNOWN:  # Already str constant
                return

            now = utcnow()
            metrics_timeout_seconds = int(max(1, self._metrics_timeout_minutes)) * 60

            # --- per-service alert decision ---
            alerting_services: set[str] = set()
            for svc, since in rt.down_since_by_service.items():
                if since and (now - since).total_seconds() >= metrics_timeout_seconds:
                    alerting_services.add(svc)

            # Maintain lightweight history for counters/debugging
            while rt.fail_timestamps and (now - rt.fail_timestamps[0]).total_seconds() > FAIL_WINDOW_SECONDS:
                rt.fail_timestamps.popleft()

            if not alerting_services:
                rt.fail_timestamps.clear()
                rt.consecutive_failures = 0
                rt.consecutive_successes += 1
            else:
                rt.fail_timestamps.append(now)
                rt.consecutive_successes = 0
                rt.consecutive_failures = len(rt.fail_timestamps)

            if not rt.current_alert_id:
                if not alerting_services:
                    return
            # update while alert open (only when services are still failing)
            elif alerting_services:
                rt.failed_services = alerting_services

            # --- Execution ---
            rt.last_overall_ok = not bool(alerting_services)

            # Rate-limit DOWN alerts to prevent flapping spam
            # Note: Cooldown applies ONLY to DOWN alerts.
            # RECOVERY alerts are always sent immediately (users expect DOWN→UP pairs).
            if alerting_services:
                last_alert = self._last_alert_at.get(target_id)
                if last_alert is not None:
                    seconds_since_last = (now - last_alert).total_seconds()
                    if seconds_since_last < ALERT_COOLDOWN_SECONDS:
                        _log.debug(
                            "DOWN alert rate-limited for target %s (%.0fs since last, cooldown=%ds)",
                            target_id,
                            seconds_since_last,
                            ALERT_COOLDOWN_SECONDS,
                        )
                        return

            # Variables for alert payload
            alert_id: str = ""
            alert_status: str = AlertStatus.OPEN  # Already str constant
            ended_at: datetime | None = None
            duration_seconds: int | None = None
            failed_service_str: str = "unknown"
            should_notify: bool = False
            started_at: datetime = now

            if alerting_services:
                # Host went DOWN - create alert record
                rt.failed_services = alerting_services

                failed_service_str = ", ".join(sorted(alerting_services))

                # Only create a new alert record if we don't already have an active one
                is_new_alert = False
                if not rt.current_alert_id:
                    alert_id = str(uuid.uuid4())
                    rt.current_alert_id = alert_id
                    is_new_alert = True

                    # Create database record only for new alerts
                    with self._db_conn() as conn:
                        sqlite_db.create_alert(
                            conn,
                            alert_id=alert_id,
                            target_id=target_id,
                            target_name=target_name,
                            failed_service=failed_service_str,
                            started_at=now,
                            recipients_notified=[],
                        )
                        conn.commit()
                        event_bus.publish("alert.changed", {"target_id": target_id})
                else:
                    alert_id = rt.current_alert_id

                should_notify = is_new_alert
                alert_status = AlertStatus.OPEN  # Already str constant
            else:
                # Recovery only when ALL services are up
                alert_id = rt.current_alert_id or str(uuid.uuid4())

                # Check if auto-close is enabled for this target
                target_cfg = self._targets.get(target_id, {})
                auto_close = target_cfg.get("auto_close_alert", True)

                if auto_close and rt.current_alert_id:
                    # Read started_at from database (single source of truth)
                    # This handles cases where down_since_by_service was reset by quality gate
                    started_at = now
                    with self._db_conn() as conn:
                        alert_row = sqlite_db.get_alert(conn, rt.current_alert_id)
                        if alert_row and alert_row["started_at"]:
                            db_started = alert_row["started_at"]
                            if isinstance(db_started, str):
                                started_at = datetime.fromisoformat(db_started).replace(tzinfo=UTC)
                            elif isinstance(db_started, datetime):
                                started_at = db_started.replace(tzinfo=UTC) if db_started.tzinfo is None else db_started
                        duration_seconds = int((now - started_at).total_seconds())
                        sqlite_db.resolve_alert(conn, rt.current_alert_id, now)
                        conn.commit()
                        event_bus.publish("alert.changed", {"target_id": target_id})

                ended_at = now

                failed_service_str = ", ".join(sorted(rt.failed_services)) if rt.failed_services else "unknown"

                rt.current_alert_id = None
                rt.failed_services.clear()
                for k in rt.down_since_by_service:
                    rt.down_since_by_service[k] = None

                should_notify = auto_close
                alert_status = AlertStatus.RESOLVED  # Already str constant

        # Only send notifications for new alerts (first DOWN) or recovery (UP)
        if should_notify:
            # Determine started_at based on alert type
            if alert_status == AlertStatus.OPEN:  # Already str constant
                earliest_down = min(
                    (s for s in rt.down_since_by_service.values() if s),
                    default=None,
                )
                started_at = earliest_down or now
            # else: started_at already set in RECOVERY block above

            alert_payload = AlertPayload(
                alert_id=alert_id,
                target_id=target_id,
                target_name=target_name,
                failed_service=failed_service_str,
                status=alert_status,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration_seconds,
            )
            await self._dispatch_alert(alert_payload, generation)

            # Record alert timestamp for rate-limiting (DOWN alerts only)
            if alert_status == AlertStatus.OPEN:  # Already str constant
                self._last_alert_at[target_id] = now

    async def _dispatch_alert(self, alert: AlertPayload, generation: int | None = None) -> None:
        """Dispatch alert via NotificationDispatcher.

        This is the single point of notification dispatch.
        All alerting goes through here → spooler.

        Args:
                alert: AlertPayload with complete alert context
        """
        generation = maintenance.current_generation() if generation is None else generation

        def _dispatch() -> Any:
            with maintenance.work_section(generation), self._db_conn() as conn:
                dispatcher = self._get_dispatcher(conn)
                return dispatcher.dispatch_alert(alert)

        try:
            result = await asyncio.to_thread(self._thread_call(self._epoch_for_thread(), _dispatch))
            if result.any_succeeded:
                _log.info(
                    "Alert %s enqueued: %d/%d channels queued",
                    alert.alert_id,
                    result.channels_succeeded,
                    result.channels_attempted,
                )
            elif result.channels_attempted > 0:
                _log.warning(
                    "Alert %s enqueue failed: 0/%d channels queued",
                    alert.alert_id,
                    result.channels_attempted,
                )
            else:
                _log.debug("Alert %s: no recipients configured", alert.alert_id)
        except maintenance.MaintenanceActiveError:
            raise
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.error("Failed to dispatch alert %s: %s", alert.alert_id, e)

    async def _maybe_cert_alert(
        self,
        *,
        target_id: int,
        target_name: str,
        days_left: int,
        cert_host: str,
        generation: int | None = None,
    ) -> None:
        """Send alert if SSL certificate is expiring soon (once only, resets when cert renewed)."""
        generation = maintenance.current_generation() if generation is None else generation
        alert_payload = None
        try:
            with maintenance.work_section(generation):
                rt = self._rt.get(target_id)
                if not rt:
                    return

                now = utcnow()
                if days_left > CERT_EXPIRY_ALERT_DAYS:
                    if rt.cert_alert_sent and rt.cert_alert_id:
                        with self._db_conn() as conn:
                            sqlite_db.resolve_alert(conn, rt.cert_alert_id, now)
                            conn.commit()
                            event_bus.publish("alert.changed", {"target_id": target_id})
                        _log.info(
                            "Cert alert %s resolved for target %d (cert renewed, %d days left)",
                            rt.cert_alert_id,
                            target_id,
                            days_left,
                        )
                        rt.cert_alert_id = None
                    rt.cert_alert_sent = False
                    return

                if rt.cert_alert_sent:
                    _log.debug("Cert alert already sent for target %s, skipping", target_id)
                    return

                if days_left <= 0:
                    failed_service = f"CERT EXPIRED - {cert_host}"
                elif days_left <= 3:
                    failed_service = f"CERT CRITICAL ({days_left}d) - {cert_host}"
                else:
                    failed_service = f"CERT WARNING ({days_left}d) - {cert_host}"

                alert_id = str(uuid.uuid4())
                with self._db_conn() as conn:
                    sqlite_db.create_alert(
                        conn,
                        alert_id=alert_id,
                        target_id=target_id,
                        target_name=target_name,
                        failed_service=failed_service,
                        started_at=now,
                        recipients_notified=[],
                    )
                    conn.commit()
                    event_bus.publish("alert.changed", {"target_id": target_id})
                rt.cert_alert_sent = True
                rt.cert_alert_id = alert_id
                alert_payload = AlertPayload(
                    alert_id=alert_id,
                    target_id=target_id,
                    target_name=target_name,
                    failed_service=failed_service,
                    status=AlertStatus.OPEN,
                    started_at=now,
                )
        except maintenance.MaintenanceActiveError:
            raise
        except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
            _log.warning("Certificate alert update failed for target %s: %s", target_id, e)
            return

        if alert_payload:
            await self._dispatch_alert(alert_payload, generation)
            _log.info(
                "CERT_ALERT target_id=%d days_left=%d host=%s alert_id=%s",
                target_id,
                days_left,
                cert_host,
                alert_payload.alert_id,
            )

    async def _run_housekeeping(self, now: datetime, generation: int | None = None) -> None:
        """
        Run daily housekeeping tasks: rollups, retention pruning and compression of closed days.
        Each target has a stable jitter offset (0-5 min) to spread I/O load.
        There is no user switch: rollups and compression are always on. A target whose compression
        backlog was not drained (migration of an existing install) is retried after a few minutes.
        """
        generation = maintenance.current_generation() if generation is None else generation
        await self._run_db_maintenance(now, generation)
        # Snapshot to avoid RuntimeError if _refresh_targets modifies dict during iteration
        targets_snapshot = list(self._targets.items())
        # Disabled targets are included: pruning must not depend on monitoring state.
        # Housekeeping never runs network checks or writes raw points (rollup, prune, then compress).
        for tid, cfg in targets_snapshot:
            if tid not in self._housekeeping_due:
                jitter = _stable_housekeeping_jitter(tid)
                # Schedule first run: now + jitter (spread initial runs)
                self._housekeeping_due[tid] = now + timedelta(seconds=jitter)
                _log.debug(
                    "Target %s housekeeping scheduled at %s (jitter=%ds)", tid, self._housekeeping_due[tid], jitter
                )
            if self._housekeeping_due[tid] > now:
                continue
            # Schedule next run: 24 hours + jitter
            jitter = _stable_housekeeping_jitter(tid)
            self._housekeeping_due[tid] = now + timedelta(seconds=HOUSEKEEPING_INTERVAL_SECONDS + jitter)
            # Run housekeeping in thread pool to avoid blocking. Bind it to the originating
            # generation so rollup/prune never runs against a data set swapped in by a restore.
            try:
                retention_days = int(cfg.get("retention_days", tsdb_db.DEFAULT_RETENTION_DAYS))
                retry_after = await asyncio.to_thread(
                    self._thread_call(self._epoch_for_thread(), _run_in_maintenance_section),
                    generation,
                    self._do_housekeeping,
                    tid,
                    retention_days,
                )
                if retry_after:
                    self._housekeeping_due[tid] = now + timedelta(seconds=retry_after + jitter)
            except maintenance.MaintenanceActiveError:
                _log.debug("Housekeeping for target %s skipped: maintenance or stale generation", tid)
            except Exception as e:  # noqa: BLE001 - scheduler-loop resilience boundary: log and continue
                _log.warning("Housekeeping failed for target %s: %s", tid, e)

    async def _run_db_maintenance(self, now: datetime, generation: int | None = None) -> None:
        """Hourly SQLite retention/WAL check, daily PRAGMA optimize; a failed or partial run retries in 5 min."""
        generation = maintenance.current_generation() if generation is None else generation
        last = self._last_db_maintenance
        if last is not None and (now - last).total_seconds() < DB_MAINTENANCE_INTERVAL_SECONDS:
            return
        daily = self._last_db_optimize is None or (now - self._last_db_optimize).total_seconds() >= 86400
        retry_at = now - timedelta(seconds=DB_MAINTENANCE_INTERVAL_SECONDS - TOKEN_CLEANUP_RETRY_SECONDS)
        try:
            # Retention deletes rows; bind it to the originating generation so it never runs
            # against a restored database.
            ok = await asyncio.to_thread(
                self._thread_call(self._epoch_for_thread(), _run_in_maintenance_section),
                generation,
                self._do_db_maintenance,
                daily,
            )
        except maintenance.MaintenanceActiveError:
            _log.debug("DB maintenance skipped: maintenance or stale generation")
            self._last_db_maintenance = retry_at
            return
        except Exception as e:  # noqa: BLE001 - housekeeping must not abort the loop
            _log.warning("DB maintenance failed (retry in %ds): %s", TOKEN_CLEANUP_RETRY_SECONDS, e)
            self._last_db_maintenance = retry_at
            return
        self._last_db_maintenance = now if ok else retry_at
        if daily and ok:
            self._last_db_optimize = now

    def _do_db_maintenance(self, daily: bool) -> bool:
        """Run retention + WAL checkpoint (+ optimize/stats once a day); True when every backlog is drained."""
        with self._db_conn() as conn:
            report = db_retention.run_retention(conn)
            wal = db_retention.checkpoint_wal(conn)
            if wal["mode"] == "TRUNCATE" or wal["busy"]:
                _log.info("WAL checkpoint: %s", wal)
            if daily:
                db_retention.run_optimize(conn)
                _log.info("DB stats: %s", db_retention.collect_db_stats(conn))
        return report.complete

    def _do_housekeeping(
        self,
        target_id: int,
        retention_days: int = tsdb_db.DEFAULT_RETENTION_DAYS,
    ) -> int | None:
        """Execute housekeeping synchronously (runs in thread pool); returns the seconds until a retry, if any."""
        result = tsdb_db.housekeep_target(
            self._tsdb_dir,
            target_id,
            retention_days,
            should_stop=lambda: self._stop.is_set() or maintenance.is_active(),
        )
        if result.rollups:
            _log.info("Target %s rollups: %s", target_id, result.rollups)
        if result.compaction.lines:
            _log.info(
                "Target %s compressed %d record(s) in %d series%s",
                target_id,
                result.compaction.lines,
                result.compaction.series,
                "" if result.complete else " (more pending)",
            )
        _log.debug("Target %s housekeeping complete=%s skipped=%s", target_id, result.complete, result.skipped)
        if result.compaction.errors:
            return HOUSEKEEPING_ERROR_RETRY_SECONDS
        return None if result.complete else HOUSEKEEPING_RETRY_SECONDS


__all__ = ["AlertStatus", "MonitorScheduler", "PortStatus", "QualityState", "TargetStatus"]
