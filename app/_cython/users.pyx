# cython: language_level=3
# Safety checks controlled by setup_cython.py (_SAFETY_CRITICAL list)
# Do NOT add boundscheck/wraparound/cdivision directives here!

"""User-related Pydantic models and authentication helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field, field_validator

from app.utils.crypto import get_primary_key


class UserCreate(BaseModel):
	"""Request model for creating a user."""
	username: str = Field(min_length=1, max_length=64)
	password: str = Field(min_length=8, max_length=256)
	is_admin: bool = False
	can_close_alerts: bool = False

	@field_validator("username")
	@classmethod
	def normalize_username(cls, v: str) -> str:
		return v.strip().lower()


class UserPublic(BaseModel):
	"""Public user representation returned by the API."""
	id: int
	username: str
	is_admin: bool
	can_close_alerts: bool = False
	is_active: bool = True
	created_at: datetime
	last_login_at: datetime | None = None
	last_login_ip: str | None = None


class UserUpdate(BaseModel):
	"""Request model for updating a user (admin-only).
	
	Note: username is required for PUT semantics. For partial updates,
	a PATCH endpoint with optional fields would be more appropriate.
	"""
	username: str = Field(min_length=1, max_length=64)
	is_admin: bool = False
	can_close_alerts: bool = False
	new_password: str | None = Field(default=None, min_length=8, max_length=256)

	@field_validator("username")
	@classmethod
	def normalize_username(cls, v: str) -> str:
		return v.strip().lower()


class LoginRequest(BaseModel):
	"""Request body for user login."""
	username: str
	password: str

	@field_validator("username")
	@classmethod
	def normalize_username(cls, v: str) -> str:
		return v.strip().lower()


class PasswordChangeRequest(BaseModel):
	"""Request body for changing a user's password."""
	current_password: str = Field(min_length=8, max_length=256)
	new_password: str = Field(min_length=8, max_length=256)


class TokenResponse(BaseModel):
	"""Token response returned after successful authentication."""
	access_token: str
	token_type: str = "bearer"
	expires_at: datetime


@dataclass(frozen=True)
class PasswordHash:
	"""Parsed representation of a stored password hash."""
	algo: str
	iterations: int
	salt_b64: str
	hash_b64: str

	def to_storage(self) -> str:
		return f"{self.algo}${self.iterations}${self.salt_b64}${self.hash_b64}"

	@classmethod
	def from_storage(cls, value: str) -> "PasswordHash":
		try:
			algo, iter_s, salt_b64, hash_b64 = value.split("$", 3)
			return cls(algo=algo, iterations=int(iter_s), salt_b64=salt_b64, hash_b64=hash_b64)
		except (ValueError, AttributeError):
			raise ValueError("Invalid password hash format")


def _b64(data: bytes) -> str:
	"""Encode bytes to URL-safe base64 without padding."""
	return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
	"""Decode URL-safe base64, returning b"" on invalid input."""
	try:
		pad = "=" * ((4 - (len(value) % 4)) % 4)
		return base64.urlsafe_b64decode((value + pad).encode("ascii"))
	except Exception:
		return b""


def hash_password(password: str, *, iterations: int = 300_000) -> str:
	"""Hash a plaintext password using PBKDF2-HMAC-SHA256.
	
	Args:
		password: Plaintext password to hash
		iterations: Number of PBKDF2 iterations (default: 300,000)
		
	Returns:
		Stored hash in format: algo$iterations$salt_b64$hash_b64
	"""
	salt = secrets.token_bytes(16)
	dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
	ph = PasswordHash(algo="pbkdf2_sha256", iterations=iterations, salt_b64=_b64(salt), hash_b64=_b64(dk))
	return ph.to_storage()


def verify_password(password: str, stored: str) -> bool:
	"""Verify a plaintext password against a stored hash.
	
	Args:
		password: Plaintext password to verify
		stored: Stored hash in format algo$iterations$salt_b64$hash_b64
		
	Returns:
		True if password matches, False otherwise
	"""
	try:
		ph = PasswordHash.from_storage(stored)
	except ValueError:
		return False

	# Timing-safe algorithm comparison
	if not hmac.compare_digest(ph.algo, "pbkdf2_sha256"):
		return False

	salt = _unb64(ph.salt_b64)
	if not salt:
		return False

	try:
		expected = _unb64(ph.hash_b64)
		if not expected:
			return False
		actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ph.iterations)
		return hmac.compare_digest(expected, actual)
	except Exception:
		return False


def new_token(
	*, 
	sliding_expiry_minutes: int = 60 * 6,
	max_expiry_minutes: int = 60 * 24
) -> tuple[str, datetime, datetime]:
	"""Generate a new opaque token with sliding and maximum expiry timestamps.
	
	Args:
		sliding_expiry_minutes: Minutes until token expires (renewable on each use)
		max_expiry_minutes: Absolute maximum lifetime from creation
		
	Returns:
		Tuple of (token, sliding_expires_at, max_expires_at)
	"""
	token = secrets.token_urlsafe(32)
	now = datetime.now(tz=timezone.utc)
	sliding_expires_at = now + timedelta(minutes=sliding_expiry_minutes)
	max_expires_at = now + timedelta(minutes=max_expiry_minutes)
	return token, sliding_expires_at, max_expires_at


def hash_token(token: str) -> str:
	"""Hash a token using HMAC-SHA256 with an HKDF-derived pepper.
	
	The pepper is derived from the primary key using HKDF, providing
	additional security beyond standard hashing. Tokens are never stored
	in plaintext, only their HMAC hashes.
	
	Args:
		token: Plaintext token to hash
		
	Returns:
		Hex-encoded HMAC-SHA256 hash
	"""
	return hmac.new(
		get_primary_key(),
		token.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


def token_expired(expires_at: datetime) -> bool:
	"""Return True if the token expiry timestamp is in the past.
	
	Args:
		expires_at: Token expiration timestamp (timezone-aware)
		
	Returns:
		True if token has expired, False otherwise
	"""
	if expires_at.tzinfo is None:
		expires_at = expires_at.replace(tzinfo=timezone.utc)
	return datetime.now(tz=timezone.utc) >= expires_at
