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
"""

from __future__ import annotations

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
        self.ops.append("close")
        return False

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

    def send_message(self, msg):
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
