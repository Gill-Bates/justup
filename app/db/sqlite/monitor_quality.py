#!/usr/bin/env python3
#
# app/db/sqlite/monitor_quality.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitor quality samples (quality gate feature)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ...utils.time import utcnow
from .connection import transaction

# ---------------------------------------------------------------------------
# Monitor Quality (Quality Gate feature)
# ---------------------------------------------------------------------------


def insert_monitor_quality(
    conn: sqlite3.Connection,
    *,
    score: float | None,
    median_latency_ms: float | None,
    success_ratio: float,
    state: str,
) -> None:
    """
    Insert a monitor quality measurement and update the latest snapshot.

    Args:
            score: Quality score (0-100), None if no connectivity
            median_latency_ms: Median RTT to probe targets
            success_ratio: Ratio of successful probes (0.0-1.0)
            state: Quality state ("ok", "degraded", "down")
    """
    now = utcnow()

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO monitor_quality(ts, score, median_latency_ms, success_ratio, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now, score, median_latency_ms, success_ratio, state),
        )

        conn.execute(
            """
            INSERT INTO monitor_quality_latest(id, score, state, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                score = excluded.score,
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (score, state, now),
        )


def get_monitor_quality_latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Get the full latest monitor quality snapshot."""
    return conn.execute("SELECT * FROM monitor_quality_latest WHERE id = 1").fetchone()


def query_monitor_quality(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[sqlite3.Row]:
    """
    Query monitor quality time series.

    Args:
            since: Start of time range (inclusive)
            until: End of time range (inclusive)
            limit: Maximum number of rows to return

    Returns:
            List of monitor quality rows, ordered by timestamp ascending
    """
    conditions = []
    params: list[Any] = []

    if since:
        conditions.append("ts >= ?")
        params.append(since)
    if until:
        conditions.append("ts <= ?")
        params.append(until)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    return conn.execute(
        f"""
        SELECT * FROM monitor_quality
        WHERE {where}
        ORDER BY ts ASC
        LIMIT ?
        """,  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
        params,
    ).fetchall()
