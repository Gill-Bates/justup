#!/usr/bin/env python3
#
# tests/test_alert_range_query.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitoring alert interval queries; notification inbox events use a separate model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from app.db import sqlite as sqlite_db
from tests import _support  # noqa: F401


def _target(conn, name: str) -> int:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cur = conn.execute(
        "INSERT INTO targets(name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) VALUES (?, 60, 30, 1, 0, 0, 'h', ?, ?)",
        (name, now, now),
    )
    return int(cur.lastrowid)


def _alert(conn, target_id: int, alert_id: str, started: datetime, ended: datetime | None) -> None:
    sqlite_db.create_alert(
        conn,
        alert_id=alert_id,
        target_id=target_id,
        target_name="target",
        failed_service="PING",
        started_at=started,
        recipients_notified=[],
    )
    if ended is not None:
        sqlite_db.resolve_alert(conn, alert_id, ended)


def test_monitoring_alert_range_returns_overlapping_intervals(tmp_path: Path) -> None:
    conn = sqlite_db.connect(tmp_path / "alerts.db")
    sqlite_db.init_schema(conn)
    try:
        target_id = _target(conn, "range-target")
        other_target_id = _target(conn, "other-target")
        start = datetime(2026, 1, 10, tzinfo=UTC)
        end = datetime(2026, 1, 20, tzinfo=UTC)
        _alert(conn, target_id, "before", start - timedelta(days=5), start - timedelta(days=1))
        _alert(conn, target_id, "cross-start", start - timedelta(days=1), start + timedelta(days=1))
        _alert(conn, target_id, "inside", start + timedelta(days=2), start + timedelta(days=3))
        _alert(conn, target_id, "cross-end", end - timedelta(days=1), end + timedelta(days=1))
        _alert(conn, target_id, "ongoing", start - timedelta(days=4), None)
        _alert(conn, target_id, "ends-at-start", start - timedelta(days=1), start)
        _alert(conn, target_id, "starts-at-end", end, end + timedelta(days=1))
        _alert(conn, other_target_id, "other-target", start, end)

        rows = sqlite_db.get_alerts_for_target_in_range(conn, target_id, start, end)
        assert [row["id"] for row in rows] == ["ongoing", "cross-start", "inside", "cross-end"]
        local_zone = timezone(timedelta(hours=2))
        local_start = start.astimezone(local_zone)
        local_end = end.astimezone(local_zone)
        local_rows = sqlite_db.get_alerts_for_target_in_range(conn, target_id, local_start, local_end)
        assert [row["id"] for row in local_rows] == [row["id"] for row in rows]
        assert sqlite_db.get_alerts_for_target_in_range(conn, target_id, end, start) == []
    finally:
        sqlite_db.close_connection(conn)
