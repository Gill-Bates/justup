#!/usr/bin/env python3
#
# app/api/auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authentication API routes and dependencies."""

from __future__ import annotations

import ipaddress
import logging
import os
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db.sqlite_auth import (
	clear_login_attempts,
	create_auth_token,
	delete_auth_token,
	get_user_by_token,
	is_ip_locked,
	record_failed_login,
)
from ..db.sqlite_users import (
	decrypt_otp_secret,
	get_user_by_id,
	get_user_by_username,
	update_last_login,
	update_user_recovery_codes,
)
from ..models.users import (
	LoginRequest,
	MFARecoveryRequest,
	MFAVerifyRequest,
)
from ..utils.crypto import DUMMY_PASSWORD_HASH, generate_token_expiry, new_token, verify_password
from ..utils.deps import get_conn
from ..utils.network import parse_ip_str
from ..utils.otp import (
	use_recovery_code,
	verify_otp,
)
from ..utils.rate_limit import RATE_LIMIT_AUTH, limiter
from .response import ok_response

_log = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)

router = APIRouter(tags=["auth"])

_DUMMY_OTP_SECRET = "JBSWY3DPEHPK3PXP"
_MFA_CHALLENGE_TTL_SECONDS = 180
_RECOVERY_DOWNLOAD_TTL_SECONDS = 300
_AUTH_COOKIE = "auth_token"
_AUTH_COOKIE_MAX_AGE = 86400  # 24 hours in seconds
_DEFAULT_TRUSTED_PROXY_CIDRS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,169.254.0.0/16,fe80::/10"

# CSRF protection is handled by CSRFMiddleware (double-submit cookie + Origin validation)
# Auth cookies use SameSite=Strict for additional defense-in-depth

# WARNING: In-memory caches are per-process. Multi-worker deployments (Gunicorn, k8s)
# will cause MFA challenges to fail intermittently if the login and verify requests
# hit different workers. For production, use Redis, DB, or signed JWT tokens.
_mfa_challenge_cache: dict[str, tuple[int, str, str, float]] = {}
_mfa_challenge_cache_lock = threading.Lock()
_recovery_download_cache: dict[str, tuple[int, str, list[str], float]] = {}
_recovery_download_cache_lock = threading.Lock()

_COOKIE_AUTH_PREFIXES = ("/ui", "/api", "/status", "/swagger")
_COOKIE_AUTH_PREFIXES_NORMALIZED = tuple(prefix.rstrip("/") for prefix in _COOKIE_AUTH_PREFIXES)


def _load_trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
	raw = os.environ.get("TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED_PROXY_CIDRS)
	networks: list[ipaddress._BaseNetwork] = []
	for cidr in (item.strip() for item in raw.split(",")):
		if not cidr:
			continue
		try:
			networks.append(ipaddress.ip_network(cidr, strict=False))
		except ValueError:
			_log.warning("Ignoring invalid TRUSTED_PROXY_CIDRS entry: %s", cidr)
	if not networks:
		networks = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
	return tuple(networks)


_TRUSTED_PROXY_NETWORKS = _load_trusted_proxy_networks()


def _is_trusted_proxy_ip(ip_text: str) -> bool:
	try:
		ip_obj = ipaddress.ip_address(ip_text)
	except ValueError:
		return False
	return any(ip_obj in network for network in _TRUSTED_PROXY_NETWORKS)


def _parse_ip(value: str | None) -> str | None:
	return parse_ip_str(value)


def _store_mfa_challenge(user_id: int, username: str, client_ip: str) -> str:
	"""Store MFA challenge. Note: cleanup only runs on write, stale entries may leak if traffic stops."""
	token = new_token()
	expires_at = time.monotonic() + _MFA_CHALLENGE_TTL_SECONDS
	with _mfa_challenge_cache_lock:
		now = time.monotonic()
		expired = [key for key, value in _mfa_challenge_cache.items() if value[3] <= now]
		for key in expired:
			_mfa_challenge_cache.pop(key, None)
		_mfa_challenge_cache[token] = (user_id, username, client_ip, expires_at)
	return token


def _consume_mfa_challenge(token: str, username: str, client_ip: str) -> int | None:
	with _mfa_challenge_cache_lock:
		entry = _mfa_challenge_cache.pop(token, None)
	if not entry:
		return None
	user_id, expected_username, expected_ip, expires_at = entry
	if expires_at <= time.monotonic():
		return None
	if expected_username != username:
		return None
	if expected_ip != client_ip:
		return None
	return user_id


def _store_recovery_download(user_id: int, username: str, codes: list[str]) -> str:
	token = new_token()
	expires_at = time.monotonic() + _RECOVERY_DOWNLOAD_TTL_SECONDS
	with _recovery_download_cache_lock:
		now = time.monotonic()
		expired = [key for key, value in _recovery_download_cache.items() if value[3] <= now]
		for key in expired:
			_recovery_download_cache.pop(key, None)
		_recovery_download_cache[token] = (user_id, username, list(codes), expires_at)
	return token


def _consume_recovery_download(token: str, user_id: int) -> tuple[str, list[str]] | None:
	with _recovery_download_cache_lock:
		entry = _recovery_download_cache.pop(token, None)
	if not entry:
		return None
	stored_user_id, username, codes, expires_at = entry
	if stored_user_id != user_id:
		return None
	if expires_at <= time.monotonic():
		return None
	return username, codes


def _allow_cookie_auth_for_path(path: str) -> bool:
	normalized = path.rstrip("/") or "/"
	return any(
		normalized == prefix or normalized.startswith(prefix + "/")
		for prefix in _COOKIE_AUTH_PREFIXES_NORMALIZED
	)


def _get_client_ip(request: Request) -> str:
	scope_client = request.scope.get("client")
	if not scope_client or not scope_client[0]:
		raise HTTPException(status_code=400, detail="Unable to determine client IP")
	socket_ip = _parse_ip(scope_client[0])
	if not socket_ip:
		raise HTTPException(status_code=400, detail="Unable to determine client IP")

	if _is_trusted_proxy_ip(socket_ip):
		forwarded_for = request.headers.get("X-Forwarded-For")
		if forwarded_for:
			candidate = _parse_ip(forwarded_for.split(",")[0])
			if candidate:
				return candidate
		x_real_ip = request.headers.get("X-Real-IP")
		if x_real_ip:
			candidate = _parse_ip(x_real_ip)
			if candidate:
				return candidate

	return socket_ip


def _is_https(request: Request) -> bool:
	if request.url.scheme == "https":
		return True
	scope_client = request.scope.get("client")
	socket_ip = _parse_ip(scope_client[0]) if scope_client and scope_client[0] else None
	if socket_ip and _is_trusted_proxy_ip(socket_ip):
		return request.headers.get("X-Forwarded-Proto", "").lower() == "https"
	return False


def _lookup_user_by_token(token: str, conn: sqlite3.Connection, require_active: bool = True) -> sqlite3.Row | None:
	user = get_user_by_token(conn, token)
	if user and require_active and not user["is_active"]:
		return None
	return user


def get_current_user_optional(
	request: Request,
	credentials: HTTPAuthorizationCredentials | None = Depends(_security),
	conn: sqlite3.Connection = Depends(get_conn),
) -> sqlite3.Row | None:
	if credentials and credentials.credentials:
		return _lookup_user_by_token(credentials.credentials, conn, require_active=True)
	path = request.url.path
	if not _allow_cookie_auth_for_path(path):
		return None
	token = request.cookies.get(_AUTH_COOKIE)
	if token:
		return _lookup_user_by_token(token, conn, require_active=True)
	return None


def get_current_user(
	request: Request,
	credentials: HTTPAuthorizationCredentials | None = Depends(_security),
	conn: sqlite3.Connection = Depends(get_conn),
) -> sqlite3.Row:
	if credentials and credentials.credentials:
		user = _lookup_user_by_token(credentials.credentials, conn, require_active=False)
		if user:
			if not user["is_active"]:
				raise HTTPException(status_code=403, detail="Account disabled")
			return user
		raise HTTPException(status_code=401, detail="Invalid or expired token")

	path = request.url.path
	if _allow_cookie_auth_for_path(path):
		token = request.cookies.get(_AUTH_COOKIE)
		if token:
			user = _lookup_user_by_token(token, conn, require_active=False)
			if user:
				if not user["is_active"]:
					raise HTTPException(status_code=403, detail="Account disabled")
				return user

	raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(
	user: sqlite3.Row = Depends(get_current_user),
) -> sqlite3.Row:
	if not user["is_admin"]:
		raise HTTPException(status_code=403, detail="Admin access required")
	return user


def _issue_session(request: Request, conn: sqlite3.Connection, user: sqlite3.Row, client_ip: str) -> Response:
	"""Issue a session token and return a response with cookie + token in body."""
	token = new_token()
	expires_at, max_expires_at = generate_token_expiry()
	create_auth_token(conn, user["id"], token, expires_at, max_expires_at)
	clear_login_attempts(conn, client_ip)
	update_last_login(conn, user["id"], client_ip)

	# Calculate max_age from actual token expiry
	from ..utils.time import utcnow
	now = utcnow()
	max_age = int((expires_at - now).total_seconds())
	if max_age < 0:
		max_age = _AUTH_COOKIE_MAX_AGE

	response = ok_response(data={"token": token})
	is_secure = _is_https(request)
	response.set_cookie(
		key=_AUTH_COOKIE,
		value=token,
		httponly=True,
		samesite="strict",
		secure=is_secure,
		path="/",
		max_age=max_age,
	)
	return response


# ── Login / Logout ──

@router.post("/login")
@limiter.limit(RATE_LIMIT_AUTH)
def login(request: Request, payload: LoginRequest, conn: sqlite3.Connection = Depends(get_conn)):
	client_ip = _get_client_ip(request)
	is_locked, seconds_remaining = is_ip_locked(conn, client_ip)
	if is_locked:
		raise HTTPException(
			status_code=429,
			detail="Too many failed attempts. Please try again later.",
			headers={"Retry-After": str(seconds_remaining)},
		)

	user = get_user_by_username(conn, payload.username)
	pw_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
	valid = verify_password(payload.password, pw_hash)

	if not user or not valid:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid username or password")

	if not user["is_active"]:
		raise HTTPException(status_code=403, detail="Account disabled")

	# Check MFA
	if user["otp_enabled"]:
		mfa_token = _store_mfa_challenge(user["id"], user["username"], client_ip)
		return ok_response(data={"mfa_required": True, "mfa_token": mfa_token})

	# Issue auth token
	return _issue_session(request, conn, user, client_ip)


@router.post("/mfa/verify")
@limiter.limit(RATE_LIMIT_AUTH)
def mfa_verify(request: Request, payload: MFAVerifyRequest, conn: sqlite3.Connection = Depends(get_conn)):
	client_ip = _get_client_ip(request)
	is_locked, seconds_remaining = is_ip_locked(conn, client_ip)
	if is_locked:
		raise HTTPException(status_code=429, detail="Too many failed attempts.")

	user_id = _consume_mfa_challenge(payload.mfa_token, payload.username, client_ip)
	if user_id is None:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")

	user = get_user_by_id(conn, user_id)
	
	# Timing-safe check: always verify OTP even if user missing/OTP not enabled
	if not user or not user["otp_enabled"]:
		verify_otp(_DUMMY_OTP_SECRET, payload.code)  # burn time
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid OTP code")

	otp_secret = decrypt_otp_secret(user["otp_secret"])
	if not otp_secret or not verify_otp(otp_secret, payload.code):
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid OTP code")

	return _issue_session(request, conn, user, client_ip)


@router.post("/mfa/recovery")
@limiter.limit(RATE_LIMIT_AUTH)
def mfa_recovery(request: Request, payload: MFARecoveryRequest, conn: sqlite3.Connection = Depends(get_conn)):
	client_ip = _get_client_ip(request)

	user_id = _consume_mfa_challenge(payload.mfa_token, payload.username, client_ip)
	if user_id is None:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")

	user = get_user_by_id(conn, user_id)
	if not user:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid recovery code")

	stored_codes = user["otp_recovery_codes"] or "[]"
	valid, remaining_codes = use_recovery_code(stored_codes, payload.recovery_code)
	if not valid:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid recovery code")

	update_user_recovery_codes(conn, user_id, remaining_codes)

	return _issue_session(request, conn, user, client_ip)


@router.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
	# Check cookie first
	token = request.cookies.get(_AUTH_COOKIE)
	
	# If no cookie, check Authorization header
	if not token:
		auth_header = request.headers.get("Authorization", "")
		if auth_header.lower().startswith("bearer "):
			token = auth_header[7:].strip()
	
	if token:
		delete_auth_token(conn, token)
	
	response = ok_response()
	response.delete_cookie(_AUTH_COOKIE, path="/")
	return response
