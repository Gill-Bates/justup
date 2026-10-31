#!/usr/bin/env python3
#
# app/api/signal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal API routes using local signal-cli subprocess.

Design principles:
- Internal API (UI-only, no public contract)
- Read-heavy, short-lived
- Defensive: signal-cli is "best effort", never critical for app start
- No business logic in API layer

HTTP Status Codes:
- 200: Success with content
- 204: Success, no content
- 400: Bad request (invalid input)
- 403: Admin privileges required
- 409: Conflict (not registered, QR not available)
- 503: Service unavailable (signal-cli not working)
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ..db import sqlite as sqlite_db
from ..services.notifications.signal_backend import get_signal_backend
from ..services.notifications.signal_errors import (
    SignalNotRegistered,
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalSendFailed,
    SignalTimeout,
    SignalUnavailable,
)
from ..utils.client_ip import resolve_trusted_client_ip
from ..utils.deps import get_conn
from ..utils.phone import normalize_phone
from ..utils.rate_limit import limiter
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/signal", tags=["signal"])
_log = logging.getLogger(__name__)

# Generic client-facing text; backend details (paths, stderr) go to the log only.
_UNAVAILABLE_DETAIL = "Signal service unavailable"


def _signal_limit_key(request: Request) -> str:
    # Per user, so minting new tokens does not reset the budget.
    user_id = getattr(request.state, "auth_user_id", None)
    if user_id is not None:
        return f"user:{user_id}"
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return "bearer:" + hashlib.sha256(token.encode()).hexdigest()
    return "ip:" + resolve_trusted_client_ip(request)


# ─── Models ─────────────────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    """Request to send a Signal message."""

    recipient: str = Field(..., min_length=5, max_length=20, description="Phone number in E.164 format")
    message: str = Field(..., min_length=1, max_length=4000, description="Message text")

    @field_validator("recipient")
    @classmethod
    def validate_e164(cls, v: str) -> str:
        """Validate and normalize E.164 phone number."""
        try:
            return normalize_phone(v, strict=True)
        except ValueError as e:
            raise ValueError(f"Invalid E.164 phone number: {e}") from e


class RegisterRequest(BaseModel):
    """Request to start Signal registration."""

    number: str = Field(..., min_length=5, max_length=20, description="Phone number in E.164 format")
    use_voice: bool = Field(False, description="Use voice call instead of SMS")
    captcha: str | None = Field(None, description="Captcha token if required")

    @field_validator("number")
    @classmethod
    def validate_number(cls, v: str) -> str:
        """Validate E.164 phone number."""
        try:
            return normalize_phone(v, strict=True)
        except ValueError as e:
            raise ValueError(f"Invalid E.164 phone number: {e}") from e


class VerifyRequest(BaseModel):
    """Request to verify Signal registration."""

    number: str = Field(..., min_length=5, max_length=20, description="Phone number in E.164 format")
    code: str = Field(..., min_length=6, max_length=6, description="6-digit verification code")


# ─── Helpers ────────────────────────────────────────────────────────────────────


# ─── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/status")
def signal_status(_user=Depends(get_current_user)):
    """Get Signal status.

    Returns status information for UI badges.
    Always returns 200 with status object - never fails hard.
    """
    backend = get_signal_backend()
    try:
        status_obj = backend.status()
        return status_obj.to_dict()

    except (SignalUnavailable, SignalTimeout) as e:
        # Return degraded status with error in payload (always 200 for UI badges)
        _log.warning("Signal unavailable: %s", e)
        return {
            "reachable": False,
            "registered": False,
            "linked": False,
            "accounts": [],
            "error": _UNAVAILABLE_DETAIL,
        }


@router.post("/send", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute", key_func=_signal_limit_key)
def signal_send(
    body: SendMessageRequest,
    request: Request,
    conn=Depends(get_conn),
    _admin=Depends(require_admin),
):
    """Send a Signal message via spooler (admin only)."""
    from ..services.notifications.spooler import (
        ChannelType,
        NotificationType,
        SignalQueueLimitError,
        get_spooler,
    )
    from ..utils.constants import SettingKeys

    # Get sender number
    signal_sender_number = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
    if not signal_sender_number:
        raise HTTPException(status_code=409, detail="Signal sender not configured")

    try:
        db_path = request.app.state.db_path
        spooler = get_spooler(lambda: sqlite_db.connect(db_path))
        spooler.enqueue(
            notification_type=NotificationType.TEST,
            channel_type=ChannelType.SIGNAL,
            recipient_id=0,
            recipient_name="Admin",
            recipient_address=body.recipient,
            payload={
                "message": body.message,
                "sender": signal_sender_number,
            },
            immediate=True,
        )
        return Response(status_code=204)
    except SignalQueueLimitError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except Exception as e:
        _log.error("Failed to enqueue Signal message: %s", e)
        raise HTTPException(status_code=500, detail="Unable to queue Signal message") from e


@router.get("/qr")
@limiter.limit("3/hour", key_func=_signal_limit_key)
def signal_qr(request: Request, _admin=Depends(require_admin)):  # noqa: ARG001
    """Get QR code for device linking (admin only).

    Returns PNG image or 409 if QR not available.
    """
    backend = get_signal_backend()

    try:
        png, session_id = backend.get_link_qr(device_name="justUp")
        if not png:
            _log.warning("QR code generation failed: session_id=%s", session_id or "none")
            raise HTTPException(status_code=409, detail="QR code not available")

        return Response(content=png, media_type="image/png")

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for QR generation: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e


@router.post("/link/confirm")
@limiter.limit("5/hour", key_func=_signal_limit_key)
def signal_link_confirm(request: Request, _admin=Depends(require_admin)):  # noqa: ARG001
    """Wait for link process to complete after QR scan (admin only).

    Call this after the user has scanned the QR code on their phone.
    Waits up to 60 seconds for signal-cli to confirm the link.

    Returns:
        200: Link successful, with account list
        408: Timeout waiting for link confirmation
        503: Signal unavailable
    """
    backend = get_signal_backend()

    try:
        # Get the current link session ID
        session_id = backend.get_link_session_id()
        if not session_id:
            _log.warning("No active link session found")
            raise HTTPException(
                status_code=409,
                detail="No link process active. Please generate a QR code first.",
            )

        # Wait for the link process to complete
        success = backend.wait_for_link(session_id=session_id, timeout=60)

        if not success:
            _log.warning("Signal link timeout after 60 seconds")
            raise HTTPException(
                status_code=504,
                detail="Timeout waiting for link confirmation. Please try again.",
            )

        # Link successful - get accounts
        accounts = backend.list_accounts()

        return {"success": True, "accounts": accounts}

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e


@router.post("/link/cancel", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour", key_func=_signal_limit_key)
def signal_link_cancel(request: Request, _admin=Depends(require_admin)):  # noqa: ARG001
    """Cancel any running link process (admin only)."""
    backend = get_signal_backend()
    backend.cancel_link()
    return Response(status_code=204)


@router.get("/accounts")
def signal_accounts(_admin=Depends(require_admin)):
    """List registered Signal accounts (admin only)."""
    backend = get_signal_backend()

    try:
        accounts = backend.list_accounts()
        return {"accounts": accounts}

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e


@router.post("/register", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/hour", key_func=_signal_limit_key)
def signal_register(request: Request, body: RegisterRequest, _admin=Depends(require_admin)):  # noqa: ARG001
    """Start Signal registration for a phone number (admin only).

    Sends a verification code via SMS (default) or voice call.
    After calling this, use /signal/verify to complete registration.
    """
    backend = get_signal_backend()

    try:
        backend.register(
            number=body.number,
            use_voice=body.use_voice,
            captcha=body.captcha,
        )
        return Response(status_code=204)

    except SignalRecipientInvalid as e:
        _log.warning("Invalid phone number for registration: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    except SignalRateLimited as e:
        raise HTTPException(
            status_code=429,
            detail="Signal registration rate limited; retry later",
            headers={"Retry-After": str(e.retry_after_seconds)},
        ) from e

    except SignalSendFailed as e:
        if "captcha" in str(e).lower():
            raise HTTPException(status_code=400, detail="Signal requires a captcha to continue registration") from e
        raise HTTPException(status_code=400, detail="Signal registration failed; check the number and try again") from e

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for registration: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e


@router.post("/verify", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour", key_func=_signal_limit_key)
def signal_verify(request: Request, body: VerifyRequest, _admin=Depends(require_admin)):  # noqa: ARG001
    """Verify Signal registration with code (admin only).

    Complete the registration started with /signal/register.
    """
    backend = get_signal_backend()

    try:
        backend.verify(number=body.number, code=body.code)
        return Response(status_code=204)

    except SignalRecipientInvalid as e:
        _log.warning("Invalid phone number for verification: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    except SignalRateLimited as e:
        raise HTTPException(
            status_code=429,
            detail="Signal verification rate limited; retry later",
            headers={"Retry-After": str(e.retry_after_seconds)},
        ) from e

    except SignalSendFailed as e:
        raise HTTPException(status_code=400, detail="Signal verification failed; check the code and try again") from e

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for verification: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e


class ProfileRequest(BaseModel):
    """Request to update Signal profile."""

    name: str = Field(..., min_length=1, max_length=50, description="Profile display name")


@router.post("/profile-name", status_code=status.HTTP_204_NO_CONTENT)
def signal_update_profile(request: ProfileRequest, _admin=Depends(require_admin)):
    """Update Signal profile name and avatar (admin only).

    Sets the display name shown in Signal chats and the justUp avatar.
    Avatar path resolution is handled by the backend.
    """
    backend = get_signal_backend()

    try:
        # Backend handles avatar path resolution
        backend.update_profile(name=request.name)
        return Response(status_code=204)

    except SignalSendFailed as e:
        raise HTTPException(status_code=400, detail="Signal profile update failed; try again later") from e

    except SignalNotRegistered as e:
        raise HTTPException(status_code=409, detail="No Signal account is linked") from e

    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for profile update: %s", e)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from e
