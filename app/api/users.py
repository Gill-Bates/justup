#!/usr/bin/env python3
#
# app/api/users.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User and authentication API routes (login/logout, self-service)."""

from __future__ import annotations

import ipaddress
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from ..db import notifications
from ..db import sqlite as sqlite_db
from ..models.users import (
    LoginRequest,
    PasswordChangeRequest,
    RequiredPasswordChangeRequest,
    TokenResponse,
    UserCreate,
    UserPublic,
    UserUpdate,
    hash_password,
    hash_token,
    new_token,
    normalize_username,
    verify_password,
)
from ..utils.auth_helpers import is_password_change_pending
from ..utils.client_ip import request_is_https, resolve_trusted_client_ip
from ..utils.deps import get_conn
from ..utils.rate_limit import RATE_LIMIT_AUTH, limiter
from .auth import get_current_user, require_admin

_security = HTTPBearer(auto_error=False)
_log = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["users"])


def _normalize_ip(ip_str: str) -> str:
    """
    Normalize an IP address for consistent rate limiting.

    - IPv4: Returns normalized format (e.g., "192.168.1.1")
    - IPv6: Returns /64 network prefix to prevent prefix rotation attacks
            (users typically get a /64 or /48, so we rate limit the network)
    - Invalid: Returns original string

    Examples:
            "192.168.1.1" -> "192.168.1.1"
            "2001:db8::1" -> "2001:db8::/64"
            "2001:0db8:0000:0000:1234:5678:90ab:cdef" -> "2001:db8::/64"
            "::ffff:192.168.1.1" -> "192.168.1.1" (IPv4-mapped)
    """
    try:
        addr = ipaddress.ip_address(ip_str)

        # Handle IPv4-mapped IPv6 addresses (::ffff:192.168.1.1)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)

        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr)

        # IPv6: Use /64 network prefix for rate limiting
        # This prevents attackers from rotating through their /64 allocation
        network = ipaddress.IPv6Network((addr, 64), strict=False)
        return str(network)

    except ValueError:
        # Invalid IP, return as-is
        return ip_str


def _get_client_ip(request: Request) -> str:
    """Client IP for rate limiting (trusted-proxy rule, IPv6 reduced to its /64)."""
    return _normalize_ip(resolve_trusted_client_ip(request))


_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(ip: str) -> bool:
    """True for private IPv4 addresses per RFC 1918 (unparsable values are treated as public)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and any(addr in net for net in _RFC1918_NETWORKS)


def _row_to_public(row) -> UserPublic:
    """Convert a SQLite user row into the public response model."""
    # sqlite3.Row supports dict-like indexing but not .get() - use try/except for optional fields
    try:
        is_active = bool(row["is_active"])
    except (KeyError, IndexError):
        is_active = True
    try:
        last_login_at = row["last_login_at"]
    except (KeyError, IndexError):
        last_login_at = None
    try:
        can_close_alerts = bool(row["can_close_alerts"])
    except (KeyError, IndexError):
        can_close_alerts = False
    try:
        last_login_ip = row["last_login_ip"]
    except (KeyError, IndexError):
        last_login_ip = None
    try:
        must_change_password = bool(row["must_change_password"])
    except (KeyError, IndexError):
        must_change_password = False
    return UserPublic(
        id=int(row["id"]),
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        can_close_alerts=can_close_alerts,
        is_active=is_active,
        must_change_password=must_change_password,
        created_at=row["created_at"],
        last_login_at=last_login_at,
        last_login_ip=last_login_ip,
    )


def _record_login_failure(conn, client_ip: str) -> tuple[int, int]:
    """Count one failed login for the IP; return (failed_count, lock_seconds) of the 2^n backoff."""
    failed_count = sqlite_db.record_failed_login(conn, client_ip)
    return failed_count, min(2**failed_count, 3600)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(RATE_LIMIT_AUTH)
def login(request: Request, payload: LoginRequest, conn=Depends(get_conn)):
    """Authenticate a user and return a bearer token."""
    client_ip = _get_client_ip(request)

    is_locked, seconds_remaining = sqlite_db.is_ip_locked(conn, client_ip)
    if is_locked:
        # Don't reveal lockout details to prevent enumeration attacks
        _log.info("LOGIN_LOCKED ip=%s remaining=%ds", client_ip, seconds_remaining)
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Please try again later.",
            headers={"Retry-After": str(seconds_remaining)},
        )

    if payload.website.strip():
        # Bot filled the hidden field: answer like a bad login and never check the credentials.
        if _is_rfc1918(client_ip):
            # Private networks are never banned (shared NAT/office egress); the hit is still logged.
            _log.warning("LOGIN_HONEYPOT ip=%s private=true lockout=none", client_ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        # Public IPs reuse the exponential per-IP backoff (2^n seconds, capped at 1 hour).
        failed_count, lock_seconds = _record_login_failure(conn, client_ip)
        _log.warning("LOGIN_HONEYPOT ip=%s attempts=%d lockout=%ds", client_ip, failed_count, lock_seconds)
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"Retry-After": str(lock_seconds)},
        )

    user = sqlite_db.get_user_by_username(conn, payload.username)
    if user is None:
        failed_count, lock_seconds = _record_login_failure(conn, client_ip)
        # Log details internally but give generic response to client
        _log.info(
            "LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=user_not_found", client_ip, failed_count, lock_seconds
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"Retry-After": str(lock_seconds)},
        )

    # Prevent login for inactive accounts
    try:
        is_active = int(user["is_active"])
    except (KeyError, IndexError):
        is_active = 1
    if is_active != 1:
        failed_count, lock_seconds = _record_login_failure(conn, client_ip)
        _log.info(
            "LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=user_inactive", client_ip, failed_count, lock_seconds
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"Retry-After": str(lock_seconds)},
        )

    if not verify_password(payload.password, user["password_hash"]):
        failed_count, lock_seconds = _record_login_failure(conn, client_ip)
        # Log details internally but give generic response to client
        _log.info(
            "LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=wrong_password", client_ip, failed_count, lock_seconds
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"Retry-After": str(lock_seconds)},
        )

    sqlite_db.clear_login_attempts(conn, client_ip)

    # Update last login timestamp and IP address
    sqlite_db.update_user_last_login(conn, int(user["id"]), client_ip)

    session_days = sqlite_db.get_session_max_days(conn)
    token, expires_at, max_expires_at = new_token(max_expiry_minutes=session_days * 24 * 60)
    token_hash = hash_token(token)
    with sqlite_db.transaction(conn):
        sqlite_db.issue_token(
            conn,
            user_id=int(user["id"]),
            token_hash=token_hash,
            expires_at=expires_at,
            max_expires_at=max_expires_at,
        )

    # Build response with HttpOnly auth cookie for browser navigation
    from fastapi.responses import JSONResponse

    # Convert datetime to ISO string for JSON serialization
    expires_at_str = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)
    content: dict = {"access_token": token, "expires_at": expires_at_str}
    if user["must_change_password"]:
        _log.info("LOGIN_MUST_CHANGE_PASSWORD ip=%s username=%s", client_ip, user["username"])
        content["must_change_password"] = True
    response = JSONResponse(content=content)

    # Determine secure flag based on request scheme
    is_secure = request_is_https(request)

    # Set HttpOnly cookie (browser will auto-send on subsequent requests)
    # Cookie name "token" matches auth_helpers.py expectation
    # max_age = full absolute session lifetime; the server still enforces the 6 h idle expiry.
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=session_days * 24 * 3600,
        path="/",
    )
    return response


@router.get("/me", response_model=UserPublic)
def me(user_row=Depends(get_current_user)):
    """Return the currently authenticated user."""
    return _row_to_public(user_row)


@router.post("/me/password")
@limiter.limit(RATE_LIMIT_AUTH)
def change_password(
    request: Request, payload: PasswordChangeRequest, user=Depends(get_current_user), conn=Depends(get_conn)
):
    """Change the current user's password."""
    current_hash = str(user["password_hash"])
    if not verify_password(payload.current_password, current_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = hash_password(payload.new_password)
    user_id = int(user["id"])
    is_admin = bool(user.get("is_admin"))
    with sqlite_db.transaction(conn):
        if not sqlite_db.update_user_password(conn, user_id, new_hash, expected_hash=current_hash):
            raise HTTPException(status_code=409, detail="Password was changed concurrently; please retry")

        # Invalidate all existing tokens for this user (security best practice).
        sqlite_db.revoke_tokens_for_user(conn, user_id)
        if is_admin:
            notifications.notify_admin_password_changed(
                conn,
                user_id=user_id,
                username=str(user["username"]),
                event_key=uuid.uuid4().hex,
            )

    user_dict = dict(user) if hasattr(user, "keys") else user
    _log.warning(
        "PASSWORD_CHANGE user=%s ip=%s tokens_revoked=true",
        user_dict.get("username", "unknown"),
        _get_client_ip(request),
    )

    return {"ok": True, "detail": "Password changed successfully. Please log in again."}


@router.post("/me/complete-required-change")
@limiter.limit(RATE_LIMIT_AUTH)
def complete_required_password_change(
    request: Request,
    payload: RequiredPasswordChangeRequest,
    user=Depends(get_current_user),
    conn=Depends(get_conn),
):
    """Complete the mandatory first-login password change.

    Only valid while the user still has ``must_change_password`` set (bootstrap
    or admin reset). The session authenticates the user, so the temporary
    password is not re-entered. The new password must differ from it.
    """
    if not is_password_change_pending(user):
        raise HTTPException(status_code=403, detail="No password change is required")

    current_hash = str(user["password_hash"])
    if verify_password(payload.new_password, current_hash):
        raise HTTPException(status_code=422, detail="New password must be different from the temporary password")

    user_id = int(user["id"])
    is_admin = bool(user.get("is_admin"))
    with sqlite_db.transaction(conn):
        updated = sqlite_db.update_user_password(
            conn,
            user_id,
            hash_password(payload.new_password),
            must_change_password=False,
            expected_hash=current_hash,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="Password was changed concurrently; please retry")
        sqlite_db.revoke_tokens_for_user(conn, user_id)
        if is_admin:
            notifications.notify_admin_password_changed(
                conn,
                user_id=user_id,
                username=str(user["username"]),
                event_key=uuid.uuid4().hex,
            )

    _log.warning(
        "PASSWORD_CHANGE user=%s ip=%s required_change=true tokens_revoked=true",
        user["username"],
        _get_client_ip(request),
    )
    release = getattr(request.app.state, "bootstrap_gate_release", None)
    if callable(release):
        release()

    response = JSONResponse({"ok": True, "detail": "Password changed successfully. Please log in again."})
    response.delete_cookie("token")
    return response


@router.get("/me/theme")
def get_user_theme(user=Depends(get_current_user), conn=Depends(get_conn)):
    """Get the current user's theme preference."""
    theme = sqlite_db.get_user_theme(conn, int(user["id"]))
    return {"theme": theme}


@router.patch("/me/theme")
def update_user_theme(payload: dict, user=Depends(get_current_user), conn=Depends(get_conn)):
    """Update the current user's theme preference."""
    theme = payload.get("theme")
    if theme not in ("light", "dark", "system"):
        raise HTTPException(status_code=400, detail="Theme must be 'light', 'dark', or 'system'")
    sqlite_db.set_user_theme(conn, int(user["id"]), theme)
    return {"theme": theme}


@router.post("/users", response_model=UserPublic)
def create_user(payload: UserCreate, conn=Depends(get_conn), _admin=Depends(require_admin)):
    """Create a new user (admin-only)."""
    if sqlite_db.get_user_by_username(conn, payload.username) is not None:
        raise HTTPException(status_code=409, detail="Username already exists")
    with sqlite_db.transaction(conn):
        user_id = sqlite_db.create_user(
            conn,
            username=payload.username,
            password_hash=hash_password(payload.password),
            is_admin=bool(payload.is_admin),
            can_close_alerts=bool(payload.can_close_alerts),
        )
        notifications.notify_user_created(conn, user_id=user_id, username=payload.username.lower(), actor=_admin)
    row = sqlite_db.get_user_by_id(conn, user_id)
    return _row_to_public(row)


@router.get("/users", response_model=list[UserPublic])
def list_users(conn=Depends(get_conn), _admin=Depends(require_admin)):
    """List all users (admin-only)."""
    rows = sqlite_db.get_all_users(conn)
    return [_row_to_public(row) for row in rows]


@router.put("/users/{user_id}", response_model=UserPublic)
def update_user(user_id: int, payload: UserUpdate, conn=Depends(get_conn), admin=Depends(require_admin)):
    """Update a user (admin-only). Supports optional password reset."""
    row = sqlite_db.get_user(conn, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent self-lockout patterns
    if int(admin["id"]) == int(user_id) and row["is_admin"] and not payload.is_admin:
        raise HTTPException(status_code=403, detail="Cannot remove your own admin privileges")

    # Username collision check (case-insensitive storage)
    new_username = normalize_username(payload.username)
    existing = sqlite_db.get_user_by_username(conn, new_username)
    if existing is not None and int(existing["id"]) != int(user_id):
        raise HTTPException(status_code=409, detail="Username already exists")

    # Apply updates
    sqlite_db.update_user(
        conn,
        user_id,
        username=new_username,
        is_admin=bool(payload.is_admin),
        can_close_alerts=bool(payload.can_close_alerts),
    )

    # Optional password reset (admin-only)
    if payload.new_password:
        new_hash = hash_password(payload.new_password)
        # An admin-set password is temporary: the user must choose their own at next login.
        is_admin_target = bool(row["is_admin"]) or bool(payload.is_admin)
        with sqlite_db.transaction(conn):
            sqlite_db.update_user_password(conn, user_id, new_hash, must_change_password=True)
            sqlite_db.revoke_tokens_for_user(conn, user_id)
            if is_admin_target:
                notifications.notify_admin_password_changed(
                    conn,
                    user_id=user_id,
                    username=new_username,
                    event_key=uuid.uuid4().hex,
                    reset_by=admin,
                )
        _log.warning("PASSWORD_RESET user_id=%d reset_by=%s tokens_revoked=true", user_id, admin["username"])

    updated = sqlite_db.get_user_by_id(conn, user_id)
    return _row_to_public(updated)


class SetUserActiveRequest(BaseModel):
    """Strict payload so strings like "false" are rejected instead of coerced."""

    model_config = ConfigDict(strict=True, extra="forbid")

    is_active: bool


@router.post("/users/{user_id}/active", response_model=UserPublic)
def set_user_active(user_id: int, payload: SetUserActiveRequest, conn=Depends(get_conn), admin=Depends(require_admin)):
    """Activate/deactivate a user (admin-only)."""
    row = sqlite_db.get_user(conn, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deactivating yourself
    if int(admin["id"]) == int(user_id):
        raise HTTPException(status_code=403, detail="Cannot deactivate your own account")

    # Prevent deactivating admin accounts (avoid lockout)
    if row["is_admin"]:
        raise HTTPException(status_code=403, detail="Cannot deactivate admin users")

    is_active = payload.is_active
    sqlite_db.set_user_active(conn, user_id, is_active=is_active)
    if not is_active:
        sqlite_db.revoke_tokens_for_user(conn, user_id)
        _log.info("USER_DEACTIVATED user_id=%d by=%s", user_id, admin["username"])
    else:
        _log.info("USER_ACTIVATED user_id=%d by=%s", user_id, admin["username"])

    updated = sqlite_db.get_user_by_id(conn, user_id)
    return _row_to_public(updated)


@router.post("/users/{user_id}/revoke-sessions")
def revoke_user_sessions(user_id: int, conn=Depends(get_conn), admin=Depends(require_admin)) -> dict[str, int]:
    """Revoke every active session token of a user (admin-only)."""
    if not sqlite_db.get_user(conn, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if int(admin["id"]) == int(user_id):
        raise HTTPException(status_code=403, detail="Cannot revoke your own sessions; sign out instead")

    revoked = sqlite_db.revoke_tokens_for_user(conn, user_id)
    _log.info("USER_SESSIONS_REVOKED user_id=%d revoked=%d by=%s", user_id, revoked, admin["username"])
    return {"revoked": revoked}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, conn=Depends(get_conn), admin=Depends(require_admin)):
    """Delete a user (admin-only). Admin users cannot be deleted."""
    target_user = sqlite_db.get_user(conn, user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deletion of admin users
    if target_user["is_admin"]:
        raise HTTPException(status_code=403, detail="Cannot delete admin users")

    # Prevent self-deletion even if not admin (edge case)
    if user_id == admin["id"]:
        raise HTTPException(status_code=403, detail="Cannot delete your own account")

    deleted = sqlite_db.delete_user(conn, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")

    _log.info("USER_DELETED user_id=%d deleted_by=%s", user_id, admin["username"])


@router.post("/logout")
def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    conn=Depends(get_conn),
):
    """Revoke the presented bearer token and the session cookie token (logout)."""
    tokens = []
    if credentials and credentials.credentials:
        tokens.append(credentials.credentials)
    cookie_token = request.cookies.get("token")
    if cookie_token and cookie_token not in tokens:
        tokens.append(cookie_token)

    if not tokens:
        detail = "No token to revoke"
    else:
        revoked = [sqlite_db.revoke_token(conn, token_hash=hash_token(t)) for t in tokens]
        if any(revoked):
            detail = "Token revoked successfully"
        else:
            # Already revoked, expired or unknown - still ok for idempotency
            _log.debug("LOGOUT_NO_TOKEN ip=%s hint=token_not_found_or_expired", _get_client_ip(request))
            detail = "Token already revoked or expired"
    resp = JSONResponse({"ok": True, "detail": detail})
    resp.delete_cookie("token")
    return resp
