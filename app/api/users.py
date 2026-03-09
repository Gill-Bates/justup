#!/usr/bin/env python3
#
# app/api/users.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User and authentication API routes (login/logout, self-service)."""

from __future__ import annotations

import ipaddress
import logging
import os
from functools import lru_cache

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db import sqlite as sqlite_db
from ..models.users import LoginRequest, PasswordChangeRequest, TokenResponse, UserCreate, UserPublic, UserUpdate, hash_password, hash_token, new_token, verify_password
from ..utils.deps import get_conn
from ..utils.rate_limit import limiter, RATE_LIMIT_AUTH
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
	"""
	Extract client IP for rate limiting.

	Security rule:
	- Proxy headers (X-Forwarded-For / X-Real-IP / Forwarded) are only honored
	  if the immediate peer (request.client.host) is a configured trusted proxy.
	- Otherwise, we use the socket peer address to prevent header spoofing.

	Configure trusted proxies via:
	  UPMON_TRUSTED_PROXIES=127.0.0.1,::1,10.0.0.0/8
	"""
	@lru_cache(maxsize=1)
	def _trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
		# Cached once per process; restart required to apply env changes
		raw = os.getenv("UPMON_TRUSTED_PROXIES", "").strip()
		if not raw:
			return tuple()
		nets: list[ipaddress._BaseNetwork] = []
		for part in raw.split(","):
			item = part.strip()
			if not item:
				continue
			try:
				# Accept either single IPs or CIDRs
				if "/" in item:
					nets.append(ipaddress.ip_network(item, strict=False))
				else:
					ip = ipaddress.ip_address(item)
					prefix = 32 if ip.version == 4 else 128
					nets.append(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
			except ValueError:
				# Ignore invalid entries (misconfig); fail closed (no trust)
				continue
		return tuple(nets)

	peer_ip_str = request.client.host if request.client else "unknown"
	try:
		peer_ip = ipaddress.ip_address(peer_ip_str)
	except ValueError:
		# Can't parse peer IP; fail closed
		return _normalize_ip(peer_ip_str)

	trusted = _trusted_proxy_networks()
	peer_is_trusted = any(peer_ip in net for net in trusted)
	if not peer_is_trusted:
		return _normalize_ip(str(peer_ip))

	# Trusted proxy: inspect headers
	forwarded_for = request.headers.get("X-Forwarded-For")
	if forwarded_for:
		# Take first IP (original client) from the chain
		candidate = forwarded_for.split(",")[0].strip()
		return _normalize_ip(candidate)

	x_real_ip = request.headers.get("X-Real-IP")
	if x_real_ip:
		return _normalize_ip(x_real_ip.strip())

	# RFC 7239 Forwarded: for=...
	forwarded = request.headers.get("Forwarded")
	if forwarded:
		try:
			# Example: Forwarded: for=192.0.2.60;proto=https;by=203.0.113.43
			parts = [p.strip() for p in forwarded.split(";")]
			for p in parts:
				if p.lower().startswith("for="):
					val = p[4:].strip().strip('"')
					# Might be IPv6 in brackets
					val = val.strip("[]")
					# Might include port
					if ":" in val and val.count(":") == 1 and val.rsplit(":", 1)[1].isdigit():
						val = val.rsplit(":", 1)[0]
					return _normalize_ip(val)
		except Exception:
			pass

	# Fall back to peer IP
	return _normalize_ip(str(peer_ip))


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
	return UserPublic(
		id=int(row["id"]),
		username=row["username"],
		is_admin=bool(row["is_admin"]),
		can_close_alerts=can_close_alerts,
		is_active=is_active,
		created_at=row["created_at"],
		last_login_at=last_login_at,
		last_login_ip=last_login_ip,
	)


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
	
	user = sqlite_db.get_user_by_username(conn, payload.username)
	if user is None:
		failed_count = sqlite_db.record_failed_login(conn, client_ip)
		lock_seconds = min(2 ** failed_count, 3600)
		# Log details internally but give generic response to client
		_log.info("LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=user_not_found", client_ip, failed_count, lock_seconds)
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
		failed_count = sqlite_db.record_failed_login(conn, client_ip)
		lock_seconds = min(2 ** failed_count, 3600)
		_log.info("LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=user_inactive", client_ip, failed_count, lock_seconds)
		raise HTTPException(
			status_code=401,
			detail="Invalid credentials",
			headers={"Retry-After": str(lock_seconds)},
		)

	if not verify_password(payload.password, user["password_hash"]):
		failed_count = sqlite_db.record_failed_login(conn, client_ip)
		lock_seconds = min(2 ** failed_count, 3600)
		# Log details internally but give generic response to client
		_log.info("LOGIN_FAILED ip=%s attempts=%d lockout=%ds reason=wrong_password", client_ip, failed_count, lock_seconds)
		raise HTTPException(
			status_code=401,
			detail="Invalid credentials",
			headers={"Retry-After": str(lock_seconds)},
		)

	sqlite_db.clear_login_attempts(conn, client_ip)
	
	# Update last login timestamp and IP address
	sqlite_db.update_user_last_login(conn, int(user["id"]), client_ip)
	
	token, expires_at, max_expires_at = new_token()
	sqlite_db.issue_token(
		conn, 
		user_id=int(user["id"]), 
		token_hash=hash_token(token), 
		expires_at=expires_at,
		max_expires_at=max_expires_at
	)
	
	# Build response with HttpOnly auth cookie for browser navigation
	from fastapi.responses import JSONResponse
	
	# Convert datetime to ISO string for JSON serialization
	expires_at_str = expires_at.isoformat() if hasattr(expires_at, 'isoformat') else str(expires_at)
	response = JSONResponse(content={"access_token": token, "expires_at": expires_at_str})
	
	# Determine secure flag based on request scheme
	forwarded_proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
	is_secure = forwarded_proto == "https"
	
	# Set HttpOnly cookie (browser will auto-send on subsequent requests)
	# Cookie name "token" matches auth_helpers.py expectation
	# max_age matches token lifetime (1 hour default)
	response.set_cookie(
		key="token",
		value=token,
		httponly=True,
		secure=is_secure,
		samesite="lax",
		max_age=3600,
		path="/",
	)
	return response


@router.get("/me", response_model=UserPublic)
def me(user_row=Depends(get_current_user)):
	"""Return the currently authenticated user."""
	return _row_to_public(user_row)


@router.post("/me/password")
def change_password(request: Request, payload: PasswordChangeRequest, user=Depends(get_current_user), conn=Depends(get_conn)):
	"""Change the current user's password."""

	if not verify_password(payload.current_password, user["password_hash"]):
		raise HTTPException(status_code=401, detail="Current password is incorrect")

	new_hash = hash_password(payload.new_password)
	sqlite_db.update_user_password(conn, int(user["id"]), new_hash)
	
	# Invalidate all existing tokens for this user (security best practice)
	sqlite_db.revoke_tokens_for_user(conn, int(user["id"]))

	user_dict = dict(user) if hasattr(user, "keys") else user
	_log.warning("PASSWORD_CHANGE user=%s ip=%s tokens_revoked=true", user_dict.get("username", "unknown"), _get_client_ip(request))

	return {"ok": True, "detail": "Password changed successfully. Please log in again."}


@router.get("/me/theme")
def get_user_theme(user=Depends(get_current_user), conn=Depends(get_conn)):
	"""Get the current user's theme preference."""
	from ..utils.constants import SettingKeys
	theme = sqlite_db.get_setting(conn, SettingKeys.THEME, "system")
	return {"theme": theme}


@router.patch("/me/theme")
def update_user_theme(payload: dict, user=Depends(get_current_user), conn=Depends(get_conn)):
	"""Update the current user's theme preference."""
	from ..utils.constants import SettingKeys
	theme = payload.get("theme")
	if theme not in ("light", "dark", "system"):
		raise HTTPException(status_code=400, detail="Theme must be 'light', 'dark', or 'system'")
	sqlite_db.set_setting(conn, SettingKeys.THEME, theme)
	return {"theme": theme}


@router.post("/users", response_model=UserPublic)
def create_user(payload: UserCreate, request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Create a new user (admin-only)."""
	if sqlite_db.get_user_by_username(conn, payload.username) is not None:
		raise HTTPException(status_code=409, detail="Username already exists")
	user_id = sqlite_db.create_user(
		conn,
		username=payload.username,
		password_hash=hash_password(payload.password),
		is_admin=bool(payload.is_admin),
		can_close_alerts=bool(payload.can_close_alerts),
	)
	row = sqlite_db.get_user_by_id(conn, user_id)
	return _row_to_public(row)


@router.get("/users", response_model=list[UserPublic])
def list_users(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""List all users (admin-only)."""
	rows = sqlite_db.get_all_users(conn)
	return [_row_to_public(row) for row in rows]


@router.put("/users/{user_id}", response_model=UserPublic)
def update_user(user_id: int, payload: UserUpdate, request: Request, conn=Depends(get_conn), admin=Depends(require_admin)):
	"""Update a user (admin-only). Supports optional password reset."""
	row = sqlite_db.get_user(conn, user_id)
	if not row:
		raise HTTPException(status_code=404, detail="User not found")

	# Prevent self-lockout patterns
	if int(admin["id"]) == int(user_id):
		# Allow rename / role change, but password reset should be done via /me/password.
		pass

	# Username collision check (case-insensitive storage)
	new_username = payload.username.strip().lower()
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
		sqlite_db.update_user_password(conn, user_id, new_hash)
		sqlite_db.revoke_tokens_for_user(conn, user_id)
		_log.warning("PASSWORD_RESET user_id=%d reset_by=%s tokens_revoked=true", user_id, admin["username"])

	updated = sqlite_db.get_user_by_id(conn, user_id)
	return _row_to_public(updated)


@router.post("/users/{user_id}/active", response_model=UserPublic)
def set_user_active(user_id: int, payload: dict, conn=Depends(get_conn), admin=Depends(require_admin)):
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

	is_active = bool(payload.get("is_active", True))
	sqlite_db.set_user_active(conn, user_id, is_active=is_active)
	if not is_active:
		sqlite_db.revoke_tokens_for_user(conn, user_id)
		_log.info("USER_DEACTIVATED user_id=%d by=%s", user_id, admin["username"])
	else:
		_log.info("USER_ACTIVATED user_id=%d by=%s", user_id, admin["username"])

	updated = sqlite_db.get_user_by_id(conn, user_id)
	return _row_to_public(updated)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, conn=Depends(get_conn), admin=Depends(require_admin)):
	"""Delete a user (admin-only). Admin users cannot be deleted."""
	target_user = sqlite_db.get_user(conn, user_id)
	if not target_user:
		raise HTTPException(status_code=404, detail="User not found")
	
	# Prevent deletion of admin users
	if target_user["is_admin"]:
		raise HTTPException(
			status_code=403,
			detail="Cannot delete admin users"
		)
	
	# Prevent self-deletion even if not admin (edge case)
	if user_id == admin["id"]:
		raise HTTPException(
			status_code=403,
			detail="Cannot delete your own account"
		)
	
	deleted = sqlite_db.delete_user(conn, user_id)
	if not deleted:
		raise HTTPException(status_code=404, detail="User not found")
	
	_log.info("USER_DELETED user_id=%d deleted_by=%s", user_id, admin["username"])
	return None


@router.post("/logout")
def logout(
	request: Request,
	credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
	conn=Depends(get_conn),
):
	"""Revoke the current token (logout)."""
	resp = JSONResponse({"ok": True, "detail": "No token to revoke"})
	# If the UI stored a token as a cookie, clear it to avoid auth divergence.
	resp.delete_cookie("token")
	if credentials and credentials.credentials:
		token_hash = hash_token(credentials.credentials)
		revoked = sqlite_db.revoke_token(conn, token_hash=token_hash)
		if revoked:
			resp = JSONResponse({"ok": True, "detail": "Token revoked successfully"})
		else:
			# Token not found (already revoked, expired, or invalid) - still return ok for idempotency
			_log.debug("LOGOUT_NO_TOKEN ip=%s hint=token_not_found_or_expired", _get_client_ip(request))
			resp = JSONResponse({"ok": True, "detail": "Token already revoked or expired"})
		resp.delete_cookie("token")
		return resp
	return resp
