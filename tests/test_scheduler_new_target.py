#!/usr/bin/env python3
#
# tests/test_scheduler_new_target.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""A target created while the scheduler runs is measured right away; startup targets stay staggered."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.monitor.scheduler import MonitorScheduler
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _create(path: Path, name: str) -> int:
    conn = sqlite_db.connect(path)
    try:
        return sqlite_db.create_target(
            conn,
            {
                "name": name,
                "interval_seconds": 60,
                "retention_days": 30,
                "host": f"{name}.example",
                "enable_http_check": True,
                "http_url": f"https://{name}.example/",
            },
        )
    finally:
        sqlite_db.close_connection(conn)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


@pytest.fixture
def sched(db_path: Path, tmp_path: Path) -> MonitorScheduler:
    return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")


def test_target_present_at_startup_keeps_aligned_schedule(db_path, sched):
    tid = _create(db_path, "existing")
    now = utcnow()
    sched._refresh_targets(now)
    assert sched._rt[tid].next_due > now


def test_target_created_while_running_is_due_immediately(db_path, sched):
    first = _create(db_path, "existing")
    sched._refresh_targets(utcnow())
    new = _create(db_path, "fresh")
    now = utcnow()
    sched._refresh_targets(now)
    assert sched._rt[new].next_due <= now
    assert sched._rt[first].next_due > now - timedelta(seconds=1)


def test_existing_target_is_not_rescheduled_by_refresh(db_path, sched):
    tid = _create(db_path, "existing")
    sched._refresh_targets(utcnow())
    due = sched._rt[tid].next_due
    sched._refresh_targets(utcnow() + timedelta(seconds=5))
    assert sched._rt[tid].next_due == due


def test_request_refresh_sets_flag(sched):
    assert sched._refresh_requested is False
    sched.request_refresh()
    assert sched._refresh_requested is True
