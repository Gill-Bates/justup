#!/usr/bin/env python3
#
# app/utils/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dependency-free in-memory rate limiting for FastAPI routes.

Usage::

    @router.post("/x", dependencies=[Depends(rate_limit("5/minute"))])

Every ``rate_limit(...)`` call owns its own sliding window, so two routes that share
a limit string still count separately. Route-level dependencies run before parameter
dependencies: the limiter counts a request before authentication or body validation
can reject it, so unauthenticated floods cannot bypass a budget.

Variants:
- ``rate_limit_auth()``: the Settings-managed login limit, re-read on every request.
- ``rate_limit_user(limit, auth_dependency)``: a per-user budget, keyed by the user the
  given authentication dependency resolves (so new tokens do not give a new budget).

justUp! runs one app process per data directory, so the in-process counters are
authoritative and no shared store is needed. Any internal failure or a full client
table rejects the request (429) instead of admitting it.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from .client_ip import normalize_ip_bucket, resolve_trusted_client_ip
from .constants import RATE_LIMIT_AUTH_DEFAULT, RATE_LIMIT_PATTERN, SettingKeys
from .runtime_settings import get_runtime_setting

__all__ = [
    "MAX_KEYS",
    "RATE_LIMIT_AUTH",
    "RATE_LIMIT_DEFAULT",
    "RATE_LIMIT_HEAVY",
    "RATE_LIMIT_PDF",
    "CapacityExhaustedError",
    "RateLimitExceededError",
    "SlidingWindow",
    "parse_limit",
    "rate_limit",
    "rate_limit_auth",
    "rate_limit_exceeded_handler",
    "rate_limit_key",
    "rate_limit_user",
    "reset_all",
]

_log = logging.getLogger(__name__)

RATE_LIMIT_DEFAULT = "60/minute"  # General API endpoints
RATE_LIMIT_HEAVY = "10/minute"  # Heavy operations (backup, restore, purge)
RATE_LIMIT_PDF = "5/minute"  # PDF generation (CPU-intensive, very strict)

# Upper bound of tracked client keys per window; a full table rejects new keys.
MAX_KEYS = 10_000
# Requests without a usable client address share one bucket: conservative, never a bypass.
_UNKNOWN_CLIENT_KEY = "unknown"
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_LIMIT_RE = re.compile(RATE_LIMIT_PATTERN)
# Minimum gap between sweeps of a full table and between failure log lines.
_SWEEP_INTERVAL = 1.0
_LOG_INTERVAL = 60.0
# Retry-After for a fail-closed rejection; capped because the cause is not the caller's own budget.
_FAIL_CLOSED_RETRY_MAX = 60

# Every window registers here so tests can drop all counters at once.
_windows: weakref.WeakSet[SlidingWindow] = weakref.WeakSet()
_last_failure_log = -_LOG_INTERVAL


class RateLimitExceededError(Exception):
    """Raised by a rate-limit dependency when the caller exceeded its budget."""

    def __init__(self, retry_after: int, limit: str) -> None:
        super().__init__(f"Rate limit exceeded ({limit})")
        self.retry_after = max(1, int(retry_after))
        self.limit = limit


class CapacityExhaustedError(RuntimeError):
    """The client table is full of active buckets."""


@lru_cache(maxsize=64)
def _parse_str(limit: str) -> tuple[int, int]:
    match = _LIMIT_RE.fullmatch(limit)
    if match is None:
        raise ValueError(f"Invalid rate limit {limit!r}; expected 'N/second|minute|hour|day'")
    return int(match.group(1)), _UNIT_SECONDS[match.group(2)]


def parse_limit(limit: str) -> tuple[int, int]:
    """Parse ``"N/second|minute|hour|day"`` into ``(count, window_seconds)``.

    Raises ValueError for anything else (spaces, plurals, zero, leading zeros, > 6 digits).
    Results are cached, so re-reading the dynamic login limit per request stays cheap.
    """
    if not isinstance(limit, str):
        raise ValueError(f"Invalid rate limit {limit!r}; expected 'N/second|minute|hour|day'")
    return _parse_str(limit)


class SlidingWindow:
    """Per-key request counter over a sliding window.

    Rejected requests are not recorded, so a caller that backs off is admitted again
    once enough admitted requests left the window. ``hit`` may be given a different
    ``limit``/``window`` per call (the dynamic login limit); the history is kept.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = -_SWEEP_INTERVAL
        # Shared by the event loop and threadpool handlers.
        self._lock = threading.Lock()
        _windows.add(self)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def reset(self) -> None:
        """Forget all history (tests)."""
        with self._lock:
            self._hits.clear()
            self._last_sweep = -_SWEEP_INTERVAL

    def _sweep(self, now: float) -> None:
        """Drop expired keys only; dropping an active one would reset a live limit."""
        for key in [k for k, s in self._hits.items() if not s or s[-1] <= now - self._window]:
            del self._hits[key]
        self._last_sweep = now

    def hit(self, key: str, *, limit: int | None = None, window: float | None = None) -> float | None:
        """Count one request; None when allowed, else the seconds until a slot frees up."""
        limit = self._limit if limit is None else limit
        with self._lock:
            now = self._clock()
            self._window = self._window if window is None else window
            stamps = self._hits.get(key)
            if stamps is None:
                # A full table is swept at most once per interval so a flood of new keys stays cheap.
                if len(self._hits) >= self._max_keys and now - self._last_sweep >= _SWEEP_INTERVAL:
                    self._sweep(now)
                if len(self._hits) >= self._max_keys:
                    raise CapacityExhaustedError("rate limiter capacity exhausted")
                stamps = self._hits[key] = deque()
            while stamps and stamps[0] <= now - self._window:
                stamps.popleft()
            if len(stamps) >= limit:
                # A lowered limit can leave more stamps than slots: wait for enough of them to expire.
                return max(0.0, stamps[len(stamps) - limit] + self._window - now)
            stamps.append(now)
            return None


def reset_all() -> None:
    """Clear every counter (test isolation)."""
    for window in list(_windows):
        window.reset()


def rate_limit_key(request: Request) -> str:
    """Client bucket: trusted-proxy aware IP, IPv6 reduced to /64, shared bucket if unknown."""
    ip = resolve_trusted_client_ip(request)
    return normalize_ip_bucket(ip) if ip else _UNKNOWN_CLIENT_KEY


def _log_failure(message: str, *, exc_info: bool = False) -> None:
    """Log at most once per interval so a flood of rejections cannot flood the log."""
    global _last_failure_log
    now = time.monotonic()
    if now - _last_failure_log >= _LOG_INTERVAL:
        _last_failure_log = now
        _log.error("%s; rejecting requests", message, exc_info=exc_info)


def _admit(counter: SlidingWindow, key: str, limit: str) -> None:
    """Count one hit for ``key`` under ``limit``; raise RateLimitExceededError when over budget."""
    count, window = parse_limit(limit)
    try:
        wait = counter.hit(key, limit=count, window=window)
    except CapacityExhaustedError:
        _log_failure("Rate limiter table is full")
        raise RateLimitExceededError(retry_after=min(window, _FAIL_CLOSED_RETRY_MAX), limit=limit) from None
    except Exception:  # noqa: BLE001 - unreadable limiter state means rejection, not admission
        _log_failure("Rate limiter state unreadable", exc_info=True)
        raise RateLimitExceededError(retry_after=min(window, _FAIL_CLOSED_RETRY_MAX), limit=limit) from None
    if wait is not None:
        raise RateLimitExceededError(retry_after=math.ceil(wait), limit=limit)


def rate_limit(limit: str, *, clock: Callable[[], float] = time.monotonic) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing ``limit`` (e.g. ``"5/minute"``) per client IP.

    An invalid limit string raises ValueError immediately, at route registration.
    """
    count, window = parse_limit(limit)
    counter = SlidingWindow(count, window, clock)

    async def _enforce(request: Request) -> None:
        _admit(counter, rate_limit_key(request), limit)

    _enforce.rate_limit_spec = limit  # introspected by the route regression test
    return _enforce


def RATE_LIMIT_AUTH() -> str:  # noqa: N802 - evaluated per request, so a Settings change applies at once
    """Login/auth limit from Settings (UI); an invalid stored value falls back to the default."""
    value = str(get_runtime_setting(SettingKeys.RATE_LIMIT_AUTH, RATE_LIMIT_AUTH_DEFAULT) or "")
    try:
        parse_limit(value)
    except ValueError:
        return RATE_LIMIT_AUTH_DEFAULT
    return value


def rate_limit_auth(*, clock: Callable[[], float] = time.monotonic) -> Callable[[Request], Awaitable[None]]:
    """Like ``rate_limit`` but with the Settings-managed login limit.

    One counter per call; each request applies the current setting to the kept history, so a
    change takes effect immediately without granting a fresh budget.
    """
    counter = SlidingWindow(*parse_limit(RATE_LIMIT_AUTH_DEFAULT), clock)

    async def _enforce(request: Request) -> None:
        _admit(counter, rate_limit_key(request), RATE_LIMIT_AUTH())

    _enforce.rate_limit_spec = "auth"
    return _enforce


def rate_limit_user(
    limit: str,
    auth_dependency: Callable[..., object],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[..., Awaitable[None]]:
    """Per-user budget for routes behind ``auth_dependency`` (e.g. ``require_admin``).

    FastAPI resolves the authentication first, so the key is the authenticated user id and
    every token of that user shares one budget. Requests the authentication rejects are not
    counted here; pair this with a pre-authentication ``rate_limit`` for those.
    """
    count, window = parse_limit(limit)
    counter = SlidingWindow(count, window, clock)

    async def _enforce(request: Request, user: object = Depends(auth_dependency)) -> None:
        user_id = user.get("id") if hasattr(user, "get") else None
        key = f"user:{user_id}" if user_id is not None else rate_limit_key(request)
        _admit(counter, key, limit)

    _enforce.rate_limit_spec = limit
    return _enforce


async def rate_limit_exceeded_handler(_request: Request, exc: RateLimitExceededError) -> JSONResponse:
    """429 with a generic message; ``detail`` matches the app's other error bodies."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
        headers={"Retry-After": str(exc.retry_after), "X-RateLimit-Limit": exc.limit.partition("/")[0]},
    )
