#!/usr/bin/env python3
#
# tests/test_spooler_recovery.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spooler crash recovery and corrupt-row handling; SignalRateLimited construction."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.services.notifications.signal_errors import SignalError, SignalRateLimited
from app.services.notifications.spooler import NotificationSpooler, QueueStatus
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def spooler(tmp_path: Path):
    db_path = tmp_path / "spool.db"
    boot = sqlite_db.connect(db_path)
    sqlite_db.init_schema(boot)
    sqlite_db.close_connection(boot)
    return NotificationSpooler(lambda: sqlite_db.connect(db_path)), db_path


def _row(db_path: Path, queue_id: int):
    conn = sqlite_db.connect(db_path)
    try:
        return conn.execute(
            "SELECT status, retry_count, last_error FROM notification_queue WHERE id = ?", (queue_id,)
        ).fetchone()
    finally:
        sqlite_db.close_connection(conn)


def _enqueue(spooler_obj, **kw):
    return spooler_obj.enqueue(
        "alert", "email", 1, "R", "r@example.com", kw.pop("payload", {"subject": "s", "body": "b"}), **kw
    )


def _set(db_path: Path, queue_id: int, **cols):
    conn = sqlite_db.connect(db_path)
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE notification_queue SET {sets} WHERE id = ?", [*cols.values(), queue_id])  # noqa: S608
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)


def test_stuck_processing_item_is_recovered_and_keeps_short_error(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp, error="smtp down")
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _set(db_path, qid, status=QueueStatus.PROCESSING.value, updated_at=old, last_error="smtp down")
    items = sp.get_pending_items()
    assert [i.id for i in items] == [qid]
    status, retries, last_error = _row(db_path, qid)
    assert status == QueueStatus.PROCESSING.value
    assert retries == 1
    assert last_error == "smtp down [RECOVERED]"


def test_stuck_item_with_long_error_is_truncated(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp)
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _set(db_path, qid, status=QueueStatus.PROCESSING.value, updated_at=old, last_error="x" * 2000)
    sp.get_pending_items()
    _, _, last_error = _row(db_path, qid)
    assert last_error.startswith("[...] ")
    assert last_error.endswith("[RECOVERED]")
    assert len(last_error) < 500


def test_corrupt_item_fails_permanently_instead_of_hot_looping(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp)
    _set(db_path, qid, payload="{not json", next_retry_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    assert sp.get_pending_items() == []
    status, _, last_error = _row(db_path, qid)
    assert status == QueueStatus.FAILED.value
    assert last_error.startswith("Parse error")
    assert sp.get_pending_items() == []


def test_rate_limited_accepts_retry_after():
    assert SignalRateLimited("x").retry_after_seconds == 3600
    exc = SignalRateLimited("slow down", retry_after_seconds=120)
    assert exc.retry_after_seconds == 120
    assert str(exc) == "slow down"
    assert isinstance(exc, SignalError)


def test_down_and_up_alert_events_are_distinct_but_each_is_idempotent(spooler):
    sp, _ = spooler

    def alert(event: str) -> int:
        return _enqueue(sp, payload={"alert_id": "a-1", "alert_event": event, "subject": "s", "body": "b"})

    down, up = alert("open"), alert("resolved")
    assert down != up
    assert alert("open") == down
    assert alert("resolved") == up


def test_late_retry_never_resurrects_a_sent_job(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp)
    sp.mark_sent(qid)
    # A late failure handler must not move a terminally-SENT job back into the retry queue.
    assert sp.mark_retry(qid, "late failure") is False
    status, retries, _ = _row(db_path, qid)
    assert status == QueueStatus.SENT.value
    assert retries == 0  # the increment is also rejected for a terminal job


def test_late_retry_never_resurrects_a_failed_job(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp)
    sp.mark_failed_permanent(qid, "invalid recipient")
    assert sp.mark_retry(qid, "late failure") is False
    status, retries, _ = _row(db_path, qid)
    assert status == QueueStatus.FAILED.value
    assert retries == 0


def test_retry_is_rejected_for_unknown_job(spooler):
    sp, _ = spooler
    assert sp.mark_retry(999999, "no such job") is False


def test_concurrent_completion_and_late_retry_keep_the_job_sent(spooler):
    sp, db_path = spooler
    qid = _enqueue(sp)
    sp.mark_processing(qid)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def complete():
        barrier.wait(5)
        sp.mark_sent(qid)

    def late_retry():
        barrier.wait(5)
        results["retry"] = sp.mark_retry(qid, "racing failure")

    threads = [threading.Thread(target=complete), threading.Thread(target=late_retry)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    # Whichever ordering the serialized immediate transactions take, a SENT job is terminal:
    # it is never left PENDING, so it is never re-delivered.
    status, _, _ = _row(db_path, qid)
    assert status in (QueueStatus.SENT.value, QueueStatus.PROCESSING.value)
    assert status != QueueStatus.PENDING.value
