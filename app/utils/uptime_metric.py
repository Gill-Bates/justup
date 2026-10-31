#!/usr/bin/env python3
#
# app/utils/uptime_metric.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Choice of the availability metric that represents a target."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_PRIORITY = (("enable_http_check", "http_up"), ("enable_ping", "ping_up"), ("enable_tcp_connect", "tcp_up"))


def _flag(target: Mapping[str, Any] | Any, key: str) -> bool:
    """Read a flag from a dict or sqlite3.Row; a missing column counts as disabled."""
    try:
        return bool(target[key])
    except (KeyError, IndexError):
        return False


def select_uptime_metric(target: Mapping[str, Any] | Any) -> str | None:
    """Return the uptime metric for a target. Priority: HTTP > Ping > TCP; None if no check is enabled."""
    for flag, metric in _PRIORITY:
        if _flag(target, flag):
            return metric
    return None
