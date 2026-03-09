#!/usr/bin/env python3
#
# app/middleware/csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF protection middleware for UI routes."""

from __future__ import annotations

import secrets
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# UI path prefixes that require CSRF protection
# Includes /login to prevent Login-CSRF attacks (OWASP)
UI_PREFIXES = ("/ui/", "/login")


class CSRFMiddleware(BaseHTTPMiddleware):
	"""
	Double-Submit-Cookie CSRF Protection.
	
	1. Generates a random token and sets it as a cookie (HttpOnly=False).
	2. On state-changing methods (POST, etc.), validates that the
	   header 'X-CSRF-Token' matches the cookie.
	3. Optional: Origin header check for additional hardening.
	"""

	def __init__(self, app: ASGIApp):
		super().__init__(app)

	def _requires_csrf(self, path: str) -> bool:
		"""Check if path requires CSRF protection."""
		# Normalize path to prevent bypass via //ui/, /UI/, /ui/../ui/, etc.
		import posixpath
		normalized = posixpath.normpath(path).lower()
		
		for prefix in UI_PREFIXES:
			norm_prefix = posixpath.normpath(prefix).lower()
			# Match exact path or subpaths (e.g., /ui matches /ui/settings)
			if normalized.startswith(norm_prefix + "/") or normalized == norm_prefix:
				return True
		return False

	def _check_origin(self, request: Request) -> bool:
		"""
		Validate Origin header matches Host (hardened against spoofing).
		
		Returns True if valid or no Origin header present.
		Note: Only uses Host header (not X-Forwarded-Host) to prevent
		client-side spoofing. Trust X-Forwarded-Host only if set by
		a verified reverse proxy with proper firewall rules.
		"""
		origin = request.headers.get("Origin")
		if not origin:
			return True  # No Origin header = not a cross-origin request
		
		# Use only Host header (set by server, not spoofable by client)
		# For reverse proxy setups, ensure nginx/traefik sets Host correctly
		host = request.headers.get("Host", "")
		
		try:
			from urllib.parse import urlparse
			origin_host = urlparse(origin).netloc
			# Remove port from both for comparison
			host_no_port = host.split(":")[0]
			origin_no_port = origin_host.split(":")[0]
			return host_no_port == origin_no_port
		except Exception:
			return False

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		# 1. Get token from cookie, or generate new one
		csrf_token = request.cookies.get("csrf_token")
		new_token = False
		if not csrf_token:
			csrf_token = secrets.token_urlsafe(32)
			new_token = True

		# 2. Attach to request state for templates/views to use
		request.state.csrf_token = csrf_token

		# 3. Validation for unsafe methods on protected paths
		if request.method not in SAFE_METHODS and self._requires_csrf(request.url.path):
			# Origin check (hardening against cross-origin attacks)
			if not self._check_origin(request):
				return JSONResponse(
					content={"detail": "Cross-origin request blocked"},
					status_code=403
				)
			
			# CSRF token validation (constant-time comparison)
			submitted_token = request.headers.get("X-CSRF-Token")
			if not submitted_token or not secrets.compare_digest(submitted_token, csrf_token):
				return JSONResponse(
					content={"detail": "CSRF Token mismatch or missing"},
					status_code=403
				)
			
			# Token rotation after successful validation (prevents replay attacks)
			csrf_token = secrets.token_urlsafe(32)
			new_token = True
			request.state.csrf_token = csrf_token

		response = await call_next(request)

		# 4. Set cookie if new (with secure flag based on scheme)
		if new_token:
			# HttpOnly=False so JS/HTMX can read it to set the header
			# Secure=True for HTTPS (check X-Forwarded-Proto for reverse proxies)
			# max_age=3600 limits token lifetime to 1 hour
			forwarded_proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
			is_secure = forwarded_proto == "https"
			response.set_cookie(
				"csrf_token",
				csrf_token,
				max_age=3600,
				httponly=False,
				samesite="lax",
				secure=is_secure
			)

		return response
