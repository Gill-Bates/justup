#!/usr/bin/env python3
#
# tests/test_check_error_labels.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# justUp! - Lightweight Uptime Monitor
# Copyright (c) 2026 Gill-Bates
#

"""HTTP check failures must carry a readable detail, even when the library error has no message."""

from __future__ import annotations

import httpx
import pytest

from app.monitor.checks import _normalize_error_type


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout(""), httpx.ConnectTimeout(""), httpx.PoolTimeout(""), httpx.WriteTimeout("")],
)
def test_httpx_timeouts_are_labelled_timeout(error: Exception) -> None:
    assert _normalize_error_type(error) == "timeout"


@pytest.mark.parametrize("error", [httpx.ReadError(""), httpx.ConnectError(""), httpx.RemoteProtocolError("")])
def test_other_httpx_errors_never_yield_an_empty_detail(error: Exception) -> None:
    # An empty detail made the scheduler log "Unknown error".
    assert _normalize_error_type(error) == type(error).__name__
