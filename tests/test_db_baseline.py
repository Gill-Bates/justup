#!/usr/bin/env python3
#
# tests/test_db_baseline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Current schema baseline: fresh creation, idempotent init, and safe legacy restamping."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.utils import bootstrap
from app.utils.constants import SettingKeys
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests._legacy_db import build_v1_database

EXPECTED_TABLES = {
    "alert_dispatch_outbox",
    "alerts",
    "auth_tokens",
    "dashboard_layouts",
    "groups",
    "ixp_exchanges",
    "ixp_prefixes",
    "ixp_snapshot",
    "login_attempts",
    "monitor_quality",
    "monitor_quality_latest",
    "notification_channel_state",
    "notification_dismissals",
    "notification_queue",
    "notification_reads",
    "notifications",
    "pop_alias_candidates",
    "pop_candidate_observations",
    "pop_candidate_reviews",
    "pop_import_runs",
    "pop_city_evidence",
    "pop_locations",
    "recipients",
    "report_jobs",
    "scheduler_runs",
    "settings",
    "target_alert_users",
    "target_locks",
    "target_network_ports",
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
    "idx_report_jobs_target",
    "idx_notification_dismissals_user",
    "idx_alerts_target_started",
    "idx_pop_observations_context",
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
        assert sqlite_db.SCHEMA_VERSION == 2
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables.discard("sqlite_sequence")
        assert tables == EXPECTED_TABLES
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


def test_exact_former_current_schema_is_migrated_to_the_current_version_without_losing_rows(db_path: Path):
    build_v1_database(db_path, stamp=sqlite_db.LEGACY_SCHEMA_VERSION)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO targets(name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) VALUES ('preserved', 60, 30, 1, 0, 0, 'p.example.org', "
        "'t', 't')"
    )
    raw.commit()
    raw.close()
    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.check_schema_compatibility(conn) == sqlite_db.LEGACY_SCHEMA_VERSION
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert conn.execute("SELECT name FROM targets").fetchone()[0] == "preserved"
        assert sqlite_db._layout(conn) == sqlite_db._expected_layout()  # noqa: SLF001
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        sqlite_db.close_connection(conn)


@pytest.mark.parametrize("old_version", [3, 9])
def test_intermediate_schema_versions_are_refused_without_writes(db_path: Path, old_version: int):
    _baseline_with(db_path, f"PRAGMA user_version = {old_version}")
    before = _snapshot(db_path.parent)
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_NO_MIGRATION_PATH
    finally:
        sqlite_db.close_connection(conn)
    assert _snapshot(db_path.parent) == before


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
        assert sqlite_db.get_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, None) is None  # default OFF applies
        sqlite_db.set_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, True)
    finally:
        sqlite_db.close_connection(conn)
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    try:
        assert sqlite_db.get_setting(conn, SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, None) is True
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
    ("build", "reason", "kind"),
    [
        pytest.param(_old_unversioned, "unversioned", sqlite_db.SCHEMA_UNVERSIONED, id="old-unversioned-tables"),
        pytest.param(
            lambda p: _baseline_with(p, f"PRAGMA user_version = {sqlite_db.LEGACY_SCHEMA_VERSION + 1}"),
            f"schema version is {sqlite_db.LEGACY_SCHEMA_VERSION + 1}",
            sqlite_db.SCHEMA_FUTURE,
            id="newer-user_version",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "PRAGMA user_version = 0"),
            "unversioned",
            sqlite_db.SCHEMA_UNVERSIONED,
            id="baseline-tables-without-version",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "ALTER TABLE targets ADD COLUMN group_name TEXT"),
            "table targets: unexpected column(s) group_name",
            sqlite_db.SCHEMA_DRIFT,
            id="old-lineage-column",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "ALTER TABLE alerts DROP COLUMN acknowledged_by"),
            "table alerts: missing column(s) acknowledged_by",
            sqlite_db.SCHEMA_DRIFT,
            id="missing-column",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "DROP TABLE notification_dismissals"),
            "table notification_dismissals: missing",
            sqlite_db.SCHEMA_DRIFT,
            id="missing-table",
        ),
        pytest.param(
            lambda p: _baseline_with(p, "DROP INDEX idx_targets_group_id"),
            "index idx_targets_group_id: missing",
            sqlite_db.SCHEMA_DRIFT,
            id="missing-index",
        ),
    ],
)
def test_incompatible_database_is_refused_without_modification(db_path: Path, build, reason: str, kind: str):
    build(db_path)
    before = _snapshot(db_path.parent)
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as excinfo:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    message = str(excinfo.value)
    assert excinfo.value.kind == kind
    assert reason in message
    assert str(db_path) in message
    assert "Back up" in message and "never deletes data" in message
    assert "v1.0.0 does not migrate" not in message  # the text is version-neutral
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


# ─── Baseline remains usable across restarts ──────────────────────────────


def test_repeated_init_is_idempotent_across_connections(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    target_id = _target(conn, "keep")
    conn.commit()
    schema = conn.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    sqlite_db.close_connection(conn)
    for _ in range(3):
        _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            == schema
        )
        assert conn.execute("SELECT id FROM targets").fetchone()[0] == target_id
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    finally:
        sqlite_db.close_connection(conn)


def test_corrupt_file_is_reported_as_such_and_left_untouched(tmp_path: Path):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"this is not a sqlite database " * 200)
    before = _sha256(path)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.check_schema_compatibility(conn)
    finally:
        conn.close()
    assert exc.value.kind == sqlite_db.SCHEMA_CORRUPT
    assert _sha256(path) == before


def _edit_schema(path: Path, table: str, old: str, new: str) -> None:
    """Rewrite one table's stored CREATE statement (writable_schema), simulating a drifted layout."""
    raw = sqlite3.connect(path)
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    assert old in sql
    raw.execute("PRAGMA writable_schema = ON")
    raw.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?", (sql.replace(old, new), table))
    raw.execute("PRAGMA writable_schema = OFF")
    raw.commit()
    raw.close()


@pytest.mark.parametrize(
    ("table", "old", "new", "expected"),
    [
        ("alerts", "target_name TEXT NOT NULL", "target_name INTEGER NOT NULL", "column target_name"),
        ("alerts", "target_name TEXT NOT NULL", "target_name TEXT", "column target_name"),
        (
            "alerts",
            "is_acknowledged INTEGER NOT NULL DEFAULT 0",
            "is_acknowledged INTEGER NOT NULL DEFAULT 1",
            "is_acknowledged",
        ),
        ("alerts", "DEFAULT 1 CHECK (active_incident", "DEFAULT 2 CHECK (active_incident", "column active_incident"),
        ("alerts", "CHECK (status IN ('open', 'resolved'))", "", "CHECK constraints differ"),
        ("alerts", "REFERENCES targets(id) ON DELETE CASCADE", "REFERENCES targets(id)", "foreign keys differ"),
    ],
)
def test_real_drift_is_still_detected_and_reported_without_row_contents(
    db_path: Path, table: str, old: str, new: str, expected: str
):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    sqlite_db.set_setting(conn, "marker", "super-secret-value-123")
    sqlite_db.close_connection(conn)
    _edit_schema(db_path, table, old, new)
    before = _snapshot(db_path.parent)
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    assert exc.value.kind == sqlite_db.SCHEMA_DRIFT
    assert f"table {table}" in str(exc.value) and expected in str(exc.value)
    assert "super-secret-value-123" not in str(exc.value)
    assert _snapshot(db_path.parent) == before


def test_wrong_index_definition_is_detected(db_path: Path):
    _init(db_path)
    _raw(
        db_path,
        "DROP INDEX idx_alerts_target_started",
        "CREATE INDEX idx_alerts_target_started ON alerts(started_at)",
    )
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError, match="index idx_alerts_target_started: differs"):
            sqlite_db.check_schema_compatibility(conn)
    finally:
        sqlite_db.close_connection(conn)


# ─── Recipient "enable for all targets" ───────────────────────────────────────


def test_enable_for_all_targets_assigns_removes_and_covers_new_targets(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        t1, t2 = _target(conn, "a"), _target(conn, "b")
        conn.commit()
        manual = sqlite_db.create_recipient(conn, name="manual", email="m@example.org")
        sqlite_db.set_target_recipients(conn, t1, [manual])
        rid = sqlite_db.create_recipient(conn, name="all", email="a@example.org")
        # Off -> off must not touch manual assignments; on is idempotent.
        sqlite_db.update_recipient(conn, manual, enable_for_all_targets=False)
        assert sqlite_db.get_target_recipient_ids(conn, t1) == [manual]
        sqlite_db.update_recipient(conn, rid, enable_for_all_targets=True)
        sqlite_db.update_recipient(conn, rid, enable_for_all_targets=True)
        assert [r["id"] for r in sqlite_db.get_recipient_targets(conn, rid)] == [t1, t2]
        t3 = _target(conn, "c")
        sqlite_db.assign_flagged_recipients(conn, t3)
        conn.commit()
        assert sqlite_db.get_target_recipient_ids(conn, t3) == [rid]
        sqlite_db.update_recipient(conn, rid, enable_for_all_targets=False)
        assert sqlite_db.get_recipient_targets(conn, rid) == []
        assert sqlite_db.get_recipient(conn, rid)["enable_for_all_targets"] == 0
        assert sqlite_db.get_target_recipient_ids(conn, t1) == [manual]
        created = sqlite_db.create_recipient(conn, name="new", email="n@example.org", enable_for_all_targets=True)
        assert len(sqlite_db.get_recipient_targets(conn, created)) == 3
    finally:
        sqlite_db.close_connection(conn)


# ─── Indexes for the dashboard alert summary ──────────────────────────

_SUMMARY_INDEXES = {"idx_alerts_status_ended", "idx_alerts_acknowledged_at"}


def _alert_indexes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='alerts'")}


def test_fresh_database_has_the_alert_summary_indexes(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert _alert_indexes(conn) >= _SUMMARY_INDEXES
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_alerts_acknowledged_at'").fetchone()[0]
        assert "WHERE is_acknowledged = 1" in sql
    finally:
        sqlite_db.close_connection(conn)
