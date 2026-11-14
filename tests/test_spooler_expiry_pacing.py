#!/usr/bin/env python3
#
# tests/test_spooler_expiry_pacing.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spooler: expiry of non-terminal rows, atomic pacing reservation, channel-key parity."""

# ruff: noqa: SLF001, S608, ARG001, ARG005
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.services.notifications import delivery_policy as policy
from app.services.notifications import spooler as spooler_mod
from app.services.notifications.spooler import NotificationSpooler
from tests import _support  # noqa: F401 - imported for its sys.path side effect

VALIDITY = policy.MESSAGE_VALIDITY_SECONDS


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
    return NotificationSpooler(lambda: sqlite_db.connect(db_path))


def _row(db_path: Path, queue_id: int) -> tuple[str, str | None]:
    conn = sqlite_db.connect(db_path)
    try:
        return conn.execute("SELECT status, last_error FROM notification_queue WHERE id = ?", (queue_id,)).fetchone()
    finally:
        sqlite_db.close_connection(conn)


def _email(sp: NotificationSpooler, address: str = "a@example.org") -> int:
    return sp.enqueue("alert", "email", 1, "R", address, {"subject": "s", "text_body": "b"}, immediate=True)


def _signal(sp: NotificationSpooler, sender: str, rid: int = 1) -> int:
    return sp.enqueue("alert", "signal", rid, "R", "+491110009999", {"message": "m", "sender": sender}, immediate=True)


def _set_status(db_path: Path, queue_id: int, status: str, age: float, lease_age: float) -> None:
    """Backdate created_at (message age) and updated_at (lease age) relative to the fake clock."""
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    conn = sqlite_db.connect(db_path)
    try:
        conn.execute(
            "UPDATE notification_queue SET status = ?, created_at = ?, updated_at = ?, next_retry_at = ? WHERE id = ?",
            (
                status,
                (now - timedelta(seconds=age)).isoformat(),
                (now - timedelta(seconds=lease_age)).isoformat(),
                (now - timedelta(seconds=age)).isoformat(),
                queue_id,
            ),
        )
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)


@pytest.mark.parametrize("status", ["pending", "processing"])
@pytest.mark.parametrize(
    ("age", "expired"),
    [(VALIDITY - 60, False), (VALIDITY + 60, True)],
)
def test_expiry_at_boundary_never_reaches_send(clock: Clock, db_path: Path, status: str, age: float, expired: bool):
    sp = _spooler(db_path)
    qid = _email(sp)
    # A processing row with a lapsed lease models crash recovery after downtime.
    _set_status(db_path, qid, status, age, lease_age=spooler_mod.PROCESSING_TIMEOUT_SECONDS + 5)
    sent: list[str] = []
    sp.process_queue_sync(None, lambda *a, **k: sent.append(a[0]) or True)
    state, error = _row(db_path, qid)
    if expired:
        assert state == "failed"
        assert error and error.startswith("[EXPIRED]")
        assert sent == []
    else:
        assert state == "sent"
        assert sent == ["a@example.org"]


def test_expiry_leaves_active_processing_lease_alone(clock: Clock, db_path: Path):
    sp = _spooler(db_path)
    qid = _email(sp)
    _set_status(db_path, qid, "processing", VALIDITY + 60, lease_age=5)
    conn = sp._db_conn_factory()
    try:
        assert sp._expire_stale(conn, clock.now) == 0
    finally:
        sqlite_db.close_connection(conn)
    assert _row(db_path, qid)[0] == "processing"


def test_expiry_checked_again_right_before_send(clock: Clock, db_path: Path):
    sp = _spooler(db_path)
    qid = _email(sp)
    sent: list[str] = []
    claimed = sp.get_pending_items()
    assert [i.id for i in claimed] == [qid]
    # The message crosses its validity between claim and send.
    clock.advance(VALIDITY + 1)
    result = sp._reserve_send(claimed[0], "email", lambda: __import__("contextlib").nullcontext())
    assert result is not None
    assert result.kind == "expired"
    assert sent == []
    state, error = _row(db_path, qid)
    assert state == "failed"
    assert error and error.startswith("[EXPIRED]")


def test_pacing_keeps_gap_across_two_workers(clock: Clock, db_path: Path):
    w1, w2 = _spooler(db_path), _spooler(db_path)
    ids = [_email(w1, "a@example.org"), _email(w1, "b@example.org")]
    items = w1.get_pending_items()
    assert sorted(i.id for i in items) == sorted(ids)
    stamps: list[datetime] = []
    barrier = threading.Barrier(2)

    def worker(sp: NotificationSpooler, item) -> None:
        barrier.wait()
        assert sp._reserve_send(item, "email", lambda: __import__("contextlib").nullcontext()) is None
        stamps.append(clock.now)

    threads = [threading.Thread(target=worker, args=(sp, it)) for sp, it in zip((w1, w2), items, strict=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = sqlite_db.connect(db_path)
    try:
        slot = conn.execute(
            "SELECT last_attempt_at FROM notification_channel_state WHERE channel_key = 'email'"
        ).fetchone()[0]
    finally:
        sqlite_db.close_connection(conn)
    # One reservation went out immediately, the other was pushed back by the minimum interval.
    interval = policy.CHANNEL_POLICIES["email"].min_interval_seconds
    assert sum(clock.sleeps) == pytest.approx(interval)
    assert datetime.fromisoformat(slot) == datetime(2030, 1, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=interval)


def test_pacing_signal_senders_are_independent(clock: Clock, db_path: Path):
    sp = _spooler(db_path)
    _signal(sp, "+491110000001", rid=1)
    _signal(sp, "+491110000002", rid=2)
    items = sp.get_pending_items() + sp.get_pending_items()  # Signal batch limit is 1 per call
    assert len(items) == 2
    for item in items:
        assert sp._reserve_send(item, spooler_mod._item_key(item), spooler_mod.nullcontext) is None
    assert clock.sleeps == []


# ─── channel key parity: Python rule vs SQL expression ───────────────────────────

PARITY_PAYLOADS = [
    ("signal", '{"sender": ""}'),
    ("signal", '{"sender": "  +491110000001  "}'),
    ("signal", '{"sender": "+491110000001"}'),
    ("signal", '{"sender": "+491110000002"}'),
    ("signal", '{"message": "no sender field"}'),
    ("signal", "not json at all"),
    ("email", '{"sender": "+491110000001"}'),
    ("email", "{}"),
]


@pytest.mark.parametrize(("channel", "payload"), PARITY_PAYLOADS)
def test_channel_key_sql_matches_python(channel: str, payload: str):
    import json

    conn = sqlite3.connect(":memory:")
    sql = f"SELECT {spooler_mod._CHANNEL_KEY_SQL.format(t='')} FROM (SELECT ? AS channel_type, ? AS payload)"
    sql_key = conn.execute(sql, (channel, payload)).fetchone()[0]
    try:
        sender = json.loads(payload).get("sender")
    except ValueError:
        sender = None
    assert sql_key == policy.channel_key(channel, sender)
