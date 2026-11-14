#!/usr/bin/env python3
#
# app/utils/uptime_metric.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Choice of the availability metric that represents a target."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any

from .constants import UPTIME_THRESHOLD_GREEN, UPTIME_THRESHOLD_YELLOW
from .tcp_ports import parse_port_list

# (service, config flag, metric) in priority order HTTP > Ping > TCP > UDP. Cert never counts as availability.
AVAILABILITY_CHECKS = (
    ("http", "enable_http_check", "http_up"),
    ("ping", "enable_ping", "ping_up"),
    ("tcp", "enable_tcp_connect", "tcp_up"),
    ("udp", "enable_udp_scan", "udp_up"),
)


def read_flag(target: Mapping[str, Any] | Any, key: str) -> bool:
    """Read a flag from a dict or sqlite3.Row; a missing column counts as disabled."""
    try:
        return bool(target[key])
    except (KeyError, IndexError, TypeError):
        return False


def has_udp_ports(target: Mapping[str, Any] | Any) -> bool:
    """Whether a target has at least one valid UDP port, judged by the shared port parser.

    A stored list such as ``","``, ``" "`` or garbage has no usable port: it is not a configuration error
    here (an old value must never break a caller) and simply means no UDP availability check.
    """
    try:
        return bool(parse_port_list(target["udp_ports"]))
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def availability_check_enabled(target: Mapping[str, Any] | Any, service: str, flag: str) -> bool:
    """Whether a check counts for availability; UDP only counts with at least one valid configured port."""
    if not read_flag(target, flag):
        return False
    return has_udp_ports(target) if service == "udp" else True


def enabled_availability_metrics(target: Mapping[str, Any] | Any) -> tuple[str, ...]:
    """Return the availability metrics of the checks currently enabled for a target."""
    return tuple(metric for svc, flag, metric in AVAILABILITY_CHECKS if availability_check_enabled(target, svc, flag))


# Points written by the scheduler carry one shared timestamp per check cycle (``cycle_at``), so they
# group by equality however long storing takes. The window only covers legacy points, which were
# stamped individually a few ms apart; the check interval is at least 5 s, so it never spans two cycles.
_CYCLE_WINDOW = timedelta(seconds=2)


def combine_availability(events: Iterable[tuple[datetime, str, int]]) -> Iterator[tuple[datetime, int]]:
    """Fold a time-ordered stream of ``(ts, metric, 0|1)`` into one availability sample per check cycle.

    A cycle is up only if every availability metric it contains is up, which is the rule of the overall
    target status (any enabled check down means down). Metrics without a point in a cycle, for example
    because the check was enabled later, are not counted against it.
    """
    start: datetime | None = None
    seen: dict[str, int] = {}
    for ts, metric, value in events:
        if start is not None and (metric in seen or ts - start > _CYCLE_WINDOW):
            yield start, int(all(v == 1 for v in seen.values()))
            start, seen = None, {}
        if start is None:
            start = ts
        seen[metric] = value
    if start is not None:
        yield start, int(all(v == 1 for v in seen.values()))


def select_uptime_metric(target: Mapping[str, Any] | Any) -> str | None:
    """Return the uptime metric for a target. Priority: HTTP > Ping > TCP, then UDP; None if no check is enabled.

    Meant for a single chart series; the PDF availability and the outage list aggregate all enabled checks.
    """
    metrics = enabled_availability_metrics(target)
    return metrics[0] if metrics else None


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
