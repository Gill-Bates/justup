#!/usr/bin/env python3
#
# app/api/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Settings and maintenance API routes (admin-only)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import tarfile
import tempfile
import threading
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..models.settings import Settings, SettingsUpdate
from ..models.users import verify_password
from ..services.notifications.email_template import (
    DEFAULT_HTML_TEMPLATE,
    DEFAULT_SUBJECT,
)
from ..services.notifications.smtp_settings import load_public_smtp_settings
from ..utils import maintenance
from ..utils.backup import (
    BACKUP_DIRECTORIES,
    MAX_BACKUP_UPLOAD_BYTES,
    BackupError,
    RestoreFailedError,
    apply_restore,
    compute_backup_hmac,
    extract_backup,
    inspect_backup_archive,
    migrate_staged_application_database,
    parse_backup_filename,
    serialize_backup_manifest,
    snapshot_pdf_reports,
    snapshot_sqlite_dir,
    verify_backup_hmac,
)
from ..utils.client_ip import audit_client_ip
from ..utils.constants import (
    DEFAULT_GEOIP_AUTO_UPDATE,
    DEFAULT_METRICS_TIMEOUT_MINUTES,
    DEFAULT_PDF_PAGE_SIZE,
    DEFAULT_PURGE_ENABLED,
    VALID_PDF_PAGE_SIZES,
    SettingKeys,
)
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.phone import mask_phone
from ..utils.public_url import get_public_hostname, normalize_public_hostname
from ..utils.rate_limit import RATE_LIMIT_HEAVY, rate_limit
from ..utils.runtime_fields import NON_NULL_RUNTIME_KEYS, load_runtime_fields, validate_quality_targets
from ..utils.settings_cache import invalidate_settings_cache, is_purge_enabled
from ..utils.smtp_readiness import SmtpReadiness, smtp_readiness
from ..utils.storage_stats import compute_storage_stats
from ..utils.time import utcnow
from .auth import require_admin

_get_client_ip = audit_client_ip


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
        purge_enabled=bool(sqlite_db.get_setting(conn, SettingKeys.PURGE_ENABLED, DEFAULT_PURGE_ENABLED)),
        geoip_auto_update=bool(sqlite_db.get_setting(conn, SettingKeys.GEOIP_AUTO_UPDATE, DEFAULT_GEOIP_AUTO_UPDATE)),
        use_utc_dashboard=bool(sqlite_db.get_setting(conn, SettingKeys.USE_UTC_DASHBOARD, False)),
        last_backup_at=sqlite_db.get_setting(conn, SettingKeys.LAST_BACKUP_AT, None),
        public_hostname=get_public_hostname(conn),
        auth_disabled=bool(sqlite_db.get_setting(conn, SettingKeys.AUTH_DISABLED, False)),
        pdf_page_size=str(
            sqlite_db.get_setting(conn, SettingKeys.PDF_PAGE_SIZE, DEFAULT_PDF_PAGE_SIZE) or DEFAULT_PDF_PAGE_SIZE
        ).lower(),
        theme=sqlite_db.get_setting(conn, SettingKeys.THEME, "system"),
        metrics_timeout_minutes=int(
            sqlite_db.get_setting(conn, SettingKeys.METRICS_TIMEOUT_MINUTES, DEFAULT_METRICS_TIMEOUT_MINUTES)
            or DEFAULT_METRICS_TIMEOUT_MINUTES
        ),
        session_max_days=sqlite_db.get_session_max_days(conn),
        **load_runtime_fields(conn),
        **load_public_smtp_settings(conn),
    )


def _encrypt_ip_api_key(value: str | None, encrypt):
    """Encrypt a submitted ip-api key; empty/None clears it."""
    if not value or not value.strip():
        return None
    try:
        return encrypt(value.strip())
    except Exception as e:
        _log.error("Failed to encrypt ip-api key: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Failed to encrypt key") from e


@router.get("", response_model=Settings)
def get_settings(conn=Depends(get_conn), _admin=Depends(require_admin)):
    """Return current settings."""
    return _load(conn)


@router.patch("", response_model=Settings)
def update_settings(payload: SettingsUpdate, conn=Depends(get_conn), _admin=Depends(require_admin)):
    """Patch settings and return the updated settings payload."""
    from ..db.sqlite import transaction
    from ..utils.crypto import encrypt_secret

    patch = payload.model_dump(exclude_unset=True)

    transformed = {}
    for k, v in patch.items():
        value = v
        if k == "pdf_page_size" and value is not None:
            value = str(value).lower()
            if value not in VALID_PDF_PAGE_SIZES:
                raise HTTPException(
                    status_code=400, detail=f"pdf_page_size must be one of: {', '.join(sorted(VALID_PDF_PAGE_SIZES))}"
                )
        if k in NON_NULL_RUNTIME_KEYS and value is None:
            raise HTTPException(status_code=422, detail=f"{k} must not be null")
        if k == SettingKeys.QUALITY_TARGETS and value is not None:
            value = validate_quality_targets(value)
        if k == SettingKeys.PUBLIC_HOSTNAME:
            if value is None:
                raise HTTPException(status_code=422, detail="public_hostname must not be null")
            try:
                value = normalize_public_hostname(value)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"public_hostname: {e}") from e
        if k == SettingKeys.IP_API_KEY:
            value = _encrypt_ip_api_key(value, encrypt_secret)
        if k == SettingKeys.SESSION_MAX_DAYS and value is None:
            raise HTTPException(status_code=422, detail="session_max_days must not be null")
        if k == SettingKeys.SMTP_USE_TLS and value is None:
            # null must never read as "encryption off"; only an explicit false is a decision
            raise HTTPException(status_code=422, detail="smtp_use_tls must be true or false, not null")
        if k == SettingKeys.SMTP_PASSWORD and value:
            try:
                value = encrypt_secret(value)
            except Exception as e:
                _log.error("Failed to encrypt SMTP password: %s", e)
                raise HTTPException(status_code=500, detail="Failed to encrypt password") from e
        transformed[k] = value

    try:
        with transaction(conn):
            for k, v in transformed.items():
                sqlite_db.set_setting(conn, k, v, auto_commit=False)
    finally:
        # Invalidate only after commit/rollback; a rollback invalidation is harmless
        for k in transformed:
            invalidate_settings_cache(str(getattr(k, "value", k)))
    return _load(conn)


class PurgeRequest(BaseModel):
    """Password confirmation payload for destructive operations."""

    password: str


@router.post("/tsdb/purge", dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))])
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
    if not tsdb_db.purge_tsdb(get_tsdb_dir(request)):
        raise HTTPException(
            status_code=500,
            detail="Some metric data could not be removed and remains; the purge can be repeated",
        )
    return {"ok": True}


@router.post("/alerts/purge", dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))])
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


# Strict rate limit for security-critical action
@router.post("/auth/toggle", dependencies=[Depends(rate_limit("5/minute"))])
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


@router.post("/signal/test", dependencies=[Depends(rate_limit("5/minute"))])
async def send_signal_test(request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
    """
    Queue a test message via Signal API to verify configuration.

    Sends the test message to the sender's own number (self-test).
    Uses signal-cli as the authority for which account to use.
    All notifications go through the spooler for consistent visibility.
    """
    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import SignalNotRegistered
    from ..services.notifications.spooler import (
        ChannelType,
        NotificationType,
        SignalQueueLimitError,
        get_spooler,
    )

    backend = get_signal_backend()

    # Get valid sender from signal-cli (THE authority), not from DB
    configured_sender = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER)
    try:
        sender_number = await run_in_threadpool(backend.get_valid_sender, configured_sender)
    except SignalNotRegistered as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}. Link a Signal device or correct the sender number in Settings.",
        ) from None

    # Store the sender on first setup (no silent switch away from a configured sender)
    if not configured_sender:
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, sender_number)

    test_message = "🔔 justUp Signal Test\n\nIf you receive this message, your Signal integration is working correctly!"

    # Enqueue to spooler (all notifications go through spooler)
    db_path = request.app.state.db_path
    spooler = get_spooler(lambda: sqlite_db.connect(db_path))

    try:
        queue_id = await run_in_threadpool(
            spooler.enqueue,
            notification_type=NotificationType.TEST,
            channel_type=ChannelType.SIGNAL,
            recipient_id=0,  # 0 = system/test message
            recipient_name="Self (Test)",
            recipient_address=sender_number,
            payload={"message": test_message, "sender": sender_number},
            error=None,
            immediate=True,
        )
    except SignalQueueLimitError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

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
    from ..services.notifications.signal_template import load_signal_templates

    return SignalTemplateResponse(**load_signal_templates(conn))


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


@router.get("/signal/status", dependencies=[Depends(rate_limit("30/minute"))])
async def get_signal_status(
    refresh: bool = False,
    _admin=Depends(require_admin),
):
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
    from ..services.notifications.signal_errors import SignalTimeout, SignalUnavailable

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


async def _resolve_signal_sender(conn, admin: dict, password: str, backend) -> str | None:
    """Verify the admin password and return the configured local Signal account.

    Returns None (and clears the stored sender) when no account exists locally.
    """
    from ..services.notifications.signal_errors import SignalTimeout, SignalUnavailable

    await run_in_threadpool(_verify_admin_password, conn, admin, password)
    configured_sender = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER)
    try:
        accounts = await run_in_threadpool(backend.list_accounts)
    except (SignalUnavailable, SignalTimeout) as e:
        raise HTTPException(status_code=503, detail="Unable to inspect Signal accounts") from e
    if not accounts:
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
        return None
    if not configured_sender or configured_sender not in accounts:
        raise HTTPException(status_code=409, detail="Configured Signal sender does not match a local account")
    return configured_sender


class UnregisterPhoneRequest(BaseModel):
    """Payload for unregistering a Signal account."""

    password: str


# Strict rate limit for destructive action
@router.post("/signal/unregister", dependencies=[Depends(rate_limit("3/minute"))])
async def unregister_phone(
    request: Request, body: UnregisterPhoneRequest, conn=Depends(get_conn), admin=Depends(require_admin)
):
    """
    Unregister the configured phone number from signal-cli.

    This is a destructive action that requires password confirmation.
    Uses the new subprocess-based backend.
    """
    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import (
        SignalNotRegistered,
        SignalSendFailed,
        SignalTimeout,
        SignalUnavailable,
    )

    backend = get_signal_backend()
    sender_number = await _resolve_signal_sender(conn, admin, body.password, backend)
    if sender_number is None:
        return {"ok": True, "message": "No Signal account was registered"}

    _log.warning(
        "Signal unregister initiated",
        extra={
            "event": "signal_unregister",
            "user": admin.get("username"),
            "client_ip": _get_client_ip(request),
            "phone_masked": mask_phone(sender_number),
        },
    )

    try:
        await run_in_threadpool(lambda: backend.unregister(sender_number=sender_number))

        # Clear the sender number from database after successful unregister
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)

        _log.info("Phone number %s successfully unregistered from signal-cli", mask_phone(sender_number))
        return {"ok": True, "message": "Phone number successfully unregistered"}

    except SignalNotRegistered:
        # Account already not registered - still clear sender number and return success
        # (the desired end state is achieved)
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
        _log.info("Phone number %s was already unregistered", mask_phone(sender_number))
        return {"ok": True, "message": "Account was already unregistered"}

    except (SignalUnavailable, SignalTimeout, SignalSendFailed) as e:
        _log.error("signal-cli unregister failed: %s", e)
        raise HTTPException(status_code=503, detail="Signal account removal failed") from e


class UnlinkDeviceRequest(BaseModel):
    """Payload for unlinking a Signal device (local data only)."""

    password: str


@router.post("/signal/unlink", dependencies=[Depends(rate_limit("5/minute"))])
async def unlink_device(
    request: Request, body: UnlinkDeviceRequest, conn=Depends(get_conn), admin=Depends(require_admin)
):
    """
    Unlink Signal device - deletes local data only, keeps Signal account.

    This is a less destructive action than unregister. The Signal account
    remains active and can be re-linked from the Signal app.
    """
    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import (
        SignalNotRegistered,
        SignalSendFailed,
        SignalTimeout,
        SignalUnavailable,
    )

    backend = get_signal_backend()
    sender_number = await _resolve_signal_sender(conn, admin, body.password, backend)
    if sender_number is None:
        return {"ok": True, "message": "No Signal device was linked"}

    _log.warning(
        "Signal unlink initiated",
        extra={
            "event": "signal_unlink",
            "user": admin.get("username"),
            "client_ip": _get_client_ip(request),
            "phone_masked": mask_phone(sender_number),
        },
    )

    try:
        await run_in_threadpool(lambda: backend.unlink_device(sender_number=sender_number, delete_cache=True))

        # Clear the sender number from database
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)

        _log.info("Device unlinked: %s", mask_phone(sender_number))
        return {"ok": True, "message": "Local Signal data removed; revoke the device in Signal on your primary phone"}

    except SignalNotRegistered:
        # Already unlinked - still clear sender and return success
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
        return {"ok": True, "message": "Device was already unlinked"}

    except (SignalUnavailable, SignalTimeout, SignalSendFailed) as e:
        _log.error("signal-cli unlink failed: %s", e)
        raise HTTPException(status_code=503, detail="Local Signal data removal failed") from e


# Note: Removed legacy /smtp/test endpoint - use /smtp/test-mail instead
# The template-based test provides better coverage and consistency


@router.get("/storage")
async def get_storage_stats(request: Request, _admin=Depends(require_admin)):
    """Get storage statistics: DB size, TSDB size, data points and the volume saved by compression.

    Active files are counted line by line; compressed segments are decompressed once and then served
    from a cache keyed by file identity, so repeated calls stay cheap.
    """

    stats = await run_in_threadpool(compute_storage_stats, request.app.state.db_path, get_tsdb_dir(request))

    return {
        "sql_size_bytes": stats["sql_size_bytes"],
        "tsdb_size_bytes": stats["tsdb_size_bytes"],
        "total_points": stats["total_points"],
        "tsdb_saved_bytes": stats["tsdb_saved_bytes"],
    }


@router.get("/db-health")
async def get_db_health(request: Request, quick_check: bool = False, _admin=Depends(require_admin)):
    """SQLite health: file/WAL size, free pages, queue backlog, connections.

    ``quick_check=true`` additionally runs PRAGMA quick_check, which reads the whole file.
    """
    from ..db import retention

    def _collect() -> dict:
        conn = sqlite_db.connect(request.app.state.db_path)
        try:
            return retention.collect_db_stats(conn, quick_check=quick_check)
        finally:
            sqlite_db.close_connection(conn)

    return await run_in_threadpool(_collect)


@router.post("/backup", dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))])
def create_backup(request: Request, conn=Depends(get_conn), _admin=Depends(require_admin)):
    """
    Create a backup of the data/ directory.

    Uses POST because this operation has side effects (snapshot and file creation).

    Returns a bzip2 tarball with SHA256 checksum in filename:
    justup_backup_YYYYMMDD_HHMMSS_<HMAC-SHA256 hex>.tar.bz2

    Uses a temporary file to avoid OOM on large datasets.
    SQLite databases enter the archive as online-backup snapshots (consistent while writers are active),
    never as a raw copy of the live file.
    """
    data_dir = request.app.state.data_dir

    if not data_dir.exists():
        raise HTTPException(status_code=404, detail="Data directory not found")

    _log.info("Creating backup of %s", data_dir)

    # Create tarball in a temp file (avoids OOM on large backups).
    # mkstemp only reserves a unique path here; the file is written below via
    # tarfile.open, so no file handle is kept open outside a context manager.
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.bz2")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        # A work section keeps a restore from swapping the directories while they are being read
        with (
            maintenance.work_section(),
            tempfile.TemporaryDirectory(prefix="justup-backup-snapshot-") as snapshot_root,
            tarfile.open(tmp_path, mode="w:bz2") as tar,
        ):
            sqlite_snapshot: Path | None = None
            # Only backup specific directories (sqlite, tsdb, pdf_reports)
            # Other files like GeoLite2-City.mmdb are excluded
            for subdir in BACKUP_DIRECTORIES:
                subdir_path = data_dir / subdir
                if not subdir_path.exists():
                    if subdir == "sqlite":
                        raise BackupError("Backup failed: application database directory is missing")
                    continue
                if subdir == "sqlite":
                    snapshot = Path(snapshot_root) / "sqlite"
                    snapshot_sqlite_dir(subdir_path, snapshot)
                    subdir_path = snapshot
                    sqlite_snapshot = snapshot / "justup.db"
                elif subdir == "tsdb":
                    snapshot = Path(snapshot_root) / "tsdb"
                    tsdb_db.snapshot_tsdb(subdir_path, snapshot)
                    subdir_path = snapshot
                elif subdir == "pdf_reports":
                    snapshot = Path(snapshot_root) / "pdf_reports"
                    snapshot_pdf_reports(subdir_path, snapshot)
                    subdir_path = snapshot
                tar.add(subdir_path, arcname=f"data/{subdir}")
                _log.debug("Added %s to backup", subdir)
            if sqlite_snapshot is None or not sqlite_snapshot.is_file():
                raise BackupError("Backup failed: application database data/sqlite/justup.db is missing")
            manifest_bytes = serialize_backup_manifest(sqlite_snapshot)
            manifest = tarfile.TarInfo("data/backup-manifest.json")
            manifest.size = len(manifest_bytes)
            tar.addfile(manifest, io.BytesIO(manifest_bytes))

        hmac_signature = compute_backup_hmac(tmp_path)

        file_size = tmp_path.stat().st_size

        now = utcnow()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"justup_backup_{timestamp}_{hmac_signature}.tar.bz2"

        sqlite_db.set_setting(conn, SettingKeys.LAST_BACKUP_AT, now.isoformat())

        _log.info("Backup created: %s (%d bytes)", filename, file_size)

        def iter_file():
            try:
                with Path(tmp_path).open("rb") as f:
                    yield from iter(lambda: f.read(1024 * 1024), b"")
            finally:
                tmp_path.unlink(missing_ok=True)

        return StreamingResponse(
            iter_file(),
            media_type="application/x-bzip2",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
            # Also runs when the generator never started (client left before the first chunk)
            background=BackgroundTask(tmp_path.unlink, missing_ok=True),
        )
    except maintenance.MaintenanceActiveError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503,
            detail="Backup postponed: a restore is in progress. Try again shortly.",
            headers={"Retry-After": "30"},
        ) from exc
    except BackupError as exc:
        tmp_path.unlink(missing_ok=True)
        _log.error("Backup failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


RESTORE_QUIESCE_TIMEOUT_SECONDS = 30.0

# Process-wide guard so a second restore fails fast (503) before any extraction begins.
_RESTORE_GUARD = threading.Lock()


class RestoreBusyError(Exception):
    """Other database users did not finish in time, so the restore was not started."""


def _quiesced_swap(extracted: Path, data_dir: Path, conn) -> list[str]:
    """Drain background work and database users (each bounded), then swap the directories.

    Both refusals are lifted again on every failure that leaves the original data in place. After a
    successful swap (or a failed rollback) they stay active until the process restarts.
    """
    if not maintenance.begin_maintenance(timeout=RESTORE_QUIESCE_TIMEOUT_SECONDS):
        raise RestoreBusyError(f"background work did not finish within {RESTORE_QUIESCE_TIMEOUT_SECONDS}s")
    keep_blocked = False
    try:
        # Refuse new connections and let running users finish; foreign handles are never closed
        if not sqlite_db.begin_quiesce(keep=[conn], timeout=RESTORE_QUIESCE_TIMEOUT_SECONDS):
            raise RestoreBusyError(f"database users did not finish within {RESTORE_QUIESCE_TIMEOUT_SECONDS}s")
        try:
            sqlite_db.close_connection(conn)  # this request's own handle; the swap replaces its files
            restored = apply_restore(extracted, data_dir)
            # Data is now replaced: advance the generation so pre-restore in-flight work is stale.
            maintenance.confirm_data_replaced()
            keep_blocked = True
            return restored
        except RestoreFailedError as exc:
            keep_blocked = not exc.rolled_back
            if exc.rolled_back:
                sqlite_db.end_quiesce()
            raise
        except BaseException:
            sqlite_db.end_quiesce()
            raise
    finally:
        if not keep_blocked:
            maintenance.end_maintenance()


@router.post("/restore", dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))])
async def restore_backup(
    request: Request,
    background_tasks: BackgroundTasks,
    password: str = Form(...),
    file: UploadFile = File(...),
    conn=Depends(get_conn),
    admin=Depends(require_admin),
):
    """
    Restore a backup from an uploaded tarball.

    CRITICAL: Requires password confirmation (most destructive operation).
    Validates HMAC signature from filename before restoring.
    Streams upload to temp file to avoid OOM on large backups.
    """

    await run_in_threadpool(_verify_admin_password, conn, admin, password)
    filename = file.filename or ""

    try:
        expected_hmac = parse_backup_filename(filename)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Stream upload to temp file to avoid OOM on large backups; file I/O runs in worker threads
    fd, tmp_upload_str = tempfile.mkstemp(suffix=".tar.bz2")
    os.close(fd)
    tmp_upload_path = Path(tmp_upload_str)

    try:
        chunk_size = 1024 * 1024
        first_chunk = True
        total_written = 0

        tmp_upload = await asyncio.to_thread(tmp_upload_path.open, "wb")
        try:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > MAX_BACKUP_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Backup file too large")
                if first_chunk:
                    if len(chunk) < 3 or chunk[:3] != b"BZh":
                        _log.warning("Invalid bzip2 magic bytes in uploaded file")
                        raise HTTPException(
                            status_code=400,
                            detail="Backup corrupt! Cannot be imported. (Invalid file format - not a bzip2 archive)",
                        )
                    first_chunk = False
                await asyncio.to_thread(tmp_upload.write, chunk)
        finally:
            await asyncio.to_thread(tmp_upload.close)

        try:
            hmac_ok = await asyncio.to_thread(verify_backup_hmac, tmp_upload_path, expected_hmac)
        except BackupError as exc:  # unusable signing configuration, not a bad upload
            _log.error("Backup verification impossible: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not hmac_ok:
            _log.warning("Backup HMAC mismatch for %s", filename)
            raise HTTPException(
                status_code=400,
                detail="Backup corrupt! Cannot be imported. (HMAC mismatch - wrong instance or tampered file)",
            )

        try:
            manifest = await asyncio.to_thread(inspect_backup_archive, tmp_upload_path)
        except BackupError as exc:
            _log.error("Rejected backup metadata: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        data_dir = request.app.state.data_dir

        _log.warning(
            "Backup restore initiated",
            extra={
                "event": "backup_restore",
                "user": admin.get("username"),
                "client_ip": _get_client_ip(request),
                "backup_filename": filename,
            },
        )
        _log.info("Restoring backup %s to %s", filename, data_dir)

        def _extract_and_swap() -> tuple[list[str], int, int]:
            # Reject a second concurrent restore before any extraction begins.
            if not _RESTORE_GUARD.acquire(blocking=False):
                raise RestoreBusyError("another restore is already in progress")
            try:
                # Stage inside data_dir so the final swap is a same-filesystem rename
                data_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".restore_staging_", dir=data_dir) as tmpdir:
                    # Validates structure and the integrity of every contained database before anything is touched
                    extracted = extract_backup(tmp_upload_path, Path(tmpdir), manifest=manifest)
                    original_version, migrated_version = migrate_staged_application_database(extracted)
                    restored = _quiesced_swap(extracted, data_dir, conn)
                    return restored, original_version, migrated_version
            finally:
                _RESTORE_GUARD.release()

        try:
            restored_dirs, original_schema_version, migrated_schema_version = await asyncio.to_thread(_extract_and_swap)
        except RestoreBusyError as exc:
            _log.warning("Restore aborted: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Restore postponed: the system is busy. Nothing was changed; try again shortly.",
                headers={"Retry-After": "30"},
            ) from exc
        except BackupError as exc:
            _log.error("Rejected backup archive: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RestoreFailedError as exc:
            if exc.rolled_back:
                raise HTTPException(
                    status_code=500, detail="Restore failed, rollback successful. Original data restored."
                ) from exc
            raise HTTPException(
                status_code=500,
                detail=f"Restore AND rollback failed! Manual intervention required. Backup at: {exc.rollback_dir}",
            ) from exc
        _log.info(
            "Restored %d directories: %s (schema %d -> %d)",
            len(restored_dirs),
            ", ".join(restored_dirs),
            original_schema_version,
            migrated_schema_version,
        )
    finally:
        await file.close()
        await asyncio.to_thread(tmp_upload_path.unlink, missing_ok=True)

    _log.warning(
        "Backup restored successfully, initiating restart",
        extra={"event": "backup_restored", "backup_filename": filename},
    )

    # Schedule application restart after response is sent
    # Note: Requires process manager (systemd, Docker, supervisor) to auto-restart on SIGTERM.
    # Without process manager, app will stop and require manual restart.
    async def restart_app():
        await asyncio.sleep(1)
        _log.critical("Intentional process termination for restart after backup restore")
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(restart_app)

    message = "Backup compatible. Restore successful. Application is restarting..."
    if original_schema_version < migrated_schema_version:
        message = (
            f"Backup compatible; database schema migrated from {original_schema_version} to "
            f"{migrated_schema_version}. Restore successful. Application is restarting..."
        )
    return {
        "ok": True,
        "message": message,
        "migrated": original_schema_version < migrated_schema_version,
        "backup_schema_version": original_schema_version,
        "current_schema_version": migrated_schema_version,
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
    from ..services.notifications.email_template import load_template_preview

    return EmailTemplateResponse(**load_template_preview(conn))


@router.post("/smtp/template")
def update_email_template(payload: EmailTemplateUpdate, conn=Depends(get_conn), _admin=Depends(require_admin)):
    """Update email template (subject and/or HTML).

    Note: Preview URL is replaced back with {{logo_url}} placeholder.
    """
    from ..services.notifications.email_template import LOGO_URL_PREVIEW

    # Validate: subject must not be empty or whitespace-only
    if payload.subject is not None:
        if not payload.subject.strip():
            raise HTTPException(status_code=400, detail="Subject must not be empty")
        subject_save = payload.subject.replace(LOGO_URL_PREVIEW, "{{logo_url}}")
        sqlite_db.set_setting(conn, "smtp_mail_template_subject", subject_save)

    # Validate: HTML must not be empty or whitespace-only
    if payload.html is not None:
        if not payload.html.strip():
            raise HTTPException(status_code=400, detail="HTML body must not be empty")
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


class EmailTemplatePreviewRequest(BaseModel):
    """Preview-render payload: the current (possibly unsaved) editor template."""

    subject: str | None = None
    html: str | None = None
    variant: str = "down"  # "down" or "resolved"


class EmailTemplatePreviewResponse(BaseModel):
    """Fully rendered preview with sample data (no unreplaced placeholders)."""

    subject: str
    html: str


@router.post("/smtp/template/preview", response_model=EmailTemplatePreviewResponse)
def preview_email_template(payload: EmailTemplatePreviewRequest, _admin=Depends(require_admin)):
    """Render the given template with deterministic sample data for the editor preview.

    Rendering runs through the same whitelist renderer as the real send path, so the
    preview cannot show anything the send path would not (no code execution, values
    escaped). The logo reference is rewritten to the local preview URL so the browser
    iframe shows the logo without a mail client. DOWN and RESOLVED samples are available.
    """
    from ..services.notifications.email_template import (
        DEFAULT_HTML_TEMPLATE as _DEF_HTML,
    )
    from ..services.notifications.email_template import (
        DEFAULT_SUBJECT as _DEF_SUBJECT,
    )
    from ..services.notifications.email_template import (
        build_alert_context,
        build_test_alert_sample,
        render_template,
        template_to_preview,
    )

    alert_data, target_data = build_test_alert_sample()
    if payload.variant == "resolved":
        # A resolved sample: 15-minute outage that just ended, so Resolved/Duration read cleanly.
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        started = _dt.fromisoformat(alert_data["started_at"])
        alert_data = {
            **alert_data,
            "status": "RESOLVED",
            "resolved_at": (started + _td(minutes=15)).isoformat(),
            "reason": "Recovered: HTTP 200 OK (justUp! test notification)",
        }

    context = build_alert_context(alert_data, target_data)
    subject_template = payload.subject if payload.subject is not None else _DEF_SUBJECT
    html_template = payload.html if payload.html is not None else _DEF_HTML

    subject = render_template(subject_template, context)
    html = template_to_preview(render_template(html_template, context))
    return EmailTemplatePreviewResponse(subject=subject, html=html)


class TestEmailRequest(BaseModel):
    """Test email request payload."""

    recipient: EmailStr


@router.post("/smtp/test-mail", dependencies=[Depends(rate_limit("5/minute"))])
async def send_test_email(
    request: Request, payload: TestEmailRequest, conn=Depends(get_conn), _admin=Depends(require_admin)
):
    """Queue a test email using current template and SMTP settings.

    All notifications go through the spooler for consistent visibility
    and automatic retry handling.
    """
    from ..services.notifications.email_template import (
        build_alert_context,
        build_test_alert_sample,
        html_to_text,
        load_email_templates,
        render_template,
    )
    from ..services.notifications.spooler import (
        ChannelType,
        NotificationType,
        get_spooler,
    )

    readiness = smtp_readiness(conn)
    if readiness is SmtpReadiness.DISABLED:
        raise HTTPException(status_code=400, detail="SMTP is not enabled")
    if readiness is SmtpReadiness.INCOMPLETE:
        raise HTTPException(status_code=400, detail="SMTP not configured (missing host or from address)")

    stored = load_email_templates(conn)
    subject_template, html_template = stored["subject"], stored["html"]

    test_alert_data, test_target_data = build_test_alert_sample()

    context = build_alert_context(test_alert_data, test_target_data)

    subject = render_template(subject_template, context)
    html_body = render_template(html_template, context)
    text_body = html_to_text(html_body)

    # Enqueue to spooler (all notifications go through spooler)
    db_path = request.app.state.db_path
    spooler = get_spooler(lambda: sqlite_db.connect(db_path))

    queue_id = await run_in_threadpool(
        spooler.enqueue,
        notification_type=NotificationType.TEST,
        channel_type=ChannelType.EMAIL,
        recipient_id=0,  # 0 = system/test message
        recipient_name=f"Test ({payload.recipient})",
        recipient_address=payload.recipient,
        payload={
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
        },
        error=None,
        immediate=True,
    )

    _log.info("Test email queued for %s (queue_id=%d)", payload.recipient, queue_id)
    return {
        "ok": True,
        "message": f"Test email queued for {payload.recipient}; the SMTP handoff result appears in the delivery log",
    }


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
    detailed = await run_in_threadpool(spooler.get_queue_stats_detailed)

    return {
        "pending": detailed["by_status"].get("pending", 0),
        "processing": detailed["by_status"].get("processing", 0),
        "sent": detailed["by_status"].get("sent", 0),
        "failed": detailed["by_status"].get("failed", 0),
        "failed_recent": detailed["failed_recent"],
        "by_status": detailed["by_status"],
        "channels": detailed["channels"],
        "cooldowns": detailed["cooldowns"],
        "oldest_pending": detailed["oldest_pending"],
        "max_retry_count": detailed["max_retry_count"],
        "next_retry_at": detailed["next_retry_at"],
        "stuck_processing": detailed["stuck_processing"],
        "processing_timeout_seconds": detailed["processing_timeout_seconds"],
        "services": getattr(getattr(request.app.state, "supervisor", None), "snapshot", dict)(),
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
    return await run_in_threadpool(
        spooler.get_log,
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
    deleted = await run_in_threadpool(spooler.flush_queue)
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
    deleted = await run_in_threadpool(spooler.clear_log)
    _log.info("Cleared %d sent notifications from log", deleted)
    return {"deleted": deleted, "message": f"Cleared {deleted} log entries"}
