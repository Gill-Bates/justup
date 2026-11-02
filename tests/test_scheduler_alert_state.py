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


def test_manual_open_alert_is_historical_after_recovery_and_restart(db_path, tmp_path):
    target_id = _target(db_path, enable_http_check=True, auto_close_alert=False)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    rt = sched._rt[target_id]
    dispatch = mock.AsyncMock()
    with mock.patch.object(sched, "_dispatch_alert", dispatch):
        rt.down_since_by_service["http"] = utcnow() - timedelta(minutes=10)
        asyncio.run(_alert(sched, target_id, TargetStatus.DOWN))
        historical_id = rt.current_alert_id
        assert historical_id is not None

        rt.down_since_by_service["http"] = None
        asyncio.run(_alert(sched, target_id, TargetStatus.UP))
        assert rt.current_alert_id is None

        rt.down_since_by_service["http"] = utcnow() - timedelta(minutes=10)
        sched._last_alert_at[target_id] = utcnow() - timedelta(seconds=scheduler.ALERT_COOLDOWN_SECONDS + 1)
        asyncio.run(_alert(sched, target_id, TargetStatus.DOWN))
        active_id = rt.current_alert_id
        assert active_id is not None and active_id != historical_id

    conn = sqlite_db.connect(db_path)
    try:
        rows = conn.execute("SELECT id, status, active_incident FROM alerts ORDER BY started_at, id").fetchall()
        assert {row["id"] for row in rows if row["status"] == "open"} == {historical_id, active_id}
        assert {row["id"] for row in rows if row["active_incident"]} == {active_id}
    finally:
        sqlite_db.close_connection(conn)

    restarted = _sched(db_path, tmp_path)
    restarted._refresh_targets(utcnow())
    assert restarted._rt[target_id].current_alert_id == active_id


def test_manual_acknowledgement_clears_active_incident_state(db_path):
    target_id = _target(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        for alert_id in ("ack-one", "ack-all"):
            sqlite_db.create_alert(
                conn,
                alert_id=alert_id,
                target_id=target_id,
                target_name="t",
                failed_service="http",
                started_at=utcnow(),
                recipients_notified=[],
            )
        conn.commit()
        assert sqlite_db.acknowledge_alert(conn, "ack-one", "operator")
        assert sqlite_db.acknowledge_all_open_alerts(conn, "operator") == 1
        rows = conn.execute("SELECT status, active_incident FROM alerts ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [("resolved", 0), ("resolved", 0)]
    finally:
        sqlite_db.close_connection(conn)


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


def test_alert_dispatch_outbox_is_atomic_with_alert_creation(db_path):
    target_id = _target(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        with (
            mock.patch.object(sqlite_db, "enqueue_alert_dispatch", side_effect=RuntimeError("outbox unavailable")),
            pytest.raises(RuntimeError, match="outbox unavailable"),
        ):
            sqlite_db.create_alert(
                conn,
                alert_id="outbox-alert",
                target_id=target_id,
                target_name="t",
                failed_service="http",
                started_at=utcnow(),
                recipients_notified=[],
                dispatch_outbox=True,
            )

        assert conn.execute("SELECT 1 FROM alerts WHERE id = 'outbox-alert'").fetchone() is None

        sqlite_db.create_alert(
            conn,
            alert_id="outbox-alert",
            target_id=target_id,
            target_name="t",
            failed_service="http",
            started_at=utcnow(),
            recipients_notified=[],
            dispatch_outbox=True,
        )
        pending = sqlite_db.list_pending_alert_dispatches(conn)
        assert len(pending) == 1
        assert pending[0]["alert_id"] == "outbox-alert"
        assert pending[0]["event_type"] == "open"
    finally:
        sqlite_db.close_connection(conn)


def test_alert_dispatch_attempt_backoff_survives_until_completion(db_path):
    target_id = _target(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.create_alert(
            conn,
            alert_id="retry-alert",
            target_id=target_id,
            target_name="t",
            failed_service="http",
            started_at=utcnow(),
            recipients_notified=[],
            dispatch_outbox=True,
        )
        conn.commit()
        sqlite_db.record_alert_dispatch_attempt(
            conn,
            alert_id="retry-alert",
            event_type="open",
            succeeded=False,
            retry_delay_seconds=60,
            error="OperationalError",
        )
        pending = conn.execute(
            "SELECT status, attempts, last_error, next_attempt_at FROM alert_dispatch_outbox "
            "WHERE alert_id = 'retry-alert'"
        ).fetchone()
        assert pending["status"] == "pending"
        assert pending["attempts"] == 1
        assert pending["last_error"] == "OperationalError"
        assert pending["next_attempt_at"] > utcnow().isoformat()
        assert sqlite_db.list_pending_alert_dispatches(conn) == []
        assert sqlite_db.has_pending_alert_dispatches(conn)

        sqlite_db.record_alert_dispatch_attempt(conn, alert_id="retry-alert", event_type="open", succeeded=True)
        assert not sqlite_db.has_pending_alert_dispatches(conn)
    finally:
        sqlite_db.close_connection(conn)


def test_outbox_retries_partial_dispatch_then_completes(db_path, tmp_path):
    target_id = _target(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.create_alert(
            conn,
            alert_id="partial-alert",
            target_id=target_id,
            target_name="t",
            failed_service="http",
            started_at=utcnow(),
            recipients_notified=[],
            dispatch_outbox=True,
        )
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)

    sched = _sched(db_path, tmp_path)
    dispatcher = mock.MagicMock()
    dispatcher.dispatch_alert.side_effect = [
        mock.Mock(channels_attempted=3, all_succeeded=False),
        mock.Mock(channels_attempted=3, all_succeeded=True),
    ]
    sched._get_dispatcher = mock.Mock(return_value=dispatcher)
    with pytest.raises(RuntimeError, match="AlertDispatchPendingError"):
        asyncio.run(sched._process_alert_dispatch_outbox())
    assert dispatcher.dispatch_alert.call_count == 1
    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.has_pending_alert_dispatches(conn)
        assert sqlite_db.list_pending_alert_dispatches(conn) == []
        conn.execute("UPDATE alert_dispatch_outbox SET next_attempt_at = ?", (utcnow() - timedelta(seconds=1),))
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)

    asyncio.run(sched._process_alert_dispatch_outbox())
    assert dispatcher.dispatch_alert.call_count == 2
    conn = sqlite_db.connect(db_path)
    try:
        assert not sqlite_db.has_pending_alert_dispatches(conn)
        row = conn.execute(
            "SELECT status, attempts FROM alert_dispatch_outbox WHERE alert_id = ?", ("partial-alert",)
        ).fetchone()
        assert tuple(row) == ("completed", 2)
    finally:
        sqlite_db.close_connection(conn)


def test_notification_worker_progresses_while_housekeeping_is_blocked(db_path, tmp_path):
    sched = _sched(db_path, tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        sched._run_epoch = 1
        sched._last_refresh = loop.time()
        sched._last_heartbeat_at = utcnow()
        sched._run_quality_probe = mock.AsyncMock()
        housekeeping_started = asyncio.Event()
        release_housekeeping = asyncio.Event()
        notification_processed = asyncio.Event()

        async def slow_housekeeping(_now, _generation=None):
            housekeeping_started.set()
            await release_housekeeping.wait()

        async def process_notifications(_now, _generation=None, **kwargs):
            assert kwargs["raise_errors"] is True
            notification_processed.set()

        sched._run_housekeeping = slow_housekeeping
        sched._process_alert_dispatch_outbox = mock.AsyncMock()
        sched._process_notification_spooler = process_notifications
        iteration = asyncio.create_task(sched._run_iteration(loop))
        await housekeeping_started.wait()
        worker = asyncio.create_task(sched.run_notification_worker())
        await asyncio.wait_for(notification_processed.wait(), timeout=1)
        assert not iteration.done()
        sched.request_notification_stop()
        await worker
        release_housekeeping.set()
        assert await iteration

    asyncio.run(scenario())


def test_iteration_fails_for_internal_target_exception_not_down_result(db_path, tmp_path):
    sched = _sched(db_path, tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        sched._last_refresh = loop.time()
        sched._last_heartbeat_at = utcnow()
        sched._run_quality_probe = mock.AsyncMock()
        sched._run_housekeeping = mock.AsyncMock()
        sched._rt[1] = scheduler.TargetRuntime(next_due=utcnow() - timedelta(seconds=1))
        sched._run_target = mock.AsyncMock(side_effect=ValueError("internal failure"))
        failed = await sched._run_iteration(loop)
        sched._rt[1].next_due = utcnow() - timedelta(seconds=1)
        sched._run_target = mock.AsyncMock(return_value=None)
        normal_down = await sched._run_iteration(loop)
        return failed, normal_down

    assert asyncio.run(scenario()) == (False, True)


def test_quality_probe_failure_keeps_scheduler_unhealthy_until_probe_recovers(db_path, tmp_path):
    sched = _sched(db_path, tmp_path)
    failed_at = utcnow()
    with mock.patch.object(scheduler, "run_quality_probe", side_effect=OSError("probe unavailable")):
        asyncio.run(sched._run_quality_probe(failed_at))
    assert sched._quality_probe_failed

    async def iteration_result():
        loop = asyncio.get_running_loop()
        sched._last_refresh = loop.time()
        sched._last_heartbeat_at = utcnow()
        sched._run_housekeeping = mock.AsyncMock()
        return await sched._run_iteration(loop)

    assert asyncio.run(iteration_result()) is False
    recovered_at = failed_at + timedelta(seconds=scheduler.QUALITY_PROBE_INTERVAL_SECONDS + 1)
    with mock.patch.object(
        scheduler,
        "run_quality_probe",
        return_value=scheduler.QualityResult(score=100, median_latency_ms=1, success_ratio=1, state="ok"),
    ):
        asyncio.run(sched._run_quality_probe(recovered_at))
    assert not sched._quality_probe_failed


def test_tsdb_write_failure_is_retained_until_that_target_persists(db_path, tmp_path):
    target_id = _target(db_path, enable_ping=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    check_result = scheduler.checks.CheckResult(ok=True, latency_ms=1.0)
    with (
        mock.patch.object(scheduler.checks, "ping", mock.AsyncMock(return_value=check_result)),
        mock.patch.object(scheduler.tsdb_db, "query", return_value=[]),
        mock.patch.object(scheduler.tsdb_db, "append_point", side_effect=OSError("disk unavailable")),
    ):
        asyncio.run(sched._run_target(target_id, utcnow()))
    assert (target_id, "tsdb") in sched._persistence_failures

    with (
        mock.patch.object(scheduler.checks, "ping", mock.AsyncMock(return_value=check_result)),
        mock.patch.object(scheduler.tsdb_db, "query", return_value=[]),
        mock.patch.object(scheduler.tsdb_db, "append_point"),
    ):
        asyncio.run(sched._run_target(target_id, utcnow()))
    assert (target_id, "tsdb") not in sched._persistence_failures


def test_certificate_alert_dispatch_failure_is_retried_from_outbox(db_path, tmp_path):
    target_id = _target(db_path, enable_cert_expiration=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    with mock.patch.object(sched, "_dispatch_alert", mock.AsyncMock()):
        asyncio.run(
            sched._maybe_cert_alert(
                target_id=target_id,
                target_name="t",
                days_left=3,
                cert_host="t.example",
            )
        )

    dispatcher = mock.MagicMock()
    dispatcher.dispatch_alert.side_effect = [
        OSError("dispatcher unavailable"),
        mock.Mock(channels_attempted=1, all_succeeded=True),
    ]
    sched._get_dispatcher = mock.Mock(return_value=dispatcher)
    with pytest.raises(RuntimeError, match="AlertDispatchPendingError"):
        asyncio.run(sched._process_alert_dispatch_outbox())
    conn = sqlite_db.connect(db_path)
    try:
        row = conn.execute("SELECT status, attempts FROM alert_dispatch_outbox WHERE event_type = 'open'").fetchone()
        assert tuple(row) == ("pending", 1)
        conn.execute("UPDATE alert_dispatch_outbox SET next_attempt_at = ?", (utcnow() - timedelta(seconds=1),))
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)

    asyncio.run(sched._process_alert_dispatch_outbox())
    assert dispatcher.dispatch_alert.call_count == 2
    conn = sqlite_db.connect(db_path)
    try:
        row = conn.execute("SELECT status, attempts FROM alert_dispatch_outbox WHERE event_type = 'open'").fetchone()
        assert tuple(row) == ("completed", 2)
    finally:
        sqlite_db.close_connection(conn)


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
