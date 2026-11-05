#!/usr/bin/env python3
#
# app/utils/report_metrics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Metric collection rules shared by the synchronous PDF endpoint and the background PDF worker."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from ..db import tsdb as tsdb_db
from .availability_rules import downtime_series

_log = logging.getLogger(__name__)


def parse_tcp_ports(raw: str) -> list[int]:
    """Parse comma-separated TCP ports; non-numeric and out-of-range tokens are dropped."""
    ports: list[int] = []
    for raw_token in str(raw).split(","):
        token = raw_token.strip()
        if not token:
            continue
        if not token.isdigit():
            _log.warning("Ignoring non-numeric TCP port token: %r", token)
            continue
        port = int(token)
        if not (1 <= port <= 65535):
            _log.warning("Ignoring out-of-range TCP port: %d", port)
            continue
        ports.append(port)
    return ports


def cap_points(data: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Hard cap as a last line of defense (should rarely trigger).
    Preserves first and last data points for accurate chart boundaries.
    Note: This uses index-based sampling (not time-weighted), which is
    acceptable for visual chart rendering but may not preserve exact
    statistical properties if data points have irregular intervals.
    """
    if max_points <= 0:
        return data
    if len(data) <= max_points:
        return data
    if max_points < 3:
        # The general path below divides by (max_points - 2).
        return [data[0], data[-1]][:max_points]
    # Keep first and last, sample middle (visual sampling)
    result = [data[0]]
    step = (len(data) - 2) / (max_points - 2)
    result.extend(data[int(i * step)] for i in range(1, max_points - 1))
    result.append(data[-1])
    return result


def fetch_metric_data(
    tsdb_dir,
    target_id: int,
    metric: str,
    since: datetime,
    until: datetime,
    limit: int = 5000,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch one metric from the TSDB, capped to ``limit``; failures yield an empty series.
    Args:
        errors: Optional list to append error messages to (for downstream reporting).
    """
    try:
        points = tsdb_db.query_auto_resolution(
            tsdb_dir,
            target_id=target_id,
            metric=metric,
            since=since,
            until=until,
            limit=limit,
        )
        data = [{"ts": p.ts.isoformat(), "value": p.value} for p in points]
        return cap_points(data, limit)
    except Exception as e:  # noqa: BLE001 - request/worker boundary: log and return empty without leaking internals
        _log.warning(
            "Metric fetch failed: metric=%s target=%d error=%s",
            metric,
            target_id,
            e,
        )
        if errors is not None:
            errors.append(f"{metric}: data unavailable")
        return []


def fetch_downtime_periods(tsdb_dir, target_id: int, target: Any, since: datetime, until: datetime) -> list | None:
    """Return exact downtime periods newest first, or ``None`` so the report falls back to sampled events."""
    try:
        return tsdb_db.query_downtime_periods(
            tsdb_dir,
            target_id=target_id,
            since=since,
            until=until,
            limit=2**63 - 1,
            series=downtime_series(target),
            check_interval_seconds=target.get("interval_seconds") or 60,
        )
    except Exception:  # noqa: BLE001 - one bad series file must not fail the whole report
        _log.exception("Downtime query failed for target %d; using sampled fallback", target_id)
        return None


def report_filename(target_name: object, report_date: datetime) -> str:
    """Return ``YYYYMMDD-<ascii-target-name>-report.pdf``; the one name used by every PDF download path."""
    name = unicodedata.normalize("NFKD", str(target_name or "")).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:100].strip("_") or "report"
    return f"{report_date.strftime('%Y%m%d')}-{name}-report.pdf"
