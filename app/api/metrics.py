#!/usr/bin/env python3
#
# app/api/metrics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Metrics API routes (TSDB queries)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..utils.deps import get_conn, get_tsdb_dir
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


def _get_uptime_metric(target) -> Optional[str]:
    """Return the appropriate uptime metric for a target based on enabled checks.
    
    Priority: HTTP > Ping > TCP (most representative check wins).
    Works with both dict and sqlite3.Row objects.
    """
    # Unified getter for both dict and sqlite3.Row
    get = target.get if isinstance(target, dict) else lambda k, d=None: target[k] if k in target.keys() else d
    
    if get("enable_http_check"):
        return "http_up"
    if get("enable_ping"):
        return "ping_up"
    if get("enable_tcp_connect"):
        return "tcp_up"
    return None


@router.get("/targets/{target_id}/metrics/{metric}")
async def get_metric(
    target_id: int,
    metric: str,
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
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
):
	"""Query metric time series points for a target."""
	if sqlite_db.get_target(conn, target_id) is None:
		raise HTTPException(status_code=404, detail="Target not found")

	if not _is_allowed_metric(metric):
		raise HTTPException(status_code=400, detail="Unknown or unsupported metric")

	# Normalize naive datetimes to UTC to prevent range calculation bugs
	if since and since.tzinfo is None:
		since = since.replace(tzinfo=timezone.utc)
	if until and until.tzinfo is None:
		until = until.replace(tzinfo=timezone.utc)

	# Validate time range order
	if since and until and since > until:
		raise HTTPException(status_code=400, detail="'since' must be <= 'until'")

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
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10_000),
):
    """Get uptime data for all targets for dashboard chart."""
    targets = sqlite_db.list_targets(conn)
    tsdb_dir = get_tsdb_dir(request)
    
    # Normalize naive datetimes to UTC
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    
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
        metric = _get_uptime_metric(target)
        if not metric:
            continue
        
        meta.append((target["id"], target["name"]))
        # latest=True: dashboard uses most recent points only
        tasks.append(query_with_sem(target["id"], metric))
    
    if not tasks:
        return {"targets": []}
        
    results = await asyncio.gather(*tasks)
    
    response_data = []
    for (target_id, name), points in zip(meta, results):
        response_data.append({
            "target_id": target_id,
            "name": name,
            "points": _serialize_points(points) if points else [],
        })
    
    return {"targets": response_data}


@router.get("/dashboard/downtime")
async def get_downtime_targets(
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10_000),
):
    """Get per-service uptime data for targets that had downtime in the given time range."""
    targets = sqlite_db.list_targets(conn)
    tsdb_dir = get_tsdb_dir(request)
    
    # Normalize naive datetimes to UTC
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    
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

    # Query all services for each target
    tasks = []
    meta = []  # (target_id, target_name, enabled_services)
	
    for target in targets:
        enabled = []
        if target["enable_http_check"]:
            enabled.append("http_up")
        if target["enable_ping"]:
            enabled.append("ping_up")
        if target["enable_tcp_connect"]:
            enabled.append("tcp_up")
        if not enabled:
            continue
        
        meta.append((target["id"], target["name"], enabled))
        for svc in enabled:
            tasks.append(query_with_sem(target["id"], svc))
    
    if not tasks:
        return {"targets": []}
        
    results = await asyncio.gather(*tasks)
    
    # Reassemble results per target
    result_idx = 0
    response_data = []
    
    for target_id, name, enabled in meta:
        services_data = {}
        has_any_downtime = False
        
        for svc in enabled:
            points = results[result_idx]
            result_idx += 1
            
            if not points:
                continue
            
            has_downtime = False
            points_list = []
            for p in points:
                points_list.append({"ts": p.ts, "value": p.value})
                # Downtime detection: value is 0 (strictly binary for uptime metrics)
                if p.value == 0:
                    has_downtime = True
                    has_any_downtime = True
            
            if has_downtime:
                services_data[svc] = points_list
        
        if has_any_downtime:
            response_data.append({
                "target_id": target_id,
                "name": name,
                "services": services_data,
            })
    
    return {"targets": response_data}
