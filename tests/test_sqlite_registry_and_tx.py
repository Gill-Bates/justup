#!/usr/bin/env python3
#
# tests/test_sqlite_registry_and_tx.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Contracts of the SQLite connection registry and of transaction() inside foreign transactions.

Synchronisation uses events and hooks only; nothing here relies on sleeping as proof.
"""

# ruff: noqa: SLF001, ARG001
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.utils import bootstrap_credentials


class _HookedConn(sqlite3.Connection):
    """Connection whose close() can fail or run a hook, to model broken or racing handles."""

    close_error: BaseException | None = None
    close_error_after_close: bool = False
    on_close: Callable[[], None] | None = None

    def close(self) -> None:
        if self.on_close is not None:
            self.on_close()
        if self.close_error is not None:
            if self.close_error_after_close:
                super().close()
            raise self.close_error
        super().close()


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """Isolate the process-wide connection registry for one test (restored afterwards)."""
    monkeypatch.setattr(sqlite_db, "_OPEN_CONNECTIONS", set())
    monkeypatch.setattr(sqlite_db, "_CONNECTIONS_OPENED", 0)
    monkeypatch.setattr(sqlite_db, "_CONNECTIONS_CLOSED", 0)
    monkeypatch.setattr(sqlite_db, "_PENDING_CONNECTS", 0)
    monkeypatch.setattr(sqlite_db, "_QUIESCED", False)
    yield
    for conn in list(sqlite_db._OPEN_CONNECTIONS):
        if isinstance(conn, _HookedConn):
            conn.close_error = None
            conn.on_close = None
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "reg.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def hooked_connect(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> Callable[[], _HookedConn]:
    real_connect = sqlite3.connect

    def _connect() -> _HookedConn:
        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_HookedConn, **k))
            conn = sqlite_db.connect(db_path)
        assert isinstance(conn, _HookedConn)
        return conn

    return _connect


def _values(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]


# ---------------------------------------------------------------------------
# transaction() inside a transaction somebody else opened
# ---------------------------------------------------------------------------


@pytest.fixture
def two(registry, db_path: Path):
    owner = sqlite_db.connect(db_path)
    reader = sqlite_db.connect(db_path)
    yield owner, reader
    for c in (owner, reader):
        sqlite_db.close_connection(c)


def test_foreign_transaction_is_not_committed_by_inner_block(two):
    owner, reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")  # implicit transaction, owned by the caller
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('inner')")
    assert owner.in_transaction
    assert _values(reader) == []  # nothing was committed by transaction()
    owner.commit()
    assert _values(reader) == ["prior", "inner"]


def test_foreign_transaction_rollback_by_owner_undoes_inner_work(two):
    owner, reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('inner')")
    owner.rollback()
    assert _values(owner) == []
    assert _values(reader) == []


def test_manual_begin_is_treated_as_foreign(two):
    owner, reader = two
    owner.execute("BEGIN")
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('inner')")
    assert owner.in_transaction
    assert _values(reader) == []
    owner.commit()
    assert _values(reader) == ["inner"]


def test_inner_error_rolls_back_only_inner_work_and_keeps_foreign_transaction_open(two):
    owner, reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")
    with pytest.raises(RuntimeError, match="boom"), sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('inner')")
        raise RuntimeError("boom")
    assert owner.in_transaction
    assert _values(owner) == ["prior"]
    assert _values(reader) == []
    owner.commit()
    assert _values(reader) == ["prior"]


def test_nested_transactions_inside_foreign_transaction_use_savepoints(two):
    owner, _reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('outer')")
        with pytest.raises(ValueError, match="inner"), sqlite_db.transaction(owner):
            owner.execute("INSERT INTO t VALUES ('inner')")
            raise ValueError("inner")
        with sqlite_db.transaction(owner):
            owner.execute("INSERT INTO t VALUES ('inner-ok')")
    assert owner.in_transaction
    assert _values(owner) == ["prior", "outer", "inner-ok"]
    owner.rollback()
    assert _values(owner) == []


def test_immediate_inside_foreign_transaction_is_refused_and_leaves_it_intact(two):
    owner, _reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")
    with pytest.raises(sqlite3.OperationalError), sqlite_db.transaction(owner, immediate=True):
        pytest.fail("block must not run")  # pragma: no cover
    assert owner.in_transaction
    assert _values(owner) == ["prior"]


def test_foreign_scope_releases_state_so_next_block_owns_its_transaction(two):
    owner, reader = two
    owner.execute("INSERT INTO t VALUES ('prior')")
    with sqlite_db.transaction(owner):
        pass
    assert id(owner) not in sqlite_db._TX_STATES
    owner.commit()
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('managed')")
        assert _values(reader) == ["prior"]
    assert _values(reader) == ["prior", "managed"]  # the managed block commits itself


def test_run_after_commit_in_foreign_transaction_is_dropped_with_warning(two, caplog: pytest.LogCaptureFixture):
    owner, _reader = two
    fired: list[str] = []
    owner.execute("INSERT INTO t VALUES ('prior')")
    with caplog.at_level(logging.WARNING, logger=sqlite_db._log.name), sqlite_db.transaction(owner):
        sqlite_db.run_after_commit(owner, lambda: fired.append("kept"))
    assert fired == []  # the owner's commit is unobservable, so nothing may fire early
    assert "foreign transaction" in caplog.text
    owner.commit()
    assert fired == []


def test_run_after_commit_in_rolled_back_foreign_block_is_discarded_silently(two, caplog: pytest.LogCaptureFixture):
    owner, _reader = two
    fired: list[str] = []
    owner.execute("INSERT INTO t VALUES ('prior')")
    with (
        caplog.at_level(logging.WARNING, logger=sqlite_db._log.name),
        pytest.raises(RuntimeError),
        sqlite_db.transaction(owner),
    ):
        sqlite_db.run_after_commit(owner, lambda: fired.append("x"))
        raise RuntimeError("fail")
    assert fired == []
    assert "foreign transaction" not in caplog.text


def test_managed_transaction_still_commits_and_fires_callbacks_after_commit(two):
    owner, reader = two
    seen: list[list[str]] = []
    with sqlite_db.transaction(owner):
        owner.execute("INSERT INTO t VALUES ('managed')")
        sqlite_db.run_after_commit(owner, lambda: seen.append(_values(reader)))
        assert seen == []
    assert seen == [["managed"]]


# ---------------------------------------------------------------------------
# close_connection(): deregister only provably closed handles
# ---------------------------------------------------------------------------


def test_close_connection_sqlite_error_keeps_handle_registered(registry, hooked_connect, caplog):
    conn = hooked_connect()
    conn.close_error = sqlite3.OperationalError("close failed")
    with caplog.at_level(logging.WARNING, logger=sqlite_db._log.name):
        assert sqlite_db.close_connection(conn) is False
    assert conn in sqlite_db._OPEN_CONNECTIONS
    assert sqlite_db.get_connection_stats()["total_closed"] == 0
    assert "stays registered" in caplog.text
    assert sqlite_db.begin_quiesce(timeout=0.2) is False  # fail closed: a handle may still be open
    assert sqlite_db._QUIESCED is False  # the block was lifted again
    conn.close_error = None
    assert sqlite_db.close_connection(conn) is True
    assert conn not in sqlite_db._OPEN_CONNECTIONS
    assert sqlite_db.get_connection_stats()["total_closed"] == 1
    assert sqlite_db.begin_quiesce(timeout=1.0) is True
    sqlite_db.end_quiesce()


def test_close_connection_unexpected_error_propagates_and_keeps_handle_registered(registry, hooked_connect):
    conn = hooked_connect()
    conn.close_error = RuntimeError("wrapper exploded")
    with pytest.raises(RuntimeError, match="wrapper exploded"):
        sqlite_db.close_connection(conn)
    assert conn in sqlite_db._OPEN_CONNECTIONS
    assert sqlite_db.get_connection_stats()["total_closed"] == 0
    assert sqlite_db.begin_quiesce(timeout=0.2) is False
    assert sqlite_db._QUIESCED is False


def test_close_connection_error_after_real_close_deregisters_once(registry, hooked_connect):
    conn = hooked_connect()
    conn.close_error = sqlite3.OperationalError("late failure")
    conn.close_error_after_close = True
    assert sqlite_db.close_connection(conn) is True  # provably closed despite the exception
    assert conn not in sqlite_db._OPEN_CONNECTIONS
    assert sqlite_db.get_connection_stats()["total_closed"] == 1
    assert sqlite_db.close_connection(conn) is True  # idempotent, no double count
    assert sqlite_db.get_connection_stats()["total_closed"] == 1


# ---------------------------------------------------------------------------
# close_all_connections(): sweep until the registry is empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [sqlite3.OperationalError("nope"), RuntimeError("wrapper exploded")])
def test_close_all_keeps_failed_handle_registered_and_counts_only_real_closes(registry, hooked_connect, error, caplog):
    good = hooked_connect()
    bad = hooked_connect()
    bad.close_error = error
    also_good = hooked_connect()
    with caplog.at_level(logging.WARNING, logger=sqlite_db._log.name):
        closed = sqlite_db.close_all_connections(settle_timeout=0)
    assert closed == 2
    assert {bad} == sqlite_db._OPEN_CONNECTIONS
    stats = sqlite_db.get_connection_stats()
    assert (stats["currently_open"], stats["total_closed"]) == (1, 2)
    assert sqlite_db._is_closed(good) and sqlite_db._is_closed(also_good)
    assert "close_failures=1" in caplog.text
    assert sqlite_db.begin_quiesce(timeout=0.2) is False
    bad.close_error = None
    assert sqlite_db.close_all_connections(settle_timeout=0) == 1
    assert sqlite_db.get_connection_stats()["total_closed"] == 3


def test_close_all_sweeps_connection_registered_between_snapshot_and_close(registry, hooked_connect):
    first = hooked_connect()
    late: list[sqlite3.Connection] = []

    def register_late() -> None:
        first.on_close = None
        late.append(hooked_connect())

    first.on_close = register_late
    assert sqlite_db.close_all_connections(settle_timeout=1.0) == 2
    assert len(late) == 1
    assert sqlite_db._is_closed(late[0])
    assert set() == sqlite_db._OPEN_CONNECTIONS
    assert sqlite_db.get_connection_stats()["total_closed"] == 2


def test_close_all_waits_for_pending_connect_and_closes_it(registry, monkeypatch: pytest.MonkeyPatch, db_path: Path):
    entered = threading.Event()
    proceed = threading.Event()
    real_connect = sqlite3.connect

    def blocking_connect(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=10)
        return real_connect(*args, **kwargs)

    results: list[sqlite3.Connection] = []
    monkeypatch.setattr(sqlite3, "connect", blocking_connect)
    worker = threading.Thread(target=lambda: results.append(sqlite_db.connect(db_path)), daemon=True)
    worker.start()
    assert entered.wait(timeout=10)
    assert sqlite_db.get_connection_stats()["pending_connects"] == 1
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    real_prune = sqlite_db._prune_closed_connections

    def prune_then_release() -> None:
        # Runs under the registry lock: the pending connect cannot finish before the sweep saw it.
        proceed.set()
        real_prune()

    monkeypatch.setattr(sqlite_db, "_prune_closed_connections", prune_then_release)
    assert sqlite_db.close_all_connections(settle_timeout=10.0) == 1
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert sqlite_db._is_closed(results[0])
    stats = sqlite_db.get_connection_stats()
    assert (stats["currently_open"], stats["pending_connects"], stats["total_closed"]) == (0, 0, 1)


def test_close_all_is_idempotent_and_does_not_change_global_state(registry, hooked_connect, db_path):
    hooked_connect()
    hooked_connect()
    assert sqlite_db.close_all_connections() == 2
    assert sqlite_db.close_all_connections() == 0
    assert sqlite_db.get_connection_stats()["total_closed"] == 2
    assert sqlite_db._QUIESCED is False
    again = sqlite_db.connect(db_path)  # no lasting block after shutdown (lifespan may run again)
    assert sqlite_db.close_connection(again) is True


# ---------------------------------------------------------------------------
# update_user_password(): discard the bootstrap credentials only after a real commit
# ---------------------------------------------------------------------------


@pytest.fixture
def user_db(registry, tmp_path: Path):
    """A connected DB with one admin user and a pending bootstrap credentials file.

    configure() points the module-global path at the file so stored_username()/discard() see it;
    it is restored after the test by the monkeypatch fixture that registry does not provide, so we
    reset it explicitly here.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite_db.connect(data_dir / "justup.db")
    sqlite_db.init_schema(conn)
    user_id = sqlite_db.create_user(conn, username="admin", password_hash="old-hash", is_admin=True)  # noqa: S106
    previous = bootstrap_credentials.configure(data_dir)
    cred_file = bootstrap_credentials.configured_path()
    assert cred_file is not None
    assert bootstrap_credentials.store("admin", "the-initial-password", cred_file) is True
    try:
        yield conn, user_id, cred_file
    finally:
        bootstrap_credentials.configure(previous.parent if previous is not None else None)
        sqlite_db.close_connection(conn)


def test_outer_rollback_after_password_change_keeps_bootstrap_credentials(user_db):
    conn, user_id, cred_file = user_db
    # update_user_password runs inside an outer transaction() that then fails and rolls back.
    with pytest.raises(RuntimeError, match="outer boom"), sqlite_db.transaction(conn):
        assert sqlite_db.update_user_password(conn, user_id, "new-hash") is True
        raise RuntimeError("outer boom")
    # The DB password reverted, so the credentials file must still be there and still load.
    assert sqlite_db.get_user_by_id(conn, user_id)["password_hash"] == "old-hash"  # noqa: S105
    assert cred_file.exists()
    assert bootstrap_credentials.load(cred_file) == ("admin", "the-initial-password")


def test_commit_after_password_change_discards_bootstrap_credentials(user_db):
    conn, user_id, cred_file = user_db
    # The managed transaction() update_user_password opens is itself the outermost: on its commit
    # the deferred discard fires.
    assert sqlite_db.update_user_password(conn, user_id, "new-hash") is True
    assert sqlite_db.get_user_by_id(conn, user_id)["password_hash"] == "new-hash"  # noqa: S105
    assert not cred_file.exists()
    assert bootstrap_credentials.load(cred_file) is None


def test_outer_commit_after_password_change_discards_bootstrap_credentials(user_db):
    conn, user_id, cred_file = user_db
    # Nested inside an outer managed transaction that commits: the discard must wait for the
    # OUTERMOST commit, not fire when the inner block releases its savepoint.
    with sqlite_db.transaction(conn):
        assert sqlite_db.update_user_password(conn, user_id, "new-hash") is True
        assert cred_file.exists()  # still present until the outermost commit succeeds
    assert sqlite_db.get_user_by_id(conn, user_id)["password_hash"] == "new-hash"  # noqa: S105
    assert not cred_file.exists()
