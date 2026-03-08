#!/usr/bin/env python3
#
# app/api/passkeys.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Passkey (WebAuthn) API routes for registration and authentication."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..db.sqlite_auth import (
	clear_login_attempts,
	create_auth_token,
	is_ip_locked,
	record_failed_login,
)
from ..db.sqlite_passkeys import (
	any_passkeys_exist,
	count_user_passkeys,
	create_passkey,
	delete_passkey,
	get_credential_ids_for_user,
	get_passkey_by_credential_id,
	get_passkey_by_id,
	get_passkeys_for_user,
	update_passkey_sign_count,
)
from ..db.sqlite_users import (
	clear_passkey_onboarding,
	disable_user_passkeys,
	get_user_by_id,
	get_user_by_username,
	set_passkey_pending,
	update_last_login,
	update_user_auth_method,
)
from ..utils.crypto import generate_token_expiry, new_token
from ..utils.deps import get_conn
from ..utils.passkeys import (
	consume_authentication_challenge,
	consume_registration_challenge,
	get_authentication_options,
	get_registration_options,
	serialize_transports,
	verify_authentication,
	verify_registration,
	InvalidChallengeError,
)
from ..utils.rate_limit import RATE_LIMIT_AUTH, limiter
from .auth import _get_client_ip, _is_https, get_current_user, require_admin
from .response import ok_response

_log = logging.getLogger(__name__)

router = APIRouter(tags=["passkeys"])

_AUTH_COOKIE = "auth_token"


def _get_rp_id(request: Request) -> str:
	host = request.headers.get("host", "localhost")
	if ":" in host:
		host = host.split(":")[0]
	return host


def _get_origin(request: Request) -> str:
	scheme = "https" if _is_https(request) else "http"
	host = request.headers.get("host", "localhost")
	return f"{scheme}://{host}"


def _get_rp_name() -> str:
	return os.environ.get("PASSKEY_RP_NAME", "justUp")


class PasskeyRegisterFinishRequest(BaseModel):
	credential: dict[str, Any] = Field(...)
	device_name: str | None = Field(None, max_length=100)


class PasskeyLoginStartRequest(BaseModel):
	username: str | None = Field(None, max_length=64)


class PasskeyLoginFinishRequest(BaseModel):
	credential: dict[str, Any] = Field(...)


@router.post("/register/start")
def passkey_register_start(
	request: Request,
	user: sqlite3.Row = Depends(get_current_user),
	conn: sqlite3.Connection = Depends(get_conn),
):
	rp_id = _get_rp_id(request)
	rp_name = _get_rp_name()
	existing_creds = get_credential_ids_for_user(conn, user["id"])
	options = get_registration_options(
		conn=conn, rp_id=rp_id, rp_name=rp_name,
		user_id=user["id"], username=user["username"],
		existing_credential_ids=existing_creds,
	)
	return ok_response(data=options)


@router.post("/register/finish")
def passkey_register_finish(
	request: Request,
	payload: PasskeyRegisterFinishRequest,
	user: sqlite3.Row = Depends(get_current_user),
	conn: sqlite3.Connection = Depends(get_conn),
):
	rp_id = _get_rp_id(request)
	origin = _get_origin(request)

	try:
		import base64, json
		client_data_json = payload.credential.get("response", {}).get("clientDataJSON", "")
		padding = 4 - len(client_data_json) % 4
		if padding != 4:
			client_data_json += "=" * padding
		client_data = json.loads(base64.urlsafe_b64decode(client_data_json))
		challenge = client_data.get("challenge", "")
	except Exception:
		raise HTTPException(status_code=400, detail="Invalid credential response")

	try:
		bound_user_id, bound_username = consume_registration_challenge(conn, challenge)
	except InvalidChallengeError:
		raise HTTPException(status_code=400, detail="Invalid or expired registration challenge")

	if bound_user_id != user["id"]:
		raise HTTPException(status_code=400, detail="Challenge was not issued for this user")

	try:
		result = verify_registration(
			credential_json=payload.credential,
			expected_challenge=challenge,
			expected_origin=origin,
			expected_rp_id=rp_id,
		)
	except Exception:
		raise HTTPException(status_code=400, detail="Passkey registration verification failed")

	passkey_id = create_passkey(
		conn=conn, user_id=user["id"],
		credential_id=result.credential_id, public_key=result.public_key,
		sign_count=result.sign_count, device_name=payload.device_name,
		transports=serialize_transports(result.transports),
	)

	if user["passkey_pending"] and not user["passkey_enabled"]:
		clear_passkey_onboarding(conn, user["id"])
	elif not user["otp_enabled"]:
		update_user_auth_method(conn, user["id"], "passkey", passkey_enabled=True)

	return ok_response(message="Passkey registered successfully", data={"passkey_id": passkey_id})


@router.post("/login/start")
@limiter.limit(RATE_LIMIT_AUTH)
def passkey_login_start(
	request: Request,
	payload: PasskeyLoginStartRequest = None,
	conn: sqlite3.Connection = Depends(get_conn),
):
	client_ip = _get_client_ip(request)
	is_locked, seconds_remaining = is_ip_locked(conn, client_ip)
	if is_locked:
		raise HTTPException(status_code=429, detail="Too many failed attempts.")

	rp_id = _get_rp_id(request)
	user_id = None
	credential_ids = None

	if payload and payload.username:
		user = get_user_by_username(conn, payload.username)
		if user and user["is_active"]:
			user_id = user["id"]
			credential_ids = get_credential_ids_for_user(conn, user_id)

	options = get_authentication_options(
		conn=conn, rp_id=rp_id, user_id=user_id, credential_ids=credential_ids,
	)
	return ok_response(data=options)


@router.post("/login/finish")
@limiter.limit(RATE_LIMIT_AUTH)
def passkey_login_finish(
	request: Request,
	payload: PasskeyLoginFinishRequest,
	conn: sqlite3.Connection = Depends(get_conn),
):
	client_ip = _get_client_ip(request)
	is_locked, seconds_remaining = is_ip_locked(conn, client_ip)
	if is_locked:
		raise HTTPException(status_code=429, detail="Too many failed attempts.")

	rp_id = _get_rp_id(request)
	origin = _get_origin(request)

	credential_id_b64 = payload.credential.get("id", "")
	passkey = get_passkey_by_credential_id(conn, credential_id_b64)
	if not passkey:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Passkey not recognized")

	if not passkey["is_active"]:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=403, detail="Account disabled")

	try:
		import base64, json
		client_data_json = payload.credential.get("response", {}).get("clientDataJSON", "")
		padding = 4 - len(client_data_json) % 4
		if padding != 4:
			client_data_json += "=" * padding
		client_data = json.loads(base64.urlsafe_b64decode(client_data_json))
		challenge = client_data.get("challenge", "")
	except Exception:
		raise HTTPException(status_code=400, detail="Invalid credential response")

	try:
		consume_authentication_challenge(conn, challenge)
	except InvalidChallengeError:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Invalid or expired authentication challenge")

	try:
		result = verify_authentication(
			credential_json=payload.credential,
			expected_challenge=challenge,
			expected_origin=origin,
			expected_rp_id=rp_id,
			stored_public_key=bytes(passkey["public_key"]),
			stored_sign_count=passkey["sign_count"],
			credential_id_b64=credential_id_b64,
		)
	except Exception:
		record_failed_login(conn, client_ip)
		raise HTTPException(status_code=401, detail="Passkey authentication failed")

	update_passkey_sign_count(conn, passkey["id"], result.new_sign_count)

	user_id = passkey["user_id"]
	token = new_token()
	expires_at, max_expires_at = generate_token_expiry()
	create_auth_token(conn, user_id, token, expires_at, max_expires_at)
	clear_login_attempts(conn, client_ip)
	update_last_login(conn, user_id, client_ip)

	response = Response(content='{"status":"ok"}', media_type="application/json")
	is_secure = _is_https(request)
	response.set_cookie(
		key=_AUTH_COOKIE, value=token, httponly=True, samesite="strict",
		secure=is_secure, path="/", max_age=86400,
	)
	return response


@router.get("/list")
def list_passkeys(
	user: sqlite3.Row = Depends(get_current_user),
	conn: sqlite3.Connection = Depends(get_conn),
):
	passkeys = get_passkeys_for_user(conn, user["id"])
	return ok_response(data=[
		{
			"id": p["id"],
			"device_name": p["device_name"],
			"created_at": p["created_at"].isoformat() if p["created_at"] else None,
			"transports": p["transports"].split(",") if p["transports"] else None,
		}
		for p in passkeys
	])


@router.delete("/{passkey_id}")
def remove_passkey(
	passkey_id: int,
	user: sqlite3.Row = Depends(get_current_user),
	conn: sqlite3.Connection = Depends(get_conn),
):
	if not delete_passkey(conn, passkey_id, user["id"]):
		raise HTTPException(status_code=404, detail="Passkey not found")
	return ok_response(message="Passkey deleted")


@router.get("/available")
def passkeys_available(conn: sqlite3.Connection = Depends(get_conn)):
	return ok_response(data={"available": any_passkeys_exist(conn)})
