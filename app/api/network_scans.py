#!/usr/bin/env python3
#
# app/api/network_scans.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin-only Nmap discovery job API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field

from app.services.nmap_client import availability
from app.services.nmap_jobs import NmapScanJob, NmapScanRequest, cancel_job, create_job, get_job
from app.services.nmap_scanner import NmapScannerError
from app.utils.rate_limit import rate_limit, rate_limit_user

from .auth import require_admin

router = APIRouter(prefix="/network-scans", tags=["network-scans"])


class NmapAvailability(BaseModel):
    """Whether port discovery can start now; ``message`` explains what to do when it cannot."""

    available: bool
    state: str
    message: str = ""
    detail: str = ""


class ServiceDetectionChange(BaseModel):
    protocol: str
    port: int
    kind: str
    previous_name: str | None
    previous_product: str | None
    previous_version: str | None
    name: str | None
    product: str | None
    version: str | None


class ServiceDetectionPreview(BaseModel):
    token: str | None
    changes: list[ServiceDetectionChange]


class ServiceDetectionConfirmation(BaseModel):
    token: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


@router.get("/status", response_model=NmapAvailability, dependencies=[Depends(rate_limit("60/minute"))])
async def worker_availability(_admin=Depends(require_admin)) -> dict:
    return await availability()


@router.post(
    "/services/{target_id}/preview",
    response_model=ServiceDetectionPreview,
    dependencies=[Depends(rate_limit("5/minute")), Depends(rate_limit_user("2/minute", require_admin))],
)
async def preview_service_detection(
    request: Request, target_id: int = Path(gt=0), admin=Depends(require_admin)
) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Monitoring is unavailable")
    try:
        return await scheduler.preview_service_detection(target_id, int(admin["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NmapScannerError as exc:
        code = 429 if exc.error.code == "busy" else 503
        raise HTTPException(status_code=code, detail=exc.error.message) from exc


@router.post(
    "/services/{target_id}/confirm",
    response_model=list[ServiceDetectionChange],
    dependencies=[Depends(rate_limit("10/minute")), Depends(rate_limit_user("5/minute", require_admin))],
)
def confirm_service_detection(
    payload: ServiceDetectionConfirmation,
    request: Request,
    target_id: int = Path(gt=0),
    admin=Depends(require_admin),
) -> list[dict]:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Monitoring is unavailable")
    try:
        return scheduler.confirm_service_detection(target_id, int(admin["id"]), payload.token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "",
    response_model=NmapScanJob,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("10/minute")), Depends(rate_limit_user("3/minute", require_admin))],
)
async def start_scan(payload: NmapScanRequest, admin=Depends(require_admin)) -> NmapScanJob:
    current = await availability()
    if not current["available"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "unavailable", "message": current["message"]},
        )
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
