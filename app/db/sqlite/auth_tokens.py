#!/usr/bin/env python3
#
# app/db/sqlite/auth_tokens.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Session token issue, renewal, revocation and expiry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ...utils.time import utcnow
from .connection import _convert_datetime, transaction
from .settings import get_setting


def get_session_max_days(conn: sqlite3.Connection) -> int:
    """Return the configured absolute session lifetime in days, clamped to the valid range."""
    from ...utils.constants import (
        SESSION_MAX_DAYS_DEFAULT,
        SESSION_MAX_DAYS_MAX,
        SESSION_MAX_DAYS_MIN,
        SettingKeys,
    )

    try:
        days = int(get_setting(conn, SettingKeys.SESSION_MAX_DAYS, SESSION_MAX_DAYS_DEFAULT))
    except (TypeError, ValueError):
        days = SESSION_MAX_DAYS_DEFAULT
    return max(SESSION_MAX_DAYS_MIN, min(days, SESSION_MAX_DAYS_MAX))


def cleanup_expired_tokens(conn: sqlite3.Connection) -> None:
    """Delete expired authentication tokens."""
    with transaction(conn):
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (utcnow(),))


def issue_token(
    conn: sqlite3.Connection, *, user_id: int, token_hash: str, expires_at: datetime, max_expires_at: datetime
) -> None:
    """Persist a new authentication token hash for a user."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO auth_tokens(user_id, token_hash, expires_at, max_expires_at, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (user_id, token_hash, expires_at, max_expires_at, utcnow()),
        )


def revoke_token(conn: sqlite3.Connection, *, token_hash: str) -> bool:
    """Revoke a token by its hash.

    Returns True if a token was actually deleted, False if not found.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
        return cursor.rowcount > 0


def revoke_tokens_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    """Revoke all tokens for a given user (e.g., after password change).

    Returns the number of tokens revoked.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        return cursor.rowcount


TOKEN_RENEW_MIN_DELTA = timedelta(minutes=5)


def get_user_for_token(conn: sqlite3.Connection, *, token_hash: str) -> sqlite3.Row | None:
    """Resolve a token hash to a user row, returning None if invalid/expired.

    Implements sliding session renewal: On successful validation, extends expires_at
    by 6 hours, but never beyond max_expires_at (the configured absolute session lifetime).
    The renewal is written only when it extends expiry noticeably, so most
    lookups stay read-only. Expired rows are pruned by the retention ``auth_tokens`` task.
    """
    now = utcnow()
    row = conn.execute(
        """
        SELECT u.*, t.max_expires_at, t.expires_at AS token_expires_at
        FROM auth_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ?
          AND t.expires_at > ?
          AND t.max_expires_at > ?
          AND u.is_active = 1
        """,
        (token_hash, now, now),
    ).fetchone()
    if row is None:
        return None
    new_expires_at = min(now + timedelta(hours=6), row["max_expires_at"])
    renew_before = new_expires_at - TOKEN_RENEW_MIN_DELTA
    if _convert_datetime(str(row["token_expires_at"]).encode()) <= renew_before:
        # Short write of its own; the guard makes a concurrent duplicate renewal a no-op.
        with transaction(conn):
            conn.execute(
                "UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ? AND expires_at <= ?",
                (new_expires_at, token_hash, renew_before),
            )
    return row


def get_user_for_token_readonly(conn: sqlite3.Connection, *, token_hash: str) -> sqlite3.Row | None:
    """Like get_user_for_token() but never renews the session (long-lived streams must not keep it alive)."""
    now = utcnow()
    return conn.execute(
        """
        SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ? AND t.expires_at > ? AND t.max_expires_at > ? AND u.is_active = 1
        """,
        (token_hash, now, now),
    ).fetchone()
