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
    from app.services.notifications.spooler import get_spooler

    spooler = get_spooler(db_conn_factory)
    spooler.enqueue(...)  # every notification is delivered through the queue
    await asyncio.to_thread(spooler.process_queue_sync, send_signal, send_email)  # scheduler
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import getaddresses
from enum import StrEnum
from typing import Any

from ...db import notifications as inbox
from ...db import sqlite as sqlite_db
from ...utils import maintenance
from ...utils.time import assume_utc
from ...utils.time import utcnow as _utcnow
from .delivery_policy import (
    CHANNEL_POLICIES,
    MAX_COOLDOWN_SECONDS,
    MAX_PACING_WAIT_SECONDS,
    MAX_RETRIES,
    MESSAGE_VALIDITY_SECONDS,
    Failure,
    FailureKind,
    backoff_delay_seconds,
    channel_key,
    channel_of_key,
    classify_failure,
)

_log = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────────

# Retry timing, message validity, pacing and failure classes live in delivery_policy.py.
# MAX_RETRIES is the budget of non-transient failures only (see FailureKind.LIMITED); transient
# failures retry until MESSAGE_VALIDITY_SECONDS. Crash recovery counts an uncertain attempt too.

# Processing timeout: recover stuck PROCESSING entries after this duration
# If the process crashes between mark_processing() and mark_sent()/mark_retry(),
# entries would be stuck forever. This timeout allows recovery.
PROCESSING_TIMEOUT_SECONDS = 600

PROCESS_BATCH_SIZE = 20

# Marker prefix in last_error for an attempt whose outcome is unconfirmed (bytes may have left the
# host). The row stays PENDING with an incremented retry_count: the retry is at-least-once and, as it
# reuses the same queue row, a business event never becomes a second row.
UNCERTAIN_MARKER = "[UNCERTAIN]"
# An email that keeps failing at the SMTP hand-off for this long raises one admin warning in the inbox.
HANDOFF_ALERT_AFTER_SECONDS = 15 * 60
HANDOFF_ALERT_MIN_ATTEMPTS = 2
# Fixed, content-free wording per failure class: no error text, address or credential reaches the inbox.
HANDOFF_CATEGORIES = {
    "transient": "SMTP server unreachable or temporary error",
    "rate_limited": "SMTP provider rate limit",
    "retry": "configuration, authentication or TLS error",
}
# {"since", "at", "kind"} of the running email hand-off failure series; removed by the next accepted mail.
_LAST_FAILURE_SETTING = "spooler.email_handoff_failures"


def _other_stuck_email(conn: sqlite3.Connection, queue_id: int) -> bool:
    """True while another email (not a test) still waits in retries after repeated hand-off failures."""
    return (
        conn.execute(
            "SELECT 1 FROM notification_queue WHERE channel_type = ? AND status IN (?, ?) AND notification_type != ? "
            "AND retry_count >= ? AND id != ? LIMIT 1",
            (
                ChannelType.EMAIL.value,
                QueueStatus.PENDING.value,
                QueueStatus.PROCESSING.value,
                NotificationType.TEST.value,
                HANDOFF_ALERT_MIN_ATTEMPTS,
                queue_id,
            ),
        ).fetchone()
        is not None
    )


def handoff_incident(channel: str) -> str:
    """Inbox incident name of one channel's hand-off outage (only email has an aggregated detector)."""
    return f"delivery.{channel}.handoff"


# Window in which a failed delivery still counts as current on the dashboard and in Settings.
RECENT_FAILURE_SECONDS = 24 * 3600

# A test message is never retried, so any earlier attempt (retry_count > 0) makes it final. The status
# API and the spooler both use this one predicate (rows queued by older versions are still PENDING).
TEST_FINAL_SQL = "retry_count > 0"
# Bounded wait for a finalize that meets an active maintenance window (not a stale generation).
FINALIZE_WAIT_SECONDS = 30.0
FINALIZE_POLL_SECONDS = 0.25

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

    kind: str  # sent | transient | rate_limited | retry (limited budget) | permanent | deferred
    error: str = ""
    delay_seconds: int | None = None
    uncertain: bool = False
    cooldown_seconds: int | None = None


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


def _expired_error(last_error: str | None) -> str:
    base = f"[EXPIRED] not delivered within {MESSAGE_VALIDITY_SECONDS // 3600} h"
    return f"{base}; last error: {last_error}" if last_error else base


def _item_key(item: QueuedNotification) -> str:
    return channel_key(item.channel_type.value, item.payload.get("sender"))


_RESULT_KIND = {
    FailureKind.TRANSIENT: "transient",
    FailureKind.RATE_LIMITED: "rate_limited",
    FailureKind.LIMITED: "retry",
    FailureKind.PERMANENT: "permanent",
}


def _result_for(failure: Failure) -> DeliveryResult:
    return DeliveryResult(
        _RESULT_KIND[failure.kind],
        failure.reason,
        delay_seconds=failure.cooldown_seconds,
        uncertain=failure.uncertain,
        cooldown_seconds=failure.cooldown_seconds,
    )


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _parse_ts(ts_str: str) -> datetime:
    """Parse a stored ISO timestamp; naive values count as UTC."""
    return assume_utc(datetime.fromisoformat(ts_str))


def _calculate_next_retry(retry_count: int) -> datetime:
    """Next attempt time after ``retry_count`` earlier retries (exponential backoff with jitter)."""
    return _utcnow() + timedelta(seconds=backoff_delay_seconds(retry_count))


# Stable state key of a queue row's send path (Signal: per sending account). Same rule as channel_key().
_CHANNEL_KEY_SQL = (
    "CASE WHEN {t}channel_type = 'signal' THEN 'signal:' || "
    "COALESCE(CASE WHEN json_valid({t}payload) THEN TRIM(json_extract({t}payload, '$.sender')) END, '') "
    "ELSE {t}channel_type END"
)
# An alert event waits behind an older unsent one of the same alert, recipient and channel, so a
# "resolved" message can never overtake the "open" message it belongs to.
_ALERT_KEY_SQL = "CASE WHEN json_valid({t}payload) THEN json_extract({t}payload, '$.alert_id') END"
_ALERT_EVENT_SQL = (
    "CASE WHEN json_valid({t}payload) THEN COALESCE(json_extract({t}payload, '$.alert_event'), 'open') END"
)
_HELD_BEHIND_OPEN_SQL = f"""
    q.notification_type = 'alert' AND {_ALERT_EVENT_SQL.format(t="q.")} = 'resolved'
    AND EXISTS (
        SELECT 1 FROM notification_queue o
        WHERE o.id < q.id AND o.status IN ('pending', 'processing') AND o.notification_type = 'alert'
          AND o.channel_type = q.channel_type AND o.recipient_id IS q.recipient_id
          AND {_ALERT_KEY_SQL.format(t="o.")} = {_ALERT_KEY_SQL.format(t="q.")}
          AND {_ALERT_EVENT_SQL.format(t="o.")} = 'open'
    )
"""  # noqa: S608 - fixed SQL fragments, no user input


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
            self._finalize_retried_tests(conn)

    @staticmethod
    def _finalize_retried_tests(conn: sqlite3.Connection) -> int:
        """Close PENDING test messages that already had an attempt (queued by an older version); idempotent."""
        cur = conn.execute(
            f"""
            UPDATE notification_queue
            SET status = ?, last_error = COALESCE(last_error, '') || ' [PERMANENT] test message is never retried',
                updated_at = ?
            WHERE notification_type = ? AND status = ? AND {TEST_FINAL_SQL}
            """,  # noqa: S608 - constant predicate, no user input
            (QueueStatus.FAILED.value, _utcnow().isoformat(), NotificationType.TEST.value, QueueStatus.PENDING.value),
        )
        if cur.rowcount:
            _log.warning("Closed %d pending test message(s) that had earlier failed attempts", cur.rowcount)
        return cur.rowcount

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
                    f"""
                    SELECT id FROM notification_queue
                    WHERE notification_type = ? AND channel_type = ? AND recipient_id = ?
                      AND {_ALERT_KEY_SQL.format(t="")} = ?
                      AND {_ALERT_EVENT_SQL.format(t="")} = ?
                    ORDER BY id DESC LIMIT 1
                    """,  # noqa: S608 - fixed SQL fragments, all values bound as params
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
            - failed_recent: failed entries finalized within RECENT_FAILURE_SECONDS (what still counts as current)
            - oldest_pending: ISO timestamp of oldest pending entry
            - max_retry_count: highest retry count in queue
            - next_retry_at: ISO timestamp of next scheduled retry
            - stuck_processing: count of PROCESSING entries older than timeout
        """
        now = _utcnow()
        processing_cutoff = (now - timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)).isoformat()

        with self._db_conn() as conn:
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

            channels, cooldowns = self._channel_overview(conn, now)
            failed_recent = conn.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE status = ? AND updated_at >= ?",
                (QueueStatus.FAILED.value, (now - timedelta(seconds=RECENT_FAILURE_SECONDS)).isoformat()),
            ).fetchone()[0]
            return {
                "failed_recent": failed_recent,
                "channels": channels,
                "cooldowns": cooldowns,
                "by_status": by_status,
                "oldest_pending": oldest_pending,
                "max_retry_count": max_retry_count,
                "next_retry_at": next_retry_at,
                "stuck_processing": stuck_processing,
                "processing_timeout_seconds": PROCESSING_TIMEOUT_SECONDS,
            }

    @staticmethod
    def _channel_overview(conn: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Per-channel queue figures and the active provider cooldowns (sender identities are not exposed)."""
        cooldowns = [
            {"channel": channel_of_key(key), "until": until, "reason": reason}
            for key, until, reason in conn.execute(
                "SELECT channel_key, cooldown_until, cooldown_reason FROM notification_channel_state "
                "WHERE cooldown_until > ? ORDER BY cooldown_until",
                (now.isoformat(),),
            )
        ]
        channels: dict[str, Any] = {}
        for channel in CHANNEL_POLICIES:
            waiting, oldest, next_retry = conn.execute(
                """
                SELECT COUNT(*) FILTER (WHERE status = ?), MIN(created_at), MIN(next_retry_at) FILTER (WHERE status = ?)
                FROM notification_queue WHERE channel_type = ? AND status IN (?, ?)
                """,
                (
                    QueueStatus.PENDING.value,
                    QueueStatus.PENDING.value,
                    channel,
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                ),
            ).fetchone()
            failed = conn.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE channel_type = ? AND status = ?",
                (channel, QueueStatus.FAILED.value),
            ).fetchone()[0]
            held_until = min((c["until"] for c in cooldowns if c["channel"] == channel), default=None)
            next_attempt = max(filter(None, (next_retry, held_until)), default=None) if waiting else None
            channels[channel] = {
                "pending": waiting,
                "next_attempt_at": next_attempt,
                "oldest_at": oldest,
                "failed": failed,
                "cooldown_until": held_until,
            }
        return channels, cooldowns

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

        Applies the per-channel batch limits of CHANNEL_POLICIES, skips channels in cooldown and
        expires messages older than MESSAGE_VALIDITY_SECONDS before claiming.

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

        with self._db_conn(immediate=True) as conn:
            # Guard: a retried test message must never be claimed (see TEST_FINAL_SQL).
            self._finalize_retried_tests(conn)
            self._expire_stale(conn, now)
            blocked_keys = self._blocked_keys(conn, now)
            # Rank by channel before applying the total limit so a Signal backlog cannot hide email.
            # Channels or senders in cooldown (or still pacing) and held-back alert events are not candidates.
            candidate_rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT q.id, q.channel_type, q.status, q.next_retry_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY q.channel_type ORDER BY q.next_retry_at, q.id
                           ) AS channel_rank
                    FROM notification_queue q
                    WHERE ((q.status = ? AND q.next_retry_at <= ?) OR (q.status = ? AND q.updated_at < ?))
                      AND {_CHANNEL_KEY_SQL.format(t="q.")}
                          NOT IN (SELECT value FROM json_each(?))
                      AND NOT ({_HELD_BEHIND_OPEN_SQL})
                )
                SELECT id, channel_type, status FROM ranked
                WHERE channel_rank <= CASE channel_type WHEN 'signal' THEN ? WHEN 'email' THEN ? ELSE ? END
                ORDER BY next_retry_at, id
                LIMIT ?
                """,  # noqa: S608 - fixed SQL fragments, all values bound as params
                (
                    QueueStatus.PENDING.value,
                    now.isoformat(),
                    QueueStatus.PROCESSING.value,
                    processing_cutoff,
                    json.dumps(sorted(blocked_keys)),
                    CHANNEL_POLICIES["signal"].batch_limit,
                    CHANNEL_POLICIES["email"].batch_limit,
                    limit,
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
                        limited_attempts = limited_attempts + 1,
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
                        updated_at = ?,
                        last_error = COALESCE(last_error, '') || ' [MAX RETRIES EXCEEDED after recovery]'
                    WHERE id IN ({stuck_placeholders}) AND limited_attempts > ?
                    """,  # noqa: S608 - placeholder count only, ids bound as params
                    [QueueStatus.FAILED.value, now.isoformat(), *list(stuck_processing_ids), MAX_RETRIES],
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
                for failed_id in failed_ids:
                    self._record_delivery(conn, failed_id, sent=False)

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

        Retries are intentionally silent: only a confirmed hand-off, a final failure, or a long-running
        outage (see check_handoff_outage) is an event.
        """
        row = conn.execute(
            "SELECT channel_type, target_id, last_error FROM notification_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if row is None or row[0] not in ("email", "signal"):
            return
        channel = row[0]
        if sent:
            inbox.emit_delivery_sent(conn, channel)
            if channel != "email":
                return  # only email has an aggregated hand-off incident; Signal reports per message
            if _other_stuck_email(conn, queue_id):
                return  # one accepted mail does not end a disturbance that still holds other mails back
            sqlite_db.delete_setting(conn, _LAST_FAILURE_SETTING)
            inbox.resolve_incident(
                conn,
                handoff_incident(channel),
                title="Email handoff recovered",
                body="Channel: Email · SMTP handoff succeeded again; delivery to the mailbox is not verifiable",
                now=_utcnow(),
            )
            return
        error = row[2] or ""
        reason = (
            "not handed over within 24 h"
            if error.startswith("[EXPIRED]")
            else "rejected by the server"
            if error.startswith("[PERMANENT]")
            else "not handed over after all retries"
        )
        inbox.notify_delivery_failed(conn, queue_id=queue_id, channel=channel, target_id=row[1], reason=reason)
        if channel != "email":
            return
        inbox.update_incident(
            conn,
            handoff_incident(channel),
            title="Email handoff failed",
            body=f"Channel: Email · {reason}; message(s) not accepted by the SMTP server; see the spooler log",
            severity="error",
        )

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
        self,
        queue_id: int,
        error: str,
        delay_seconds: int | None = None,
        *,
        uncertain: bool = False,
        transient: bool = False,
    ) -> bool:
        """
        Mark a notification for retry after a failure (atomic implementation).

        A transient failure (outage, provider rate limit) only ends with the message validity; any
        other failure also spends the small MAX_RETRIES budget (``limited_attempts``).

        Args:
            queue_id: ID of the queued notification
            error: Error message from the failed attempt (truncated if >500 chars)
            delay_seconds: Optional custom delay override (e.g., the provider's retry-after)
            uncertain: The send outcome is unconfirmed; the error is tagged with UNCERTAIN_MARKER
            transient: The failure says nothing about the message; it consumes no retry budget

        Returns:
            True if scheduled for retry; False if the budget or the validity is exhausted, the job is
            unknown, or it already reached a terminal state (SENT/FAILED), which a late retry must
            never reset.
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
                """
                UPDATE notification_queue
                SET retry_count = retry_count + 1, limited_attempts = limited_attempts + ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (0 if transient else 1, queue_id, QueueStatus.PENDING.value, QueueStatus.PROCESSING.value),
            )
            if not cur.rowcount:
                return False
            row = conn.execute(
                "SELECT retry_count, limited_attempts, created_at FROM notification_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if not row:
                return False

            retry_count, limited_attempts, created_at = row
            expired = (now - _parse_ts(created_at)).total_seconds() >= MESSAGE_VALIDITY_SECONDS
            if expired or limited_attempts > MAX_RETRIES:
                final_error = _expired_error(error) if expired else error
                cur = conn.execute(
                    """
                    UPDATE notification_queue
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        QueueStatus.FAILED.value,
                        final_error[:500],
                        now.isoformat(),
                        queue_id,
                        QueueStatus.PENDING.value,
                        QueueStatus.PROCESSING.value,
                    ),
                )
                if cur.rowcount:
                    self._record_delivery(conn, queue_id, sent=False)
                _log.warning(
                    "Notification %d permanently failed (%s) after %d attempts: %s",
                    queue_id,
                    "expired" if expired else "retry budget exhausted",
                    retry_count,
                    error,
                )
                return False

            next_retry = (
                (now + timedelta(seconds=delay_seconds))
                if delay_seconds
                else _calculate_next_retry((retry_count if transient else limited_attempts) - 1)
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
                "Notification %d scheduled for retry %d at %s",
                queue_id,
                retry_count,
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
    ) -> DeliveryResult:
        """Perform the blocking network send for one claimed item. Never touches the queue DB."""
        try:
            if item.channel_type == ChannelType.SIGNAL:
                if not send_signal:
                    return DeliveryResult("retry", "Signal sender not configured")
                ok = send_signal(
                    item.recipient_address, item.payload.get("message", ""), item.payload.get("sender", "")
                )
                return DeliveryResult("sent") if ok else DeliveryResult("retry", "send_signal returned False")
            if item.channel_type == ChannelType.EMAIL:
                if not send_email:
                    return DeliveryResult("retry", "Email sender not configured")
                if len(getaddresses([item.recipient_address])) != 1 or not item.recipient_address.strip():
                    # One envelope recipient per entry keeps a partial acceptance (and a duplicating retry) impossible.
                    return DeliveryResult("permanent", "queue entry must have exactly one recipient address")
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
        except maintenance.MaintenanceActiveError:
            # Raised by the sender before any I/O (it could not read its settings): nothing was sent.
            return DeliveryResult("deferred", "maintenance")
        except Exception as e:  # noqa: BLE001 - queue-worker boundary: a bad item is logged and classified, never crashes the worker
            failure = classify_failure(e)
            _log.warning(
                "Notification %d failed (%s, %s%s): %s",
                item.id,
                failure.kind.value,
                type(e).__name__,
                ", outcome unconfirmed" if failure.uncertain else "",
                failure.reason,
            )
            return _result_for(failure)

    def _apply_result(self, item: QueuedNotification, result: DeliveryResult) -> None:
        """Write one delivery outcome (short DB phase)."""
        if result.cooldown_seconds:
            self.set_cooldown(_item_key(item), result.cooldown_seconds, result.error)
        if result.kind == "sent":
            self.mark_sent(item.id)
        elif result.kind == "permanent":
            self.mark_failed_permanent(item.id, result.error)
        elif result.kind == "deferred":
            self.release(item.id)
        elif item.notification_type == NotificationType.TEST:
            # A test message is never retried; the first failed attempt closes it (keeps the uncertain tag).
            error = f"{UNCERTAIN_MARKER} {result.error}" if result.uncertain else result.error
            self.mark_failed_permanent(item.id, error)
        else:
            if item.channel_type == ChannelType.EMAIL:
                self._remember_failure_kind(result.kind)
            self.mark_retry(
                item.id,
                result.error,
                result.delay_seconds,
                uncertain=result.uncertain,
                transient=result.kind in {"transient", "rate_limited"},
            )

    def _remember_failure_kind(self, kind: str) -> None:
        """Record the failure series (start, latest class) the outage warning is based on."""
        stamp = _utcnow().isoformat()
        with self._db_conn(immediate=True) as conn:
            series = sqlite_db.get_setting(conn, _LAST_FAILURE_SETTING)
            since = series["since"] if isinstance(series, dict) and "since" in series else stamp
            sqlite_db.set_setting(conn, _LAST_FAILURE_SETTING, {"since": since, "at": stamp, "kind": kind})

    def check_handoff_outage(self) -> bool:
        """Raise (or refresh) the admin warning when email has failed at the SMTP hand-off for 15 minutes.

        Only messages that already failed repeatedly count, so a normally waiting message, the pacing
        interval or a cooldown without errors never alarms. Read-only unless the state has to change.
        """
        now = _utcnow()
        with self._db_conn() as conn:
            # The series starts with the first failure after the last accepted mail, so a success ends it
            # and old messages still in backoff cannot reopen a finished outage.
            series = sqlite_db.get_setting(conn, _LAST_FAILURE_SETTING)
            if not isinstance(series, dict) or "since" not in series:
                return False
            if (now - _parse_ts(series["since"])).total_seconds() < HANDOFF_ALERT_AFTER_SECONDS:
                return False
            waiting, oldest, attempts, next_retry = conn.execute(
                """
                SELECT COUNT(*), MIN(created_at), MAX(retry_count), MIN(next_retry_at) FROM notification_queue
                WHERE channel_type = ? AND status = ? AND notification_type != ? AND retry_count >= ?
                """,
                (
                    ChannelType.EMAIL.value,
                    QueueStatus.PENDING.value,
                    NotificationType.TEST.value,
                    HANDOFF_ALERT_MIN_ATTEMPTS,
                ),
            ).fetchone()
            if not waiting:
                return False
            state = inbox.get_incident(conn, handoff_incident("email"))
            if state and (now - _parse_ts(state["last_update"])).total_seconds() < inbox.INCIDENT_UPDATE_MIN_SECONDS:
                return False
            category = HANDOFF_CATEGORIES.get(series.get("kind"), "SMTP hand-off error")
        body = (
            f"Channel: Email · {category} · {waiting} message(s) waiting since {_parse_ts(oldest):%H:%M} UTC · "
            f"{attempts} attempts, next retry {_parse_ts(next_retry):%H:%M} UTC"
        )
        with self._db_conn(immediate=True) as conn:
            # Decide again under the write lock: an accepted mail may have ended the series since the read above.
            if not isinstance(sqlite_db.get_setting(conn, _LAST_FAILURE_SETTING), dict):
                return False
            inbox.report_incident(conn, handoff_incident("email"), title="Email handoff failing", body=body, now=now)
        _log.warning("Email SMTP hand-off failing for over %d min: %s", HANDOFF_ALERT_AFTER_SECONDS // 60, category)
        return True

    # ─── Channel state: pacing and provider cooldown (persistent) ────────────────

    def _blocked_keys(self, conn: sqlite3.Connection, now: datetime) -> set[str]:
        """Send paths that must not be claimed now: in provider cooldown or still pacing for too long."""
        blocked: set[str] = set()
        for key, last_attempt, cooldown_until in conn.execute(
            "SELECT channel_key, last_attempt_at, cooldown_until FROM notification_channel_state"
        ):
            if cooldown_until and _parse_ts(cooldown_until) > now:
                blocked.add(key)
                continue
            policy = CHANNEL_POLICIES.get(channel_of_key(key))
            if policy and last_attempt:
                ready_at = _parse_ts(last_attempt) + timedelta(seconds=policy.min_interval_seconds)
                if (ready_at - now).total_seconds() > MAX_PACING_WAIT_SECONDS:
                    blocked.add(key)
        return blocked

    def pacing_wait_seconds(self, key: str) -> float | None:
        """Seconds to wait before the next send attempt on ``key``; None while it is in cooldown."""
        now = _utcnow()
        with self._db_conn() as conn:
            row = conn.execute(
                "SELECT last_attempt_at, cooldown_until FROM notification_channel_state WHERE channel_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return 0.0
        if row[1] and _parse_ts(row[1]) > now:
            return None
        policy = CHANNEL_POLICIES.get(channel_of_key(key))
        if policy is None or not row[0]:
            return 0.0
        ready_at = _parse_ts(row[0]) + timedelta(seconds=policy.min_interval_seconds)
        return max(0.0, (ready_at - now).total_seconds())

    def record_attempt(self, key: str) -> None:
        """Persist the start of a send attempt (before the network call, so a crash keeps the spacing)."""
        stamp = _utcnow().isoformat()
        with self._db_conn(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO notification_channel_state (channel_key, last_attempt_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET last_attempt_at = excluded.last_attempt_at,
                                                       updated_at = excluded.updated_at
                """,
                (key, stamp, stamp),
            )

    def set_cooldown(self, key: str, seconds: int, reason: str) -> None:
        """Pause a send path for ``seconds`` (capped); a longer cooldown already stored is kept."""
        now = _utcnow()
        until = (now + timedelta(seconds=min(seconds, MAX_COOLDOWN_SECONDS))).isoformat()
        with self._db_conn(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO notification_channel_state (channel_key, cooldown_until, cooldown_reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET
                    cooldown_reason = CASE WHEN cooldown_until IS NULL OR cooldown_until < excluded.cooldown_until
                                           THEN excluded.cooldown_reason ELSE cooldown_reason END,
                    cooldown_until = CASE WHEN cooldown_until IS NULL OR cooldown_until < excluded.cooldown_until
                                          THEN excluded.cooldown_until ELSE cooldown_until END,
                    updated_at = excluded.updated_at
                """,
                (key, until, reason[:200], now.isoformat()),
            )
        _log.warning("Send path %s paused until %s (provider limit)", channel_of_key(key), until)

    def _expire_stale(self, conn: sqlite3.Connection, now: datetime) -> int:
        """Finalize unsent messages older than MESSAGE_VALIDITY_SECONDS as failed/expired (visible in the log)."""
        cutoff = (now - timedelta(seconds=MESSAGE_VALIDITY_SECONDS)).isoformat()
        rows = conn.execute(
            "SELECT id, last_error FROM notification_queue WHERE status = ? AND created_at < ?",
            (QueueStatus.PENDING.value, cutoff),
        ).fetchall()
        for queue_id, last_error in rows:
            error = _expired_error(last_error)
            cur = conn.execute(
                "UPDATE notification_queue SET status = ?, last_error = ?, updated_at = ? WHERE id = ? AND status = ?",
                (QueueStatus.FAILED.value, error[:500], now.isoformat(), queue_id, QueueStatus.PENDING.value),
            )
            if cur.rowcount:
                self._record_delivery(conn, queue_id, sent=False)
                _log.warning(
                    "Notification %d expired: not handed over within %d h", queue_id, MESSAGE_VALIDITY_SECONDS // 3600
                )
        if rows:
            _log.warning("Expired %d undelivered notification(s) older than the message validity", len(rows))
        return len(rows)

    def _reserve_send(self, item: QueuedNotification, key: str, section: Callable[[], Any]) -> DeliveryResult | None:
        """Pace the send and take the claim; None means go ahead, otherwise the outcome without a send.

        The spacing since the last attempt on this path comes from the database, so it also holds
        after a scheduler or container restart. Waiting for it is not a failure.
        """
        try:
            with section():
                wait = self.pacing_wait_seconds(key)
        except (maintenance.StaleWorkError, maintenance.MaintenanceActiveError):
            return DeliveryResult("deferred", "maintenance")
        if wait is None:
            return DeliveryResult("deferred", "cooldown")
        if wait > 0:
            time.sleep(wait)
        try:
            with section():
                lease = self.renew_lease(item.id, item.updated_at.isoformat())
                if lease is not None:
                    self.record_attempt(key)
        except (maintenance.StaleWorkError, maintenance.MaintenanceActiveError):
            return DeliveryResult("deferred", "maintenance")  # window/restore in progress: defer instead of sending
        return DeliveryResult("lost") if lease is None else None

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
           - Other exceptions are classified by delivery_policy.classify_failure
             (transient / rate_limited / limited / permanent)
        3. Returning False spends the limited retry budget like a non-transient failure

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
            try:
                self.check_handoff_outage()
            except sqlite3.Error as exc:
                _log.warning("Email hand-off outage check failed: %s", exc)
            items = self.get_pending_items()

        if not items:
            return (0, 0)

        _log.info("Processing %d queued notifications (atomically claimed)", len(items))

        processed = 0
        succeeded = 0
        cooled: set[str] = set()  # send paths that hit a provider limit during this run

        for item in items:
            processed += 1
            key = _item_key(item)

            # Phase 2: network I/O outside any maintenance section.
            # TOCTOU: a restore may begin right after this check. Accepted by design (at-least-once):
            # the finalize is then rejected as stale and the restored row is retried.
            if key in cooled:
                result = DeliveryResult("deferred", "cooldown")
            elif may_send is not None and not may_send():
                result = DeliveryResult("deferred", "maintenance")
            else:
                result = self._reserve_send(item, key, section)
                if result is None:
                    result = self._send_item(item, send_signal, send_email)
                elif result.kind == "lost":
                    _log.warning("Notification %d: claim lost before send, skipping", item.id)
                    processed -= 1
                    continue
            if result.cooldown_seconds:
                cooled.add(key)

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
_spooler_last_failure: float | None = None
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
        now = time.monotonic()
        if _spooler_last_failure is not None and now - _spooler_last_failure < _SPOOLER_RETRY_INTERVAL:
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
