#!/usr/bin/env python3
#
# app/services/nmap_jobs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Owner-bound, bounded discovery jobs for the Nmap worker."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.targets import validate_host
from app.services.nmap_client import request_scan
from app.services.nmap_scanner import NmapScanError, NmapScannerError, NmapScanResult
from app.utils.ssrf import resolve_validated
from app.utils.time import utcnow

_log = logging.getLogger(__name__)
MAX_ACTIVE_JOBS = 16
MAX_ACTIVE_JOBS_PER_OWNER = 4
JOB_TTL = timedelta(minutes=15)
EXPIRED_RETENTION = timedelta(minutes=5)


class NmapScanRequest(BaseModel):
    """Host and protocols only: ports, ranges and Nmap options are never accepted from clients."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    protocols: set[Literal["tcp", "udp"]] = Field(min_length=1, max_length=2)
    profile: Literal["quick", "extended"] = "quick"

    @field_validator("host")
    @classmethod
    def validate_target_host(cls, value: str) -> str:
        try:
            host = validate_host(value)
        except ValueError as exc:
            raise ValueError("Invalid target host") from exc
        if not host:
            raise ValueError("Target host is required")
        return host


class NmapScanJob(BaseModel):
    id: str
    host: str
    protocols: list[Literal["tcp", "udp"]]
    profile: Literal["quick", "extended"]
    status: Literal["queued", "running", "completed", "failed", "cancelled", "expired"]
    created_at: datetime
    expires_at: datetime
    result: NmapScanResult | None = None
    error: NmapScanError | None = None


@dataclass
class _Record:
    job: NmapScanJob
    owner_id: int
    address: str
    task: asyncio.Task[None] | None = None


_jobs: dict[str, _Record] = {}
_active_addresses: dict[str, str] = {}  # address -> id of the job that reserved it


def _release(record: _Record) -> None:
    # Only the owning job may release: a pruned job unwinds late, after a successor took the address.
    if _active_addresses.get(record.address) == record.job.id:
        del _active_addresses[record.address]


def _prune() -> None:
    now = utcnow()
    for job_id, record in list(_jobs.items()):
        if record.job.expires_at > now:
            continue
        if record.job.status != "expired":
            if record.job.status in {"queued", "running"} and record.task:
                record.task.cancel()
            record.job.status = "expired"
            record.job.result = None
            _release(record)
        if now >= record.job.expires_at + EXPIRED_RETENTION:
            del _jobs[job_id]


def get_job(job_id: str, owner_id: int) -> NmapScanJob | None:
    _prune()
    record = _jobs.get(job_id)
    if record is None or record.owner_id != owner_id:
        return None
    return record.job.model_copy(deep=True)


def completed_job(job_id: str, owner_id: int) -> NmapScanJob | None:
    """Expose only an owner's completed discovery to target persistence."""
    job = get_job(job_id, owner_id)
    return job if job is not None and job.status == "completed" else None


async def create_job(request: NmapScanRequest, owner_id: int) -> NmapScanJob:
    _prune()
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(resolve_validated, request.host, allow_private=True), timeout=5
        )
    except TimeoutError as exc:
        raise NmapScannerError("invalid_host", "Target host could not be resolved in time") from exc
    if not addresses:
        raise NmapScannerError("invalid_host", "Target host has no permitted address")
    address = str(addresses[0])
    if address in _active_addresses:
        raise NmapScannerError("busy", "A scan is already running for this target")
    active = [record for record in _jobs.values() if record.job.status in {"queued", "running"}]
    if len(active) >= MAX_ACTIVE_JOBS:
        raise NmapScannerError("busy", "The scan queue is full")
    if sum(record.owner_id == owner_id for record in active) >= MAX_ACTIVE_JOBS_PER_OWNER:
        raise NmapScannerError("busy", "Too many scans are already running for this user")
    now = utcnow()
    job = NmapScanJob(
        id=str(uuid.uuid4()),
        host=request.host,
        protocols=sorted(request.protocols),
        profile=request.profile,
        status="queued",
        created_at=now,
        expires_at=now + JOB_TTL,
    )
    record = _Record(job=job, owner_id=owner_id, address=address)
    _jobs[job.id] = record
    _active_addresses[address] = job.id
    record.task = asyncio.create_task(_run(record))
    return job.model_copy(deep=True)


async def _run(record: _Record) -> None:
    job = record.job
    try:
        if job.status != "queued":
            return
        job.status = "running"
        result = await request_scan(
            record.address,
            protocols=set(job.protocols),
            profile=job.profile,
            timeout=90 if job.profile == "quick" else 180,
        )
        if job.status == "running":
            job.result = result
            job.status = "completed"
    except asyncio.CancelledError:
        if job.status in {"queued", "running"}:
            job.status = "cancelled"
        raise
    except NmapScannerError as exc:
        if job.status == "running":
            job.error = exc.error
            job.status = "failed"
    except Exception:  # noqa: BLE001 - background job boundary converts unexpected failures to a safe status
        _log.exception("Nmap discovery job failed")
        if job.status == "running":
            job.error = NmapScanError(code="process_failed", message="Nmap discovery failed")
            job.status = "failed"
    finally:
        _release(record)


async def cancel_job(job_id: str, owner_id: int) -> bool:
    _prune()
    record = _jobs.get(job_id)
    if record is None or record.owner_id != owner_id:
        return False
    if record.job.status in {"queued", "running"} and record.task:
        record.job.status = "cancelled"
        record.task.cancel()
        with suppress(asyncio.CancelledError):
            await record.task
        _release(record)
    return True


async def shutdown_jobs() -> None:
    tasks = [record.task for record in _jobs.values() if record.task and not record.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _jobs.clear()
    _active_addresses.clear()
