#!/usr/bin/env python3
#
# app/services/release_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Daily check for a newer stable justUp! release, reported through the notification inbox.

The service asks the fixed GitHub endpoint for the latest *published, stable* release. Only a
strictly validated ``vMAJOR.MINOR.MINOR`` tag is accepted, and the release link is rebuilt from that
tag; no URL or text from the response is stored or rendered. A 404 means "no release yet" and is
not an error. The installed version comes only from ``utils.version.get_version()``.

State (last success, ETag, failure streak, Retry-After) is one JSON value in the ``settings`` table,
so a restart resumes the schedule instead of querying again. All database writes happen inside one
transaction and a maintenance work section; a failed or refused write publishes no SSE event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..db import notifications as inbox
from ..db import sqlite as sqlite_db
from ..utils import maintenance
from ..utils.constants import SettingKeys
from ..utils.time import utcnow
from ..utils.version import get_version
from . import event_bus

_log = logging.getLogger(__name__)

REPO = "Gill-Bates/justup"
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASE_PAGE_PREFIX = f"https://github.com/{REPO}/releases/tag/"
EVENT_TYPE = "activity.release.available"

CHECK_INTERVAL_SECONDS = 24 * 3600.0
START_JITTER_MAX_SECONDS = 600.0
BACKOFF_BASE_SECONDS = 900.0  # 15 min, doubled per consecutive failure
BACKOFF_MAX_SECONDS = 6 * 3600.0
RETRY_AFTER_MAX_SECONDS = 24 * 3600.0
MAINTENANCE_RETRY_SECONDS = 60.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_VERSION_RE = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})$")
_TAG_RE = re.compile(r"^v(\d{1,6})\.(\d{1,6})\.(\d{1,6})$")

Version = tuple[int, int, int]


# ─── Version helpers (central, isolated) ─────────────────────────────────────


def parse_version(value: object) -> Version | None:
    """Parse ``1.2.3`` or ``v1.2.3`` into a comparable tuple; anything else (``dev``, rc, ...) is None."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value.strip().removeprefix("v"))
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def format_version(version: Version) -> str:
    return "v" + ".".join(str(part) for part in version)


def release_url(tag: object) -> str | None:
    """Official release page for a validated tag, else None. Never built from untrusted URLs."""
    if isinstance(tag, str) and _TAG_RE.fullmatch(tag):
        return RELEASE_PAGE_PREFIX + tag
    return None


def is_update_available(local: object, remote: object) -> bool:
    """True only when both versions parse and *remote* is strictly newer than *local*."""
    local_v, remote_v = parse_version(local), parse_version(remote)
    return local_v is not None and remote_v is not None and remote_v > local_v


# ─── Response handling ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one request. ``status``: release | none | not_modified | rate_limited | error."""

    status: str
    tag: str | None = None
    etag: str | None = None
    retry_after: float | None = None
    error: str | None = None


def latest_stable_tag(releases: list[object]) -> str | None:
    """Highest SemVer tag among published stable releases; drafts, pre-releases and odd tags are ignored.

    The list order (publication date) is irrelevant: a late-published older release must not hide a newer one.
    """
    best: tuple[Version, str] | None = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") is not False or item.get("prerelease") is not False:
            continue
        tag = item.get("tag_name")
        version = parse_version(tag) if isinstance(tag, str) and _TAG_RE.fullmatch(tag) else None
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


def _retry_after_seconds(response: httpx.Response, now: datetime) -> float | None:
    """Seconds to wait from Retry-After or an exhausted rate-limit reset; None when not rate limited."""
    raw = response.headers.get("retry-after")
    if raw:
        raw = raw.strip()
        if raw.isdigit():
            return min(float(raw), RETRY_AFTER_MAX_SECONDS)
        with suppress(TypeError, ValueError):
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return min(max((when - now).total_seconds(), 0.0), RETRY_AFTER_MAX_SECONDS)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset", "")
        if reset.isdigit():
            return min(max(float(reset) - now.timestamp(), 0.0), RETRY_AFTER_MAX_SECONDS)
    return None


async def fetch_latest(client: httpx.AsyncClient, etag: str | None, now: datetime) -> FetchResult:
    """One conditional, size-capped request; every network, HTTP and parsing problem becomes a FetchResult."""
    headers = {"If-None-Match": etag} if etag else {}
    try:
        async with client.stream("GET", RELEASES_API_URL, headers=headers) as response:
            code = response.status_code
            if code == 304:
                return FetchResult("not_modified", etag=etag)
            if code == 404:
                return FetchResult("none")
            if code in (403, 429):
                wait = _retry_after_seconds(response, now)
                if wait is not None or code == 429:
                    return FetchResult("rate_limited", retry_after=wait if wait is not None else BACKOFF_BASE_SECONDS)
                return FetchResult("error", error=f"HTTP {code}")
            if code != 200:
                return FetchResult("error", error=f"HTTP {code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    return FetchResult("error", error="response too large")
            new_etag = response.headers.get("etag")
    except httpx.HTTPError as exc:
        return FetchResult("error", error=type(exc).__name__)
    try:
        payload = json.loads(body)
    except ValueError:
        return FetchResult("error", error="invalid JSON")
    if not isinstance(payload, list):
        return FetchResult("error", error="unexpected payload")
    tag = latest_stable_tag(payload)
    return FetchResult("release" if tag else "none", tag=tag, etag=new_etag)


# ─── Schedule state ──────────────────────────────────────────────────────────


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def plan_delay(state: dict[str, Any], now: datetime) -> float:
    """Seconds until the persisted ``next_attempt_at`` (0 = due now); the stored state is the only authority."""
    due = _parse_time(state.get("next_attempt_at"))
    return 0.0 if due is None else max(0.0, (due - now).total_seconds())


def _backoff_seconds(failures: int) -> float:
    return min(BACKOFF_BASE_SECONDS * 2 ** max(failures - 1, 0), BACKOFF_MAX_SECONDS)


def _load_state(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        value = sqlite_db.get_setting(conn, SettingKeys.RELEASE_CHECK_STATE, {})
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ─── Database effects ────────────────────────────────────────────────────────


def reconcile_obsolete(conn: sqlite3.Connection, local_version: object) -> int:
    """Retire release notices at or below the installed version, for every user including future admins.

    The row keeps its idempotency key (so it is never re-created) but moves to an event type the inbox
    queries exclude. Runs inside the caller's transaction and publishes one SSE event after the commit.
    """
    local = parse_version(local_version)
    if local is None:
        return 0
    stale = [
        (int(row["id"]),)
        for row in conn.execute("SELECT id, resource_id FROM notifications WHERE event_type = ?", (EVENT_TYPE,))
        if (version := parse_version(row["resource_id"])) is not None and version <= local
    ]
    if not stale:
        return 0
    conn.executemany(f"UPDATE notifications SET event_type = '{inbox.EVENT_RELEASE_SUPERSEDED}' WHERE id = ?", stale)  # noqa: S608
    sqlite_db.run_after_commit(conn, lambda: event_bus.publish("notification.changed", {}, admin_only=True))
    return len(stale)


def notify_release(conn: sqlite3.Connection, local: Version, remote_tag: str) -> int | None:
    """Create the single admin notice for *remote_tag*; None when it already exists."""
    return inbox.emit(
        conn,
        event_type=EVENT_TYPE,
        category="activity",
        severity="info",
        title=f"justUp! {remote_tag} is available",
        body=f"Installed: {format_version(local)} · New release: {remote_tag}",
        idempotency_key=f"{EVENT_TYPE}:{remote_tag}",
        audience=inbox.AUDIENCE_ADMINS,
        # No resource_type: the column's CHECK allows no release type; the event type identifies the tag.
        resource_id=remote_tag,
    )


def apply_result(conn: sqlite3.Connection, result: FetchResult, now: datetime) -> None:
    """Persist the outcome and any notification atomically (one transaction, one SSE publish)."""
    local_raw = get_version()
    with sqlite_db.transaction(conn):
        state = _load_state(conn)
        if result.status in ("release", "none", "not_modified"):
            state.update(
                last_success_at=now.isoformat(),
                failures=0,
                next_attempt_at=(now + timedelta(seconds=CHECK_INTERVAL_SECONDS)).isoformat(),
            )
            if result.status == "none":
                state["latest_tag"] = None
            if result.status != "not_modified":
                state["etag"] = result.etag
            if result.status == "release":
                state["latest_tag"] = result.tag
            latest = state.get("latest_tag")
            local = parse_version(local_raw)
            if local is not None and is_update_available(local_raw, latest):
                notify_release(conn, local, latest)
            reconcile_obsolete(conn, local_raw)
        else:
            failures = int(state.get("failures") or 0) + 1
            wait = max(_backoff_seconds(failures), result.retry_after or 0.0)
            state.update(failures=failures, next_attempt_at=(now + timedelta(seconds=wait)).isoformat())
        sqlite_db.set_setting(conn, SettingKeys.RELEASE_CHECK_STATE, state)


# ─── Service ─────────────────────────────────────────────────────────────────


class ReleaseChecker:
    """Supervised service: at most one request at a time, interruptible waits, no blocking I/O."""

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self._conn_factory = conn_factory
        self._client_factory = client_factory or self._default_client
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - schedule jitter, not security-sensitive
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=False,
            headers={
                "User-Agent": f"justUp!/{get_version()} (+https://github.com/{REPO})",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def request_stop(self) -> None:
        self._stop.set()

    async def _wait(self, seconds: float) -> None:
        """Sleep that returns early on stop."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _with_db(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._conn_factory()
        try:
            return func(conn)
        finally:
            sqlite_db.close_connection(conn)

    def _read_state(self) -> dict[str, Any]:
        return self._with_db(_load_state)

    def _write(self, generation: int, fn: Callable[[sqlite3.Connection], Any]) -> bool:
        """Run a write inside a maintenance section; False when a restore refused it."""
        try:
            with maintenance.work_section(generation):
                self._with_db(fn)
        except maintenance.MaintenanceActiveError:
            return False
        return True

    async def _fetch(self, etag: str | None, now: datetime) -> FetchResult | None:
        """Request the release, abandoning the request as soon as a stop is signalled."""
        async with self._client_factory() as client:
            task = asyncio.ensure_future(fetch_latest(client, etag, now))
            stopper = asyncio.ensure_future(self._stop.wait())
            try:
                await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if not task.done():
                    return None
                return task.result()
            finally:
                for pending in (task, stopper):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(task, stopper, return_exceptions=True)

    async def check_once(self) -> str:
        """One guarded check; returns the result status (or skipped/maintenance/stopped/busy)."""
        if self._lock.locked():
            return "busy"
        async with self._lock:
            local_raw = get_version()
            if parse_version(local_raw) is None:
                _log.info("Release check skipped: installed version %r is not a release version", local_raw)
                return "skipped"
            generation = maintenance.current_generation()
            state = await asyncio.to_thread(self._read_state)
            now = self._clock()
            result = await self._fetch(state.get("etag") if isinstance(state.get("etag"), str) else None, now)
            if result is None:
                return "stopped"
            if result.status == "error":
                _log.warning("Release check failed: %s", result.error)
            elif result.status == "rate_limited":
                _log.warning("Release check rate limited; retry in %.0fs", result.retry_after or 0)
            stored = await asyncio.to_thread(self._write, generation, lambda conn: apply_result(conn, result, now))
            return result.status if stored else "maintenance"

    async def run(self) -> None:
        """Service body for the supervisor: reconcile once, then check whenever the plan says due."""
        await self._reconcile_startup()
        while not self._stop.is_set():
            try:
                state = await asyncio.to_thread(self._read_state)
                delay = plan_delay(state, self._clock())
                # Jitter once per wait; after it the check runs without re-planning, so it cannot slide.
                await self._wait(delay + self._rng.uniform(0.0, START_JITTER_MAX_SECONDS))
                if self._stop.is_set():
                    break
                status = await self.check_once()
            except Exception:  # noqa: BLE001 - GitHub or storage trouble must never reach the supervisor or monitoring
                _log.exception("Release check iteration failed")
                status = "crashed"
            if status == "skipped":
                await self._wait(CHECK_INTERVAL_SECONDS)
            elif status in ("maintenance", "busy"):
                await self._wait(MAINTENANCE_RETRY_SECONDS)
            elif status == "crashed":
                # Only for failures before the outcome was persisted; otherwise next_attempt_at rules.
                await self._wait(BACKOFF_BASE_SECONDS)

    async def _reconcile_startup(self) -> None:
        """After an upgrade, retire notices for the installed version without any network access."""
        try:
            generation = maintenance.current_generation()

            def _fn(conn: sqlite3.Connection) -> None:
                with sqlite_db.transaction(conn):
                    reconcile_obsolete(conn, get_version())

            await asyncio.to_thread(self._write, generation, _fn)
        except Exception:  # noqa: BLE001 - housekeeping only; the check loop still runs
            _log.exception("Release notice reconciliation failed")
