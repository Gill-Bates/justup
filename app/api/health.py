#!/usr/bin/env python3
#
# app/api/health.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Health and meta endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse

from ..utils.version import APP_NAME, VERSION

router = APIRouter(tags=["health"])


@router.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
	"""Redirect legacy favicon.ico requests to the SVG favicon."""
	return RedirectResponse(
		"/static/favicon.svg", 
		status_code=301,
		headers={"Cache-Control": "public, max-age=86400"}
	)

@router.get("/health")
@router.get("/healthz")
def health():
	"""Liveness probe for container healthchecks.
	
	Both /health and /healthz are supported for broad compatibility with
	different monitoring systems (Docker, Kubernetes, CDNs, etc.).
	
	Note: This is a liveness probe, not a readiness probe. It does NOT check
	database connectivity or external services by design - those failures
	should not cause container restarts.
	"""
	return JSONResponse(
		content={"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
	)


@router.get("/api/meta")
def meta():
	"""Application metadata (version, etc.) for frontend consumption."""
	return {
		"version": VERSION,
		"name": APP_NAME,
	}
