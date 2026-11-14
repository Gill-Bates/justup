#!/usr/bin/env python3
#
# app/middleware/body_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cap the request body of the backup restore upload before multipart parsing and authentication."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..utils.backup import MAX_BACKUP_UPLOAD_BYTES

RESTORE_PATH = "/settings/restore"
# Multipart framing and the password field on top of the archive itself.
_OVERHEAD_BYTES = 1024 * 1024
MAX_RESTORE_BODY_BYTES = MAX_BACKUP_UPLOAD_BYTES + _OVERHEAD_BYTES


class _BodyTooLarge(Exception):  # noqa: N818 - internal control-flow signal, not an error type
    """Raised inside the receive channel to abort the app's body read."""


_TOO_LARGE = {"detail": "Request body too large"}


class RestoreBodyLimitMiddleware:
    """Pure ASGI middleware: BaseHTTPMiddleware cannot bound a streamed request body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != RESTORE_PATH:
            await self.app(scope, receive, send)
            return
        limit = MAX_RESTORE_BODY_BYTES
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await JSONResponse(_TOO_LARGE, status_code=413, headers={"Connection": "close"})(scope, receive, send)
            return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Counts chunked bodies without Content-Length; aborts the app's read.
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # drop the app's own error response; the 413 below replaces it
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await JSONResponse(_TOO_LARGE, status_code=413, headers={"Connection": "close"})(scope, receive, send)
