#!/usr/bin/env python3
#
# app/db/sqlite/report_jobs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Report job bookkeeping."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ...utils.time import utcnow
from .connection import transaction

# ─── Report Jobs (Async PDF Generation) ────────────────────────────────────


def create_report_job(
    conn: sqlite3.Connection,
    job_id: str,
    target_id: int,
    expires_hours: int = 1,
    user_id: int | None = None,
) -> None:
    """Create a new pending report job owned by *user_id* (None = ownerless, admin-only access)."""
    now = utcnow()
    expires_at = now + timedelta(hours=expires_hours)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO report_jobs(job_id, target_id, status, progress, created_at, expires_at, user_id)
            VALUES(?, ?, 'pending', 0, ?, ?, ?)
            """,
            (job_id, target_id, now, expires_at, user_id),
        )


def _notify_report_stage(conn: sqlite3.Connection, job_id: str, stage: str) -> None:
    """Record one terminal report result in the caller's transaction."""
    row = conn.execute(
        "SELECT j.user_id, j.target_id, j.filename, t.name FROM report_jobs j "
        "LEFT JOIN targets t ON t.id = j.target_id WHERE j.job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return
    from .. import notifications

    notifications.notify_report(
        conn,
        stage=stage,
        job_id=job_id,
        user_id=row["user_id"],
        target_id=int(row["target_id"]),
        target_name=row["name"] or "target",
        filename=row["filename"],
    )


def get_report_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Get a report job by ID."""
    return conn.execute(
        "SELECT * FROM report_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()


def update_report_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    filename: str | None = None,
    error: str | None = None,
    completed: bool = False,
) -> None:
    """Update report job fields."""
    updates = []
    params: list[Any] = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if progress is not None:
        updates.append("progress = ?")
        params.append(progress)
    if filename is not None:
        updates.append("filename = ?")
        params.append(filename)
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    if completed:
        updates.append("completed_at = ?")
        params.append(utcnow())

    if not updates:
        return

    params.append(job_id)
    terminal = status in ("completed", "failed")
    # A finished job is never rewritten: not by a repeated terminal update (no second event) and not by a
    # late progress/processing write from a stale worker.
    guard = " AND status IN ('pending', 'processing')"
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE report_jobs SET {', '.join(updates)} WHERE job_id = ?{guard}",  # noqa: S608 - code-built column list, values bound as params
            params,
        )
        if terminal and cur.rowcount:
            _notify_report_stage(conn, job_id, status)


def get_expired_job_ids(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Get list of expired job IDs for cleanup (DAL abstraction)."""
    rows = conn.execute(
        "SELECT job_id FROM report_jobs WHERE expires_at < ?",
        (now,),
    ).fetchall()
    return [row["job_id"] for row in rows]


def prune_expired_report_jobs(conn: sqlite3.Connection) -> int:
    """Delete expired report job rows; their files on disk are not touched here."""
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM report_jobs WHERE expires_at < ?",
            (now,),
        )
        return cur.rowcount


def mark_stale_report_jobs_failed(conn: sqlite3.Connection) -> int:
    """Mark any 'pending' or 'processing' jobs as failed (called on startup).

    Jobs in these states after a restart are orphaned because the in-memory
    queue was lost. Mark them as failed so the frontend stops polling.
    """
    now = utcnow()
    with transaction(conn, immediate=True):
        stale_ids = [
            r[0] for r in conn.execute("SELECT job_id FROM report_jobs WHERE status IN ('pending', 'processing')")
        ]
        cur = conn.execute(
            """
            UPDATE report_jobs
            SET status = 'failed',
                progress = 100,
                error = 'Server restarted before job completed',
                completed_at = ?
            WHERE status IN ('pending', 'processing')
            """,
            (now,),
        )
        for stale_id in stale_ids:
            _notify_report_stage(conn, stale_id, "failed")
        return cur.rowcount
