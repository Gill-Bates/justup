#!/usr/bin/env python3
#
# tests/test_smtp_transport_integration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Transport against a real SMTP server on localhost (real sockets, real smtplib, no fakes).

The server records the envelope it actually received and misbehaves at a chosen point of the dialog.
No mail leaves the machine.
"""

from __future__ import annotations

import contextlib
import email
import smtplib
import socket
import threading
from email.message import EmailMessage, Message

import pytest

from app.services.notifications import delivery_policy as policy
from app.services.notifications import smtp_transport
from app.services.notifications.smtp_transport import SMTPConnection
from tests import _support  # noqa: F401 - imported for its sys.path side effect


class _Server:
    """One-connection-at-a-time SMTP server; ``mode`` selects where the dialog goes wrong."""

    def __init__(self, mode: str = "accept", *, starttls: bool = False) -> None:
        self.mode, self.starttls = mode, starttls
        self.rcpts: list[str] = []
        self.mails: list[str] = []
        self.commands: list[str] = []
        self._release = threading.Event()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self._release.set()
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                self._session(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        reader = conn.makefile("rb")
        conn.sendall(b"220 test ready\r\n")
        for raw in reader:
            line = raw.decode("ascii", "replace").strip()
            self.commands.append(line.split(" ")[0].upper())
            verb = line.upper()
            if verb.startswith("EHLO"):
                conn.sendall(b"250-test\r\n250 STARTTLS\r\n" if self.starttls else b"250 test\r\n")
            elif verb.startswith("RCPT"):
                self.rcpts.append(line.partition(":")[2])
                reply = {"rcpt550": b"550 5.1.1 no such user\r\n"}.get(self.mode, b"250 ok\r\n")
                conn.sendall(reply)
            elif verb.startswith("DATA"):
                if self.mode == "drop_at_data_command":
                    return
                conn.sendall(b"354 go\r\n")
                if self.mode == "drop_mid_content":
                    reader.readline()
                    return
                if self.mode == "hang_after_354":
                    self._release.wait(10)  # never reads, never answers
                    return
                body = []
                for body_line in reader:
                    if body_line == b".\r\n":
                        break
                    body.append(body_line)
                self.mails.append(b"".join(body).decode("utf-8", "replace"))
                if self.mode == "drop_after_content":
                    return
                conn.sendall(
                    {"data451": b"451 4.3.0 try later\r\n", "data550": b"550 5.7.1 rejected\r\n"}.get(
                        self.mode, b"250 queued\r\n"
                    )
                )
            elif verb.startswith("QUIT"):
                if self.mode == "quit_drop":
                    return
                conn.sendall(b"500 quit failed\r\n" if self.mode == "quit500" else b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")


@pytest.fixture
def server():
    created: list[_Server] = []

    def make(mode: str = "accept", **kwargs) -> _Server:
        created.append(_Server(mode, **kwargs))
        return created[-1]

    yield make
    for s in created:
        s.close()


def _message(**headers: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "justup@example.com", "ops@example.com", "alert"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("body line " * 200)
    return msg


def _send(srv: _Server, msg: Message | None = None, *, use_tls: bool = False, timeout: int = 2) -> None:
    config = SMTPConnection(host="127.0.0.1", port=srv.port, user=None, password=None, use_tls=use_tls)
    smtp_transport.send_message(msg or _message(), config, timeout=timeout)


def _failure(srv: _Server, **kwargs) -> policy.Failure:
    with pytest.raises(Exception) as caught:
        _send(srv, **kwargs)
    return policy.classify_failure(caught.value)


# ─── Dialog outcomes ────────────────────────────────────────────────────────────


def test_accepted_message_reaches_exactly_one_recipient(server):
    srv = server()
    _send(srv)
    assert srv.rcpts == ["<ops@example.com>"]
    assert len(srv.mails) == 1


@pytest.mark.parametrize("mode", ["quit500", "quit_drop"])
def test_quit_failure_after_250_is_still_success(server, mode):
    srv = server(mode)
    _send(srv)
    assert len(srv.mails) == 1


def test_rcpt_550_is_permanent_and_sends_no_content(server):
    srv = server("rcpt550")
    assert _failure(srv).kind is policy.FailureKind.PERMANENT
    assert srv.mails == []


def test_data_451_is_transient_and_certain(server):
    failure = _failure(server("data451"))
    assert failure.kind is policy.FailureKind.TRANSIENT and not failure.uncertain


def test_data_550_is_permanent(server):
    assert _failure(server("data550")).kind is policy.FailureKind.PERMANENT


def test_drop_at_data_command_is_not_accepted(server):
    srv = server("drop_at_data_command")
    failure = _failure(srv)
    assert failure.kind is policy.FailureKind.TRANSIENT and not failure.uncertain
    assert srv.mails == []


@pytest.mark.parametrize("mode", ["drop_mid_content", "drop_after_content", "hang_after_354"])
def test_loss_once_content_may_have_left_is_acceptance_unknown(server, mode):
    failure = _failure(server(mode), timeout=1)
    assert failure.kind is policy.FailureKind.TRANSIENT
    assert failure.uncertain  # a "certainly not accepted" verdict here would retry and duplicate the mail


def test_large_content_cut_mid_transfer_is_acceptance_unknown(server):
    msg = _message()
    msg.set_content("x" * 5_000_000)
    assert _failure(server("drop_mid_content"), msg=msg, timeout=3).uncertain


# ─── Envelope ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        {"Cc": "cc@example.com"},
        {"Bcc": "bcc@example.com"},
        {"Resent_To": "resent@example.com"},
        {"Resent_Bcc": "resent-bcc@example.com"},
    ],
)
def test_implicit_recipient_headers_are_refused_before_connecting(server, extra):
    srv = server()
    failure = _failure(srv, msg=_message(**extra))
    assert failure.kind is policy.FailureKind.PERMANENT
    assert srv.rcpts == [] and srv.commands == []


@pytest.mark.parametrize(
    "to", ["a@example.com, b@example.com", "a@example.com; b@example.com", "a@example.com b@example.com", ""]
)
def test_multiple_or_malformed_recipients_are_refused_before_connecting(server, to):
    srv = server()
    msg = _message()
    del msg["To"]
    msg["To"] = to
    assert _failure(srv, msg=msg).kind is policy.FailureKind.PERMANENT
    assert srv.commands == []


def test_second_to_header_is_refused(server):
    # A parsed (compat32) message can carry duplicate headers that EmailMessage itself refuses to build.
    msg = email.message_from_string(
        "From: justup@example.com\nTo: a@example.com\nTo: b@example.com\nSubject: s\n\nbody\n"
    )
    srv = server()
    assert _failure(srv, msg=msg).kind is policy.FailureKind.PERMANENT
    assert srv.commands == []


def test_header_injection_is_rejected_by_the_message_model():
    msg = _message()
    del msg["Subject"], msg["To"]
    with pytest.raises(ValueError, match="linefeed"):
        msg["Subject"] = "hi\r\nBcc: evil@example.com"
    with pytest.raises(ValueError, match="linefeed"):
        msg["To"] = "a@example.com\r\nBcc: evil@example.com"


def test_display_name_form_still_yields_a_single_bare_recipient(server):
    srv = server()
    msg = _message()
    del msg["To"]
    msg["To"] = "Ops Team <ops@example.com>"
    _send(srv, msg)
    assert srv.rcpts == ["<ops@example.com>"]


# ─── TLS policy over a real socket ──────────────────────────────────────────────


def test_missing_starttls_refuses_to_send_in_cleartext(server):
    srv = server(starttls=False)
    with pytest.raises(smtp_transport.SMTPEncryptionError):
        _send(srv, use_tls=True)
    assert srv.mails == [] and srv.rcpts == []


def test_credentials_without_tls_never_reach_the_wire(server):
    srv = server()
    config = SMTPConnection(host="127.0.0.1", port=srv.port, user="u", password="p", use_tls=False)  # noqa: S106 - dummy value
    with pytest.raises(smtp_transport.SMTPEncryptionError):
        smtp_transport.send_message(_message(), config, timeout=2)
    assert "AUTH" not in srv.commands and srv.mails == []


def test_smtplib_exposes_the_hooks_the_phase_model_relies_on():
    # The classifier wraps SMTP.send/getreply; if smtplib drops them every loss would count as "not accepted".
    assert callable(smtplib.SMTP.send) and callable(smtplib.SMTP.getreply)
