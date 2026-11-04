#!/usr/bin/env python3
#
# app/services/notifications/smtp_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single reader of the stored SMTP settings for the UI, the API and the send path.

The encryption rule lives here once: a missing, null or otherwise unusable ``smtp_use_tls``
value means STARTTLS is required. Only an explicitly stored JSON ``false`` is an
administrator's decision against encryption.
"""

from __future__ import annotations

import logging
import sqlite3

from ...db import sqlite as sqlite_db
from ...utils.constants import DEFAULT_SMTP_USE_TLS, SettingKeys
from ...utils.crypto import decrypt_secret
from ...utils.runtime_fields import is_secret_set
from ...utils.smtp_readiness import SmtpReadiness, smtp_readiness
from ...utils.tcp_ports import parse_port
from .smtp_transport import SMTPConfigurationError, SMTPConnection

_log = logging.getLogger(__name__)

_MISSING = object()
DEFAULT_SMTP_PORT = 587


def resolve_use_tls(conn: sqlite3.Connection) -> bool:
    """Return the effective STARTTLS requirement.

    Missing key -> default (required). Stored ``true`` -> required. Stored ``false`` -> the
    compatibility exception. Anything else (null, text, number, unparsable JSON) is never read
    as a decision against encryption: it counts as required and is logged.
    """
    try:
        stored = sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, _MISSING)
    except ValueError:
        _log.warning("Stored smtp_use_tls is not valid JSON; STARTTLS stays required")
        return DEFAULT_SMTP_USE_TLS
    if stored is _MISSING:
        return DEFAULT_SMTP_USE_TLS
    if isinstance(stored, bool):
        return stored
    _log.warning("Stored smtp_use_tls is not a boolean; STARTTLS stays required")
    return DEFAULT_SMTP_USE_TLS


def _stored_port(conn: sqlite3.Connection) -> int | None:
    """Return the stored SMTP port, or None when none is stored (the default applies).

    Raises:
        ValueError: A value is stored but is not a whole number from 1 to 65535 (the shared TCP port rule).
    """
    stored = sqlite_db.get_setting(conn, SettingKeys.SMTP_PORT, None)
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return None
    return parse_port(stored)


def _public_port(conn: sqlite3.Connection) -> int | None:
    """Port for display: an invalid stored value is logged and shown as empty, never raised into the UI."""
    try:
        return _stored_port(conn)
    except ValueError:
        _log.warning("Stored smtp_port is invalid (expected 1-65535); correct it in the SMTP settings")
        return None


def load_public_smtp_settings(conn: sqlite3.Connection) -> dict:
    """Return the non-secret SMTP settings; the password is reduced to a ``smtp_password_set`` flag."""
    get = sqlite_db.get_setting
    return {
        "smtp_enabled": bool(get(conn, SettingKeys.SMTP_ENABLED, False)),
        "smtp_host": get(conn, SettingKeys.SMTP_HOST, "") or "",
        "smtp_port": _public_port(conn),
        "smtp_from": get(conn, SettingKeys.SMTP_FROM, "") or "",
        "smtp_user": get(conn, SettingKeys.SMTP_USER, "") or "",
        "smtp_use_tls": resolve_use_tls(conn),
        "smtp_password_set": is_secret_set(conn, SettingKeys.SMTP_PASSWORD),
    }


def load_connection(conn: sqlite3.Connection) -> tuple[SMTPConnection, str]:
    """Build the transport config and sender address from the stored settings.

    Raises:
        RuntimeError: SMTP is disabled or host/sender are missing.
        SMTPConfigurationError: A stored password cannot be decrypted (it is never replaced by an
            empty password, which would turn the send into an anonymous one), or the stored port is invalid.
    """
    readiness = smtp_readiness(conn)
    if readiness is SmtpReadiness.DISABLED:
        raise RuntimeError("SMTP not enabled")
    if readiness is SmtpReadiness.INCOMPLETE:
        raise RuntimeError("SMTP not configured")

    host = sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST)
    sender = sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM)

    password = None
    encrypted = sqlite_db.get_setting(conn, SettingKeys.SMTP_PASSWORD)
    if encrypted:
        try:
            password = decrypt_secret(encrypted)
        except Exception:  # noqa: BLE001 - any decrypt failure must stop the send, details stay out of the message
            raise SMTPConfigurationError(
                "The stored SMTP password cannot be decrypted; re-enter it in the SMTP settings"
            ) from None

    try:
        port = _stored_port(conn)
    except ValueError:
        raise SMTPConfigurationError(
            "The stored SMTP port is invalid (expected 1-65535); correct it in the SMTP settings"
        ) from None
    config = SMTPConnection(
        host=host,
        port=DEFAULT_SMTP_PORT if port is None else port,
        user=sqlite_db.get_setting(conn, SettingKeys.SMTP_USER),
        password=password,
        use_tls=resolve_use_tls(conn),
    )
    return config, sender
