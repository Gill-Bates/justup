#!/usr/bin/env python3
#
# tests/test_smtp_handoff_alerting.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SMTP hand-off states and administrative alerting (no real mail, no external recipient).

A local SMTP double speaks the plain dialog (RCPT TO, DATA) so the transport result is real; the
spooler runs on a controlled clock. ``sent`` means "accepted by the SMTP server", never "delivered".
"""

# ruff: noqa: ARG002
from __future__ import annotations

import contextlib
import smtplib
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from app.db import sqlite as sqlite_db
from app.services.notifications import delivery_policy as policy
from app.services.notifications import smtp_transport
from app.services.notifications.smtp_transport import SMTPConnection
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests import test_spooler_resilience as _base
from tests.test_spooler_resilience import Clock, Recorder, _mail, _query, _row, _spooler, _tick  # noqa: F401

# Shared fixtures of the spooler tests (controlled clock, temporary queue database).
clock = _base.clock
db_path = _base.db_path

INCIDENT = "incident.delivery.email.handoff"


class _Relay:
    """Minimal SMTP server on localhost; ``mode`` decides how RCPT TO and DATA are answered."""

    def __init__(self, mode: str = "accept") -> None:
        self.mode = mode
        self.accepted = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept(), so the port really stops listening
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

    def _rcpt_reply(self, line: bytes) -> bytes:
        if self.mode == "rcpt550":
            return b"550 5.1.1 no such user\r\n"
        if self.mode == "rcpt451":
            return b"451 4.3.0 try again later\r\n"
        if self.mode == "partial" and b"bad@" in line:
            return b"550 5.1.1 no such user\r\n"
        return b"250 ok\r\n"

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        reader = conn.makefile("rb")
        conn.sendall(b"220 relay ready\r\n")
        for raw in reader:
            cmd = raw.strip().upper()
            if cmd.startswith((b"EHLO", b"HELO")):
                conn.sendall(b"250 relay\r\n")
            elif cmd.startswith(b"MAIL"):
                conn.sendall(b"250 ok\r\n")
            elif cmd.startswith(b"RCPT"):
                conn.sendall(self._rcpt_reply(raw.lower()))
            elif cmd.startswith(b"DATA"):
                if self.mode == "drop_before_data":
                    return
                conn.sendall(b"354 go\r\n")
                complete = False
                for body_line in reader:
                    if body_line == b".\r\n":
                        complete = True
                        break
                if not complete:
                    return  # client vanished before the final "." line: nothing was accepted
                if self.mode == "data_drop":
                    return  # connection lost before the verdict: acceptance unknown
                if self.mode == "data550":
                    conn.sendall(b"550 5.7.1 content rejected\r\n")
                    continue
                self.accepted += 1
                conn.sendall(b"250 queued\r\n")
            elif cmd.startswith(b"QUIT"):
                if self.mode == "quit500":
                    conn.sendall(b"500 5.5.1 quit not understood\r\n")
                elif self.mode == "quit_timeout":
                    threading.Event().wait(2)  # no answer within the client timeout
                else:
                    conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def email(self, to, subject, text, html, message_id_key=None, *, timeout: int = 5) -> bool:
        """Sender callable for the spooler that uses the real transport against this relay."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "justup@example.com", to, subject
        msg.set_content(text)
        config = SMTPConnection(host="127.0.0.1", port=self.port, user=None, password=None, use_tls=False)
        smtp_transport.send_message(msg, config, timeout=timeout)
        return True


@pytest.fixture
def relay():
    created: list[_Relay] = []

    def make(mode: str = "accept") -> _Relay:
        created.append(_Relay(mode))
        return created[-1]

    yield make
    for r in created:
        r.close()


def _inbox(db_path, like: str = "%"):
    return _query(
        db_path,
        "SELECT event_type, severity, title, body FROM notifications WHERE event_type LIKE ? ORDER BY id",
        (like,),
    )


# ─── SMTP dialog ────────────────────────────────────────────────────────────────


def test_accepted_handoff_is_sent_and_never_claims_delivery(db_path, clock, relay):
    r = relay("accept")
    qid = _mail(_spooler(db_path))
    _tick(db_path, clock, email=r.email)
    assert _row(db_path, qid)[0] == "sent"
    assert r.accepted == 1
    rows = _inbox(db_path, "delivery.email.sent")
    assert len(rows) == 1
    text = f"{rows[0][2]} {rows[0][3]}".lower()
    assert "accepted by the smtp server" in text or "accepted by smtp server" in text
    assert "not verifiable" in text
    assert "delivered" not in text


def test_recipient_rejected_with_550_fails_once_with_one_admin_alert(db_path, clock, relay):
    r = relay("rcpt550")
    sp = _spooler(db_path)
    qid = _mail(sp)
    for _ in range(4):
        _tick(db_path, clock, email=r.email)
    status, retries, _, error, _ = _row(db_path, qid)
    assert (status, retries) == ("failed", 0)
    assert error.startswith("[PERMANENT]")
    failed = _inbox(db_path, "delivery.email.failed")
    assert len(failed) == 1
    assert failed[0][1] == "error"
    assert "rejected by the server" in failed[0][3]
    assert "r@example.com" not in failed[0][3]


def test_temporary_451_is_retried_with_backoff(db_path, clock, relay):
    r = relay("rcpt451")
    qid = _mail(_spooler(db_path))
    _tick(db_path, clock, email=r.email)
    status, retries, _, _, next_retry = _row(db_path, qid)
    assert (status, retries) == ("pending", 1)
    wait = (
        datetime.fromisoformat(next_retry).replace(tzinfo=UTC) - clock.now
    ).total_seconds() + 10  # clock advanced one tick
    assert wait >= 55
    assert _inbox(db_path, "delivery.email.failed") == []


def test_connection_loss_after_data_is_uncertain_but_before_data_is_not(db_path, clock, relay):
    r = relay("data_drop")
    qid = _mail(_spooler(db_path))
    _tick(db_path, clock, email=r.email)
    status, _, _, error, _ = _row(db_path, qid)
    assert status == "pending"  # never booked as accepted
    assert error.startswith("[UNCERTAIN]")
    assert _inbox(db_path, "delivery.email.sent") == []
    # Nothing listens: the message certainly was not accepted, so no uncertain marker.
    closed = relay("accept")
    closed.close()
    qid2 = _mail(_spooler(db_path), subject="second")
    _tick(db_path, clock, email=closed.email)
    assert not _row(db_path, qid2)[3].startswith("[UNCERTAIN]")


def test_partial_acceptance_is_not_reported_as_success_and_not_retried(relay, monkeypatch):
    r = relay("partial")
    # The envelope guard now prevents several recipients; force two to test the transport-level safety net.
    monkeypatch.setattr(
        smtp_transport, "_envelope", lambda _msg: ("justup@example.com", ["good@example.com", "bad@example.com"])
    )
    with pytest.raises(smtp_transport.SMTPPartialRefusal) as caught:
        r.email("good@example.com, bad@example.com", "s", "t", "<p>t</p>")
    assert "good@" not in str(caught.value) and "bad@" not in str(caught.value)  # counts and codes only
    failure = policy.classify_failure(caught.value)
    assert failure.kind is policy.FailureKind.PERMANENT  # a retry would duplicate the mail for the accepted recipient


def test_queue_entry_with_several_recipients_is_never_sent(db_path, clock):
    rec = Recorder(clock)
    qid = _mail(_spooler(db_path), address="good@example.com, bad@example.com")
    _tick(db_path, clock, email=rec.email)
    assert rec.calls == []
    assert _row(db_path, qid)[0] == "failed"
    assert len(_inbox(db_path, "delivery.email.failed")) == 1


@pytest.mark.parametrize("mode", ["quit500", "quit_timeout"])
def test_failing_quit_after_accepted_data_stays_a_successful_handoff(db_path, clock, relay, mode):
    r = relay(mode)
    qid = _mail(_spooler(db_path))
    _tick(db_path, clock, email=lambda *a, **k: r.email(*a, timeout=1, **k))
    for _ in range(3):
        _tick(db_path, clock, email=r.email)
    assert _row(db_path, qid)[:2] == ("sent", 0)  # no retry, no second delivery
    assert r.accepted == 1
    assert _inbox(db_path, "delivery.email.failed") == []


def test_disconnect_before_data_is_not_accepted_and_retried(db_path, clock, relay):
    r = relay("drop_before_data")
    qid = _mail(_spooler(db_path))
    with pytest.raises(smtp_transport.SMTPNotAccepted):
        r.email("r@example.com", "s", "t", "<p>t</p>")
    _tick(db_path, clock, email=r.email)
    status, retries, _, error, _ = _row(db_path, qid)
    assert (status, retries) == ("pending", 1)
    assert not error.startswith("[UNCERTAIN]")


def test_disconnect_during_content_is_acceptance_unknown(relay):
    r = relay("data_drop")
    with pytest.raises(smtp_transport.SMTPAcceptanceUnknown) as caught:
        r.email("r@example.com", "s", "t", "<p>t</p>")
    failure = policy.classify_failure(caught.value)
    assert failure.uncertain and failure.kind is policy.FailureKind.TRANSIENT


def test_explicit_data_rejection_is_permanent_without_retry(db_path, clock, relay):
    r = relay("data550")
    qid = _mail(_spooler(db_path))
    for _ in range(3):
        _tick(db_path, clock, email=r.email)
    status, retries, _, error, _ = _row(db_path, qid)
    assert (status, retries) == ("failed", 0)
    assert "[UNCERTAIN]" not in error
    assert r.accepted == 0


class _TlsFlaky(smtplib.SMTP):
    """Real client that fails with an SSL error at a chosen point."""

    fail_at = "final"

    def ehlo(self, name=""):
        if self.fail_at == "handshake":
            raise ssl.SSLError("handshake failure")
        return super().ehlo(name)

    def getreply(self):
        if self.fail_at == "final" and getattr(self, "_content_sent", False):
            raise ssl.SSLError("bad record mac")
        return super().getreply()

    def send(self, s):
        if isinstance(s, bytes):
            self._content_sent = True
        return super().send(s)


@pytest.mark.parametrize(
    ("fail_at", "uncertain", "kind"),
    [
        ("final", True, policy.FailureKind.TRANSIENT),
        ("handshake", False, policy.FailureKind.LIMITED),
    ],
)
def test_ssl_errors_are_classified_by_phase(relay, monkeypatch, fail_at, uncertain, kind):
    r = relay("accept")
    monkeypatch.setattr(_TlsFlaky, "fail_at", fail_at)
    monkeypatch.setattr(smtplib, "SMTP", _TlsFlaky)
    with pytest.raises(Exception) as caught:
        r.email("r@example.com", "s", "t", "<p>t</p>")
    if fail_at == "handshake":
        assert isinstance(caught.value, ssl.SSLError)  # raw: the fail-closed TLS policy still applies
    else:
        assert isinstance(caught.value, smtp_transport.SMTPAcceptanceUnknown)
    failure = policy.classify_failure(caught.value)
    assert (failure.uncertain, failure.kind) == (uncertain, kind)


def test_classification_of_session_level_refusals():
    assert policy.classify_failure(smtplib.SMTPConnectError(554, b"blocked")).kind is policy.FailureKind.LIMITED
    assert policy.classify_failure(smtplib.SMTPHeloError(501, b"bad helo")).kind is policy.FailureKind.LIMITED
    assert policy.classify_failure(smtplib.SMTPConnectError(421, b"busy")).kind is policy.FailureKind.TRANSIENT
    unknown = policy.classify_failure(smtp_transport.SMTPAcceptanceUnknown("lost"))
    assert unknown.uncertain and unknown.kind is policy.FailureKind.TRANSIENT
    not_accepted = policy.classify_failure(smtp_transport.SMTPNotAccepted("down"))
    assert not not_accepted.uncertain and not_accepted.kind is policy.FailureKind.TRANSIENT


# ─── Outage alert, dedupe, recovery, restart ────────────────────────────────────


def _run(db_path, clock, rec, minutes: float, step: float = 60):
    for _ in range(int(minutes * 60 / step)):
        _tick(db_path, clock, email=rec.email, step=step)


def test_outage_warns_after_fifteen_minutes_once_without_leaking_details(db_path, clock):
    secret = ConnectionRefusedError("secret-pw for admin@example.com refused")
    rec = Recorder(clock, fail=secret)
    _mail(_spooler(db_path), address="alice@example.com")
    _run(db_path, clock, rec, 14)
    assert _inbox(db_path, INCIDENT) == []  # still within the grace period
    _run(db_path, clock, rec, 5)
    rows = _inbox(db_path, INCIDENT)
    assert len(rows) == 1
    _event, severity, title, body = rows[0]
    assert severity == "warning"
    assert "Email" in title and "SMTP server unreachable" in body and "1 message" in body
    for forbidden in ("secret-pw", "admin@example.com", "alice@example.com", "refused"):
        assert forbidden not in body
        assert forbidden not in title
    # Hours of further failures: still one row, no flood.
    _run(db_path, clock, rec, 600, step=300)
    assert len(_inbox(db_path, INCIDENT)) == 1
    assert len(_query(db_path, "SELECT id FROM notifications")) <= 2  # incident (+ final failure after 24 h)


def test_no_alert_for_pacing_waiting_or_first_failures(db_path, clock):
    rec = Recorder(clock)
    sp = _spooler(db_path)
    for i in range(12):
        _mail(sp, subject=f"m{i}")
    _run(db_path, clock, rec, 20, step=10)
    waiting = _mail(_spooler(db_path), subject="waiting")
    # A message that simply waits (never failed) must not alarm, however old it is.
    conn = sqlite_db.connect(db_path)
    conn.execute(
        "UPDATE notification_queue SET created_at = ?, next_retry_at = ? WHERE id = ?",
        ((clock.now - timedelta(hours=2)).isoformat(), (clock.now + timedelta(hours=1)).isoformat(), waiting),
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    _run(db_path, clock, rec, 5)
    assert _inbox(db_path, "incident.%") == []


def test_recovery_is_reported_once_as_handoff_not_delivery(db_path, clock):
    rec = Recorder(clock, fail=ConnectionRefusedError("down"))
    qid = _mail(_spooler(db_path))
    _run(db_path, clock, rec, 20)
    assert len(_inbox(db_path, INCIDENT)) == 1
    rec.fail = None
    for _ in range(40):
        _tick(db_path, clock, email=rec.email, step=60)
        if _row(db_path, qid)[0] == "sent":
            break
    assert _row(db_path, qid)[0] == "sent"
    more = _mail(_spooler(db_path), subject="later")
    _tick(db_path, clock, email=rec.email)
    assert _row(db_path, more)[0] == "sent"
    recovered = _inbox(db_path, f"{INCIDENT}.recovered")
    assert len(recovered) == 1
    assert recovered[0][1] == "success"
    assert "handoff succeeded again" in recovered[0][3]
    assert "delivered" not in recovered[0][3].lower()


def test_restart_keeps_the_state_and_invents_no_recovery(db_path, clock):
    rec = Recorder(clock)
    # A scheduler restart on a healthy system never produces a recovery message.
    _mail(_spooler(db_path))
    _tick(db_path, clock, email=rec.email)
    assert _inbox(db_path, f"{INCIDENT}.recovered") == []

    rec.fail = ConnectionRefusedError("down")
    _mail(_spooler(db_path), subject="x")
    _run(db_path, clock, rec, 20)  # every tick builds a fresh spooler: a restart per tick
    assert len(_inbox(db_path, INCIDENT)) == 1
    stored = _query(db_path, "SELECT value FROM settings WHERE key = 'inbox_incident.delivery.email.handoff'")
    assert stored  # the open incident lives in SQLite, not in process memory
    _run(db_path, clock, rec, 30)
    assert len(_inbox(db_path, INCIDENT)) == 1
    assert _inbox(db_path, f"{INCIDENT}.recovered") == []


def test_expired_message_fails_with_one_alert_linked_to_the_open_warning(db_path, clock):
    rec = Recorder(clock, fail=ConnectionRefusedError("down"))
    qid = _mail(_spooler(db_path))
    _run(db_path, clock, rec, 25 * 60, step=300)
    status, _, _, error, _ = _row(db_path, qid)
    assert status == "failed"
    assert error.startswith("[EXPIRED]")
    assert len(_inbox(db_path, "delivery.email.failed")) == 1
    incident = _inbox(db_path, INCIDENT)
    assert len(incident) == 1  # updated in place, no second warning
    assert incident[0][1] == "error"
    assert "Email handoff failed" in incident[0][2]
    _run(db_path, clock, rec, 60, step=300)
    assert len(_inbox(db_path, "delivery.email.failed")) == 1


def test_test_mail_failure_is_final_after_one_attempt(db_path, clock):
    rec = Recorder(clock, fail=smtplib.SMTPRecipientsRefused({"t@example.com": (550, b"no")}))
    sp = _spooler(db_path)
    qid = sp.enqueue(
        "test",
        "email",
        0,
        "Test",
        "t@example.com",
        {"subject": "s", "text_body": "t", "html_body": "<p>t</p>"},
        immediate=True,
    )
    for _ in range(5):
        _tick(db_path, clock, email=rec.email)
    assert _row(db_path, qid)[0] == "failed"
    assert len(rec.calls) == 1  # the one-shot rule of test messages is kept
    assert len(_inbox(db_path, "delivery.email.failed")) == 1


# ─── Dashboard / settings statistics ────────────────────────────────────────────


def test_stats_report_recent_failures_for_an_empty_active_queue(db_path, clock, relay):
    r = relay("rcpt550")
    sp = _spooler(db_path)
    for i in range(3):
        _mail(sp, subject=f"f{i}")
    for _ in range(6):
        _tick(db_path, clock, email=r.email)
    stats = _spooler(db_path).get_queue_stats_detailed()
    assert stats["by_status"].get("pending", 0) == 0 and stats["by_status"].get("processing", 0) == 0
    assert stats["by_status"]["failed"] == 3
    assert stats["failed_recent"] == 3
    clock.advance(25 * 3600)
    assert _spooler(db_path).get_queue_stats_detailed()["failed_recent"] == 0


def test_success_while_old_mails_wait_neither_recovers_early_nor_reopens(db_path, clock):
    rec = Recorder(clock, fail=ConnectionRefusedError("down"))
    old = [_mail(_spooler(db_path), subject=f"old{i}") for i in range(2)]
    _run(db_path, clock, rec, 20)
    assert len(_inbox(db_path, INCIDENT)) == 1
    # The server works again: a new mail succeeds while the old ones still wait for their backoff.
    rec.fail = None
    fresh = _mail(_spooler(db_path), subject="fresh")
    conn = sqlite_db.connect(db_path)
    conn.execute(
        "UPDATE notification_queue SET next_retry_at = ? WHERE id IN (?, ?)",
        ((clock.now + timedelta(minutes=10)).isoformat(), *old),
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    _tick(db_path, clock, email=rec.email)
    assert _row(db_path, fresh)[0] == "sent"
    # Old mails are still held back, so the outage is not over yet: no recovery, and no second warning.
    assert _inbox(db_path, f"{INCIDENT}.recovered") == []
    _run(db_path, clock, rec, 3)
    assert len(_inbox(db_path, INCIDENT)) == 1
    _run(db_path, clock, rec, 15)
    assert all(_row(db_path, q)[0] == "sent" for q in old)
    assert len(_inbox(db_path, INCIDENT)) == 1
    assert len(_inbox(db_path, f"{INCIDENT}.recovered")) == 1


class _ContentSendBreaks(smtplib.SMTP):
    """Real client whose socket write fails at a chosen point of the DATA exchange."""

    break_on = "content"

    def send(self, s):
        is_content = isinstance(s, bytes)
        is_data_command = isinstance(s, str) and s.lower().startswith("data")
        if (self.break_on == "content" and is_content) or (self.break_on == "command" and is_data_command):
            self.close()
            raise smtplib.SMTPServerDisconnected("Server not connected")  # what smtplib.send() raises on OSError
        return super().send(s)


@pytest.mark.parametrize(
    ("break_on", "expected"),
    [
        ("content", smtp_transport.SMTPAcceptanceUnknown),  # failure inside send() of the content, after 354
        ("command", smtp_transport.SMTPNotAccepted),  # DATA command never left: certainly not accepted
    ],
)
def test_failure_inside_send_is_phased_by_the_data_exchange(relay, monkeypatch, break_on, expected):
    r = relay("accept")
    monkeypatch.setattr(_ContentSendBreaks, "break_on", break_on)
    monkeypatch.setattr(smtplib, "SMTP", _ContentSendBreaks)
    with pytest.raises(expected):
        r.email("r@example.com", "s", "t", "<p>t</p>")
    assert r.accepted == 0
