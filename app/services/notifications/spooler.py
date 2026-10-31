#!/usr/bin/env python3
#
# app/services/notifications/spooler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Notification Spooler - Persistent Queue for Failed Notifications.

This module implements a spooler/retry queue for notifications that failed
to send (e.g., due to rate limiting, network issues, service unavailability).

Key features:
- SQLite-backed persistent queue (survives restarts)
- Exponential backoff with jitter
- Configurable max retries
- Automatic cleanup of old entries
- Background processing via scheduler

Message types:
- 'welcome': Target assignment welcome notifications
- 'alert': Down/recovery alert notifications
- 'test': Configuration test messages

Usage:
    from app.services.notifications.spooler import NotificationSpooler

    spooler = NotificationSpooler(db_conn_factory)
    spooler.enqueue(...)  # Add failed notification to queue
    await spooler.process_queue()  # Process pending (called by scheduler)
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import random
import smtplib
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ...db import notifications as inbox
from ...db import sqlite as sqlite_db
from ...utils import maintenance
from ...utils.time import utcnow as _utcnow
from .signal_errors import (
    SignalNotRegistered,
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalTimeout,
)

_log = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────────

# Maximum retry attempts before giving up
# With MAX_RETRIES=5: initial send fails, then up to 5 retries → FAILED
# Total attempts = 1 (initial) + MAX_RETRIES = 6
# Crash recovery counts an uncertain attempt before trying the job again.
MAX_RETRIES = 5

# Base delay for exponential backoff (seconds)
BASE_DELAY_SECONDS = 60  # 1 minute

# Maximum delay cap (seconds)
MAX_DELAY_SECONDS = 3600  # 1 hour

# Jitter factor (0.0 - 1.0) to prevent thundering herd
JITTER_FACTOR = 0.25

# Processing timeout: recover stuck PROCESSING entries after this duration
# If the process crashes between mark_processing() and mark_sent()/mark_retry(),
# entries would be stuck forever. This timeout allows recovery.
PROCESSING_TIMEOUT_SECONDS = 600  # 10 minutes

# Process batch size (per run)
PROCESS_BATCH_SIZE = 20

# Marker prefix in last_error for an attempt whose outcome is unconfirmed (bytes may have left the
# host). The row stays PENDING with an incremented retry_count: the retry is at-least-once and, as it
# reuses the same queue row, a business event never becomes a second row.
UNCERTAIN_MARKER = "[UNCERTAIN]"

# Bounded wait for a finalize that meets an active maintenance window (not a stale generation).
FINALIZE_WAIT_SECONDS = 30.0
FINALIZE_POLL_SECONDS = 0.25

# Failures after which the remote side may already have accepted the message.
_UNCERTAIN_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    SignalTimeout,
    smtplib.SMTPServerDisconnected,
    ConnectionResetError,
    BrokenPipeError,
)

# Channel-specific batch limits per scheduler run
# Signal is extremely rate-limited, so we only send 1 per run to avoid bursts
# Email can handle more volume
SIGNAL_BATCH_LIMIT = 1
EMAIL_BATCH_LIMIT = 10

# Throttle delay between Signal messages (seconds)
# Signal servers are very sensitive to bursts
SIGNAL_THROTTLE_SECONDS = 2.0
SIGNAL_TEST_HOURLY_LIMIT = 30
SIGNAL_TEST_RECIPIENT_LIMIT = 5
SIGNAL_TEST_PENDING_LIMIT = 50


class SignalQueueLimitError(Exception):
    """The Signal test-message queue has reached an abuse limit."""


class NotificationType(StrEnum):
    """Type of notification in the queue."""

    WELCOME = "welcome"
    ALERT = "alert"
    TEST = "test"


class ChannelType(StrEnum):
    """Notification channel type."""

    SIGNAL = "signal"
    EMAIL = "email"


class QueueStatus(StrEnum):
    """Status of a queued notification."""

    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"  # Permanently failed (max retries exceeded)


@dataclass
class DeliveryResult:
    """Outcome of one network send attempt; applied to the DB in a separate short phase."""

    kind: str  # sent | retry | uncertain | permanent | deferred
    error: str = ""
    delay_seconds: int | None = None


@dataclass
class QueuedNotification:
    """A notification in the queue."""

    id: int
    notification_type: NotificationType
    channel_type: ChannelType
    recipient_id: int
    recipient_name: str
    recipient_address: str  # Phone number or email address
    payload: dict[str, Any]  # JSON payload (message, subject, etc.)
    status: QueueStatus
    retry_count: int
    next_retry_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _message_id_key(item: QueuedNotification) -> str:
    """Stable per-queue-row key (local part of an RFC 5322 Message-ID), identical on every retry."""
    digest = hashlib.sha256(f"{item.id}|{item.created_at.isoformat()}".encode()).hexdigest()[:16]
    return f"justup-q{item.id}-{digest}"


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _calculate_next_retry(retry_count: int) -> datetime:
    """
    Calculate next retry time using exponential backoff with jitter.

    Formula: delay = min(BASE * 2^retry + jitter, MAX_DELAY)
    """
    # Exponential backoff: 1min, 2min, 4min, 8min, 16min, ...
    delay = BASE_DELAY_SECONDS * (2**retry_count)
    delay = min(delay, MAX_DELAY_SECONDS)

    # Add jitter (±25% by default)
    jitter = delay * JITTER_FACTOR * (random.random() * 2 - 1)  # noqa: S311 - retry jitter, not security-sensitive
    delay = max(BASE_DELAY_SECONDS, delay + jitter)

    return _utcnow() + timedelta(seconds=delay)


class NotificationSpooler:
    """
    Persistent notification queue with retry logic.

    Stores failed notifications in SQLite and retries them with
    exponential backoff until successful or max retries exceeded.
    """

    def __init__(self, db_conn_factory: Callable[[], sqlite3.Connection]):
        """
        Initialize the spooler.

        Args:
            db_conn_factory: Factory function that returns a new DB connection

        Raises:
            sqlite3.Error: If database is inaccessible or corrupt

        Note: _ensure_table() can raise exceptions during initialization.
        Callers should be prepared to handle this, especially in singleton
        patterns where repeated failures could cause retry loops.
        """
        self._db_conn_factory = db_conn_factory
        self._ensure_table()

    @contextmanager
    def _db_conn(self, immediate: bool = False):
        """
        Context manager for database connections with automatic transaction handling.

        Args:
            immediate: If True, use BEGIN IMMEDIATE for write-intent locking
        """
        conn = self._db_conn_factory()
        try:
            # Prevent SQLITE_BUSY errors on slow filesystems or concurrent access
            conn.execute("PRAGMA busy_timeout = 5000")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            # Auto-commit if transaction was started
            if immediate:
                conn.commit()
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            with suppress(Exception):
                sqlite_db.close_connection(conn)

    def _ensure_table(self) -> None:
        """Create the notification_queue table if it doesn't exist.

        The definition lives in sqlite_db.ensure_notification_queue_table(), shared with init_schema().

        The WAL journal mode is enabled once by sqlite_db.ensure_wal_mode() at initialization;
        it cannot be changed inside the BEGIN IMMEDIATE transaction used here.
        """
        with self._db_conn(immediate=True) as conn:
            sqlite_db.ensure_notification_queue_table(conn)

    def is_channel_idle(self, channel_type: ChannelType | str) -> bool:
        """
        Check if a channel has no pending or processing notifications.

        Used to determine if immediate sending is safe (no queue backlog).

        WARNING: This is a point-in-time check. The result may be stale
        by the time the caller acts on it (TOCTOU race). For strict ordering
        guarantees, always enqueue and let the spooler process in order.

        Args:
            channel_type: The channel to check (signal, email)

        Returns:
            True if no pending/processing items for this channel
        """
        try:
            if isinstance(channel_type, str):
                channel_type = ChannelType(channel_type)
        except ValueError:
            raise ValueError(
                f"Unknown channel type: {channel_type!r}. Valid: {[c.value for c in ChannelType]}"
            ) from None

        with self._db_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM notification_queue
                WHERE channel_type = ? AND status IN (?, ?)
                """,
                (channel_type.value, QueueStatus.PENDING.value, QueueStatus.PROCESSING.value),
            ).fetchone()
            return row[0] == 0 if row else True

    def enqueue(
        self,
        notification_type: NotificationType | str,
        channel_type: ChannelType | str,
        recipient_id: int,
        recipient_name: str,
        recipient_address: str,
        payload: dict[str, Any],
        error: str | None = None,
        immediate: bool = False,
        target_id: int | None = None,
    ) -> int:
        """
        Add a notification to the persistent delivery queue.

        Alert IDs are idempotent per recipient and channel. Signal test messages
        are deduplicated while pending and have bounded enqueue rates.

        Args:
            notification_type: Type of notification (welcome, alert, test)
            channel_type: Channel (signal, email)
            recipient_id: ID of the recipient (None for test messages)
            recipient_name: Name of the recipient (for logging)
            recipient_address: Phone number or email address
            payload: JSON-serializable payload with message details
            error: The error that caused the initial failure
            immediate: If True, set next_retry_at to now (process immediately)
            target_id: ID of the associated target (None for test messages)

        Returns:
            ID of the queued notification
        """
        now = _utcnow()
        # If immediate=True, set next_retry to now so it's picked up immediately
        next_retry = now if immediate else _calculate_next_retry(0)

        # Normalize and validate enums
        try:
            if isinstance(notification_type, str):
                notification_type = NotificationType(notification_type)
            if isinstance(channel_type, str):
                channel_type = ChannelType(channel_type)
        except ValueError as e:
            raise ValueError(
                f"Invalid notification/channel type: {e}. "
                f"Valid types: {[t.value for t in NotificationType]}, "
                f"Valid channels: {[c.value for c in ChannelType]}"
            ) from e

        try:
            payload_json = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Payload is not JSON-serializable: {e}") from e

        with self._db_conn(immediate=True) as conn:
            if notification_type == NotificationType.ALERT and isinstance(payload.get("alert_id"), str):
                duplicate = conn.execute(
                    """
                    SELECT id FROM notification_queue
                    WHERE notification_type = ? AND channel_type = ? AND recipient_id = ?
                      AND CASE WHEN json_valid(payload) THEN json_extract(payload, '$.alert_id') END = ?
                      AND CASE WHEN json_valid(payload)
                               THEN COALESCE(json_extract(payload, '$.alert_event'), 'open') END = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        NotificationType.ALERT.value,
                        channel_type.value,
                        recipient_id,
                        payload["alert_id"],
                        # Legacy payloads without the field count as the open phase
                        payload.get("alert_event") or "open",
                    ),
                ).fetchone()
                if duplicate:
                    return duplicate[0]

            if channel_type == ChannelType.SIGNAL and notification_type == NotificationType.TEST:
                duplicate = conn.execute(
                    """
                    SELECT id FROM notification_queue
                    WHERE channel_type = ? AND notification_type = ?
                      AND recipient_address = ? AND payload = ?
                      AND status IN (?, ?)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        ChannelType.SIGNAL.value,
                        NotificationType.TEST.value,
                        recipient_address,
                        payload_json,
                        QueueStatus.PENDING.value,
                        QueueStatus.PROCESSING.value,
                    ),
                ).fetchone()
                if duplicate:
                    return duplicate[0]

                hour_ago = (now - timedelta(hours=1)).isoformat()
                ten_minutes_ago = (now - timedelta(minutes=10)).isoformat()
                counts = conn.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE recipient_address = ? AND created_at >= ?)
                    FROM notification_queue
                    WHERE channel_type = ? AND notification_type = ? AND created_at >= ?
                    """,
                    (
                        recipient_address,
                        ten_minutes_ago,
                        ChannelType.SIGNAL.value,
                        NotificationType.TEST.value,
                        hour_ago,
                    ),
                ).fetchone()
                pending = conn.execute(
                    """
                    SELECT COUNT(*) FROM notification_queue
                    WHERE channel_type = ? AND notification_type = ? AND status IN (?, ?)
                    """,
                    (
                        ChannelType.SIGNAL.value,
                        NotificationType.TEST.value,
                        QueueStatus.PENDING.value,
                        QueueStatus.PROCESSING.value,
                    ),
                ).fetchone()[0]
                if (
                    counts[0] >= SIGNAL_TEST_HOURLY_LIMIT
                    or counts[1] >= SIGNAL_TEST_RECIPIENT_LIMIT
                    or pending >= SIGNAL_TEST_PENDING_LIMIT
                ):
                    raise SignalQueueLimitError("Signal test-message limit reached; try again later")

            cursor = conn.execute(
                """
                INSERT INTO notification_queue (
                    notification_type, channel_type, recipient_id, recipient_name,
                    recipient_address, payload, status, retry_count, next_retry_at,
                    last_error, created_at, updated_at, target_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_type.value,
                    channel_type.value,
                    recipient_id,
                    recipient_name,
                    recipient_address,
                    payload_json,
                    QueueStatus.PENDING.value,
                    0,
                    next_retry.isoformat(),
                    error,
                    now.isoformat(),
                    now.isoformat(),
                    target_id,
                ),
            )
            queue_id = cursor.lastrowid

        _log.info(
            "Queued %s notification for %s via %s (id=%d, next_retry=%s)",
            notification_type.value,
            recipient_name,
            channel_type.value,
            queue_id,
            next_retry.strftime("%H:%M:%S"),
        )

        return queue_id

    def get_queue_stats_detailed(self) -> dict[str, Any]:
        """
        Get detailed queue statistics with time dimension.

        Useful for debugging and operational monitoring.

        Returns:
            Dict with:
            - by_status: count per status
            - oldest_pending: ISO timestamp of oldest pending entry
            - max_retry_count: highest retry count in queue
            - next_retry_at: ISO timestamp of next scheduled retry
            - stuck_processing: count of PROCESSING entries older than timeout
        """
        now = _utcnow()
        processing_cutoff = (now - timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)).isoformat()

        with self._db_conn() as conn:
            # Count by status
            status_rows = conn.execute("SELECT status, COUNT(*) FROM notification_queue GROUP BY status").fetchall()
            by_status = {row[0]: row[1] for row in status_rows}

            # Oldest pending entry
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM notification_queue WHERE status = ?",
                (QueueStatus.PENDING.value,),
            ).fetchone()
            oldest_pending = oldest[0] if oldest and oldest[0] else None

            # Max retry count (longest retry chain)
            max_retry = conn.execute(
                "SELECT MAX(retry_count) FROM notification_queue WHERE status IN (?, ?)",
                (QueueStatus.PENDING.value, QueueStatus.PROCESSING.value),
            ).fetchone()
            max_retry_count = max_retry[0] if max_retry and max_retry[0] else 0

            # Next scheduled retry
            next_retry = conn.execute(
                "SELECT MIN(next_retry_at) FROM notification_queue WHERE status = ?",
                (QueueStatus.PENDING.value,),
            ).fetchone()
            next_retry_at = next_retry[0] if next_retry and next_retry[0] else None

            # Count stuck PROCESSING entries (crash recovery candidates)
            stuck = conn.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE status = ? AND updated_at < ?",
                (QueueStatus.PROCESSING.value, processing_cutoff),
            ).fetchone()
            stuck_processing = stuck[0] if stuck else 0

            return {
                "by_status": by_status,
                "oldest_pending": oldest_pending,
                "max_retry_count": max_retry_count,
                "next_retry_at": next_retry_at,
                "stuck_processing": stuck_processing,
                "processing_timeout_seconds": PROCESSING_TIMEOUT_SECONDS,
            }

    def get_log(
        self,
        limit: int = 100,
        offset: int = 0,
        status_filter: str | None = None,
        channel_filter: str | None = None,
    ) -> dict[str, Any]:
        """
        Get notification log entries for the spooler UI.

        Returns entries ordered by most recent first, with pagination.

        Args:
            limit: Max entries to return (capped at 500)
            offset: Pagination offset
            status_filter: Optional status filter (pending/processing/sent/failed)
            channel_filter: Optional channel filter (signal/email)

        Returns:
            Dict with 'entries' list and 'total' count.
        """
        limit = min(limit, 500)
        conditions: list[str] = []
        params: list[Any] = []

        if status_filter:
            conditions.append("nq.status = ?")
            params.append(status_filter)
        if channel_filter:
            conditions.append("nq.channel_type = ?")
            params.append(channel_filter)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._db_conn() as conn:
            total_row = conn.execute(f"SELECT COUNT(*) FROM notification_queue nq {where}", params).fetchone()  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
            total = total_row[0] if total_row else 0

            rows = conn.execute(
                f"""
                SELECT nq.id, nq.notification_type, nq.channel_type, nq.recipient_name,
                       nq.recipient_address, nq.status, nq.retry_count, nq.last_error,
                       nq.created_at, nq.updated_at, nq.target_id, t.name as target_name
                FROM notification_queue nq
                LEFT JOIN targets t ON t.id = nq.target_id
                {where}
                ORDER BY nq.updated_at DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
                [*params, limit, offset],
            ).fetchall()

            entries = [
                {
                    "id": r[0],
                    "notification_type": r[1],
                    "channel_type": r[2],
                    "recipient_name": r[3],
                    "recipient_address": r[4],
                    "status": r[5],
                    "retry_count": r[6],
                    "last_error": r[7],
                    "created_at": r[8],
                    "updated_at": r[9],
                    "target_id": r[10],
                    "target_name": r[11],
                }
                for r in rows
            ]

        return {"entries": entries, "total": total}

    def flush_queue(self) -> int:
        """
        Delete all pending and failed entries from the queue.

        Returns:
            Number of entries deleted.
        """
        with self._db_conn(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM notification_queue WHERE status IN (?, ?)",
                (QueueStatus.PENDING.value, QueueStatus.FAILED.value),
            )
            deleted = cursor.rowcount

        if deleted > 0:
            _log.info("Flushed %d pending/failed notifications from queue", deleted)

        return deleted

    def clear_log(self) -> int:
        """
        Delete all sent notifications from the log (keep pending/processing/failed).

        Returns:
            Number of entries deleted.
        """
        with self._db_conn(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM notification_queue WHERE status = ?",
                (QueueStatus.SENT.value,),
            )
            deleted = cursor.rowcount

        if deleted > 0:
            _log.info("Cleared %d sent notifications from log", deleted)

        return deleted

    def get_pending_items(self, limit: int = PROCESS_BATCH_SIZE) -> list[QueuedNotification]:
        """
        Get pending notifications that are ready for retry.

        Also recovers stuck PROCESSING entries that have timed out (crash recovery).

        Applies channel-specific batch limits to prevent rate-limiting:
        - Signal: max SIGNAL_BATCH_LIMIT (1) per run (very rate-limited)
        - Email: max EMAIL_BATCH_LIMIT (10) per run

        The candidate read and claim share one immediate transaction so another
        worker cannot claim the same item between those steps.

        Args:
            limit: Maximum total number of items to fetch from DB

        Returns:
            List of QueuedNotification objects ready for processing (already claimed)
        """
        now = _utcnow()
        processing_cutoff = (now - timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)).isoformat()

        all_items: list[QueuedNotification] = []

        # Helper to parse timestamps with timezone fallback
        def _parse_ts(ts_str: str) -> datetime:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt

        with self._db_conn(immediate=True) as conn:
            # Rank by channel before applying the total limit so a Signal backlog cannot hide email.
            candidate_rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT id, channel_type, status, next_retry_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY channel_type ORDER BY next_retry_at, id
                           ) AS channel_rank
                    FROM notification_queue
                    WHERE (status = ? AND next_retry_at <= ?)
                       OR (status = ? AND updated_at < ?)
                )
                SELECT id, channel_type, status FROM ranked
                WHERE (channel_type = ? AND channel_rank <= ?)
                   OR (channel_type = ? AND channel_rank <= ?)
                   OR channel_type NOT IN (?, ?)
                ORDER BY next_retry_at, id
                LIMIT ?
                """,
                (
                    QueueStatus.PENDING.value,
                    now.isoformat(),
                    QueueStatus.PROCESSING.value,
                    processing_cutoff,
                    ChannelType.SIGNAL.value,
                    SIGNAL_BATCH_LIMIT,
                    ChannelType.EMAIL.value,
                    EMAIL_BATCH_LIMIT,
                    ChannelType.SIGNAL.value,
                    ChannelType.EMAIL.value,
                    limit,
                ),
            ).fetchall()

            ids_to_claim = [row[0] for row in candidate_rows]
            stuck_processing_ids = {row[0] for row in candidate_rows if row[2] == QueueStatus.PROCESSING.value}

            # Increment retry_count for stuck PROCESSING items (crash recovery)
            # This prevents infinite loops if a worker keeps crashing
            # Limit last_error growth: truncate if >1000 chars before appending
            if stuck_processing_ids:
                stuck_placeholders = ",".join("?" * len(stuck_processing_ids))
                conn.execute(
                    f"""
                    UPDATE notification_queue
                    SET retry_count = retry_count + 1,
                        last_error = CASE
                            WHEN LENGTH(COALESCE(last_error, '')) > 1000
                            THEN '[...] ' || SUBSTR(last_error, -400) || ' [RECOVERED]'
                            ELSE COALESCE(last_error, '') || ' [RECOVERED]'
                        END
                    WHERE id IN ({stuck_placeholders})
                    """,  # noqa: S608 - placeholder count only, ids bound as params
                    list(stuck_processing_ids),
                )

                # Mark items that exceeded max retries as FAILED
                conn.execute(
                    f"""
                    UPDATE notification_queue
                    SET status = ?,
                        last_error = COALESCE(last_error, '') || ' [MAX RETRIES EXCEEDED after recovery]'
                    WHERE id IN ({stuck_placeholders}) AND retry_count > ?
                    """,  # noqa: S608 - placeholder count only, ids bound as params
                    [QueueStatus.FAILED.value, *list(stuck_processing_ids), MAX_RETRIES],
                )

                # Get IDs that were marked as failed to exclude from claim
                failed_rows = conn.execute(
                    f"""
                    SELECT id FROM notification_queue
                    WHERE id IN ({stuck_placeholders}) AND status = ?
                    """,  # noqa: S608 - placeholder count only, ids bound as params
                    [*list(stuck_processing_ids), QueueStatus.FAILED.value],
                ).fetchall()
                failed_ids = {row[0] for row in failed_rows}

                # Remove failed items from claim list
                ids_to_claim = [claim_id for claim_id in ids_to_claim if claim_id not in failed_ids]

                _log.warning(
                    "Recovered %d stuck PROCESSING items (retry_count incremented), %d marked FAILED",
                    len(stuck_processing_ids),
                    len(failed_ids),
                )

            if not ids_to_claim:
                return []

            # Atomically claim selected items by updating status to PROCESSING
            # Include status check for full idempotency (prevents race with other workers)
            placeholders = ",".join("?" * len(ids_to_claim))
            conn.execute(
                f"""
                UPDATE notification_queue
                SET status = ?, updated_at = ?
                WHERE id IN ({placeholders})
                  AND (
                    (status = ? AND next_retry_at <= ?)
                    OR (status = ? AND updated_at < ?)
                  )
                """,  # noqa: S608 - placeholder count only, ids bound as params
                [
                    QueueStatus.PROCESSING.value,
                    now.isoformat(),
                    *ids_to_claim,
                    QueueStatus.PENDING.value,
                    now.isoformat(),
                    QueueStatus.PROCESSING.value,
                    processing_cutoff,
                ],
            )

            # Fetch full data for claimed items
            rows = conn.execute(
                f"""
                SELECT id, notification_type, channel_type, recipient_id,
                       recipient_name, recipient_address, payload, status,
                       retry_count, next_retry_at, last_error, created_at, updated_at
                FROM notification_queue
                WHERE id IN ({placeholders})
                ORDER BY next_retry_at ASC
                """,  # noqa: S608 - placeholder count only, ids bound as params
                ids_to_claim,
            ).fetchall()

        # Parse rows AFTER commit to ensure claims persist even if parsing fails
        # Individual parsing errors release the item back to PENDING for retry
        for row in rows:
            try:
                all_items.append(
                    QueuedNotification(
                        id=row[0],
                        notification_type=NotificationType(row[1]),
                        channel_type=ChannelType(row[2]),
                        recipient_id=row[3],
                        recipient_name=row[4],
                        recipient_address=row[5],
                        payload=json.loads(row[6]),
                        status=QueueStatus(row[7]),
                        retry_count=row[8],
                        next_retry_at=_parse_ts(row[9]),
                        last_error=row[10],
                        created_at=_parse_ts(row[11]),
                        updated_at=_parse_ts(row[12]),
                    )
                )
            except Exception as e:  # noqa: BLE001 - queue-worker boundary: a bad item is logged and released/failed, never crashes the worker
                # A corrupt row cannot heal by retrying: fail it permanently instead of hot-looping.
                _log.error("Failed to parse queued item %d, marking FAILED: %s", row[0], e)
                try:
                    with self._db_conn(immediate=True) as release_conn:
                        cur = release_conn.execute(
                            "UPDATE notification_queue SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                            (QueueStatus.FAILED.value, f"Parse error: {e}", now.isoformat(), row[0]),
                        )
                        if cur.rowcount:
                            self._record_delivery(release_conn, row[0], sent=False)
                except sqlite3.Error as release_exc:
                    # The timeout mechanism recovers the item later.
                    _log.warning("Could not release queued item %d: %s", row[0], release_exc)

        return all_items

    @staticmethod
    def _record_delivery(conn: sqlite3.Connection, queue_id: int, *, sent: bool) -> None:
        """Write the inbox event for a final delivery state in the same transaction as the status change.

        Retries are intentionally silent: only a confirmed send or a permanent failure is an event.
        """
        row = conn.execute(
            "SELECT channel_type, target_id FROM notification_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if row is None or row[0] not in ("email", "signal"):
            return
        if sent:
            inbox.emit_delivery_sent(conn, row[0])
        else:
            inbox.notify_delivery_failed(conn, queue_id=queue_id, channel=row[0], target_id=row[1])

    def mark_processing(self, queue_id: int) -> None:
        """Mark a notification as being processed.

        Note: With the new atomic claim in get_pending_items(), this is now
        only needed for explicit re-processing or external callers.
        Items returned by get_pending_items() are already marked PROCESSING.
        """
        now = _utcnow()
        with self._db_conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (QueueStatus.PROCESSING.value, now.isoformat(), queue_id),
            )

    def mark_sent(self, queue_id: int) -> None:
        """Mark a notification as successfully sent."""
        now = _utcnow()
        with self._db_conn(immediate=True) as conn:
            # Only a real pending/processing -> sent transition is an event; a repeated call is not.
            cur = conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    QueueStatus.SENT.value,
                    now.isoformat(),
                    queue_id,
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                ),
            )
            if cur.rowcount:
                self._record_delivery(conn, queue_id, sent=True)
        _log.info("Notification %d sent successfully", queue_id)

    def renew_lease(self, queue_id: int, held_stamp: str) -> str | None:
        """Extend the PROCESSING claim of one item; returns the new stamp, or None if the claim was lost.

        Items of a batch are sent one after another, so a late item could outlive
        PROCESSING_TIMEOUT_SECONDS and be re-claimed by recovery while still unsent here. The lease is
        only renewed while the row still carries the stamp this worker holds.
        """
        stamp = _utcnow().isoformat()
        with self._db_conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE notification_queue SET updated_at = ? WHERE id = ? AND status = ? AND updated_at = ?",
                (stamp, queue_id, QueueStatus.PROCESSING.value, held_stamp),
            )
        return stamp if cur.rowcount else None

    def release(self, queue_id: int) -> bool:
        """Give a claimed item back to PENDING without counting an attempt (nothing was sent)."""
        with self._db_conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE notification_queue SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (QueueStatus.PENDING.value, _utcnow().isoformat(), queue_id, QueueStatus.PROCESSING.value),
            )
            return bool(cur.rowcount)

    def mark_retry(
        self, queue_id: int, error: str, delay_seconds: int | None = None, *, uncertain: bool = False
    ) -> bool:
        """
        Mark a notification for retry after a failure (atomic implementation).

        Uses atomic UPDATE + SELECT to prevent race conditions when multiple
        workers or the recovery process increment retry_count simultaneously.

        Args:
            queue_id: ID of the queued notification
            error: Error message from the failed attempt (truncated if >500 chars)
            delay_seconds: Optional custom delay override (e.g., for rate limiting)
            uncertain: The send outcome is unconfirmed; the error is tagged with UNCERTAIN_MARKER

        Returns:
            True if scheduled for retry; False if max retries exceeded, the job is unknown, or it
            already reached a terminal state (SENT/FAILED), which a late retry must never reset.
        """
        now = _utcnow()

        # Truncate error to prevent unbounded growth
        if uncertain:
            error = f"{UNCERTAIN_MARKER} {error}"
        if len(error) > 500:
            error = error[:497] + "..."

        with self._db_conn(immediate=True) as conn:
            # Atomic increment + read in one transaction. The status guard keeps a late retry from
            # resurrecting a terminal job: SENT and FAILED must never be reset to PENDING.
            cur = conn.execute(
                "UPDATE notification_queue SET retry_count = retry_count + 1 WHERE id = ? AND status IN (?, ?)",
                (queue_id, QueueStatus.PENDING.value, QueueStatus.PROCESSING.value),
            )
            if not cur.rowcount:
                return False
            row = conn.execute(
                "SELECT retry_count FROM notification_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()

            if not row:
                return False

            retry_count = row[0]

            if retry_count > MAX_RETRIES:
                # Max retries exceeded - mark as permanently failed
                cur = conn.execute(
                    """
                    UPDATE notification_queue
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        QueueStatus.FAILED.value,
                        error,
                        now.isoformat(),
                        queue_id,
                        QueueStatus.PENDING.value,
                        QueueStatus.PROCESSING.value,
                    ),
                )
                if cur.rowcount:
                    self._record_delivery(conn, queue_id, sent=False)
                _log.warning(
                    "Notification %d permanently failed after %d retries: %s",
                    queue_id,
                    retry_count,
                    error,
                )
                return False

            # Schedule next retry (use custom delay if provided, otherwise exponential backoff)
            next_retry = (
                (now + timedelta(seconds=delay_seconds)) if delay_seconds else _calculate_next_retry(retry_count - 1)
            )
            cur = conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    QueueStatus.PENDING.value,
                    next_retry.isoformat(),
                    error,
                    now.isoformat(),
                    queue_id,
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                ),
            )
            if not cur.rowcount:
                # The job reached a terminal state between the increment and now; do not reschedule.
                return False

            _log.info(
                "Notification %d scheduled for retry %d/%d at %s",
                queue_id,
                retry_count,
                MAX_RETRIES,
                next_retry.strftime("%H:%M:%S"),
            )
            return True

    def mark_failed_permanent(self, queue_id: int, error: str) -> None:
        """
        Mark a notification as permanently failed (no further retries).

        Use this for errors that cannot be resolved by retrying:
        - Invalid recipient (phone number/email doesn't exist)
        - Sender not registered
        - Configuration errors

        Args:
            queue_id: ID of the queued notification
            error: Error message describing why it failed permanently (truncated if >500 chars)
        """
        now = _utcnow()

        # Truncate error to prevent unbounded growth
        if len(error) > 500:
            error = error[:497] + "..."

        with self._db_conn(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    QueueStatus.FAILED.value,
                    f"[PERMANENT] {error}",
                    now.isoformat(),
                    queue_id,
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                ),
            )
            if cur.rowcount:
                self._record_delivery(conn, queue_id, sent=False)

        _log.warning(
            "Notification %d marked as permanently failed (no retry): %s",
            queue_id,
            error,
        )

    @staticmethod
    def _send_item(
        item: QueuedNotification,
        send_signal: Callable[[str, str, str], bool] | None,
        send_email: Callable[[str, str, str, str], bool] | None,
        *,
        throttle: bool,
    ) -> DeliveryResult:
        """Perform the blocking network send for one claimed item. Never touches the queue DB."""
        try:
            if item.channel_type == ChannelType.SIGNAL:
                if not send_signal:
                    return DeliveryResult("retry", "Signal sender not configured")
                if throttle:
                    # Signal servers are very rate-limit sensitive; sleep only between consecutive sends.
                    time.sleep(SIGNAL_THROTTLE_SECONDS)
                ok = send_signal(
                    item.recipient_address, item.payload.get("message", ""), item.payload.get("sender", "")
                )
                return DeliveryResult("sent") if ok else DeliveryResult("retry", "send_signal returned False")
            if item.channel_type == ChannelType.EMAIL:
                if not send_email:
                    return DeliveryResult("retry", "Email sender not configured")
                email_args = (
                    item.recipient_address,
                    item.payload.get("subject", ""),
                    item.payload.get("text_body", ""),
                    item.payload.get("html_body", ""),
                )
                # Retries of one queue row carry the same key, so the recipient can drop duplicates.
                if _accepts_kwarg(send_email, "message_id_key"):
                    ok = send_email(*email_args, message_id_key=_message_id_key(item))
                else:
                    ok = send_email(*email_args)
                return DeliveryResult("sent") if ok else DeliveryResult("retry", "send_email returned False")
            return DeliveryResult("retry", "Unknown error")
        except SignalRateLimited as e:
            _log.warning(
                "Signal rate limited for notification %d, entering %ds cooldown", item.id, e.retry_after_seconds
            )
            return DeliveryResult("retry", str(e), e.retry_after_seconds)
        except (SignalRecipientInvalid, SignalNotRegistered) as e:
            _log.warning("Permanent Signal error for notification %d, marking failed: %s", item.id, e)
            return DeliveryResult("permanent", str(e))
        except maintenance.MaintenanceActiveError:
            # Raised by the sender before any I/O (it could not read its settings): nothing was sent.
            return DeliveryResult("deferred", "maintenance")
        except _UNCERTAIN_EXCEPTIONS as e:
            _log.warning("Delivery outcome of notification %d is unconfirmed: %s", item.id, e)
            return DeliveryResult("uncertain", f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - queue-worker boundary: a bad item is logged and retried, never crashes the worker
            _log.exception("Error processing queued notification %d", item.id)
            return DeliveryResult("retry", str(e))

    def _apply_result(self, item: QueuedNotification, result: DeliveryResult) -> None:
        """Write one delivery outcome (short DB phase)."""
        if result.kind == "sent":
            self.mark_sent(item.id)
        elif result.kind == "permanent":
            self.mark_failed_permanent(item.id, result.error)
        elif result.kind == "deferred":
            self.release(item.id)
        else:
            self.mark_retry(item.id, result.error, result.delay_seconds, uncertain=result.kind == "uncertain")

    def _finalize(
        self,
        item: QueuedNotification,
        result: DeliveryResult,
        section: Callable[[], AbstractContextManager[Any]],
    ) -> bool:
        """Apply an outcome inside a maintenance-aware section; True if it was persisted.

        An active maintenance window is waited out for a bounded time. A stale generation is final:
        the database was replaced, so the outcome is dropped and the restored row is retried
        (at-least-once) instead of writing into data it does not belong to.
        """
        deadline = time.monotonic() + FINALIZE_WAIT_SECONDS
        while True:
            try:
                with section():
                    self._apply_result(item, result)
                return True
            except maintenance.StaleWorkError:
                _log.warning(
                    "Notification %d: %s outcome dropped, data was replaced by a restore (will be retried)",
                    item.id,
                    result.kind,
                )
                return False
            except maintenance.MaintenanceActiveError:
                if time.monotonic() >= deadline:
                    _log.warning(
                        "Notification %d: %s outcome not recorded, maintenance outlasted the wait; "
                        "crash recovery will retry it",
                        item.id,
                        result.kind,
                    )
                    return False
                time.sleep(FINALIZE_POLL_SECONDS)

    def process_queue_sync(
        self,
        send_signal: Callable[[str, str, str], bool] | None = None,
        send_email: Callable[[str, str, str, str], bool] | None = None,
        *,
        section: Callable[[], AbstractContextManager[Any]] | None = None,
        may_send: Callable[[], bool] | None = None,
    ) -> tuple[int, int]:
        """
        Process pending notifications in the queue (SYNCHRONOUS version).

        Runs in three phases: a short claim (DB), the blocking network send per item (no DB, no
        maintenance section) and a short finalize (DB). ``section`` is a factory for the context
        manager that guards each DB phase (the scheduler passes a generation-tagged
        ``maintenance.work_section``); without it the phases are unguarded. ``may_send`` is checked
        before each send; when it returns False the remaining claimed items are released unsent.

        WARNING: Do NOT call this directly from async code. Use asyncio.to_thread().

        SEND FUNCTION CONTRACT:
        -----------------------
        1. Return True on success (message delivered)
        2. Raise specific exceptions for different failure modes:
           - SignalRateLimited: Rate limited, will use retry_after_seconds for backoff
           - SignalRecipientInvalid / SignalNotRegistered: permanent failure (no retry)
           - Timeout/disconnect while sending: outcome unconfirmed, retried tagged UNCERTAIN_MARKER
           - Any other Exception: Transient error, will retry with exponential backoff
        3. Return False is treated as transient error (will retry)

        Returns:
            Tuple of (processed_count, success_count)
        """
        # Guard against async context misuse
        try:
            import asyncio

            asyncio.get_running_loop()
            _log.warning(
                "process_queue_sync() called from async context! "
                "This will block the event loop. Use asyncio.to_thread()."
            )
        except RuntimeError:
            pass  # No running loop - correct usage

        section = section or nullcontext

        # Phase 1: claim. Items come back atomically marked PROCESSING (no second worker gets them).
        with section():
            items = self.get_pending_items()

        if not items:
            return (0, 0)

        _log.info("Processing %d queued notifications (atomically claimed)", len(items))

        processed = 0
        succeeded = 0
        last_signal_sent = False  # Track consecutive Signal sends for throttling

        for item in items:
            processed += 1

            # Phase 2: network I/O outside any maintenance section.
            # TOCTOU: a restore may begin right after this check. Accepted by design (at-least-once):
            # the finalize is then rejected as stale and the restored row is retried.
            if may_send is not None and not may_send():
                result = DeliveryResult("deferred", "maintenance")
            else:
                try:
                    with section():
                        lease = self.renew_lease(item.id, item.updated_at.isoformat())
                except (maintenance.StaleWorkError, maintenance.MaintenanceActiveError):
                    lease = ""  # window/restore in progress: defer instead of sending
                if lease is None:
                    _log.warning("Notification %d: claim lost before send, skipping", item.id)
                    processed -= 1
                    continue
                if lease == "":
                    result = DeliveryResult("deferred", "maintenance")
                else:
                    throttle = item.channel_type == ChannelType.SIGNAL and last_signal_sent
                    if item.channel_type == ChannelType.SIGNAL and send_signal:
                        last_signal_sent = True
                    result = self._send_item(item, send_signal, send_email, throttle=throttle)

            # Phase 3: finalize.
            persisted = self._finalize(item, result, section)
            if result.kind == "sent":
                succeeded += 1
                _log.info(
                    "Successfully sent queued %s to %s via %s (outcome %s)",
                    item.notification_type.value,
                    item.recipient_name,
                    item.channel_type.value,
                    "recorded" if persisted else "NOT recorded",
                )

        _log.info(
            "Queue processing complete: %d processed, %d succeeded, %d retried/failed",
            processed,
            succeeded,
            processed - succeeded,
        )

        return (processed, succeeded)


# ─── Module-level singleton for easy access ─────────────────────────────────────
#
# Note: This singleton pattern works for single-process deployments.
# For multi-process setups (e.g., gunicorn with multiple workers),
# each worker gets its own singleton instance - which is fine since
# the SQLite database provides the actual coordination/locking.
# The singleton is just a convenience to avoid re-creating the object.

_spooler: NotificationSpooler | None = None
_spooler_lock = threading.Lock()
_spooler_last_failure: float = 0
_SPOOLER_RETRY_INTERVAL = 60  # seconds - backoff after init failure


def get_spooler(db_conn_factory: Callable[[], sqlite3.Connection]) -> NotificationSpooler:
    """
    Get or create the global spooler instance (thread-safe singleton).

    Implements backoff after initialization failures to prevent log flooding
    when the database is persistently unavailable.

    Raises:
        RuntimeError: If spooler initialization recently failed (within retry interval)
        sqlite3.Error: If database initialization fails
    """
    global _spooler, _spooler_last_failure
    if _spooler is None:
        # Check if we should back off due to recent failure
        now = time.monotonic()
        if now - _spooler_last_failure < _SPOOLER_RETRY_INTERVAL:
            raise RuntimeError(
                f"Spooler initialization recently failed, backing off "
                f"for {int(_SPOOLER_RETRY_INTERVAL - (now - _spooler_last_failure))}s"
            )

        with _spooler_lock:
            # Double-checked locking to prevent race condition
            if _spooler is None:
                try:
                    _spooler = NotificationSpooler(db_conn_factory)
                except Exception:
                    _spooler_last_failure = time.monotonic()
                    raise
    return _spooler
