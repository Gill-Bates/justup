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

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ..services.notifications.signal_backend import get_signal_backend
from ..services.notifications.signal_errors import (
    SignalNotRegistered,
    SignalRecipientInvalid,
    SignalSendFailed,
    SignalTimeout,
    SignalUnavailable,
)
from ..utils.phone import normalize_phone
from .auth import get_current_user
from ..db import sqlite as sqlite_db
from ..utils.deps import get_conn

router = APIRouter(prefix="/signal", tags=["signal"])
_log = logging.getLogger(__name__)


# ─── Models ─────────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    """Request to send a Signal message."""
    recipient: str = Field(..., min_length=5, max_length=20, description="Phone number in E.164 format")
    message: str = Field(..., min_length=1, max_length=4000, description="Message text")
    
    @field_validator('recipient')
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
    captcha: Optional[str] = Field(None, description="Captcha token if required")
    
    @field_validator('number')
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


class UnregisterRequest(BaseModel):
    """Request to unregister Signal account."""
    delete_cache: bool = Field(True, description="Delete local cache data (keys, session data)")


# ─── Helpers ────────────────────────────────────────────────────────────────────

def require_admin(user=Depends(get_current_user)):
    """Dependency that requires admin privileges."""
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


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
        
    except SignalUnavailable as e:
        # Return degraded status with error in payload (always 200 for UI badges)
        _log.warning("Signal unavailable: %s", e)
        return {
            "reachable": False,
            "registered": False,
            "linked": False,
            "accounts": [],
            "error": str(e),
        }


@router.post("/send", status_code=status.HTTP_204_NO_CONTENT)
def signal_send(
    request: SendMessageRequest,
    http_request: Request,
    conn=Depends(get_conn),
    _admin=Depends(require_admin),
):
    """Send a Signal message via spooler (admin only)."""
    from ..services.notifications.spooler import get_spooler, NotificationType, ChannelType
    from ..utils.constants import SettingKeys
    
    # Get sender number
    signal_sender_number = sqlite_db.get_setting(
        conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None
    )
    if not signal_sender_number:
        raise HTTPException(status_code=409, detail="Signal sender not configured")
    
    try:
        db_path = http_request.app.state.db_path
        spooler = get_spooler(lambda: sqlite_db.connect(db_path))
        spooler.enqueue(
            notification_type=NotificationType.TEST,
            channel_type=ChannelType.SIGNAL,
            recipient_id=0,
            recipient_name="Admin",
            recipient_address=request.recipient,
            payload={
                "message": request.message,
                "sender": signal_sender_number,
            },
            immediate=True,
        )
        return Response(status_code=204)
    except Exception as e:
        _log.error("Failed to enqueue Signal message: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qr")
def signal_qr(_admin=Depends(require_admin)):
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
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/link/confirm")
def signal_link_confirm(_admin=Depends(require_admin)):
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
                detail="No link process active. Please generate a QR code first."
            )
        
        # Wait for the link process to complete
        success = backend.wait_for_link(session_id=session_id, timeout=60)
        
        if not success:
            _log.warning("Signal link timeout after 60 seconds")
            raise HTTPException(
                status_code=504,
                detail="Timeout waiting for link confirmation. Please try again."
            )
        
        # Link successful - get accounts
        accounts = backend.list_accounts()
        
        return {"success": True, "accounts": accounts}
        
    except (SignalUnavailable, SignalTimeout) as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/link/cancel", status_code=status.HTTP_204_NO_CONTENT)
def signal_link_cancel(_admin=Depends(require_admin)):
    """Cancel any running link process (admin only)."""
    backend = get_signal_backend()
    backend.cancel_link()
    return Response(status_code=204)


@router.post("/unregister", status_code=status.HTTP_204_NO_CONTENT)
def signal_unregister(request: UnregisterRequest, _admin=Depends(require_admin)):
    """Unregister Signal account (admin only).
    
    Args:
        delete_cache: If True, deletes local signal-cli cache data. Default: True
    """
    backend = get_signal_backend()
    
    try:
        backend.unregister(delete_cache=request.delete_cache)
        return Response(status_code=204)
        
    except SignalNotRegistered:
        raise HTTPException(status_code=409, detail="No account to unregister")
        
    except (SignalUnavailable, SignalTimeout) as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/unlink", status_code=status.HTTP_204_NO_CONTENT)
def signal_unlink(request: UnregisterRequest, _admin=Depends(require_admin)):
    """Unlink Signal device (admin only).
    
    Removes the Signal connection from justUp. The Signal account remains active.
    
    Args:
        delete_cache: If True, deletes local signal-cli cache data. Default: False
    """
    backend = get_signal_backend()
    
    try:
        backend.unlink_device(delete_cache=request.delete_cache)
        return Response(status_code=204)
        
    except SignalNotRegistered:
        raise HTTPException(status_code=409, detail="No account to unlink")
        
    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for unlink: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/accounts")
def signal_accounts(_admin=Depends(require_admin)):
    """List registered Signal accounts (admin only)."""
    backend = get_signal_backend()
    
    try:
        accounts = backend.list_accounts()
        return {"accounts": accounts}
        
    except (SignalUnavailable, SignalTimeout) as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/register", status_code=status.HTTP_204_NO_CONTENT)
def signal_register(request: RegisterRequest, _admin=Depends(require_admin)):
    """Start Signal registration for a phone number (admin only).
    
    Sends a verification code via SMS (default) or voice call.
    After calling this, use /signal/verify to complete registration.
    """
    backend = get_signal_backend()
    
    try:
        backend.register(
            number=request.number,
            use_voice=request.use_voice,
            captcha=request.captcha,
        )
        return Response(status_code=204)
        
    except SignalRecipientInvalid as e:
        _log.warning("Invalid phone number for registration: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
        
    except SignalSendFailed as e:
        _log.error("Signal registration failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
        
    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for registration: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/verify", status_code=status.HTTP_204_NO_CONTENT)
def signal_verify(request: VerifyRequest, _admin=Depends(require_admin)):
    """Verify Signal registration with code (admin only).
    
    Complete the registration started with /signal/register.
    """
    backend = get_signal_backend()
    
    try:
        backend.verify(number=request.number, code=request.code)
        return Response(status_code=204)
        
    except SignalRecipientInvalid as e:
        _log.warning("Invalid phone number for verification: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
        
    except SignalSendFailed as e:
        _log.error("Signal verification failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
        
    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for verification: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


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
        _log.error("Signal profile update failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
        
    except (SignalUnavailable, SignalTimeout) as e:
        _log.warning("Signal unavailable for profile update: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
