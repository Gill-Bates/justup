#!/usr/bin/env python3
#
# app/api/dashboard_layout.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-user dashboard layout endpoints (GridStack).

The layout is personal UI config: every endpoint resolves the row owner from the authenticated
user's id (never from input), so a user can only read, write or delete their own layout. Mutations
are CSRF-protected by CSRFMiddleware (path listed there) and rate limited.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..db.dashboard_layouts import (
    DASHBOARD_MAX_Y,
    DASHBOARD_WIDGET_MIN_SIZES,
    delete_dashboard_layout,
    get_dashboard_layout,
    set_dashboard_layout,
)
from ..utils.availability_rules import gap_threshold_seconds
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.rate_limit import RATE_LIMIT_DEFAULT, rate_limit
from ..utils.time import parse_utc, utcnow
from ..utils.uptime_metric import select_uptime_metric
from .auth import get_current_user

# Server-side allowlist of widget ids, derived from the minimum-size table; kept in sync with the
# registry in app/static/js/dashboard-widgets.js by tests/test_dashboard_layout.py.
DASHBOARD_WIDGETS = frozenset(DASHBOARD_WIDGET_MIN_SIZES)


class DashboardWidgetLayout(BaseModel):
    """One placed widget. extra="forbid" rejects any DOM/HTML or unknown field."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=DASHBOARD_MAX_Y)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=100)
    visible: bool = True
    height_mode: Literal["auto", "manual"]


class DashboardLayout(BaseModel):
    """The sole active layout contract: version 2 with an explicit height mode per widget."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[2]
    widgets: list[DashboardWidgetLayout] = Field(max_length=len(DASHBOARD_WIDGETS))


def validate_dashboard_layout(body: DashboardLayout) -> None:
    """Reject unknown/duplicate ids and tiles crossing the grid bounds (422)."""
    ids = [widget.id for widget in body.widgets]
    unknown = sorted(set(ids) - DASHBOARD_WIDGETS)
    if unknown:
        raise HTTPException(422, f"Unknown widget id(s): {', '.join(unknown)}")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Duplicate widget id")
    for widget in body.widgets:
        min_w, min_h = DASHBOARD_WIDGET_MIN_SIZES[widget.id]
        if widget.w < min_w or widget.h < min_h:
            raise HTTPException(422, f"Widget {widget.id} is smaller than its minimum size ({min_w}x{min_h})")
        if widget.x + widget.w > 12:
            raise HTTPException(422, f"Widget {widget.id} extends past the 12-column grid")
        if widget.y + widget.h > DASHBOARD_MAX_Y:
            raise HTTPException(422, f"Widget {widget.id} extends past the {DASHBOARD_MAX_Y}-row vertical bound")


router = APIRouter(prefix="/me/dashboard-layout", tags=["dashboard"])


def _availability_seconds(points: list[dict], since: datetime, until: datetime, stale_after: float):
    """Integrate observed transitions; unknown intervals contribute to neither total."""
    known = 0.0
    up = 0.0
    for index, point in enumerate(points):
        start = parse_utc(point["ts"])
        if start is None or point["up"] not in (0, 1):
            continue
        # A sample only vouches for stale_after seconds, however late the next one arrives.
        next_ts = parse_utc(points[index + 1]["ts"]) if index + 1 < len(points) else None
        end = min(next_ts or until, start + timedelta(seconds=stale_after))
        duration = max(0.0, (min(until, end) - max(since, start)).total_seconds())
        known += duration
        up += duration * point["up"]
    return up, known


@router.get("/uptime", dependencies=[Depends(rate_limit(RATE_LIMIT_DEFAULT))])
async def get_uptime_widget(request: Request, conn=Depends(get_conn), _user=Depends(get_current_user)):
    """Return time-weighted availability and coverage without shipping raw checks."""
    until = utcnow()
    since = until - timedelta(days=1)
    tsdb_dir = get_tsdb_dir(request)
    targets = [
        target for target in sqlite_db.list_targets(conn) if target["is_enabled"] and select_uptime_metric(target)
    ]
    semaphore = asyncio.Semaphore(4)

    async def summarize(target):
        metric = select_uptime_metric(target)
        service = {"http_up": "HTTP", "ping_up": "PING", "tcp_up": "TCP", "udp_up": "UDP"}[metric]
        async with semaphore:
            series = await run_in_threadpool(
                tsdb_db.query_availability_series,
                tsdb_dir,
                target_id=target["id"],
                since=since,
                until=until,
                limit=100_000,
                enabled_services={service},
                check_interval_seconds=target["interval_seconds"],
            )
        return _availability_seconds(
            series.get(service, []), since, until, max(60, gap_threshold_seconds(target["interval_seconds"]))
        )

    results = await asyncio.gather(*(summarize(target) for target in targets))
    up = sum(result[0] for result in results)
    known = sum(result[1] for result in results)
    total = len(targets) * 86400
    return {
        "uptime_percent": 100 * up / known if known else None,
        "coverage_percent": 100 * known / total if total else 0,
    }


@router.get("/monitors", dependencies=[Depends(rate_limit(RATE_LIMIT_DEFAULT))])
def get_monitor_widgets(conn=Depends(get_conn), _user=Depends(get_current_user)):
    """Return only public metadata and cached measurements needed by dashboard widgets."""
    statuses = {row["target_id"]: dict(row) for row in sqlite_db.get_all_target_statuses(conn)}
    fields = ("http_latency_ms", "cert_days_left", "cert_checked_at", "updated_at")
    return {
        "targets": [
            {
                "id": target["id"],
                "name": target["name"],
                "is_enabled": bool(target["is_enabled"]),
                "interval_seconds": target["interval_seconds"],
                "enable_http_check": bool(target["enable_http_check"]),
                "enable_cert_expiration": bool(target["enable_cert_expiration"]),
                "status": {field: statuses.get(target["id"], {}).get(field) for field in fields},
            }
            for target in sqlite_db.list_targets(conn)
        ]
    }


@router.get("", dependencies=[Depends(rate_limit(RATE_LIMIT_DEFAULT))])
def get_layout(
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Return the current user's stored layout, or {"layout": null} when none is stored."""
    layout = get_dashboard_layout(conn, int(user["id"]))
    if layout is not None and not user["is_admin"]:
        layout["widgets"] = [widget for widget in layout["widgets"] if widget["id"] != "delivery-queue"]
    return {"layout": layout}


@router.put("", dependencies=[Depends(rate_limit(RATE_LIMIT_DEFAULT))])
def put_layout(
    body: DashboardLayout,
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Validate and persist the current user's layout, then return it."""
    validate_dashboard_layout(body)
    if not user["is_admin"] and any(widget.id == "delivery-queue" for widget in body.widgets):
        raise HTTPException(403, "Delivery queue widget requires administrator access")
    layout = body.model_dump()
    try:
        set_dashboard_layout(conn, int(user["id"]), layout)
    except ValueError as exc:
        raise HTTPException(400, "Invalid dashboard layout") from exc
    return {"layout": layout}


@router.delete("", dependencies=[Depends(rate_limit(RATE_LIMIT_DEFAULT))])
def delete_layout(
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Delete the current user's stored layout (idempotent)."""
    delete_dashboard_layout(conn, int(user["id"]))
    return {"layout": None}
