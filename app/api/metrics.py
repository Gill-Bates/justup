#!/usr/bin/env python3
#
# app/api/metrics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Metrics API routes (TSDB queries)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.metric_limits import choose_display_bucket_seconds
from ..utils.time import assume_utc, utcnow
from ..utils.uptime_metric import select_uptime_metric
from .auth import get_current_user

router = APIRouter(prefix="", tags=["metrics"])

# Whitelist of allowed metric names (security + predictability)
ALLOWED_METRICS = {
    "http_ms",
    "http_up",
    "http_status",
    "ping_ms",
    "ping_up",
    "tcp_ms",
    "tcp_up",
    "cert_days_left",
}

# Pattern for port-specific TCP metrics (tcp_ms_80, tcp_up_443, etc.)
TCP_PORT_METRIC_PATTERN = re.compile(r"^tcp_(ms|up)_(\d{1,5})$")


def _is_allowed_metric(metric: str) -> bool:
    """Check if metric name is allowed (static or port-specific TCP)."""
    if metric in ALLOWED_METRICS:
        return True
    # Allow tcp_ms_<port> and tcp_up_<port> patterns
    match = TCP_PORT_METRIC_PATTERN.match(metric)
    if match:
        port = int(match.group(2))
        return 1 <= port <= 65535
    return False


# Threshold (seconds) above which auto_resolution downsampling kicks in.
# 1 hour is chosen because below this range, full-resolution data is manageable
# and provides better detail for short-term analysis.
AUTO_RESOLUTION_THRESHOLD_SECONDS = 3600

# Max concurrent TSDB queries for dashboard endpoints to prevent I/O overload
# when many targets exist. Balances responsiveness vs resource usage.
MAX_CONCURRENT_DASHBOARD_QUERIES = 20


def _serialize_points(points):
    """Serialize MetricPoint objects to JSON-compatible dicts."""
    return [{"ts": p.ts, "value": p.value} for p in points]


@router.get("/targets/{target_id}/metrics/{metric}")
async def get_metric(
    target_id: int,
    metric: str,
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=50_000),
    latest: bool = Query(
        default=False,
        description="If true, return the latest N points (up to 'limit') instead of oldest. "
        "When used with since/until, returns latest N within that range.",
    ),
    auto_resolution: bool = Query(
        default=False,
        description="If true, automatically select resolution based on time range (disables 'latest'). "
        "Requires 'since' parameter to calculate range.",
    ),
    display: bool = Query(default=False),
    plot_width: int = Query(default=900, ge=120, le=2400),
):
    """Query metric time series points for a target."""
    if sqlite_db.get_target(conn, target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found")

    if not _is_allowed_metric(metric):
        raise HTTPException(status_code=400, detail="Unknown or unsupported metric")

    # Normalize naive datetimes to UTC to prevent range calculation bugs
    since = assume_utc(since) if since else since
    until = assume_utc(until) if until else until

    # Validate time range order
    if since and until and since > until:
        raise HTTPException(status_code=400, detail="'since' must be <= 'until'")

    if display:
        if since is None or until is None:
            raise HTTPException(status_code=400, detail="'since' and 'until' are required for display")
        bucket_seconds = choose_display_bucket_seconds(since=since, until=until, plot_width=plot_width)
        result = await run_in_threadpool(
            tsdb_db.query_display_series,
            get_tsdb_dir(request),
            target_id=target_id,
            metric=metric,
            since=since,
            until=until,
            bucket_seconds=bucket_seconds,
        )
        return {"target_id": target_id, "metric": metric, **result}

    use_auto_res = False
    if auto_resolution and since:
        until_dt = until or datetime.now(tz=since.tzinfo)
        range_seconds = (until_dt - since).total_seconds()
        use_auto_res = range_seconds > AUTO_RESOLUTION_THRESHOLD_SECONDS

    if use_auto_res:
        latest = False  # auto_resolution and latest are mutually exclusive
        points = await run_in_threadpool(
            tsdb_db.query_auto_resolution,
            get_tsdb_dir(request),
            target_id=target_id,
            metric=metric,
            since=since,
            until=until,
            limit=limit,
        )
    else:
        points = await run_in_threadpool(
            tsdb_db.query,
            get_tsdb_dir(request),
            target_id=target_id,
            metric=metric,
            since=since,
            until=until,
            limit=limit,
            latest=latest,
        )
    return {
        "target_id": target_id,
        "metric": metric,
        "points": _serialize_points(points),
    }


@router.get("/dashboard/uptime")
async def get_all_uptime(
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10_000),
):
    """Get uptime data for all targets for dashboard chart."""
    targets = sqlite_db.list_targets(conn)
    tsdb_dir = get_tsdb_dir(request)

    # Normalize naive datetimes to UTC
    since = assume_utc(since) if since else since
    until = assume_utc(until) if until else until

    if since and until and since > until:
        raise HTTPException(status_code=400, detail="'since' must be <= 'until'")

    meta = []  # (target_id, target_name)

    # Cap limit for large target counts to prevent performance issues
    effective_limit = min(limit, 200) if len(targets) > 100 else limit

    # Semaphore limits concurrent TSDB I/O to prevent resource exhaustion
    sem = asyncio.Semaphore(MAX_CONCURRENT_DASHBOARD_QUERIES)

    async def query_with_sem(target_id: int, metric: str):
        async with sem:
            return await run_in_threadpool(
                tsdb_db.query,
                tsdb_dir,
                target_id=target_id,
                metric=metric,
                since=since,
                until=until,
                limit=effective_limit,
                latest=True,
            )

    tasks = []
    for target in targets:
        metric = select_uptime_metric(target)
        if not metric:
            continue

        meta.append((target["id"], target["name"]))
        # latest=True: dashboard uses most recent points only
        tasks.append(query_with_sem(target["id"], metric))

    if not tasks:
        return {"targets": []}

    results = await asyncio.gather(*tasks)

    response_data = []
    for (target_id, name), points in zip(meta, results, strict=False):
        response_data.append(
            {
                "target_id": target_id,
                "name": name,
                "points": _serialize_points(points) if points else [],
            }
        )

    return {"targets": response_data}


# Incidents shorter than this are transient check failures, not downtime.
MIN_DOWNTIME_SECONDS = 60
# Raw points read per series for the 24h overview (covers a 1s interval).
DOWNTIME_MAX_POINTS = 100_000


def _summarize_downtime(points, *, now: datetime, stale_after: float) -> dict:
    """Sum down periods (value 0 until the next up point) of one binary series.

    A series that ends down is ongoing only while its last point is fresh; a stale one is
    cut off at the last observed point so a stopped scheduler cannot grow the downtime.
    """
    total = 0.0
    incidents = 0
    down_since: datetime | None = None
    last_ts: datetime | None = None
    for pt in points:
        last_ts = pt.ts
        if pt.value == 0:
            if down_since is None:
                down_since = pt.ts
        elif down_since is not None:
            duration = (pt.ts - down_since).total_seconds()
            if duration >= MIN_DOWNTIME_SECONDS:
                total += duration
                incidents += 1
            down_since = None
    ongoing = False
    if down_since is not None and last_ts is not None:
        ongoing = (now - last_ts).total_seconds() <= stale_after
        end = now if ongoing else last_ts
        duration = (end - down_since).total_seconds()
        if duration >= MIN_DOWNTIME_SECONDS:
            total += duration
            incidents += 1
    return {"downtime_seconds": round(total), "incidents": incidents, "ongoing": ongoing}


@router.get("/dashboard/downtime")
async def get_downtime_targets(
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
):
    """Per-service downtime summary for enabled targets with at least one incident in the range."""
    targets = sqlite_db.list_targets(conn)
    tsdb_dir = get_tsdb_dir(request)

    # Normalize naive datetimes to UTC
    since = assume_utc(since) if since else since
    until = assume_utc(until) if until else until

    if since and until and since > until:
        raise HTTPException(status_code=400, detail="'since' must be <= 'until'")

    now = utcnow()
    # Semaphore limits concurrent TSDB I/O to prevent resource exhaustion
    sem = asyncio.Semaphore(MAX_CONCURRENT_DASHBOARD_QUERIES)

    async def summarize(target_id: int, metric: str, stale_after: float):
        async with sem:
            points = await run_in_threadpool(
                tsdb_db.query,
                tsdb_dir,
                target_id=target_id,
                metric=metric,
                since=since,
                until=until,
                limit=DOWNTIME_MAX_POINTS,
                latest=False,
            )
        return _summarize_downtime(points, now=now, stale_after=stale_after)

    tasks = []
    meta = []  # (target_id, target_name, enabled_services)

    for target in targets:
        # Disabled targets are not measured; their last state is not a running outage.
        if not target["is_enabled"]:
            continue
        enabled = []
        if target["enable_http_check"]:
            enabled.append("http_up")
        if target["enable_ping"]:
            enabled.append("ping_up")
        if target["enable_tcp_connect"]:
            enabled.append("tcp_up")
        if not enabled:
            continue

        stale_after = max(3 * target["interval_seconds"], MIN_DOWNTIME_SECONDS)
        meta.append((target["id"], target["name"], enabled))
        tasks.extend(summarize(target["id"], svc, stale_after) for svc in enabled)

    if not tasks:
        return {"targets": []}

    results = iter(await asyncio.gather(*tasks))
    response_data = []
    for target_id, name, enabled in meta:
        services = {svc: summary for svc in enabled if (summary := next(results))["incidents"] > 0}
        if services:
            response_data.append({"target_id": target_id, "name": name, "services": services})

    return {"targets": response_data}
