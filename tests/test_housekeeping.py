#!/usr/bin/env python3
#
# tests/test_housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001, ARG005
"""Scheduler housekeeping: pruning is decoupled from monitoring, DB maintenance retries."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest import mock

from app.monitor import scheduler
from app.monitor.scheduler import MonitorScheduler
from app.utils.time import utcnow
from tests import _support  # noqa: F401


def _sched(tmp_path, enabled: bool) -> MonitorScheduler:
    s = MonitorScheduler(get_conn=mock.MagicMock(), tsdb_dir=tmp_path)
    s._targets[7] = {"is_enabled": enabled, "retention_days": 365}
    return s


def _run(s: MonitorScheduler, now, *, cleanup=None):
    calls: list[str] = []
    report = scheduler.db_retention.RetentionReport()
    wal = {"mode": "PASSIVE", "busy": 0}
    with (
        mock.patch.object(scheduler.sqlite_db, "close_connection"),
        mock.patch.object(scheduler.db_retention, "run_retention", side_effect=cleanup or (lambda c: report)),
        mock.patch.object(scheduler.db_retention, "checkpoint_wal", return_value=wal),
        mock.patch.object(scheduler.db_retention, "run_optimize"),
        mock.patch.object(scheduler.db_retention, "collect_db_stats", return_value={}),
        mock.patch.object(scheduler.tsdb_db, "run_rollups", side_effect=lambda *a, **k: calls.append("rollup") or {}),
        mock.patch.object(scheduler.tsdb_db, "prune_all_series", side_effect=lambda *a, **k: calls.append("prune")),
        mock.patch.object(scheduler, "_stable_housekeeping_jitter", return_value=0),
    ):
        asyncio.run(s._run_housekeeping(now))
    return calls


def test_disabled_target_still_rolled_up_then_pruned(tmp_path):
    s = _sched(tmp_path, enabled=False)
    assert _run(s, utcnow()) == ["rollup", "prune"]


def test_incomplete_compression_backlog_is_retried_after_five_minutes(tmp_path):
    s = _sched(tmp_path, enabled=False)
    now = utcnow()
    pending = scheduler.tsdb_db.HousekeepingResult(compaction=scheduler.tsdb_db.CompactionResult(complete=False))
    with mock.patch.object(scheduler.tsdb_db, "housekeep_target", return_value=pending):
        assert _run(s, now) == []
    assert s._housekeeping_due[7] == now + timedelta(seconds=scheduler.HOUSEKEEPING_RETRY_SECONDS)
    assert _run(s, now + timedelta(minutes=6)) == ["rollup", "prune"]
    assert s._housekeeping_due[7] > now + timedelta(hours=23)


def test_housekeeping_passes_target_retention_to_rollups_and_pruning(tmp_path):
    s = _sched(tmp_path, enabled=False)
    s._targets[7]["retention_days"] = 45
    with (
        mock.patch.object(scheduler.sqlite_db, "close_connection"),
        mock.patch.object(
            scheduler.db_retention, "run_retention", return_value=scheduler.db_retention.RetentionReport()
        ),
        mock.patch.object(scheduler.db_retention, "checkpoint_wal", return_value={"mode": "PASSIVE", "busy": 0}),
        mock.patch.object(scheduler.db_retention, "run_optimize"),
        mock.patch.object(scheduler.db_retention, "collect_db_stats", return_value={}),
        mock.patch.object(scheduler.tsdb_db, "run_rollups") as run_rollups,
        mock.patch.object(scheduler.tsdb_db, "prune_all_series") as prune_all,
        mock.patch.object(scheduler, "_stable_housekeeping_jitter", return_value=0),
    ):
        asyncio.run(s._run_housekeeping(utcnow()))

    run_rollups.assert_called_once_with(tmp_path, 7, 45)
    prune_all.assert_called_once_with(tmp_path, 7, 45)


def test_enabled_to_disabled_and_reactivation_keep_schedule(tmp_path):
    s = _sched(tmp_path, enabled=True)
    now = utcnow()
    assert _run(s, now) == ["rollup", "prune"]
    s._targets[7]["is_enabled"] = False
    assert _run(s, now + timedelta(seconds=1)) == []  # not due yet
    assert _run(s, now + timedelta(days=2)) == ["rollup", "prune"]
    s._targets[7]["is_enabled"] = True
    assert _run(s, now + timedelta(days=4)) == ["rollup", "prune"]


def test_housekeeping_adds_no_raw_points_and_runs_no_checks(tmp_path):
    s = _sched(tmp_path, enabled=False)
    with mock.patch.object(s, "_run_target") as run_target:
        _run(s, utcnow())
    run_target.assert_not_called()
    assert not list(tmp_path.glob("target_7/*.jsonl"))


def test_db_maintenance_timestamp_only_on_success_and_retries_soon(tmp_path):
    s = _sched(tmp_path, enabled=True)
    now = utcnow()

    def boom(conn):
        raise RuntimeError("db locked")

    _run(s, now, cleanup=boom)
    assert s._last_db_maintenance is not None
    # Failure: next attempt due after the short retry delay, not after an hour.
    retry_in = scheduler.DB_MAINTENANCE_INTERVAL_SECONDS - (now - s._last_db_maintenance).total_seconds()
    assert retry_in == scheduler.TOKEN_CLEANUP_RETRY_SECONDS

    ok_calls = mock.Mock(return_value=scheduler.db_retention.RetentionReport())
    later = now + timedelta(seconds=scheduler.TOKEN_CLEANUP_RETRY_SECONDS + 1)
    _run(s, later, cleanup=ok_calls)
    ok_calls.assert_called_once()
    assert s._last_db_maintenance == later

    ok_calls.reset_mock()
    _run(s, now + timedelta(seconds=600), cleanup=ok_calls)
    ok_calls.assert_not_called()


def test_incomplete_retention_backlog_is_retried_soon(tmp_path):
    s = _sched(tmp_path, enabled=True)
    now = utcnow()
    partial = scheduler.db_retention.RetentionReport(complete=False)
    _run(s, now, cleanup=lambda c: partial)
    assert scheduler.DB_MAINTENANCE_INTERVAL_SECONDS - (now - s._last_db_maintenance).total_seconds() == (
        scheduler.TOKEN_CLEANUP_RETRY_SECONDS
    )


def test_io_errors_back_off_for_an_hour_instead_of_five_minutes(tmp_path):
    s = _sched(tmp_path, enabled=False)
    now = utcnow()
    failing = scheduler.tsdb_db.HousekeepingResult(
        compaction=scheduler.tsdb_db.CompactionResult(complete=False, errors=1)
    )
    with mock.patch.object(scheduler.tsdb_db, "housekeep_target", return_value=failing):
        _run(s, now)
    assert s._housekeeping_due[7] == now + timedelta(seconds=scheduler.HOUSEKEEPING_ERROR_RETRY_SECONDS)
