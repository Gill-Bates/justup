#!/usr/bin/env python3
#
# app/db/sqlite/target_status.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Latest per-target status snapshot rows."""

from __future__ import annotations

import sqlite3

from ...utils.time import utcnow
from .connection import transaction

# ─── Target Status Cache ────────────────────────────────────────────────────


def set_target_status_snapshot(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    ping_up: int | None,
    ping_latency_ms: float | None,
    http_up: int | None,
    http_status: int | None,
    http_latency_ms: float | None,
    tcp_up: int | None,
    tcp_latency_ms: float | None,
    tcp_port_status: str | None = None,
    cert_days_left: int | None,
    uptime_percent: float | None,
    uptime_percent_30d: float | None = None,
    overall_status: str,
) -> None:
    """Atomically update the full status snapshot for a target.

    This prevents mixed states from partial per-metric updates.
    The scheduler is the source of truth for the status semantics.
    """
    now = utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO target_status(target_id, overall_status, updated_at)
            VALUES (?, 'unknown', ?)
            ON CONFLICT(target_id) DO NOTHING
            """,
            (target_id, now),
        )

        prev = conn.execute(
            "SELECT overall_status, last_down_at, cert_days_left, cert_checked_at "
            "FROM target_status WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        prev_overall = prev["overall_status"] if prev else "unknown"
        last_down_at = prev["last_down_at"] if prev else None
        if overall_status == "down" and prev_overall != "down":
            last_down_at = now

        # Preserve last known cert_days_left if current check failed (None)
        # This prevents the badge from flickering to grey on transient failures
        effective_cert_days = cert_days_left
        effective_cert_checked_at = now if cert_days_left is not None else None
        if cert_days_left is None and prev:
            effective_cert_days = prev["cert_days_left"]
            effective_cert_checked_at = prev["cert_checked_at"]

        conn.execute(
            """
            UPDATE target_status
            SET
                ping_up = ?,
                ping_latency_ms = ?,
                ping_checked_at = ?,
                http_up = ?,
                http_status = ?,
                http_latency_ms = ?,
                http_checked_at = ?,
                tcp_up = ?,
                tcp_latency_ms = ?,
                tcp_checked_at = ?,
                tcp_port_status = ?,
                cert_days_left = ?,
                cert_checked_at = ?,
                uptime_percent = ?,
                uptime_percent_30d = ?,
                overall_status = ?,
                last_down_at = ?,
                updated_at = ?
            WHERE target_id = ?
            """,
            (
                ping_up,
                ping_latency_ms,
                now if ping_up is not None else None,
                http_up,
                http_status,
                http_latency_ms,
                now if http_up is not None else None,
                tcp_up,
                tcp_latency_ms,
                now if tcp_up is not None else None,
                tcp_port_status,
                effective_cert_days,
                effective_cert_checked_at,
                uptime_percent,
                uptime_percent_30d,
                overall_status,
                last_down_at,
                now,
                target_id,
            ),
        )


def get_target_status(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    """Get cached status for a target."""
    return conn.execute("SELECT * FROM target_status WHERE target_id = ?", (target_id,)).fetchone()


def get_all_target_statuses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get cached status for all targets."""
    return conn.execute("SELECT * FROM target_status ORDER BY target_id").fetchall()


def delete_target_status(conn: sqlite3.Connection, target_id: int) -> None:
    """Delete cached status for a target."""
    with transaction(conn):
        conn.execute("DELETE FROM target_status WHERE target_id = ?", (target_id,))
