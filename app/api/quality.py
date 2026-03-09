#!/usr/bin/env python3
#
# app/api/quality.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for monitor quality (Quality Gate feature)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request

from ..db import sqlite as sqlite_db
from ..monitor.quality_probe import get_quality_targets
from ..utils.deps import get_conn
from ..utils.time import utcnow
from .auth import get_current_user

router = APIRouter(prefix="", tags=["quality"])


@router.get("/quality/current")
def get_quality_current(
    request: Request,
    conn=Depends(get_conn),
    _user: dict = Depends(get_current_user),
):
    """Get the current monitor quality state, score, and probe targets."""
    row = sqlite_db.get_monitor_quality_latest(conn)
    
    # Always include probe targets
    targets = get_quality_targets()
    
    if not row:
        return {
            "score": None,
            "state": "unknown",
            "updated_at": None,
            "message": "No quality data available yet",
            "targets": targets,
        }
    
    return {
        "score": row["score"],
        "state": row["state"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "note": "Quality score is based on recent probe runs",
        "targets": targets,
    }


@router.get("/quality/history")
def get_quality_history(
    request: Request,
    conn=Depends(get_conn),
    hours: int = Query(default=24, ge=1, le=168, description="Hours to look back (max 7 days)"),
    _user: dict = Depends(get_current_user),
):
    """Get quality score history for charting."""
    since = utcnow() - timedelta(hours=hours)
    rows = sqlite_db.query_monitor_quality(conn, since=since, limit=500)
    
    return [
        {
            "ts": row["ts"].isoformat() if row["ts"] else None,
            "score": row["score"],
            "median_latency_ms": row["median_latency_ms"],
            "success_ratio": row["success_ratio"],
            "state": row["state"],
        }
        for row in rows
    ]
