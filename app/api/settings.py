#!/usr/bin/env python3
#
# app/api/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Settings and maintenance API routes (admin-only)."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import signal
import tarfile
import tempfile
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..models.settings import Settings, SettingsUpdate
from ..utils.settings_cache import is_purge_enabled
from ..utils.deps import get_tsdb_dir
from ..models.users import verify_password
from ..utils.deps import get_conn
from ..utils.rate_limit import limiter, RATE_LIMIT_HEAVY
from ..utils.constants import SettingKeys
from ..utils.time import utcnow
from .auth import require_admin
from ..services.notifications.email_template import DEFAULT_SUBJECT, DEFAULT_HTML_TEMPLATE


def _mask_phone(number: str | None) -> str:
    """Mask phone number for GDPR-compliant logging (e.g., +49****12 for +4912345612)."""
    if not number or len(number) < 6:
        return "***"
    return number[:3] + "****" + number[-2:]


def _get_client_ip(request: Request) -> str:
    """
    Extract client IP from request, handling reverse proxy headers.
    
    X-Forwarded-For can contain multiple IPs: 'client, proxy1, proxy2'
    We take only the first (leftmost) which is the original client.
    
    Note: Requires trusted reverse proxy (Caddy/nginx) to set X-Forwarded-For.
    If deployed without proxy, this will use request.client.host directly.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        candidate = xff.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass

    candidate = request.client.host if request.client and request.client.host else None
    if candidate:
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return "unknown"


# Valid PDF page sizes for reports
VALID_PDF_PAGE_SIZES = {"a4", "letter"}

# Directories to include in backup (relative to data_dir)
# Only these folders are backed up and restored, everything else is ignored
BACKUP_DIRECTORIES = ("sqlite", "tsdb", "pdf_reports")
MAX_BACKUP_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
BACKUP_HMAC_SECRET_ENV = "JUSTUP_BACKUP_HMAC_SECRET"


def _get_backup_hmac_secret(conn) -> str:
    """
    Return backup HMAC secret.

    Priority:
    1) Environment variable (preferred, not part of backup archive)
    2) Database setting (legacy fallback for backward compatibility)
    """
    env_secret = os.getenv(BACKUP_HMAC_SECRET_ENV)
    if env_secret:
        return env_secret

    import secrets

    secret = sqlite_db.get_setting(conn, "backup_hmac_secret")
    if not secret:
        secret = secrets.token_hex(32)  # 256-bit key
        sqlite_db.set_setting(conn, "backup_hmac_secret", secret)
    return secret


def _compute_backup_hmac(filepath: Path, conn) -> str:
    """
    Compute HMAC of backup file using instance-specific secret.
    
    Uses streaming to avoid loading large backups into memory.
    The secret key is unique per installation, preventing backup forgery.
    
    Returns: 16-character hex HMAC signature
    """
    import hmac

    # Get HMAC key (prefer env var outside backup archive)
    secret = _get_backup_hmac_secret(conn)

    # Streaming HMAC calculation
    h = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()[:16]


def _safe_tar_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """
    Safely extract tar archive, preventing path traversal attacks.
    
    Uses Python 3.12+ filter='data' when available, falls back to manual checks.
    Raises ValueError if any member attempts to escape the destination directory
    or if any symlinks are present (known TAR exploit vector).
    """
    import sys

    if sys.version_info >= (3, 12):
        # Modern Python: use built-in security filter
        tar.extractall(dest, filter="data")
    else:
        # Legacy: manual security checks
        dest = dest.resolve()
        members = tar.getmembers()
        for member in members:
            # Block symlinks entirely - they can point outside dest even after extraction
            if member.issym() or member.islnk():
                raise ValueError(f"Symlinks are not allowed in backups: {member.name}")
            # Normalize and resolve the target path
            member_path = (dest / member.name).resolve()
            # Ensure it's within the destination (using parents to avoid /data/foo2 vs /data/foo issue)
            if dest not in member_path.parents and member_path != dest:
                raise ValueError(f"Path traversal attempt detected: {member.name}")
        # All members are safe, extract
        tar.extractall(dest, members=members)


def _verify_admin_password(conn, admin: dict, password: str) -> None:
    """
    Verify admin user's password for destructive operations.
    
    Raises HTTPException(401) if password is invalid.
    Centralized to ensure consistent security checks.
    """
    user = sqlite_db.get_user_by_id(conn, admin["id"])
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid password")


router = APIRouter(prefix="/settings", tags=["settings"])
_log = logging.getLogger(__name__)


def _load(conn) -> Settings:
	"""Load settings from the database into the Settings model."""
	return Settings(
		signal_enabled=bool(sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)),
		purge_enabled=bool(sqlite_db.get_setting(conn, SettingKeys.PURGE_ENABLED, True)),
		use_utc_dashboard=bool(sqlite_db.get_setting(conn, SettingKeys.USE_UTC_DASHBOARD, False)),
		last_backup_at=sqlite_db.get_setting(conn, SettingKeys.LAST_BACKUP_AT, None),
		auth_disabled=bool(sqlite_db.get_setting(conn, SettingKeys.AUTH_DISABLED, False)),
		pdf_page_size=str(sqlite_db.get_setting(conn, SettingKeys.PDF_PAGE_SIZE, "a4") or "a4").lower(),
		theme=sqlite_db.get_setting(conn, SettingKeys.THEME, "system"),
		metrics_timeout_minutes=int(sqlite_db.get_setting(conn, SettingKeys.METRICS_TIMEOUT_MINUTES, 5) or 5),
		telemetry_enabled=bool(sqlite_db.get_setting(conn, SettingKeys.TELEMETRY_ENABLED, True)),
	)


@router.get("", response_model=Settings)
def get_settings(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Return current settings."""
	return _load(conn)


@router.patch("", response_model=Settings)
def update_settings(payload: SettingsUpdate, request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Patch settings and return the updated settings payload."""
	from ..utils.crypto import encrypt_secret
	from ..db.sqlite import transaction
	
	patch = payload.model_dump(exclude_unset=True)
	
	# Validate and transform values
	transformed = {}
	for k, v in patch.items():
		if k == "pdf_page_size" and v is not None:
			v = str(v).lower()
			if v not in VALID_PDF_PAGE_SIZES:
				raise HTTPException(
					status_code=400, 
					detail=f"pdf_page_size must be one of: {', '.join(sorted(VALID_PDF_PAGE_SIZES))}"
				)
		# Encrypt SMTP password before storing
		if k == SettingKeys.SMTP_PASSWORD and v:
			try:
				v = encrypt_secret(v)
			except Exception as e:
				_log.error("Failed to encrypt SMTP password: %s", e)
				raise HTTPException(status_code=500, detail="Failed to encrypt password")
		transformed[k] = v
	
	# Task 9: Atomic save - all settings in one transaction
	with transaction(conn):
		for k, v in transformed.items():
			sqlite_db.set_setting(conn, k, v, auto_commit=False)
	
	# Note: set_setting auto-invalidates cache for each key
	return _load(conn)


class PurgeRequest(BaseModel):
	"""Password confirmation payload for destructive operations."""
	password: str


@router.post("/tsdb/purge")
@limiter.limit(RATE_LIMIT_HEAVY)
def purge_tsdb(request: Request, body: PurgeRequest, conn=Depends(get_conn), admin=Depends(require_admin)):
	"""Purge all TSDB data (irreversible)."""
	if not is_purge_enabled(conn):
		raise HTTPException(status_code=403, detail="TSDB purge is disabled")

	_verify_admin_password(conn, admin, body.password)

	_log.warning(
		"TSDB purge initiated",
		extra={
			"event": "tsdb_purge",
			"user": admin.get("username"),
			"client_ip": _get_client_ip(request),
		},
	)
	tsdb_db.purge_tsdb(get_tsdb_dir(request))
	return {"ok": True}


@router.post("/alerts/purge")
@limiter.limit(RATE_LIMIT_HEAVY)
def purge_alerts(request: Request, body: PurgeRequest, conn=Depends(get_conn), admin=Depends(require_admin)):
	"""Delete all alerts from the database."""
	if not is_purge_enabled(conn):
		raise HTTPException(status_code=403, detail="Purge is disabled")

	_verify_admin_password(conn, admin, body.password)

	_log.warning(
		"Alerts purge initiated",
		extra={
			"event": "alerts_purge",
			"user": admin.get("username"),
			"client_ip": _get_client_ip(request),
		},
	)
	deleted_count = sqlite_db.delete_all_alerts(conn)
	return {"ok": True, "deleted": deleted_count}


class AuthToggleRequest(BaseModel):
	"""Payload for enabling/disabling authentication."""
	password: str
	disable: bool


@router.post("/auth/toggle")
@limiter.limit("5/minute")  # Strict rate limit for security-critical action
def toggle_auth(request: Request, body: AuthToggleRequest, conn=Depends(get_conn), admin=Depends(require_admin)):
	"""Enable or disable authentication (requires password confirmation)."""
	_verify_admin_password(conn, admin, body.password)
	if body.disable:
		_log.critical(
			"AUTHENTICATION DISABLED by %s from %s — all endpoints are now public",
			admin.get("username"),
			_get_client_ip(request),
		)
	
	sqlite_db.set_setting(conn, SettingKeys.AUTH_DISABLED, body.disable)
	_log.warning(
		"Authentication %s",
		"disabled" if body.disable else "enabled",
		extra={
			"event": "auth_toggle",
			"auth_disabled": body.disable,
			"user": admin.get("username"),
			"client_ip": _get_client_ip(request),
		},
	)
	return {"ok": True, "auth_disabled": body.disable}


@router.post("/signal/test")
@limiter.limit("5/minute")  # Strict rate limit to prevent spam
async def send_signal_test(request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""
	Queue a test message via Signal API to verify configuration.
	
	Sends the test message to the sender's own number (self-test).
	Uses signal-cli as the authority for which account to use.
	All notifications go through the spooler for consistent visibility.
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalNotRegistered
	from ..services.notifications.spooler import get_spooler, NotificationType, ChannelType
	
	backend = get_signal_backend()
	
	# Get valid sender from signal-cli (THE authority), not from DB
	configured_sender = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER)
	try:
		sender_number = await run_in_threadpool(backend.get_valid_sender, configured_sender)
	except SignalNotRegistered:
		raise HTTPException(
			status_code=400, 
			detail="No Signal account registered. Please complete the device linking process first."
		)
	
	# Auto-correct DB if it was wrong
	if configured_sender != sender_number:
		_log.warning("Auto-correcting Signal sender in DB: %s -> %s", 
			_mask_phone(configured_sender), _mask_phone(sender_number))
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, sender_number)
	
	test_message = "🔔 justUp Signal Test\n\nIf you receive this message, your Signal integration is working correctly!"
	
	# Enqueue to spooler (all notifications go through spooler)
	db_path = request.app.state.db_path
	spooler = get_spooler(lambda: sqlite_db.connect(db_path))
	
	queue_id = spooler.enqueue(
		notification_type=NotificationType.TEST,
		channel_type=ChannelType.SIGNAL,
		recipient_id=0,  # 0 = system/test message
		recipient_name="Self (Test)",
		recipient_address=sender_number,
		payload={"message": test_message, "sender": sender_number},
		error=None,
		immediate=True,  # Process immediately
	)
	
	_log.info("Signal test message queued (queue_id=%d)", queue_id)
	return {"ok": True, "message": "Test message queued"}


# ============================================================================
# Signal Template Management
# ============================================================================

class SignalTemplateResponse(BaseModel):
	"""Signal message templates (DOWN/UP)."""
	down: str
	up: str


class SignalTemplateUpdate(BaseModel):
	"""Update payload for Signal message templates."""
	down: str | None = None
	up: str | None = None


@router.get("/signal/template", response_model=SignalTemplateResponse)
def get_signal_template(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Get current Signal templates or defaults if not configured."""
	from ..services.notifications.signal_template import (
		DEFAULT_SIGNAL_TEMPLATE_DOWN,
		DEFAULT_SIGNAL_TEMPLATE_UP,
	)

	down = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, None) or DEFAULT_SIGNAL_TEMPLATE_DOWN
	up = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, None) or DEFAULT_SIGNAL_TEMPLATE_UP

	return SignalTemplateResponse(down=down, up=up)


@router.post("/signal/template")
def update_signal_template(payload: SignalTemplateUpdate, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Update Signal templates (DOWN and/or UP)."""
	if payload.down is not None:
		if not payload.down.strip():
			raise HTTPException(status_code=400, detail="DOWN template must not be empty")
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, payload.down)

	if payload.up is not None:
		if not payload.up.strip():
			raise HTTPException(status_code=400, detail="UP template must not be empty")
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, payload.up)

	return {"ok": True}


@router.post("/signal/template/reset", response_model=SignalTemplateResponse)
def reset_signal_template(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Reset Signal templates to defaults by clearing custom values."""
	from ..services.notifications.signal_template import (
		DEFAULT_SIGNAL_TEMPLATE_DOWN,
		DEFAULT_SIGNAL_TEMPLATE_UP,
	)

	sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, None)
	sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, None)

	return SignalTemplateResponse(down=DEFAULT_SIGNAL_TEMPLATE_DOWN, up=DEFAULT_SIGNAL_TEMPLATE_UP)


@router.get("/signal/status")
@limiter.limit("30/minute")  # Rate limit status checks
async def get_signal_status(request: Request, refresh: bool = False, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""
	Get Signal API status including health and registered accounts.
	
	Args:
		refresh: If True, bypass cache and fetch fresh status from signal-cli
	
	Returns:
		- healthy: bool - True if signal-cli is working and registered
		- reachable: bool - True if signal-cli is available
		- registered_accounts: list[str] - List of phone numbers registered
		- error: str | null - Error message if status check failed
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalUnavailable, SignalTimeout
	
	backend = get_signal_backend()
	
	try:
		status = await run_in_threadpool(lambda: backend.status(force_refresh=refresh))
		return {
			"healthy": status.registered and status.reachable,
			"reachable": status.reachable,
			"registered_accounts": status.accounts,
			"error": status.error,
		}
	except SignalTimeout as e:
		return {
			"healthy": False,
			"reachable": False,
			"registered_accounts": [],
			"error": f"Signal-cli timed out: {e}",
		}
	except SignalUnavailable as e:
		return {
			"healthy": False,
			"reachable": False,
			"registered_accounts": [],
			"error": str(e),
		}


class UnregisterPhoneRequest(BaseModel):
	"""Payload for unregistering a Signal account."""
	password: str


@router.post("/signal/unregister")
@limiter.limit("3/minute")  # Strict rate limit for destructive action
async def unregister_phone(
	request: Request,
	body: UnregisterPhoneRequest,
	conn=Depends(get_conn),
	admin=Depends(require_admin)
):
	"""
	Unregister the configured phone number from signal-cli.
	
	This is a destructive action that requires password confirmation.
	Uses the new subprocess-based backend.
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalNotRegistered, SignalUnavailable, SignalTimeout
	
	_verify_admin_password(conn, admin, body.password)
	
	backend = get_signal_backend()
	
	# Get valid sender from signal-cli (THE authority)
	configured_sender = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER)
	try:
		sender_number = await run_in_threadpool(backend.get_valid_sender, configured_sender)
	except SignalNotRegistered:
		# No accounts at all - clear DB and return success (desired end state)
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		return {"ok": True, "message": "No Signal account was registered"}
	
	_log.warning(
		"Signal unregister initiated",
		extra={
			"event": "signal_unregister",
			"user": admin.get("username"),
			"client_ip": _get_client_ip(request),
			"phone_masked": _mask_phone(sender_number),
		},
	)
	
	try:
		await run_in_threadpool(lambda: backend.unregister(sender_number=sender_number))
		
		# Clear the sender number from database after successful unregister
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		
		_log.info("Phone number %s successfully unregistered from signal-cli", _mask_phone(sender_number))
		return {"ok": True, "message": "Phone number successfully unregistered"}
		
	except SignalNotRegistered:
		# Account already not registered - still clear sender number and return success
		# (the desired end state is achieved)
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		_log.info("Phone number %s was already unregistered", _mask_phone(sender_number))
		return {"ok": True, "message": "Account was already unregistered"}
		
	except (SignalUnavailable, SignalTimeout) as e:
		_log.error("signal-cli unregister failed: %s", e)
		raise HTTPException(status_code=503, detail=str(e))


class UnlinkDeviceRequest(BaseModel):
	"""Payload for unlinking a Signal device (local data only)."""
	password: str


@router.post("/signal/unlink")
@limiter.limit("5/minute")
async def unlink_device(
	request: Request,
	body: UnlinkDeviceRequest,
	conn=Depends(get_conn),
	admin=Depends(require_admin)
):
	"""
	Unlink Signal device - deletes local data only, keeps Signal account.
	
	This is a less destructive action than unregister. The Signal account
	remains active and can be re-linked from the Signal app.
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalNotRegistered, SignalUnavailable, SignalTimeout
	
	_verify_admin_password(conn, admin, body.password)
	
	backend = get_signal_backend()
	
	# Get valid sender from signal-cli (THE authority)
	configured_sender = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER)
	try:
		sender_number = await run_in_threadpool(backend.get_valid_sender, configured_sender)
	except SignalNotRegistered:
		# No accounts at all - clear DB and return success (desired end state)
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		return {"ok": True, "message": "No Signal device was linked"}
	
	_log.warning(
		"Signal unlink initiated",
		extra={
			"event": "signal_unlink",
			"user": admin.get("username"),
			"client_ip": _get_client_ip(request),
			"phone_masked": _mask_phone(sender_number),
		},
	)
	
	try:
		await run_in_threadpool(lambda: backend.unlink_device(sender_number=sender_number))
		
		# Clear the sender number from database
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		
		_log.info("Device unlinked: %s", _mask_phone(sender_number))
		return {"ok": True, "message": "Device unlinked successfully"}
		
	except SignalNotRegistered:
		# Already unlinked - still clear sender and return success
		sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
		return {"ok": True, "message": "Device was already unlinked"}
		
	except (SignalUnavailable, SignalTimeout) as e:
		_log.error("signal-cli unlink failed: %s", e)
		raise HTTPException(status_code=503, detail=str(e))


# Note: Removed legacy /smtp/test endpoint - use /smtp/test-mail instead
# The template-based test provides better coverage and consistency


@router.get("/storage")
async def get_storage_stats(request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Get storage statistics: DB size, TSDB size, and total data points.
	
	Note: Line counting for total_points can be slow on large TSDB (1M+ data points).
	This is acceptable for admin-only endpoint with low request frequency.
	"""
	
	def _compute_storage():
		# SQLite DB size
		db_path = request.app.state.db_path
		sql_size_bytes = db_path.stat().st_size if db_path.exists() else 0
		
		# TSDB directory size and point count
		tsdb_dir = get_tsdb_dir(request)
		tsdb_size_bytes = 0
		total_points = 0
		
		if tsdb_dir.exists():
			for jsonl_file in tsdb_dir.rglob("*.jsonl"):
				tsdb_size_bytes += jsonl_file.stat().st_size
				# Count lines (each line = one data point)
				try:
					with open(jsonl_file, "r") as f:
						total_points += sum(1 for _ in f)
				except Exception:
					pass
		
		return sql_size_bytes, tsdb_size_bytes, total_points
	
	sql_size_bytes, tsdb_size_bytes, total_points = await run_in_threadpool(_compute_storage)
	
	return {
		"sql_size_bytes": sql_size_bytes,
		"tsdb_size_bytes": tsdb_size_bytes,
		"total_points": total_points,
	}


@router.post("/backup")
@limiter.limit(RATE_LIMIT_HEAVY)
def create_backup(request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""
	Create a backup of the data/ directory.
	
	Uses POST because this operation has side effects (WAL checkpoint, file creation).
	
	Returns a bzip2 tarball with SHA256 checksum in filename:
	justup_backup_YYYYMMDD_HHMMSS_<SHA256[:8]>.tar.bz2
	
	Uses a temporary file to avoid OOM on large datasets.
	Performs WAL checkpoint before backup to ensure consistency.
	"""
	data_dir = request.app.state.data_dir
	
	if not data_dir.exists():
		raise HTTPException(status_code=404, detail="Data directory not found")
	
	# Force WAL checkpoint using dedicated connection (not request-scoped)
	# TRUNCATE mode writes all pending changes and resets WAL to zero bytes
	checkpoint_conn = sqlite_db.connect(request.app.state.db_path)
	try:
		checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
		_log.info("WAL checkpoint completed before backup")
	except Exception as e:
		_log.warning("WAL checkpoint failed (non-fatal): %s", e)
	finally:
		sqlite_db.close_connection(checkpoint_conn)
	
	_log.info("Creating backup of %s", data_dir)
	
	# Create tarball in a temp file (avoids OOM on large backups)
	tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.bz2")
	tmp_path = Path(tmp_file.name)
	tmp_file.close()
	
	try:
		with tarfile.open(tmp_path, mode="w:bz2") as tar:
			# Only backup specific directories (sqlite, tsdb, pdf_reports)
			# Other files like GeoLite2-City.mmdb are excluded
			for subdir in BACKUP_DIRECTORIES:
				subdir_path = data_dir / subdir
				if subdir_path.exists():
					tar.add(subdir_path, arcname=f"data/{subdir}")
					_log.debug("Added %s to backup", subdir)
		
		# Calculate HMAC using instance-specific secret (streaming, no OOM)
		hmac_signature = _compute_backup_hmac(tmp_path, conn)
		
		file_size = tmp_path.stat().st_size
		
		# Generate filename with HMAC signature
		now = utcnow()
		timestamp = now.strftime("%Y%m%d_%H%M%S")
		filename = f"justup_backup_{timestamp}_{hmac_signature}.tar.bz2"
		
		# Save backup timestamp to database
		sqlite_db.set_setting(conn, SettingKeys.LAST_BACKUP_AT, now.isoformat())
		
		_log.info("Backup created: %s (%d bytes)", filename, file_size)
		
		# Stream from file
		def iter_file():
			try:
				with open(tmp_path, "rb") as f:
					yield from iter(lambda: f.read(1024 * 1024), b"")
			finally:
				tmp_path.unlink(missing_ok=True)
		
		return StreamingResponse(
			iter_file(),
			media_type="application/x-bzip2",
			headers={"Content-Disposition": f"attachment; filename={filename}"}
		)
	except Exception:
		tmp_path.unlink(missing_ok=True)
		raise

@router.post("/restore")
@limiter.limit(RATE_LIMIT_HEAVY)
async def restore_backup(
	request: Request,
	background_tasks: BackgroundTasks,
	password: str = Form(...),
	file: UploadFile = File(...),
	conn=Depends(get_conn),
	admin=Depends(require_admin)
):
	"""
	Restore a backup from an uploaded tarball.
	
	CRITICAL: Requires password confirmation (most destructive operation).
	Validates HMAC signature from filename before restoring.
	Streams upload to temp file to avoid OOM on large backups.
	"""
	# Require password confirmation for this destructive operation
	_verify_admin_password(conn, admin, password)
	
	filename = file.filename or ""
	
	# Extract expected HMAC from filename
	# Format: justup_backup_YYYYMMDD_HHMMSS_<HMAC16>.tar.bz2
	match = re.match(r"justup_backup_\d{8}_\d{6}_([a-f0-9]{16})\.tar\.bz2$", filename)
	if not match:
		raise HTTPException(
			status_code=400,
			detail="Backup corrupt! Cannot be imported. (Invalid filename format)"
		)
	
	expected_hmac = match.group(1)
	
	# Stream upload to temp file to avoid OOM on large backups (2-5 GB possible)
	# Use mkstemp instead of deprecated mktemp (TOCTOU-safe)
	fd, tmp_upload_str = tempfile.mkstemp(suffix=".tar.bz2")
	os.close(fd)  # Close the file descriptor, we'll open it ourselves
	tmp_upload_path = Path(tmp_upload_str)
	
	try:
		# Stream file content to disk in chunks
		chunk_size = 1024 * 1024  # 1 MB chunks
		first_chunk = True
		total_written = 0
		
		with open(tmp_upload_path, "wb") as tmp_upload:
			while True:
				chunk = await file.read(chunk_size)
				if not chunk:
					break
				total_written += len(chunk)
				if total_written > MAX_BACKUP_UPLOAD_BYTES:
					tmp_upload_path.unlink(missing_ok=True)
					raise HTTPException(status_code=413, detail="Backup file too large")
				
				# Validate bzip2 magic bytes on first chunk
				if first_chunk:
					if len(chunk) < 3 or chunk[:3] != b"BZh":
						tmp_upload_path.unlink(missing_ok=True)
						_log.warning("Invalid bzip2 magic bytes in uploaded file")
						raise HTTPException(
							status_code=400,
							detail="Backup corrupt! Cannot be imported. (Invalid file format - not a bzip2 archive)"
						)
					first_chunk = False
				
				tmp_upload.write(chunk)
		
		# Calculate HMAC using instance-specific secret (streaming)
		actual_hmac = _compute_backup_hmac(tmp_upload_path, conn)
		
		if actual_hmac != expected_hmac:
			tmp_upload_path.unlink(missing_ok=True)
			_log.warning("Backup HMAC mismatch: expected %s, got %s", expected_hmac, actual_hmac)
			raise HTTPException(
				status_code=400,
				detail="Backup corrupt! Cannot be imported. (HMAC signature mismatch - wrong instance or tampered file)"
			)
		
		data_dir = request.app.state.data_dir
		
		_log.warning(
			"Backup restore initiated",
			extra={
				"event": "backup_restore",
				"user": admin.get("username"),
				"client_ip": _get_client_ip(request),
				"filename": filename,
			},
		)
		
		_log.info("Restoring backup %s to %s", filename, data_dir)
		
		# Extract to temporary directory first, then move
		# CRITICAL: All code using tmp_path MUST stay inside this block!
		# The TemporaryDirectory is deleted when the context manager exits.
		with tempfile.TemporaryDirectory() as tmpdir:
			tmp_path = Path(tmpdir)
			
			# Extract tarball from file path (with path traversal protection)
			try:
				with tarfile.open(tmp_upload_path, mode="r:bz2") as tar:
					_safe_tar_extract(tar, tmp_path)
			except ValueError as e:
				_log.error("Unsafe tar archive: %s", e)
				raise HTTPException(
					status_code=400,
					detail="Backup corrupt! Cannot be imported. (Security violation)"
				)
			except Exception as e:
				_log.error("Failed to extract backup: %s", e)
				raise HTTPException(
					status_code=400,
					detail="Backup corrupt! Cannot be imported. (Extraction failed)"
				)
			
			extracted_data = tmp_path / "data"
			if not extracted_data.exists():
				raise HTTPException(
					status_code=400,
					detail="Backup corrupt! Cannot be imported. (No data directory found)"
				)

			# Prevent ghost DB handles by closing all tracked SQLite connections
			closed_count = sqlite_db.close_all_connections()
			_log.info("Closed %d SQLite connections before backup restore swap", closed_count)
			
			# Selective restore: only replace sqlite, tsdb, pdf_reports
			# Other files in data/ (like GeoLite2-City.mmdb) are preserved
			# 
			# Atomic restore with rollback capability per directory:
			# 1. Create backup of current directories
			# 2. Move current directories aside
			# 3. Move restored directories into place
			# 4. On success: clean up backup
			# 5. On failure: rollback from backup
			
			# Rollback directory inside data/ (where we have write permissions)
			rollback_dir = data_dir / f".rollback_{utcnow().strftime('%Y%m%d_%H%M%S')}"
			rollback_dir.mkdir(exist_ok=True)
			restore_succeeded = False
			restored_dirs = []
			
			try:
				# Ensure data_dir exists
				data_dir.mkdir(parents=True, exist_ok=True)
				
				# Restore only specific directories
				for subdir in BACKUP_DIRECTORIES:
					extracted_subdir = extracted_data / subdir
					target_subdir = data_dir / subdir
					
					if extracted_subdir.exists():
						# Backup current directory if it exists
						if target_subdir.exists():
							shutil.move(str(target_subdir), str(rollback_dir / subdir))
							_log.debug("Backed up %s for rollback", subdir)
						
						# Move restored directory into place
						shutil.move(str(extracted_subdir), str(target_subdir))
						restored_dirs.append(subdir)
						_log.info("Restored directory: %s", subdir)
				
				restore_succeeded = True
				_log.info("Restored %d directories: %s", len(restored_dirs), ", ".join(restored_dirs))
				
			except Exception as e:
				_log.error("Restore failed: %s. Attempting rollback...", e)
				
				# Attempt rollback for each directory
				try:
					for subdir in BACKUP_DIRECTORIES:
						target_subdir = data_dir / subdir
						rollback_subdir = rollback_dir / subdir
						
						# Remove partially restored directory
						if target_subdir.exists() and subdir in restored_dirs:
							shutil.rmtree(target_subdir)
						
						# Restore from rollback if available
						if rollback_subdir.exists():
							shutil.move(str(rollback_subdir), str(target_subdir))
					
					_log.info("Rollback successful")
				except Exception as rollback_error:
					_log.critical("ROLLBACK FAILED: %s. Manual intervention required!", rollback_error)
					raise HTTPException(
						status_code=500,
						detail=f"Restore AND rollback failed! Manual intervention required. Backup at: {rollback_dir}"
					)
				
				raise HTTPException(
					status_code=500,
					detail="Restore failed, rollback successful. Original data restored."
				)
			
			finally:
				# Clean up rollback directory only on success
				if restore_succeeded and rollback_dir.exists():
					try:
						shutil.rmtree(rollback_dir)
						_log.info("Cleaned up rollback backup")
					except Exception as cleanup_error:
						# Non-fatal, just log
						_log.warning("Failed to clean up rollback backup: %s", cleanup_error)
	
	finally:
		await file.close()
		# Always clean up the temp upload file
		tmp_upload_path.unlink(missing_ok=True)
	
	_log.warning(
		"Backup restored successfully, initiating restart",
		extra={"event": "backup_restored", "filename": filename},
	)
	
	# Schedule application restart after response is sent
	# Note: Requires process manager (systemd, Docker, supervisor) to auto-restart on SIGTERM.
	# Without process manager, app will stop and require manual restart.
	async def restart_app():
		await asyncio.sleep(1)  # Wait for response to be sent
		_log.critical("Intentional process termination for restart after backup restore")
		os.kill(os.getpid(), signal.SIGTERM)
	
	background_tasks.add_task(restart_app)
	
	return {
		"ok": True,
		"message": "Backup restored successfully. Application is restarting..."
	}


# ============================================================================
# Email Template Management
# ============================================================================

class EmailTemplateResponse(BaseModel):
	"""Email template data."""
	subject: str
	html: str


class EmailTemplateUpdate(BaseModel):
	"""Email template update payload."""
	subject: str | None = None
	html: str | None = None


@router.get("/smtp/template", response_model=EmailTemplateResponse)
def get_email_template(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Get current email template (subject + HTML) or defaults if not configured.
	
	Note: {{logo_url}} placeholder is replaced with preview URL for TinyMCE.
	"""
	from ..services.notifications.email_template import LOGO_URL_PREVIEW
	
	subject = sqlite_db.get_setting(conn, "smtp_mail_template_subject", None)
	html = sqlite_db.get_setting(conn, "smtp_mail_template_html", None)
	
	# Replace {{logo_url}} placeholder with local preview URL for TinyMCE
	subject_display = (subject or DEFAULT_SUBJECT).replace("{{logo_url}}", LOGO_URL_PREVIEW)
	html_display = (html or DEFAULT_HTML_TEMPLATE).replace("{{logo_url}}", LOGO_URL_PREVIEW)
	
	return EmailTemplateResponse(
		subject=subject_display,
		html=html_display,
	)


@router.post("/smtp/template")
def update_email_template(
	payload: EmailTemplateUpdate,
	conn=Depends(get_conn),
	_admin=Depends(require_admin)
):
	"""Update email template (subject and/or HTML).
	
	Note: Preview URL is replaced back with {{logo_url}} placeholder.
	"""
	from ..services.notifications.email_template import LOGO_URL_PREVIEW
	
	# Validate: subject must not be empty or whitespace-only
	if payload.subject is not None:
		if not payload.subject.strip():
			raise HTTPException(status_code=400, detail="Subject must not be empty")
		# Restore placeholder before saving
		subject_save = payload.subject.replace(LOGO_URL_PREVIEW, "{{logo_url}}")
		sqlite_db.set_setting(conn, "smtp_mail_template_subject", subject_save)
	
	# Validate: HTML must not be empty or whitespace-only
	if payload.html is not None:
		if not payload.html.strip():
			raise HTTPException(status_code=400, detail="HTML body must not be empty")
		# Restore placeholder before saving
		html_save = payload.html.replace(LOGO_URL_PREVIEW, "{{logo_url}}")
		sqlite_db.set_setting(conn, "smtp_mail_template_html", html_save)
	
	return {"ok": True}


@router.post("/smtp/template/reset")
def reset_email_template(conn=Depends(get_conn), _admin=Depends(require_admin)):
	"""Reset email template to defaults by clearing custom values.
	
	Sets template values to NULL so defaults are used on read.
	This allows distinguishing between 'user accepted default' and 'never changed'.
	"""
	sqlite_db.set_setting(conn, "smtp_mail_template_subject", None)
	sqlite_db.set_setting(conn, "smtp_mail_template_html", None)
	
	return EmailTemplateResponse(
		subject=DEFAULT_SUBJECT,
		html=DEFAULT_HTML_TEMPLATE,
	)


class TestEmailRequest(BaseModel):
	"""Test email request payload."""
	recipient: EmailStr


@router.post("/smtp/test-mail")
@limiter.limit("5/minute")  # Rate limit to prevent spam
async def send_test_email(
	request: Request,
	payload: TestEmailRequest,
	conn=Depends(get_conn),
	_admin=Depends(require_admin)
):
	"""Queue a test email using current template and SMTP settings.
	
	All notifications go through the spooler for consistent visibility
	and automatic retry handling.
	"""
	from ..services.notifications.email_template import build_alert_context, render_template
	from ..services.notifications.spooler import get_spooler, NotificationType, ChannelType
	
	# Load SMTP settings
	smtp_enabled = sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False)
	smtp_host = sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST, None)
	from_address = sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM, None)
	
	if not smtp_enabled:
		raise HTTPException(status_code=400, detail="SMTP is not enabled")
	if not smtp_host or not from_address:
		raise HTTPException(status_code=400, detail="SMTP not configured (missing host or from address)")
	
	# Load template
	subject_template = sqlite_db.get_setting(conn, "smtp_mail_template_subject", None) or DEFAULT_SUBJECT
	html_template = sqlite_db.get_setting(conn, "smtp_mail_template_html", None) or DEFAULT_HTML_TEMPLATE
	
	# Build test context
	test_alert_data = {
		"status": "OPEN",
		"started_at": "2026-01-29T12:00:00Z",
		"resolved_at": None,
		"reason": "HTTP 503 Service Unavailable",
		"downtime_minutes": 15,
		"sla_threshold": "99.9%",
		"sla_actual": "99.7%",
		"system_url": "https://monitoring.example.com",
	}
	test_target_data = {
		"name": "Test Target",
		"host": "example.com",
		"group": "Production",
		"check_type": "HTTP",
	}
	
	context = build_alert_context(test_alert_data, test_target_data)
	
	# Render templates
	subject = render_template(subject_template, context)
	html_body = render_template(html_template, context)
	
	# Enqueue to spooler (all notifications go through spooler)
	db_path = request.app.state.db_path
	spooler = get_spooler(lambda: sqlite_db.connect(db_path))
	
	queue_id = spooler.enqueue(
		notification_type=NotificationType.TEST,
		channel_type=ChannelType.EMAIL,
		recipient_id=0,  # 0 = system/test message
		recipient_name=f"Test ({payload.recipient})",
		recipient_address=payload.recipient,
		payload={
			"subject": subject,
			"text_body": "",  # HTML-only email
			"html_body": html_body,
		},
		error=None,
		immediate=True,  # Process immediately
	)
	
	_log.info("Test email queued for %s (queue_id=%d)", payload.recipient, queue_id)
	return {"ok": True, "message": f"Test email queued for {payload.recipient}"}


# ─── Notification Spooler ────────────────────────────────────────────────────────


@router.get("/notification-queue/stats")
async def get_notification_queue_stats(
	request: Request,
	_admin=Depends(require_admin),
):
	"""
	Get notification queue statistics.
	
	Shows the current state of the notification spooler (retry queue).
	Includes time dimension for debugging: oldest entry, max retry count, etc.
	"""
	from ..services.notifications.spooler import get_spooler
	
	# Factory must create fresh connections - do NOT return a request-scoped conn
	# because the singleton would hold a reference to a closed connection
	db_path = request.app.state.db_path
	def _get_db():
		return sqlite_db.connect(db_path)
	
	spooler = get_spooler(_get_db)
	detailed = spooler.get_queue_stats_detailed()
	
	return {
		"pending": detailed["by_status"].get("pending", 0),
		"processing": detailed["by_status"].get("processing", 0),
		"sent": detailed["by_status"].get("sent", 0),
		"failed": detailed["by_status"].get("failed", 0),
		"by_status": detailed["by_status"],
		"oldest_pending": detailed["oldest_pending"],
		"max_retry_count": detailed["max_retry_count"],
		"next_retry_at": detailed["next_retry_at"],
		"stuck_processing": detailed["stuck_processing"],
		"processing_timeout_seconds": detailed["processing_timeout_seconds"],
	}


@router.get("/notification-queue/log")
async def get_notification_queue_log(
	request: Request,
	limit: int = Query(default=100, ge=1, le=500),
	offset: int = Query(default=0, ge=0),
	status: str | None = Query(default=None),
	channel: str | None = Query(default=None),
	_admin=Depends(require_admin),
):
	"""
	Get notification log entries for the spooler UI.

	Supports pagination and optional filtering by status/channel.
	"""
	from ..services.notifications.spooler import get_spooler

	db_path = request.app.state.db_path
	def _get_db():
		return sqlite_db.connect(db_path)

	spooler = get_spooler(_get_db)
	return spooler.get_log(
		limit=limit,
		offset=offset,
		status_filter=status,
		channel_filter=channel,
	)


@router.delete("/notification-queue/flush")
async def flush_notification_queue(
	request: Request,
	_admin=Depends(require_admin),
):
	"""
	Flush (delete) all pending/failed notifications from the queue.
	
	This is useful when Signal is rate-limited or broken and you want
	to clear the backlog instead of waiting for retries.
	"""
	from ..services.notifications.spooler import get_spooler

	db_path = request.app.state.db_path
	def _get_db():
		return sqlite_db.connect(db_path)

	spooler = get_spooler(_get_db)
	deleted = spooler.flush_queue()
	_log.info("Flushed %d notifications from queue", deleted)
	return {"deleted": deleted, "message": f"Flushed {deleted} notifications"}


@router.delete("/notification-queue/clear-log")
async def clear_notification_log(
	request: Request,
	_admin=Depends(require_admin),
):
	"""Clear sent notifications from the log.

	Keeps pending, processing and failed entries intact.
	"""
	from ..services.notifications.spooler import get_spooler

	db_path = request.app.state.db_path
	def _get_db():
		return sqlite_db.connect(db_path)

	spooler = get_spooler(_get_db)
	deleted = spooler.clear_log()
	_log.info("Cleared %d sent notifications from log", deleted)
	return {"deleted": deleted, "message": f"Cleared {deleted} log entries"}
