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
    conn.commit()  # an open implicit transaction makes the later immediate transactions fail by design
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


def test_dashboard_alert_summary_uses_each_alerts_own_timestamp(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    conn = sqlite_db.connect(tmp_path / "summary.db")
    sqlite_db.init_schema(conn)
    try:
        assert sqlite_db.summarize_alerts(conn, now=now) == {
            "open": 0,
            "acknowledged_24h": 0,
            "started_24h": 0,
            "resolved_24h": 0,
        }
        target_id = _target(conn, "summary-target")
        # Open and never acknowledged, started an hour ago.
        _alert(conn, target_id, "open-new", now - timedelta(hours=1), None)
        # Started two days ago, resolved an hour ago: counts as resolved, not as started.
        _alert(conn, target_id, "long-resolved", now - timedelta(days=2), now - timedelta(hours=1))
        # Resolved two days ago: outside every 24 h figure.
        _alert(conn, target_id, "old-resolved", now - timedelta(days=3), now - timedelta(days=2))
        # Acknowledged (closed) 30 minutes ago: resolved and acknowledged, no longer open.
        _alert(conn, target_id, "acked", now - timedelta(hours=5), None)
        monkeypatch.setattr("app.db.sqlite.alerts.utcnow", lambda: now - timedelta(minutes=30))
        assert sqlite_db.acknowledge_alert(conn, "acked", "admin")
        # Acknowledged long ago: outside the 24 h acknowledged figure.
        _alert(conn, target_id, "acked-old", now - timedelta(days=4), None)
        monkeypatch.setattr("app.db.sqlite.alerts.utcnow", lambda: now - timedelta(days=3))
        assert sqlite_db.acknowledge_alert(conn, "acked-old", "admin")

        assert sqlite_db.summarize_alerts(conn, now=now) == {
            "open": 1,
            "acknowledged_24h": 1,
            "started_24h": 2,  # open-new, acked
            "resolved_24h": 2,  # long-resolved, acked
        }
    finally:
        sqlite_db.close_connection(conn)


def test_dashboard_alert_summary_is_exact_beyond_one_page(tmp_path: Path) -> None:
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    conn = sqlite_db.connect(tmp_path / "summary-many.db")
    sqlite_db.init_schema(conn)
    try:
        target_id = _target(conn, "many")
        for index in range(130):
            _alert(conn, target_id, f"open-{index}", now - timedelta(minutes=index + 1), None)
        for index in range(7):
            _alert(conn, target_id, f"done-{index}", now - timedelta(days=2), now - timedelta(minutes=index + 1))
        summary = sqlite_db.summarize_alerts(conn, now=now)
        assert summary["open"] == 130
        assert summary["started_24h"] == 130
        assert summary["resolved_24h"] == 7
    finally:
        sqlite_db.close_connection(conn)


def test_alert_duration_truncates_to_whole_seconds_on_all_paths(tmp_path: Path) -> None:
    conn = sqlite_db.connect(tmp_path / "duration.db")
    sqlite_db.init_schema(conn)
    try:
        target_id = _target(conn, "duration")
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        _alert(conn, target_id, "r", start, start + timedelta(seconds=59, milliseconds=999))
        _alert(conn, target_id, "r2", start, start + timedelta(seconds=60, milliseconds=1))
        _alert(conn, target_id, "bulk", start - timedelta(seconds=30, milliseconds=900), None)
        assert sqlite_db.acknowledge_all_open_alerts(conn, "admin") == 1
        got = {r["id"]: r["duration_seconds"] for r in conn.execute("SELECT id, duration_seconds FROM alerts")}
        assert got["r"] == 59
        assert got["r2"] == 60
        assert got["bulk"] >= 30  # now - (start - 30.9s) is at least 30 whole seconds
    finally:
        sqlite_db.close_connection(conn)


def test_acknowledge_keeps_preset_duration_seconds(tmp_path: Path) -> None:
    conn = sqlite_db.connect(tmp_path / "preset.db")
    sqlite_db.init_schema(conn)
    try:
        target_id = _target(conn, "preset")
        _alert(conn, target_id, "a", datetime(2026, 1, 1, tzinfo=UTC), None)
        conn.execute("UPDATE alerts SET duration_seconds = 7 WHERE id = 'a'")
        conn.commit()
        sqlite_db.acknowledge_all_open_alerts(conn, "admin")
        assert conn.execute("SELECT duration_seconds FROM alerts WHERE id = 'a'").fetchone()[0] == 7
    finally:
        sqlite_db.close_connection(conn)
