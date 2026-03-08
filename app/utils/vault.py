#!/usr/bin/env python3
#
# app/utils/vault.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Fernet-based encryption for secrets at rest."""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger(__name__)

_VAULT_PREFIX = "vault:1:"


def _derive_key(pepper: str, salt: bytes) -> bytes:
	dk = hashlib.pbkdf2_hmac("sha256", pepper.encode("utf-8"), salt, iterations=480_000)
	return base64.urlsafe_b64encode(dk)


def encrypt(plaintext: str, pepper: str) -> str:
	if not pepper:
		raise ValueError("JUSTUP_SECRET_KEY is not set")
	salt = os.urandom(16)
	key = _derive_key(pepper, salt)
	f = Fernet(key)
	token = f.encrypt(plaintext.encode("utf-8"))
	return f"{_VAULT_PREFIX}{salt.hex()}:{token.decode('ascii')}"


def decrypt(ciphertext: str, pepper: str) -> str:
	if not pepper:
		raise ValueError("JUSTUP_SECRET_KEY is not set")
	if not ciphertext.startswith(_VAULT_PREFIX):
		raise ValueError("Not a vault-encrypted value")
	payload = ciphertext[len(_VAULT_PREFIX):]
	salt_hex, fernet_token = payload.split(":", 1)
	salt = bytes.fromhex(salt_hex)
	key = _derive_key(pepper, salt)
	f = Fernet(key)
	return f.decrypt(fernet_token.encode("ascii")).decode("utf-8")


def is_encrypted(value: str) -> bool:
	return value.startswith(_VAULT_PREFIX)
