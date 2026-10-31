#!/usr/bin/env python3
#
# app/db/sqlite.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite database access layer and schema initialization."""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from ..models.users import hash_password, verify_password
from ..utils import bootstrap_credentials
from ..utils.time import ensure_utc, utcnow

_log = logging.getLogger(__name__)


def _adapt_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Naive datetime not allowed in SQLite")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _convert_datetime(value: bytes) -> datetime:
    s = value.decode("utf-8")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        _log.warning(
            "Corrupt timestamp in database: %r — returning epoch-0. This may cause incorrect calculations.",
            value.decode("utf-8", errors="replace"),
        )
        return datetime.fromtimestamp(0, tz=UTC)


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("timestamp", _convert_datetime)


# ---------------------------------------------------------------------------
# Connection Registry (for graceful shutdown)
# ---------------------------------------------------------------------------

_OPEN_CONNECTIONS: set[sqlite3.Connection] = set()
_CONNECTIONS_LOCK = threading.Lock()
_CONNECTIONS_OPENED = 0  # Total connections opened (cumulative)
_CONNECTIONS_CLOSED = 0  # Total connections closed (cumulative)
_PENDING_CONNECTS = 0  # connect() calls admitted but not yet registered (counted by begin_quiesce)


# Per-connection PRAGMAs. WAL is a persistent database-level mode and is enabled once in
# ensure_wal_mode() during initialization, not on every connection.
BUSY_TIMEOUT_MS = 5000
# FULL (the SQLite default) keeps every committed transaction durable across power loss; NORMAL
# would be faster in WAL mode but can lose the last commits on a system crash. Changing it needs
# a deliberate durability decision plus a measurement.
SYNCHRONOUS_MODE = "FULL"

_QUIESCED = False


class DatabaseQuiescedError(RuntimeError):
    """New database connections are refused while a restore or other maintenance quiesces the database."""


def connect(db_path: Path) -> sqlite3.Connection:
    """
    Create a SQLite connection configured for this application.

    Thread Safety:
        Connections use check_same_thread=False but are designed to be
        short-lived and not shared across concurrent writers. Each request
        should use its own connection or use a connection pool.

    Configuration (per connection):
        - busy_timeout (BUSY_TIMEOUT_MS), foreign keys enforced, synchronous (SYNCHRONOUS_MODE)
        - Automatic datetime conversion (UTC-only)
        The journal mode (WAL) is database-level and set by ensure_wal_mode() at initialization.

    Raises:
        DatabaseQuiescedError: While begin_quiesce() is active.
    """
    global _CONNECTIONS_OPENED, _PENDING_CONNECTS
    # Admission is atomic with the quiesce flag: a connect either is refused here or is counted as
    # pending, and begin_quiesce() waits for pending connects, so none can slip past it unregistered.
    with _CONNECTIONS_LOCK:
        if _QUIESCED:
            raise DatabaseQuiescedError("Database is temporarily unavailable (maintenance in progress)")
        _PENDING_CONNECTS += 1
    conn: sqlite3.Connection | None = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(db_path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA synchronous={SYNCHRONOUS_MODE}")
    except BaseException:
        if conn is not None:
            conn.close()  # never leak a handle when a PRAGMA fails
        with _CONNECTIONS_LOCK:
            _PENDING_CONNECTS -= 1
        raise

    # Track connection for graceful shutdown; registration and pending release are one atomic step
    with _CONNECTIONS_LOCK:
        _OPEN_CONNECTIONS.add(conn)
        _CONNECTIONS_OPENED += 1
        _PENDING_CONNECTS -= 1

    return conn


def ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Enable WAL (persistent) and verify it; must run outside a transaction."""
    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current).lower() in {"wal", "memory"}:
        return
    if conn.in_transaction:
        raise RuntimeError("journal_mode cannot be changed inside a transaction")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise sqlite3.OperationalError(f"Could not enable WAL journal mode (database reports {mode!r})")


def _prune_closed_connections() -> None:
    """Forget tracked handles that were closed directly; caller holds _CONNECTIONS_LOCK."""
    global _CONNECTIONS_CLOSED
    for conn in list(_OPEN_CONNECTIONS):
        try:
            conn.total_changes  # noqa: B018 - raises ProgrammingError on a closed connection
        except sqlite3.ProgrammingError:
            _OPEN_CONNECTIONS.discard(conn)
            _CONNECTIONS_CLOSED += 1  # closed directly by its owner, still a real close


def begin_quiesce(*, keep: Iterable[sqlite3.Connection] = (), timeout: float = 30.0) -> bool:
    """Refuse new connections and wait until all other tracked connections are closed.

    ``keep`` are connections of the caller that may stay open. Returns False (and lifts the
    block) when others are still open after ``timeout`` seconds, and False without touching the
    state when another quiesce is already active. Never closes foreign connections.
    """
    global _QUIESCED
    keep_set = set(keep)
    with _CONNECTIONS_LOCK:
        if _QUIESCED:
            return False
        _QUIESCED = True
    deadline = time.monotonic() + timeout
    while True:
        with _CONNECTIONS_LOCK:
            _prune_closed_connections()
            if _PENDING_CONNECTS == 0 and not (_OPEN_CONNECTIONS - keep_set):
                return True
        if time.monotonic() >= deadline:
            end_quiesce()
            return False
        time.sleep(0.05)


def end_quiesce() -> None:
    """Allow new connections again."""
    global _QUIESCED
    with _CONNECTIONS_LOCK:
        _QUIESCED = False


def _is_closed(conn: sqlite3.Connection) -> bool:
    """True only when the handle provably refuses use (ProgrammingError on a closed connection)."""
    try:
        conn.total_changes  # noqa: B018 - raises ProgrammingError on a closed connection
    except sqlite3.ProgrammingError:
        return True
    except Exception:  # noqa: BLE001 - any other state is not proof of closure
        return False
    return False


def _deregister_if_closed(conn: sqlite3.Connection) -> bool:
    """Drop ``conn`` from the registry only if it is provably closed; True when this call removed it.

    A handle that is still usable stays registered, so begin_quiesce() keeps waiting (and finally
    fails closed on timeout) instead of treating a possibly open connection as gone.
    """
    global _CONNECTIONS_CLOSED
    with _CONNECTIONS_LOCK:
        if not _is_closed(conn):
            return False
        if conn in _OPEN_CONNECTIONS:
            _OPEN_CONNECTIONS.discard(conn)
            _CONNECTIONS_CLOSED += 1
            return True
        return False


def close_connection(conn: sqlite3.Connection) -> bool:
    """Close a connection and deregister it only once it is provably closed.

    The handle stays registered for the whole duration of ``conn.close()``. If closing fails
    (``sqlite3.Error`` is logged as a warning, any other exception propagates) the handle remains
    registered, so a concurrent ``begin_quiesce`` cannot mistake it for closed. The closed-counter
    only counts real closes.

    Returns:
        True if the connection is closed afterwards, False if it may still be open.
    """
    try:
        conn.close()
    except sqlite3.Error as e:
        _log.warning("Failed to close SQLite connection; it stays registered: %s", e)
    finally:
        _deregister_if_closed(conn)
    with _CONNECTIONS_LOCK:
        return _is_closed(conn)


def close_all_connections(*, settle_timeout: float = 2.0) -> int:
    """
    Close all tracked connections for graceful shutdown.

    Each handle is closed individually and deregistered only if it is provably closed; failures
    are logged and leave the handle registered. Afterwards the registry is re-checked: handles
    registered meanwhile and connect() calls still pending are swept as well, for at most
    ``settle_timeout`` seconds (no global state is changed, so it is safe to call repeatedly).

    Returns:
            Number of connections this call verifiably closed and deregistered.
    """
    deadline = time.monotonic() + settle_timeout
    closed = 0
    failed: set[sqlite3.Connection] = set()
    while True:
        with _CONNECTIONS_LOCK:
            snapshot = list(_OPEN_CONNECTIONS)
        for conn in snapshot:
            try:
                conn.close()
            except sqlite3.Error as e:
                _log.warning("Failed to close SQLite connection during shutdown: %s", e)
            except Exception:  # noqa: BLE001 - one broken handle must not stop the sweep
                _log.exception("Unexpected error closing SQLite connection during shutdown")
            if _deregister_if_closed(conn):
                closed += 1
            else:
                with _CONNECTIONS_LOCK:
                    if not _is_closed(conn):
                        failed.add(conn)
        seen = set(snapshot)
        with _CONNECTIONS_LOCK:
            _prune_closed_connections()
            late = any(c not in seen for c in _OPEN_CONNECTIONS)
            pending = _PENDING_CONNECTS
        if not late and pending == 0:
            break
        if time.monotonic() >= deadline:
            break
        if not late:
            time.sleep(0.005)  # a connect() is still in flight; let it register

    with _CONNECTIONS_LOCK:
        remaining = len(_OPEN_CONNECTIONS)
        pending = _PENDING_CONNECTS
    if failed or remaining or pending:
        _log.warning(
            "SQLite shutdown sweep: closed=%d, close_failures=%d, still_registered=%d, pending_connects=%d",
            closed,
            len(failed),
            remaining,
            pending,
        )
    return closed


def get_connection_stats() -> dict[str, int]:
    """
    Get connection statistics for monitoring/debugging.

    Returns:
            Dict with 'currently_open', 'pending_connects', 'total_opened', 'total_closed'.
    """
    with _CONNECTIONS_LOCK:
        return {
            "currently_open": len(_OPEN_CONNECTIONS),
            "pending_connects": _PENDING_CONNECTS,
            "total_opened": _CONNECTIONS_OPENED,
            "total_closed": _CONNECTIONS_CLOSED,
        }


class _TxState:
    """Bookkeeping of one outermost managed transaction (or savepoint scope) on a connection."""

    __slots__ = ("after_commit", "foreign", "savepoints")

    def __init__(self, *, foreign: bool = False) -> None:
        self.savepoints = 0
        self.after_commit: list[Callable[[], None]] = []
        # True when the real transaction was opened by someone else; its commit is not observable.
        self.foreign = foreign


# id(conn) -> state, present only while an outermost transaction() is active on that connection.
_TX_STATES: dict[int, _TxState] = {}
_TX_LOCK = threading.Lock()


def run_after_commit(conn: sqlite3.Connection, callback: Callable[[], None]) -> None:
    """Run ``callback`` once the current managed transaction committed (immediately if none is active).

    Callbacks registered inside a rolled-back (nested) transaction are discarded. Inside a
    transaction that was opened by someone else (see ``transaction``) the commit cannot be
    observed, so the callback is NOT run: firing it early could let a reader re-cache pre-commit
    data. It is dropped with a warning; callers needing it must invoke it after their own commit.
    """
    with _TX_LOCK:
        state = _TX_STATES.get(id(conn))
    if state is None:
        callback()
    else:
        state.after_commit.append(callback)


@contextmanager
def _savepoint(conn: sqlite3.Connection, state: _TxState):
    state.savepoints += 1
    name = f"justup_sp_{state.savepoints}"
    mark = len(state.after_commit)
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        del state.after_commit[mark:]
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False):
    """Commit on success, roll back on error; nesting uses savepoints.

    The outermost call owns the real transaction (``BEGIN`` or, with ``immediate``,
    ``BEGIN IMMEDIATE`` to take the write lock up front) and commits. Inner calls on the same
    connection only open a savepoint, so an inner failure rolls back just the inner work and an
    inner success never commits the outer transaction. Callbacks from run_after_commit() fire
    only after the outermost commit succeeded.

    A transaction already open on the connection that this function did not start (implicit
    sqlite3 transaction before DML, manual ``BEGIN``) belongs to its owner: this block only
    opens a savepoint inside it and never commits or rolls back the owner's work. Success
    releases the savepoint, an error rolls back to it and re-raises, leaving the owner's
    transaction open. Because that commit is not observable, run_after_commit() callbacks
    registered in such a block are dropped with a warning (see run_after_commit).
    ``immediate=True`` is refused there with OperationalError.
    """
    with _TX_LOCK:
        state = _TX_STATES.get(id(conn))
    if state is not None:
        with _savepoint(conn, state):
            yield
        return

    if conn.in_transaction:
        if immediate:
            raise sqlite3.OperationalError("cannot start an immediate transaction within a transaction")
        state = _TxState(foreign=True)
        with _TX_LOCK:
            _TX_STATES[id(conn)] = state
        try:
            with _savepoint(conn, state):
                yield
        finally:
            with _TX_LOCK:
                _TX_STATES.pop(id(conn), None)
        if state.after_commit:
            _log.warning(
                "Dropped %d post-commit callback(s): transaction() ran inside a foreign transaction "
                "whose commit cannot be observed",
                len(state.after_commit),
            )
        return

    # Explicit BEGIN so inner savepoints can never become the outermost (committing) scope.
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    state = _TxState()
    with _TX_LOCK:
        _TX_STATES[id(conn)] = state
    committed = False
    try:
        yield
        conn.commit()
        committed = True
    except BaseException:
        conn.rollback()
        raise
    finally:
        with _TX_LOCK:
            _TX_STATES.pop(id(conn), None)
    if committed:
        for callback in state.after_commit:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a failing post-commit hook must not undo or mask the commit
                _log.exception("Post-commit callback failed")


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """Serialize a read-then-write operation across SQLite connections."""
    with transaction(conn, immediate=True):
        yield


_NOTIFICATION_QUEUE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_type TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        recipient_id INTEGER,
        recipient_name TEXT NOT NULL,
        recipient_address TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        next_retry_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        target_id INTEGER
    )
    """,
    # Due-item selection (pending, next_retry_at) and crash recovery / retention (status, updated_at).
    "CREATE INDEX IF NOT EXISTS idx_notification_queue_status_retry ON notification_queue (status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_notification_queue_status_updated ON notification_queue (status, updated_at)",
)


def ensure_notification_queue_table(conn: sqlite3.Connection) -> None:
    """Create the notification queue table and its indexes (single definition, idempotent).

    Used by init_schema() and by the notification spooler, so both always agree on the shape.
    """
    for statement in _NOTIFICATION_QUEUE_DDL:
        conn.execute(statement)


_NOTIFICATIONS_DDL = (
    # Central inbox. Separate from `alerts` (monitoring state) and `notification_queue` (SMTP/Signal
    # delivery); one row per business event, deduplicated by the unique idempotency_key.
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        category TEXT NOT NULL
            CHECK (category IN ('monitoring', 'reports', 'delivery', 'security', 'activity')),
        severity TEXT NOT NULL CHECK (severity IN ('info', 'success', 'warning', 'error')),
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        created_at timestamp NOT NULL,
        resource_type TEXT
            CHECK (resource_type IS NULL OR resource_type IN ('target', 'alert', 'report_job', 'queue')),
        resource_id TEXT,
        target_id INTEGER,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor_name TEXT,
        audience TEXT NOT NULL CHECK (audience IN ('admins', 'user', 'target')),
        recipient_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        event_count INTEGER NOT NULL DEFAULT 1 CHECK (event_count >= 1),
        idempotency_key TEXT NOT NULL UNIQUE,
        CHECK (audience <> 'user' OR recipient_user_id IS NOT NULL),
        CHECK (audience <> 'target' OR target_id IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications (recipient_user_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_category ON notifications (category, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_audience ON notifications (audience, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at)",
    # Per-user read state, normalized: a missing row means unread.
    """
    CREATE TABLE IF NOT EXISTS notification_reads (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        read_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_reads_user ON notification_reads (user_id, notification_id)",
    # Per-user dismissal ("delete" in the UI): hides one notification for one user without touching others.
    """
    CREATE TABLE IF NOT EXISTS notification_dismissals (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        dismissed_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_dismissals_user ON notification_dismissals (user_id, notification_id)",
)


def ensure_notifications_tables(conn: sqlite3.Connection) -> None:
    """Create the notification inbox tables and indexes (idempotent)."""
    for statement in _NOTIFICATIONS_DDL:
        conn.execute(statement)


def ensure_report_jobs_user_id(conn: sqlite3.Connection) -> None:
    """Add report_jobs.user_id to a database created before report ownership existed (idempotent)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(report_jobs)")}
    if columns and "user_id" not in columns:
        conn.execute("ALTER TABLE report_jobs ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the current schema (idempotent), enable WAL and apply pending migrations."""
    ensure_wal_mode(conn)  # journal_mode cannot change inside the transaction below
    with transaction(conn, immediate=True):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                can_close_alerts INTEGER NOT NULL DEFAULT 0 CHECK (can_close_alerts IN (0, 1)),
                must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
                theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
                last_login_at timestamp,
                last_login_ip TEXT,
                created_at timestamp NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at timestamp NOT NULL,
                max_expires_at timestamp NOT NULL,
                created_at timestamp NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        # Index for token expiry cleanup and user cascade
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires_at ON auth_tokens(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id ON auth_tokens(user_id)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at timestamp NOT NULL
            )
            """
        )

        conn.execute(
            """
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    group_name TEXT,
                    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
                interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
                retention_days INTEGER NOT NULL CHECK (retention_days > 0),
                enable_ping INTEGER NOT NULL CHECK (enable_ping IN (0, 1)),
                enable_http_check INTEGER NOT NULL CHECK (enable_http_check IN (0, 1)),
                enable_cert_expiration INTEGER NOT NULL CHECK (enable_cert_expiration IN (0, 1)),
                enable_tcp_connect INTEGER NOT NULL DEFAULT 0 CHECK (enable_tcp_connect IN (0, 1)),
                host TEXT NOT NULL DEFAULT '',
                ping_host TEXT,
                http_url TEXT,
                http_username TEXT,
                http_password TEXT,
                http_prefer_head INTEGER NOT NULL DEFAULT 1 CHECK (http_prefer_head IN (0, 1)),
                http_basic_auth_enabled INTEGER NOT NULL DEFAULT 0 CHECK (http_basic_auth_enabled IN (0, 1)),
                http_success_codes TEXT DEFAULT '200-399',
                http_port INTEGER,
                cert_host TEXT,
                    cert_port INTEGER,
                    ignore_cert_errors INTEGER NOT NULL DEFAULT 0 CHECK (ignore_cert_errors IN (0, 1)),
                    tcp_host TEXT,
                    tcp_ports TEXT DEFAULT '80',
                    max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
                    ping_timeout_minutes INTEGER NOT NULL DEFAULT 5 CHECK (ping_timeout_minutes > 0),
                    auto_close_alert INTEGER NOT NULL DEFAULT 1 CHECK (auto_close_alert IN (0, 1)),
                    sla_enabled INTEGER NOT NULL DEFAULT 0 CHECK (sla_enabled IN (0, 1)),
                                    sla_availability_pct REAL,
                sla_response_time_ms REAL,
                sla_ping_latency_ms REAL,
                identity_signature TEXT,
                created_at timestamp NOT NULL,
                updated_at timestamp NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_group_name ON targets(group_name, name)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at timestamp NOT NULL,
                updated_at timestamp NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL UNIQUE,
                failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
                last_attempt_at timestamp NOT NULL,
                locked_until timestamp,
                created_at timestamp NOT NULL
            )
            """
        )

        # Current status cache - updated by scheduler after each check
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_status (
                target_id INTEGER PRIMARY KEY,
                ping_up INTEGER,
                ping_latency_ms REAL,
                ping_checked_at timestamp,
                http_up INTEGER,
                http_status INTEGER,
                http_latency_ms REAL,
                http_checked_at timestamp,
                tcp_up INTEGER,
                tcp_latency_ms REAL,
                tcp_checked_at timestamp,
                tcp_port_status TEXT,
                cert_days_left INTEGER,
                cert_checked_at timestamp,
                uptime_percent REAL,
                uptime_percent_30d REAL,
                overall_status TEXT NOT NULL DEFAULT 'unknown',
                last_down_at timestamp,
                updated_at timestamp NOT NULL,
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
            )
            """
        )

        # Scheduler run tracking for idempotency and crash recovery
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            )
            """
        )

        # Target check locks to prevent duplicate checks across restarts
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_locks (
                target_id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                locked_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_locks_expires ON target_locks(expires_at)")

        # Many-to-many: targets <-> users (who gets alerted)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_alert_users (
                target_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (target_id, user_id),
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_alert_users_user ON target_alert_users(user_id)")

        # Alert recipients table (phone contacts for Signal notifications + email)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
                created_at timestamp NOT NULL,
                updated_at timestamp NOT NULL,
                CHECK (phone IS NOT NULL OR email IS NOT NULL)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_phone ON recipients(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_enabled ON recipients(is_enabled)")

        # Many-to-many: targets <-> recipients (which phone numbers get alerted)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_recipients (
                target_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                PRIMARY KEY (target_id, recipient_id),
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
                FOREIGN KEY(recipient_id) REFERENCES recipients(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_recipients_recipient ON target_recipients(recipient_id)")

        # Downtime alerts table - tracks all downtime events with UUID
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                target_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                failed_service TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
                started_at timestamp NOT NULL,
                ended_at timestamp,
                duration_seconds INTEGER,
                recipients_notified TEXT,
                is_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (is_acknowledged IN (0, 1)),
                acknowledged_by TEXT,
                acknowledged_at timestamp,
                created_at timestamp NOT NULL,
                updated_at timestamp NOT NULL,
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
            )
            """
        )
        # Composite (equality column, started_at): serves target/status filters AND the ORDER BY
        # started_at DESC without a sort; the plain target_id/status indexes are their prefixes.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_target_started ON alerts(target_id, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status_started ON alerts(status, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_started_at ON alerts(started_at)")

        # Monitor quality time series (Quality Gate feature)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_quality (
                ts timestamp PRIMARY KEY,
                score REAL,
                median_latency_ms REAL,
                success_ratio REAL NOT NULL,
                state TEXT NOT NULL
            )
            """
        )

        # Monitor quality latest snapshot (for fast gating decisions)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_quality_latest (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                score REAL,
                state TEXT NOT NULL,
                updated_at timestamp NOT NULL
            )
            """
        )

        # Report jobs for async PDF generation
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_jobs (
                job_id TEXT PRIMARY KEY,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                progress INTEGER DEFAULT 0,
                filename TEXT,
                error TEXT,
                created_at timestamp NOT NULL,
                completed_at timestamp,
                expires_at timestamp,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_expires ON report_jobs(expires_at)")
        ensure_report_jobs_user_id(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_user ON report_jobs(user_id)")

        # POP locations for hostname-based geolocation (Single Source of Truth)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pop_locations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_code  TEXT NOT NULL CHECK(length(canonical_code) <= 8),
                alias_code      TEXT NOT NULL CHECK(length(alias_code) <= 8),
                city            TEXT NOT NULL CHECK(length(city) <= 64),
                country         TEXT NOT NULL CHECK(length(country) <= 5),
                lat             REAL NOT NULL CHECK(lat BETWEEN -90 AND 90),
                lon             REAL NOT NULL CHECK(lon BETWEEN -180 AND 180),
                source          TEXT NOT NULL,
                confidence      INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
                priority        INTEGER NOT NULL CHECK(priority >= 0),
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                last_seen_at    TEXT,
                import_run_id   TEXT,
                retired         INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0, 1)),
                UNIQUE(alias_code, canonical_code)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_alias_prio ON pop_locations(alias_code, priority, confidence)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_canonical ON pop_locations(canonical_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_retired ON pop_locations(retired)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_import_run ON pop_locations(import_run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_source_retired ON pop_locations(source, retired)")

        # Candidates for learning new POP aliases (review queue)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pop_alias_candidates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_code      TEXT NOT NULL UNIQUE,
                canonical_guess TEXT,
                city_guess      TEXT,
                country_guess   TEXT,
                seen_count      INTEGER NOT NULL DEFAULT 1 CHECK(seen_count >= 0),
                last_seen       TEXT NOT NULL,
                reviewed        INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0, 1)),
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_candidates_count ON pop_alias_candidates(seen_count)")

        # POP import run tracking (delta-based refresh)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pop_import_runs (
                run_id          TEXT PRIMARY KEY,
                source          TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                status          TEXT NOT NULL DEFAULT 'running',
                records_processed INTEGER DEFAULT 0,
                records_inserted INTEGER DEFAULT 0,
                records_updated INTEGER DEFAULT 0,
                records_retired INTEGER DEFAULT 0,
                error_message   TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_runs_source ON pop_import_runs(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_runs_status ON pop_import_runs(status)")

        ensure_notification_queue_table(conn)
        ensure_notifications_tables(conn)

        # Per-user dashboard layout: one row per placed widget. The CHECKs mirror the API validation
        # (x 0..11, w 1..12, x+w<=12, h 1..100, y+h<=1000 = DASHBOARD_MAX_Y in dashboard_layouts.py).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_layouts (
                user_id INTEGER NOT NULL,
                widget_id TEXT NOT NULL CHECK (length(widget_id) BETWEEN 1 AND 64),
                position INTEGER NOT NULL CHECK (position >= 0),
                x INTEGER NOT NULL CHECK (x BETWEEN 0 AND 11),
                y INTEGER NOT NULL CHECK (y >= 0),
                w INTEGER NOT NULL CHECK (w BETWEEN 1 AND 12),
                h INTEGER NOT NULL CHECK (h BETWEEN 1 AND 100),
                visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
                layout_version INTEGER NOT NULL DEFAULT 1 CHECK (layout_version = 1),
                PRIMARY KEY (user_id, widget_id),
                CHECK (x + w <= 12),
                CHECK (y + h <= 1000),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            ) WITHOUT ROWID
            """
        )

        apply_migrations(conn)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_identity_signature ON targets(identity_signature)")
        # Retired switches: drop stored values on every start (idempotent; no schema bump keeps rollback possible).
        conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in _RETIRED_SETTING_KEYS])
    # After index/schema changes: refresh planner statistics where stale (cheap, outside the transaction).
    conn.execute("PRAGMA optimize")


# ---------------------------------------------------------------------------
# Schema versioning (PRAGMA user_version)
# ---------------------------------------------------------------------------

# Version 1 is the schema built by init_schema() before report ownership and the notification inbox (v2).
# A future change appends one Migration with the next number and bumps SCHEMA_VERSION;
# init_schema() keeps creating the full current schema for
# fresh databases, so migrations only ever transform an existing older database.
# Migrations must be non-destructive (no silent deletes or merges) and run inside init_schema's
# transaction: any error rolls the schema AND user_version back. Take a backup before releasing one.
SCHEMA_VERSION = 5


class Migration(NamedTuple):
    """One forward-only schema step that upgrades a database from ``version - 1`` to ``version``."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Report ownership column and the notification inbox; both steps are additive and idempotent."""
    ensure_report_jobs_user_id(conn)
    ensure_notifications_tables(conn)


# Obsolete user settings removed once POP refresh became self-managing. Deleting these stored rows
# is the only "destructive" step, and it is safe: the keys no longer back any model field or
# runtime accessor, so a leftover value could only mislead an operator reading the settings table.
_OBSOLETE_SETTING_KEYS = ("pop_refresh_interval_seconds", "pop_refresh_initial_delay_seconds")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Drop the removed POP refresh settings so an upgraded install keeps no stale, ignored values."""
    conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in _OBSOLETE_SETTING_KEYS])


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Backfill complete check identities without deleting pre-existing duplicate targets."""
    from ..services.target_identity import identity_signature

    if "identity_signature" not in {row[1] for row in conn.execute("PRAGMA table_info(targets)")}:
        conn.execute("ALTER TABLE targets ADD COLUMN identity_signature TEXT")
    seen: dict[str, int] = {}
    for row in list(conn.execute("SELECT * FROM targets ORDER BY id")):
        signature = identity_signature(dict(row))
        if signature is not None:
            if previous_id := seen.get(signature):
                raise RuntimeError(
                    f"Cannot migrate targets: IDs {previous_id} and {row['id']} have identical monitoring identities"
                )
            seen[signature] = row["id"]
        conn.execute("UPDATE targets SET identity_signature = ? WHERE id = ?", (signature, row["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_identity_signature ON targets(identity_signature)")


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """Add the per-user notification dismissals table; additive and idempotent."""
    ensure_notifications_tables(conn)


# Settings whose user switch was removed. Their rows are ignored by the application anyway; dropping them
# keeps the settings table honest. ``downsample_enabled``: rollups now always run.
_RETIRED_SETTING_KEYS = ("downsample_enabled",)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(2, "report_jobs.user_id (report ownership) and the notification inbox tables", _migrate_to_v2),
    Migration(3, "remove obsolete POP refresh interval/initial-delay settings (now self-managed)", _migrate_to_v3),
    Migration(4, "backfill full identities; refuse pre-existing duplicates", _migrate_to_v4),
    Migration(5, "per-user notification dismissals (notification_dismissals)", _migrate_to_v5),
)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the stored PRAGMA user_version (0 = never stamped)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``user_version`` up to SCHEMA_VERSION; call inside the init_schema transaction.

    A database without a stamp (0) already received the current schema from init_schema and is
    stamped directly. A newer database than this code knows is refused instead of being touched.
    """
    current = get_schema_version(conn)
    target = max([SCHEMA_VERSION, *(m.version for m in MIGRATIONS)])
    if current > target:
        raise RuntimeError(
            f"Database schema version {current} is newer than this application supports ({target}); "
            "refusing to start. Use a matching or newer release."
        )
    if current == 0:
        # Fresh database: init_schema just built the complete current schema, nothing to migrate.
        conn.execute(f"PRAGMA user_version = {target}")
        return target
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= current:
            continue
        _log.info("Applying schema migration %d: %s", migration.version, migration.description)
        migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        current = migration.version
    return current


def set_setting(conn: sqlite3.Connection, key: str, value: Any, *, auto_commit: bool = True) -> None:
    """Persist a setting value and invalidate the in-process settings cache.

    Args:
            conn: Database connection
            key: Setting key
            value: Setting value (will be JSON-serialized)
            auto_commit: If True, wraps in transaction() (default; nests via savepoint). Set False when
                the caller commits itself; the cache is then invalidated after the commit of an enclosing
                transaction() or, outside one, NOT here and the caller must invalidate after its own commit.
    """
    payload = json.dumps(value)

    def _execute():
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, payload, utcnow()),
        )

    from ..utils.settings_cache import invalidate_settings_cache

    key_str = key.value if hasattr(key, "value") else str(key)

    if not auto_commit:
        # Inside a managed transaction() the invalidation is deferred to its commit; otherwise
        # the caller must invalidate after its own commit (invalidating earlier would let another
        # connection re-cache the old value).
        _execute()
        with _TX_LOCK:
            managed = id(conn) in _TX_STATES
        if managed:
            run_after_commit(conn, lambda: invalidate_settings_cache(key_str))
        return

    with transaction(conn):
        _execute()
        run_after_commit(conn, lambda: invalidate_settings_cache(key_str))


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read a setting value from the database."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return json.loads(row["value"])


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    """Delete a setting key and invalidate its in-process cache entry after the commit."""
    from ..utils.settings_cache import invalidate_settings_cache

    key_str = key.value if hasattr(key, "value") else str(key)
    with transaction(conn):
        conn.execute("DELETE FROM settings WHERE key=?", (key_str,))
        run_after_commit(conn, lambda: invalidate_settings_cache(key_str))


# ─── Target Status Cache ────────────────────────────────────────────────────


def set_target_status_snapshot(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    ping_up: int | None,
    ping_latency_ms: float | None,
    http_up: int | None,
    http_status: int | None,
    http_latency_ms: float | None,
    tcp_up: int | None,
    tcp_latency_ms: float | None,
    tcp_port_status: str | None = None,
    cert_days_left: int | None,
    uptime_percent: float | None,
    uptime_percent_30d: float | None = None,
    overall_status: str,
) -> None:
    """Atomically update the full status snapshot for a target.

    This prevents mixed states from partial per-metric updates.
    The scheduler is the source of truth for the status semantics.
    """
    now = utcnow()
    with transaction(conn):
        # Ensure row exists
        conn.execute(
            """
            INSERT INTO target_status(target_id, overall_status, updated_at)
            VALUES (?, 'unknown', ?)
            ON CONFLICT(target_id) DO NOTHING
            """,
            (target_id, now),
        )

        prev = conn.execute(
            "SELECT overall_status, last_down_at, cert_days_left, cert_checked_at "
            "FROM target_status WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        prev_overall = prev["overall_status"] if prev else "unknown"
        last_down_at = prev["last_down_at"] if prev else None
        if prev_overall in ("up", "unknown") and overall_status == "down":
            last_down_at = now

        # Preserve last known cert_days_left if current check failed (None)
        # This prevents the badge from flickering to grey on transient failures
        effective_cert_days = cert_days_left
        effective_cert_checked_at = now if cert_days_left is not None else None
        if cert_days_left is None and prev:
            effective_cert_days = prev["cert_days_left"]
            effective_cert_checked_at = prev["cert_checked_at"]

        conn.execute(
            """
            UPDATE target_status
            SET
                ping_up = ?,
                ping_latency_ms = ?,
                ping_checked_at = ?,
                http_up = ?,
                http_status = ?,
                http_latency_ms = ?,
                http_checked_at = ?,
                tcp_up = ?,
                tcp_latency_ms = ?,
                tcp_checked_at = ?,
                tcp_port_status = ?,
                cert_days_left = ?,
                cert_checked_at = ?,
                uptime_percent = ?,
                uptime_percent_30d = ?,
                overall_status = ?,
                last_down_at = ?,
                updated_at = ?
            WHERE target_id = ?
            """,
            (
                ping_up,
                ping_latency_ms,
                now if ping_up is not None else None,
                http_up,
                http_status,
                http_latency_ms,
                now if http_up is not None else None,
                tcp_up,
                tcp_latency_ms,
                now if tcp_up is not None else None,
                tcp_port_status,
                effective_cert_days,
                effective_cert_checked_at,
                uptime_percent,
                uptime_percent_30d,
                overall_status,
                last_down_at,
                now,
                target_id,
            ),
        )


def get_target_status(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    """Get cached status for a target."""
    return conn.execute("SELECT * FROM target_status WHERE target_id = ?", (target_id,)).fetchone()


def get_all_target_statuses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get cached status for all targets."""
    return list(conn.execute("SELECT * FROM target_status ORDER BY target_id").fetchall())


def delete_target_status(conn: sqlite3.Connection, target_id: int) -> None:
    """Delete cached status for a target."""
    with transaction(conn):
        conn.execute("DELETE FROM target_status WHERE target_id = ?", (target_id,))


def get_session_max_days(conn: sqlite3.Connection) -> int:
    """Return the configured absolute session lifetime in days, clamped to the valid range."""
    from ..utils.constants import (
        SESSION_MAX_DAYS_DEFAULT,
        SESSION_MAX_DAYS_MAX,
        SESSION_MAX_DAYS_MIN,
        SettingKeys,
    )

    try:
        days = int(get_setting(conn, SettingKeys.SESSION_MAX_DAYS, SESSION_MAX_DAYS_DEFAULT))
    except (TypeError, ValueError):
        days = SESSION_MAX_DAYS_DEFAULT
    return max(SESSION_MAX_DAYS_MIN, min(days, SESSION_MAX_DAYS_MAX))


def cleanup_expired_tokens(conn: sqlite3.Connection) -> None:
    """Delete expired authentication tokens."""
    with transaction(conn):
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (utcnow(),))


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    can_close_alerts: bool = False,
    must_change_password: bool = False,
) -> int:
    """Create a user and return the new user id."""
    with transaction(conn):
        # Admins always have can_close_alerts permission
        can_close_value = 1 if is_admin or can_close_alerts else 0
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, "
            "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                username.lower(),
                password_hash,
                1 if is_admin else 0,
                can_close_value,
                1,
                1 if must_change_password else 0,
                utcnow(),
            ),
        )
        return int(cur.lastrowid)


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    """Get a user row by username (case-insensitive)."""
    return conn.execute("SELECT * FROM users WHERE username = ?", (username.lower(),)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Get a user row by id."""
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_theme(conn: sqlite3.Connection, user_id: int) -> str:
    """Return a user's theme preference."""
    row = conn.execute("SELECT theme FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["theme"] if row else "system"


def set_user_theme(conn: sqlite3.Connection, user_id: int, theme: str) -> None:
    """Persist a user's theme preference."""
    with transaction(conn):
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Alias for get_user_by_id for consistency."""
    return get_user_by_id(conn, user_id)


def get_all_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all users ordered by username."""
    return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete a user. Associated tokens and alert assignments are
    automatically removed via ON DELETE CASCADE.

    Returns True if user was deleted, False if not found.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def get_first_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Get the first admin user (used as fallback when auth is disabled)."""
    return conn.execute("SELECT * FROM users WHERE is_admin = 1 AND is_active = 1 ORDER BY id LIMIT 1").fetchone()


def update_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    username: str,
    is_admin: bool,
    can_close_alerts: bool = False,
) -> None:
    """Update a user's basic fields."""
    with transaction(conn):
        # Admins always have can_close_alerts permission
        can_close_value = 1 if is_admin or can_close_alerts else 0
        conn.execute(
            "UPDATE users SET username = ?, is_admin = ?, can_close_alerts = ? WHERE id = ?",
            (username.lower(), 1 if is_admin else 0, can_close_value, user_id),
        )


def set_user_active(conn: sqlite3.Connection, user_id: int, *, is_active: bool) -> None:
    """Activate/deactivate a user."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, user_id),
        )


def update_user_password(
    conn: sqlite3.Connection,
    user_id: int,
    password_hash: str,
    *,
    must_change_password: bool | None = None,
    expected_hash: str | None = None,
) -> bool:
    """Update a user's password hash and optionally the must-change flag.

    ``expected_hash`` turns the write into a compare-and-swap so a concurrent
    password change is not overwritten. Returns True if a row was updated.
    """
    sets = ["password_hash = ?"]
    params: list[Any] = [password_hash]
    if must_change_password is not None:
        sets.append("must_change_password = ?")
        params.append(1 if must_change_password else 0)
    where = "id = ?"
    params.append(user_id)
    if expected_hash is not None:
        where += " AND password_hash = ?"
        params.append(expected_hash)
    with transaction(conn):
        cursor = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE {where}", params)  # noqa: S608 - fixed column names only
        updated = cursor.rowcount > 0
        if updated:
            _discard_bootstrap_credentials_for(conn, user_id)
    return updated


def _discard_bootstrap_credentials_for(conn: sqlite3.Connection, user_id: int) -> None:
    """Drop the retrievable initial password once its account got a new one.

    Must run inside the transaction() that changed the password: the actual file unlink is
    deferred via run_after_commit() so it only happens once the OUTERMOST commit succeeds. If
    the enclosing transaction rolls back (e.g. an outer transaction fails after this call), the
    callback is dropped and the file survives, keeping the file and the DB password consistent.
    The username decision is read here, under the same transaction, so it reflects the state the
    commit will persist. If the real transaction was opened by someone else (foreign), its commit
    cannot be observed and run_after_commit() drops the callback with a warning - the safe
    direction, since that only risks showing the bootstrap password once more, never deleting it
    while the DB still holds the old one.
    """
    pending = bootstrap_credentials.configured_path()
    if pending is None or not pending.exists():
        return
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    stored = bootstrap_credentials.stored_username()
    if row is not None and stored in (None, row["username"]):
        run_after_commit(conn, bootstrap_credentials.discard)


def update_user_last_login(conn: sqlite3.Connection, user_id: int, ip_address: str | None = None) -> None:
    """Update the last_login_at timestamp and IP address for a user."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
            (utcnow(), ip_address, user_id),
        )


def issue_token(
    conn: sqlite3.Connection, *, user_id: int, token_hash: str, expires_at: datetime, max_expires_at: datetime
) -> None:
    """Persist a new authentication token hash for a user."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO auth_tokens(user_id, token_hash, expires_at, max_expires_at, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (user_id, token_hash, expires_at, max_expires_at, utcnow()),
        )


def revoke_token(conn: sqlite3.Connection, *, token_hash: str) -> bool:
    """Revoke a token by its hash.

    Returns True if a token was actually deleted, False if not found.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
        return cursor.rowcount > 0


def revoke_tokens_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    """Revoke all tokens for a given user (e.g., after password change).

    Returns the number of tokens revoked.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        return cursor.rowcount


TOKEN_RENEW_MIN_DELTA = timedelta(minutes=5)


def get_user_for_token(conn: sqlite3.Connection, *, token_hash: str) -> sqlite3.Row | None:
    """Resolve a token hash to a user row, returning None if invalid/expired.

    Implements sliding session renewal: On successful validation, extends expires_at
    by 6 hours, but never beyond max_expires_at (the configured absolute session lifetime).

    The renewal is written only when it extends expiry noticeably, so most
    lookups stay read-only. Expired rows are removed by cleanup_expired_tokens().
    """
    now = utcnow()

    with transaction(conn):
        # Single query: get user + token info
        row = conn.execute(
            """
            SELECT u.*, t.max_expires_at, t.expires_at AS token_expires_at
            FROM auth_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
              AND t.expires_at > ?
              AND t.max_expires_at > ?
              AND u.is_active = 1
            """,
            (token_hash, now, now),
        ).fetchone()

        if row is None:
            return None

        # Sliding renewal within same transaction
        new_expires_at = min(now + timedelta(hours=6), row["max_expires_at"])
        if new_expires_at - _convert_datetime(str(row["token_expires_at"]).encode()) >= TOKEN_RENEW_MIN_DELTA:
            conn.execute("UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?", (new_expires_at, token_hash))

    return row


def get_user_for_token_readonly(conn: sqlite3.Connection, *, token_hash: str) -> sqlite3.Row | None:
    """Like get_user_for_token() but never renews the session (long-lived streams must not keep it alive)."""
    now = utcnow()
    return conn.execute(
        """
        SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ? AND t.expires_at > ? AND t.max_expires_at > ? AND u.is_active = 1
        """,
        (token_hash, now, now),
    ).fetchone()


# ─── Target Alert Users ─────────────────────────────────────────────────────────


def set_target_alert_users(conn: sqlite3.Connection, target_id: int, user_ids: list[int]) -> None:
    """Set which users get alerts for a target (replaces existing)."""
    with transaction(conn):
        conn.execute("DELETE FROM target_alert_users WHERE target_id = ?", (target_id,))
        for user_id in user_ids:
            conn.execute(
                "INSERT INTO target_alert_users(target_id, user_id) VALUES (?, ?)",
                (target_id, user_id),
            )


def get_target_alert_users(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Get all users that should receive alerts for a target."""
    return list(
        conn.execute(
            """
            SELECT u.*
            FROM users u
            JOIN target_alert_users tau ON tau.user_id = u.id
            WHERE tau.target_id = ?
            ORDER BY u.username
            """,
            (target_id,),
        ).fetchall()
    )


def list_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List targets ordered by group/name/id."""
    rows = conn.execute("SELECT * FROM targets ORDER BY group_name, name, id").fetchall()
    return list(rows)


def list_targets_paginated(
    conn: sqlite3.Connection, page: int = 1, page_size: int = 50
) -> tuple[list[sqlite3.Row], int]:
    """List targets with pagination. Returns (rows, total_count)."""
    offset = (page - 1) * page_size

    # Get total count
    total = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]

    # Get page items - sort groups first, NULL groups (ungrouped) last
    rows = conn.execute(
        """
        SELECT * FROM targets
        ORDER BY CASE WHEN group_name IS NULL THEN 1 ELSE 0 END, group_name, name, id
        LIMIT ? OFFSET ?
        """,
        (page_size, offset),
    ).fetchall()

    return list(rows), total


def get_target(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    """Get a target row by id."""
    return conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()


class DuplicateTargetError(ValueError):
    """A target with an identical monitoring identity (same checks on the same endpoints) already exists."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("An identical monitoring target already exists")
        self.conflicts = conflicts


def _assert_no_exact_duplicate(conn: sqlite3.Connection, data: dict[str, Any], exclude_id: int | None = None) -> None:
    """Raise DuplicateTargetError when ``data`` repeats an existing target's complete check identity.

    Runs inside the writer's transaction, so the DAL stays the single place that enforces the rule
    for the API, importers and direct callers alike.
    """
    from ..services.target_identity import classify_conflicts

    exact = [
        c for c in classify_conflicts(data, list_targets(conn), exclude_id=exclude_id) if c["classification"] == "exact"
    ]
    if exact:
        raise DuplicateTargetError(exact)


def create_target(conn: sqlite3.Connection, values: dict[str, Any], *, manage_transaction: bool = True) -> int:
    """Create a target and return the new target id.

    Raises:
        DuplicateTargetError: An identical monitoring target exists.
    """
    now = utcnow()
    from ..services.target_identity import identity_signature

    with transaction(conn, immediate=True) if manage_transaction else nullcontext():
        _assert_no_exact_duplicate(conn, values)
        cur = conn.execute(
            """
                INSERT INTO targets(
                    name, group_name, is_enabled,
                    interval_seconds, retention_days,
                    enable_ping, enable_http_check, enable_cert_expiration, enable_tcp_connect,
                    host, ping_host, http_url, http_username, http_password, http_prefer_head, http_basic_auth_enabled,
                    http_success_codes, http_port,
                    cert_host, cert_port, ignore_cert_errors, tcp_host, tcp_ports,
                    max_retries, ping_timeout_minutes,
                    auto_close_alert,
                    sla_enabled, sla_availability_pct, sla_response_time_ms, sla_ping_latency_ms,
                    identity_signature, created_at, updated_at
                ) VALUES(
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?,
                    ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
            (
                values["name"],
                values.get("group_name"),
                1 if values.get("is_enabled", True) else 0,
                int(values["interval_seconds"]),
                int(values["retention_days"]),
                1 if values.get("enable_ping", False) else 0,
                1 if values.get("enable_http_check", False) else 0,
                1 if values.get("enable_cert_expiration", False) else 0,
                1 if values.get("enable_tcp_connect", False) else 0,
                values["host"],
                values.get("ping_host"),
                values.get("http_url"),
                values.get("http_username"),
                values.get("http_password"),
                1 if values.get("http_prefer_head", True) else 0,
                1 if values.get("http_basic_auth_enabled", False) else 0,
                values.get("http_success_codes"),
                int(values["http_port"]) if values.get("http_port") else None,
                values.get("cert_host"),
                int(values["cert_port"]) if values.get("cert_port") else None,
                1 if values.get("ignore_cert_errors", False) else 0,
                values.get("tcp_host"),
                str(values.get("tcp_ports") or "80"),
                int(values.get("max_retries", 5)),
                int(values.get("ping_timeout_minutes", 5)),
                1 if values.get("auto_close_alert", True) else 0,
                1 if values.get("sla_enabled", False) else 0,
                values.get("sla_availability_pct"),
                values.get("sla_response_time_ms"),
                values.get("sla_ping_latency_ms"),
                identity_signature(values),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_target(
    conn: sqlite3.Connection, *, target_id: int, patch: dict[str, Any], manage_transaction: bool = True
) -> None:
    """Apply a partial update to a target.

    Raises:
        DuplicateTargetError: The resulting target would repeat another target's identity.
    """
    if not patch:
        return

    _allowed_target_columns = frozenset(
        {
            "name",
            "group_name",
            "is_enabled",
            "interval_seconds",
            "retention_days",
            "enable_ping",
            "enable_http_check",
            "enable_cert_expiration",
            "enable_tcp_connect",
            "host",
            "ping_host",
            "http_url",
            "http_username",
            "http_password",
            "http_prefer_head",
            "http_basic_auth_enabled",
            "http_success_codes",
            "http_port",
            "cert_host",
            "cert_port",
            "ignore_cert_errors",
            "tcp_host",
            "tcp_ports",
            "max_retries",
            "auto_close_alert",
            "ping_timeout_minutes",
            "sla_enabled",
            "sla_availability_pct",
            "sla_response_time_ms",
            "sla_ping_latency_ms",
        }
    )

    cols: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k not in _allowed_target_columns:
            continue
        if (
            k
            in {
                "is_enabled",
                "enable_ping",
                "enable_http_check",
                "enable_cert_expiration",
                "enable_tcp_connect",
                "ignore_cert_errors",
                "sla_enabled",
                "http_prefer_head",
                "http_basic_auth_enabled",
                "auto_close_alert",
            }
            and v is not None
        ):
            value = 1 if bool(v) else 0
        else:
            value = v
        cols.append(f'"{k}" = ?')  # Quoted identifier
        params.append(value)
    cols.append("updated_at = ?")
    params.append(utcnow())
    params.append(target_id)
    with transaction(conn, immediate=True) if manage_transaction else nullcontext():
        existing = get_target(conn, target_id)
        if existing is not None:
            from ..services.target_identity import identity_signature

            updated = {**dict(existing), **patch}
            _assert_no_exact_duplicate(conn, updated, exclude_id=target_id)
            cols.insert(-1, "identity_signature = ?")
            params.insert(-2, identity_signature(updated))
        conn.execute(f"UPDATE targets SET {', '.join(cols)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
    """Delete a target by id."""
    with transaction(conn):
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))


# ─────────────────────────────────────────────────────────────────────────────
# Groups CRUD
# ─────────────────────────────────────────────────────────────────────────────


def list_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all groups."""
    return conn.execute("SELECT * FROM groups ORDER BY name").fetchall()


def list_groups_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all groups with their target count (N+1 query optimization)."""
    sql = """
        SELECT g.*, COUNT(t.id) as target_count
        FROM groups g
        LEFT JOIN targets t ON t.group_name = g.name
        GROUP BY g.id
        ORDER BY g.name
    """
    return conn.execute(sql).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    """Get a group by id."""
    return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def get_group_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Get a group by name."""
    return conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()


def create_group(conn: sqlite3.Connection, *, name: str, description: str | None = None) -> int:
    """Create a group and return the new group id."""
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO groups(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description, now, now),
        )
        return int(cur.lastrowid)


def update_group(conn: sqlite3.Connection, *, group_id: int, patch: dict[str, Any]) -> None:
    """Apply a partial update to a group."""
    if not patch:
        return
    allowed = {"name", "description"}
    cols: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k not in allowed:
            continue
        cols.append(f"{k} = ?")
        params.append(v)
    cols.append("updated_at = ?")
    params.append(utcnow())
    params.append(group_id)
    with transaction(conn):
        conn.execute(f"UPDATE groups SET {', '.join(cols)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Delete a group by id."""
    with transaction(conn):
        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))


def count_targets_in_group(conn: sqlite3.Connection, group_name: str) -> int:
    """Count how many targets are assigned to the given group name."""
    row = conn.execute("SELECT COUNT(*) as cnt FROM targets WHERE group_name = ?", (group_name,)).fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Login Attempts (Rate Limiting)
# ---------------------------------------------------------------------------


def get_login_attempt(conn: sqlite3.Connection, ip_address: str) -> sqlite3.Row | None:
    """Get login attempt record for an IP address."""
    return conn.execute(
        "SELECT * FROM login_attempts WHERE ip_address = ?",
        (ip_address,),
    ).fetchone()


def record_failed_login(conn: sqlite3.Connection, ip_address: str) -> int:
    """
    Record a failed login attempt and return the new failed count.

    Implements exponential backoff: lock_seconds = 2^failed_count (capped at 1 hour).
    """
    now = utcnow()

    with transaction(conn):
        # First, get current count (if exists) to calculate correct locked_until
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE ip_address = ?",
            (ip_address,),
        ).fetchone()

        new_count = (int(row["failed_count"]) + 1) if row else 1
        lock_seconds = min(2**new_count, 3600)
        locked_until = now + timedelta(seconds=lock_seconds)

        # Atomic upsert with correct locked_until from the start
        conn.execute(
            """
            INSERT INTO login_attempts(ip_address, failed_count, last_attempt_at, locked_until, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ip_address) DO UPDATE SET
                failed_count = ?,
                last_attempt_at = ?,
                locked_until = ?
            """,
            (ip_address, new_count, now, locked_until, now, new_count, now, locked_until),
        )
        return new_count


def clear_login_attempts(conn: sqlite3.Connection, ip_address: str) -> None:
    """Clear login attempts after successful login."""
    with transaction(conn):
        conn.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,))


def is_ip_locked(conn: sqlite3.Connection, ip_address: str) -> tuple[bool, int | None]:
    """
    Check if an IP is currently locked out.

    Returns: (is_locked, seconds_remaining)
    """
    record = get_login_attempt(conn, ip_address)
    if not record or not record["locked_until"]:
        return False, None

    now = utcnow()
    locked_until = record["locked_until"]

    if locked_until > now:
        remaining = int((locked_until - now).total_seconds())
        return True, remaining

    return False, None


# ─── Recipients ─────────────────────────────────────────────────────────────


def list_recipients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all alert recipients."""
    return list(conn.execute("SELECT * FROM recipients ORDER BY name").fetchall())


def get_recipient(conn: sqlite3.Connection, recipient_id: int) -> sqlite3.Row | None:
    """Get a single recipient by ID."""
    return conn.execute("SELECT * FROM recipients WHERE id = ?", (recipient_id,)).fetchone()


def get_recipient_by_phone(conn: sqlite3.Connection, phone: str) -> sqlite3.Row | None:
    """Get a recipient by phone number."""
    return conn.execute("SELECT * FROM recipients WHERE phone = ?", (phone,)).fetchone()


def create_recipient(
    conn: sqlite3.Connection, *, name: str, phone: str | None = None, email: str | None = None, is_enabled: bool = True
) -> int:
    """Create a new recipient."""
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO recipients(name, phone, email, is_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, phone, email, 1 if is_enabled else 0, now, now),
        )
        return cur.lastrowid


def update_recipient(
    conn: sqlite3.Connection,
    recipient_id: int,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    is_enabled: bool | None = None,
    clear_phone: bool = False,
    clear_email: bool = False,
) -> None:
    """Update a recipient.

    Args:
            recipient_id: ID of recipient to update
            name: New name (optional)
            phone: New phone number (optional)
            email: New email address (optional)
            is_enabled: New enabled status (optional)
            clear_phone: If True, set phone to NULL
            clear_email: If True, set email to NULL
    """
    updates = ["updated_at = ?"]
    params: list[Any] = [utcnow()]

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if clear_phone:
        updates.append("phone = NULL")
    elif phone is not None:
        updates.append("phone = ?")
        params.append(phone)
    if clear_email:
        updates.append("email = NULL")
    elif email is not None:
        updates.append("email = ?")
        params.append(email)
    if is_enabled is not None:
        updates.append("is_enabled = ?")
        params.append(1 if is_enabled else 0)

    params.append(recipient_id)
    with transaction(conn):
        conn.execute(f"UPDATE recipients SET {', '.join(updates)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params


def delete_recipient(conn: sqlite3.Connection, recipient_id: int) -> None:
    """Delete a recipient."""
    with transaction(conn):
        conn.execute("DELETE FROM recipients WHERE id = ?", (recipient_id,))


# ─── Target Recipients (Many-to-Many) ───────────────────────────────────────


def get_target_recipients(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Get all recipients assigned to a target."""
    return list(
        conn.execute(
            """
        SELECT r.* FROM recipients r
        JOIN target_recipients tr ON r.id = tr.recipient_id
        WHERE tr.target_id = ?
        ORDER BY r.name
        """,
            (target_id,),
        ).fetchall()
    )


def get_target_recipient_ids(conn: sqlite3.Connection, target_id: int) -> list[int]:
    """Get recipient IDs assigned to a target."""
    rows = conn.execute(
        "SELECT recipient_id FROM target_recipients WHERE target_id = ?",
        (target_id,),
    ).fetchall()
    return [row["recipient_id"] for row in rows]


def set_target_recipients(
    conn: sqlite3.Connection, target_id: int, recipient_ids: list[int], *, manage_transaction: bool = True
) -> None:
    """Set the recipients for a target (replaces existing assignments)."""
    with transaction(conn) if manage_transaction else nullcontext():
        conn.execute("DELETE FROM target_recipients WHERE target_id = ?", (target_id,))
        for rid in recipient_ids:
            conn.execute(
                "INSERT INTO target_recipients(target_id, recipient_id) VALUES (?, ?)",
                (target_id, rid),
            )


def get_targets_by_recipient(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """Get the assigned targets of all recipients in one query, grouped by recipient id."""
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT tr.recipient_id AS recipient_id, t.id, t.name
        FROM targets t
        JOIN target_recipients tr ON t.id = tr.target_id
        ORDER BY t.name
        """
    ).fetchall():
        grouped.setdefault(row["recipient_id"], []).append(row)
    return grouped


def get_recipient_targets(conn: sqlite3.Connection, recipient_id: int) -> list[sqlite3.Row]:
    """Get all targets assigned to a recipient."""
    return list(
        conn.execute(
            """
        SELECT t.id, t.name
        FROM targets t
        JOIN target_recipients tr ON t.id = tr.target_id
        WHERE tr.recipient_id = ?
        ORDER BY t.name
        """,
            (recipient_id,),
        ).fetchall()
    )


# ─── Alerts (Downtime Events) ───────────────────────────────────────────────


def create_alert(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    target_id: int,
    target_name: str,
    failed_service: str,
    started_at: datetime,
    recipients_notified: list[str],
) -> None:
    """Create a new downtime alert record."""
    now = utcnow()
    recipients_json = json.dumps(recipients_notified)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO alerts (
                id, target_id, target_name, failed_service, status,
                started_at, recipients_notified, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (alert_id, target_id, target_name, failed_service, started_at, recipients_json, now, now),
        )
        from . import notifications

        notifications.notify_alert_opened(
            conn, alert_id=alert_id, target_id=target_id, target_name=target_name, failed_service=failed_service
        )


def resolve_alert(
    conn: sqlite3.Connection,
    alert_id: str,
    ended_at: datetime,
) -> None:
    """Mark an alert as resolved and record the end time."""
    now = utcnow()
    with transaction(conn):
        # Get started_at to compute duration
        row = conn.execute(
            "SELECT started_at, target_id, target_name, failed_service FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        if row:
            duration_seconds = int((ended_at - row["started_at"]).total_seconds())
            conn.execute(
                """
                UPDATE alerts SET
                    status = 'resolved',
                    ended_at = ?,
                    duration_seconds = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (ended_at, duration_seconds, now, alert_id),
            )
            from . import notifications

            notifications.notify_alert_resolved(
                conn,
                alert_id=alert_id,
                target_id=int(row["target_id"]),
                target_name=row["target_name"],
                failed_service=row["failed_service"],
            )


def list_open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all currently open alerts.

    Returns:
            List of alert rows with columns: target_id, id, started_at, failed_service
    """
    return conn.execute("SELECT target_id, id, started_at, failed_service FROM alerts WHERE status = 'open'").fetchall()


def acknowledge_alert(
    conn: sqlite3.Connection,
    alert_id: str,
    acknowledged_by: str,
) -> bool:
    """Acknowledge (close) an alert. Sets status to resolved and records who closed it. Returns True if updated."""
    now = utcnow()
    with transaction(conn):
        # Get started_at to compute duration
        row = conn.execute("SELECT started_at FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        duration_seconds = None
        if row:
            duration_seconds = int((now - row["started_at"]).total_seconds())

        cur = conn.execute(
            """
            UPDATE alerts SET
                status = 'resolved',
                ended_at = COALESCE(ended_at, ?),
                duration_seconds = COALESCE(duration_seconds, ?),
                is_acknowledged = 1,
                acknowledged_by = ?,
                acknowledged_at = ?,
                updated_at = ?
            WHERE id = ? AND is_acknowledged = 0
            """,
            (now, duration_seconds, acknowledged_by, now, now, alert_id),
        )
        return cur.rowcount > 0


def acknowledge_all_open_alerts(
    conn: sqlite3.Connection,
    acknowledged_by: str,
) -> int:
    """Acknowledge (close) all open alerts and return number of updated rows."""
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            """
            UPDATE alerts SET
                status = 'resolved',
                ended_at = COALESCE(ended_at, ?),
                duration_seconds = COALESCE(
                    duration_seconds,
                    CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)
                ),
                is_acknowledged = 1,
                acknowledged_by = ?,
                acknowledged_at = ?,
                updated_at = ?
            WHERE status = 'open' AND is_acknowledged = 0
            """,
            (now, now, acknowledged_by, now, now),
        )
        return cur.rowcount


def list_alerts(
    conn: sqlite3.Connection,
    *,
    target_id: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
    """
    List alerts with optional filters and pagination.
    Returns (items, total_count).
    """
    conditions = []
    params: list[Any] = []

    if target_id is not None:
        conditions.append("target_id = ?")
        params.append(target_id)
    if from_date is not None:
        conditions.append("started_at >= ?")
        params.append(from_date)
    if to_date is not None:
        conditions.append("started_at <= ?")
        params.append(to_date)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Count total
    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM alerts WHERE {where_clause}",  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
        params,
    ).fetchone()
    total = count_row["cnt"] if count_row else 0

    # Fetch page
    offset = (page - 1) * page_size
    params_with_pagination = [*params, page_size, offset]
    rows = conn.execute(
        f"""
        SELECT * FROM alerts
        WHERE {where_clause}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
        params_with_pagination,
    ).fetchall()

    return list(rows), total


def get_alert(conn: sqlite3.Connection, alert_id: str) -> sqlite3.Row | None:
    """Get a single alert by ID."""
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def get_alerts_for_target_in_range(
    conn: sqlite3.Connection,
    target_id: int,
    from_date: datetime,
    to_date: datetime,
) -> list[sqlite3.Row]:
    """Return monitoring-alert intervals overlapping the half-open range [from_date, to_date).

    These rows describe monitoring state, not the user-facing notification inbox. Inbox events
    use ``notifications.created_at`` and are queried by ``app.db.notifications``.
    """
    from_date = ensure_utc(from_date)
    to_date = ensure_utc(to_date)
    if from_date >= to_date:
        return []
    return list(
        conn.execute(
            """
        SELECT * FROM alerts
        WHERE target_id = ?
            AND started_at < ?
            AND (ended_at IS NULL OR ended_at > ?)
        ORDER BY started_at ASC
        """,
            (target_id, to_date, from_date),
        ).fetchall()
    )


def delete_all_alerts(conn: sqlite3.Connection) -> int:
    """Delete all alerts from the database. Returns the number of deleted rows."""
    with transaction(conn):
        cur = conn.execute("DELETE FROM alerts")
        return cur.rowcount


def delete_alerts_by_target(conn: sqlite3.Connection, target_id: int) -> int:
    """Delete all alerts (open and closed) for a specific target. Returns the number of deleted rows."""
    with transaction(conn):
        cur = conn.execute("DELETE FROM alerts WHERE target_id = ?", (target_id,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Monitor Quality (Quality Gate feature)
# ---------------------------------------------------------------------------


def insert_monitor_quality(
    conn: sqlite3.Connection,
    *,
    score: float | None,
    median_latency_ms: float | None,
    success_ratio: float,
    state: str,
) -> None:
    """
    Insert a monitor quality measurement and update the latest snapshot.

    Args:
            score: Quality score (0-100), None if no connectivity
            median_latency_ms: Median RTT to probe targets
            success_ratio: Ratio of successful probes (0.0-1.0)
            state: Quality state ("ok", "degraded", "down")
    """
    now = utcnow()

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO monitor_quality(ts, score, median_latency_ms, success_ratio, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now, score, median_latency_ms, success_ratio, state),
        )

        conn.execute(
            """
            INSERT INTO monitor_quality_latest(id, score, state, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                score = excluded.score,
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (score, state, now),
        )


def get_monitor_quality_latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Get the full latest monitor quality snapshot."""
    return conn.execute("SELECT * FROM monitor_quality_latest WHERE id = 1").fetchone()


def query_monitor_quality(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[sqlite3.Row]:
    """
    Query monitor quality time series.

    Args:
            since: Start of time range (inclusive)
            until: End of time range (inclusive)
            limit: Maximum number of rows to return

    Returns:
            List of monitor quality rows, ordered by timestamp ascending
    """
    conditions = []
    params: list[Any] = []

    if since:
        conditions.append("ts >= ?")
        params.append(since)
    if until:
        conditions.append("ts <= ?")
        params.append(until)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    return list(
        conn.execute(
            f"""
        SELECT * FROM monitor_quality
        WHERE {where}
        ORDER BY ts ASC
        LIMIT ?
        """,  # noqa: S608 - code-built WHERE from fixed clauses, values bound as params
            params,
        ).fetchall()
    )


# ─── Report Jobs (Async PDF Generation) ────────────────────────────────────


def create_report_job(
    conn: sqlite3.Connection,
    job_id: str,
    target_id: int,
    expires_hours: int = 1,
    user_id: int | None = None,
) -> None:
    """Create a new pending report job owned by *user_id* (None = legacy/ownerless, admin-only access)."""
    now = utcnow()
    expires_at = now + timedelta(hours=expires_hours)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO report_jobs(job_id, target_id, status, progress, created_at, expires_at, user_id)
            VALUES(?, ?, 'pending', 0, ?, ?, ?)
            """,
            (job_id, target_id, now, expires_at, user_id),
        )


def _notify_report_stage(conn: sqlite3.Connection, job_id: str, stage: str) -> None:
    """Record one terminal report result in the caller's transaction."""
    row = conn.execute(
        "SELECT j.user_id, j.target_id, t.name FROM report_jobs j LEFT JOIN targets t ON t.id = j.target_id "
        "WHERE j.job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return
    from . import notifications

    notifications.notify_report(
        conn,
        stage=stage,
        job_id=job_id,
        user_id=row["user_id"],
        target_id=int(row["target_id"]),
        target_name=row["name"] or "target",
    )


def get_report_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Get a report job by ID."""
    return conn.execute(
        "SELECT * FROM report_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()


def update_report_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    filename: str | None = None,
    error: str | None = None,
    completed: bool = False,
) -> None:
    """Update report job fields."""
    updates = []
    params: list[Any] = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if progress is not None:
        updates.append("progress = ?")
        params.append(progress)
    if filename is not None:
        updates.append("filename = ?")
        params.append(filename)
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    if completed:
        updates.append("completed_at = ?")
        params.append(utcnow())

    if not updates:
        return

    params.append(job_id)
    terminal = status in ("completed", "failed")
    # A finished job is never rewritten: not by a repeated terminal update (no second event) and not by a
    # late progress/processing write from a stale worker.
    guard = " AND status IN ('pending', 'processing')"
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE report_jobs SET {', '.join(updates)} WHERE job_id = ?{guard}",  # noqa: S608 - code-built column list, values bound as params
            params,
        )
        if terminal and cur.rowcount:
            _notify_report_stage(conn, job_id, status)


def get_expired_job_ids(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Get list of expired job IDs for cleanup (DAL abstraction)."""
    rows = conn.execute(
        "SELECT job_id FROM report_jobs WHERE expires_at < ?",
        (now,),
    ).fetchall()
    return [row["job_id"] for row in rows]


def prune_expired_report_jobs(conn: sqlite3.Connection) -> int:
    """Delete expired report jobs and their files."""
    now = utcnow()
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM report_jobs WHERE expires_at < ?",
            (now,),
        )
        return cur.rowcount


# ─── Scheduler Locking (Idempotency) ────────────────────────────────────────


def register_scheduler_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Register a new scheduler run for idempotency tracking."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO scheduler_runs (run_id, started_at, heartbeat_at) VALUES (?, ?, ?)",
            (run_id, now, now),
        )


def update_scheduler_heartbeat(conn: sqlite3.Connection, run_id: str) -> None:
    """Update scheduler heartbeat to prevent lock expiry."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "UPDATE scheduler_runs SET heartbeat_at = ? WHERE run_id = ?",
            (now, run_id),
        )


def cleanup_stale_scheduler_runs(conn: sqlite3.Connection, timeout_seconds: int = 300) -> int:
    """Remove scheduler runs that haven't heartbeated recently."""
    cutoff = utcnow() - timedelta(seconds=timeout_seconds)
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM scheduler_runs WHERE heartbeat_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def try_lock_target(
    conn: sqlite3.Connection,
    target_id: int,
    run_id: str,
    ttl_seconds: int,
) -> bool:
    """
    Try to acquire lock for target check.

    Returns True if lock acquired, False if already locked by another run.
    Uses INSERT OR REPLACE with expiry check for atomic locking.
    """
    now = utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)

    with transaction(conn):
        # Clean up expired locks first
        conn.execute(
            "DELETE FROM target_locks WHERE expires_at < ?",
            (now,),
        )

        # Check if already locked by different run
        existing = conn.execute(
            "SELECT run_id FROM target_locks WHERE target_id = ? AND expires_at >= ?",
            (target_id, now),
        ).fetchone()

        if existing and existing[0] != run_id:
            # Locked by another run
            return False

        # Acquire or refresh lock
        conn.execute(
            "INSERT OR REPLACE INTO target_locks (target_id, run_id, locked_at, expires_at) VALUES (?, ?, ?, ?)",
            (target_id, run_id, now, expires_at),
        )
        return True


def release_target_lock(conn: sqlite3.Connection, target_id: int, run_id: str) -> None:
    """Release target lock after check completes."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM target_locks WHERE target_id = ? AND run_id = ?",
            (target_id, run_id),
        )


def mark_stale_report_jobs_failed(conn: sqlite3.Connection) -> int:
    """Mark any 'pending' or 'processing' jobs as failed (called on startup).

    Jobs in these states after a restart are orphaned because the in-memory
    queue was lost. Mark them as failed so the frontend stops polling.
    """
    now = utcnow()
    with transaction(conn):
        stale_ids = [
            r[0] for r in conn.execute("SELECT job_id FROM report_jobs WHERE status IN ('pending', 'processing')")
        ]
        cur = conn.execute(
            """
            UPDATE report_jobs
            SET status = 'failed',
                progress = 100,
                error = 'Server restarted before job completed',
                completed_at = ?
            WHERE status IN ('pending', 'processing')
            """,
            (now,),
        )
        for stale_id in stale_ids:
            _notify_report_stage(conn, stale_id, "failed")
        return cur.rowcount


# ---------------------------------------------------------------------------
# Bootstrap admin (first start only)
# ---------------------------------------------------------------------------

BOOTSTRAP_ADMIN_USERNAME = "admin"
_PRONOUNCEABLE_VOWELS = "aeiou"
_PRONOUNCEABLE_CONSONANTS = "bcdfghjklmnpqrstvwxyz"
_BOOTSTRAP_SPECIALS = "!?#$%&*+-=@"


def _print_bootstrap_credentials(username: str, password: str) -> None:
    """Write the bootstrap credentials to stdout, bypassing the logging handlers."""
    banner = (
        "\n"
        "======================================================================\n"
        " justUp! bootstrap admin (shown on every start until the password is changed)\n"
        f"   Username: {username}\n"
        f"   Password: {password}\n"
        " Sign in and change this password to continue.\n"
        " This notice disappears for good after the password was changed.\n"
        "======================================================================\n"
    )
    sys.stdout.write(banner)
    sys.stdout.flush()


def reprint_pending_bootstrap_credentials(conn: sqlite3.Connection) -> bool:
    """Show the initial admin password again while it is still the valid, unchanged one.

    The stored plaintext is only trusted when it still matches the account's hash and the
    account still must change its password; otherwise (changed via API, CLI or a restore)
    the file is stale and removed without printing. Returns True if the banner was printed.
    """
    creds = bootstrap_credentials.load()
    if creds is None:
        bootstrap_credentials.discard()  # missing is a no-op; unreadable or malformed is never trusted
        return False
    username, password = creds
    user = get_user_by_username(conn, username)
    if user is None or not user["must_change_password"] or not verify_password(password, user["password_hash"]):
        bootstrap_credentials.discard()
        return False
    _print_bootstrap_credentials(username, password)
    return True


def _generate_pronounceable_password() -> str:
    """Generate a short one-time password in 'Tujoza47!' style.

    Pattern: 3 consonant-vowel syllables (6 letters, first one capitalised),
    2 digits and 1 special character from a small, easily typed set (9 chars).
    ``must_change_password=1`` forces an immediate change. Randomness comes
    only from the ``secrets`` module.
    """
    letters = "".join(
        secrets.choice(_PRONOUNCEABLE_CONSONANTS) + secrets.choice(_PRONOUNCEABLE_VOWELS) for _ in range(3)
    )
    digits = f"{secrets.randbelow(100):02d}"
    return letters.capitalize() + digits + secrets.choice(_BOOTSTRAP_SPECIALS)


def ensure_default_admin(conn: sqlite3.Connection, credentials_path: Path | None = None) -> bool:
    """Create the bootstrap admin on first start when no users exist.

    Generates a random pronounceable password, stores only its hash with
    ``must_change_password = 1`` and prints the plaintext to stdout after the
    insert is committed. The plaintext also goes to a private 0600 file (see
    ``utils.bootstrap_credentials``) so every later start can show it again until
    the password was changed; it never reaches the database or the logging
    handlers. Returns True if the admin was created.
    """
    password: str | None = None
    try:
        # IMMEDIATE takes the write lock first so concurrent workers serialise on the user count.
        with transaction(conn, immediate=True):
            (count,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            if count == 0:
                password = _generate_pronounceable_password()
                conn.execute(
                    "INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, "
                    "must_change_password, created_at) VALUES (?, ?, 1, 1, 1, 1, ?)",
                    (BOOTSTRAP_ADMIN_USERNAME, hash_password(password), utcnow()),
                )
                # Written before the commit: a file without a committed account is stale and ignored.
                bootstrap_credentials.store(BOOTSTRAP_ADMIN_USERNAME, password, credentials_path)
    except sqlite3.IntegrityError:
        _log.debug("Default admin insert skipped because a user already exists")
        return False
    if password is None:
        return False
    _print_bootstrap_credentials(BOOTSTRAP_ADMIN_USERNAME, password)
    _log.info("Bootstrap admin '%s' created; the initial password was printed to the console", BOOTSTRAP_ADMIN_USERNAME)
    return True
