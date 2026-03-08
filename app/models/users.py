#!/usr/bin/env python3
#
# app/models/users.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User-related Pydantic models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")
_CONSECUTIVE_SPECIAL_RE = re.compile(r"[-_]{2}")
_UPPER_RE = re.compile(r"[A-Z]")
_LOWER_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")
_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")
_COMMON_PASSWORDS = {
	"password", "password123", "qwerty", "qwerty123",
	"admin", "admin123", "letmein", "welcome",
	"12345678", "123456789",
}
_PASSWORD_MAX_BYTES = 72


def _validate_username(v: str) -> str:
	v_lower = v.strip().lower()
	if not v_lower or not v_lower.isprintable():
		raise ValueError("Invalid username")
	if not _USERNAME_RE.match(v_lower):
		raise ValueError(
			"Username must be 3-64 chars, start/end with alphanumeric, "
			"and contain only letters, digits, hyphens, or underscores"
		)
	if _CONSECUTIVE_SPECIAL_RE.search(v_lower):
		raise ValueError("Username cannot contain consecutive special characters")
	return v_lower


def _validate_password_strength(v: str) -> str:
	if len(v.encode("utf-8")) > _PASSWORD_MAX_BYTES:
		raise ValueError("Password must be at most 72 bytes")
	if v.lower() in _COMMON_PASSWORDS:
		raise ValueError("Password is too common")
	checks = (_UPPER_RE, _LOWER_RE, _DIGIT_RE, _SPECIAL_RE)
	if sum(bool(regex.search(v)) for regex in checks) < 3:
		raise ValueError(
			"Password must contain at least 3 of: uppercase, lowercase, digit, special character"
		)
	return v


class LoginRequest(BaseModel):
	username: str = Field(..., min_length=1, max_length=64)
	password: str = Field(..., min_length=1, max_length=256)


class MFAVerifyRequest(BaseModel):
	username: str = Field(..., min_length=1, max_length=64)
	mfa_token: str = Field(..., min_length=1)
	code: str = Field(..., min_length=6, max_length=8)


class OTPConfirmRequest(BaseModel):
	code: str = Field(..., min_length=6, max_length=8)


class RecoveryDownloadRequest(BaseModel):
	download_token: str = Field(..., min_length=1)


class UserCreate(BaseModel):
	username: str = Field(..., min_length=3, max_length=64)
	password: str = Field(..., min_length=8, max_length=256)
	is_admin: bool = False

	@field_validator("username")
	@classmethod
	def validate_username(cls, v: str) -> str:
		return _validate_username(v)

	@field_validator("password")
	@classmethod
	def validate_password(cls, v: str) -> str:
		return _validate_password_strength(v)


class UserUpdate(BaseModel):
	username: str | None = Field(None, min_length=3, max_length=64)
	is_admin: bool | None = None
	is_active: bool | None = None

	@field_validator("username")
	@classmethod
	def validate_username(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_username(v)
		return v


class PasswordChangeRequest(BaseModel):
	current_password: str = Field(..., min_length=1)
	new_password: str = Field(..., min_length=8, max_length=256)

	@field_validator("new_password")
	@classmethod
	def validate_password(cls, v: str) -> str:
		return _validate_password_strength(v)


class AdminPasswordResetRequest(BaseModel):
	new_password: str = Field(..., min_length=8, max_length=256)

	@field_validator("new_password")
	@classmethod
	def validate_password(cls, v: str) -> str:
		return _validate_password_strength(v)


class UserPublic(BaseModel):
	id: int
	username: str
	is_admin: bool
	is_active: bool
	otp_enabled: bool
	created_at: datetime | None = None
	last_login_at: datetime | None = None
	last_login_ip: str | None = None
