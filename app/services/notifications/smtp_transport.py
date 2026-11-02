#!/usr/bin/env python3
#
# app/services/notifications/smtp_transport.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single hardened SMTP transport shared by every email send path.

DECISIONS B6 (P0): SMTP must be encrypted. This module is the ONE place that
opens an SMTP connection and sends a message, so the encryption rules apply
identically to the scheduler spooler closure.

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
  unauthenticated relay: neither username nor password AND the operator has
  explicitly stored "Use TLS" as off. That is the single documented escape hatch
  for a trusted local relay; it still refuses to carry credentials.
- Incomplete credentials (username without password or the reverse) are a
  configuration error on every port. They never degrade to an anonymous send.

The GUI keeps its two existing controls (Port + "Use TLS"). The "Use TLS"
switch is the explicit STARTTLS marker for non-465 ports and defaults to on
(see ``smtp_settings.resolve_use_tls``); leaving it off with no credentials is
what marks an unauthenticated plaintext relay. Port 465 is always implicit TLS
regardless of the switch.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Protocol

_log = logging.getLogger(__name__)

# Port that denotes implicit TLS (SMTPS). Everything else is plaintext-or-STARTTLS.
IMPLICIT_TLS_PORT = 465


class SMTPTransportConfig(Protocol):
    """Connection attributes the transport needs.

    Deliberately a structural type, so any config object with these attributes
    satisfies it without a shared base class or circular import.
    """

    host: str | None
    port: int
    user: str | None
    password: str | None
    use_tls: bool


@dataclass(frozen=True)
class SMTPConnection:
    """Minimal connection config for callers without their own config object.

    The scheduler spooler closure builds this one.
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


class SMTPConfigurationError(smtplib.SMTPException):
    """Raised before any connection is opened when the stored SMTP settings are unusable."""


class SMTPNotAccepted(smtplib.SMTPException):
    """Connection, TLS, AUTH or RCPT stage failed before DATA: the server certainly did not accept the message."""


class SMTPAcceptanceUnknown(smtplib.SMTPException):
    """Connection lost or timed out after DATA started: the server may or may not have accepted the message."""


class SMTPPartialRefusal(smtplib.SMTPException):
    """The server accepted the message for some recipients and refused others.

    Resending would duplicate the mail for the accepted recipients, so the caller treats it as final.
    """

    def __init__(self, refused: dict[str, tuple[int, bytes]]):
        self.refused = refused
        codes = sorted({code for code, _text in refused.values()})
        super().__init__(f"{len(refused)} recipient(s) refused after partial acceptance (SMTP {codes})")


class SMTPEnvelopeError(smtplib.SMTPException):
    """The message does not name exactly one sender and one recipient; nothing is connected or sent."""


# Headers from which ``smtplib.send_message`` would derive further envelope recipients on its own.
_IMPLICIT_RECIPIENT_HEADERS = ("Cc", "Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc")
_ADDR_SPEC = re.compile(r"[^\s,;<>\"()\[\]\\]+@[^\s,;<>\"()\[\]\\]+")


def _single_address(msg: EmailMessage, header: str) -> str:
    values = msg.get_all(header, [])
    parsed = getaddresses([str(value) for value in values])
    # A header the parser had to repair (e.g. "a@x; b@y" is cut down to "a@x") carries defects.
    if (
        len(values) != 1
        or getattr(values[0], "defects", ())
        or len(parsed) != 1
        or not _ADDR_SPEC.fullmatch(parsed[0][1])
    ):
        raise SMTPEnvelopeError(f"message must carry exactly one valid {header} address; refusing to send")
    return parsed[0][1]


def _envelope(msg: EmailMessage) -> tuple[str, list[str]]:
    """Return (sender, recipient) and refuse any message that could reach a second recipient."""
    if any(msg.get_all(name) for name in _IMPLICIT_RECIPIENT_HEADERS):
        raise SMTPEnvelopeError("message carries Cc/Bcc/Resent recipients; refusing to send")
    return _single_address(msg, "From"), [_single_address(msg, "To")]


def _credentials(config: SMTPTransportConfig) -> bool:
    """True when username and password are both set, False when both are empty.

    Raises:
        SMTPConfigurationError: Only one of the two is set. Treating that as "no credentials"
            would allow an anonymous cleartext send for a relay that expects authentication.
    """
    has_user = bool((config.user or "").strip())
    has_password = bool(config.password)
    if has_user != has_password:
        raise SMTPConfigurationError(
            "SMTP credentials are incomplete: set both username and password, or neither; refusing to send"
        )
    return has_user


class _Phase:
    """Follows the DATA exchange through smtplib's public ``send``/``getreply`` hooks.

    States: ``idle`` -> ``command`` (DATA sent, 354 pending) -> ``ready`` (354 received, content not yet
    sent) -> ``content`` (content on the wire, final reply pending) -> ``accepted`` (final 2xx received).
    Only ``content`` leaves the acceptance open; a loss before it proves nothing was accepted. A server
    without these hooks (a test double) stays ``idle``, i.e. every loss counts as "not accepted".
    """

    def __init__(self) -> None:
        self.state = "idle"

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"

    def watch(self, server: smtplib.SMTP) -> None:
        send, getreply = getattr(server, "send", None), getattr(server, "getreply", None)
        if not (callable(send) and callable(getreply)):
            return

        def tracked_send(data, *args, **kwargs):
            if self.state == "idle" and isinstance(data, str) and data.lower().startswith("data"):
                self.state = "command"
            elif self.state == "ready":
                self.state = "content"
            return send(data, *args, **kwargs)

        def tracked_getreply(*args, **kwargs):
            code, text = getreply(*args, **kwargs)
            if self.state == "command":
                self.state = "ready" if code == 354 else "idle"
            elif self.state == "content":
                self.state = "accepted" if 200 <= code < 300 else "idle"
            return code, text

        server.send, server.getreply = tracked_send, tracked_getreply

    def submit(self, server: smtplib.SMTP, msg: EmailMessage, envelope: tuple[str, list[str]]) -> None:
        """Hand the message over and refuse to report a partial acceptance as success."""
        self.watch(server)
        refused = server.send_message(msg, from_addr=envelope[0], to_addrs=envelope[1])
        if isinstance(refused, dict) and refused:
            raise SMTPPartialRefusal(refused)


def _close_quietly(server: smtplib.SMTP) -> None:
    """Best-effort QUIT/close: a failure here can never change the outcome of the hand-off."""
    try:
        server.quit()
    except (OSError, smtplib.SMTPException) as exc:
        _log.debug("SMTP QUIT failed after the exchange (%s)", type(exc).__name__)
    finally:
        with suppress(Exception):
            server.close()


def send_message(msg: EmailMessage, config: SMTPTransportConfig, *, timeout: int = 30) -> None:
    """Hand ``msg`` to the SMTP server; success means only that the server accepted it.

    Network failures are classified by phase: before the content went out they raise
    ``SMTPNotAccepted`` (TLS errors stay untouched so the fail-closed policy applies); while the
    content or its final reply is outstanding they raise ``SMTPAcceptanceUnknown``. An explicit
    SMTP reply (e.g. ``SMTPDataError``) is never "unknown". A failing QUIT after the final 250
    cannot undo the acceptance.
    """
    envelope = _envelope(msg)
    phase = _Phase()
    try:
        _deliver(msg, config, timeout, phase, envelope)
    except OSError as exc:
        explicit_reply = isinstance(exc, smtplib.SMTPException) and not isinstance(exc, smtplib.SMTPServerDisconnected)
        if explicit_reply or phase.accepted:
            raise
        if phase.state == "content":
            raise SMTPAcceptanceUnknown(
                f"connection lost while sending the content ({type(exc).__name__}); acceptance unknown"
            ) from exc
        if isinstance(exc, TimeoutError | ConnectionError | smtplib.SMTPServerDisconnected):
            raise SMTPNotAccepted(
                f"not accepted, connection failed before the content was sent ({type(exc).__name__})"
            ) from exc
        raise


def _deliver(
    msg: EmailMessage, config: SMTPTransportConfig, timeout: int, phase: _Phase, envelope: tuple[str, list[str]]
) -> None:
    """Send ``msg`` over a connection secured per DECISIONS B6.

    Args:
        msg: Fully composed message to deliver.
        config: Connection settings (host, port, credentials, use_tls).
        timeout: Socket timeout in seconds.

    Raises:
        SMTPConfigurationError: Username or password is set without the other. Nothing is
            connected or sent.
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
        server = smtplib.SMTP_SSL(config.host, config.port, timeout=timeout, context=context)
        try:
            if has_credentials:
                server.login(config.user, config.password)
            phase.submit(server, msg, envelope)
        finally:
            _close_quietly(server)
        return

    # Non-465: plaintext socket. Either upgrade with STARTTLS or - only for an
    # explicit unauthenticated relay - send without encryption.
    server = smtplib.SMTP(config.host, config.port, timeout=timeout)
    try:
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
            phase.submit(server, msg, envelope)
            return

        # use_tls is off. Credentials over an unencrypted link are never allowed.
        if has_credentials:
            raise SMTPEncryptionError(
                f"{config.host}:{config.port} has credentials but TLS is disabled; refusing to send them in cleartext"
            )

        # Explicitly configured unauthenticated relay: plaintext send permitted.
        phase.submit(server, msg, envelope)
    finally:
        _close_quietly(server)
