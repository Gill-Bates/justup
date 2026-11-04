#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3

import pytest

from app.utils.rate_limit import reset_all


def pytest_report_header() -> str:
    return f"SQLite runtime: {sqlite3.sqlite_version}"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate-limit counters are process-wide; no test may inherit another test's budget."""
    reset_all()
    yield
    reset_all()
