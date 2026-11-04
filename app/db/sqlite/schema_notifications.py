#!/usr/bin/env python3
#
# app/db/sqlite/schema_notifications.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""DDL and ensure-helpers for the notification queue, alert dispatch outbox and inbox tables."""

from __future__ import annotations

import sqlite3

_NOTIFICATION_QUEUE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_type TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        recipient_id INTEGER,
        recipient_name TEXT NOT NULL,
        recipient_address TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        next_retry_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        target_id INTEGER,
        limited_attempts INTEGER NOT NULL DEFAULT 0 CHECK (limited_attempts >= 0)
    )
    """,
    # Per-channel (and Signal per-sender) send pacing and provider cooldown; survives restarts.
    """
    CREATE TABLE IF NOT EXISTS notification_channel_state (
        channel_key TEXT PRIMARY KEY,
        last_attempt_at TEXT,
        cooldown_until TEXT,
        cooldown_reason TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    # Due-item selection (pending, next_retry_at) and crash recovery / retention (status, updated_at).
    "CREATE INDEX IF NOT EXISTS idx_notification_queue_status_retry ON notification_queue (status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_notification_queue_status_updated ON notification_queue (status, updated_at)",
)


def ensure_notification_queue_table(conn: sqlite3.Connection) -> None:
    """Create the notification queue table and its indexes (single definition, idempotent).

    Used by init_schema() and by the notification spooler, so both always agree on the shape.
    """
    for statement in _NOTIFICATION_QUEUE_DDL:
        conn.execute(statement)


_ALERT_DISPATCH_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS alert_dispatch_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('open', 'resolved')),
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL,
    failed_service TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (alert_id, event_type)
)
"""


def ensure_alert_dispatch_outbox_table(conn: sqlite3.Connection) -> None:
    conn.execute(_ALERT_DISPATCH_OUTBOX_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_dispatch_outbox_pending ON alert_dispatch_outbox(status, created_at, id)"
    )


_NOTIFICATIONS_DDL = (
    # Central inbox. Separate from `alerts` (monitoring state) and `notification_queue` (SMTP/Signal
    # delivery); one row per business event, deduplicated by the unique idempotency_key.
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        category TEXT NOT NULL
            CHECK (category IN ('monitoring', 'reports', 'delivery', 'security', 'activity')),
        severity TEXT NOT NULL CHECK (severity IN ('info', 'success', 'warning', 'error')),
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        created_at timestamp NOT NULL,
        resource_type TEXT
            CHECK (resource_type IS NULL OR resource_type IN ('target', 'alert', 'report_job', 'queue')),
        resource_id TEXT,
        target_id INTEGER,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor_name TEXT,
        audience TEXT NOT NULL CHECK (audience IN ('admins', 'user', 'target')),
        recipient_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        event_count INTEGER NOT NULL DEFAULT 1 CHECK (event_count >= 1),
        idempotency_key TEXT NOT NULL UNIQUE,
        CHECK (audience <> 'user' OR recipient_user_id IS NOT NULL),
        CHECK (audience <> 'target' OR target_id IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications (recipient_user_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_category ON notifications (category, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_audience ON notifications (audience, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at)",
    # Per-user read state, normalized: a missing row means unread.
    """
    CREATE TABLE IF NOT EXISTS notification_reads (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        read_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_reads_user ON notification_reads (user_id, notification_id)",
    # Per-user dismissal ("delete" in the UI): hides one notification for one user without touching others.
    """
    CREATE TABLE IF NOT EXISTS notification_dismissals (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        dismissed_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_dismissals_user ON notification_dismissals (user_id, notification_id)",
)


def ensure_notifications_tables(conn: sqlite3.Connection) -> None:
    """Create the notification inbox tables and indexes (idempotent)."""
    for statement in _NOTIFICATIONS_DDL:
        conn.execute(statement)
