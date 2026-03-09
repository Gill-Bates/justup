#!/usr/bin/env python3
#
# app/api/recipients.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for managing notification recipients."""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..db import sqlite as sqlite_db
from ..models.recipients import RecipientCreate, RecipientPublic, RecipientResponse, RecipientUpdate, TargetBrief
from ..utils.deps import get_conn
from ..utils.phone import normalize_phone
from .auth import get_current_user

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/recipients", tags=["recipients"])

# Cache for Signal registration status: phone -> (is_registered, timestamp_monotonic)
# Protected by _SIGNAL_CACHE_LOCK for thread-safety across FastAPI workers and ThreadPoolExecutor
_SIGNAL_REGISTRATION_CACHE: dict[str, tuple[bool, float]] = {}
_SIGNAL_CACHE_LOCK = Lock()
_SIGNAL_CACHE_TTL = 86400.0  # 24 hours
_SIGNAL_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth


def _mask_phone(number: str) -> str:
	"""Mask phone number for GDPR-compliant logging/messages."""
	if not number or len(number) < 6:
		return "***"
	return number[:3] + "****" + number[-2:]


def _signal_registration_status(phone: str) -> bool | None:
	"""Check if phone is registered with Signal.
	
	Centralized low-level function used by both warning generation and 
	verification API. Caches results for 24h using time.monotonic().
	Thread-safe via _SIGNAL_CACHE_LOCK.
	
	Args:
		phone: E.164 formatted phone number
		
	Returns:
		True if registered, False if not registered, None if check unavailable
	"""
	now = time.monotonic()
	
	# Check cache first (thread-safe read)
	with _SIGNAL_CACHE_LOCK:
		if phone in _SIGNAL_REGISTRATION_CACHE:
			is_registered, cached_at = _SIGNAL_REGISTRATION_CACHE[phone]
			if (now - cached_at) < _SIGNAL_CACHE_TTL:
				return is_registered
	
	try:
		from ..services.notifications.signal_backend import get_signal_backend
		from ..services.notifications.signal_errors import SignalNotRegistered, SignalUnavailable
		
		backend = get_signal_backend()
		accounts = backend.list_accounts()
		if not accounts:
			_log.debug("No Signal accounts registered, skipping registration check")
			return None  # Signal not configured
		
		sender = accounts[0]
		is_registered = backend.check_user_registered(phone, sender=sender)
		
		# Cache the result (thread-safe write with LRU eviction)
		with _SIGNAL_CACHE_LOCK:
			if len(_SIGNAL_REGISTRATION_CACHE) >= _SIGNAL_CACHE_MAX_SIZE:
				# Remove oldest entry
				oldest_key = min(_SIGNAL_REGISTRATION_CACHE, key=lambda k: _SIGNAL_REGISTRATION_CACHE[k][1])
				del _SIGNAL_REGISTRATION_CACHE[oldest_key]
			_SIGNAL_REGISTRATION_CACHE[phone] = (is_registered, now)
		
		return is_registered
		
	except SignalNotRegistered:
		_log.debug("No Signal sender configured")
		return None
	except SignalUnavailable:
		_log.debug("Signal backend unavailable")
		return None
	except Exception as e:
		_log.debug("Error checking Signal registration: %s", e)
		return None


def _check_signal_registration(phone: str) -> str | None:
	"""Check if phone is registered with Signal, return warning if not.
	
	Uses _signal_registration_status() for actual check.
	
	Returns:
		Warning message if not registered, None if registered or check unavailable
	"""
	registration_status = _signal_registration_status(phone)
	
	# None = check unavailable, True = registered -> no warning
	if registration_status is None or registration_status is True:
		return None
	
	# status is False -> not registered
	return f"The provided phone number ({_mask_phone(phone)}) is not registered with Signal. Signal notifications will fail."


def _row_to_public(row: Mapping[str, Any], targets: Sequence[Mapping[str, Any]] | None = None) -> RecipientPublic:
	return RecipientPublic(
		id=int(row["id"]),
		name=row["name"],
		phone=row["phone"] if row["phone"] else None,
		email=row["email"] if row["email"] else None,
		is_enabled=bool(row["is_enabled"]),
		created_at=row["created_at"],
		updated_at=row["updated_at"],
		targets=[TargetBrief(id=t["id"], name=t["name"]) for t in (targets or [])],
	)


@router.get("", response_model=list[RecipientPublic])
def list_recipients(conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""List all alert recipients."""
	rows = sqlite_db.list_recipients(conn)
	result = []
	for r in rows:
		targets = sqlite_db.get_recipient_targets(conn, r["id"])
		result.append(_row_to_public(r, targets))
	return result


def _is_phone_probably_registered(phone: str) -> bool | None:
	"""Check if a phone is likely registered with Signal.
	
	Wrapper around _signal_registration_status() for tri-state API.
	
	Args:
		phone: E.164 formatted phone number
		
	Returns:
		True if registered, False if not registered, None if check unavailable
	"""
	return _signal_registration_status(phone)


@router.post("/verify-signal")
def verify_signal_numbers(
	phones: list[str],
	_conn=Depends(get_conn),
	_user=Depends(get_current_user)
):
	"""Verify which phone numbers are registered with Signal.
	
	Checks are run in parallel (up to 3 concurrent) to minimize latency.
	Cached results are returned immediately.
	
	Args:
		phones: List of phone numbers to check (will be normalized to E.164)
		
	Returns:
		Dict with:
		- "results": mapping of normalized E.164 phone numbers to registration status
		- "invalid": list of phone numbers that could not be normalized
	"""
	results: dict[str, bool | None] = {}
	invalid_phones: list[str] = []
	phones_to_check: list[str] = []  # normalized phones to check
	
	for phone in phones:
		if not phone:
			continue
		
		# Normalize to E.164 for consistent cache keys
		try:
			normalized = normalize_phone(phone)
		except ValueError:
			# Invalid phone format -> track separately
			invalid_phones.append(phone)
			continue
		
		# Check cache first (fast path, thread-safe)
		with _SIGNAL_CACHE_LOCK:
			if normalized in _SIGNAL_REGISTRATION_CACHE:
				is_registered, cached_at = _SIGNAL_REGISTRATION_CACHE[normalized]
				if (time.monotonic() - cached_at) < _SIGNAL_CACHE_TTL:
					results[normalized] = is_registered
					continue
		
		phones_to_check.append(normalized)
	
	# No uncached phones -> return immediately
	if not phones_to_check:
		return {"results": results, "invalid": invalid_phones}
	
	# Check uncached phones sequentially.
	# signal-cli uses file-level locking, so parallel threads just queue up
	# behind the global process lock.  Sequential is simpler and avoids
	# thread-pool timeout issues.
	for normalized in phones_to_check:
		try:
			is_registered = _is_phone_probably_registered(normalized)
			results[normalized] = is_registered
		except Exception as e:
			_log.warning("Signal check failed for %s: %s", _mask_phone(normalized), e)
			results[normalized] = None
	
	return {"results": results, "invalid": invalid_phones}


@router.get("/{recipient_id}", response_model=RecipientPublic)
def get_recipient(recipient_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Get a single recipient."""
	row = sqlite_db.get_recipient(conn, recipient_id)
	if not row:
		raise HTTPException(status_code=404, detail="Recipient not found")
	targets = sqlite_db.get_recipient_targets(conn, recipient_id)
	return _row_to_public(row, targets)


@router.post("", response_model=RecipientResponse, status_code=status.HTTP_201_CREATED)
def create_recipient(payload: RecipientCreate, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Create a new alert recipient."""
	warnings: list[str] = []
	
	# Validate at least one channel
	if not payload.phone and not payload.email:
		raise HTTPException(status_code=400, detail="At least one contact method (phone or email) is required")
	
	# Normalize phone to E.164 format if provided
	normalized_phone = None
	if payload.phone:
		try:
			normalized_phone = normalize_phone(payload.phone)
		except ValueError as e:
			raise HTTPException(status_code=400, detail=f"Invalid phone number: {e}")
		
		# Check for duplicate phone
		existing = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
		if existing:
			raise HTTPException(status_code=400, detail="Phone number already exists")
		
		# Check if phone is registered with Signal
		signal_warning = _check_signal_registration(normalized_phone)
		if signal_warning:
			warnings.append(signal_warning)
	
	new_id = sqlite_db.create_recipient(
		conn,
		name=payload.name,
		phone=normalized_phone,
		email=payload.email,
		is_enabled=payload.is_enabled,
	)
	row = sqlite_db.get_recipient(conn, new_id)
	recipient = _row_to_public(row, [])
	return RecipientResponse(recipient=recipient, warnings=warnings)


@router.patch("/{recipient_id}", response_model=RecipientResponse)
def update_recipient(recipient_id: int, payload: RecipientUpdate, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Update an alert recipient."""
	warnings: list[str] = []
	clear_phone = False
	clear_email = False
	
	existing = sqlite_db.get_recipient(conn, recipient_id)
	if not existing:
		raise HTTPException(status_code=404, detail="Recipient not found")
	
	update_data = payload.model_dump(exclude_unset=True)
	
	# Normalize phone if provided
	if "phone" in update_data:
		if payload.phone:  # Not empty string or None
			normalized_phone = normalize_phone(payload.phone)
			# Check phone uniqueness if changing
			if normalized_phone != existing["phone"]:
				dup = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
				if dup and dup["id"] != recipient_id:
					raise HTTPException(status_code=400, detail="Phone number already exists")
				
				# Check if new phone is registered with Signal
				signal_warning = _check_signal_registration(normalized_phone)
				if signal_warning:
					warnings.append(signal_warning)
			update_data["phone"] = normalized_phone
		else:
			# Phone is being cleared (null or empty string)
			clear_phone = True
			del update_data["phone"]
	
	# Handle email clearing
	if "email" in update_data:
		if not payload.email:  # Empty string or None means clear
			clear_email = True
			del update_data["email"]
	
	# Validate at least one channel after update
	final_phone = None if clear_phone else update_data.get("phone", existing["phone"])
	final_email = None if clear_email else update_data.get("email", existing["email"])
	if not final_phone and not final_email:
		raise HTTPException(status_code=400, detail="At least one contact method (phone or email) is required")
	
	if update_data or clear_phone or clear_email:
		sqlite_db.update_recipient(conn, recipient_id, clear_phone=clear_phone, clear_email=clear_email, **update_data)
	
	row = sqlite_db.get_recipient(conn, recipient_id)
	targets = sqlite_db.get_recipient_targets(conn, recipient_id)
	recipient = _row_to_public(row, targets)
	return RecipientResponse(recipient=recipient, warnings=warnings)


@router.delete("/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipient(recipient_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Delete an alert recipient."""
	existing = sqlite_db.get_recipient(conn, recipient_id)
	if not existing:
		raise HTTPException(status_code=404, detail="Recipient not found")
	
	sqlite_db.delete_recipient(conn, recipient_id)
	return Response(status_code=status.HTTP_204_NO_CONTENT)
