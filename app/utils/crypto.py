#!/usr/bin/env python3
#
# app/utils/crypto.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Application-level encryption helpers for secrets stored at rest."""

from __future__ import annotations

import logging
import os
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_log = logging.getLogger(__name__)

# Environment variables
_ENV_KEY_NAME = "SECRET_KEY"
_ENV_KEYS_NAME = "SECRET_KEYS"  # Comma-separated for rotation

# Cached Fernet/MultiFernet instance
_fernet_instance: Fernet | MultiFernet | None = None

# Locks for thread-safety in shared instances
_fernet_lock = Lock()
_primary_lock = Lock()

# Cached primary key for HMAC operations
_primary_key: bytes | None = None


class MissingEncryptionKeyError(Exception):
    """Raised when SECRET_KEY is not configured."""


def _get_encryption_keys() -> list[bytes]:
    """
    Get encryption key(s) from environment.

    Priority:
    1. SECRET_KEYS - Comma-separated list (first = primary for encrypt)
    2. SECRET_KEY - Single key

    Returns list of key bytes.
    Raises MissingEncryptionKeyError if no key is configured.
    """

    def _validate_key(key: bytes) -> bytes:
        try:
            Fernet(key)  # validates length and urlsafe-b64 format
            return key
        except Exception as e:
            raise MissingEncryptionKeyError(
                "Invalid Fernet key format. Provide a urlsafe_base64-encoded 32-byte key (44 chars)."
            ) from e

    # Multiple keys (rotation support)
    keys_str = os.environ.get(_ENV_KEYS_NAME, "").strip()
    if keys_str:
        keys = [_validate_key(k.strip().encode("utf-8")) for k in keys_str.split(",") if k.strip()]
        if keys:
            return keys

    # Single key
    key_str = os.environ.get(_ENV_KEY_NAME, "").strip()
    if key_str:
        return [_validate_key(key_str.encode("utf-8"))]

    raise MissingEncryptionKeyError(
        f"Environment variable {_ENV_KEY_NAME} is required but not set. "
        "Generate a key with: head -c 32 /dev/urandom | base64"
    )


def validate_encryption_key() -> None:
    """
    Validate that SECRET_KEY is configured.

    Call this at application startup to fail fast if key is missing.
    Raises MissingEncryptionKeyError if not configured.
    """
    _get_encryption_keys()
    _log.debug("Encryption key validated")


def get_primary_key() -> bytes:
    """
    Get the primary encryption key for HMAC operations.

    Used as pepper for token hashing and other HMAC operations.
    Caches the key after first call.
    """
    global _primary_key
    if _primary_key is not None:
        return _primary_key
    with _primary_lock:
        if _primary_key is None:
            base_key = _get_encryption_keys()[0]
            _primary_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b"upmon-hmac-pepper",
            ).derive(base_key)
        return _primary_key


def derive_domain_keys(info: bytes) -> list[bytes]:
    """Derive one 32-byte key per configured SECRET_KEY(S) entry, separated by ``info`` (primary first)."""
    return [
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(base_key)
        for base_key in _get_encryption_keys()
    ]


def _init_fernet() -> Fernet | MultiFernet:
    """
    Initialize the Fernet instance.

    Uses configured key(s) from environment.
    Raises MissingEncryptionKeyError if no key is configured.
    """
    keys = _get_encryption_keys()  # Raises if not configured

    # Use configured key(s)
    if len(keys) == 1:
        return Fernet(keys[0])
    # MultiFernet: first key for encryption, all keys for decryption
    fernets = [Fernet(k) for k in keys]
    return MultiFernet(fernets)


def _get_fernet() -> Fernet | MultiFernet:
    """Get or create the Fernet instance."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    with _fernet_lock:
        if _fernet_instance is None:
            _fernet_instance = _init_fernet()
        return _fernet_instance


def encrypt_secret(plaintext: str) -> str:
    """
    Encrypt a secret string for storage.

    Returns a prefixed string: "enc$<base64-ciphertext>"
    """
    if not plaintext:
        return ""

    fernet = _get_fernet()
    ciphertext = fernet.encrypt(plaintext.encode("utf-8"))
    return f"enc${ciphertext.decode('utf-8')}"


def decrypt_secret(stored: str) -> str:
    """
    Decrypt a stored secret string.

    Handles only "enc$...". Legacy formats are rejected.

    Raises:
    - ValueError: Wrong key, corrupted data, or unsupported format
    """
    if not stored:
        return ""

    if stored.startswith("enc$"):
        fernet = _get_fernet()
        try:
            ciphertext = stored[4:].encode("utf-8")
            return fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise ValueError(
                "Failed to decrypt secret - wrong key or corrupted data. "
                "If you changed the encryption key, ensure old key is in SECRET_KEYS for rotation."
            ) from None

    # Legacy or unsupported formats are rejected
    raise ValueError("Secret uses unsupported format. Please re-enter the secret to store it encrypted (enc$...).")


def reset_fernet_cache() -> None:
    """Reset the cached Fernet instance and derived primary key (for testing/key changes)."""
    global _fernet_instance, _primary_key
    with _fernet_lock:
        _fernet_instance = None
    with _primary_lock:
        _primary_key = None
