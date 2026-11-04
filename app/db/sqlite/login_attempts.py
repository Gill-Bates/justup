#!/usr/bin/env python3
#
# app/db/sqlite/login_attempts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Login failure tracking and IP lockout (rate limiting)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from ...utils.time import utcnow
from .connection import transaction

# ---------------------------------------------------------------------------
# Login Attempts (Rate Limiting)
# ---------------------------------------------------------------------------


def get_login_attempt(conn: sqlite3.Connection, ip_address: str) -> sqlite3.Row | None:
    """Get login attempt record for an IP address."""
    return conn.execute(
        "SELECT * FROM login_attempts WHERE ip_address = ?",
        (ip_address,),
    ).fetchone()


_LOGIN_LOCK_MAX_SECONDS = 3600


def login_lock_seconds(failed_count: int) -> int:
    """Per-IP lockout after ``failed_count`` failed logins: 2^n seconds, capped at one hour."""
    # 2**12 already exceeds the cap; bounding the exponent keeps huge counts cheap.
    return min(2 ** min(failed_count, 12), _LOGIN_LOCK_MAX_SECONDS)


def record_failed_login(conn: sqlite3.Connection, ip_address: str) -> int:
    """
    Record a failed login attempt and return the new failed count.

    The lockout duration follows login_lock_seconds().
    """
    now = utcnow()

    with transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE ip_address = ?",
            (ip_address,),
        ).fetchone()

        new_count = (int(row["failed_count"]) + 1) if row else 1
        lock_seconds = login_lock_seconds(new_count)
        locked_until = now + timedelta(seconds=lock_seconds)

        # Atomic upsert with correct locked_until from the start
        conn.execute(
            """
            INSERT INTO login_attempts(ip_address, failed_count, last_attempt_at, locked_until, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ip_address) DO UPDATE SET
                failed_count = ?,
                last_attempt_at = ?,
                locked_until = ?
            """,
            (ip_address, new_count, now, locked_until, now, new_count, now, locked_until),
        )
        return new_count


def clear_login_attempts(conn: sqlite3.Connection, ip_address: str) -> None:
    """Clear login attempts after successful login."""
    with transaction(conn):
        conn.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,))


def is_ip_locked(conn: sqlite3.Connection, ip_address: str) -> tuple[bool, int | None]:
    """
    Check if an IP is currently locked out.

    Returns: (is_locked, seconds_remaining)
    """
    record = get_login_attempt(conn, ip_address)
    if not record or not record["locked_until"]:
        return False, None

    now = utcnow()
    locked_until = record["locked_until"]

    if locked_until > now:
        remaining = int((locked_until - now).total_seconds())
        return True, remaining

    return False, None
