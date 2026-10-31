#!/usr/bin/env python3
#
# app/services/notifications/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification services - spooler-based alert distribution.

The notification system uses a spooler-first pattern:
- NotificationDispatcher: Resolves target → recipients, pre-renders messages, enqueues to spooler
- NotificationSpooler: Persistent SQLite queue — the single exit point for ALL notifications
- Signal/Email backends: Used only by the spooler's send callbacks (never called directly)

Architecture:
    ALL notifications go through the spooler. Nothing leaves the system
    without being enqueued first. This ensures visibility in the Spooler UI,
    automatic retry handling, and consistent rate limiting.
"""

from .dispatcher import (
    AlertPayload,
    DispatchResult,
    NotificationDispatcher,
    RecipientInfo,
    SendResult,
)
from .signal_backend import SignalBackend, get_signal_backend
from .signal_errors import (
    SignalError,
    SignalNotRegistered,
    SignalRecipientInvalid,
    SignalSendFailed,
    SignalTimeout,
    SignalUnavailable,
)
from .signal_types import SignalStatus
from .spooler import ChannelType, NotificationSpooler, NotificationType, get_spooler

__all__ = [
    "AlertPayload",
    "ChannelType",
    "DispatchResult",
    # Dispatcher (resolves recipients, enqueues to spooler)
    "NotificationDispatcher",
    # Spooler (single exit point for all notifications)
    "NotificationSpooler",
    "NotificationType",
    "RecipientInfo",
    "SendResult",
    # Signal backend (used by spooler callbacks only)
    "SignalBackend",
    "SignalError",
    "SignalNotRegistered",
    "SignalRecipientInvalid",
    "SignalSendFailed",
    "SignalStatus",
    "SignalTimeout",
    "SignalUnavailable",
    "get_signal_backend",
    "get_spooler",
]
