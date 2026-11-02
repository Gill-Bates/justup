#!/usr/bin/env python3
#
# tests/test_db_baseline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""v1.0.0 schema baseline: fresh creation, idempotent init, and refusal of incompatible databases."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.utils import bootstrap
from app.utils.constants import SettingKeys
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

EXPECTED_TABLES = {
    "alert_dispatch_outbox",
    "alerts",
    "auth_tokens",
    "dashboard_layouts",
    "groups",
    "login_attempts",
    "monitor_quality",
    "monitor_quality_latest",
    "notification_dismissals",
    "notification_queue",
    "notification_reads",
    "notifications",
    "pop_alias_candidates",
    "pop_import_runs",
    "pop_locations",
    "recipients",
    "report_jobs",
    "scheduler_runs",
    "settings",
    "target_alert_users",
    "target_locks",
    "target_recipients",
    "target_status",
    "targets",
    "users",
}
EXPECTED_INDEXES = {
    "idx_alert_dispatch_outbox_pending",
    "idx_targets_group_id",
    "idx_targets_identity_signature",
    "idx_report_jobs_user",
    "idx_notification_dismissals_user",
    "idx_alerts_target_started",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sqlite" / "justup.db"


def _init(path: Path) -> None:
    conn = sqlite_db.connect(path)
    try:
        sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(directory: Path) -> dict[str, str]:
    return {p.name: _sha256(p) for p in sorted(directory.iterdir()) if p.is_file()}


def _raw(path: Path, *statements: str) -> None:
    """Build a database file with plain sqlite3 (rollback journal), bypassing the application."""
    raw = sqlite3.connect(path)
    for statement in statements:
        raw.execute(statement)
    raw.commit()
    raw.close()


def _target(conn: sqlite3.Connection, name: str = "t", group_id: int | None = None) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO targets(name, group_id, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) VALUES (?, ?, 60, 30, 1, 0, 0, ?, ?, ?)",
        (name, group_id, f"{name}.example.org", now, now),
    )
    return int(cur.lastrowid)


# ─── Fresh database ───────────────────────────────────────────────────────────


def test_new_file_is_initialised_as_baseline(db_path: Path):
    assert not db_path.exists()
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.SCHEMA_VERSION == 3  # v1.0.0 baseline + spooler resilience + alert outbox
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables >= EXPECTED_TABLES
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert indexes >= EXPECTED_INDEXES
        assert "group_name" not in {r[1] for r in conn.execute("PRAGMA table_info(targets)")}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        sqlite_db.close_connection(conn)


def test_empty_existing_file_counts_as_a_new_database(db_path: Path):
    db_path.parent.mkdir(parents=True)
    db_path.touch()
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    finally:
        sqlite_db.close_connection(conn)


def test_v2_migration_restores_only_the_currently_active_alert(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        down_target = _target(conn, "down")
        up_target = _target(conn, "up")
        conn.commit()
        started = utcnow()
        for alert_id, target_id, alert_started in (
            ("old-down", down_target, started - timedelta(minutes=5)),
            ("current-down", down_target, started),
            ("historical-up", up_target, started),
        ):
            sqlite_db.create_alert(
                conn,
                alert_id=alert_id,
                target_id=target_id,
                target_name="target",
                failed_service="http",
                started_at=alert_started,
                recipients_notified=[],
            )
        conn.execute(
            "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, 'down', ?)",
            (down_target, started),
        )
        conn.execute(
            "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, 'up', ?)",
            (up_target, started),
        )
        conn.commit()
        conn.execute("DROP TABLE alert_dispatch_outbox")
        conn.execute("ALTER TABLE alerts DROP COLUMN active_incident")
        conn.execute("PRAGMA user_version = 2")
        sqlite_db.init_schema(conn)

        active = {row["id"] for row in conn.execute("SELECT id FROM alerts WHERE active_incident = 1")}
        assert active == {"current-down"}
        pending = {row["alert_id"] for row in sqlite_db.list_pending_alert_dispatches(conn)}
        assert pending == {"current-down"}
        assert sqlite_db.get_schema_version(conn) == 3
    finally:
        sqlite_db.close_connection(conn)


def test_second_init_on_a_baseline_database_changes_nothing(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        before = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite!_%' ESCAPE '!' ORDER BY type, name"
        ).fetchall()
        sqlite_db.set_setting(conn, "purge_enabled", False)
        sqlite_db.init_schema(conn)
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite!_%' ESCAPE '!' ORDER BY type, name"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert sqlite_db.get_setting(conn, "purge_enabled", None) is False  # data untouched
    finally:
        sqlite_db.close_connection(conn)


# ─── Foreign keys ─────────────────────────────────────────────────────────────


def test_group_foreign_key_sets_null_on_delete_and_rejects_unknown_group(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        gid = sqlite_db.create_group(conn, name="web")
        tid = _target(conn, group_id=gid)
        conn.commit()
        assert conn.execute("SELECT g.name FROM targets t JOIN groups g ON g.id = t.group_id").fetchone()[0] == "web"
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
        assert conn.execute("SELECT group_id FROM targets WHERE id = ?", (tid,)).fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _target(conn, "orphan", group_id=9999)
        conn.rollback()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        sqlite_db.close_connection(conn)


# ─── Geo opt-out is stored durably ────────────────────────────────────────────


def test_geo_opt_out_survives_reinitialisation_and_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    db.parent.mkdir()
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    try:
        assert sqlite_db.get_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, None) is None  # default ON applies
        sqlite_db.set_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, False)
    finally:
        sqlite_db.close_connection(conn)
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    try:
        assert sqlite_db.get_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, None) is False
    finally:
        sqlite_db.close_connection(conn)


# ─── Incompatible databases are refused and left untouched ───────────────────


def _old_unversioned(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _raw(
        path,
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)",
        "CREATE TABLE targets (id INTEGER PRIMARY KEY, name TEXT, group_name TEXT)",
        "INSERT INTO targets(name, group_name) VALUES ('keep-me', 'web')",
    )


def _baseline_with(path: Path, *statements: str) -> None:
    _init(path)
    _raw(path, *statements)


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        pytest.param(_old_unversioned, "unversioned", id="old-unversioned-tables"),
        pytest.param(
            lambda p: _baseline_with(p, "PRAGMA user_version = 7"),
            "schema version is 7",
            id="newer-user_version",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "PRAGMA user_version = 0"),
            "unversioned",
            id="baseline-tables-without-version",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "ALTER TABLE targets ADD COLUMN group_name TEXT"),
            "table targets",
            id="old-lineage-column",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "DROP TABLE notification_dismissals"),
            "table notification_dismissals",
            id="missing-table",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "DROP INDEX idx_targets_group_id"),
            "index idx_targets_group_id",
            id="missing-index",
        ),
    ],
)
def test_incompatible_database_is_refused_without_modification(db_path: Path, build, reason: str):
    build(db_path)
    before = _snapshot(db_path.parent)
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as excinfo:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    message = str(excinfo.value)
    assert reason in message
    assert str(db_path) in message
    assert "Back up" in message and "never deletes data" in message
    assert _snapshot(db_path.parent) == before


def test_bootstrap_refuses_an_old_database_and_keeps_it_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    _old_unversioned(db)
    before = _snapshot(db.parent)
    with pytest.raises(sqlite_db.IncompatibleSchemaError):
        bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    assert _snapshot(db.parent) == before


def _downgrade_phone_index(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX idx_recipients_phone")
    conn.execute("CREATE INDEX idx_recipients_phone ON recipients(phone)")
    conn.commit()


def _phone_index_sql(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_recipients_phone'").fetchone()[0]


def test_init_upgrades_non_unique_phone_index_when_data_is_clean(db_path: Path) -> None:
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    _downgrade_phone_index(conn)
    sqlite_db.init_schema(conn)
    assert "UNIQUE" in _phone_index_sql(conn)
    sqlite_db.close_connection(conn)


def test_init_keeps_existing_duplicate_phones_and_old_index(db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    _downgrade_phone_index(conn)
    for name in ("A", "B"):
        conn.execute(
            "INSERT INTO recipients(name, phone, created_at, updated_at) VALUES (?, '+4915112345678', 't', 't')",
            (name,),
        )
    conn.commit()
    with caplog.at_level("WARNING"):
        sqlite_db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 2
    assert "UNIQUE" not in _phone_index_sql(conn)
    assert "phone number" in caplog.text and "new ones are rejected" in caplog.text
    sqlite_db.close_connection(conn)


def test_legacy_duplicates_block_new_duplicates_but_not_unrelated_edits(db_path: Path) -> None:
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    _downgrade_phone_index(conn)
    a = sqlite_db.create_recipient(conn, name="A", phone="+4915112345678")
    conn.execute("INSERT INTO recipients(name, phone, created_at, updated_at) VALUES ('B', '+4915112345678', 't', 't')")
    conn.commit()
    c = sqlite_db.create_recipient(conn, name="C", phone="+4915100000001")

    with pytest.raises(sqlite_db.DuplicatePhoneError):
        sqlite_db.create_recipient(conn, name="D", phone="+4915112345678")
    with pytest.raises(sqlite_db.DuplicatePhoneError):
        sqlite_db.update_recipient(conn, c, phone="+4915112345678")
    # Unchanged number and other fields stay editable; nothing was deleted.
    sqlite_db.update_recipient(conn, a, name="A2", phone="+4915112345678")
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 3
    sqlite_db.close_connection(conn)
