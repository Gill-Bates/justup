#!/usr/bin/env python3
#
# app/services/pdf_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Background worker for async PDF report generation."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import os
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils import maintenance
from ..utils.report_metrics import collect_report_inputs, report_filename

_log = logging.getLogger(__name__)

PROGRESS_PROCESSING = 10
PROGRESS_METRICS_FETCHED = 40
PROGRESS_PDF_RENDERED = 90
PROGRESS_DONE = 100
PDF_DISABLED_ERROR = "PDF export is disabled"  # public job error: shown to the client as written

# Rough peak memory of one concurrent PDF render; used to cap CPU workers on small hosts.
_MEM_PER_CPU_WORKER = 256 * 1024 * 1024
# Share of available memory the PDF worker pools may plan for.
_MEM_BUDGET_FRACTION = 0.25
_MAX_IO_WORKERS = 16


def _read_int(path: str) -> int | None:
    """Read a single integer from a (cgroup/proc) file; None if absent or unlimited."""
    try:
        raw = Path(path).read_text().strip()
        return int(raw) if raw.isdigit() else None
    except (OSError, ValueError):
        return None


def _effective_cores() -> int:
    """CPU cores usable by this process (affinity and cgroup quota aware)."""
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1

    quota: float | None = None
    try:  # cgroup v2: "<max|quota> <period>"
        parts = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if len(parts) == 2 and parts[0] != "max":
            quota = int(parts[0]) / int(parts[1])
    except (OSError, ValueError, ZeroDivisionError):
        pass
    if quota is None:  # cgroup v1
        q = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and period:
            quota = q / period

    if quota is not None and quota > 0:
        cores = min(cores, max(1, math.ceil(quota)))
    return max(1, cores)


def _available_memory() -> int | None:
    """Available memory in bytes (cgroup limit if set, else MemAvailable); None if unknown."""
    candidates: list[int] = []
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit and limit < 1 << 60:  # v1 reports a huge number for "unlimited"
        candidates.append(limit)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    return min(candidates) if candidates else None


def compute_worker_counts() -> tuple[int, int]:
    """Derive (cpu_workers, io_workers) from host cores and memory.

    CPU workers: half the cores, capped only by a memory budget. I/O workers: 2x cores.
    """
    cores = _effective_cores()
    cpu = max(1, cores // 2)

    mem = _available_memory()
    if mem is not None:
        cpu = min(cpu, max(1, int(mem * _MEM_BUDGET_FRACTION) // _MEM_PER_CPU_WORKER))

    io = min(max(2, cores * 2), _MAX_IO_WORKERS)

    return cpu, io


@contextlib.contextmanager
def _db_connection(db_connector: Callable, db_path: Path):
    """Close and unregister a connection after each worker operation."""
    from ..db import sqlite as sqlite_db

    conn = db_connector(db_path)
    try:
        with conn:
            yield conn
    finally:
        sqlite_db.close_connection(conn)


def _export_enabled(db_connector: Callable, db_path: Path) -> bool:
    """Read the PDF export switch on a short-lived connection (blocking; call it off the event loop)."""
    from ..utils.settings_cache import is_pdf_export_enabled

    with _db_connection(db_connector, db_path) as conn:
        return is_pdf_export_enabled(conn)


def _update_job_status(db_connector: Callable, db_path: Path, job_id: str, **fields) -> None:
    """Write one job-status update on a short-lived connection (blocking; call it off the event loop)."""
    from ..db import sqlite as sqlite_db

    with _db_connection(db_connector, db_path) as conn:
        sqlite_db.update_report_job(conn, job_id, **fields)


def _run_in_maintenance_section(generation: int, callback: Callable, *args, **kwargs):
    """Keep maintenance registered in the executor thread for its full callback lifetime."""
    with maintenance.work_section(generation):
        return callback(*args, **kwargs)


class PDFWorker:
    """Single-instance background PDF generation worker."""

    def __init__(self, *, storage_dir: Path, cpu_workers: int | None = None, io_workers: int | None = None) -> None:
        auto_cpu, auto_io = compute_worker_counts()
        cpu_workers = cpu_workers or auto_cpu
        io_workers = io_workers or auto_io
        self.storage_dir = storage_dir
        # Note: storage_dir is created on-demand when first PDF is generated
        self.queue: asyncio.Queue[PDFJobRequest] = asyncio.Queue(maxsize=50)
        self.cpu_executor = ThreadPoolExecutor(max_workers=cpu_workers, thread_name_prefix="pdf_cpu")
        self.io_executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix="pdf_io")
        self.stop_event = asyncio.Event()

        _log.info(
            "PDF worker initialized: storage=%s, cpu_workers=%d, io_workers=%d (host: %d cores, %s MiB available)",
            self.storage_dir,
            cpu_workers,
            io_workers,
            _effective_cores(),
            (_available_memory() or 0) // (1024 * 1024) or "?",
        )

    def shutdown(self) -> None:
        """Signal the loops to stop and wait for running jobs to finish."""
        self.stop_event.set()
        self.cpu_executor.shutdown(wait=True, cancel_futures=False)
        self.io_executor.shutdown(wait=True, cancel_futures=False)
        _log.info("PDF worker shutdown complete")

    def get_pdf_path(self, job_id: str) -> Path:
        return self.storage_dir / f"{job_id}.pdf"

    def _atomic_write_bytes(self, pdf_path: Path, data: bytes) -> None:
        # Temp file beside the target so the rename is an atomic same-filesystem replace.
        fd, tmp_name = tempfile.mkstemp(dir=str(pdf_path.parent), prefix=pdf_path.stem + ".", suffix=".pdf.tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(pdf_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)  # never leave a stray .tmp on failure
            raise

    def cleanup_pdf_file(self, job_id: str) -> None:
        pdf_path = self.get_pdf_path(job_id)
        if not pdf_path.exists():
            return
        try:
            pdf_path.unlink()
            _log.debug("Cleaned up PDF file: %s", pdf_path)
        except OSError as e:
            _log.warning("Failed to cleanup PDF file %s: %s", pdf_path, e)

    def sweep_orphan_pdfs(self, conn, *, min_age_seconds: int = 3600) -> int:
        """Delete PDF files whose report job row is gone (e.g. cascade after target deletion)."""
        from ..db import sqlite as sqlite_db

        if not self.storage_dir.is_dir():
            return 0
        cutoff = time.time() - min_age_seconds
        removed = 0
        for pdf_path in self.storage_dir.glob("*.pdf"):
            try:
                if pdf_path.stat().st_mtime > cutoff or sqlite_db.get_report_job(conn, pdf_path.stem) is not None:
                    continue
                pdf_path.unlink()
                removed += 1
            except OSError as e:
                _log.warning("Failed to sweep orphan PDF %s: %s", pdf_path, e)
        return removed

    async def submit(self, request: PDFJobRequest) -> None:
        if self.queue.full():
            raise RuntimeError("PDF generation queue is full")
        await self.queue.put(request)
        _log.info("PDF job %s submitted for target %d", request.job_id, request.target_id)

    async def run_loop(self, db_connector: Callable, *, stop_event: asyncio.Event | None = None) -> None:
        """Main worker loop that processes PDF generation jobs."""
        from .pdf_report import ReportData, generate_pdf_report

        stop = stop_event or self.stop_event
        _log.info("PDF worker loop started")

        while not stop.is_set():
            job: PDFJobRequest | None = None
            try:
                # Allow periodic stop checks even when idle
                job = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - worker-loop resilience: log and keep the loop alive on any job failure
                _log.exception("PDF worker loop error while waiting for job: %s", e)
                await asyncio.sleep(1)
                continue

            if job is None:
                continue

            # Explicit binding to avoid closure race conditions
            _job = job

            # A job writes SQLite, TSDB reads and the report file: hold it back during maintenance
            section = contextlib.ExitStack()
            if not await self._enter_work_section(section, stop):
                self.queue.task_done()  # shutting down while held back
                continue

            loop = asyncio.get_running_loop()
            try:
                _log.info("Processing PDF job %s for target %d", _job.job_id, _job.target_id)

                # Basic sanity check (defensive against bad external context)
                if not isinstance(_job.target_dict, dict):
                    raise ValueError("target_dict must be a dict")

                async def _set_status(_j=_job, _loop=loop, **fields) -> None:
                    await _loop.run_in_executor(
                        self.io_executor,
                        functools.partial(_update_job_status, db_connector, _j.db_path, _j.job_id, **fields),
                    )

                # A job queued before the switch went off is not rendered; the row stays, marked failed.
                if not await loop.run_in_executor(self.io_executor, _export_enabled, db_connector, _job.db_path):
                    _log.info("PDF job %s skipped: PDF export is disabled", _job.job_id)
                    await _set_status(status="failed", progress=PROGRESS_DONE, error=PDF_DISABLED_ERROR, completed=True)
                    continue

                await _set_status(status="processing", progress=PROGRESS_PROCESSING)

                # Default args bind _job now; a plain closure would see a later loop value.
                def fetch_metrics(_j=_job) -> dict[str, Any]:
                    return collect_report_inputs(
                        _j.tsdb_dir, _j.target_id, _j.target_dict, _j.from_date, _j.to_date, profile=_j.profile
                    )

                generation = maintenance.current_generation()
                metrics = await loop.run_in_executor(
                    self.io_executor,
                    _run_in_maintenance_section,
                    generation,
                    fetch_metrics,
                )
                await _set_status(progress=PROGRESS_METRICS_FETCHED)

                report_data = ReportData(
                    target=_job.target_dict,
                    from_date=_job.from_date,
                    to_date=_job.to_date,
                    **metrics,
                )

                def _generate(_j=_job, _data=report_data) -> bytes:
                    return generate_pdf_report(_data, _j.page_size)

                try:
                    pdf_bytes = await asyncio.wait_for(
                        loop.run_in_executor(
                            self.cpu_executor,
                            _run_in_maintenance_section,
                            generation,
                            _generate,
                        ),
                        timeout=120.0,
                    )
                except TimeoutError:
                    # wait_for only stops waiting: the render thread (and the matplotlib lock) keeps
                    # running until it finishes; concurrency is bounded by the cpu_executor size.
                    _log.warning("PDF job %s timed out after 120s; render thread may still be running", _job.job_id)
                    raise RuntimeError(f"PDF generation timed out after 120s for target {_job.target_id}") from None

                await _set_status(progress=PROGRESS_PDF_RENDERED)

                pdf_path = self.get_pdf_path(_job.job_id)
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                await loop.run_in_executor(
                    self.io_executor,
                    _run_in_maintenance_section,
                    generation,
                    self._atomic_write_bytes,
                    pdf_path,
                    pdf_bytes,
                )

                filename = report_filename(_job.target_dict.get("name"), _job.from_date)

                await _set_status(status="completed", progress=PROGRESS_DONE, filename=filename, completed=True)

                _log.info("PDF job %s completed: %s (%d bytes)", _job.job_id, filename, len(pdf_bytes))

            except Exception as e:  # noqa: BLE001 - worker-loop resilience: log and keep the loop alive on any job failure
                _log.exception("PDF job %s failed", getattr(_job, "job_id", "?"))
                try:
                    # Preserve full error context with traceback
                    error_msg = traceback.format_exception_only(type(e), e)
                    error_detail = "".join(error_msg).strip()[:1000]

                    await loop.run_in_executor(
                        self.io_executor,
                        functools.partial(
                            _update_job_status,
                            db_connector,
                            _job.db_path,
                            _job.job_id,
                            status="failed",
                            progress=PROGRESS_DONE,  # Mark as complete even on failure
                            error=error_detail,
                            completed=True,
                        ),
                    )
                except Exception:  # noqa: BLE001 - worker-loop resilience: log and keep the loop alive on any job failure
                    # Ensure worker loop stays alive even if DB update fails
                    _log.exception("Failed to update failed status for PDF job %s", getattr(_job, "job_id", "?"))
            finally:
                section.close()
                self.queue.task_done()

        _log.info("PDF worker loop stopped")

    @staticmethod
    async def _enter_work_section(section: contextlib.ExitStack, stop: asyncio.Event) -> bool:
        """Enter a maintenance work section, waiting while maintenance is active; False once stopped."""
        while not stop.is_set():
            try:
                section.enter_context(maintenance.work_section())
            except maintenance.MaintenanceActiveError:
                await asyncio.sleep(0.5)
                continue
            return True
        return False

    async def cleanup_loop(
        self,
        db_connector: Callable,
        db_path: Path,
        *,
        stop_event: asyncio.Event | None = None,
        on_failure: Callable[[str], None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        """Periodic cleanup of expired jobs and their PDF files.

        ``on_failure`` (exception class name) and ``on_success`` report each cycle's outcome to the supervisor.
        """
        from ..db import sqlite as sqlite_db
        from ..utils.time import utcnow

        stop = stop_event or self.stop_event

        while not stop.is_set():
            try:
                # Run immediately, then every 5 minutes; deletes PDF files, so not during maintenance
                with maintenance.work_section(), _db_connection(db_connector, db_path) as conn:
                    now = utcnow()
                    expired_ids = sqlite_db.get_expired_job_ids(conn, now)

                    for job_id in expired_ids:
                        self.cleanup_pdf_file(job_id)

                    deleted = sqlite_db.prune_expired_report_jobs(conn)
                    if deleted > 0:
                        _log.info("Cleaned up %d expired PDF jobs", deleted)
                    orphans = self.sweep_orphan_pdfs(conn)
                    if orphans > 0:
                        _log.info("Removed %d orphaned PDF file(s)", orphans)
                if on_success is not None:
                    on_success()

                # Wait until the next cycle, interruptible by the stop event.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=300)
            except asyncio.CancelledError:
                break
            except maintenance.MaintenanceActiveError:
                await asyncio.sleep(5)
            except Exception as e:  # noqa: BLE001 - worker-loop resilience: log and keep the loop alive on any job failure
                _log.warning("Cleanup task error: %s", e)
                if on_failure is not None:
                    on_failure(type(e).__name__)
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


_worker: PDFWorker | None = None
_worker_lock = threading.Lock()


def init_pdf_worker(data_dir: Path, max_workers: int | None = None) -> None:
    """Initialize the PDF worker singleton; pool sizes are auto-derived from the host unless given."""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.shutdown()
        # max_workers maps to CPU workers; None = auto.
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


def get_pdf_path(job_id: str) -> Path | None:
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


async def run_pdf_worker_loop(db_connector: Callable, stop_event: asyncio.Event | None = None) -> None:
    """Backward-compatible wrapper around the worker run loop."""
    if _worker is None:
        raise RuntimeError("PDF worker not initialized")
    await _worker.run_loop(db_connector, stop_event=stop_event)


async def cleanup_expired_jobs(
    db_connector: Callable,
    db_path: Path,
    stop_event: asyncio.Event | None = None,
    *,
    on_failure: Callable[[str], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Backward-compatible wrapper around the worker cleanup loop."""
    if _worker is None:
        raise RuntimeError("PDF worker not initialized")
    await _worker.cleanup_loop(
        db_connector, db_path, stop_event=stop_event, on_failure=on_failure, on_success=on_success
    )
