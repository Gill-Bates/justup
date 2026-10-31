#!/usr/bin/env python3
#
# app/utils/auth_helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized helpers for resolving the current user.

Goal: keep auth behavior consistent across API, frontend, and HTMX routes.

Key rules:
- If `auth_disabled` is enabled: return a fallback admin user (if any).
- Bearer tokens are accepted via the Authorization header.
- Cookie tokens are only allowed when explicitly enabled by the caller
  (e.g., UI routes), to avoid CSRF issues on API routes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..db import sqlite as sqlite_db
from ..models.users import hash_token
from .settings_cache import is_auth_disabled

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import RedirectResponse, Response

_log = logging.getLogger(__name__)

# Maximum allowed token length (API tokens are typically 32-128 chars)
# Prevents DoS via oversized Authorization headers
MAX_TOKEN_LENGTH = 512

# Dummy hash for constant-time token validation
_DUMMY_HASH = "0" * 64

# Type alias for user dict (improves readability)
User = dict[str, Any]


# Paths a user with a pending mandatory password change may still reach.
PASSWORD_CHANGE_ALLOWED_PATHS = frozenset({"/ui/change-password", "/me/complete-required-change", "/logout"})


def is_password_change_pending(user: User | None) -> bool:
    """Return True if the user must change the password before using the app."""
    if not user:
        return False
    return bool(user.get("must_change_password", 0))


def is_password_change_exempt_path(path: str) -> bool:
    """Return True if the path stays reachable while a password change is pending."""
    return (path.rstrip("/") or "/") in PASSWORD_CHANGE_ALLOWED_PATHS


def _row_to_user(row) -> User | None:
    """Convert a database row to a User dict.

    Centralizes the sqlite3.Row → dict conversion to ensure consistent
    .get() support throughout the codebase.
    """
    if row is None:
        return None
    return dict(row)


def get_user_by_token(conn, token: str) -> User | None:
    """Resolve a plaintext token to a user dict, returning None if invalid.

    Uses constant-time patterns to prevent timing-based token enumeration.
    """
    # Always hash and perform DB lookup (constant-time behavior)
    token_hash = _DUMMY_HASH
    if token:
        try:
            token_hash = hash_token(token)
        except Exception:  # noqa: BLE001 - constant-timing security path: fall back to dummy hash on any hashing error
            # Use dummy hash, still do DB lookup for constant timing
            token_hash = _DUMMY_HASH

    # Always perform DB lookup
    row = sqlite_db.get_user_for_token(conn, token_hash=token_hash)

    # Only return user if original token was valid
    if not token or token_hash == _DUMMY_HASH:
        return None

    return _row_to_user(row)


def get_fallback_admin(conn) -> User | None:
    """Get the first admin user (ORDER BY id ASC) as fallback when auth is disabled."""
    row = sqlite_db.get_first_admin(conn)
    return _row_to_user(row)


def _safe_redirect_target(target: str, default: str = "/ui/login") -> str:
    """Validate redirect target to prevent open redirects.

    Only allows relative paths starting with '/'.
    Rejects protocol-relative URLs (//evil.com) and absolute URLs.
    """
    if not target:
        return default
    # Block absolute URLs and protocol-relative URLs
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return default
    # Must start with / (relative to origin)
    if not target.startswith("/"):
        return default
    # Block protocol-relative URLs disguised as paths
    if target.startswith("//"):
        return default
    return target


def _has_authorization_header(request: Request) -> bool:
    """Return True if the request carries a non-empty Authorization header.

    Fail-closed auth hinges on the *presence* of the header, not on whether the
    token inside it parses: a malformed/expired/revoked bearer value is still an
    explicit attempt to authenticate via the header and must never silently fall
    back to the ambient session cookie. A header that is missing or only
    whitespace is treated as absent so cookie auth can still run on UI routes.
    """
    return bool(request.headers.get("Authorization", "").strip())


def _extract_bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header[:7].lower() == "bearer ":
        token = auth_header[7:].strip()
        if not token:
            return None
        # Prevent DoS via oversized tokens
        if len(token) > MAX_TOKEN_LENGTH:
            _log.debug("Bearer token exceeds maximum length (%d > %d)", len(token), MAX_TOKEN_LENGTH)
            return None
        return token
    return None


def _extract_cookie_token(request: Request, *, cookie_name: str = "token") -> str | None:
    token = request.cookies.get(cookie_name)
    if not token:
        return None
    # Prevent DoS via oversized cookie tokens
    if len(token) > MAX_TOKEN_LENGTH:
        return None
    return token


def resolve_user_optional(
    request: Request,
    conn,
    *,
    allow_cookie: bool = False,
    cookie_name: str = "token",
) -> User | None:
    """Resolve the authenticated user or return None.

    - Respects auth_disabled setting.
    - Checks Authorization header Bearer token.
    - Optionally checks a cookie token (only when allow_cookie=True).

    Returns a dict (not sqlite3.Row) for consistent .get() support.
    """
    if is_auth_disabled(conn):
        fallback = get_fallback_admin(conn)
        if fallback:
            # Audit log for auth bypass
            _log.debug(
                "Auth disabled: request from %s authenticated as admin user '%s'",
                request.client.host if request.client else "unknown",
                fallback.get("username", "?"),
            )
            return fallback
        # Warning if auth_disabled but no admin exists
        _log.warning("auth_disabled is True but no admin user exists – falling through to token authentication")

    # Fail-closed: a present Authorization header is resolved strictly from its
    # own token. If it is malformed/expired/revoked we return None WITHOUT ever
    # reading the cookie, regardless of allow_cookie. Cookie auth may only run
    # when NO Authorization header is present (and the caller permits it).
    if _has_authorization_header(request):
        token = _extract_bearer_token(request)
        if token:
            return get_user_by_token(conn, token)
        return None

    if allow_cookie:
        token = _extract_cookie_token(request, cookie_name=cookie_name)
        if token:
            return get_user_by_token(conn, token)

    return None


def resolve_user_or_redirect(
    request: Request,
    conn,
    target: str = "/ui/login",
) -> User | RedirectResponse:
    """Resolve user or return 302 Redirect to login."""
    from fastapi.responses import RedirectResponse

    user = resolve_user_optional(request, conn, allow_cookie=True)
    if user:
        if is_password_change_pending(user) and not is_password_change_exempt_path(request.url.path):
            return RedirectResponse("/ui/change-password", status_code=302)
        return user
    # Validate redirect target to prevent open redirects
    return RedirectResponse(_safe_redirect_target(target), status_code=302)


def resolve_user_or_htmx_redirect(
    request: Request,
    conn,
    target: str = "/ui/login",
) -> User | Response:
    """Resolve user or return HTMX-compatible redirect (200 OK + HX-Redirect)."""
    user = resolve_user_optional(request, conn, allow_cookie=True)
    must_change = is_password_change_pending(user) and not is_password_change_exempt_path(request.url.path)
    if user and not must_change:
        return user

    from fastapi.responses import Response

    if must_change:
        target = "/ui/change-password"
    safe_target = _safe_redirect_target(target)
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = safe_target
    # Prevent cache poisoning
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response
