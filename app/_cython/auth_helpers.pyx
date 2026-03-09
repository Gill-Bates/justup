# cython: language_level=3
# Safety checks controlled by setup_cython.py (_SAFETY_CRITICAL list)
# Do NOT add boundscheck/wraparound/cdivision directives here!

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
from typing import Any, Optional, Union, TYPE_CHECKING
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


def _row_to_user(row) -> Optional[User]:
	"""Convert a database row to a User dict.
	
	Centralizes the sqlite3.Row → dict conversion to ensure consistent
	.get() support throughout the codebase.
	"""
	if row is None:
		return None
	return dict(row)


def get_user_by_token(conn, token: str) -> Optional[User]:
	"""Resolve a plaintext token to a user dict, returning None if invalid.
	
	Uses constant-time patterns to prevent timing-based token enumeration.
	"""
	# Always hash and perform DB lookup (constant-time behavior)
	token_hash = _DUMMY_HASH
	if token:
		try:
			token_hash = hash_token(token)
		except Exception:
			# Use dummy hash, still do DB lookup for constant timing
			token_hash = _DUMMY_HASH
	
	# Always perform DB lookup
	row = sqlite_db.get_user_for_token(conn, token_hash=token_hash)
	
	# Only return user if original token was valid
	if not token or token_hash == _DUMMY_HASH:
		return None
	
	return _row_to_user(row)


def get_fallback_admin(conn) -> Optional[User]:
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


def _extract_bearer_token(request: "Request") -> str | None:
	auth_header = request.headers.get("Authorization", "")
	if auth_header.startswith("Bearer "):
		token = auth_header[7:].strip()
		if not token:
			return None
		# Prevent DoS via oversized tokens
		if len(token) > MAX_TOKEN_LENGTH:
			_log.debug("Bearer token exceeds maximum length (%d > %d)",
					   len(token), MAX_TOKEN_LENGTH)
			return None
		return token
	return None


def _extract_cookie_token(request: "Request", *, cookie_name: str = "token") -> str | None:
	token = request.cookies.get(cookie_name)
	if not token:
		return None
	# Prevent DoS via oversized cookie tokens
	if len(token) > MAX_TOKEN_LENGTH:
		return None
	return token


def resolve_user_optional(
	request: "Request",
	conn,
	*,
	allow_cookie: bool = False,
	cookie_name: str = "token",
) -> Optional[User]:
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
		else:
			# Warning if auth_disabled but no admin exists
			_log.warning(
				"auth_disabled is True but no admin user exists – "
				"falling through to token authentication"
			)

	token = _extract_bearer_token(request)
	if not token and allow_cookie:
		token = _extract_cookie_token(request, cookie_name=cookie_name)

	if token:
		return get_user_by_token(conn, token)

	return None


def resolve_user_or_redirect(
	request: "Request",
	conn,
	target: str = "/ui/login",
) -> Union[User, "RedirectResponse"]:
	"""Resolve user or return 302 Redirect to login."""
	user = resolve_user_optional(request, conn, allow_cookie=True)
	if user:
		return user
	from fastapi.responses import RedirectResponse
	# Validate redirect target to prevent open redirects
	return RedirectResponse(_safe_redirect_target(target), status_code=302)


def resolve_user_or_htmx_redirect(
	request: "Request",
	conn,
	target: str = "/ui/login",
) -> Union[User, "Response"]:
	"""Resolve user or return HTMX-compatible redirect (200 OK + HX-Redirect)."""
	user = resolve_user_optional(request, conn, allow_cookie=True)
	if user:
		return user
	
	from fastapi.responses import Response
	safe_target = _safe_redirect_target(target)
	response = Response(status_code=200)
	response.headers["HX-Redirect"] = safe_target
	# Prevent cache poisoning
	response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
	response.headers["Pragma"] = "no-cache"
	return response

