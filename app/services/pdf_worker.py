#!/usr/bin/env python3
#
# app/services/pdf_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Background worker for async PDF report generation."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import traceback
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

PROGRESS_PROCESSING = 10
PROGRESS_METRICS_FETCHED = 40
PROGRESS_PDF_RENDERED = 90
PROGRESS_DONE = 100


class PDFWorker:
    """Single-instance background PDF generation worker."""

    def __init__(self, *, storage_dir: Path, cpu_workers: int = 2, io_workers: int = 4) -> None:
        self.storage_dir = storage_dir
        # Note: storage_dir is created on-demand when first PDF is generated
        self.queue: asyncio.Queue[PDFJobRequest] = asyncio.Queue(maxsize=50)
        self.cpu_executor = ThreadPoolExecutor(max_workers=cpu_workers, thread_name_prefix="pdf_cpu")
        self.io_executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix="pdf_io")
        self.stop_event = asyncio.Event()

        _log.info(
            "PDF worker initialized: storage=%s, cpu_workers=%d, io_workers=%d",
            self.storage_dir,
            cpu_workers,
            io_workers,
        )

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Graceful shutdown: finish current work, then force-stop."""
        self.stop_event.set()
        # Give running jobs a chance to finish
        self.cpu_executor.shutdown(wait=True, cancel_futures=False)
        self.io_executor.shutdown(wait=True, cancel_futures=False)
        _log.info("PDF worker shutdown complete")

    def get_pdf_path(self, job_id: str) -> Path:
        return self.storage_dir / f"{job_id}.pdf"

    def cleanup_pdf_file(self, job_id: str) -> None:
        pdf_path = self.get_pdf_path(job_id)
        if not pdf_path.exists():
            return
        try:
            pdf_path.unlink()
            _log.debug("Cleaned up PDF file: %s", pdf_path)
        except OSError as e:
            _log.warning("Failed to cleanup PDF file %s: %s", pdf_path, e)

    async def submit(self, request: "PDFJobRequest") -> None:
        if self.queue.full():
            raise RuntimeError("PDF generation queue is full")
        await self.queue.put(request)
        _log.info("PDF job %s submitted for target %d", request.job_id, request.target_id)

    async def run_loop(self, db_connector: Callable, *, stop_event: Optional[asyncio.Event] = None) -> None:
        """Main worker loop that processes PDF generation jobs."""
        from ..db import sqlite as sqlite_db
        from ..db import tsdb as tsdb_db
        from .pdf_report import ReportData, generate_pdf_report
        from ..utils.metric_limits import choose_points_limit

        stop = stop_event or self.stop_event
        _log.info("PDF worker loop started")

        while not stop.is_set():
            job: Optional[PDFJobRequest] = None
            try:
                # Allow periodic stop checks even when idle
                job = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.exception("PDF worker loop error while waiting for job: %s", e)
                await asyncio.sleep(1)
                continue

            if job is None:
                continue
            
            # Explicit binding to avoid closure race conditions
            _job = job
            
            try:
                _log.info("Processing PDF job %s for target %d", _job.job_id, _job.target_id)

                # Basic sanity check (defensive against bad external context)
                if not isinstance(_job.target_dict, dict):
                    raise ValueError("target_dict must be a dict")

                with db_connector(_job.db_path) as conn:
                    sqlite_db.update_report_job(conn, _job.job_id, status="processing", progress=PROGRESS_PROCESSING)

                loop = asyncio.get_running_loop()

                # Explicit binding to avoid closure over _job variable
                def fetch_metrics(_j=_job) -> dict[str, list[dict]]:
                    http_data: list[dict] = []
                    uptime_data: list[dict] = []
                    ping_data: list[dict] = []
                    tcp_data: list[dict] = []
                    cert_data: list[dict] = []
                    status_code_data: list[dict] = []
                    tcp_port_data: dict[int, list[dict]] = {}

                    def _fetch(metric: str, limit: int = 5000) -> list[dict]:
                        try:
                            points = tsdb_db.query_auto_resolution(
                                _j.tsdb_dir,
                                target_id=_j.target_id,
                                metric=metric,
                                since=_j.from_date,
                                until=_j.to_date,
                                limit=limit,
                            )
                            return [{"ts": p.ts.isoformat(), "value": p.value} for p in points]
                        except Exception as e:
                            _log.warning("Failed to fetch metric %s: %s", metric, e)
                            return []

                    target = _j.target_dict
                    profile = _j.profile

                    # Uptime source priority: HTTP > Ping > TCP
                    if target.get("enable_http_check"):
                        http_data = _fetch(
                            "http_ms",
                            limit=choose_points_limit(metric="http_ms", since=_j.from_date, until=_j.to_date, profile=profile),
                        )
                        uptime_data = _fetch(
                            "http_up",
                            limit=choose_points_limit(metric="http_up", since=_j.from_date, until=_j.to_date, profile=profile),
                        )
                        status_code_data = _fetch(
                            "http_status",
                            limit=choose_points_limit(metric="http_status", since=_j.from_date, until=_j.to_date, profile=profile),
                        )

                    if target.get("enable_ping"):
                        ping_data = _fetch(
                            "ping_ms",
                            limit=choose_points_limit(metric="ping_ms", since=_j.from_date, until=_j.to_date, profile=profile),
                        )
                        if not uptime_data:
                            uptime_data = _fetch(
                                "ping_up",
                                limit=choose_points_limit(metric="ping_up", since=_j.from_date, until=_j.to_date, profile=profile),
                            )

                    if target.get("enable_tcp_connect"):
                        tcp_data = _fetch(
                            "tcp_ms",
                            limit=choose_points_limit(metric="tcp_ms", since=_j.from_date, until=_j.to_date, profile=profile),
                        )
                        if not uptime_data:
                            uptime_data = _fetch(
                                "tcp_up",
                                limit=choose_points_limit(metric="tcp_up", since=_j.from_date, until=_j.to_date, profile=profile),
                            )
                        
                        # Fetch per-port TCP data for multi-port chart
                        tcp_port_data = {}
                        tcp_ports_str = target.get("tcp_ports") or "80"
                        tcp_ports = [int(p.strip()) for p in str(tcp_ports_str).split(",") if p.strip().isdigit()]
                        if len(tcp_ports) > 1:
                            for port in tcp_ports:
                                port_data = _fetch(
                                    f"tcp_ms_{port}",
                                    limit=choose_points_limit(metric="tcp_ms", since=_j.from_date, until=_j.to_date, profile=profile),
                                )
                                if port_data:
                                    tcp_port_data[port] = port_data

                    if target.get("enable_cert_expiration"):
                        cert_data = _fetch(
                            "cert_days_left",
                            limit=choose_points_limit(metric="cert_days_left", since=_j.from_date, until=_j.to_date, profile=profile),
                        )

                    return {
                        "http_data": http_data,
                        "uptime_data": uptime_data,
                        "ping_data": ping_data,
                        "tcp_data": tcp_data,
                        "cert_data": cert_data,
                        "status_code_data": status_code_data,
                        "tcp_port_data": tcp_port_data or {},
                    }

                metrics = await loop.run_in_executor(self.io_executor, fetch_metrics)
                with db_connector(_job.db_path) as conn:
                    sqlite_db.update_report_job(conn, _job.job_id, progress=PROGRESS_METRICS_FETCHED)

                report_data = ReportData(
                    target=_job.target_dict,
                    from_date=_job.from_date,
                    to_date=_job.to_date,
                    **metrics,
                )

                # Explicit binding to avoid closure over _job and report_data
                def _generate(_j=_job, _data=report_data) -> bytes:
                    return generate_pdf_report(_data, _j.page_size)

                try:
                    pdf_bytes = await asyncio.wait_for(
                        loop.run_in_executor(self.cpu_executor, _generate),
                        timeout=120.0,  # 2 minutes max
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"PDF generation timed out after 120s for target {_job.target_id}"
                    )
                
                with db_connector(_job.db_path) as conn:
                    sqlite_db.update_report_job(conn, _job.job_id, progress=PROGRESS_PDF_RENDERED)

                pdf_path = self.get_pdf_path(_job.job_id)
                # Ensure storage directory exists (on-demand creation)
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                await loop.run_in_executor(self.io_executor, pdf_path.write_bytes, pdf_bytes)

                # Unicode-safe filename sanitization
                target_name = str(_job.target_dict.get("name", "report"))
                target_name = unicodedata.normalize("NFKD", target_name)
                target_name = target_name.encode("ascii", "ignore").decode()
                target_name = re.sub(r"[^a-zA-Z0-9_-]", "_", target_name)[:100]
                target_name = target_name.strip("_") or "report"
                date_str = _job.from_date.strftime("%Y-%m-%d")
                filename = f"{target_name}_report_{date_str}.pdf"

                with db_connector(_job.db_path) as conn:
                    sqlite_db.update_report_job(
                        conn,
                        _job.job_id,
                        status="completed",
                        progress=PROGRESS_DONE,
                        filename=filename,
                        completed=True,
                    )

                _log.info("PDF job %s completed: %s (%d bytes)", _job.job_id, filename, len(pdf_bytes))

            except Exception as e:
                _log.exception("PDF job %s failed", getattr(_job, "job_id", "?"))
                try:
                    # Preserve full error context with traceback
                    error_msg = traceback.format_exception_only(type(e), e)
                    error_detail = "".join(error_msg).strip()[:1000]
                    
                    with db_connector(_job.db_path) as conn:
                        sqlite_db.update_report_job(
                            conn,
                            _job.job_id,
                            status="failed",
                            progress=PROGRESS_DONE,  # Mark as complete even on failure
                            error=error_detail,
                            completed=True,
                        )
                except Exception:
                    # Ensure worker loop stays alive even if DB update fails
                    _log.exception("Failed to update failed status for PDF job %s", getattr(_job, "job_id", "?"))
            finally:
                self.queue.task_done()

        _log.info("PDF worker loop stopped")

    async def cleanup_loop(self, db_connector: Callable, db_path: Path, *, stop_event: Optional[asyncio.Event] = None) -> None:
        """Periodic cleanup of expired jobs and their PDF files."""
        from ..db import sqlite as sqlite_db
        from ..utils.time import utcnow

        stop = stop_event or self.stop_event

        while not stop.is_set():
            try:
                # Run immediately, then every 5 minutes
                with db_connector(db_path) as conn:
                    now = utcnow()
                    # Use DAL instead of direct SQL
                    expired_ids = sqlite_db.get_expired_job_ids(conn, now)

                    for job_id in expired_ids:
                        self.cleanup_pdf_file(job_id)

                    deleted = sqlite_db.prune_expired_report_jobs(conn)
                    if deleted > 0:
                        _log.info("Cleaned up %d expired PDF jobs", deleted)

                # Wait (interruptible)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.warning("Cleanup task error: %s", e)
                await asyncio.sleep(1)


@dataclass
class PDFJobRequest:
    """Request to generate a PDF report."""
    job_id: str
    target_id: int
    target_dict: dict[str, Any]
    from_date: datetime
    to_date: datetime
    page_size: str
    db_path: Path
    tsdb_dir: Path
    profile: str = "pdf"


_worker: Optional[PDFWorker] = None
_worker_lock = threading.Lock()


def init_pdf_worker(data_dir: Path, max_workers: int = 2) -> None:
    """Initialize the PDF worker singleton (backward-compatible wrapper)."""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.shutdown()
        # Keep signature stable: max_workers maps to CPU workers.
        _worker = PDFWorker(storage_dir=data_dir / "pdf_reports", cpu_workers=max_workers)


def shutdown_pdf_worker() -> None:
    """Shutdown the PDF worker singleton."""
    global _worker
    if _worker is None:
        return
    _worker.shutdown()
    _worker = None


def generate_job_id() -> str:
    """Generate a unique job ID."""
    return uuid.uuid4().hex[:16]


def get_pdf_path(job_id: str) -> Optional[Path]:
    """Get the path to a generated PDF file."""
    if _worker is None:
        return None
    return _worker.get_pdf_path(job_id)


def cleanup_pdf_file(job_id: str) -> None:
    """Remove a PDF file after download or expiration."""
    if _worker is None:
        return
    _worker.cleanup_pdf_file(job_id)


async def submit_pdf_job(request: PDFJobRequest) -> None:
    """Submit a PDF generation job to the queue."""
    if _worker is None:
        raise RuntimeError("PDF worker not initialized")
    await _worker.submit(request)


async def run_pdf_worker_loop(db_connector: Callable, stop_event: Optional[asyncio.Event] = None) -> None:
    """Backward-compatible wrapper around the worker run loop."""
    if _worker is None:
        raise RuntimeError("PDF worker not initialized")
    await _worker.run_loop(db_connector, stop_event=stop_event)


async def cleanup_expired_jobs(db_connector: Callable, db_path: Path, stop_event: Optional[asyncio.Event] = None) -> None:
    """Backward-compatible wrapper around the worker cleanup loop."""
    if _worker is None:
        raise RuntimeError("PDF worker not initialized")
    await _worker.cleanup_loop(db_connector, db_path, stop_event=stop_event)
