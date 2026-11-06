#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.utils.rate_limit import reset_all


def pytest_report_header() -> str:
    return f"SQLite runtime: {sqlite3.sqlite_version}"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate-limit counters are process-wide; no test may inherit another test's budget."""
    reset_all()
    yield
    reset_all()


@pytest.fixture(autouse=True)
def _no_local_nmap_worker(monkeypatch):
    """App lifespans in tests must never spawn a worker process; tests of the supervisor opt back in."""
    monkeypatch.setattr("app.services.nmap_local.external_worker_configured", lambda: True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh database file with the current schema; modules needing another layout define their own."""
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path
