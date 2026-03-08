#!/usr/bin/env python3
#
# app/utils/otp.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TOTP/OTP utility helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets

import pyotp

_OTP_RE = re.compile(r"^\d{6,8}$")
_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


def _normalize_recovery_code(code: str) -> str:
	return code.strip().lower()


def _hash_recovery_code(code: str) -> str:
	normalized = _normalize_recovery_code(code)
	return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_sha256_hex(value: str) -> bool:
	return bool(_SHA256_HEX_RE.fullmatch(value))


def generate_otp_secret() -> str:
	return pyotp.random_base32()


def build_provisioning_uri(secret: str, username: str, issuer: str = "justUp") -> str:
	totp = pyotp.TOTP(secret)
	return totp.provisioning_uri(name=username, issuer_name=issuer)


def verify_otp(secret: str, code: str) -> bool:
	if not _OTP_RE.fullmatch(str(code or "").strip()):
		return False
	totp = pyotp.TOTP(secret)
	return bool(totp.verify(str(code).strip(), valid_window=0))


def generate_recovery_codes(n: int = 8, byte_length: int = 8) -> list[str]:
	count = int(n)
	length = int(byte_length)
	if count <= 0 or length <= 0:
		return []
	return [secrets.token_hex(length) for _ in range(count)]


def serialize_recovery_codes(codes: list[str]) -> str:
	hashed = [_hash_recovery_code(code) for code in codes if _normalize_recovery_code(str(code))]
	return json.dumps(hashed)


def deserialize_recovery_codes(raw: str | None) -> list[str]:
	if not raw:
		return []
	try:
		loaded = json.loads(raw)
	except (json.JSONDecodeError, TypeError):
		return []
	if not isinstance(loaded, list):
		return []
	return [str(c) for c in loaded if isinstance(c, str)]


def use_recovery_code(stored_codes_json: str, submitted_code: str) -> tuple[bool, str]:
	codes = deserialize_recovery_codes(stored_codes_json)
	if not codes:
		return False, stored_codes_json or "[]"
	submitted_hash = _hash_recovery_code(submitted_code)
	if _is_sha256_hex(submitted_hash) and submitted_hash in codes:
		codes.remove(submitted_hash)
		return True, json.dumps(codes)
	return False, stored_codes_json or "[]"
