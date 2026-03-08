#!/usr/bin/env python3
#
# app/api/users.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User management API routes."""

from __future__ import annotations

import base64
import io
import logging
import sqlite3

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..db.sqlite_auth import delete_user_tokens
from ..db.sqlite_users import (
	LastAdminError,
	UpdateResult,
	confirm_user_otp,
	count_admins,
	create_user as db_create_user,
	decrypt_otp_secret,
	delete_user as db_delete_user,
	disable_user_otp,
	get_all_users,
	get_user_by_id,
	set_user_otp_secret,
	update_user as db_update_user,
)
from ..models.users import (
	AdminPasswordResetRequest,
	OTPConfirmRequest,
	PasswordChangeRequest,
	UserCreate,
	UserPublic,
	UserUpdate,
)
from ..utils.crypto import verify_password
from ..utils.deps import get_conn
from ..utils.otp import (
	build_provisioning_uri,
	generate_otp_secret,
	generate_recovery_codes,
	serialize_recovery_codes,
	verify_otp,
)
from ..utils.rate_limit import RATE_LIMIT_AUTH, limiter
from .auth import get_current_user, require_admin
from .response import ok_response

_log = logging.getLogger(__name__)

router = APIRouter(tags=["users"])


def _to_qr_data_url(content: str) -> str:
	img = qrcode.make(content)
	buffer = io.BytesIO()
	img.save(buffer, format="PNG")
	encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
	return f"data:image/png;base64,{encoded}"


def _row_to_public(row: sqlite3.Row) -> UserPublic:
	return UserPublic(
		id=row["id"],
		username=row["username"],
		is_admin=bool(row["is_admin"]),
		is_active=bool(row["is_active"]),
		otp_enabled=bool(row["otp_enabled"]),
		created_at=row["created_at"],
		last_login_at=row["last_login_at"],
		last_login_ip=row["last_login_ip"],
	)


@router.get("")
def list_users(conn: sqlite3.Connection = Depends(get_conn), _: sqlite3.Row = Depends(require_admin)):
	rows = get_all_users(conn)
	return ok_response(data=[_row_to_public(row) for row in rows])


@router.post("", status_code=201)
def create_user(
	payload: UserCreate,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(require_admin),
):
	try:
		user_id = db_create_user(conn, username=payload.username, password=payload.password, is_admin=payload.is_admin)
	except ValueError as e:
		raise HTTPException(status_code=422, detail=str(e))
	if user_id is None:
		raise HTTPException(status_code=409, detail="Username already exists")
	user = get_user_by_id(conn, user_id)
	return ok_response(data=_row_to_public(user))


@router.get("/{user_id}")
def get_user(
	user_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	if current_user["id"] != user_id and not current_user["is_admin"]:
		raise HTTPException(status_code=403, detail="Not authorized")
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")
	return ok_response(data=_row_to_public(user))


@router.patch("/{user_id}")
def update_user(
	user_id: int,
	payload: UserUpdate,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	is_self = current_user["id"] == user_id
	is_admin = bool(current_user["is_admin"])
	if not is_self and not is_admin:
		raise HTTPException(status_code=403, detail="Not authorized")
	if not is_admin and (payload.is_admin is not None or payload.is_active is not None):
		raise HTTPException(status_code=403, detail="Cannot change admin/active status")
	if is_self and payload.is_admin is False:
		raise HTTPException(status_code=400, detail="Cannot remove your own admin rights")
	if is_self and payload.is_active is False:
		raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

	result = db_update_user(conn, user_id, username=payload.username, is_admin=payload.is_admin, is_active=payload.is_active)
	if result == UpdateResult.NOT_FOUND:
		raise HTTPException(status_code=404, detail="User not found")
	if result == UpdateResult.CONFLICT:
		raise HTTPException(status_code=409, detail="Username already exists")
	if result == UpdateResult.LAST_ADMIN:
		raise HTTPException(status_code=400, detail="Cannot remove the last admin")

	updated = get_user_by_id(conn, user_id)
	return ok_response(data=_row_to_public(updated))


@router.delete("/{user_id}", status_code=204, response_class=Response)
def delete_user(
	user_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(require_admin),
):
	if current_user["id"] == user_id:
		raise HTTPException(status_code=400, detail="Cannot delete yourself")
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")
	if user["is_admin"] and count_admins(conn) <= 1:
		raise HTTPException(status_code=400, detail="Cannot delete the last admin")
	try:
		db_delete_user(conn, user_id)
	except LastAdminError:
		raise HTTPException(status_code=400, detail="Cannot delete the last admin")
	delete_user_tokens(conn, user_id)
	return Response(status_code=204)


@router.post("/{user_id}/change-password")
@limiter.limit(RATE_LIMIT_AUTH)
def change_password(
	request: Request,
	user_id: int,
	payload: PasswordChangeRequest,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	if current_user["id"] != user_id:
		raise HTTPException(status_code=403, detail="Not authorized")
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")
	if not verify_password(payload.current_password, user["password_hash"]):
		raise HTTPException(status_code=422, detail="Current password incorrect")
	db_update_user(conn, user_id, password=payload.new_password)
	delete_user_tokens(conn, user_id)
	return ok_response(message="Password changed successfully")


@router.post("/{user_id}/reset-password")
def reset_password(
	user_id: int,
	payload: AdminPasswordResetRequest,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(require_admin),
):
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")
	db_update_user(conn, user_id, password=payload.new_password)
	delete_user_tokens(conn, user_id)
	return ok_response(message="Password reset successfully")


@router.post("/{user_id}/otp/enable")
def enable_user_otp(
	user_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	if current_user["id"] != user_id and not current_user["is_admin"]:
		raise HTTPException(status_code=403, detail="Not authorized")
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")
	if user["otp_enabled"]:
		raise HTTPException(status_code=400, detail="OTP already enabled")

	secret = generate_otp_secret()
	set_user_otp_secret(conn, user_id, secret)
	uri = build_provisioning_uri(secret, user["username"])
	qr_url = _to_qr_data_url(uri)

	return ok_response(data={"secret": secret, "qr_code": qr_url, "provisioning_uri": uri})


@router.post("/{user_id}/otp/confirm")
def confirm_otp(
	user_id: int,
	payload: OTPConfirmRequest,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	if current_user["id"] != user_id and not current_user["is_admin"]:
		raise HTTPException(status_code=403, detail="Not authorized")
	user = get_user_by_id(conn, user_id)
	if not user:
		raise HTTPException(status_code=404, detail="User not found")

	otp_secret = decrypt_otp_secret(user["otp_secret"])
	if not otp_secret:
		raise HTTPException(status_code=400, detail="OTP not set up")
	if not verify_otp(otp_secret, payload.code):
		raise HTTPException(status_code=422, detail="Invalid OTP code")

	codes = generate_recovery_codes()
	from ..db.sqlite_users import update_user_recovery_codes
	update_user_recovery_codes(conn, user_id, serialize_recovery_codes(codes))
	confirm_user_otp(conn, user_id)

	return ok_response(data={"recovery_codes": codes})


@router.post("/{user_id}/otp/disable")
def disable_otp(
	user_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	current_user: sqlite3.Row = Depends(get_current_user),
):
	if current_user["id"] != user_id and not current_user["is_admin"]:
		raise HTTPException(status_code=403, detail="Not authorized")
	disable_user_otp(conn, user_id)
	return ok_response(message="OTP disabled")
