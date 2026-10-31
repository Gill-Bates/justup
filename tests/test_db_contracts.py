#!/usr/bin/env python3
#
# tests/test_db_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: S106
#!/usr/bin/env python3
#
# tests/test_db_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite DAL contracts: scheduler bookkeeping durability and token lookup write behaviour."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


def _fresh(path: Path) -> sqlite3.Connection:
    conn = sqlite_db.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")  # lock rows are exercised without target fixtures
    return conn


def test_heartbeat_is_durable_after_connection_close(db_path: Path):
    c = _fresh(db_path)
    sqlite_db.register_scheduler_run(c, "run-a")
    before = c.execute("SELECT heartbeat_at FROM scheduler_runs WHERE run_id='run-a'").fetchone()[0]
    sqlite_db.update_scheduler_heartbeat(c, "run-a")
    sqlite_db.close_connection(c)
    c2 = _fresh(db_path)
    after = c2.execute("SELECT heartbeat_at FROM scheduler_runs WHERE run_id='run-a'").fetchone()[0]
    assert after >= before
    sqlite_db.close_connection(c2)


def test_released_lock_is_immediately_available_to_other_run(db_path: Path):
    c = _fresh(db_path)
    assert sqlite_db.try_lock_target(c, 1, "run-a", 300)
    sqlite_db.release_target_lock(c, 1, "run-a")
    sqlite_db.close_connection(c)
    c2 = _fresh(db_path)
    assert sqlite_db.try_lock_target(c2, 1, "run-b", 300)
    sqlite_db.close_connection(c2)


def test_lock_held_by_other_run_blocks_and_foreign_release_is_noop(db_path: Path):
    c = _fresh(db_path)
    assert sqlite_db.try_lock_target(c, 1, "run-a", 300)
    sqlite_db.release_target_lock(c, 1, "run-b")
    assert not sqlite_db.try_lock_target(c, 1, "run-b", 300)
    sqlite_db.close_connection(c)


def _user_with_token(c: sqlite3.Connection, *, expires_in: timedelta, max_in: timedelta) -> str:
    uid = sqlite_db.create_user(c, username="alice", password_hash="x", is_admin=False)
    now = utcnow()
    sqlite_db.issue_token(c, user_id=uid, token_hash="h1", expires_at=now + expires_in, max_expires_at=now + max_in)
    return "h1"


def test_token_lookup_skips_write_when_renewal_is_negligible(db_path: Path):
    c = _fresh(db_path)
    h = _user_with_token(c, expires_in=timedelta(hours=6), max_in=timedelta(hours=24))
    before = c.execute("SELECT expires_at FROM auth_tokens").fetchone()[0]
    before_changes = c.total_changes
    assert sqlite_db.get_user_for_token(c, token_hash=h) is not None
    assert c.total_changes == before_changes
    assert c.execute("SELECT expires_at FROM auth_tokens").fetchone()[0] == before
    sqlite_db.close_connection(c)


def test_token_lookup_extends_but_never_beyond_max_expiry(db_path: Path):
    c = _fresh(db_path)
    h = _user_with_token(c, expires_in=timedelta(minutes=30), max_in=timedelta(hours=2))
    row = sqlite_db.get_user_for_token(c, token_hash=h)
    assert row is not None
    new_exp = c.execute("SELECT expires_at FROM auth_tokens").fetchone()[0]
    assert new_exp > utcnow() + timedelta(minutes=90)
    assert new_exp <= row["max_expires_at"]
    sqlite_db.close_connection(c)


def test_expired_revoked_or_inactive_tokens_are_rejected_and_cleanup_removes_expired(db_path: Path):
    c = _fresh(db_path)
    h = _user_with_token(c, expires_in=timedelta(seconds=-5), max_in=timedelta(hours=1))
    assert sqlite_db.get_user_for_token(c, token_hash=h) is None
    assert c.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 1  # lookup no longer deletes
    sqlite_db.cleanup_expired_tokens(c)
    assert c.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0

    uid = sqlite_db.create_user(c, username="bob", password_hash="x", is_admin=False)
    now = utcnow()
    sqlite_db.issue_token(
        c, user_id=uid, token_hash="h2", expires_at=now + timedelta(hours=1), max_expires_at=now + timedelta(hours=2)
    )
    assert sqlite_db.get_user_for_token(c, token_hash="h2") is not None
    assert sqlite_db.revoke_token(c, token_hash="h2")
    assert sqlite_db.get_user_for_token(c, token_hash="h2") is None
    sqlite_db.close_connection(c)


# ─── Transactions: nesting, savepoints, post-commit cache invalidation ───────


def _target_values(name: str = "t") -> dict:
    return {"name": name, "host": "example.org", "interval_seconds": 60, "retention_days": 30}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - test-controlled name


def test_inner_helpers_do_not_commit_the_outer_transaction(db_path: Path):
    conn = sqlite_db.connect(db_path)
    other = sqlite_db.connect(db_path)
    with pytest.raises(RuntimeError, match="boom"), sqlite_db.transaction(conn):
        tid = sqlite_db.create_target(conn, _target_values())  # manage_transaction=True: nested
        sqlite_db.set_target_recipients(conn, tid, [])
        assert _count(other, "targets") == 0  # nothing visible before the OUTER commit
        raise RuntimeError("boom")
    assert _count(conn, "targets") == 0  # the outer rollback undid the inner "commit"
    with sqlite_db.transaction(conn):
        sqlite_db.create_target(conn, _target_values("kept"))
    assert _count(other, "targets") == 1
    sqlite_db.close_connection(conn)
    sqlite_db.close_connection(other)


def test_inner_failure_rolls_back_only_the_savepoint(db_path: Path):
    conn = sqlite_db.connect(db_path)
    with sqlite_db.transaction(conn):
        sqlite_db.create_target(conn, _target_values("outer"))
        with pytest.raises(sqlite3.IntegrityError), sqlite_db.transaction(conn):
            sqlite_db.create_target(conn, _target_values("inner"))
            conn.execute("INSERT INTO target_recipients(target_id, recipient_id) VALUES (9999, 9999)")
        assert [r["name"] for r in conn.execute("SELECT name FROM targets")] == ["outer"]
    assert [r["name"] for r in conn.execute("SELECT name FROM targets")] == ["outer"]
    assert not conn.in_transaction
    sqlite_db.close_connection(conn)


def test_immediate_transaction_takes_the_write_lock_up_front(db_path: Path):
    first = sqlite_db.connect(db_path)
    second = sqlite_db.connect(db_path)
    second.execute("PRAGMA busy_timeout=50")
    with sqlite_db.immediate_transaction(first), pytest.raises(sqlite3.OperationalError, match="locked"):
        second.execute("BEGIN IMMEDIATE")
    second.execute("BEGIN IMMEDIATE")  # free again after the commit
    second.rollback()
    sqlite_db.close_connection(first)
    sqlite_db.close_connection(second)


def test_settings_cache_is_invalidated_only_after_commit(db_path: Path):
    from app.utils import settings_cache

    conn = sqlite_db.connect(db_path)
    sqlite_db.set_setting(conn, "demo_key", "v1")
    assert settings_cache.get_setting_cached(conn, "demo_key", None) == "v1"
    with pytest.raises(RuntimeError), sqlite_db.transaction(conn):
        sqlite_db.set_setting(conn, "demo_key", "v2")
        sqlite_db.delete_setting(conn, "demo_key")
        raise RuntimeError("rollback")
    assert settings_cache.get_setting_cached(conn, "demo_key", None) == "v1"  # cache untouched, DB unchanged
    assert sqlite_db.get_setting(conn, "demo_key") == "v1"
    with sqlite_db.transaction(conn):
        sqlite_db.set_setting(conn, "demo_key", "v3")
    assert settings_cache.get_setting_cached(conn, "demo_key", None) == "v3"
    sqlite_db.close_connection(conn)


def test_delete_setting_invalidates_the_cache(db_path: Path):
    from app.utils import settings_cache

    conn = sqlite_db.connect(db_path)
    sqlite_db.set_setting(conn, "demo_key", "v1")
    assert settings_cache.get_setting_cached(conn, "demo_key", "default") == "v1"
    sqlite_db.delete_setting(conn, "demo_key")
    assert settings_cache.get_setting_cached(conn, "demo_key", "default") == "default"
    sqlite_db.close_connection(conn)


# ─── PRAGMAs / WAL ────────────────────────────────────────────────────────────


def test_pragmas_on_fresh_database(db_path: Path):
    conn = sqlite_db.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # persisted by init_schema
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == sqlite_db.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    sqlite_db.close_connection(conn)


def test_connect_does_not_switch_the_journal_mode(tmp_path: Path):
    path = tmp_path / "plain.db"
    conn = sqlite_db.connect(path)
    conn.execute("CREATE TABLE t(a)")
    conn.commit()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # WAL only via ensure_wal_mode
    sqlite_db.ensure_wal_mode(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    sqlite_db.close_connection(conn)


def test_pragma_failure_leaves_no_open_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    closed: list[bool] = []
    real_connect = sqlite3.connect

    class Exploding:
        def __init__(self, *args, **kwargs):
            self._conn = real_connect(*args, **kwargs)
            self.row_factory = None

        def execute(self, *_args):
            raise sqlite3.OperationalError("simulated PRAGMA failure")

        def close(self):
            closed.append(True)
            self._conn.close()

    monkeypatch.setattr(sqlite_db.sqlite3, "connect", Exploding)
    before = sqlite_db.get_connection_stats()["currently_open"]
    with pytest.raises(sqlite3.OperationalError):
        sqlite_db.connect(tmp_path / "x.db")
    assert closed == [True]
    assert sqlite_db.get_connection_stats()["currently_open"] == before


def test_parallel_readers_and_writers_do_not_hit_database_is_locked(db_path: Path):
    import threading

    errors: list[Exception] = []
    stop = threading.Event()

    def writer(n: int):
        conn = sqlite_db.connect(db_path)
        try:
            for i in range(40):
                with sqlite_db.immediate_transaction(conn):
                    sqlite_db.set_setting(conn, f"k{n}_{i}", i, auto_commit=False)
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)
        finally:
            sqlite_db.close_connection(conn)

    def reader():
        conn = sqlite_db.connect(db_path)
        try:
            while not stop.is_set():
                conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)
        finally:
            sqlite_db.close_connection(conn)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert not errors
    check = sqlite_db.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'k%'").fetchone()[0] == 120
    sqlite_db.close_connection(check)


# ─── Schema versioning ────────────────────────────────────────────────────────


def test_fresh_database_is_stamped_with_the_current_schema_version(db_path: Path):
    conn = sqlite_db.connect(db_path)
    assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    sqlite_db.init_schema(conn)  # idempotent
    assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    sqlite_db.close_connection(conn)


def test_target_identity_constraint_rejects_exact_checks_but_allows_other_service(db_path: Path):
    conn = sqlite_db.connect(db_path)
    first = {**_target_values("ping"), "enable_ping": True}
    first_id = sqlite_db.create_target(conn, first)
    signature = conn.execute("SELECT identity_signature FROM targets WHERE id = ?", (first_id,)).fetchone()[0]
    assert signature is not None
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        conn.execute(
            "INSERT INTO targets(name, interval_seconds, retention_days, enable_ping, enable_http_check, "
            "enable_cert_expiration, identity_signature, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("duplicate", 60, 30, 1, 0, 0, signature, utcnow(), utcnow()),
        )
    conn.rollback()
    other = {**_target_values("http"), "enable_http_check": True, "http_url": "https://example.org/"}
    other_id = sqlite_db.create_target(conn, other)
    assert other_id != first_id
    sqlite_db.update_target(conn, target_id=other_id, patch={"http_url": "https://other.example.org/"})
    assert conn.execute("SELECT identity_signature FROM targets WHERE id = ?", (other_id,)).fetchone()[0] != signature
    sqlite_db.close_connection(conn)


def test_target_identity_migration_backfills_old_schema_without_data_loss(db_path: Path):
    conn = sqlite_db.connect(db_path)
    ping = {**_target_values("ping"), "enable_ping": True}
    http = {**_target_values("http"), "enable_http_check": True, "http_url": "https://example.org/"}
    ids = [sqlite_db.create_target(conn, values) for values in (ping, http)]
    conn.execute("DROP INDEX idx_targets_identity_signature")
    conn.execute("ALTER TABLE targets DROP COLUMN identity_signature")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    sqlite_db.init_schema(conn)
    assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    assert [row[0] for row in conn.execute("SELECT id FROM targets ORDER BY id")] == ids
    assert all(row[0] for row in conn.execute("SELECT identity_signature FROM targets"))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    sqlite_db.close_connection(conn)


def test_target_identity_migration_refuses_existing_duplicates_without_deleting(db_path: Path):
    conn = sqlite_db.connect(db_path)
    first = {**_target_values("ping"), "enable_ping": True}
    first_id = sqlite_db.create_target(conn, first)
    conn.execute("DROP INDEX idx_targets_identity_signature")
    conn.execute("ALTER TABLE targets DROP COLUMN identity_signature")
    conn.execute(
        "INSERT INTO targets(name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("duplicate", 60, 30, 1, 0, 0, "example.org", utcnow(), utcnow()),
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    with pytest.raises(RuntimeError, match="identical monitoring identities"):
        sqlite_db.init_schema(conn)
    assert sqlite_db.get_schema_version(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 2
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT id FROM targets ORDER BY id").fetchone()[0] == first_id
    sqlite_db.close_connection(conn)


def test_migrations_apply_once_in_order_and_are_idempotent(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    v1, v2 = sqlite_db.SCHEMA_VERSION + 1, sqlite_db.SCHEMA_VERSION + 2

    def add_column(conn):
        calls.append(v1)
        conn.execute("ALTER TABLE groups ADD COLUMN color TEXT")

    def add_index(conn):
        calls.append(v2)
        conn.execute("CREATE INDEX idx_groups_color ON groups(color)")

    monkeypatch.setattr(
        sqlite_db,
        "MIGRATIONS",
        (sqlite_db.Migration(v2, "index", add_index), sqlite_db.Migration(v1, "column", add_column)),
    )
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    sqlite_db.init_schema(conn)
    assert calls == [v1, v2]
    assert sqlite_db.get_schema_version(conn) == v2
    sqlite_db.close_connection(conn)


def test_failing_migration_rolls_back_schema_and_version(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(conn):
        conn.execute("ALTER TABLE groups ADD COLUMN half_done TEXT")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(sqlite_db, "MIGRATIONS", (sqlite_db.Migration(sqlite_db.SCHEMA_VERSION + 1, "broken", broken),))
    conn = sqlite_db.connect(db_path)
    with pytest.raises(RuntimeError, match="migration failed"):
        sqlite_db.init_schema(conn)
    assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    assert "half_done" not in [r["name"] for r in conn.execute("PRAGMA table_info(groups)")]
    sqlite_db.close_connection(conn)


def test_database_from_a_newer_release_is_refused(db_path: Path):
    conn = sqlite_db.connect(db_path)
    conn.execute(f"PRAGMA user_version = {sqlite_db.SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="newer than this application"):
        sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)


# ─── Shutdown ordering ────────────────────────────────────────────────────────


def test_lifespan_stops_owners_before_closing_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.monitor.scheduler import MonitorScheduler
    from app.services import pdf_worker
    from app.utils import bootstrap

    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    order: list[str] = []

    real_stop = MonitorScheduler.stop

    async def stop(self):
        order.append("scheduler.stop")
        await real_stop(self)

    real_pdf = pdf_worker.shutdown_pdf_worker
    real_close = sqlite_db.close_all_connections
    monkeypatch.setattr(MonitorScheduler, "stop", stop)
    monkeypatch.setattr(pdf_worker, "shutdown_pdf_worker", lambda: (order.append("pdf.shutdown"), real_pdf())[1])
    monkeypatch.setattr(sqlite_db, "close_all_connections", lambda: (order.append("db.close_all"), real_close())[1])

    app = create_app()
    with TestClient(app):
        assert app.state.shutting_down is False
    assert app.state.shutting_down is True
    assert order == ["scheduler.stop", "pdf.shutdown", "db.close_all"]
