# cython: language_level=3
# Safety checks controlled by setup_cython.py (_SAFETY_CRITICAL list)
# Do NOT add boundscheck/wraparound/cdivision directives here!

"""Application-level encryption helpers for secrets stored at rest."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from threading import Lock
from typing import Optional, Union

# We use cryptography's Fernet for symmetric encryption.
try:
	from cryptography.fernet import Fernet, InvalidToken, MultiFernet
	from cryptography.hazmat.primitives import hashes
	from cryptography.hazmat.primitives.kdf.hkdf import HKDF

	_HAS_FERNET = True
except ImportError:
	_HAS_FERNET = False
	InvalidToken = Exception  # type: ignore
	MultiFernet = None  # type: ignore

_log = logging.getLogger(__name__)

# Environment variables
_ENV_KEY_NAME = "UPMON_ENCRYPTION_KEY"
_ENV_KEYS_NAME = "UPMON_ENCRYPTION_KEYS"  # Comma-separated for rotation

# Cached Fernet/MultiFernet instance
_fernet_instance: Optional[Union["Fernet", "MultiFernet"]] = None

# Locks for thread-safety in shared instances
_fernet_lock = Lock()
_primary_lock = Lock()

# Cached primary key for HMAC operations
_primary_key: Optional[bytes] = None


class EncryptionError(Exception):
	"""Raised when encryption/decryption fails."""

	pass


class MissingEncryptionKeyError(Exception):
	"""Raised when UPMON_ENCRYPTION_KEY is not configured."""

	pass


def _get_encryption_keys() -> list[bytes]:
	"""
	Get encryption key(s) from environment.

	Priority:
	1. UPMON_ENCRYPTION_KEYS - Comma-separated list (first = primary for encrypt)
	2. UPMON_ENCRYPTION_KEY - Single key

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

	# Check for multiple keys (rotation support)
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
	Validate that UPMON_ENCRYPTION_KEY is configured.

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


def current_key_fingerprint(length: int = 12) -> str:
	"""Return a short fingerprint of the primary key (for debugging)."""
	return base64.urlsafe_b64encode(hashlib.sha256(_get_encryption_keys()[0]).digest())[:length].decode("utf-8")


def _init_fernet() -> Union["Fernet", "MultiFernet"]:
	"""
	Initialize the Fernet instance.

	Uses configured key(s) from environment.
	Raises MissingEncryptionKeyError if no key is configured.
	"""
	if not _HAS_FERNET:
		raise EncryptionError(
			"cryptography package not installed. "
			"Install with: pip install cryptography"
		)

	keys = _get_encryption_keys()  # Raises if not configured

	# Use configured key(s)
	if len(keys) == 1:
		return Fernet(keys[0])
	else:
		# MultiFernet: first key for encryption, all keys for decryption
		fernets = [Fernet(k) for k in keys]
		return MultiFernet(fernets)


def _get_fernet() -> Union["Fernet", "MultiFernet"]:
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
				"If you changed the encryption key, ensure old key is in UPMON_ENCRYPTION_KEYS for rotation."
			)

	# Legacy or unsupported formats are rejected
	raise ValueError("Secret uses unsupported format. Please re-enter the secret to store it encrypted (enc$...).")


def is_encrypted(stored: str) -> bool:
	"""Check if a stored value is properly encrypted (not legacy format)."""
	if not stored:
		return True  # Empty is fine
	return stored.startswith("enc$")


def generate_encryption_key() -> str:
	"""
	Generate a new Fernet encryption key.

	Use this to generate a key for UPMON_ENCRYPTION_KEY environment variable.
	"""
	if not _HAS_FERNET:
		raise EncryptionError("cryptography package not installed")
	return Fernet.generate_key().decode("utf-8")


def reset_fernet_cache() -> None:
	"""Reset the cached Fernet instance (for testing)."""
	global _fernet_instance
	_fernet_instance = None

