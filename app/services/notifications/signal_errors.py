#!/usr/bin/env python3
#
# app/services/notifications/signal_errors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal-specific exception classes.

These exceptions are raised by SignalBackend implementations
and mapped to HTTP status codes in the API layer:

    SignalUnavailable -> 503 Service Unavailable
    SignalNotRegistered -> 409 Conflict
    SignalTimeout -> 503 Service Unavailable
    SignalRecipientInvalid -> 400 Bad Request
"""

from __future__ import annotations

__all__ = [
    "SignalError",
    "SignalNotRegistered",
    "SignalRateLimited",
    "SignalRecipientInvalid",
    "SignalSendFailed",
    "SignalTimeout",
    "SignalUnavailable",
]


class SignalError(Exception):
    """Base exception for all Signal-related errors."""


class SignalUnavailable(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """signal-cli binary not found or not executable."""


class SignalNotRegistered(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """No Signal account is registered/linked."""


class SignalTimeout(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """signal-cli subprocess timed out."""


class SignalRateLimited(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """
    Signal rejected the message due to rate limiting.

    This is a temporary condition and requires a long cooldown.
    """

    retry_after_seconds: int = 3600  # 1 hour minimum (class default)

    def __init__(self, message: str = "", retry_after_seconds: int = 3600) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SignalRecipientInvalid(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """Invalid recipient phone number."""


class SignalSendFailed(SignalError):  # noqa: N818 - public Signal exception name, kept for API stability
    """Message sending failed."""
