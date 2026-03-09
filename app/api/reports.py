#!/usr/bin/env python3
#
# app/api/reports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for report generation (sync and async)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..services.pdf_report import ReportData, generate_pdf_report
from ..utils.metric_limits import choose_points_limit
from ..utils.time import parse_utc, utcnow
from ..services.pdf_worker import (
    PDFJobRequest,
    generate_job_id,
    get_pdf_path,
    submit_pdf_job,
)
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.rate_limit import limiter, RATE_LIMIT_PDF
from .auth import get_current_user

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


MAX_REPORT_DAYS = 90

# Valid page sizes for PDF generation
VALID_PAGE_SIZES = frozenset({"a4", "letter"})
JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_job_id(job_id: str) -> str:
    """Validate job ID format at API boundary before DB/filesystem access."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    return job_id


def _safe_filename(name: str, *, default: str = "report.pdf") -> str:
    """Sanitize filename for Content-Disposition and filesystem safety."""
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(name or "")).strip("._")
    return (sanitized[:255] or default)


def _parse_tcp_ports(raw: str) -> list[int]:
    """Parse and validate comma-separated TCP ports."""
    ports: list[int] = []
    for token in str(raw).split(","):
        token = token.strip()
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


def _parse_datetime(value: Optional[str], param_name: str = "datetime") -> Optional[datetime]:
    """
    Parse ISO datetime string to datetime object.
    
    Raises HTTPException if format is invalid (instead of silently returning None).
    """
    if not value:
        return None
    try:
        return parse_utc(value, strict=True)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid datetime format for '{param_name}': {value}"
        )


def _cap_points(data: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
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
    if max_points == 1:
        return [data[0]]
    if max_points == 2:
        return [data[0], data[-1]]
    # Keep first and last, sample middle (visual sampling)
    result = [data[0]]
    step = (len(data) - 2) / (max_points - 2)
    for i in range(1, max_points - 1):
        result.append(data[int(i * step)])
    result.append(data[-1])
    return result


def _fetch_metric_data(
    tsdb_dir,
    target_id: int,
    metric: str,
    since: datetime,
    until: datetime,
    limit: int = 5000,
    errors: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch metric data from TSDB.
    
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
        return _cap_points(data, limit)
    except Exception as e:
        _log.warning(
            "Metric fetch failed: metric=%s target=%d error=%s",
            metric,
            target_id,
            e,
        )
        if errors is not None:
            errors.append(f"{metric}: data unavailable")
        return []


def _get_uptime_metric(target_dict: dict) -> Optional[str]:
    """Determine uptime metric based on enabled checks (single source of truth)."""
    if target_dict.get("enable_http_check"):
        return "http_up"
    if target_dict.get("enable_ping"):
        return "ping_up"
    if target_dict.get("enable_tcp_connect"):
        return "tcp_up"
    return None


def _validate_report_range(
    since_str: Optional[str],
    until_str: Optional[str],
) -> tuple[datetime, datetime]:
    """
    Parse and validate report date range.
    
    Returns (from_date, to_date) with defaults applied.
    Raises HTTPException on invalid input.
    """
    now = utcnow()
    from_date = _parse_datetime(since_str, "since")
    to_date = _parse_datetime(until_str, "until")
    
    # Default: today
    if not from_date:
        from_date = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
    if not to_date:
        to_date = now
    
    # Validate range
    if from_date >= to_date:
        raise HTTPException(
            status_code=400,
            detail="Invalid date range: 'since' must be before 'until'"
        )
    
    if (to_date - from_date).total_seconds() > (MAX_REPORT_DAYS * 86400):
        raise HTTPException(
            status_code=400,
            detail=f"Maximum report range is {MAX_REPORT_DAYS} days"
        )
    
    return from_date, to_date


def _validate_page_size(page_size: str) -> str:
    """Validate and normalize page size.
    
    Raises HTTPException if page_size is not a valid option.
    """
    normalized = page_size.lower()
    if normalized not in VALID_PAGE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid page_size '{page_size}'. Must be one of: {', '.join(sorted(VALID_PAGE_SIZES))}"
        )
    return normalized


@router.get("/targets/{target_id}/pdf")
@limiter.limit(RATE_LIMIT_PDF)
async def get_target_pdf_report(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: Optional[str] = Query(None, description="Start date (ISO format)"),
    until: Optional[str] = Query(None, description="End date (ISO format)"),
    page_size: str = Query("a4", description="Page size: a4 or letter"),
):
    """
    Generate and download a PDF report for a target.
    
    Rate limited to 5 requests per minute to prevent abuse.
    Returns a PDF file with uptime statistics, charts, and downtime log.
    """
    # Fetch target
    target = sqlite_db.get_target(conn, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    
    target_dict = dict(target)
    tsdb_dir = get_tsdb_dir(request)
    
    # Parse and validate date range (raises HTTPException on invalid)
    from_date, to_date = _validate_report_range(since, until)
    
    # Validate page size (raises HTTPException on invalid)
    page_size = _validate_page_size(page_size)
    
    # Fetch all required metrics
    fetch_errors: list[str] = []
    http_data = []
    uptime_data = []
    ping_data = []
    tcp_data = []
    cert_data = []
    status_code_data = []
    
    # Determine uptime metric (single source of truth)
    uptime_metric = _get_uptime_metric(target_dict)
    
    # HTTP metrics
    if target_dict.get("enable_http_check"):
        http_data = _fetch_metric_data(
            tsdb_dir,
            target_id,
            "http_ms",
            from_date,
            to_date,
            limit=choose_points_limit(metric="http_ms", since=from_date, until=to_date, profile="pdf"),
            errors=fetch_errors,
        )
        if uptime_metric == "http_up":
            uptime_data = _fetch_metric_data(
                tsdb_dir,
                target_id,
                "http_up",
                from_date,
                to_date,
                limit=choose_points_limit(metric="http_up", since=from_date, until=to_date, profile="pdf"),
                errors=fetch_errors,
            )
        status_code_data = _fetch_metric_data(
            tsdb_dir,
            target_id,
            "http_status",
            from_date,
            to_date,
            limit=choose_points_limit(metric="http_status", since=from_date, until=to_date, profile="pdf"),
            errors=fetch_errors,
        )
    
    # Ping metrics
    if target_dict.get("enable_ping"):
        ping_data = _fetch_metric_data(
            tsdb_dir,
            target_id,
            "ping_ms",
            from_date,
            to_date,
            limit=choose_points_limit(metric="ping_ms", since=from_date, until=to_date, profile="pdf"),
            errors=fetch_errors,
        )
        if uptime_metric == "ping_up":
            uptime_data = _fetch_metric_data(
                tsdb_dir,
                target_id,
                "ping_up",
                from_date,
                to_date,
                limit=choose_points_limit(metric="ping_up", since=from_date, until=to_date, profile="pdf"),
                errors=fetch_errors,
            )
    
    # TCP metrics
    tcp_port_data = {}
    if target_dict.get("enable_tcp_connect"):
        tcp_data = _fetch_metric_data(
            tsdb_dir,
            target_id,
            "tcp_ms",
            from_date,
            to_date,
            limit=choose_points_limit(metric="tcp_ms", since=from_date, until=to_date, profile="pdf"),
            errors=fetch_errors,
        )
        if uptime_metric == "tcp_up":
            uptime_data = _fetch_metric_data(
                tsdb_dir,
                target_id,
                "tcp_up",
                from_date,
                to_date,
                limit=choose_points_limit(metric="tcp_up", since=from_date, until=to_date, profile="pdf"),
                errors=fetch_errors,
            )
        
        # Fetch per-port TCP data for multi-port chart
        tcp_ports_str = target_dict.get("tcp_ports") or "80"
        tcp_ports = _parse_tcp_ports(str(tcp_ports_str))
        if len(tcp_ports) > 1:
            for port in tcp_ports:
                port_data = _fetch_metric_data(
                    tsdb_dir,
                    target_id,
                    f"tcp_ms_{port}",
                    from_date,
                    to_date,
                    limit=choose_points_limit(metric="tcp_ms", since=from_date, until=to_date, profile="pdf"),
                    errors=fetch_errors,
                )
                if port_data:
                    tcp_port_data[port] = port_data
    
    # Certificate metrics
    if target_dict.get("enable_cert_expiration"):
        cert_data = _fetch_metric_data(
            tsdb_dir,
            target_id,
            "cert_days_left",
            from_date,
            to_date,
            limit=choose_points_limit(metric="cert_days_left", since=from_date, until=to_date, profile="pdf"),
            errors=fetch_errors,
        )

    if fetch_errors:
        _log.warning("Partial report for target %d; unavailable metrics: %s", target_id, ", ".join(fetch_errors))
    
    # Build report data
    report_data = ReportData(
        target=target_dict,
        from_date=from_date,
        to_date=to_date,
        http_data=http_data,
        uptime_data=uptime_data,
        ping_data=ping_data,
        tcp_data=tcp_data,
        cert_data=cert_data,
        status_code_data=status_code_data,
        tcp_port_data=tcp_port_data if tcp_port_data else None,
    )
    
    # Generate PDF (in threadpool to avoid blocking event loop)
    try:
        pdf_bytes = await run_in_threadpool(generate_pdf_report, report_data, page_size)
    except Exception:
        _log.exception("PDF generation failed for target %d", target_id)
        # Don't leak internal error details to client
        raise HTTPException(status_code=500, detail="PDF generation failed")
    
    # Generate filename (sanitize to alphanumeric + underscore/dash only)
    target_name = _safe_filename(str(target_dict.get("name", "report")), default="report")
    date_str = from_date.strftime("%Y-%m-%d")
    filename = _safe_filename(f"{target_name}_report_{date_str}.pdf")
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        }
    )


# ─── Async PDF Generation Endpoints ─────────────────────────────────────────


@router.post("/targets/{target_id}/pdf/request")
@limiter.limit(RATE_LIMIT_PDF)
async def request_pdf_report(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: Optional[str] = Query(None, description="Start date (ISO format)"),
    until: Optional[str] = Query(None, description="End date (ISO format)"),
    page_size: str = Query("a4", description="Page size: a4 or letter"),
):
    """
    Request async PDF generation.
    
    Returns a job_id that can be polled for status.
    The user can navigate away and the download will complete in background.
    """
    # Fetch target
    target = sqlite_db.get_target(conn, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    
    target_dict = dict(target)
    tsdb_dir = get_tsdb_dir(request)
    db_path = Path(request.app.state.db_path)
    
    # Parse and validate date range (raises HTTPException on invalid)
    from_date, to_date = _validate_report_range(since, until)
    
    # Validate page size (raises HTTPException on invalid)
    page_size = _validate_page_size(page_size)
    
    # Create job
    job_id = generate_job_id()
    sqlite_db.create_report_job(conn, job_id, target_id)
    
    # Submit to worker queue
    job_request = PDFJobRequest(
        job_id=job_id,
        target_id=target_id,
        target_dict=target_dict,
        from_date=from_date,
        to_date=to_date,
        profile="pdf",
        page_size=page_size,
        db_path=db_path,
        tsdb_dir=Path(tsdb_dir),
    )
    await submit_pdf_job(job_request)
    
    _log.info("PDF job %s created for target %d", job_id, target_id)
    
    return {
        "job_id": job_id,
        "status": "pending",
        "message": "PDF generation started. Poll /reports/jobs/{job_id} for status.",
    }


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
):
    """Get the status of a PDF generation job."""
    job_id = _validate_job_id(job_id)

    # NOTE: report_jobs schema currently has no user_id, so jobs are scoped
    # to authenticated instance users, not per-user ownership.
    job = sqlite_db.get_report_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Clamp progress to valid range (log if unexpected value)
    raw_progress = int(job["progress"] or 0)
    if raw_progress < 0 or raw_progress > 100:
        _log.warning("Job %s has unexpected progress value: %d", job_id, raw_progress)
    progress = max(0, min(100, raw_progress))
    
    result = {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": progress,
    }
    
    if job["status"] == "completed":
        result["filename"] = _safe_filename(job["filename"] or f"report_{job_id}.pdf")
        result["download_url"] = f"/reports/jobs/{job_id}/download"
    elif job["status"] == "failed":
        # Sanitize error: don't expose internal stack traces or implementation details
        raw_error = job["error"] or "Unknown error"
        # If error looks like a stack trace or internal message, genericize it
        if "\n" in raw_error or "Traceback" in raw_error or len(raw_error) > 200:
            result["error"] = "PDF generation failed"
        else:
            result["error"] = raw_error
    
    return result


@router.get("/jobs/{job_id}/download")
async def download_job_pdf(
    job_id: str,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
):
    """Download a completed PDF report."""
    job_id = _validate_job_id(job_id)

    # NOTE: report_jobs schema currently has no user_id, so jobs are scoped
    # to authenticated instance users, not per-user ownership.
    job = sqlite_db.get_report_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job not ready: status={job['status']}"
        )
    
    pdf_path = get_pdf_path(job_id)
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    
    filename = _safe_filename(job["filename"] or f"report_{job_id}.pdf")
    
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
    )
