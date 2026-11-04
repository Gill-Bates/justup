#!/usr/bin/env python3
#
# app/middleware/security_headers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Central security response headers (anti-clickjacking, report-only CSP)."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP_REPORT_PATH = "/csp-report"

# Enforced: only framing control. A full enforced policy would block the inline scripts/styles.
ENFORCED_CSP = "frame-ancestors 'self'"

# Report-only: tailored to the sources the UI uses today (everything same-origin, inline
# script/style allowed). Tighten after the reported violations are gone.
REPORT_ONLY_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https://cdn.jsdelivr.net",
        "font-src 'self' data:",
        "connect-src 'self'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        f"report-uri {CSP_REPORT_PATH}",
    )
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Content-Security-Policy", ENFORCED_CSP),
    ("Content-Security-Policy-Report-Only", REPORT_ONLY_CSP),
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware; a header a route already set is never duplicated or replaced."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
