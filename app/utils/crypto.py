#!/usr/bin/env python3
#
# app/utils/crypto.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cryptographic helpers for password hashing and token generation."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

DUMMY_PASSWORD_HASH = "pbkdf2:sha256:600000$0000000000000000000000000000000000000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000"


def hash_password(password: str) -> str:
	salt = os.urandom(16)
	iterations = 600_000
	dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
	return f"pbkdf2:sha256:{iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
	try:
		parts = password_hash.split("$")
		if len(parts) != 3:
			return False
		method_parts = parts[0].split(":")
		if len(method_parts) != 3 or method_parts[0] != "pbkdf2":
			return False
		algorithm = method_parts[1]
		iterations = int(method_parts[2])
		salt = bytes.fromhex(parts[1])
		stored_hash = bytes.fromhex(parts[2])
		dk = hashlib.pbkdf2_hmac(algorithm, password.encode("utf-8"), salt, iterations)
		return hmac.compare_digest(dk, stored_hash)
	except (ValueError, IndexError, TypeError):
		return False


def new_token() -> str:
	return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_expired(expires_at: datetime) -> bool:
	return datetime.now(timezone.utc) >= expires_at


def generate_token_expiry(hours: int = 1, max_hours: int = 24) -> tuple[datetime, datetime]:
	now = datetime.now(timezone.utc)
	expires_at = now + timedelta(hours=hours)
	max_expires_at = now + timedelta(hours=max_hours)
	return expires_at, max_expires_at
