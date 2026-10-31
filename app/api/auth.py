#!/usr/bin/env python3
#
# app/api/auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authentication dependencies for API routes and the HTML frontend."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..utils.auth_helpers import (
    User,
    get_user_by_token,
    is_password_change_exempt_path,
    is_password_change_pending,
    resolve_user_optional,
)
from ..utils.deps import get_conn

_log = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)

# Public API exports
__all__ = [
    "get_current_user",
    "get_current_user_optional",
    "require_admin",
    "require_can_close_alerts",
    "require_permission",
]


def _allow_cookie_auth_for_path(path: str) -> bool:
    """Return True if cookie-based auth is allowed for this request path.

    Security: cookie tokens are only allowed on the UI/root routes to avoid
    CSRF exposure via ambient cookies on API routes.
    """
    normalized = path.rstrip("/") or "/"
    return normalized in {"/", "/ui", "/me/theme", "/me/complete-required-change"} or normalized.startswith("/ui/")


def _enforce_password_change(request: Request, user: User) -> None:
    """Reject API use while a mandatory password change is pending."""
    if is_password_change_pending(user) and not is_password_change_exempt_path(request.url.path):
        raise HTTPException(status_code=428, detail="Password change required")


def _has_flag(user_row: User, key: str) -> bool:
    """Return True if the given user flag is truthy (stored as 0/1 int)."""
    if user_row is None:
        return False
    try:
        val = user_row.get(key, 0)
        return bool(val) and val != 0
    except Exception:  # noqa: BLE001 - fail-closed flag read: malformed user record returns False
        _log.warning("Failed to read flag '%s' from user record: %r", key, user_row)
        return False


def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    conn: sqlite3.Connection = Depends(get_conn),
) -> User | None:
    """Get the authenticated user, or None.

    IMPORTANT: Only allow cookie-based auth for UI/root routes.
    API routes require an explicit Authorization header except for the
    CSRF-protected self-service theme route.

    Returns a dict (via auth_helpers) for consistent .get() support.

    Fail-closed: a well-formed bearer header (credentials populated by
    HTTPBearer) resolves strictly from its own token and returns None on
    failure. A PRESENT but malformed header is handled fail-closed inside
    resolve_user_optional (no cookie fallback). Cookie auth is only reached when
    no Authorization header is sent, and only on UI/root and the CSRF-protected
    theme route.
    """
    # 1. Well-formed bearer token (works on all routes).
    if credentials and credentials.credentials:
        return get_user_by_token(conn, credentials.credentials)

    # 2. No well-formed bearer: resolve_user_optional fail-closes on a present
    #    (malformed) header and only reads the cookie when no header is present.
    path = request.url.path
    allow_cookie = _allow_cookie_auth_for_path(path)
    return resolve_user_optional(request, conn, allow_cookie=allow_cookie)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    conn: sqlite3.Connection = Depends(get_conn),
) -> User:
    """FastAPI dependency that enforces authentication for API routes.

    NOTE: Rate-limiting for failed auth attempts must be handled by
    middleware or reverse proxy (e.g. fail2ban, nginx rate-limit).
    This function does NOT throttle brute-force attempts.

    WARNING: Token comparison in get_user_by_token() must use
    hmac.compare_digest() or hash-based lookup to prevent timing attacks.

    Fail-closed: a PRESENT Authorization header is resolved strictly from its
    own token and yields 401 when it is malformed/invalid/expired/revoked,
    never silently falling back to the session cookie. A well-formed bearer
    takes the explicit path below; a present-but-malformed header is caught by
    resolve_user_optional (no cookie fallback). Cookie auth is only reached on
    UI/root and the CSRF-protected theme route when no Authorization header is
    sent.
    """

    def _finalize(user: User) -> User:
        # Disabled users cannot authenticate (applies to both bearer and cookie).
        if not user.get("is_active", True):
            raise HTTPException(status_code=403, detail="Account disabled")
        _enforce_password_change(request, user)
        request.state.auth_user_id = user.get("id")
        return user

    # 1. Well-formed bearer token: resolve strictly, 401 on any failure.
    if credentials and credentials.credentials:
        user = get_user_by_token(conn, credentials.credentials)
        if user:
            return _finalize(user)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # 2. No well-formed bearer. resolve_user_optional fail-closes on a present
    #    (malformed) header and only reads the cookie when no header is present
    #    (and only on the allow-listed UI/root routes).
    path = request.url.path
    allow_cookie = _allow_cookie_auth_for_path(path)
    user = resolve_user_optional(request, conn, allow_cookie=allow_cookie)
    if user:
        return _finalize(user)

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(user_row: User = Depends(get_current_user)) -> User:
    """FastAPI dependency that enforces admin privileges."""
    if not _has_flag(user_row, "is_admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user_row


def require_can_close_alerts(user_row: User = Depends(get_current_user)) -> User:
    """FastAPI dependency that enforces the permission to close/acknowledge alerts.

    Admins always have this permission implicitly.
    """
    if _has_flag(user_row, "is_admin"):
        return user_row
    if not _has_flag(user_row, "can_close_alerts"):
        raise HTTPException(status_code=403, detail="Permission required: can_close_alerts")
    return user_row


def require_permission(permission: str):
    """Factory: creates a FastAPI dependency that checks a specific permission.

    Admins implicitly have all permissions.

    Usage:
            @router.post("/targets")
            def create_target(user: User = Depends(require_permission("can_edit_targets"))):
                    ...
    """

    def _dependency(user_row: User = Depends(get_current_user)) -> User:
        if _has_flag(user_row, "is_admin"):
            return user_row
        if not _has_flag(user_row, permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission required: {permission}",
            )
        return user_row

    return _dependency
