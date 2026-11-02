#!/usr/bin/env python3
#
# app/services/service_alerts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Administrative inbox alerts for the service supervisor.

The supervisor hands every crash, degradation and recovery to ``ServiceAlertSink``. The sink keeps a
small bounded queue and writes each alert with its own short-lived database connection from a
background task, so it depends on neither the monitoring scheduler nor the SMTP spooler (an outage
of those must still be reported) and never blocks the supervisor. A database that is briefly
unavailable is retried with backoff; the alert is stored as soon as it is reachable again, as long
as the process lives. Rows are one deduplicated incident per service (``db.notifications``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from ..db import notifications as inbox
from ..db import sqlite as sqlite_db
from ..utils.supervisor import ServiceAlert

_log = logging.getLogger(__name__)

MAX_PENDING = 50
MAX_ATTEMPTS = 30  # with the 60 s backoff cap roughly half an hour of database trouble
RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 60.0

_EXC_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,59}$")
_CAUSES = {"crash_loop": "repeated crashes", "hangs": "repeated hangs", "failure_series": "repeated failures"}


def incident_name(service: str) -> str:
    return f"supervisor.{service}"


class ServiceAlertSink:
    """Callable notifier for ``ServiceSupervisor``; call it from the event loop only."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._connect = connect
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._pending: deque[ServiceAlert] = deque(maxlen=MAX_PENDING)
        self._task: asyncio.Task | None = None

    def __call__(self, alert: ServiceAlert) -> None:
        if len(self._pending) == self._pending.maxlen:
            _log.warning("Service alert queue full; dropping the oldest alert")
        self._pending.append(alert)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._drain(), name="service-alert-sink")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _drain(self) -> None:
        attempts = 0
        delay = RETRY_INITIAL_SECONDS
        while self._pending:
            alert = self._pending[0]
            try:
                await asyncio.to_thread(self.write, alert)
            except Exception as exc:  # noqa: BLE001 - alarm boundary: retry later, never raise into the event loop
                attempts += 1
                _log.warning(
                    "Service alert for %s not stored (%s), attempt %d", alert.service, type(exc).__name__, attempts
                )
                if attempts >= self._max_attempts:
                    self._discard(alert)
                    attempts, delay = 0, RETRY_INITIAL_SECONDS
                    continue
                await self._sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue
            self._discard(alert)
            attempts, delay = 0, RETRY_INITIAL_SECONDS

    def _discard(self, alert: ServiceAlert) -> None:
        if self._pending and self._pending[0] is alert:  # the queue may have dropped it while it was written
            self._pending.popleft()

    def write(self, alert: ServiceAlert) -> None:
        """Store one alert with a fresh connection; raises when it could not be persisted."""
        conn = self._connect()
        try:
            with sqlite_db.transaction(conn, immediate=True):
                self._apply(conn, alert)
        finally:
            sqlite_db.close_connection(conn)

    @staticmethod
    def _apply(conn: sqlite3.Connection, alert: ServiceAlert) -> None:
        name = incident_name(alert.service)
        label = alert.label
        exc = alert.exc_type if alert.exc_type and _EXC_TYPE.match(alert.exc_type) else "Error"
        when = f"{alert.at:%H:%M} UTC"
        common = {"now": alert.at, "category": "activity", "resource_type": None}
        if alert.kind == "recovered":
            inbox.resolve_incident(
                conn,
                name,
                title=f"{label} recovered",
                body=f"Running stably again · {alert.crashes_total} crash(es) since start · {when}",
                now=alert.at,
            )
            persisted = inbox.get_incident(conn, name) is None
        else:
            degraded = alert.kind == "degraded" or alert.state == "degraded"
            if degraded:
                title = f"{label} degraded"
                cause = _CAUSES.get(alert.reason, "repeated errors")
                body = f"Status: degraded · {cause} · last error {exc} · {alert.crashes_total} crash(es) · {when}"
                severity = "error"
            else:
                title = f"{label} crashed"
                body = f"Restarting automatically · {exc} · {alert.crashes_total} crash(es) since start · {when}"
                severity = "warning"
            if alert.kind == "degraded" and inbox.get_incident(conn, name) is not None:
                persisted = inbox.update_incident(conn, name, title=title, body=body, severity=severity, renotify=True)
            else:
                inbox.report_incident(conn, name, title=title, body=body, severity=severity, **common)
                persisted = inbox.get_incident(conn, name) is not None
        if not persisted:  # the inbox helpers swallow storage errors; make the sink retry instead
            raise sqlite3.OperationalError("service alert was not persisted")


__all__ = ["ServiceAlertSink", "incident_name"]
