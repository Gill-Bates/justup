#!/usr/bin/env python3
#
# app/utils/crypto.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Application-level encryption helpers for secrets stored at rest.

Implementation is in the compiled Cython extension.
"""

from app._cython.crypto import (
	EncryptionError,
	MissingEncryptionKeyError,
	current_key_fingerprint,
	decrypt_secret,
	encrypt_secret,
	generate_encryption_key,
	get_primary_key,
	is_encrypted,
	reset_fernet_cache,
	validate_encryption_key,
)

__all__ = [
	"EncryptionError",
	"MissingEncryptionKeyError",
	"current_key_fingerprint",
	"decrypt_secret",
	"encrypt_secret",
	"generate_encryption_key",
	"get_primary_key",
	"is_encrypted",
	"reset_fernet_cache",
	"validate_encryption_key",
]

