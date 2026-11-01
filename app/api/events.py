#!/usr/bin/env python3
#
# app/api/events.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authenticated Server-Sent Events channel for the web UI (``/ui/events``).

The stream carries invalidation events only; browsers reload through the existing REST endpoints.
It authenticates with the session cookie (never a token in the URL), re-validates the session on a
recurring schedule without renewing it, and holds no database connection while it is open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..models.users import hash_token
from ..services.event_bus import EventBus, Subscription
from ..utils.auth_helpers import (
    _extract_bearer_token,
    _extract_cookie_token,
    _has_authorization_header,
    get_fallback_admin,
    is_password_change_pending,
)
from ..utils.settings_cache import is_auth_disabled
from ..utils.version import get_version

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/events", tags=["events"])

HEARTBEAT_SECONDS = 15.0
SESSION_RECHECK_SECONDS = 10.0
RECONNECT_HINT_MS = 3000

_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    # Tell nginx-style reverse proxies not to buffer the stream, otherwise heartbeats never arrive.
    "X-Accel-Buffering": "no",
}


@dataclass(frozen=True)
class StreamSession:
    """Identity captured when the stream opened; re-checked against the database while it stays open."""

    user_id: int
    is_admin: bool
    token_hash: str | None  # None when authentication is disabled (fallback admin)


def resolve_session(db_path: Path | str, request: Request) -> StreamSession | None:
    """Resolve the caller without renewing the session; ``None`` when it is not (or no longer) valid."""
    token = None
    if _has_authorization_header(request):
        token = _extract_bearer_token(request)
    else:
        token = _extract_cookie_token(request)
    return check_token(db_path, token)


def check_token(db_path: Path | str, token: str | None) -> StreamSession | None:
    conn = sqlite_db.connect(db_path)
    try:
        if is_auth_disabled(conn):
            admin = get_fallback_admin(conn)
            if admin:
                return StreamSession(int(admin["id"]), True, None)
        if not token:
            return None
        token_hash = hash_token(token)
        row = sqlite_db.get_user_for_token_readonly(conn, token_hash=token_hash)
        if row is None or is_password_change_pending(dict(row)):
            return None
        return StreamSession(int(row["id"]), bool(row["is_admin"]), token_hash)
    finally:
        sqlite_db.close_connection(conn)


def revalidate(db_path: Path | str, session: StreamSession) -> bool:
    """True while the same identity (user, role, token) still holds."""
    conn = sqlite_db.connect(db_path)
    try:
        if is_auth_disabled(conn):
            admin = get_fallback_admin(conn)
            return session.token_hash is None and admin is not None and int(admin["id"]) == session.user_id
        if session.token_hash is None:
            return False
        row = sqlite_db.get_user_for_token_readonly(conn, token_hash=session.token_hash)
        return (
            row is not None
            and int(row["id"]) == session.user_id
            and bool(row["is_admin"]) == session.is_admin
            and not is_password_change_pending(dict(row))
        )
    finally:
        sqlite_db.close_connection(conn)


def format_frame(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_frames(
    sub: Subscription,
    session_valid: Callable[[], Awaitable[bool]],
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    recheck_seconds: float = SESSION_RECHECK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Yield SSE frames until the bus closes, the client goes away or the session is no longer valid."""
    version = get_version()

    def heartbeat() -> str:
        return format_frame("system.heartbeat", {"ts": int(time.time()), "version": version})

    yield f"retry: {RECONNECT_HINT_MS}\n\n" + heartbeat()
    last_sent = last_check = clock()
    try:
        while True:
            # Sleep until the nearest deadline so heartbeat and session check keep their own cadence.
            wait = min(last_sent + heartbeat_seconds, last_check + recheck_seconds) - clock()
            try:
                event = await sub.next(max(wait, 0.0))
            except StopAsyncIteration:
                return
            now = clock()
            if now - last_check >= recheck_seconds:
                last_check = now
                try:
                    valid = await session_valid()
                except Exception:  # noqa: BLE001 - a failing check must not keep a possibly revoked stream open
                    _log.warning("SSE session re-check failed; closing stream", exc_info=True)
                    valid = False
                if not valid:
                    yield format_frame("system.auth-lost", {})
                    return
            if event is not None:
                last_sent = now
                yield format_frame(event.type, event.data)
            elif now - last_sent >= heartbeat_seconds:
                last_sent = now
                yield heartbeat()
    finally:
        sub.close()


def _no_store(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail}, headers=_HEADERS)


@router.get("")
async def events(request: Request):
    """Open the event stream for the authenticated session."""
    db_path = request.app.state.db_path
    session = await run_in_threadpool(resolve_session, db_path, request)
    if session is None:
        return _no_store(401, "Not authenticated")
    bus: EventBus | None = getattr(request.app.state, "event_bus", None)
    sub = bus.subscribe(session.user_id, session.is_admin) if bus else None
    if sub is None:
        response = _no_store(503, "Event stream unavailable")
        response.headers["Retry-After"] = "10"
        return response

    async def session_valid() -> bool:
        return await asyncio.to_thread(revalidate, db_path, session)

    return StreamingResponse(
        stream_frames(sub, session_valid, heartbeat_seconds=HEARTBEAT_SECONDS, recheck_seconds=SESSION_RECHECK_SECONDS),
        media_type="text/event-stream",
        headers=_HEADERS,
    )


@router.get("/session")
async def session_state(request: Request):
    """Cheap authentication probe for the client after the stream dropped (401 = log in again)."""
    session = await run_in_threadpool(resolve_session, request.app.state.db_path, request)
    if session is None:
        return _no_store(401, "Not authenticated")
    return JSONResponse(content={"ok": True}, headers=_HEADERS)
