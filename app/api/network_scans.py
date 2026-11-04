#!/usr/bin/env python3
#
# app/api/network_scans.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin-only Nmap discovery job API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.services.nmap_jobs import NmapScanJob, NmapScanRequest, cancel_job, create_job, get_job
from app.services.nmap_scanner import NmapScannerError
from app.utils.rate_limit import rate_limit, rate_limit_user

from .auth import require_admin

router = APIRouter(prefix="/network-scans", tags=["network-scans"])


@router.post(
    "",
    response_model=NmapScanJob,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("10/minute")), Depends(rate_limit_user("3/minute", require_admin))],
)
async def start_scan(payload: NmapScanRequest, admin=Depends(require_admin)) -> NmapScanJob:
    try:
        return await create_job(payload, int(admin["id"]))
    except NmapScannerError as exc:
        code = 429 if exc.error.code == "busy" else 422
        raise HTTPException(status_code=code, detail=exc.error.message) from exc


@router.get("/{scan_id}", response_model=NmapScanJob, dependencies=[Depends(rate_limit("60/minute"))])
def scan_status(scan_id: str, admin=Depends(require_admin)) -> NmapScanJob:
    job = get_job(scan_id, int(admin["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return job


@router.delete("/{scan_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(rate_limit("10/minute"))])
async def stop_scan(scan_id: str, admin=Depends(require_admin)) -> Response:
    if not await cancel_job(scan_id, int(admin["id"])):
        raise HTTPException(status_code=404, detail="Scan not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
