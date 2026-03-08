#!/usr/bin/env python3
#
# app/middleware/csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF protection middleware for UI routes."""

from __future__ import annotations

import posixpath
import secrets
from typing import Callable
from urllib.parse import urlparse

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
UI_PREFIXES = ("/ui/", "/login")


class CSRFMiddleware(BaseHTTPMiddleware):

	def __init__(self, app: ASGIApp):
		super().__init__(app)

	def _requires_csrf(self, path: str) -> bool:
		normalized = posixpath.normpath(path).lower()
		for prefix in UI_PREFIXES:
			norm_prefix = posixpath.normpath(prefix).lower()
			if normalized.startswith(norm_prefix + "/") or normalized == norm_prefix:
				return True
		return False

	def _check_origin(self, request: Request) -> bool:
		origin = request.headers.get("Origin")
		if not origin:
			return True
		try:
			origin_parsed = urlparse(origin)
			req_host = (request.url.hostname or "").lower()
			origin_host = (origin_parsed.hostname or "").lower()
			if not origin_host:
				return False
			return origin_host == req_host
		except Exception:
			return False

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		csrf_token = request.cookies.get("csrf_token")
		new_token = False

		if request.url.path == "/login" and request.method in SAFE_METHODS:
			csrf_token = secrets.token_urlsafe(32)
			new_token = True
		if not csrf_token:
			csrf_token = secrets.token_urlsafe(32)
			new_token = True

		request.state.csrf_token = csrf_token

		if request.method not in SAFE_METHODS:
			path = request.url.path
			if path.startswith("/api/") or path.startswith("/ui/"):
				header_token = request.headers.get("X-CSRF-Token", "")
				if not secrets.compare_digest(header_token, csrf_token):
					return JSONResponse(
						status_code=403,
						content={"detail": "CSRF token mismatch"},
					)
				if not self._check_origin(request):
					return JSONResponse(
						status_code=403,
						content={"detail": "Origin mismatch"},
					)

		response = await call_next(request)

		if new_token:
			response.set_cookie(
				key="csrf_token",
				value=csrf_token,
				httponly=False,
				samesite="strict",
				secure=request.url.scheme == "https",
				path="/",
			)

		return response
