#!/usr/bin/env python3
#
# tests/test_smtp_transport.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Hardened SMTP transport: encryption is enforced on every send path (DECISIONS B6).

Covers each branch of smtp_transport.send_message using fake smtplib clients:
- port 465 -> implicit TLS (SMTP_SSL), AUTH allowed
- non-465 with STARTTLS advertised -> STARTTLS then AUTH, never cleartext
- non-465 with STARTTLS refused -> raises, sends nothing
- credentials over a would-be-cleartext link -> refused
- explicit unauthenticated relay (no creds, TLS off) -> plaintext send allowed
- incomplete credentials (user xor password) -> configuration error, nothing connected or sent
- failed TLS negotiation -> no AUTH and no message, against fakes and real localhost doubles
"""

from __future__ import annotations

import socket
import ssl
import threading
from email.message import EmailMessage
from typing import ClassVar

import pytest

from app.services.notifications import smtp_transport
from tests import _support  # noqa: F401 - imported for its sys.path side effect


class _FakeSMTP:
    """Fake smtplib.SMTP capturing the ordered sequence of operations.

    ``starttls_available`` toggles whether the server advertises STARTTLS so a
    test can exercise the refused-fallback path.
    """

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ops: list[str] = []
        self.sent: list[EmailMessage] = []
        self.logged_in: tuple[str, str] | None = None
        self.started_tls = False
        self.starttls_available = True
        _FakeSMTP.instances.append(self)

    # Context manager protocol (smtplib clients support `with`).
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def quit(self):
        self.ops.append("close")

    def close(self):
        pass

    def ehlo_or_helo_if_needed(self):
        self.ops.append("ehlo")

    def ehlo(self):
        self.ops.append("ehlo")

    def has_extn(self, name):
        return name == "starttls" and self.starttls_available

    def starttls(self, context=None):
        assert context is not None  # policy: always a verified ssl context
        self.started_tls = True
        self.ops.append("starttls")

    def login(self, user, password):
        # AUTH must never run before the connection is protected.
        assert self.started_tls, "login attempted before STARTTLS"
        self.logged_in = (user, password)
        self.ops.append("login")

    def send_message(self, msg, from_addr=None, to_addrs=None):  # noqa: ARG002
        assert to_addrs is not None and len(to_addrs) == 1  # the envelope is always explicit and single
        self.sent.append(msg)
        self.ops.append("send")


class _FakeSMTPSSL(_FakeSMTP):
    """Fake smtplib.SMTP_SSL: the socket is already encrypted, so login is safe."""

    ssl_instances: ClassVar[list[_FakeSMTPSSL]] = []

    def __init__(self, host, port, timeout=None, context=None):
        super().__init__(host, port, timeout=timeout)
        assert context is not None  # policy: implicit TLS uses a verified context
        self.started_tls = True  # implicit TLS: protected from the first byte
        _FakeSMTPSSL.ssl_instances.append(self)


@pytest.fixture
def smtp_fakes(monkeypatch: pytest.MonkeyPatch):
    _FakeSMTP.instances = []
    _FakeSMTPSSL.ssl_instances = []
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP, _FakeSMTPSSL


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "s"
    m["From"] = "from@example.com"
    m["To"] = "to@example.com"
    m.set_content("body")
    return m


_TEST_PASSWORD = "p"  # noqa: S105 - dummy credential for fake SMTP, never a real secret


def _config(**kw):
    base = {"host": "mail.example.com", "port": 587, "user": None, "password": None, "use_tls": True}
    base.update(kw)
    return smtp_transport.SMTPConnection(**base)


def test_port_465_uses_implicit_tls_and_logs_in(smtp_fakes):
    _, fake_ssl = smtp_fakes
    smtp_transport.send_message(_msg(), _config(port=465, user="u", password=_TEST_PASSWORD, use_tls=False))
    assert len(fake_ssl.ssl_instances) == 1
    inst = fake_ssl.ssl_instances[0]
    assert inst.logged_in == ("u", "p")
    assert inst.ops == ["login", "send", "close"]  # no STARTTLS on an already-encrypted socket


def test_starttls_success_then_auth(smtp_fakes):
    fake, _ = smtp_fakes
    smtp_transport.send_message(_msg(), _config(port=587, user="u", password=_TEST_PASSWORD, use_tls=True))
    inst = fake.instances[0]
    assert inst.started_tls is True
    assert inst.logged_in == ("u", "p")
    # STARTTLS must precede login, which must precede send.
    assert inst.ops.index("starttls") < inst.ops.index("login") < inst.ops.index("send")


def test_starttls_refused_raises_and_sends_nothing(smtp_fakes):
    fake, _ = smtp_fakes

    def _no_starttls(host, port, timeout=None):
        inst = _FakeSMTP(host, port, timeout)
        inst.starttls_available = False
        return inst

    # Patch construction so the single instance advertises no STARTTLS.
    import app.services.notifications.smtp_transport as mod

    orig = mod.smtplib.SMTP
    mod.smtplib.SMTP = _no_starttls
    try:
        with pytest.raises(smtp_transport.SMTPEncryptionError):
            smtp_transport.send_message(_msg(), _config(port=587, user="u", password=_TEST_PASSWORD, use_tls=True))
    finally:
        mod.smtplib.SMTP = orig

    inst = fake.instances[-1]
    assert inst.sent == []  # nothing left the system in cleartext
    assert inst.started_tls is False
    assert "send" not in inst.ops


def test_credentials_without_tls_are_refused(smtp_fakes):
    fake, _ = smtp_fakes
    with pytest.raises(smtp_transport.SMTPEncryptionError):
        smtp_transport.send_message(_msg(), _config(port=25, user="u", password=_TEST_PASSWORD, use_tls=False))
    inst = fake.instances[0]
    assert inst.sent == []  # credentials never go over an unencrypted link
    assert inst.logged_in is None


def test_explicit_unauthenticated_relay_sends_plaintext(smtp_fakes):
    fake, _ = smtp_fakes
    smtp_transport.send_message(_msg(), _config(port=25, user=None, password=None, use_tls=False))
    inst = fake.instances[0]
    assert len(inst.sent) == 1  # permitted: no creds + TLS explicitly off
    assert inst.started_tls is False
    assert inst.logged_in is None


@pytest.mark.parametrize(
    ("port", "use_tls", "user", "password"),
    [
        (587, True, "u", None),
        (587, True, None, "secret-value"),
        (25, False, "u", ""),
        (25, False, "", "secret-value"),
        (465, False, "u", None),
        (465, True, None, "secret-value"),
    ],
)
def test_incomplete_credentials_are_a_configuration_error(smtp_fakes, port, use_tls, user, password):
    fake, fake_ssl = smtp_fakes
    cfg = _config(port=port, use_tls=use_tls, user=user, password=password)
    with pytest.raises(smtp_transport.SMTPConfigurationError) as exc:
        smtp_transport.send_message(_msg(), cfg)
    assert "secret-value" not in str(exc.value)
    # Refused before any connection exists, so there is nothing that could carry a message.
    assert fake.instances == []
    assert fake_ssl.ssl_instances == []


def test_failed_tls_handshake_sends_no_credentials_and_no_message(smtp_fakes, monkeypatch):
    fake, _ = smtp_fakes

    def _bad_handshake(*_args, **_kwargs):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(_FakeSMTP, "starttls", _bad_handshake)
    with pytest.raises(ssl.SSLError):
        smtp_transport.send_message(_msg(), _config(port=587, user="u", password=_TEST_PASSWORD, use_tls=True))
    inst = fake.instances[0]
    assert inst.sent == []
    assert inst.logged_in is None
    assert "send" not in inst.ops


def test_port_465_ignores_a_disabled_starttls_switch(smtp_fakes):
    fake, fake_ssl = smtp_fakes
    smtp_transport.send_message(_msg(), _config(port=465, use_tls=False))
    assert len(fake_ssl.ssl_instances) == 1
    assert fake.instances[0] is fake_ssl.ssl_instances[0]  # no plain SMTP connection was opened


class _LocalSMTPDouble:
    """Tiny SMTP server on 127.0.0.1 recording the commands it receives (test double, no mail leaves the host)."""

    def __init__(self, *, offer_starttls: bool):
        self.offer_starttls = offer_starttls
        self.commands: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(10)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            reader = conn.makefile("rb")
            conn.sendall(b"220 localhost ready\r\n")
            try:
                for raw in reader:
                    line = raw.decode("ascii", "replace").strip()
                    self.commands.append(line.split(" ", 1)[0].upper())
                    verb = self.commands[-1]
                    if verb == "EHLO":
                        feats = (
                            ["250-localhost"] + (["250-STARTTLS"] if self.offer_starttls else []) + ["250 AUTH PLAIN"]
                        )
                        conn.sendall(("\r\n".join(feats) + "\r\n").encode())
                    elif verb == "STARTTLS":
                        # Not a TLS handshake: the client's ssl layer must reject this and stop.
                        conn.sendall(b"220 go ahead\r\n")
                        conn.sendall(b"this is not a TLS server hello\r\n")
                        return
                    elif verb == "QUIT":
                        conn.sendall(b"221 bye\r\n")
                        return
                    else:
                        conn.sendall(b"250 ok\r\n")
            except OSError:
                return

    def close(self):
        self._sock.close()
        self._thread.join(timeout=5)


@pytest.mark.parametrize("offer_starttls", [False, True], ids=["starttls-not-offered", "handshake-fails"])
def test_local_server_without_working_tls_receives_no_mail_and_no_auth(offer_starttls):
    server = _LocalSMTPDouble(offer_starttls=offer_starttls)
    try:
        cfg = smtp_transport.SMTPConnection("127.0.0.1", server.port, "u", _TEST_PASSWORD, True)
        with pytest.raises((smtp_transport.SMTPEncryptionError, ssl.SSLError, OSError)):
            smtp_transport.send_message(_msg(), cfg, timeout=5)
    finally:
        server.close()
    assert not {"MAIL", "RCPT", "DATA", "AUTH"} & set(server.commands)
