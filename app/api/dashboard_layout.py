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

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..db.dashboard_layouts import (
    DASHBOARD_MAX_Y,
    delete_dashboard_layout,
    get_dashboard_layout,
    set_dashboard_layout,
)
from ..utils.deps import get_conn
from ..utils.rate_limit import RATE_LIMIT_DEFAULT, limiter
from .auth import get_current_user

# Server-side allowlist of widget ids; kept in sync with the registry in
# app/static/js/dashboard-widgets.js by tests/test_dashboard_layout.py.
DASHBOARD_WIDGETS = frozenset(
    {
        "status-online",
        "status-offline",
        "status-disabled",
        "status-total",
        "quality",
        "downtime",
    }
)


class DashboardWidgetLayout(BaseModel):
    """One placed widget. extra="forbid" rejects any DOM/HTML or unknown field."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=DASHBOARD_MAX_Y)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=100)
    visible: bool = True


class DashboardLayout(BaseModel):
    """The stored layout contract: version pinned to 1, at most one entry per known widget."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(ge=1, le=1)
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
        if widget.x + widget.w > 12:
            raise HTTPException(422, f"Widget {widget.id} extends past the 12-column grid")
        if widget.y + widget.h > DASHBOARD_MAX_Y:
            raise HTTPException(422, f"Widget {widget.id} extends past the {DASHBOARD_MAX_Y}-row vertical bound")


router = APIRouter(prefix="/me/dashboard-layout", tags=["dashboard"])


@router.get("")
@limiter.limit(RATE_LIMIT_DEFAULT)
def get_layout(
    request: Request,  # noqa: ARG001 - slowapi needs the request
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Return the current user's stored layout, or {"layout": null} when none is stored."""
    return {"layout": get_dashboard_layout(conn, int(user["id"]))}


@router.put("")
@limiter.limit(RATE_LIMIT_DEFAULT)
def put_layout(
    request: Request,  # noqa: ARG001 - slowapi needs the request
    body: DashboardLayout,
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Validate and persist the current user's layout, then return it."""
    validate_dashboard_layout(body)
    layout = body.model_dump()
    try:
        set_dashboard_layout(conn, int(user["id"]), layout)
    except ValueError as exc:
        raise HTTPException(400, "Invalid dashboard layout") from exc
    return {"layout": layout}


@router.delete("")
@limiter.limit(RATE_LIMIT_DEFAULT)
def delete_layout(
    request: Request,  # noqa: ARG001 - slowapi needs the request
    conn: sqlite3.Connection = Depends(get_conn),
    user=Depends(get_current_user),
):
    """Delete the current user's stored layout (idempotent)."""
    delete_dashboard_layout(conn, int(user["id"]))
    return {"layout": None}
