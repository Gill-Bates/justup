#!/usr/bin/env python3
#
# app/middleware/security_headers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Security headers middleware for clickjacking protection and other security headers."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
	"""Add security headers that cannot be set via meta tags."""

	async def dispatch(self, request: Request, call_next) -> Response:
		response = await call_next(request)

		# X-Frame-Options: Prevent clickjacking (legacy but still useful)
		response.headers["X-Frame-Options"] = "DENY"

		# X-Content-Type-Options: Prevent MIME-sniffing
		response.headers["X-Content-Type-Options"] = "nosniff"

		# Referrer-Policy: Control referrer leakage
		response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

		# Permissions-Policy: Disable unnecessary browser features
		response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

		return response
