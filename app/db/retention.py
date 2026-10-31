#!/usr/bin/env python3
#
# app/db/retention.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite retention and maintenance: batched pruning, WAL/size diagnostics, PRAGMA optimize.

Everything here is bounded: deletes run in small batches with one short write transaction each, so
monitoring writers interleave instead of waiting for one large DELETE. A failed task is logged and
retried by the next scheduler run; it never aborts the other tasks. Retention values are fixed
defaults (no setting, no environment variable); see docs/development/database-maintenance.md.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..utils.time import utcnow
from . import sqlite as sqlite_db

_log = logging.getLogger(__name__)

BATCH_SIZE = 1000  # rows per DELETE transaction
BATCH_PAUSE_SECONDS = 0.01  # lets waiting writers take the lock between batches
RUN_TIME_BUDGET_SECONDS = 10.0  # one run stops starting new batches after this and continues next time

NOTIFICATION_RETENTION_DAYS = 7  # sent/failed queue rows
ALERT_RETENTION_DAYS = 730  # resolved alerts only; open alerts are never pruned
LOGIN_ATTEMPT_MAX_AGE_HOURS = 24
QUALITY_RETENTION_DAYS = 30
SCHEDULER_RUN_STALE_SECONDS = 300
POP_IMPORT_RUN_RETENTION_DAYS = 90
POP_IMPORT_RUNS_KEEP_LATEST = 20
POP_CANDIDATE_REVIEWED_RETENTION_DAYS = 90
POP_CANDIDATE_UNREVIEWED_RETENTION_DAYS = 365
WAL_TRUNCATE_THRESHOLD_BYTES = 64 * 1024 * 1024  # TRUNCATE checkpoint only above this WAL size

# Tables pruned here are fixed internal names with a rowid; the WHERE fragments are constants.
_ALLOWED_TABLES = frozenset(
    {
        "auth_tokens",
        "login_attempts",
        "monitor_quality",
        "alerts",
        "notification_queue",
        "scheduler_runs",
        "target_locks",
        "pop_import_runs",
        "pop_alias_candidates",
    }
)


def delete_in_batches(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple = (),
    *,
    batch_size: int = BATCH_SIZE,
    deadline: float | None = None,
) -> tuple[int, bool]:
    """Delete rows matching ``where`` in batches; returns ``(deleted, complete)``.

    Each batch is its own short ``BEGIN IMMEDIATE`` transaction. ``complete`` is False when the
    ``deadline`` (``time.monotonic()`` value) stopped the loop before the backlog was drained.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"table {table!r} is not prunable")
    sql = f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)"  # noqa: S608 - fixed table allowlist and constant WHERE, values bound
    total = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return total, False
        with sqlite_db.transaction(conn, immediate=True):
            deleted = conn.execute(sql, (*params, batch_size)).rowcount
        total += deleted
        if deleted < batch_size:
            return total, True
        time.sleep(BATCH_PAUSE_SECONDS)


def prune_notification_queue(
    conn: sqlite3.Connection, days: int = NOTIFICATION_RETENTION_DAYS, *, deadline: float | None = None
) -> tuple[int, bool]:
    """Delete sent/failed queue entries older than ``days`` (pending/processing rows are never touched)."""
    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    return delete_in_batches(
        conn, "notification_queue", "status IN ('sent', 'failed') AND updated_at < ?", (cutoff,), deadline=deadline
    )


@dataclass
class RetentionReport:
    """Outcome of one run: rows deleted per task, failed tasks and whether every backlog is drained."""

    deleted: dict[str, int] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    complete: bool = True

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def run_retention(
    conn: sqlite3.Connection, *, now: datetime | None = None, time_budget: float = RUN_TIME_BUDGET_SECONDS
) -> RetentionReport:
    """Apply every retention rule once. Report-job rows/PDF files are owned by the PDF worker loop."""
    now = now or utcnow()
    deadline = time.monotonic() + time_budget
    ts = lambda delta: now - delta  # noqa: E731 - tiny local helper

    tasks: dict[str, Callable[[], tuple[int, bool]]] = {
        "auth_tokens": lambda: delete_in_batches(conn, "auth_tokens", "expires_at <= ?", (now,), deadline=deadline),
        "login_attempts": lambda: delete_in_batches(
            conn,
            "login_attempts",
            "(locked_until IS NULL OR locked_until < ?) AND last_attempt_at < ?",
            (now, ts(timedelta(hours=LOGIN_ATTEMPT_MAX_AGE_HOURS))),
            deadline=deadline,
        ),
        "monitor_quality": lambda: delete_in_batches(
            conn, "monitor_quality", "ts < ?", (ts(timedelta(days=QUALITY_RETENTION_DAYS)),), deadline=deadline
        ),
        "alerts": lambda: delete_in_batches(
            conn,
            "alerts",
            "status = 'resolved' AND started_at < ?",
            (ts(timedelta(days=ALERT_RETENTION_DAYS)),),
            deadline=deadline,
        ),
        "notification_queue": lambda: prune_notification_queue(conn, deadline=deadline),
        "scheduler_runs": lambda: delete_in_batches(
            conn,
            "scheduler_runs",
            "heartbeat_at < ?",
            (ts(timedelta(seconds=SCHEDULER_RUN_STALE_SECONDS)),),
            deadline=deadline,
        ),
        "target_locks": lambda: delete_in_batches(conn, "target_locks", "expires_at < ?", (now,), deadline=deadline),
        "pop_import_runs": lambda: delete_in_batches(
            conn,
            "pop_import_runs",
            "status != 'running' AND started_at < ? AND run_id NOT IN "
            "(SELECT run_id FROM pop_import_runs ORDER BY started_at DESC LIMIT ?)",
            (ts(timedelta(days=POP_IMPORT_RUN_RETENTION_DAYS)), POP_IMPORT_RUNS_KEEP_LATEST),
            deadline=deadline,
        ),
        "pop_alias_candidates": lambda: _prune_pop_candidates(conn, ts, deadline),
    }

    report = RetentionReport()
    for name, task in tasks.items():
        try:
            deleted, complete = task()
        except Exception as e:  # noqa: BLE001 - one failing task must not block the others; retried next run
            report.failed.append(name)
            report.complete = False
            _log.warning("DB retention task %s failed (will retry next run): %s", name, e)
            continue
        report.deleted[name] = deleted
        report.complete = report.complete and complete
        if deleted:
            _log.info("DB retention: removed %d row(s) from %s%s", deleted, name, "" if complete else " (more pending)")
    return report


def _prune_pop_candidates(
    conn: sqlite3.Connection, ts: Callable[[timedelta], datetime], deadline: float
) -> tuple[int, bool]:
    reviewed, done_a = delete_in_batches(
        conn,
        "pop_alias_candidates",
        "reviewed = 1 AND last_seen < ?",
        (ts(timedelta(days=POP_CANDIDATE_REVIEWED_RETENTION_DAYS)),),
        deadline=deadline,
    )
    unreviewed, done_b = delete_in_batches(
        conn,
        "pop_alias_candidates",
        "reviewed = 0 AND last_seen < ?",
        (ts(timedelta(days=POP_CANDIDATE_UNREVIEWED_RETENTION_DAYS)),),
        deadline=deadline,
    )
    return reviewed + unreviewed, done_a and done_b


def _db_file(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def collect_db_stats(conn: sqlite3.Connection, *, quick_check: bool = False) -> dict:
    """Measurable database health: file/WAL size, free pages, queue backlog, connections.

    ``quick_check`` additionally runs PRAGMA quick_check (reads the whole file; diagnostics only).
    """
    path = _db_file(conn)
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    queue = {
        str(r[0]): int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM notification_queue GROUP BY status")
    }
    stats: dict = {
        "db_bytes": _size(path) if path else page_size * page_count,
        "wal_bytes": _size(path.with_name(path.name + "-wal")) if path else 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist,
        "freelist_bytes": freelist * page_size,
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "synchronous": sqlite_db.SYNCHRONOUS_MODE,
        "schema_version": sqlite_db.get_schema_version(conn),
        "queue": queue,
        "connections": sqlite_db.get_connection_stats(),
    }
    if quick_check:
        stats["quick_check"] = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
    return stats


def checkpoint_wal(conn: sqlite3.Connection, *, truncate_threshold: int = WAL_TRUNCATE_THRESHOLD_BYTES) -> dict:
    """PASSIVE checkpoint (never blocks); TRUNCATE only when the WAL grew beyond ``truncate_threshold``.

    Returns the WAL size before/after and the checkpoint result, so growth by long-lived readers
    shows up in the log instead of silently.
    """
    path = _db_file(conn)
    wal = path.with_name(path.name + "-wal") if path else None
    before = _size(wal) if wal else 0
    busy, log_frames, checkpointed = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    mode = "PASSIVE"
    if wal is not None and _size(wal) > truncate_threshold:
        busy, log_frames, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        mode = "TRUNCATE"
    after = _size(wal) if wal else 0
    return {
        "mode": mode,
        "busy": int(busy),
        "log_frames": int(log_frames),
        "checkpointed_frames": int(checkpointed),
        "wal_bytes_before": before,
        "wal_bytes_after": after,
    }


def run_optimize(conn: sqlite3.Connection) -> None:
    """PRAGMA optimize: refreshes planner statistics only where SQLite judges them stale (cheap)."""
    conn.execute("PRAGMA optimize")
