#!/usr/bin/env python3
#
# app/api/system.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""System status and health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from .response import ok_response

router = APIRouter(tags=["system"])


@router.get("/status")
def get_system_status():
	"""Return system status for health checks."""
	return ok_response(data={"status": "ok", "service": "justUp"})
