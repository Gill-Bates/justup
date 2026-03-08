#!/usr/bin/env python3
#
# app/utils/passkeys.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WebAuthn/Passkey utility helpers for registration and authentication."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from webauthn import (
	generate_authentication_options,
	generate_registration_options,
	verify_authentication_response,
	verify_registration_response,
)
from webauthn.helpers import (
	base64url_to_bytes,
	bytes_to_base64url,
)
from webauthn.helpers.structs import (
	AuthenticationCredential,
	AuthenticatorAssertionResponse,
	AuthenticatorAttestationResponse,
	AuthenticatorSelectionCriteria,
	PublicKeyCredentialDescriptor,
	RegistrationCredential,
	ResidentKeyRequirement,
	UserVerificationRequirement,
)

_log = logging.getLogger(__name__)

_MAX_PASSKEYS_PER_USER = int(os.environ.get("MAX_PASSKEYS_PER_USER", "20"))
_CHALLENGE_TTL_SECONDS = 300


class InvalidChallengeError(Exception):
	pass


@dataclass
class PasskeyRegistrationResult:
	credential_id: str
	public_key: bytes
	sign_count: int
	transports: list[str] | None


@dataclass
class PasskeyAuthenticationResult:
	credential_id: str
	new_sign_count: int


def _get_user_handle_secret() -> bytes:
	secret = os.environ.get("JUSTUP_SECRET_KEY", "")
	if not secret:
		raise RuntimeError("JUSTUP_SECRET_KEY not set - required for passkey user handles")
	return secret.encode("utf-8")


def _user_handle_for_id(user_id: int) -> bytes:
	secret = _get_user_handle_secret()
	return hmac.new(secret, str(user_id).encode(), hashlib.sha256).digest()


def _cleanup_expired_challenges(conn: sqlite3.Connection) -> None:
	now = time.time()
	conn.execute("DELETE FROM passkey_challenges WHERE expires_at <= ?", (now,))
	conn.commit()


def store_registration_challenge(
	conn: sqlite3.Connection,
	challenge: str,
	user_id: int,
	username: str,
) -> None:
	now = time.time()
	conn.execute(
		"""
		INSERT OR REPLACE INTO passkey_challenges
		(challenge, ceremony_type, user_id, username, expires_at, created_at)
		VALUES (?, 'registration', ?, ?, ?, ?)
		""",
		(challenge, user_id, username, now + _CHALLENGE_TTL_SECONDS, now),
	)
	conn.commit()
	_cleanup_expired_challenges(conn)


def consume_registration_challenge(conn: sqlite3.Connection, challenge: str) -> tuple[int, str]:
	now = time.time()
	row = conn.execute(
		"""
		SELECT user_id, username FROM passkey_challenges
		WHERE challenge = ? AND ceremony_type = 'registration' AND expires_at > ?
		""",
		(challenge, now),
	).fetchone()
	if not row:
		raise InvalidChallengeError("Registration challenge not found or expired")
	conn.execute("DELETE FROM passkey_challenges WHERE challenge = ?", (challenge,))
	conn.commit()
	return row[0], row[1]


def store_authentication_challenge(
	conn: sqlite3.Connection,
	challenge: str,
	user_id: int | None = None,
) -> None:
	now = time.time()
	conn.execute(
		"""
		INSERT OR REPLACE INTO passkey_challenges
		(challenge, ceremony_type, user_id, username, expires_at, created_at)
		VALUES (?, 'authentication', ?, NULL, ?, ?)
		""",
		(challenge, user_id, now + _CHALLENGE_TTL_SECONDS, now),
	)
	conn.commit()
	_cleanup_expired_challenges(conn)


def consume_authentication_challenge(conn: sqlite3.Connection, challenge: str) -> int | None:
	now = time.time()
	row = conn.execute(
		"""
		SELECT user_id FROM passkey_challenges
		WHERE challenge = ? AND ceremony_type = 'authentication' AND expires_at > ?
		""",
		(challenge, now),
	).fetchone()
	if not row:
		raise InvalidChallengeError("Authentication challenge not found or expired")
	conn.execute("DELETE FROM passkey_challenges WHERE challenge = ?", (challenge,))
	conn.commit()
	return row[0]


def serialize_transports(transports: list | None) -> str | None:
	if transports is None:
		return None
	return ",".join(str(t) for t in transports)


def get_registration_options(
	conn: sqlite3.Connection,
	rp_id: str,
	rp_name: str,
	user_id: int,
	username: str,
	existing_credential_ids: list[str] | None = None,
) -> dict:
	from ..db.sqlite_passkeys import count_user_passkeys

	if count_user_passkeys(conn, user_id) >= _MAX_PASSKEYS_PER_USER:
		raise ValueError(f"Maximum of {_MAX_PASSKEYS_PER_USER} passkeys reached")

	exclude = []
	if existing_credential_ids:
		exclude = [
			PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
			for cid in existing_credential_ids
		]

	options = generate_registration_options(
		rp_id=rp_id,
		rp_name=rp_name,
		user_id=_user_handle_for_id(user_id),
		user_name=username,
		user_display_name=username,
		exclude_credentials=exclude,
		authenticator_selection=AuthenticatorSelectionCriteria(
			resident_key=ResidentKeyRequirement.PREFERRED,
			user_verification=UserVerificationRequirement.PREFERRED,
		),
	)

	challenge_b64 = bytes_to_base64url(options.challenge)
	store_registration_challenge(conn, challenge_b64, user_id, username)

	return {
		"challenge": challenge_b64,
		"rp": {"id": options.rp.id, "name": options.rp.name},
		"user": {
			"id": bytes_to_base64url(options.user.id),
			"name": options.user.name,
			"displayName": options.user.display_name,
		},
		"pubKeyCredParams": [
			{"type": "public-key", "alg": p.alg} for p in options.pub_key_cred_params
		],
		"timeout": options.timeout,
		"excludeCredentials": [
			{"type": "public-key", "id": bytes_to_base64url(c.id)}
			for c in (options.exclude_credentials or [])
		],
		"authenticatorSelection": {
			"residentKey": options.authenticator_selection.resident_key.value
			if options.authenticator_selection
			else "preferred",
			"userVerification": options.authenticator_selection.user_verification.value
			if options.authenticator_selection
			else "preferred",
		},
		"attestation": options.attestation.value if options.attestation else "none",
	}


def get_authentication_options(
	conn: sqlite3.Connection,
	rp_id: str,
	user_id: int | None = None,
	credential_ids: list[str] | None = None,
) -> dict:
	allow = []
	if credential_ids:
		allow = [
			PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
			for cid in credential_ids
		]

	options = generate_authentication_options(
		rp_id=rp_id,
		allow_credentials=allow if allow else None,
		user_verification=UserVerificationRequirement.PREFERRED,
	)

	challenge_b64 = bytes_to_base64url(options.challenge)
	store_authentication_challenge(conn, challenge_b64, user_id)

	return {
		"challenge": challenge_b64,
		"rp_id": rp_id,
		"timeout": options.timeout,
		"user_verification": options.user_verification.value
		if options.user_verification
		else "preferred",
		"allow_credentials": [
			{
				"type": "public-key",
				"id": bytes_to_base64url(c.id),
				"transports": [t.value for t in c.transports] if c.transports else None,
			}
			for c in (options.allow_credentials or [])
		],
	}


def verify_registration(
	credential_json: dict,
	expected_challenge: str,
	expected_origin: str,
	expected_rp_id: str,
) -> PasskeyRegistrationResult:
	response_data = credential_json.get("response", {})
	credential = RegistrationCredential(
		id=credential_json["id"],
		raw_id=base64url_to_bytes(credential_json.get("rawId", credential_json["id"])),
		response=AuthenticatorAttestationResponse(
			client_data_json=base64url_to_bytes(response_data["clientDataJSON"]),
			attestation_object=base64url_to_bytes(response_data["attestationObject"]),
		),
		type=credential_json.get("type", "public-key"),
	)

	result = verify_registration_response(
		credential=credential,
		expected_challenge=base64url_to_bytes(expected_challenge),
		expected_origin=expected_origin,
		expected_rp_id=expected_rp_id,
	)

	transports = None
	raw_transports = response_data.get("transports")
	if isinstance(raw_transports, list):
		transports = [str(t) for t in raw_transports]

	return PasskeyRegistrationResult(
		credential_id=bytes_to_base64url(result.credential_id),
		public_key=result.credential_public_key,
		sign_count=result.sign_count,
		transports=transports,
	)


def verify_authentication(
	credential_json: dict,
	expected_challenge: str,
	expected_origin: str,
	expected_rp_id: str,
	stored_public_key: bytes,
	stored_sign_count: int,
	credential_id_b64: str,
) -> PasskeyAuthenticationResult:
	response_data = credential_json.get("response", {})
	credential = AuthenticationCredential(
		id=credential_json["id"],
		raw_id=base64url_to_bytes(credential_json.get("rawId", credential_json["id"])),
		response=AuthenticatorAssertionResponse(
			client_data_json=base64url_to_bytes(response_data["clientDataJSON"]),
			authenticator_data=base64url_to_bytes(response_data["authenticatorData"]),
			signature=base64url_to_bytes(response_data["signature"]),
			user_handle=base64url_to_bytes(response_data["userHandle"]) if response_data.get("userHandle") else None,
		),
		type=credential_json.get("type", "public-key"),
	)

	result = verify_authentication_response(
		credential=credential,
		expected_challenge=base64url_to_bytes(expected_challenge),
		expected_origin=expected_origin,
		expected_rp_id=expected_rp_id,
		credential_public_key=stored_public_key,
		credential_current_sign_count=stored_sign_count,
	)

	return PasskeyAuthenticationResult(
		credential_id=credential_id_b64,
		new_sign_count=result.new_sign_count,
	)
