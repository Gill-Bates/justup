#!/usr/bin/env python3
#
# app/services/notifications/smtp_transport.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single hardened SMTP transport shared by every email send path.

DECISIONS B6 (P0): SMTP must be encrypted. This module is the ONE place that
opens an SMTP connection and sends a message, so the encryption rules apply
identically to both the EmailSender channel and the scheduler spooler closure.

Rules enforced here:
- Port 465 uses implicit TLS (``smtplib.SMTP_SSL`` + a default-verified
  ``ssl`` context). This is SMTPS: the socket is encrypted before any SMTP
  command is spoken.
- Any other port REQUIRES STARTTLS. If the server does not advertise STARTTLS
  we raise and send nothing - we never silently downgrade to cleartext.
- SMTP AUTH (login) runs only after the connection is protected (implicit TLS
  or a completed STARTTLS handshake). Credentials are never put on an
  unprotected socket.
- A plaintext, unencrypted send is allowed ONLY for an explicitly configured
  unauthenticated relay: no username/password AND the operator has explicitly
  opted out of TLS. That is the single documented escape hatch for a trusted
  local relay; it still refuses to carry credentials.

The GUI keeps its two existing controls (Port + "Use TLS"). The "Use TLS"
switch is the explicit STARTTLS marker for non-465 ports; leaving it off with
no credentials is what marks an unauthenticated plaintext relay. Port 465 is
always implicit TLS regardless of the switch.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

# Port that denotes implicit TLS (SMTPS). Everything else is plaintext-or-STARTTLS.
IMPLICIT_TLS_PORT = 465


class SMTPTransportConfig(Protocol):
    """Connection attributes the transport needs.

    Deliberately a structural type so both the EmailSender ``SMTPConfig``
    dataclass and the scheduler's ``SMTPConnection`` satisfy it without a
    shared base class or circular import.
    """

    host: str | None
    port: int
    user: str | None
    password: str | None
    use_tls: bool


@dataclass(frozen=True)
class SMTPConnection:
    """Minimal connection config for callers without their own config object.

    The EmailSender already has a richer ``SMTPConfig`` (templates etc.) that
    structurally matches ``SMTPTransportConfig``; the scheduler spooler closure
    builds this one instead.
    """

    host: str | None
    port: int
    user: str | None
    password: str | None
    use_tls: bool


class SMTPEncryptionError(smtplib.SMTPException):
    """Raised when the connection cannot be secured as the policy requires.

    A subclass of ``SMTPException`` so existing ``except smtplib.SMTPException``
    handlers in the send paths turn it into a failed delivery result instead of
    crashing - the message is simply never sent in cleartext.
    """


def _credentials(config: SMTPTransportConfig) -> bool:
    """True when both a username and a password are configured for AUTH."""
    return bool(config.user and config.password)


def send_message(msg: EmailMessage, config: SMTPTransportConfig, *, timeout: int = 30) -> None:
    """Send ``msg`` over a connection secured per DECISIONS B6.

    Args:
        msg: Fully composed message to deliver.
        config: Connection settings (host, port, credentials, use_tls).
        timeout: Socket timeout in seconds.

    Raises:
        SMTPEncryptionError: The connection could not be protected as required
            (STARTTLS unavailable on a non-465 port, or credentials present on
            a connection that would otherwise stay in cleartext). Nothing is
            sent in these cases.
        smtplib.SMTPException / OSError: Normal SMTP/network failures, left for
            the caller's existing handlers.
    """
    has_credentials = _credentials(config)

    if config.port == IMPLICIT_TLS_PORT:
        # Implicit TLS: the socket is encrypted from the start, so AUTH is safe.
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.host, config.port, timeout=timeout, context=context) as server:
            if has_credentials:
                server.login(config.user, config.password)
            server.send_message(msg)
        return

    # Non-465: plaintext socket. Either upgrade with STARTTLS or - only for an
    # explicit unauthenticated relay - send without encryption.
    with smtplib.SMTP(config.host, config.port, timeout=timeout) as server:
        # ehlo() populates server.esmtp_features so we can check STARTTLS support.
        server.ehlo_or_helo_if_needed()

        if config.use_tls:
            if not server.has_extn("starttls"):
                # No silent cleartext fallback: the server cannot protect the
                # connection, so refuse to send rather than leak the message.
                raise SMTPEncryptionError(
                    f"{config.host}:{config.port} does not advertise STARTTLS; refusing to send in cleartext"
                )
            context = ssl.create_default_context()
            server.starttls(context=context)
            # Re-EHLO after STARTTLS: the feature set (incl. AUTH) is renegotiated.
            server.ehlo()
            if has_credentials:
                server.login(config.user, config.password)
            server.send_message(msg)
            return

        # use_tls is off. Credentials over an unencrypted link are never allowed.
        if has_credentials:
            raise SMTPEncryptionError(
                f"{config.host}:{config.port} has credentials but TLS is disabled; refusing to send them in cleartext"
            )

        # Explicitly configured unauthenticated relay: plaintext send permitted.
        server.send_message(msg)
