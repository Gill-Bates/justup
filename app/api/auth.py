#!/usr/bin/env python3
#
# app/api/auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authentication dependencies for API routes and the HTML frontend."""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..utils.deps import get_conn
from ..utils.auth_helpers import get_user_by_token, resolve_user_optional, User

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

# Cookie-based auth allowed only on these path prefixes
_COOKIE_AUTH_PREFIXES = ("/", "/ui")


def _allow_cookie_auth_for_path(path: str) -> bool:
	"""Return True if cookie-based auth is allowed for this request path.

	Security: cookie tokens are only allowed on the UI/root routes to avoid
	CSRF exposure via ambient cookies on API routes.
	"""
	# Normalize path: strip trailing slash (except for root)
	normalized = path.rstrip("/") or "/"
	return any(
		normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/")
		for prefix in _COOKIE_AUTH_PREFIXES
	)


def _has_flag(user_row: User, key: str) -> bool:
	"""Return True if the given user flag is truthy (stored as 0/1 int)."""
	if user_row is None:
		return False
	try:
		val = user_row.get(key, 0)
		return bool(val) and val != 0
	except Exception:
		_log.warning("Failed to read flag '%s' from user record: %r", key, user_row)
		return False


def get_current_user_optional(
	request: Request,
	credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
	conn: sqlite3.Connection = Depends(get_conn),
) -> Optional[User]:
	"""Get the authenticated user, or None.

	IMPORTANT: Only allow cookie-based auth for UI/root routes.
	API routes must require an explicit Authorization header to avoid CSRF
	exposure via ambient cookies.
	
	Returns a dict (via auth_helpers) for consistent .get() support.
	"""
	# 1. Explicit Bearer token (works on all routes)
	if credentials and credentials.credentials:
		user = get_user_by_token(conn, credentials.credentials)
		if user:
			return user
		# Bearer was provided but invalid → return None (optional = no error)
		return None

	# 2. Cookie-based auth (UI routes only)
	path = request.url.path
	allow_cookie = _allow_cookie_auth_for_path(path)
	return resolve_user_optional(request, conn, allow_cookie=allow_cookie)


def get_current_user(
	request: Request,
	credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
	conn: sqlite3.Connection = Depends(get_conn),
) -> User:
	"""FastAPI dependency that enforces authentication for API routes.
	
	NOTE: Rate-limiting for failed auth attempts must be handled by
	middleware or reverse proxy (e.g. fail2ban, nginx rate-limit).
	This function does NOT throttle brute-force attempts.
	
	WARNING: Token comparison in get_user_by_token() must use
	hmac.compare_digest() or hash-based lookup to prevent timing attacks.
	"""
	# Prefer explicit bearer tokens for API routes.
	if credentials and credentials.credentials:
		user = get_user_by_token(conn, credentials.credentials)
		if user:
			# Validate user is active (disabled users cannot authenticate)
			if not user.get("is_active", True):
				raise HTTPException(status_code=403, detail="Account disabled")
			return user
		raise HTTPException(status_code=401, detail="Invalid or expired token")

	# Allow cookie-based auth only on UI/root routes (and allow auth_disabled fallback).
	path = request.url.path
	allow_cookie = _allow_cookie_auth_for_path(path)
	user = resolve_user_optional(request, conn, allow_cookie=allow_cookie)
	if user:
		# Also check is_active for cookie-based auth
		if not user.get("is_active", True):
			raise HTTPException(status_code=403, detail="Account disabled")
		return user

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

