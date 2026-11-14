#!/usr/bin/env python3
#
# app/db/sqlite/scheduler_runs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Scheduler run registry and per-target locks."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from ...utils.time import utcnow
from .connection import transaction

# ─── Scheduler Locking (Idempotency) ────────────────────────────────────────


def register_scheduler_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Register a new scheduler run for idempotency tracking."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO scheduler_runs (run_id, started_at, heartbeat_at) VALUES (?, ?, ?)",
            (run_id, now, now),
        )


def update_scheduler_heartbeat(conn: sqlite3.Connection, run_id: str) -> None:
    """Update scheduler heartbeat to prevent lock expiry."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "UPDATE scheduler_runs SET heartbeat_at = ? WHERE run_id = ?",
            (now, run_id),
        )


def cleanup_stale_scheduler_runs(conn: sqlite3.Connection, timeout_seconds: int = 300) -> int:
    """Remove scheduler runs that haven't heartbeated recently."""
    cutoff = utcnow() - timedelta(seconds=timeout_seconds)
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM scheduler_runs WHERE heartbeat_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def try_lock_target(
    conn: sqlite3.Connection,
    target_id: int,
    run_id: str,
    ttl_seconds: int,
) -> bool:
    """
    Try to acquire lock for target check.

    Returns True if lock acquired, False if already locked by another run.
    The expiry DELETE takes the write lock first, so the holder check and the write run
    serialized; INSERT OR REPLACE alone would take over a live lock and is only the write step.
    """
    now = utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)

    with transaction(conn):
        # Clean up expired locks first
        conn.execute(
            "DELETE FROM target_locks WHERE expires_at < ?",
            (now,),
        )

        existing = conn.execute(
            "SELECT run_id FROM target_locks WHERE target_id = ? AND expires_at >= ?",
            (target_id, now),
        ).fetchone()

        if existing and existing[0] != run_id:
            return False

        conn.execute(
            "INSERT OR REPLACE INTO target_locks (target_id, run_id, locked_at, expires_at) VALUES (?, ?, ?, ?)",
            (target_id, run_id, now, expires_at),
        )
        return True


def renew_target_lock(conn: sqlite3.Connection, target_id: int, run_id: str, ttl_seconds: int) -> bool:
    """Extend a still valid lock of ``run_id``; never creates one.

    False means the lock expired or was taken over, even if it has been released again since:
    the holder must then discard its results.
    """
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            "UPDATE target_locks SET expires_at = ? WHERE target_id = ? AND run_id = ? AND expires_at >= ?",
            (now + timedelta(seconds=ttl_seconds), target_id, run_id, now),
        )
        return cur.rowcount == 1


def release_target_lock(conn: sqlite3.Connection, target_id: int, run_id: str) -> None:
    """Release target lock after check completes."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM target_locks WHERE target_id = ? AND run_id = ?",
            (target_id, run_id),
        )
