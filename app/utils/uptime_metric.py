#!/usr/bin/env python3
#
# app/utils/uptime_metric.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Choice of the availability metric that represents a target."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .constants import UPTIME_THRESHOLD_GREEN, UPTIME_THRESHOLD_YELLOW

_PRIORITY = (("enable_http_check", "http_up"), ("enable_ping", "ping_up"), ("enable_tcp_connect", "tcp_up"))


def _flag(target: Mapping[str, Any] | Any, key: str) -> bool:
    """Read a flag from a dict or sqlite3.Row; a missing column counts as disabled."""
    try:
        return bool(target[key])
    except (KeyError, IndexError):
        return False


def enabled_availability_metrics(target: Mapping[str, Any] | Any) -> tuple[str, ...]:
    """Return the availability metrics of the checks currently enabled for a target."""
    return tuple(metric for flag, metric in _PRIORITY if _flag(target, flag))


def select_uptime_metric(target: Mapping[str, Any] | Any) -> str | None:
    """Return the uptime metric for a target. Priority: HTTP > Ping > TCP; None if no check is enabled."""
    for flag, metric in _PRIORITY:
        if _flag(target, flag):
            return metric
    return None


def uptime_level(
    uptime: float | None,
    *,
    sla_enabled: bool = False,
    sla_availability_pct: float | None = None,
) -> str:
    """Classify an uptime percentage as "ok", "warn", "bad" or "none" (no data).

    With an enabled availability SLA the goal alone decides (ok at/above, bad below, no warn band).
    Without one, the fixed UPTIME_THRESHOLD_* limits apply.
    """
    if uptime is None:
        return "none"
    if sla_enabled and sla_availability_pct is not None:
        return "ok" if uptime >= float(sla_availability_pct) else "bad"
    if uptime >= UPTIME_THRESHOLD_GREEN:
        return "ok"
    if uptime >= UPTIME_THRESHOLD_YELLOW:
        return "warn"
    return "bad"
