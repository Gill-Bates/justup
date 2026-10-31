#!/usr/bin/env python3
#
# app/api/pop.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
POP (Point of Presence) admin API endpoints.

Provides observability into POP locations database and refresh status.

Note: /refresh is a BLOCKING call that may take 30-60s for full PeeringDB import.
Consider using async background tasks for production deployments.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import partial
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db import pop_locations
from ..db import sqlite as sqlite_db
from ..services import pop_refresh
from ..utils.deps import get_conn
from ..utils.rate_limit import RATE_LIMIT_HEAVY, limiter
from .auth import require_admin

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/pop", tags=["admin", "pop"])


# ---------------------------------------------------------------------------
# Enums & Response Models
# ---------------------------------------------------------------------------


class RefreshSource(StrEnum):
    """Valid refresh source types."""

    PEERINGDB = "peeringdb"
    UNLOCODE = "unlocode"
    ALL = "all"


class PopStatusResponse(BaseModel):
    """Response model for POP status endpoint."""

    active_locations: int = Field(description="Count of non-retired POP locations")
    retired_locations: int = Field(description="Count of retired POP locations")
    total_locations: int = Field(description="Total count (active + retired)")
    sources: dict[str, int] = Field(description="Breakdown by source (active only)")
    last_peeringdb_run: dict | None = Field(description="Last PeeringDB import run")
    last_unlocode_run: dict | None = Field(description="Last UN/LOCODE import run")
    recent_runs: list[dict] = Field(description="Recent import runs")
    auto_refresh: dict = Field(description="Self-managing refresh state: last success/attempt, next attempt")
    locks: dict[str, dict] = Field(description="Lock status per source (process-local)")


class RefreshResultItem(BaseModel):
    """Single refresh result."""

    source: str
    success: bool
    run_id: str
    processed: int
    inserted: int
    updated: int
    retired: int
    skipped: int
    validation_errors: int
    error: str | None


class RefreshResponse(BaseModel):
    """Response model for refresh endpoint."""

    triggered: str
    results: list[RefreshResultItem]
    summary: dict[str, Any]


def _result_row(result: Any) -> dict[str, Any]:
    """Serialize a pop_refresh result into the RefreshResultItem shape."""
    return {name: getattr(result, name) for name in RefreshResultItem.model_fields}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", dependencies=[Depends(require_admin)], response_model=PopStatusResponse)
def get_pop_status(request: Request, conn=Depends(get_conn)) -> dict[str, Any]:
    """
    Get POP locations database status and refresh info.

    Returns:
        - active_locations: Count of non-retired POP locations
        - retired_locations: Count of retired POP locations
        - total_locations: Total count (active + retired)
        - sources: Breakdown by source (active only)
        - last_refresh: Last successful refresh info per source
        - locks: Current lock status (process-local only!)
    """
    # Use factory pattern correctly - pass factory, not connection
    status = pop_refresh.get_refresh_status(partial(sqlite_db.connect, request.app.state.db_path))

    # Add source breakdown (active only)
    # Known sources always appear (even at 0); any other stored source is listed too
    sources = dict.fromkeys(("seed", "manual", "peeringdb", "unlocode"), 0)
    for row in conn.execute("SELECT source, COUNT(*) FROM pop_locations WHERE retired = 0 GROUP BY source"):
        sources[row[0]] = row[1]

    status["sources"] = sources

    # Clarify lock semantics: these are process-local only
    if "locks" in status:
        status["locks"] = {
            source: {"locked": locked, "scope": "process-local", "note": "Only reflects this worker process"}
            for source, locked in status["locks"].items()
        }

    return status


@router.get("/runs", dependencies=[Depends(require_admin)])
def get_import_runs(
    limit: int = Query(10, ge=1, le=100, description="Max runs to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    conn=Depends(get_conn),
) -> list[dict[str, Any]]:
    """
    Get recent POP import run history.

    Args:
        limit: Max number of runs to return (default 10, max 100)
        offset: Pagination offset

    Returns:
        List of import run records with statistics
    """
    return pop_locations.get_import_run_stats(conn, limit=limit, offset=offset)


@router.post("/refresh", dependencies=[Depends(require_admin)])
@limiter.limit(RATE_LIMIT_HEAVY)
def trigger_refresh(
    request: Request,
    source: RefreshSource = Query(RefreshSource.ALL, description="Source to refresh"),
) -> JSONResponse:
    """
    Manually trigger a POP locations refresh.

    WARNING: This is a BLOCKING call that may take 30-60 seconds.

    Args:
        source: Source to refresh ("peeringdb", "unlocode", or "all")

    Returns:
        - 200: Success (full or partial)
        - 207: Multi-status (some sources failed)
        - 409: Conflict (already running)
        - 502: All sources failed
    """
    _log.info("Manual POP refresh triggered for source: %s", source.value)

    results = []
    connection_factory = partial(sqlite_db.connect, request.app.state.db_path)

    if source in (RefreshSource.PEERINGDB, RefreshSource.ALL):
        result = pop_refresh.refresh_from_peeringdb(connection_factory)
        results.append(_result_row(result))

    if source in (RefreshSource.UNLOCODE, RefreshSource.ALL):
        result = pop_refresh.refresh_from_unlocode(connection_factory)
        results.append(_result_row(result))

    # Determine response status
    total_processed = sum(r["processed"] for r in results)
    total_inserted = sum(r["inserted"] for r in results)
    failures = [r for r in results if not r["success"]]
    already_running = [r for r in results if r["error"] == "already_running"]

    response_data = {
        "triggered": source.value,
        "results": results,
        "summary": {
            "total_processed": total_processed,
            "total_inserted": total_inserted,
            "has_errors": len(failures) > 0,
            "all_failed": len(failures) == len(results),
        },
    }

    # HTTP status based on outcome
    if already_running and len(already_running) == len(results):
        # All requested sources already running
        return JSONResponse(status_code=409, content=response_data)
    if len(failures) == len(results) and len(results) > 0:
        # All sources failed
        return JSONResponse(status_code=502, content=response_data)
    if len(failures) > 0:
        # Partial failure
        return JSONResponse(status_code=207, content=response_data)
    # Full success
    return JSONResponse(status_code=200, content=response_data)


@router.get("/locations", dependencies=[Depends(require_admin)])
def list_locations(
    source: str | None = Query(None, description="Filter by source"),
    include_retired: bool = Query(False, description="Include retired locations"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    sort_by: Literal["canonical_code", "city", "country"] = Query("canonical_code", description="Sort field"),
    conn=Depends(get_conn),
) -> dict[str, Any]:
    """
    List POP locations with optional filtering.

    Args:
        source: Filter by source (optional, case-insensitive)
        include_retired: Include retired locations (default False)
        limit: Max results (default 100, max 500)
        offset: Pagination offset
        sort_by: Sort field (canonical_code, city, country)

    Returns:
        List of POP locations with correct pagination info
    """
    # Normalize source (case-insensitive)
    if source:
        source = source.lower()

    locations = pop_locations.list_pop_locations(
        conn,
        source=source,
        include_retired=include_retired,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )

    # Count with same filters for correct pagination
    retired_clause = "" if include_retired else "AND retired = 0"
    if source:
        row = conn.execute(
            f"SELECT COUNT(*) FROM pop_locations WHERE source = ? {retired_clause}",  # noqa: S608 - code-controlled retired_clause, source bound as param
            (source,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT COUNT(*) FROM pop_locations WHERE 1=1 {retired_clause}",  # noqa: S608 - code-controlled retired_clause
        ).fetchone()
    total_filtered = row[0]

    # Also get absolute totals for context
    total_all = pop_locations.get_pop_location_count(conn)
    total_active = pop_locations.count_active_pop_locations(conn)

    return {
        "locations": locations,
        "count": len(locations),
        "total_filtered": total_filtered,
        "total_active": total_active,
        "total_all": total_all,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(locations) < total_filtered,
    }
