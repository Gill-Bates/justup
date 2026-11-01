#!/usr/bin/env python3
#
# app/middleware/maintenance.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Reject new HTTP write requests while a restore is swapping persistent data."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..utils import maintenance

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def maintenance_unavailable_response() -> JSONResponse:
    """The 503 every maintenance-blocked request receives."""
    return JSONResponse(
        status_code=503,
        content={"detail": "The system is temporarily unavailable (maintenance in progress)."},
        headers={"Retry-After": "30"},
    )


class MaintenanceWriteMiddleware(BaseHTTPMiddleware):
    """Refuse new state-changing requests while maintenance is active."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if maintenance.is_active() and request.method.upper() not in _SAFE_METHODS:
            return maintenance_unavailable_response()
        return await call_next(request)
