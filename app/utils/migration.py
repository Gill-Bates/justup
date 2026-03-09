#!/usr/bin/env python3
#
# app/utils/migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Database migration framework for JustUp.

This module provides a versioned migration system that tracks applied migrations
in a dedicated table and applies new ones on startup.

Usage:
    from app.utils.migration import run_migrations
    run_migrations(conn)  # Call after init_schema()
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from .http_status import DEFAULT_HTTP_SUCCESS_CODES

_log = logging.getLogger(__name__)


def _utcnow() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the migrations tracking table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of already-applied migration versions."""
    rows = conn.execute("SELECT version FROM _migrations").fetchall()
    return {row[0] for row in rows}


def _mark_applied(conn: sqlite3.Connection, version: int, name: str) -> None:
    """Record that a migration was successfully applied.
    
    Note: Caller must commit the transaction.
    """
    conn.execute(
        "INSERT INTO _migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, _utcnow()),
    )

# ─────────────────────────────────────────────────────────────────────────────
# Migration Definitions
# ─────────────────────────────────────────────────────────────────────────────
# Each migration is a tuple: (version, name, migration_function)
# Version numbers must be unique and should be sequential.
# Migration functions receive a sqlite3.Connection and should commit changes.


def _migration_001_tcp_ports_multi(conn: sqlite3.Connection) -> None:
    """
    Migration 001: Convert tcp_port (INTEGER) to tcp_ports (TEXT).
    
    This enables multiple TCP ports per target, stored as comma-separated values.
    Example: "80,443,8080"
    """
    # Check if tcp_ports column already exists
    cursor = conn.execute("PRAGMA table_info(targets)")
    columns = {row[1] for row in cursor.fetchall()}
    
    if "tcp_ports" in columns:
        _log.debug("Migration 001: tcp_ports column already exists, skipping")
        return
    
    if "tcp_port" not in columns:
        _log.warning("Migration 001: tcp_port column not found, skipping")
        return
    
    _log.info("Migration 001: Converting tcp_port to tcp_ports...")
    
    # Add new column
    conn.execute("ALTER TABLE targets ADD COLUMN tcp_ports TEXT DEFAULT ''")
    
    # Migrate existing data: convert integer port to string
    conn.execute(
        """
        UPDATE targets 
        SET tcp_ports = CAST(COALESCE(tcp_port, 80) AS TEXT)
        WHERE tcp_port IS NOT NULL AND tcp_port > 0
        """
    )
    
    _log.info("Migration 001: Completed - tcp_ports column created and data migrated")


def _migration_002_tcp_port_status(conn: sqlite3.Connection) -> None:
    """
    Migration 002: Add tcp_port_status column to target_status.
    
    This stores per-port status as JSON for multi-port TCP monitoring.
    Example: {"80": "up", "443": "up", "8080": "down"}
    """
    cursor = conn.execute("PRAGMA table_info(target_status)")
    columns = {row[1] for row in cursor.fetchall()}
    
    if "tcp_port_status" in columns:
        _log.debug("Migration 002: tcp_port_status column already exists, skipping")
        return
    
    _log.info("Migration 002: Adding tcp_port_status column to target_status...")
    conn.execute("ALTER TABLE target_status ADD COLUMN tcp_port_status TEXT")
    _log.info("Migration 002: Completed - tcp_port_status column added")


def _migration_003_drop_user_notification_channels(conn: sqlite3.Connection) -> None:
    """
    Migration 003: Drop deprecated user_notification_channels table.
    
    The old architecture had users receive alerts via notification_channels.
    The new architecture uses Recipients (dedicated phone/email contacts).
    Users are now only for frontend login, not for notifications.
    """
    # Check if table exists
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_notification_channels'"
    )
    if cursor.fetchone() is None:
        _log.debug("Migration 003: user_notification_channels table doesn't exist, skipping")
        return
    
    _log.info("Migration 003: Dropping deprecated user_notification_channels table...")
    
    # Drop indexes first (SQLite will auto-drop them with the table, but explicit is cleaner)
    conn.execute("DROP INDEX IF EXISTS idx_user_channels_user")
    conn.execute("DROP INDEX IF EXISTS idx_user_channels_type")
    conn.execute("DROP INDEX IF EXISTS idx_user_channels_enabled")
    
    # Drop the table
    conn.execute("DROP TABLE IF EXISTS user_notification_channels")
    
    _log.info("Migration 003: Completed - user_notification_channels table dropped")


def _migration_004_users_is_active_last_login(conn: sqlite3.Connection) -> None:
    """
    Migration 004: Add is_active and last_login_at columns to users table.
    
    - is_active: Allows admins to disable user accounts (default: 1 = active)
    - last_login_at: Tracks when user last logged in
    """
    cursor = conn.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in cursor.fetchall()}
    
    if "is_active" not in columns:
        _log.info("Migration 004: Adding is_active column to users...")
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    
    if "last_login_at" not in columns:
        _log.info("Migration 004: Adding last_login_at column to users...")
        conn.execute("ALTER TABLE users ADD COLUMN last_login_at timestamp")
    
    _log.info("Migration 004: Completed - users table extended with is_active and last_login_at")


def _migration_005_http_success_codes(conn: sqlite3.Connection) -> None:
    """
    Migration 005: Add http_success_codes column to targets.

    Allows configuring which HTTP status codes count as UP for the HTTP check.
    """
    cursor = conn.execute("PRAGMA table_info(targets)")
    columns = {row[1] for row in cursor.fetchall()}

    if "http_success_codes" in columns:
        _log.debug("Migration 005: http_success_codes column already exists, skipping")
        return

    _log.info("Migration 005: Adding http_success_codes column to targets...")
    # Validate constant before embedding in DDL (defense in depth)
    import re
    if not re.match(r'^[0-9xX,\-\s]+$', DEFAULT_HTTP_SUCCESS_CODES):
        raise ValueError(f"Unsafe DEFAULT_HTTP_SUCCESS_CODES value: {DEFAULT_HTTP_SUCCESS_CODES!r}")
    # NOTE: SQLite does not reliably support bound parameters in DDL; embed the constant.
    conn.execute(
        f"ALTER TABLE targets ADD COLUMN http_success_codes TEXT DEFAULT '{DEFAULT_HTTP_SUCCESS_CODES}'"
    )
    # Defensive: ensure existing rows have a usable value even if defaults don't backfill.
    conn.execute(
        "UPDATE targets SET http_success_codes = ? WHERE http_success_codes IS NULL OR http_success_codes = ''",
        (DEFAULT_HTTP_SUCCESS_CODES,),
    )
    _log.info("Migration 005: Completed - http_success_codes column added")


def _migration_006_enable_http_check(conn: sqlite3.Connection) -> None:
    """
    Migration 006: Rename enable_http_head -> enable_http_check.

    The old name was misleading (it suggests HEAD method). The new name reflects
    that this flag enables the HTTP availability check.
    """
    cursor = conn.execute("PRAGMA table_info(targets)")
    columns = {row[1] for row in cursor.fetchall()}

    if "enable_http_check" in columns:
        _log.debug("Migration 006: enable_http_check already exists, skipping")
        return

    if "enable_http_head" not in columns:
        _log.warning("Migration 006: enable_http_head column not found, skipping")
        return

    _log.info("Migration 006: Renaming enable_http_head to enable_http_check...")
    conn.execute("ALTER TABLE targets RENAME COLUMN enable_http_head TO enable_http_check")
    _log.info("Migration 006: Completed - column renamed")


def _migration_007_http_port(conn: sqlite3.Connection) -> None:
    """
    Migration 007: Add http_port column to targets.

    Separates request port override from certificate port. Backfills from the
    legacy cert_port when it looks like a non-default override.
    """
    cursor = conn.execute("PRAGMA table_info(targets)")
    columns = {row[1] for row in cursor.fetchall()}

    if "http_port" in columns:
        _log.debug("Migration 007: http_port column already exists, skipping")
        return

    _log.info("Migration 007: Adding http_port column to targets...")
    conn.execute("ALTER TABLE targets ADD COLUMN http_port INTEGER")

    # Backfill from legacy cert_port when it appears to be used as a request-port override.
    if "cert_port" in columns:
        conn.execute(
            """
            UPDATE targets
            SET http_port = cert_port
            WHERE http_port IS NULL
              AND cert_port IS NOT NULL
              AND cert_port NOT IN (80, 443)
            """
        )

    _log.info("Migration 007: Completed - http_port column added")


def _migration_008_http_basic_auth_enabled(conn: sqlite3.Connection) -> None:
    """
    Migration 008: Add http_basic_auth_enabled column to targets.

    This decouples storing credentials from actually sending them. Backfills
    to preserve previous behavior: if credentials exist, auth is enabled.
    """
    cursor = conn.execute("PRAGMA table_info(targets)")
    columns = {row[1] for row in cursor.fetchall()}

    if "http_basic_auth_enabled" in columns:
        _log.debug("Migration 008: http_basic_auth_enabled column already exists, skipping")
        return

    _log.info("Migration 008: Adding http_basic_auth_enabled column to targets...")
    conn.execute("ALTER TABLE targets ADD COLUMN http_basic_auth_enabled INTEGER NOT NULL DEFAULT 0")

    # Preserve legacy behavior: if username+password are configured, auth was effectively enabled.
    if "http_username" in columns and "http_password" in columns:
        conn.execute(
            """
            UPDATE targets
            SET http_basic_auth_enabled = 1
            WHERE http_basic_auth_enabled = 0
              AND http_username IS NOT NULL AND http_username != ''
              AND http_password IS NOT NULL AND http_password != ''
            """
        )

    _log.info("Migration 008: Completed - http_basic_auth_enabled column added")


def _migration_009_users_can_close_alerts(conn: sqlite3.Connection) -> None:
    """
    Migration 009: Add can_close_alerts column to users table.

    This allows non-admin users to close/acknowledge alerts.
    Admins always have this permission implicitly.
    """
    cursor = conn.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in cursor.fetchall()}

    if "can_close_alerts" in columns:
        _log.debug("Migration 009: can_close_alerts column already exists, skipping")
        return

    _log.info("Migration 009: Adding can_close_alerts column to users...")
    conn.execute("ALTER TABLE users ADD COLUMN can_close_alerts INTEGER NOT NULL DEFAULT 0")

    _log.info("Migration 009: Completed - can_close_alerts column added")


def _migration_010_users_last_login_ip(conn: sqlite3.Connection) -> None:
    """
    Migration 010: Add last_login_ip column to users table.

    Tracks the IP address of the user's last successful login.
    Properly handles reverse proxy headers (X-Forwarded-For).
    """
    cursor = conn.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in cursor.fetchall()}

    if "last_login_ip" in columns:
        _log.debug("Migration 010: last_login_ip column already exists, skipping")
        return

    _log.info("Migration 010: Adding last_login_ip column to users...")
    conn.execute("ALTER TABLE users ADD COLUMN last_login_ip TEXT")

    _log.info("Migration 010: Completed - last_login_ip column added")


def _migration_011_notification_queue(conn: sqlite3.Connection) -> None:
    """
    Migration 011: Create notification_queue table for the spooler.

    This table stores failed notifications for retry with exponential backoff.
    Survives restarts and provides crash recovery for stuck PROCESSING entries.
    
    Note: recipient_id is nullable because test notifications have no recipient.
    """
    # Check if table already exists (created by spooler _ensure_table)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_queue'"
    )
    if cursor.fetchone():
        _log.debug("Migration 011: notification_queue table already exists, ensuring index")
    else:
        _log.info("Migration 011: Creating notification_queue table...")
        conn.execute("""
            CREATE TABLE notification_queue (
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

    # Ensure index exists (idempotent)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notification_queue_status_retry
        ON notification_queue (status, next_retry_at)
    """)
    
    # Add index for crash recovery query (status + updated_at)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notification_queue_status_updated
        ON notification_queue (status, updated_at)
    """)

    _log.info("Migration 011: Completed - notification_queue table ready")


def _migration_012_notification_queue_recipient_id_nullable(conn: sqlite3.Connection) -> None:
    """
    Migration 012: Make notification_queue.recipient_id nullable.
    
    Test notifications don't have a recipient_id (self-test messages).
    SQLite doesn't support ALTER COLUMN, so we recreate the table.
    """
    # Check if recipient_id is already nullable
    cursor = conn.execute("PRAGMA table_info(notification_queue)")
    columns = {row[1]: row for row in cursor.fetchall()}
    
    if "recipient_id" not in columns:
        _log.debug("Migration 012: recipient_id column not found, skipping")
        return
    
    # Column info: (cid, name, type, notnull, dflt_value, pk)
    col_info = columns["recipient_id"]
    is_not_null = col_info[3] == 1  # notnull flag
    
    if not is_not_null:
        _log.debug("Migration 012: recipient_id is already nullable, skipping")
        return
    
    _log.info("Migration 012: Making notification_queue.recipient_id nullable for test messages...")
    
    # SQLite limitation: recreate table with new schema
    conn.execute("""BEGIN IMMEDIATE""")
    try:
        # Create temp table with nullable recipient_id
        conn.execute("""
            CREATE TABLE notification_queue_new (
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
        
        # Copy existing data
        conn.execute("""
            INSERT INTO notification_queue_new 
            SELECT * FROM notification_queue
        """)
        
        # Drop old table
        conn.execute("DROP TABLE notification_queue")
        
        # Rename new table
        conn.execute("ALTER TABLE notification_queue_new RENAME TO notification_queue")
        
        # Recreate indices
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_queue_status_retry
            ON notification_queue (status, next_retry_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_queue_status_updated
            ON notification_queue (status, updated_at)
        """)
        
        # Migration function must NOT commit (run_migrations handles it)
        _log.info("Migration 012: Completed - recipient_id is now nullable")
    except Exception as e:
        # run_migrations will handle rollback
        _log.error("Migration 012 failed: %s", e)
        raise


def _migration_013_telemetry_enabled(conn: sqlite3.Connection) -> None:
    """
    Migration 013: Add telemetry_enabled setting with default ON.
    
    Telemetry is enabled by default for new installations.
    Existing installations will get the setting added with default true.
    """
    from .constants import SettingKeys
    
    # Check if setting already exists
    cursor = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (SettingKeys.TELEMETRY_ENABLED,)
    )
    row = cursor.fetchone()
    
    if row is not None:
        _log.debug("Migration 013: telemetry_enabled setting already exists, skipping")
        return
    
    _log.info("Migration 013: Adding telemetry_enabled setting (default: true)...")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (SettingKeys.TELEMETRY_ENABLED, "true", _utcnow())
    )
    _log.info("Migration 013: Completed - telemetry_enabled setting added")


def _migration_014_auto_close_alert(conn: sqlite3.Connection) -> None:
    """
    Migration 014: Add auto_close_alert column to targets table.
    
    When enabled (default), alerts are automatically resolved when the target
    recovers (all services back up). When disabled, alerts stay open until
    manually closed by a user.
    """
    columns = [r[1] for r in conn.execute("PRAGMA table_info(targets)").fetchall()]
    
    if "auto_close_alert" in columns:
        _log.debug("Migration 014: auto_close_alert column already exists, skipping")
        return
    
    _log.info("Migration 014: Adding auto_close_alert column to targets...")
    conn.execute("ALTER TABLE targets ADD COLUMN auto_close_alert INTEGER NOT NULL DEFAULT 1")
    _log.info("Migration 014: Completed - auto_close_alert column added")


def _migration_015_fix_ghost_alerts(conn: sqlite3.Connection) -> None:
    """
    Migration 015: Resolve orphaned open+acknowledged alerts.
    
    Before this fix, acknowledge_alert() only set is_acknowledged=1 but did NOT
    change status to 'resolved'.  This left ghost alerts with status='open' that
    could never be closed through the UI.
    
    This migration retroactively resolves any such orphaned rows.
    """
    cur = conn.execute(
        """
        UPDATE alerts
        SET status   = 'resolved',
            ended_at = COALESCE(ended_at, acknowledged_at, updated_at),
            duration_seconds = COALESCE(duration_seconds,
                CAST((julianday(COALESCE(acknowledged_at, updated_at))
                      - julianday(started_at)) * 86400 AS INTEGER))
        WHERE status = 'open' AND is_acknowledged = 1
        """
    )
    if cur.rowcount > 0:
        _log.info("Migration 015: Resolved %d orphaned open+acknowledged alert(s)", cur.rowcount)
    else:
        _log.debug("Migration 015: No orphaned alerts found")


def _migration_016_pop_locations_retired(conn: sqlite3.Connection) -> None:
    """
    Migration 016: Add retired column to pop_locations.

    Soft-delete flag for POP entries that are no longer seen in upstream sources.
    """
    cursor = conn.execute("PRAGMA table_info(pop_locations)")
    columns = {row[1] for row in cursor.fetchall()}

    if "retired" in columns:
        _log.debug("Migration 016: retired column already exists, skipping")
        return

    _log.info("Migration 016: Adding retired column to pop_locations...")
    conn.execute(
        "ALTER TABLE pop_locations ADD COLUMN retired INTEGER NOT NULL DEFAULT 0"
    )
    _log.info("Migration 016: Completed - retired column added")


def _migration_017_pop_locations_tracking(conn: sqlite3.Connection) -> None:
    """
    Migration 017: Add last_seen_at and import_run_id to pop_locations.

    Tracks when a POP entry was last seen in an import and which run imported it.
    """
    cursor = conn.execute("PRAGMA table_info(pop_locations)")
    columns = {row[1] for row in cursor.fetchall()}

    if "last_seen_at" not in columns:
        _log.info("Migration 017: Adding last_seen_at column to pop_locations...")
        conn.execute("ALTER TABLE pop_locations ADD COLUMN last_seen_at TEXT")

    if "import_run_id" not in columns:
        _log.info("Migration 017: Adding import_run_id column to pop_locations...")
        conn.execute("ALTER TABLE pop_locations ADD COLUMN import_run_id TEXT")

    _log.info("Migration 017: Completed - pop_locations tracking columns added")


def _migration_018_recipients_email(conn: sqlite3.Connection) -> None:
    """
    Migration 018: Add email column to recipients table.

    Enables email-based alert recipients alongside Signal phone contacts.
    """
    cursor = conn.execute("PRAGMA table_info(recipients)")
    columns = {row[1] for row in cursor.fetchall()}

    if "email" in columns:
        _log.debug("Migration 018: email column already exists, skipping")
        return

    _log.info("Migration 018: Adding email column to recipients...")
    conn.execute("ALTER TABLE recipients ADD COLUMN email TEXT")
    _log.info("Migration 018: Completed - email column added")


def _migration_019_auth_tokens_max_expires(conn: sqlite3.Connection) -> None:
    """
    Migration 019: Add max_expires_at column to auth_tokens.

    Absolute expiry cap to prevent indefinite token renewal.
    """
    cursor = conn.execute("PRAGMA table_info(auth_tokens)")
    columns = {row[1] for row in cursor.fetchall()}

    if "max_expires_at" in columns:
        _log.debug("Migration 019: max_expires_at column already exists, skipping")
        return

    _log.info("Migration 019: Adding max_expires_at column to auth_tokens...")
    conn.execute("ALTER TABLE auth_tokens ADD COLUMN max_expires_at timestamp")
    # Backfill: set max_expires_at = expires_at + 18 hours for existing tokens
    conn.execute("""
        UPDATE auth_tokens
        SET max_expires_at = datetime(expires_at, '+18 hours')
        WHERE max_expires_at IS NULL
    """)
    _log.info("Migration 019: Completed - max_expires_at column added")


def _migration_020_uptime_percent_30d(conn: sqlite3.Connection) -> None:
    """
    Migration 020: Add uptime_percent_30d column to target_status.

    Stores 30-day uptime percentage alongside the existing 24h metric.
    """
    cursor = conn.execute("PRAGMA table_info(target_status)")
    columns = {row[1] for row in cursor.fetchall()}

    if "uptime_percent_30d" in columns:
        _log.debug("Migration 020: uptime_percent_30d column already exists, skipping")
        return

    _log.info("Migration 020: Adding uptime_percent_30d column to target_status...")
    conn.execute("ALTER TABLE target_status ADD COLUMN uptime_percent_30d REAL")
    _log.info("Migration 020: Completed - uptime_percent_30d column added")


def _migration_021_notification_queue_target(conn: sqlite3.Connection) -> None:
    """
    Migration 021: Add target_id column to notification_queue.

    Allows displaying target name in the notification log UI.
    Can be NULL for test notifications that aren't associated with a target.
    """
    cursor = conn.execute("PRAGMA table_info(notification_queue)")
    columns = {row[1] for row in cursor.fetchall()}

    if "target_id" in columns:
        _log.debug("Migration 021: target_id column already exists, skipping")
        return

    _log.info("Migration 021: Adding target_id column to notification_queue...")
    conn.execute("ALTER TABLE notification_queue ADD COLUMN target_id INTEGER")
    
    # Backfill target_id from payload JSON for existing entries
    _log.info("Migration 021: Backfilling target_id from payload...")
    rows = conn.execute(
        "SELECT id, payload FROM notification_queue WHERE target_id IS NULL"
    ).fetchall()
    
    import json
    for row in rows:
        try:
            payload = json.loads(row[1])
            target_id = payload.get("target_id")
            if target_id is not None:
                conn.execute(
                    "UPDATE notification_queue SET target_id = ? WHERE id = ?",
                    (target_id, row[0])
                )
        except (json.JSONDecodeError, TypeError):
            pass
    
    _log.info("Migration 021: Completed - target_id column added")


# ─────────────────────────────────────────────────────────────────────────────
# Migration Registry
# ─────────────────────────────────────────────────────────────────────────────

MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "tcp_ports_multi", _migration_001_tcp_ports_multi),
    (2, "tcp_port_status", _migration_002_tcp_port_status),
    (3, "drop_user_notification_channels", _migration_003_drop_user_notification_channels),
    (4, "users_is_active_last_login", _migration_004_users_is_active_last_login),
    (5, "http_success_codes", _migration_005_http_success_codes),
    (6, "enable_http_check", _migration_006_enable_http_check),
    (7, "http_port", _migration_007_http_port),
    (8, "http_basic_auth_enabled", _migration_008_http_basic_auth_enabled),
    (9, "users_can_close_alerts", _migration_009_users_can_close_alerts),
    (10, "users_last_login_ip", _migration_010_users_last_login_ip),
    (11, "notification_queue", _migration_011_notification_queue),
    (12, "notification_queue_recipient_id_nullable", _migration_012_notification_queue_recipient_id_nullable),
    (13, "telemetry_enabled", _migration_013_telemetry_enabled),
    (14, "auto_close_alert", _migration_014_auto_close_alert),
    (15, "fix_ghost_alerts", _migration_015_fix_ghost_alerts),
    (16, "pop_locations_retired", _migration_016_pop_locations_retired),
    (17, "pop_locations_tracking", _migration_017_pop_locations_tracking),
    (18, "recipients_email", _migration_018_recipients_email),
    (19, "auth_tokens_max_expires", _migration_019_auth_tokens_max_expires),
    (20, "uptime_percent_30d", _migration_020_uptime_percent_30d),
    (21, "notification_queue_target", _migration_021_notification_queue_target),
]


def mark_all_migrations_applied(conn: sqlite3.Connection) -> int:
    """
    Mark all known migrations as applied without executing them.
    
    Use this for fresh installations where init_schema() already created
    the complete current schema.
    
    Args:
        conn: SQLite database connection
        
    Returns:
        Number of migrations marked as applied
    """
    _ensure_migrations_table(conn)
    applied = _get_applied_versions(conn)
    
    count = 0
    for version, name, _ in sorted(MIGRATIONS, key=lambda x: x[0]):
        if version in applied:
            continue
        conn.execute(
            "INSERT INTO _migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, _utcnow()),
        )
        count += 1
    
    if count > 0:
        conn.commit()
        _log.debug("Fresh install: marked %d migration(s) as applied", count)
    
    return count


def run_migrations(conn: sqlite3.Connection, fresh_install: bool = False) -> int:
    """
    Run all pending database migrations.
    
    Each migration runs in a single atomic transaction with the tracking record.
    DDL statements (ALTER TABLE) auto-commit in SQLite, but DML (UPDATE) can be
    rolled back on error.
    
    Args:
        conn: SQLite database connection
        fresh_install: If True, skip execution and only mark migrations as applied
        
    Returns:
        Number of migrations applied
    """
    # Fresh install: schema is already complete, just mark migrations as applied
    if fresh_install:
        count = mark_all_migrations_applied(conn)
        if count > 0:
            _log.info("Fresh installation: marked %d migration(s) as applied (no execution needed)", count)
        return count
    
    _ensure_migrations_table(conn)
    applied = _get_applied_versions(conn)
    
    count = 0
    for version, name, migrate_fn in sorted(MIGRATIONS, key=lambda x: x[0]):
        if version in applied:
            continue
        
        _log.info("Applying migration %03d: %s", version, name)
        try:
            # Single transaction: migration + tracking
            migrate_fn(conn)  # Must NOT commit internally
            conn.execute(
                "INSERT INTO _migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utcnow()),
            )
            conn.commit()  # Single atomic commit
            count += 1
            _log.info("Migration %03d completed successfully", version)
        except Exception as e:
            conn.rollback()  # Rollback on error
            _log.error("Migration %03d failed: %s", version, e)
            raise
    
    if count > 0:
        _log.info("Applied %d migration(s)", count)
    else:
        _log.debug("No pending migrations")
    
    return count


def get_migration_status(conn: sqlite3.Connection) -> list[dict]:
    """
    Get the status of all migrations.
    
    Returns:
        List of dicts with version, name, applied (bool), and applied_at
    """
    _ensure_migrations_table(conn)
    applied = {
        row[0]: row[1]
        for row in conn.execute("SELECT version, applied_at FROM _migrations").fetchall()
    }
    
    result = []
    for version, name, _ in sorted(MIGRATIONS, key=lambda x: x[0]):
        result.append({
            "version": version,
            "name": name,
            "applied": version in applied,
            "applied_at": applied.get(version),
        })
    
    return result

