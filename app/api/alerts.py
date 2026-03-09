#!/usr/bin/env python3
#
# app/api/alerts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Alert management API routes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import sqlite as sqlite_db
from ..models.alerts import AlertListResponse, AlertPublic
from ..utils.deps import get_conn
from ..utils.formatters import row_to_alert_dict
from .auth import get_current_user, require_can_close_alerts

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=AlertListResponse)
def list_alerts(
    target_id: Optional[str] = Query(None, description="Filter by target ID"),
    from_date: Optional[datetime] = Query(None, description="Filter from date (ISO format)"),
    to_date: Optional[datetime] = Query(None, description="Filter to date (ISO format)"),
    status: Optional[Literal["open", "resolved"]] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    conn: sqlite3.Connection = Depends(get_conn),
    _current_user: dict = Depends(get_current_user),
):
    """List all alerts with optional filters and pagination."""
    # Parse target_id (empty string → None)
    target_id_int: Optional[int] = None
    if target_id and target_id.strip():
        try:
            target_id_int = int(target_id)
            if target_id_int < 1:
                raise HTTPException(status_code=400, detail="target_id must be >= 1")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target_id")
    
    rows, total = sqlite_db.list_alerts(
        conn,
        target_id=target_id_int,
        from_date=from_date,
        to_date=to_date,
        status=status,
        page=page,
        page_size=page_size,
    )
    
    items = [AlertPublic(**row_to_alert_dict(row)) for row in rows]

    total_pages = (total + page_size - 1) // page_size
    
    return AlertListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{alert_id}", response_model=AlertPublic)
def get_alert(
    alert_id: UUID,
    conn: sqlite3.Connection = Depends(get_conn),
    _current_user: dict = Depends(get_current_user),
):
    """Get a single alert by ID (UUID format)."""
    row = sqlite_db.get_alert(conn, str(alert_id))
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return AlertPublic(**row_to_alert_dict(row))



@router.post("/{alert_id}/acknowledge", response_model=AlertPublic)
def acknowledge_alert(
    alert_id: UUID,
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(require_can_close_alerts),
):
    """Acknowledge an alert. Uses authenticated user as acknowledger."""
    alert_id_str = str(alert_id)
    row = sqlite_db.get_alert(conn, alert_id_str)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    # Check if already acknowledged
    if row["is_acknowledged"]:
        raise HTTPException(status_code=409, detail="Alert already acknowledged")
    
    # Always use authenticated username (no client override)
    ack_by = user["username"]
    
    success = sqlite_db.acknowledge_alert(conn, alert_id_str, ack_by)
    if not success:
        # Race condition: another request acknowledged it first
        raise HTTPException(status_code=409, detail="Alert already acknowledged")
    
    # Fetch updated record
    row = sqlite_db.get_alert(conn, alert_id_str)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return AlertPublic(**row_to_alert_dict(row))


@router.post("/acknowledge-all")
def acknowledge_all_alerts(
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(require_can_close_alerts),
):
    """Acknowledge all open alerts. Uses authenticated user as acknowledger."""
    updated = sqlite_db.acknowledge_all_open_alerts(conn, user["username"])
    return {"updated": updated}
