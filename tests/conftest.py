#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app.utils.rate_limit import reset_all


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate-limit counters are process-wide; no test may inherit another test's budget."""
    reset_all()
    yield
    reset_all()
