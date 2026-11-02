#!/usr/bin/env python3
#
# tests/test_spooler_resilience.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spooler resilience: restart recovery, outage retry policy, pacing, provider cooldowns, alert order.

A controlled clock replaces real waiting: ``Clock.sleep`` advances time instead of blocking.
"""

# ruff: noqa: ARG002
from __future__ import annotations

import smtplib
import ssl
import threading
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.db import sqlite as sqlite_db
from app.services.notifications import delivery_policy as policy
from app.services.notifications import spooler as spooler_mod
from app.services.notifications.signal_errors import (
    SignalNotInstalled,
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalTimeout,
)
from app.services.notifications.smtp_transport import SMTPConfigurationError, SMTPEncryptionError
from app.services.notifications.spooler import MAX_RETRIES, NotificationSpooler
from tests import _support  # noqa: F401 - imported for its sys.path side effect

SENDER_A = "+491110000001"
SENDER_B = "+491110000002"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(spooler_mod, "_utcnow", c)
    monkeypatch.setattr(spooler_mod.time, "sleep", c.sleep)
    return c


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "spool.db"
    boot = sqlite_db.connect(path)
    sqlite_db.init_schema(boot)
    sqlite_db.close_connection(boot)
    return path


def _spooler(db_path: Path) -> NotificationSpooler:
    """A fresh spooler on the same file: what a process restart produces."""
    return NotificationSpooler(lambda: sqlite_db.connect(db_path))


def _query(db_path: Path, sql: str, params: tuple = ()):
    conn = sqlite_db.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        sqlite_db.close_connection(conn)


def _row(db_path: Path, queue_id: int):
    return _query(
        db_path,
        "SELECT status, retry_count, limited_attempts, last_error, next_retry_at FROM notification_queue WHERE id = ?",
        (queue_id,),
    )[0]


def _mail(sp, subject: str = "s", payload: dict | None = None, address: str = "r@example.com") -> int:
    body = {"subject": subject, "text_body": "t", "html_body": "<p>t</p>", **(payload or {})}
    return sp.enqueue("alert", "email", 1, "R", address, body, immediate=True)


def _sig(sp, sender: str = SENDER_A, address: str = "+491110009999") -> int:
    return sp.enqueue("alert", "signal", 1, "R", address, {"message": "m", "sender": sender}, immediate=True)


def _tick(db_path: Path, clock: Clock, *, email=None, signal=None, step: float = 10, fresh: bool = True, sp=None):
    sp = sp or (_spooler(db_path) if fresh else None)
    result = sp.process_queue_sync(signal, email)
    clock.advance(step)
    return result


class Recorder:
    """Fake sender: records (time, subject/sender key) and fails while ``fail`` is set."""

    def __init__(self, clock: Clock, fail=None) -> None:
        self.clock = clock
        self.fail = fail  # exception instance/factory or callable(now) -> exception | None
        self.calls: list[tuple[datetime, str]] = []
        self.message_keys: list[str | None] = []

    def _maybe_fail(self):
        exc = self.fail(self.clock.now) if callable(self.fail) else self.fail
        if exc is not None:
            raise exc

    def email(self, to, subject, text, html, message_id_key=None):
        self.calls.append((self.clock.now, subject))
        self.message_keys.append(message_id_key)
        self._maybe_fail()
        return True

    def signal(self, phone, message, sender):
        self.calls.append((self.clock.now, sender))
        self._maybe_fail()
        return True

    @property
    def times(self) -> list[datetime]:
        return [t for t, _ in self.calls]


def _gaps(times: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in pairwise(times)]


# ─── Restart recovery ───────────────────────────────────────────────────────────


def test_due_pending_message_is_sent_by_a_recreated_spooler(db_path, clock):
    qid = _mail(_spooler(db_path))
    rec = Recorder(clock)
    _tick(db_path, clock, email=rec.email)  # a new instance of the spooler on the same file
    assert _row(db_path, qid)[0] == "sent"
    assert len(rec.calls) == 1


def test_processing_message_is_only_recovered_after_the_timeout(db_path, clock):
    sp = _spooler(db_path)
    done = _mail(sp, "done")
    qid = _mail(sp, "interrupted")
    conn = sqlite_db.connect(db_path)
    conn.execute("UPDATE notification_queue SET status = 'sent' WHERE id = ?", (done,))
    conn.execute(
        "UPDATE notification_queue SET status = 'processing', updated_at = ? WHERE id = ?",
        ((clock.now - timedelta(seconds=100)).isoformat(), qid),
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    rec = Recorder(clock)

    _tick(db_path, clock, email=rec.email)
    assert rec.calls == []  # claim still held by the (crashed) worker
    assert _row(db_path, qid)[0] == "processing"

    clock.advance(spooler_mod.PROCESSING_TIMEOUT_SECONDS)
    _tick(db_path, clock, email=rec.email)
    assert [s for _, s in rec.calls] == ["interrupted"]  # the finished message is not touched
    assert _row(db_path, qid)[0] == "sent"
    assert _row(db_path, done)[0] == "sent"


def test_repeated_restarts_lose_nothing_and_send_once(db_path, clock):
    sp = _spooler(db_path)
    ids = [_mail(sp, f"m{i}") for i in range(8)]
    rec = Recorder(clock)
    for _ in range(6):
        _tick(db_path, clock, email=rec.email)  # new instance each run
    assert sorted(s for _, s in rec.calls) == sorted(f"m{i}" for i in range(8))
    assert all(_row(db_path, i)[0] == "sent" for i in ids)


def test_parallel_workers_never_send_a_message_twice(db_path, clock):
    ids = [_mail(_spooler(db_path), f"m{i}") for i in range(8)]
    rec = Recorder(clock)
    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(5):
                _spooler(db_path).process_queue_sync(None, rec.email)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    subjects = [s for _, s in rec.calls]
    assert sorted(subjects) == sorted(f"m{i}" for i in range(8))
    assert all(_row(db_path, i)[0] == "sent" for i in ids)


def test_empty_queue_makes_no_network_call_and_writes_no_state(db_path, clock):
    email, signal = MagicMock(), MagicMock()
    for _ in range(3):
        assert _tick(db_path, clock, email=email, signal=signal) == (0, 0)
    email.assert_not_called()
    signal.assert_not_called()
    assert _query(db_path, "SELECT COUNT(*) FROM notification_channel_state")[0][0] == 0


# ─── Outages: retry budget and message validity ─────────────────────────────────


def _outage(db_path, clock, *, down_for: timedelta, total: timedelta, step: float = 60):
    end = clock.now + down_for
    rec = Recorder(clock, fail=lambda now: ConnectionRefusedError("down") if now < end else None)
    stop = clock.now + total
    while clock.now < stop:
        _tick(db_path, clock, email=rec.email, step=step)
    return rec


def test_network_down_for_15_minutes_is_delivered_after_it_returns(db_path, clock):
    qid = _mail(_spooler(db_path))
    rec = _outage(db_path, clock, down_for=timedelta(minutes=15), total=timedelta(minutes=40))
    status, _retries, limited, _err, _ = _row(db_path, qid)
    assert (status, limited) == ("sent", 0)  # transient failures spend no retry budget
    assert len(rec.calls) <= 8  # backoff, no tight loop


def test_outage_longer_than_the_old_retry_budget_still_delivers_within_validity(db_path, clock):
    qid = _mail(_spooler(db_path))
    rec = _outage(db_path, clock, down_for=timedelta(hours=6), total=timedelta(hours=8))
    assert _row(db_path, qid)[0] == "sent"
    assert len(rec.calls) <= 40  # about four attempts per hour once the backoff has capped


def test_message_older_than_its_validity_fails_visibly_as_expired(db_path, clock):
    qid = _mail(_spooler(db_path))
    rec = _outage(db_path, clock, down_for=timedelta(hours=48), total=timedelta(hours=26))
    status, _r, _l, error, _ = _row(db_path, qid)
    assert status == "failed"
    assert error.startswith("[EXPIRED]")
    calls_at_expiry = len(rec.calls)
    _tick(db_path, clock, email=rec.email)
    assert len(rec.calls) == calls_at_expiry  # nothing is attempted after expiry


@pytest.mark.parametrize(
    ("exc", "attempts"),
    [
        (SMTPConfigurationError("credentials incomplete"), MAX_RETRIES + 1),
        (SMTPEncryptionError("no STARTTLS offered"), MAX_RETRIES + 1),
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), MAX_RETRIES + 1),
        (smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")}), 1),
        (smtplib.SMTPDataError(554, b"message rejected"), 1),
    ],
)
def test_unrepairable_failures_are_finalized_without_endless_retries(db_path, clock, exc, attempts):
    qid = _mail(_spooler(db_path))
    rec = Recorder(clock, fail=exc)
    for _ in range(300):
        _tick(db_path, clock, email=rec.email, step=30)
    assert _row(db_path, qid)[0] == "failed"
    assert len(rec.calls) == attempts


def test_unconfirmed_send_is_retried_later_with_the_same_message_id(db_path, clock):
    qid = _mail(_spooler(db_path))
    rec = Recorder(clock, fail=smtplib.SMTPServerDisconnected("connection lost"))
    _tick(db_path, clock, email=rec.email, step=30)
    status, retries, limited, error, _ = _row(db_path, qid)
    assert (status, retries, limited) == ("pending", 1, 0)
    assert error.startswith(spooler_mod.UNCERTAIN_MARKER)
    _tick(db_path, clock, email=rec.email, step=10)  # inside the backoff: no retry storm
    assert len(rec.calls) == 1
    rec.fail = None
    clock.advance(120)
    _tick(db_path, clock, email=rec.email)
    assert _row(db_path, qid)[0] == "sent"
    assert len(set(rec.message_keys)) == 1  # at-least-once, but receivers can drop the duplicate


# ─── Pacing ─────────────────────────────────────────────────────────────────────


def test_a_backlog_of_emails_is_sent_with_the_minimum_gap(db_path, clock):
    sp = _spooler(db_path)
    for i in range(12):
        _mail(sp, f"m{i}")
    rec = Recorder(clock)
    for _ in range(6):
        _tick(db_path, clock, email=rec.email)
    assert len(rec.calls) == 12
    gaps = _gaps(rec.times)
    assert min(gaps) >= policy.CHANNEL_POLICIES["email"].min_interval_seconds
    per_run = policy.CHANNEL_POLICIES["email"].batch_limit
    assert sum(1 for g in gaps if g < 10) <= (per_run - 1) * 3  # at most one short burst per run


def test_signal_gap_holds_across_runs_and_restarts(db_path, clock):
    sp = _spooler(db_path)
    for _ in range(3):
        _sig(sp)
    rec = Recorder(clock)
    for _ in range(40):
        _tick(db_path, clock, signal=rec.signal, step=2)  # frequent ticks, new spooler each time
    assert len(rec.calls) == 3
    assert min(_gaps(rec.times)) >= policy.CHANNEL_POLICIES["signal"].min_interval_seconds


# ─── Provider cooldown ──────────────────────────────────────────────────────────


def _rate_limited(seconds: int):
    return SignalRateLimited("limited", retry_after_seconds=seconds)


def test_rate_limit_pauses_every_message_of_the_sender_until_it_expires(db_path, clock):
    sp = _spooler(db_path)
    first, second, third = (_sig(sp) for _ in range(3))
    started = clock.now
    state = {"limited": True}
    rec = Recorder(clock, fail=lambda _now: _rate_limited(3600) if state["limited"] else None)
    _tick(db_path, clock, signal=rec.signal)
    state["limited"] = False  # the provider would accept again; our cooldown must still hold
    late = None
    while clock.now < started + timedelta(seconds=3590):
        if late is None and clock.now >= started + timedelta(seconds=100):
            late = _sig(_spooler(db_path))  # arrives during the cooldown
        _tick(db_path, clock, signal=rec.signal)
    assert len(rec.calls) == 1
    # Waiting is not a failure: untouched messages keep their counters.
    assert _row(db_path, second)[1:3] == (0, 0)
    assert _row(db_path, third)[1:3] == (0, 0)
    assert _row(db_path, late)[1:3] == (0, 0)
    assert _row(db_path, first)[1:3] == (1, 0)
    assert _spooler(db_path).get_queue_stats_detailed()["cooldowns"][0]["channel"] == "signal"

    while clock.now < started + timedelta(seconds=3600 + 120):
        _tick(db_path, clock, signal=rec.signal)
    assert len(rec.calls) == 5  # all four messages went out after the cooldown, queue not stuck
    assert min(_gaps(rec.times[1:])) >= policy.CHANNEL_POLICIES["signal"].min_interval_seconds


def test_cooldown_survives_a_restart(db_path, clock):
    sp = _spooler(db_path)
    _sig(sp)
    _tick(db_path, clock, signal=Recorder(clock, fail=_rate_limited(1800)).signal, fresh=False, sp=sp)
    _sig(sp)
    rec = Recorder(clock)
    clock.advance(600)
    _tick(db_path, clock, signal=rec.signal)  # a brand-new spooler instance reads the stored cooldown
    assert rec.calls == []
    clock.advance(1300)
    _tick(db_path, clock, signal=rec.signal)
    assert len(rec.calls) >= 1


def test_signal_cooldown_neither_blocks_email_nor_another_sender(db_path, clock):
    sp = _spooler(db_path)
    _sig(sp, SENDER_A)
    _tick(db_path, clock, signal=Recorder(clock, fail=_rate_limited(3600)).signal, fresh=False, sp=sp)
    _sig(sp, SENDER_A)
    other = _sig(sp, SENDER_B)
    mail = _mail(sp)
    rec = Recorder(clock)
    clock.advance(30)
    _tick(db_path, clock, email=rec.email, signal=rec.signal)
    assert _row(db_path, mail)[0] == "sent"
    assert _row(db_path, other)[0] == "sent"
    assert [s for _, s in rec.calls if s == SENDER_A] == []


def test_smtp_throttling_answer_pauses_email_only_and_releases_the_rest_of_the_run(db_path, clock):
    sp = _spooler(db_path)
    mails = [_mail(sp, f"m{i}") for i in range(3)]
    sig = _sig(sp)
    throttle = smtplib.SMTPResponseException(452, b"4.5.3 Too many messages, rate limit exceeded")
    rec = Recorder(clock, fail=lambda _n: None)
    mail_rec = Recorder(clock, fail=throttle)
    sp.process_queue_sync(rec.signal, mail_rec.email)
    assert len(mail_rec.calls) == 1  # the other two were released unsent
    assert _row(db_path, sig)[0] == "sent"
    waiting = [_row(db_path, m) for m in mails]
    assert sorted(w[1] for w in waiting) == [0, 0, 1]  # only the attempted message counts a retry
    assert all(w[0] == "pending" and w[2] == 0 for w in waiting)
    ok = Recorder(clock)
    clock.advance(60)
    _tick(db_path, clock, email=ok.email)
    assert ok.calls == []  # SMTP cooldown still active
    clock.advance(policy.SMTP_RATE_LIMIT_COOLDOWN_SECONDS)
    _tick(db_path, clock, email=ok.email)
    assert len(ok.calls) >= 1


# ─── Alert order ────────────────────────────────────────────────────────────────


def test_resolved_never_overtakes_a_delayed_down_message(db_path, clock):
    sp = _spooler(db_path)
    _mail(sp, "down", {"alert_id": "A1", "alert_event": "open"})
    network_back = clock.now + timedelta(minutes=5)
    rec = Recorder(clock, fail=lambda now: ConnectionRefusedError("down") if now < network_back else None)
    _tick(db_path, clock, email=rec.email)  # DOWN fails and is scheduled for a later retry
    _mail(sp, "up", {"alert_id": "A1", "alert_event": "resolved"})
    _mail(sp, "other", {"alert_id": "B7", "alert_event": "resolved"})  # unrelated alert is not held back
    for _ in range(60):
        _tick(db_path, clock, email=rec.email)
    delivered = [s for t, s in rec.calls if t >= network_back]
    assert delivered.index("down") < delivered.index("up")
    assert "other" in [s for t, s in rec.calls if t < network_back]


# ─── Classification (single place) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "kind", "uncertain"),
    [
        (ConnectionRefusedError("refused"), policy.FailureKind.TRANSIENT, False),
        (OSError("network unreachable"), policy.FailureKind.TRANSIENT, False),
        (TimeoutError("timed out"), policy.FailureKind.TRANSIENT, True),
        (smtplib.SMTPServerDisconnected("gone"), policy.FailureKind.TRANSIENT, True),
        (smtplib.SMTPResponseException(451, b"4.7.1 greylisted, try later"), policy.FailureKind.TRANSIENT, False),
        (smtplib.SMTPResponseException(421, b"4.7.0 too many connections"), policy.FailureKind.RATE_LIMITED, False),
        (smtplib.SMTPResponseException(550, b"no such user"), policy.FailureKind.PERMANENT, False),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), policy.FailureKind.LIMITED, False),
        (ssl.SSLCertVerificationError(1, "bad certificate"), policy.FailureKind.LIMITED, False),
        (SMTPEncryptionError("no STARTTLS"), policy.FailureKind.LIMITED, False),
        (SignalTimeout("timeout"), policy.FailureKind.TRANSIENT, True),
        (SignalNotInstalled("missing"), policy.FailureKind.LIMITED, False),
        (SignalRecipientInvalid("bad number"), policy.FailureKind.PERMANENT, False),
        (SignalRateLimited("slow", retry_after_seconds=120), policy.FailureKind.RATE_LIMITED, False),
        (ValueError("bug"), policy.FailureKind.LIMITED, False),
    ],
)
def test_failures_are_classified_in_one_place(exc, kind, uncertain):
    failure = policy.classify_failure(exc)
    assert (failure.kind, failure.uncertain) == (kind, uncertain)


def test_provider_retry_after_is_respected_and_capped():
    assert policy.classify_failure(_rate_limited(120)).cooldown_seconds == 120
    assert policy.classify_failure(_rate_limited(10**9)).cooldown_seconds == policy.MAX_COOLDOWN_SECONDS


def test_queue_status_reports_channels_and_cooldowns(db_path, clock):
    sp = _spooler(db_path)
    _sig(sp)
    _sig(sp)
    _mail(sp)
    _tick(db_path, clock, signal=Recorder(clock, fail=_rate_limited(900)).signal, fresh=False, sp=sp)
    stats = sp.get_queue_stats_detailed()
    assert stats["channels"]["signal"]["pending"] == 2
    assert stats["channels"]["email"]["pending"] == 1
    assert stats["channels"]["signal"]["cooldown_until"] == stats["cooldowns"][0]["until"]
    assert stats["channels"]["signal"]["next_attempt_at"] == stats["cooldowns"][0]["until"]
    assert "+49" not in str(stats["cooldowns"])  # sender numbers are not exposed


def test_sql_and_python_channel_keys_agree(db_path):
    sp = _spooler(db_path)
    _sig(sp, SENDER_A)
    _sig(sp, "")
    _mail(sp)
    sql = f"SELECT {spooler_mod._CHANNEL_KEY_SQL.format(t='')} FROM notification_queue"  # noqa: S608, SLF001
    expected = [policy.channel_key("signal", SENDER_A), policy.channel_key("signal", ""), policy.channel_key("email")]
    assert [r[0] for r in _query(db_path, sql)] == expected
