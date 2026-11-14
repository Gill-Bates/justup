#!/usr/bin/env python3
#
# app/db/sqlite/alerts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Alerts and the durable alert dispatch outbox."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ...utils.time import elapsed_seconds, ensure_utc, utcnow
from .connection import transaction


def _duration_seconds(started_at: datetime, ended_at: datetime) -> int:
    """Whole seconds between two timestamps, truncated toward zero."""
    return elapsed_seconds(started_at, ended_at)


def create_alert(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    target_id: int,
    target_name: str,
    failed_service: str,
    started_at: datetime,
    recipients_notified: list[str],
    dispatch_outbox: bool = False,
) -> None:
    """Create a new downtime alert record."""
    now = utcnow()
    recipients_json = json.dumps(recipients_notified)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO alerts (
                id, target_id, target_name, failed_service, status, active_incident,
                started_at, recipients_notified, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'open', 1, ?, ?, ?, ?)
            """,
            (alert_id, target_id, target_name, failed_service, started_at, recipients_json, now, now),
        )
        from .. import notifications

        notifications.notify_alert_opened(
            conn, alert_id=alert_id, target_id=target_id, target_name=target_name, failed_service=failed_service
        )
        if dispatch_outbox:
            enqueue_alert_dispatch(
                conn,
                alert_id=alert_id,
                event_type="open",
                target_id=target_id,
                target_name=target_name,
                failed_service=failed_service,
                started_at=started_at,
            )


def enqueue_alert_dispatch(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    event_type: str,
    target_id: int,
    target_name: str,
    failed_service: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    duration_seconds: int | None = None,
) -> None:
    """Persist an alert event for retryable external notification dispatch."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO alert_dispatch_outbox (
                alert_id, event_type, target_id, target_name, failed_service,
                started_at, ended_at, duration_seconds, status, attempts,
                next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                alert_id,
                event_type,
                target_id,
                target_name,
                failed_service,
                started_at,
                ended_at,
                duration_seconds,
                now,
                now,
                now,
            ),
        )


def record_alert_dispatch_attempt(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    event_type: str,
    succeeded: bool,
    retry_delay_seconds: int = 0,
    error: str | None = None,
) -> None:
    now = utcnow()
    next_attempt_at = now + timedelta(seconds=max(0, retry_delay_seconds))
    with transaction(conn):
        conn.execute(
            """
            UPDATE alert_dispatch_outbox
            SET status = ?, attempts = attempts + 1, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE alert_id = ? AND event_type = ? AND status = 'pending'
            """,
            (
                "completed" if succeeded else "pending",
                None if succeeded else error,
                next_attempt_at,
                now,
                alert_id,
                event_type,
            ),
        )


# Outbox rows whose alert was removed by a cascade can never be dispatched; they must not count as pending.
_DISPATCHABLE_SQL = "status = 'pending' AND alert_id IN (SELECT id FROM alerts)"


def list_pending_alert_dispatches(conn: sqlite3.Connection, *, limit: int = 100) -> list[sqlite3.Row]:
    sql = (
        "SELECT * FROM alert_dispatch_outbox WHERE " + _DISPATCHABLE_SQL + " AND next_attempt_at <= ? "  # noqa: S608 - constants only
        "ORDER BY created_at, id LIMIT ?"
    )
    return conn.execute(sql, (utcnow(), limit)).fetchall()


def has_pending_alert_dispatches(conn: sqlite3.Connection) -> bool:
    sql = "SELECT 1 FROM alert_dispatch_outbox WHERE " + _DISPATCHABLE_SQL + " LIMIT 1"  # noqa: S608 - constants only
    return conn.execute(sql).fetchone() is not None


def resolve_alert(
    conn: sqlite3.Connection,
    alert_id: str,
    ended_at: datetime,
    *,
    dispatch_outbox: bool = False,
) -> None:
    """Mark an alert as resolved and record the end time."""
    now = utcnow()
    with transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT started_at, target_id, target_name, failed_service FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        if row:
            duration_seconds = _duration_seconds(row["started_at"], ended_at)
            conn.execute(
                """
                UPDATE alerts SET
                    status = 'resolved',
                    active_incident = 0,
                    ended_at = ?,
                    duration_seconds = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (ended_at, duration_seconds, now, alert_id),
            )
            if dispatch_outbox:
                enqueue_alert_dispatch(
                    conn,
                    alert_id=alert_id,
                    event_type="resolved",
                    target_id=int(row["target_id"]),
                    target_name=row["target_name"],
                    failed_service=row["failed_service"],
                    started_at=row["started_at"],
                    ended_at=ended_at,
                    duration_seconds=duration_seconds,
                )
            from .. import notifications

            notifications.notify_alert_resolved(
                conn,
                alert_id=alert_id,
                target_id=int(row["target_id"]),
                target_name=row["target_name"],
                failed_service=row["failed_service"],
            )


def deactivate_alert(conn: sqlite3.Connection, alert_id: str) -> None:
    """Keep an alert open for manual review while ending its active incident."""
    with transaction(conn):
        conn.execute(
            "UPDATE alerts SET active_incident = 0, updated_at = ? WHERE id = ? AND status = 'open'",
            (utcnow(), alert_id),
        )


def list_open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all currently open alerts.

    Returns:
            List of alert rows with columns: target_id, id, started_at, failed_service
    """
    return conn.execute("SELECT target_id, id, started_at, failed_service FROM alerts WHERE status = 'open'").fetchall()


def list_active_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List open alerts that still represent an active incident."""
    return conn.execute(
        "SELECT target_id, id, started_at, failed_service FROM alerts WHERE status = 'open' AND active_incident = 1"
    ).fetchall()


def acknowledge_alert(
    conn: sqlite3.Connection,
    alert_id: str,
    acknowledged_by: str,
) -> bool:
    """Acknowledge (close) an alert. Sets status to resolved and records who closed it. Returns True if updated."""
    now = utcnow()
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT started_at FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        duration_seconds = None
        if row:
            duration_seconds = _duration_seconds(row["started_at"], now)

        cur = conn.execute(
            """
            UPDATE alerts SET
                status = 'resolved',
                active_incident = 0,
                ended_at = COALESCE(ended_at, ?),
                duration_seconds = COALESCE(duration_seconds, ?),
                is_acknowledged = 1,
                acknowledged_by = ?,
                acknowledged_at = ?,
                updated_at = ?
            WHERE id = ? AND is_acknowledged = 0
            """,
            (now, duration_seconds, acknowledged_by, now, now, alert_id),
        )
        return cur.rowcount > 0


def acknowledge_all_open_alerts(
    conn: sqlite3.Connection,
    acknowledged_by: str,
) -> int:
    """Acknowledge (close) all open alerts and return number of updated rows."""
    now = utcnow()
    with transaction(conn, immediate=True):
        rows = conn.execute(
            "SELECT id, started_at FROM alerts WHERE status = 'open' AND is_acknowledged = 0"
        ).fetchall()
        # Durations are computed in Python so all paths share one truncation rule.
        conn.executemany(
            """
            UPDATE alerts SET
                status = 'resolved',
                active_incident = 0,
                ended_at = COALESCE(ended_at, ?),
                duration_seconds = COALESCE(duration_seconds, ?),
                is_acknowledged = 1,
                acknowledged_by = ?,
                acknowledged_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            [(now, _duration_seconds(r["started_at"], now), acknowledged_by, now, now, r["id"]) for r in rows],
        )
        return len(rows)


def list_alerts(
    conn: sqlite3.Connection,
    *,
    target_id: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
    """
    List alerts with optional filters and pagination.
    Returns (items, total_count).
    """
    conditions = []
    params: list[Any] = []

    if target_id is not None:
        conditions.append("target_id = ?")
        params.append(target_id)
    if from_date is not None:
        conditions.append("started_at >= ?")
        params.append(from_date)
    if to_date is not None:
        conditions.append("started_at <= ?")
        params.append(to_date)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM alerts WHERE {where_clause}",  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
        params,
    ).fetchone()
    total = count_row["cnt"] if count_row else 0

    offset = (page - 1) * page_size
    params_with_pagination = [*params, page_size, offset]
    rows = conn.execute(
        f"""
        SELECT * FROM alerts
        WHERE {where_clause}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
        params_with_pagination,
    ).fetchall()

    return rows, total


def summarize_alerts(
    conn: sqlite3.Connection, *, now: datetime | None = None, window: timedelta = timedelta(hours=24)
) -> dict[str, int]:
    """Count alerts for the dashboard, each figure on its own timestamp field.

    The window is the half-open UTC interval ``[now - window, now]``:
    ``open`` is every alert still open (no window); ``started`` counts ``started_at``;
    ``resolved`` counts ``ended_at`` of resolved alerts (acknowledged alerts included, as
    acknowledging also resolves); ``acknowledged`` counts ``acknowledged_at``.
    """
    upper = ensure_utc(now) if now is not None else utcnow()
    since = upper - window
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM alerts WHERE status = 'open'),
            (SELECT COUNT(*) FROM alerts WHERE started_at >= ? AND started_at <= ?),
            (SELECT COUNT(*) FROM alerts WHERE status = 'resolved' AND ended_at >= ? AND ended_at <= ?),
            (SELECT COUNT(*) FROM alerts
             WHERE status = 'resolved' AND is_acknowledged = 1 AND acknowledged_at >= ? AND acknowledged_at <= ?)
        """,
        (since, upper, since, upper, since, upper),
    ).fetchone()
    return {"open": row[0], "acknowledged_24h": row[3], "started_24h": row[1], "resolved_24h": row[2]}


def get_alert(conn: sqlite3.Connection, alert_id: str) -> sqlite3.Row | None:
    """Get a single alert by ID."""
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def get_alerts_for_target_in_range(
    conn: sqlite3.Connection,
    target_id: int,
    from_date: datetime,
    to_date: datetime,
) -> list[sqlite3.Row]:
    """Return monitoring-alert intervals overlapping the half-open range [from_date, to_date).

    These rows describe monitoring state, not the user-facing notification inbox. Inbox events
    use ``notifications.created_at`` and are queried by ``app.db.notifications``.
    """
    from_date = ensure_utc(from_date)
    to_date = ensure_utc(to_date)
    if from_date >= to_date:
        return []
    return conn.execute(
        """
        SELECT * FROM alerts
        WHERE target_id = ?
            AND started_at < ?
            AND (ended_at IS NULL OR ended_at > ?)
        ORDER BY started_at ASC
        """,
        (target_id, to_date, from_date),
    ).fetchall()


def delete_all_alerts(conn: sqlite3.Connection) -> int:
    """Delete all alerts from the database. Returns the number of deleted rows."""
    with transaction(conn):
        conn.execute("DELETE FROM alert_dispatch_outbox")
        cur = conn.execute("DELETE FROM alerts")
        return cur.rowcount


def delete_alerts_by_target(conn: sqlite3.Connection, target_id: int) -> int:
    """Delete all alerts (open and closed) for a specific target. Returns the number of deleted rows."""
    with transaction(conn):
        conn.execute("DELETE FROM alert_dispatch_outbox WHERE target_id = ?", (target_id,))
        cur = conn.execute("DELETE FROM alerts WHERE target_id = ?", (target_id,))
        return cur.rowcount
