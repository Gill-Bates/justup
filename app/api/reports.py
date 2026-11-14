#!/usr/bin/env python3
#
# app/api/reports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for report generation (sync and async)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..services.pdf_report import ReportData, generate_pdf_report
from ..services.pdf_worker import (
    PDF_DISABLED_ERROR,
    PDFJobRequest,
    generate_job_id,
    get_pdf_path,
    submit_pdf_job,
)
from ..utils.constants import VALID_PDF_PAGE_SIZES
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.rate_limit import RATE_LIMIT_PDF, rate_limit
from ..utils.report_metrics import collect_report_inputs, fetch_metric_data, report_filename
from ..utils.settings_cache import is_pdf_export_enabled
from ..utils.time import parse_utc, utcnow
from .auth import get_current_user

_log = logging.getLogger(__name__)

PDF_DISABLED_DETAIL = "PDF export is disabled"


def require_pdf_export(conn=Depends(get_conn), _user=Depends(get_current_user)) -> None:
    """Refuse every PDF route with 403 while Settings > General > PDF is switched off.

    Depends on the user so an anonymous caller still gets 401 and learns nothing about the setting.
    """
    if not is_pdf_export_enabled(conn):
        raise HTTPException(status_code=403, detail=PDF_DISABLED_DETAIL)


router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_pdf_export)])

# Job error texts written by this module that are safe to show to clients.
_QUEUE_FAILED_ERROR = "PDF job could not be queued"
_PUBLIC_JOB_ERRORS = frozenset({_QUEUE_FAILED_ERROR, PDF_DISABLED_ERROR})
ui_router = APIRouter(prefix="/ui/reports", tags=["reports"], dependencies=[Depends(require_pdf_export)])


MAX_REPORT_DAYS = 730

JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_job_id(job_id: str) -> str:
    """Validate job ID format at API boundary before DB/filesystem access."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    return job_id


def _get_authorized_job(conn, job_id: str, user):
    """Return the job for its owner or an admin; any other user gets a plain 404 (as for a missing job)."""
    job = sqlite_db.get_report_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    owner = job["user_id"]
    is_owner = owner is not None and int(owner) == int(user["id"])
    if not is_owner and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _safe_filename(name: str, *, default: str = "report.pdf") -> str:
    """Sanitize filename for Content-Disposition and filesystem safety."""
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(name or "")).strip("._")
    return sanitized[:255] or default


def _parse_datetime(value: str | None, param_name: str = "datetime") -> datetime | None:
    """
    Parse ISO datetime string to datetime object.

    Raises HTTPException if format is invalid (instead of silently returning None).
    """
    if not value:
        return None
    try:
        return parse_utc(value, strict=True)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format for '{param_name}': {value}") from None


async def _fetch_metric_data_async(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Run the blocking TSDB fetch in the threadpool to keep the event loop free."""
    return await run_in_threadpool(fetch_metric_data, *args, **kwargs)


def _validate_report_range(
    since_str: str | None,
    until_str: str | None,
) -> tuple[datetime, datetime]:
    """
    Parse and validate report date range.

    Returns (from_date, to_date) with defaults applied.
    Raises HTTPException on invalid input.
    """
    now = utcnow()
    from_date = _parse_datetime(since_str, "since")
    to_date = _parse_datetime(until_str, "until")

    # Default range is the current day.
    if not from_date:
        from_date = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=UTC)
    if not to_date:
        to_date = now

    if from_date >= to_date:
        raise HTTPException(status_code=400, detail="Invalid date range: 'since' must be before 'until'")

    if (to_date - from_date).total_seconds() > (MAX_REPORT_DAYS * 86400):
        raise HTTPException(status_code=400, detail=f"Maximum report range is {MAX_REPORT_DAYS} days")

    return from_date, to_date


def _validate_page_size(page_size: str) -> str:
    """Validate and normalize page size.

    Raises HTTPException if page_size is not a valid option.
    """
    normalized = page_size.lower()
    if normalized not in VALID_PDF_PAGE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid page_size '{page_size}'. Must be one of: {', '.join(sorted(VALID_PDF_PAGE_SIZES))}",
        )
    return normalized


@router.get("/targets/{target_id}/pdf", dependencies=[Depends(rate_limit(RATE_LIMIT_PDF))])
async def get_target_pdf_report(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    _user=Depends(get_current_user),
    since: str | None = Query(None, description="Start date (ISO format)"),
    until: str | None = Query(None, description="End date (ISO format)"),
    page_size: str = Query("a4", description="Page size: a4 or letter"),
):
    """
    Generate and download a PDF report for a target.

    Rate limited to 5 requests per minute to prevent abuse.
    Returns a PDF file with uptime statistics, charts, and downtime log.
    """
    target = sqlite_db.get_target(conn, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    target_dict = dict(target)
    tsdb_dir = get_tsdb_dir(request)

    from_date, to_date = _validate_report_range(since, until)

    page_size = _validate_page_size(page_size)

    fetch_errors: list[str] = []
    inputs = await run_in_threadpool(
        collect_report_inputs, tsdb_dir, target_id, target_dict, from_date, to_date, errors=fetch_errors
    )
    if fetch_errors:
        _log.warning("Partial report for target %d; unavailable metrics: %s", target_id, ", ".join(fetch_errors))

    report_data = ReportData(
        target=target_dict,
        from_date=from_date,
        to_date=to_date,
        **{**inputs, "tcp_port_data": inputs["tcp_port_data"] or None},
    )

    try:
        pdf_bytes = await run_in_threadpool(generate_pdf_report, report_data, page_size)
    except Exception:  # noqa: BLE001 - request boundary: log and return empty/error without leaking internals
        _log.exception("PDF generation failed for target %d", target_id)
        # Don't leak internal error details to client
        raise HTTPException(status_code=500, detail="PDF generation failed") from None

    filename = report_filename(target_dict.get("name"), from_date)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# ─── Async PDF Generation Endpoints ─────────────────────────────────────────


@router.post("/targets/{target_id}/pdf/request", dependencies=[Depends(rate_limit(RATE_LIMIT_PDF))])
async def request_pdf_report(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    user=Depends(get_current_user),
    since: str | None = Query(None, description="Start date (ISO format)"),
    until: str | None = Query(None, description="End date (ISO format)"),
    page_size: str = Query("a4", description="Page size: a4 or letter"),
):
    """
    Request async PDF generation.

    Returns a job_id that can be polled for status.
    The user can navigate away and the download will complete in background.
    """
    target = sqlite_db.get_target(conn, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    target_dict = dict(target)
    tsdb_dir = get_tsdb_dir(request)
    db_path = Path(request.app.state.db_path)

    from_date, to_date = _validate_report_range(since, until)

    page_size = _validate_page_size(page_size)

    job_id = generate_job_id()
    sqlite_db.create_report_job(conn, job_id, target_id, user_id=int(user["id"]))

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
    submitted = False
    try:
        await submit_pdf_job(job_request)
        submitted = True
    except BaseException:
        # queue.put is atomic: only a job that was never handed over is marked failed.
        if not submitted:
            sqlite_db.update_report_job(conn, job_id, status="failed", error=_QUEUE_FAILED_ERROR, completed=True)
        raise

    _log.info("PDF job %s created for target %d", job_id, target_id)

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "PDF generation started. Poll /reports/jobs/{job_id} for status.",
    }


@router.get("/jobs/{job_id}")
def get_job_status(
    job_id: str,
    conn=Depends(get_conn),
    user=Depends(get_current_user),
):
    """Get the status of a PDF generation job."""
    job_id = _validate_job_id(job_id)

    job = _get_authorized_job(conn, job_id, user)

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

    expires_at = job["expires_at"]
    if job["status"] == "completed" and expires_at is not None and expires_at <= utcnow():
        raise HTTPException(status_code=410, detail="Report has expired")

    if job["status"] == "completed":
        result["filename"] = _safe_filename(job["filename"] or f"report_{job_id}.pdf")
        result["download_url"] = f"/ui/reports/jobs/{job_id}/download"
    elif job["status"] == "failed":
        # Only messages this module wrote itself are public; worker exceptions stay in the server log.
        raw_error = job["error"]
        result["error"] = raw_error if raw_error in _PUBLIC_JOB_ERRORS else "PDF generation failed"
    return result


def _job_pdf_response(conn, job_id: str, user) -> FileResponse:
    """Build the attachment response for a finished job; owner/admin only (see _get_authorized_job)."""
    job_id = _validate_job_id(job_id)
    job = _get_authorized_job(conn, job_id, user)

    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job not ready: status={job['status']}")

    expires_at = job["expires_at"]
    if expires_at is not None and expires_at <= utcnow():
        raise HTTPException(status_code=410, detail="Report has expired")

    pdf_path = get_pdf_path(job_id)
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")

    filename = _safe_filename(job["filename"] or f"report_{job_id}.pdf")

    # Forced attachment keeps mobile browsers (iOS Safari) from opening an inline PDF viewer.
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/jobs/{job_id}/download")
def download_job_pdf(
    job_id: str,
    conn=Depends(get_conn),
    user=Depends(get_current_user),
):
    """Download a completed PDF report (Bearer auth, used by API clients)."""
    return _job_pdf_response(conn, job_id, user)


@ui_router.api_route("/jobs/{job_id}/download", methods=["GET", "HEAD"])
def download_job_pdf_navigation(
    job_id: str,
    conn=Depends(get_conn),
    user=Depends(get_current_user),
):
    """Attachment download for browser navigation: the /ui/ prefix allows the session cookie.

    GET/HEAD only and read-only, so CSRF protection is not needed. HEAD lets the UI
    check availability before navigating.
    """
    return _job_pdf_response(conn, job_id, user)
