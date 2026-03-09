#!/usr/bin/env python3
#
# app/utils/request_id.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Request ID middleware for tracing requests through the system.

Generates a unique request ID for each incoming request and:
1. Stores it in request.state.request_id
2. Adds it to response headers (X-Request-ID)
3. Sets it in a context variable for structured logging

Usage in templates or handlers:
    request.state.request_id

Usage in logs (via context var):
    from app.utils.request_id import get_request_id
    log.info("...", extra={"request_id": get_request_id()})
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Context variable for request ID (available anywhere in the request lifecycle)
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
	"""Get the current request ID from context."""
	return _request_id_ctx.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
	"""
	Middleware that assigns a unique ID to each request.
	
	- Generates UUID4-based request ID (first 12 chars for brevity)
	- Respects incoming X-Request-ID header if present (for distributed tracing)
	- Adds X-Request-ID to response headers
	- Stores in request.state and context variable
	"""
	
	async def dispatch(
		self, request: Request, call_next: RequestResponseEndpoint
	) -> Response:
		# Check for incoming request ID (distributed tracing)
		request_id = request.headers.get("X-Request-ID")
		
		if not request_id:
			# Generate new request ID (12 char hex for brevity)
			request_id = uuid.uuid4().hex[:12]
		
		# Store in request state
		request.state.request_id = request_id
		
		# Set in context variable for logging
		token = _request_id_ctx.set(request_id)
		
		try:
			response = await call_next(request)
			
			# Add to response headers
			response.headers["X-Request-ID"] = request_id
			
			return response
		finally:
			# Reset context variable
			_request_id_ctx.reset(token)
