#!/usr/bin/env python3
#
# app/services/event_bus.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-process, non-persistent event bus feeding the SSE channel (``/ui/events``).

Events are invalidation hints (ids, no payload data): browsers reload through the existing REST
endpoints. Publishing is thread-safe, never blocks and never raises, so the scheduler and request
threads can call it without risk. Every subscriber owns a bounded buffer; when it overflows (slow
client) or the publish backlog is exceeded, buffered events are dropped and the subscriber receives
one ``system.resync`` so the browser reloads its state instead of relying on replayed events.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

EVENT_TYPES = frozenset(
    {
        "target.measurement",
        "target.status",
        "alert.changed",
        "notification.changed",
        "target.changed",
        "system.heartbeat",
        "system.resync",
    }
)

SUBSCRIBER_QUEUE_SIZE = 64
# Events queued for delivery on the loop; beyond this the backlog is dropped and everyone resyncs.
MAX_PENDING_PUBLISHES = 2000
MAX_SUBSCRIBERS = 200


@dataclass(frozen=True)
class LiveEvent:
    """One invalidation event with its audience.

    ``user_ids`` limits delivery to those users (plus every admin when ``admins_too``); without it the
    event goes to everyone, or to admins only when ``admin_only``.
    """

    type: str
    data: dict[str, Any]
    user_ids: frozenset[int] | None = None
    admin_only: bool = False
    admins_too: bool = False

    def visible_to(self, user_id: int, is_admin: bool) -> bool:
        if self.user_ids is None:
            return is_admin or not self.admin_only
        return user_id in self.user_ids or (self.admins_too and is_admin)


class Subscription:
    """A bounded per-client buffer. Used from the event loop thread only."""

    def __init__(self, bus: EventBus, user_id: int, is_admin: bool, size: int) -> None:
        self._bus = bus
        self.user_id = user_id
        self.is_admin = is_admin
        self._size = size
        self._items: deque[LiveEvent] = deque()
        self._wakeup = asyncio.Event()
        self._resync = False
        self._closed = False

    def _offer(self, event: LiveEvent) -> None:
        if self._closed or self._resync:
            # A resync is already pending: the client reloads everything, so buffering adds nothing.
            return
        if len(self._items) >= self._size:
            self._items.clear()
            self._resync = True
        else:
            self._items.append(event)
        self._wakeup.set()

    def _force_resync(self) -> None:
        if not self._closed:
            self._items.clear()
            self._resync = True
            self._wakeup.set()

    def _close(self) -> None:
        self._closed = True
        self._items.clear()
        self._wakeup.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._bus._remove(self)  # noqa: SLF001 - same module
        self._close()

    async def next(self, timeout: float) -> LiveEvent | None:
        """Return the next event, ``None`` on timeout; raise ``StopAsyncIteration`` once closed."""
        while True:
            if self._resync:
                self._resync = False
                return LiveEvent("system.resync", {})
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except TimeoutError:
                return None


class EventBus:
    """Per-app event hub; ``start`` binds it to the serving event loop, ``close`` ends all streams."""

    def __init__(self, *, queue_size: int = SUBSCRIBER_QUEUE_SIZE, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()
        self._pending = 0
        self._backlog_lost = False
        self._closed = False

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self, user_id: int, is_admin: bool) -> Subscription | None:
        """Register a client (event loop thread). ``None`` when the bus is closed or full."""
        if self._closed or self._loop is None or len(self._subs) >= self._max_subscribers:
            return None
        sub = Subscription(self, user_id, is_admin, self._queue_size)
        self._subs.add(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, event: LiveEvent) -> None:
        """Queue ``event`` for delivery. Thread-safe, non-blocking, never raises."""
        loop = self._loop
        if loop is None or self._closed or event.type not in EVENT_TYPES:
            return
        with self._lock:
            if self._pending >= MAX_PENDING_PUBLISHES:
                self._backlog_lost = True
                return
            self._pending += 1
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop already closed during shutdown
            with self._lock:
                self._pending -= 1

    def _deliver(self, event: LiveEvent) -> None:
        with self._lock:
            self._pending -= 1
            lost, self._backlog_lost = self._backlog_lost, False
        try:
            for sub in list(self._subs):
                if lost:
                    sub._force_resync()  # noqa: SLF001 - same module
                if event.visible_to(sub.user_id, sub.is_admin):
                    sub._offer(event)  # noqa: SLF001 - same module
        except Exception:  # noqa: BLE001 - delivery must never break the event loop callback
            _log.exception("Event delivery failed")

    async def close(self) -> None:
        """End every stream and refuse new subscribers (idempotent)."""
        self._closed = True
        for sub in list(self._subs):
            sub._close()  # noqa: SLF001 - same module
        self._subs.clear()
        # Give stream generators one loop iteration to observe the closed state.
        await asyncio.sleep(0)


_current: EventBus | None = None


def set_current_bus(bus: EventBus | None) -> None:
    global _current
    _current = bus


def publish(
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    user_ids: Iterable[int] | None = None,
    admin_only: bool = False,
    admins_too: bool = False,
) -> None:
    """Publish on the running app's bus; a no-op without one. Safe to call from any thread."""
    bus = _current
    if bus is None:
        return
    try:
        bus.publish(
            LiveEvent(
                event_type,
                dict(data or {}),
                None if user_ids is None else frozenset(user_ids),
                admin_only,
                admins_too,
            )
        )
    except Exception:  # noqa: BLE001 - SSE must never affect measurements, alerts or requests
        _log.debug("Event publish failed", exc_info=True)
