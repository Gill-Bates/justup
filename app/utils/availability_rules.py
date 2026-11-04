#!/usr/bin/env python3
#
# app/utils/availability_rules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""One gap and outage rule for every availability consumer.

The availability graph, the downtime history, the dashboard downtime widget, the uptime percentage
and the PDF report all derive downtime from this module. A missing measurement is UNKNOWN: it is
neither downtime nor uptime, and it must never be turned into either of them.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .tcp_ports import parse_port_list

# A gap is a distance between two points of one series above this many expected check intervals.
GAP_FACTOR = 3
# An outage shorter than this is a transient check failure, not downtime.
MIN_DOWNTIME_SECONDS = 60
# UDP scans are comparatively expensive, so they run at most this often (open decision D9).
UDP_MIN_INTERVAL_SECONDS = 300
# A UDP result older than this many UDP intervals is stale and counts as UNKNOWN.
UDP_RESULT_MAX_AGE_FACTOR = 2


def gap_threshold_seconds(interval_seconds: float) -> float:
    """Largest distance between two points that still counts as one continuous series."""
    return float(interval_seconds) * GAP_FACTOR


def is_gap(delta_seconds: float, interval_seconds: float) -> bool:
    """True if the distance between two points hides at least one missing measurement run."""
    return delta_seconds > gap_threshold_seconds(interval_seconds)


def udp_interval_seconds(target_interval_seconds: int) -> int:
    """Effective UDP scan interval: the target interval, but never faster than the UDP minimum."""
    return max(int(target_interval_seconds), UDP_MIN_INTERVAL_SECONDS)


def series_interval_seconds(metric: str, target_interval_seconds: float) -> float:
    """Expected spacing of one series: UDP series follow the (slower) UDP scan interval."""
    if metric.startswith("udp_"):
        return float(udp_interval_seconds(int(target_interval_seconds)))
    return float(target_interval_seconds)


def effective_interval_seconds(stamps: Iterable[datetime], interval_seconds: float) -> float:
    """Spacing of a series that may be resampled or rolled up (median distance, never below the interval).

    A thinned series has a larger regular spacing than the check interval; applying the gap rule with the
    check interval would turn every neighbouring pair into a gap.
    """
    ordered = sorted(stamps)
    deltas = sorted((b - a).total_seconds() for a, b in itertools.pairwise(ordered) if b > a)
    if len(deltas) < 3:
        return float(interval_seconds)
    return max(float(interval_seconds), deltas[len(deltas) // 2])


@dataclass(frozen=True)
class Outage:
    """Measured downtime of one series; ``end`` is None while the outage is ongoing."""

    start: datetime
    end: datetime | None

    def seconds(self) -> int | None:
        return None if self.end is None else int((self.end - self.start).total_seconds())


def measured_outages(
    points: Iterable[tuple[datetime, int | None]],
    *,
    interval_seconds: float,
    gap_interval_seconds: float | None = None,
    now: datetime | None = None,
) -> list[Outage]:
    """Derive outages from one time-ordered series of ``(ts, 1 | 0 | None)`` points.

    ``None`` is an explicit UNKNOWN point. An outage starts at a confirmed DOWN point and ends at the
    next UP point. Across a gap or an UNKNOWN point it ends at its last DOWN point (one interval for a
    single point), so unmeasured time is never counted. The outage is ongoing only while its last point is fresh.
    """
    interval = timedelta(seconds=interval_seconds)
    gap_interval = gap_interval_seconds or interval_seconds  # a thinned series has a larger regular spacing
    outages: list[Outage] = []
    start: datetime | None = None
    last: datetime | None = None

    def measured_end(down_start: datetime, last_down: datetime) -> datetime:
        # The outage is proven up to its last DOWN point; a single point covers one interval.
        return last_down if last_down > down_start else down_start + interval

    def close_unmeasured(next_ts: datetime) -> None:
        nonlocal start
        if start is not None and last is not None:
            outages.append(Outage(start, min(measured_end(start, last), next_ts)))
        start = None

    for ts, value in points:
        if start is not None and last is not None and is_gap((ts - last).total_seconds(), gap_interval):
            close_unmeasured(ts)
        if value == 0:
            if start is None:
                start = ts
        elif value == 1:
            if start is not None:
                outages.append(Outage(start, ts))
                start = None
        else:
            close_unmeasured(ts)
        last = ts
    if start is not None and last is not None:
        if now is None or not is_gap((now - last).total_seconds(), gap_interval):
            outages.append(Outage(start, None))
        else:
            outages.append(Outage(start, measured_end(start, last)))
    return outages


@dataclass(frozen=True)
class MergedOutage:
    """Overlapping outages of several endpoints, with the labels of the endpoints that failed."""

    start: datetime
    end: datetime | None
    services: tuple[str, ...]


def merge_outages(outages: Iterable[tuple[str, Outage]]) -> list[MergedOutage]:
    """Merge per-endpoint outages: the target is down while at least one enabled endpoint is down."""
    merged: list[tuple[datetime, datetime | None, set[str]]] = []
    for label, outage in sorted(outages, key=lambda item: item[1].start):
        if merged:
            start, end, labels = merged[-1]
            if end is None or outage.start <= end:
                new_end = None if end is None or outage.end is None else max(end, outage.end)
                labels.add(label)
                merged[-1] = (start, new_end, labels)
                continue
        merged.append((outage.start, outage.end, {label}))
    return [MergedOutage(start, end, tuple(sorted(labels))) for start, end, labels in merged]


def downtime_series(target: object) -> list[tuple[str, str]]:
    """``(metric, label)`` of every availability series of the checks currently enabled for a target.

    Each TCP and UDP port has its own series; disabled checks and removed ports contribute nothing.
    """

    def flag(key: str) -> bool:
        try:
            return bool(target[key])  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return False

    def text(key: str) -> str:
        try:
            return str(target[key] or "")  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return ""

    result: list[tuple[str, str]] = []
    if flag("enable_http_check"):
        result.append(("http_up", "HTTP"))
    if flag("enable_ping"):
        result.append(("ping_up", "Ping"))
    if flag("enable_tcp_connect"):
        try:
            tcp_ports = parse_port_list(text("tcp_ports")) or [80]
        except ValueError:
            tcp_ports = []
        result.extend((f"tcp_up_{port}", f"TCP:{port}") for port in tcp_ports)
    if flag("enable_udp_scan"):
        try:
            udp_ports = parse_port_list(text("udp_ports"))
        except ValueError:
            udp_ports = []
        result.extend((f"udp_up_{port}", f"UDP:{port}") for port in udp_ports)
    return result
