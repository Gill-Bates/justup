#!/usr/bin/env python3
#
# tests/test_scheduler_alert_state.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Scheduler alert state machine, restart restore, housekeeping retry and interval alignment."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from app.db import sqlite as sqlite_db
from app.monitor import scheduler
from app.monitor.scheduler import MonitorScheduler, TargetStatus
from app.utils import maintenance
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


def _sched(db_path: Path, tmp_path: Path) -> MonitorScheduler:
    return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")


def _target(db_path: Path, **flags) -> int:
    values = {
        "name": "t",
        "interval_seconds": 60,
        "retention_days": 30,
        "host": "t.example",
        "enable_http_check": False,
        "enable_ping": False,
        "enable_tcp_connect": False,
        "enable_cert_expiration": False,
        "http_url": "https://t.example/",
        "ping_host": "t.example",
        "tcp_host": "t.example",
        "tcp_ports": "80",
    }
    values.update(flags)
    conn = sqlite_db.connect(db_path)
    try:
        return sqlite_db.create_target(conn, values)
    finally:
        sqlite_db.close_connection(conn)


def _alert_rows(db_path: Path) -> list:
    conn = sqlite_db.connect(db_path)
    try:
        return list(conn.execute("SELECT id, status FROM alerts"))
    finally:
        sqlite_db.close_connection(conn)


async def _alert(sched: MonitorScheduler, tid: int, status: str) -> None:
    await sched._maybe_alert(
        target_id=tid, target_name="t", overall_ok=status == TargetStatus.UP, overall_status=status
    )


# ── A1: a still-down target must not be reported as recovered ────────────────


def test_open_alert_survives_new_failure_below_timeout(db_path, tmp_path):
    tid = _target(db_path, enable_http_check=True, enable_tcp_connect=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    rt = sched._rt[tid]
    dispatch = mock.AsyncMock()
    with mock.patch.object(sched, "_dispatch_alert", dispatch):
        rt.down_since_by_service["http"] = utcnow() - timedelta(minutes=10)
        asyncio.run(_alert(sched, tid, TargetStatus.DOWN))
        alert_id = rt.current_alert_id
        assert alert_id and dispatch.await_count == 1

        # HTTP recovers, TCP just failed (below the timeout): target is still DOWN
        rt.down_since_by_service["http"] = None
        rt.down_since_by_service["tcp"] = utcnow() - timedelta(minutes=1)
        failures = rt.consecutive_failures
        asyncio.run(_alert(sched, tid, TargetStatus.DOWN))
        assert rt.current_alert_id == alert_id
        assert dispatch.await_count == 1
        assert rt.last_overall_ok is False
        assert rt.consecutive_failures == failures
        assert rt.consecutive_successes == 0
        assert [r["status"] for r in _alert_rows(db_path)] == ["open"]

        # Full UP resolves exactly once
        rt.down_since_by_service["tcp"] = None
        asyncio.run(_alert(sched, tid, TargetStatus.UP))
    assert rt.current_alert_id is None
    assert dispatch.await_count == 2
    assert [r["status"] for r in _alert_rows(db_path)] == ["resolved"]


# ── A2: restore after restart only touches relevant, enabled services ────────


@pytest.mark.parametrize(
    ("flags", "failed", "expected"),
    [
        ({"enable_http_check": True}, "http", {"http"}),
        ({"enable_ping": True}, "ping", {"ping"}),
        ({"enable_tcp_connect": True}, "tcp", {"tcp"}),
        ({"enable_http_check": True, "enable_tcp_connect": True}, "tcp", {"tcp"}),
        ({"enable_http_check": True, "enable_ping": True}, "http, ping", {"http", "ping"}),
        # named service is disabled now: fall back to the enabled availability checks
        ({"enable_http_check": True, "enable_cert_expiration": True}, "tcp", {"http"}),
        ({"enable_ping": True, "enable_tcp_connect": True}, "", {"ping", "tcp"}),
    ],
)
def test_restart_restores_only_relevant_enabled_services(db_path, tmp_path, flags, failed, expected):
    tid = _target(db_path, **flags)
    started = utcnow() - timedelta(minutes=30)
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.create_alert(
            conn,
            alert_id="a-1",
            target_id=tid,
            target_name="t",
            failed_service=failed,
            started_at=started,
            recipients_notified=[],
        )
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)

    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    rt = sched._rt[tid]
    assert rt.current_alert_id == "a-1"
    assert {k for k, v in rt.down_since_by_service.items() if v} == expected
    assert rt.down_since_by_service["cert"] is None

    # Recovery: only enabled services report, the alert resolves without naming others
    dispatch = mock.AsyncMock()
    for svc in expected:
        rt.down_since_by_service[svc] = None
    with mock.patch.object(sched, "_dispatch_alert", dispatch):
        asyncio.run(_alert(sched, tid, TargetStatus.UP))
    assert rt.current_alert_id is None
    payload = dispatch.await_args.args[0]
    assert payload.failed_service == (failed or "unknown")
    assert [r["status"] for r in _alert_rows(db_path)] == ["resolved"]


# ── A3: alert id becomes active only after a successful insert ───────────────


def test_failed_alert_insert_is_retried_without_duplicates(db_path, tmp_path):
    tid = _target(db_path, enable_http_check=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    rt = sched._rt[tid]
    rt.down_since_by_service["http"] = utcnow() - timedelta(minutes=10)
    dispatch = mock.AsyncMock()
    real_create = sqlite_db.create_alert
    with (
        mock.patch.object(sched, "_dispatch_alert", dispatch),
        mock.patch.object(
            scheduler.sqlite_db, "create_alert", side_effect=sqlite_db.sqlite3.OperationalError("locked")
        ),
    ):
        asyncio.run(_alert(sched, tid, TargetStatus.DOWN))
    assert rt.current_alert_id is None
    assert dispatch.await_count == 0
    assert _alert_rows(db_path) == []

    with (
        mock.patch.object(sched, "_dispatch_alert", dispatch),
        mock.patch.object(scheduler.sqlite_db, "create_alert", side_effect=real_create),
    ):
        asyncio.run(_alert(sched, tid, TargetStatus.DOWN))
        asyncio.run(_alert(sched, tid, TargetStatus.DOWN))
    assert rt.current_alert_id is not None
    assert len(_alert_rows(db_path)) == 1
    assert dispatch.await_count == 1


# ── A6: housekeeping schedules the next slot from the actual outcome ─────────


def _hk_sched(tmp_path) -> MonitorScheduler:
    s = MonitorScheduler(get_conn=mock.MagicMock(), tsdb_dir=tmp_path)
    s._targets[7] = {"is_enabled": True, "retention_days": 30}
    return s


def _run_hk(s: MonitorScheduler, now, *, outcome) -> None:
    with (
        mock.patch.object(s, "_run_db_maintenance", mock.AsyncMock()),
        mock.patch.object(s, "_do_housekeeping", side_effect=outcome),
        mock.patch.object(scheduler, "_stable_housekeeping_jitter", return_value=0),
    ):
        asyncio.run(s._run_housekeeping(now))


def test_housekeeping_exception_retries_soon(tmp_path):
    s = _hk_sched(tmp_path)
    now = utcnow()
    _run_hk(s, now, outcome=RuntimeError("boom"))
    assert s._housekeeping_due[7] == now + timedelta(seconds=scheduler.HOUSEKEEPING_ERROR_RETRY_SECONDS)


def test_housekeeping_during_maintenance_is_rechecked_soon(tmp_path):
    s = _hk_sched(tmp_path)
    now = utcnow()
    _run_hk(s, now, outcome=maintenance.MaintenanceActiveError("busy"))
    assert s._housekeeping_due[7] == now + timedelta(seconds=scheduler.HOUSEKEEPING_RETRY_SECONDS)


def test_housekeeping_success_waits_a_day(tmp_path):
    s = _hk_sched(tmp_path)
    now = utcnow()
    _run_hk(s, now, outcome=[None])
    assert s._housekeeping_due[7] == now + timedelta(seconds=scheduler.HOUSEKEEPING_INTERVAL_SECONDS)


# ── A9: next slot is strictly after now and may lie in the current interval ──


def _at(second: float) -> datetime:
    return datetime(2026, 1, 1, 12, 35, 0, tzinfo=UTC) + timedelta(seconds=second)


@pytest.mark.parametrize(
    ("now_s", "interval", "offset", "expected_s"),
    [
        (1, 60, 7, 7),  # before the slot: same interval
        (7, 60, 7, 67),  # exactly on the slot: the next one
        (8, 60, 7, 67),  # after the slot
        (-15, 60, 7, 7),  # previous interval
        (1, 5, 7, 2),  # offset >= interval: slots stay on k*5+7
        (0, 60, 0, 60),  # no jitter
    ],
)
def test_align_to_interval(now_s, interval, offset, expected_s):
    result = scheduler._align_to_interval(_at(now_s), interval, offset)
    assert result == _at(expected_s)
    assert result.tzinfo is UTC
    assert result > _at(now_s)
    assert (result.timestamp() - offset) % interval == 0


# ── Certificate alert wording and thresholds (alert starts at 7 days, critical at 3) ──


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (8, None),
        (7, "CERT WARNING (7d) - t.example"),
        (4, "CERT WARNING (4d) - t.example"),
        (3, "CERT CRITICAL (3d) - t.example"),
        (0, "CERT CRITICAL (0d) - t.example"),  # under 24 h left is not yet expired
        (-1, "CERT EXPIRED - t.example"),
    ],
)
def test_cert_alert_text_follows_thresholds(db_path, tmp_path, days_left, expected):
    assert scheduler.CERT_EXPIRY_ALERT_DAYS == 7
    tid = _target(db_path, enable_cert_expiration=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    with mock.patch.object(sched, "_dispatch_alert", mock.AsyncMock()):
        asyncio.run(sched._maybe_cert_alert(target_id=tid, target_name="t", days_left=days_left, cert_host="t.example"))
    conn = sqlite_db.connect(db_path)
    try:
        texts = [r[0] for r in conn.execute("SELECT failed_service FROM alerts")]
    finally:
        sqlite_db.close_connection(conn)
    assert texts == ([expected] if expected else [])
