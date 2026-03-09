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

import json
import logging
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

from .signal_errors import (
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalNotRegistered,
)

_log = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────────

# Maximum retry attempts before giving up
# With MAX_RETRIES=5: initial send fails, then up to 5 retries → FAILED
# Total attempts = 1 (initial) + MAX_RETRIES = 6
# Note: Crash recovery can increment retry_count before the next send attempt,
# but the >= check ensures we never exceed MAX_RETRIES actual retry attempts.
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

# Cleanup: delete entries older than this (days)
CLEANUP_AFTER_DAYS = 7

# Process batch size (per run)
PROCESS_BATCH_SIZE = 20

# Channel-specific batch limits per scheduler run
# Signal is rate-limited, but 1 per run causes batched notifications when
# multiple targets fail simultaneously. With SIGNAL_THROTTLE_SECONDS=2.0
# between messages, 5 per run = max ~8s processing time, acceptable.
SIGNAL_BATCH_LIMIT = 5
EMAIL_BATCH_LIMIT = 10

# Throttle delay between Signal messages (seconds)
# Signal servers are very sensitive to bursts
SIGNAL_THROTTLE_SECONDS = 2.0


class NotificationType(str, Enum):
    """Type of notification in the queue."""
    WELCOME = "welcome"
    ALERT = "alert"
    TEST = "test"


class ChannelType(str, Enum):
    """Notification channel type."""
    SIGNAL = "signal"
    EMAIL = "email"


class QueueStatus(str, Enum):
    """Status of a queued notification."""
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"  # Permanently failed (max retries exceeded)


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


def _utcnow() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


def _calculate_next_retry(retry_count: int) -> datetime:
    """
    Calculate next retry time using exponential backoff with jitter.
    
    Formula: delay = min(BASE * 2^retry + jitter, MAX_DELAY)
    """
    # Exponential backoff: 1min, 2min, 4min, 8min, 16min, ...
    delay = BASE_DELAY_SECONDS * (2 ** retry_count)
    delay = min(delay, MAX_DELAY_SECONDS)
    
    # Add jitter (±25% by default)
    jitter = delay * JITTER_FACTOR * (random.random() * 2 - 1)
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
        # Prevent SQLITE_BUSY errors on slow filesystems or concurrent access
        conn.execute("PRAGMA busy_timeout = 5000")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            # Auto-commit if transaction was started
            if immediate:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    
    def _ensure_table(self) -> None:
        """Create the notification_queue table if it doesn't exist.
        
        Schema migration is handled by app.utils.migration (Migration 012).
        This method only creates the table if completely missing.
        
        Note: PRAGMA journal_mode = WAL is a database-level setting, not connection-level.
        If the main application uses a different journal mode, there may be conflicts.
        WAL mode persists across connections, so setting it once is sufficient.
        """
        with self._db_conn(immediate=True) as conn:
            # WAL mode for better concurrent read/write performance
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_type TEXT NOT NULL,
                    channel_type TEXT NOT NULL,
                    recipient_id INTEGER,
                    recipient_name TEXT NOT NULL,
                    recipient_address TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # Index for efficient queue processing
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_queue_status_retry
                ON notification_queue (status, next_retry_at)
            """)
            # Index for crash recovery query (status + updated_at)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_queue_status_updated
                ON notification_queue (status, updated_at)
            """)
    
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
                f"Unknown channel type: {channel_type!r}. "
                f"Valid: {[c.value for c in ChannelType]}"
            )
        
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
        Add a failed notification to the queue for retry.
        
        Note on idempotency:
            This method does NOT check for duplicates. The same notification
            can be enqueued multiple times if the caller doesn't guard against it.
            For alert notifications, consider checking if a pending entry for the
            same (alert_id, recipient_id, channel_type) already exists before
            enqueueing. For welcome notifications, duplicates are generally harmless.
        
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
            payload_json = json.dumps(payload)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Payload is not JSON-serializable: {e}") from e
        
        with self._db_conn(immediate=True) as conn:
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
    
    def get_pending_count(self) -> int:
        """Get the number of pending notifications in the queue."""
        with self._db_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE status = ?",
                (QueueStatus.PENDING.value,),
            ).fetchone()
            return row[0] if row else 0
    
    def get_queue_stats(self) -> dict[str, int]:
        """Get queue statistics by status."""
        with self._db_conn() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM notification_queue
                GROUP BY status
                """
            ).fetchall()
            return {row[0]: row[1] for row in rows}
    
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
            status_rows = conn.execute(
                "SELECT status, COUNT(*) FROM notification_queue GROUP BY status"
            ).fetchall()
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
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM notification_queue nq {where}", params
            ).fetchone()
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
                """,
                params + [limit, offset],
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
        - Signal: max SIGNAL_BATCH_LIMIT (5) per run (with throttling between sends)
        - Email: max EMAIL_BATCH_LIMIT (10) per run
        
        IMPORTANT: This method uses atomic claim via UPDATE...RETURNING to prevent
        race conditions when multiple workers are running. Each item is atomically
        marked as PROCESSING in the same query that selects it.
        
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
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        
        with self._db_conn(immediate=True) as conn:
            try:
                # Get candidate IDs first (pending ready for retry, or stuck processing)
                # Include status to differentiate normal pending from stuck processing
                candidate_rows = conn.execute(
                    """
                    SELECT id, channel_type, status
                    FROM notification_queue
                    WHERE (
                        (status = ? AND next_retry_at <= ?)
                        OR (status = ? AND updated_at < ?)
                    )
                    ORDER BY next_retry_at ASC
                    LIMIT ?
                    """,
                    (
                        QueueStatus.PENDING.value, now.isoformat(),
                        QueueStatus.PROCESSING.value, processing_cutoff,
                        limit,
                    ),
                ).fetchall()
                
                # Apply channel-specific batch limits
                signal_count = 0
                email_count = 0
                ids_to_claim: list[int] = []
                stuck_processing_ids: set[int] = set()  # Track stuck items for retry_count increment
                
                for row in candidate_rows:
                    item_id, channel, status = row[0], row[1], row[2]
                    if channel == ChannelType.SIGNAL.value:
                        if signal_count < SIGNAL_BATCH_LIMIT:
                            ids_to_claim.append(item_id)
                            signal_count += 1
                            if status == QueueStatus.PROCESSING.value:
                                stuck_processing_ids.add(item_id)
                    elif channel == ChannelType.EMAIL.value:
                        if email_count < EMAIL_BATCH_LIMIT:
                            ids_to_claim.append(item_id)
                            email_count += 1
                            if status == QueueStatus.PROCESSING.value:
                                stuck_processing_ids.add(item_id)
                    else:
                        # Unknown channel - include anyway
                        ids_to_claim.append(item_id)
                        if status == QueueStatus.PROCESSING.value:
                            stuck_processing_ids.add(item_id)
                
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
                        WHERE id IN ({stuck_placeholders})
                        """,
                        list(stuck_processing_ids),
                    )
                    
                    # Mark items that exceeded max retries as FAILED
                    conn.execute(
                        f"""
                        UPDATE notification_queue 
                        SET status = ?, 
                            last_error = COALESCE(last_error, '') || ' [MAX RETRIES EXCEEDED after recovery]'
                        WHERE id IN ({stuck_placeholders}) AND retry_count > ?
                        """,
                        [QueueStatus.FAILED.value] + list(stuck_processing_ids) + [MAX_RETRIES],
                    )
                    
                    # Get IDs that were marked as failed to exclude from claim
                    failed_rows = conn.execute(
                        f"""
                        SELECT id FROM notification_queue
                        WHERE id IN ({stuck_placeholders}) AND status = ?
                        """,
                        list(stuck_processing_ids) + [QueueStatus.FAILED.value],
                    ).fetchall()
                    failed_ids = {row[0] for row in failed_rows}
                    
                    # Remove failed items from claim list
                    ids_to_claim = [id for id in ids_to_claim if id not in failed_ids]
                    
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
                    """,
                    [QueueStatus.PROCESSING.value, now.isoformat()] + ids_to_claim + [
                        QueueStatus.PENDING.value, now.isoformat(),
                        QueueStatus.PROCESSING.value, processing_cutoff,
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
                    """,
                    ids_to_claim,
                ).fetchall()
                
            except Exception:
                raise
        
        # Parse rows AFTER commit to ensure claims persist even if parsing fails
        # Individual parsing errors release the item back to PENDING for retry
        for row in rows:
            try:
                all_items.append(QueuedNotification(
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
                ))
            except Exception as e:
                # Parsing failed - release this item back to PENDING
                _log.error("Failed to parse queued item %d, releasing: %s", row[0], e)
                try:
                    with self._db_conn(immediate=True) as release_conn:
                        release_conn.execute(
                            "UPDATE notification_queue SET status = ?, last_error = ? WHERE id = ?",
                            (QueueStatus.PENDING.value, f"Parse error: {e}", row[0]),
                        )
                except Exception:
                    # Will be recovered by timeout mechanism
                    pass
        
        return all_items
    
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
            conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (QueueStatus.SENT.value, now.isoformat(), queue_id),
            )
        _log.info("Notification %d sent successfully", queue_id)
    
    def mark_retry(self, queue_id: int, error: str, delay_seconds: int | None = None) -> bool:
        """
        Mark a notification for retry after a failure (atomic implementation).
        
        Uses atomic UPDATE + SELECT to prevent race conditions when multiple
        workers or the recovery process increment retry_count simultaneously.
        
        Args:
            queue_id: ID of the queued notification
            error: Error message from the failed attempt (truncated if >500 chars)
            delay_seconds: Optional custom delay override (e.g., for rate limiting)
            
        Returns:
            True if will retry, False if max retries exceeded
        """
        now = _utcnow()
        
        # Truncate error to prevent unbounded growth
        if len(error) > 500:
            error = error[:497] + "..."
        
        with self._db_conn(immediate=True) as conn:
            # Atomic increment + read in one transaction
            conn.execute(
                "UPDATE notification_queue SET retry_count = retry_count + 1 WHERE id = ?",
                (queue_id,),
            )
            row = conn.execute(
                "SELECT retry_count FROM notification_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            
            if not row:
                return False
            
            retry_count = row[0]
            
            # >= instead of > to match documented behavior:
            # MAX_RETRIES=5 means max 5 retries (6 total attempts including initial)
            if retry_count >= MAX_RETRIES:
                # Max retries exceeded - mark as permanently failed
                conn.execute(
                    """
                    UPDATE notification_queue
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (QueueStatus.FAILED.value, error, now.isoformat(), queue_id),
                )
                _log.warning(
                    "Notification %d permanently failed after %d retries: %s",
                    queue_id, retry_count, error,
                )
                return False
            
            # Schedule next retry (use custom delay if provided, otherwise exponential backoff)
            next_retry = (now + timedelta(seconds=delay_seconds)) if delay_seconds else _calculate_next_retry(retry_count)
            conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    QueueStatus.PENDING.value,
                    next_retry.isoformat(),
                    error,
                    now.isoformat(),
                    queue_id,
                ),
            )
            
            _log.info(
                "Notification %d scheduled for retry %d/%d at %s",
                queue_id, retry_count, MAX_RETRIES,
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
            conn.execute(
                """
                UPDATE notification_queue
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (QueueStatus.FAILED.value, f"[PERMANENT] {error}", now.isoformat(), queue_id),
            )
        
        _log.warning(
            "Notification %d marked as permanently failed (no retry): %s",
            queue_id, error,
        )
    
    def cleanup_old_entries(self, days: int = CLEANUP_AFTER_DAYS) -> int:
        """
        Remove old sent/failed entries from the queue.
        
        Args:
            days: Delete entries older than this many days
            
        Returns:
            Number of entries deleted
        """
        cutoff = _utcnow() - timedelta(days=days)
        
        with self._db_conn(immediate=True) as conn:
            cursor = conn.execute(
                """
                DELETE FROM notification_queue
                WHERE status IN (?, ?) AND updated_at < ?
                """,
                (QueueStatus.SENT.value, QueueStatus.FAILED.value, cutoff.isoformat()),
            )
            deleted = cursor.rowcount
        
        if deleted > 0:
            _log.info("Cleaned up %d old notification queue entries", deleted)
        
        return deleted
    
    def process_queue_sync(
        self,
        send_signal: Callable[[str, str, str], bool] | None = None,
        send_email: Callable[[str, str, str, str], bool] | None = None,
    ) -> tuple[int, int]:
        """
        Process pending notifications in the queue (SYNCHRONOUS version).
        
        This method is designed to be called via asyncio.to_thread() from the
        scheduler to avoid blocking the event loop. All I/O operations (Signal
        subprocess calls, SMTP connections) are blocking.
        
        Signal messages are throttled with SIGNAL_THROTTLE_SECONDS delay to
        prevent rate-limiting from Signal servers.
        
        WARNING: Do NOT call this directly from async code. Use asyncio.to_thread().
        
        SEND FUNCTION CONTRACT:
        -----------------------
        The send_signal and send_email callbacks should follow this contract:
        
        1. Return True on success (message delivered)
        2. Raise specific exceptions for different failure modes:
           - SignalRateLimited: Rate limited, will use retry_after_seconds for backoff
           - SignalRecipientInvalid: Invalid recipient, permanent failure (no retry)
           - SignalNotRegistered: Sender not registered, permanent failure (no retry)
           - SignalError / Exception: Transient error, will retry with exponential backoff
        3. Return False is treated as transient error (will retry)
        
        DO NOT mix return False with exceptions for the same error type.
        Exceptions provide richer semantics (rate limit duration, permanent vs transient).
        
        Args:
            send_signal: Function to send Signal message: (phone, message, sender) -> success
            send_email: Function to send email: (to, subject, text, html) -> success
            
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
        
        # Items are already atomically claimed as PROCESSING by get_pending_items()
        items = self.get_pending_items()
        
        if not items:
            return (0, 0)
        
        _log.info("Processing %d queued notifications (atomically claimed)", len(items))
        
        processed = 0
        succeeded = 0
        last_signal_sent = False  # Track consecutive Signal sends for throttling
        
        for item in items:
            # No need to call mark_processing() - items are already PROCESSING
            processed += 1
            
            try:
                success = False
                error_msg = None
                
                if item.channel_type == ChannelType.SIGNAL:
                    if send_signal:
                        # HARD THROTTLE: Signal servers are very rate-limit sensitive
                        # Only sleep between consecutive Signal sends (not before the first)
                        if last_signal_sent:
                            _log.debug(
                                "Signal throttle: sleeping %.1fs before send",
                                SIGNAL_THROTTLE_SECONDS,
                            )
                            time.sleep(SIGNAL_THROTTLE_SECONDS)
                        last_signal_sent = True
                        
                        phone = item.recipient_address
                        message = item.payload.get("message", "")
                        sender = item.payload.get("sender", "")
                        # Let exceptions propagate to outer handler for proper retry strategy
                        success = send_signal(phone, message, sender)
                        if not success:
                            error_msg = "send_signal returned False"
                    else:
                        error_msg = "Signal sender not configured"
                
                elif item.channel_type == ChannelType.EMAIL:
                    if send_email:
                        to_addr = item.recipient_address
                        subject = item.payload.get("subject", "")
                        text_body = item.payload.get("text_body", "")
                        html_body = item.payload.get("html_body", "")
                        # Let exceptions propagate for proper error handling
                        success = send_email(to_addr, subject, text_body, html_body)
                        if not success:
                            error_msg = "send_email returned False"
                    else:
                        error_msg = "Email sender not configured"
                
                if success:
                    self.mark_sent(item.id)
                    succeeded += 1
                    _log.info(
                        "Successfully sent queued %s to %s via %s",
                        item.notification_type.value,
                        item.recipient_name,
                        item.channel_type.value,
                    )
                else:
                    self.mark_retry(item.id, error_msg or "Unknown error")
            
            except SignalRateLimited as e:
                # Rate limiting: long cooldown (1 hour default)
                _log.warning(
                    "Signal rate limited for notification %d, entering %ds cooldown",
                    item.id,
                    e.retry_after_seconds,
                )
                self.mark_retry(item.id, str(e), delay_seconds=e.retry_after_seconds)
            
            except SignalRecipientInvalid as e:
                # Permanent error: invalid recipient, no point retrying
                _log.warning(
                    "Invalid Signal recipient for notification %d, marking failed: %s",
                    item.id,
                    str(e),
                )
                self.mark_failed_permanent(item.id, str(e))
            
            except SignalNotRegistered as e:
                # Permanent error: sender not registered, requires manual intervention
                _log.error(
                    "Signal sender not registered for notification %d, marking failed: %s",
                    item.id,
                    str(e),
                )
                self.mark_failed_permanent(item.id, str(e))
                    
            except Exception as e:
                _log.exception("Error processing queued notification %d", item.id)
                self.mark_retry(item.id, str(e))
        
        _log.info(
            "Queue processing complete: %d processed, %d succeeded, %d retried/failed",
            processed, succeeded, processed - succeeded,
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
