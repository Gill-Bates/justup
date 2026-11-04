#!/usr/bin/env python3
#
# app/db/sqlite/connection.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite connections: registry, quiesce for backups, pragmas and transaction helpers."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Keep the pre-split logger name: log filters and caplog assertions address app.db.sqlite.
_log = logging.getLogger("app.db.sqlite")


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
    if str(current).lower() == "wal":
        return
    if str(current).lower() == "memory" and not conn.execute("PRAGMA database_list").fetchone()[2]:
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
