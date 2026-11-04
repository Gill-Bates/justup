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
        assert sqlite_db.SCHEMA_VERSION == 9  # baseline plus schema migrations through the alert summary indexes
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
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    finally:
        sqlite_db.close_connection(conn)


def test_v3_migration_preserves_layout_and_classifies_old_heights_as_automatic(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES ('layout-user', 'hash', ?)", (utcnow(),)
        )
        user_id = conn.execute("SELECT id FROM users WHERE username = 'layout-user'").fetchone()[0]
        conn.execute("DROP TABLE dashboard_layouts")
        conn.execute(
            """
            CREATE TABLE dashboard_layouts (
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
        conn.execute(
            "INSERT INTO dashboard_layouts (user_id, widget_id, position, x, y, w, h, visible) "
            "VALUES (?, 'downtime', 0, 3, 12, 6, 8, 1)",
            (user_id,),
        )
        conn.execute(
            "CREATE TABLE layout_notes (user_id INTEGER, widget_id TEXT, note TEXT, "
            "FOREIGN KEY(user_id, widget_id) REFERENCES dashboard_layouts(user_id, widget_id) ON DELETE CASCADE)"
        )
        conn.execute("INSERT INTO layout_notes VALUES (?, 'downtime', 'keep')", (user_id,))
        conn.execute("CREATE INDEX idx_layout_position ON dashboard_layouts(position)")
        conn.execute("CREATE TRIGGER layout_touch AFTER UPDATE ON dashboard_layouts BEGIN SELECT 1; END")
        conn.execute("CREATE VIEW layout_view AS SELECT user_id, widget_id FROM dashboard_layouts")
        conn.execute("CREATE VIEW layout_view_chain AS SELECT widget_id FROM layout_view")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        sqlite_db.init_schema(conn)
        widget = conn.execute(
            "SELECT x, y, w, h, visible, layout_version, height_mode FROM dashboard_layouts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        assert tuple(widget) == (3, 12, 6, 8, 1, 2, "auto")
        assert conn.execute("SELECT note FROM layout_notes").fetchone()[0] == "keep"
        assert conn.execute("SELECT widget_id FROM layout_view_chain").fetchone()[0] == "downtime"
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'idx_layout_position'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'layout_touch'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        sqlite_db.close_connection(conn)


def test_v3_layout_migration_failure_rolls_back_dependent_data(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        conn.execute("DROP TABLE dashboard_layouts")
        conn.execute(
            "CREATE TABLE dashboard_layouts (user_id INTEGER, widget_id TEXT, position INTEGER, x INTEGER, "
            "y INTEGER, w INTEGER, h INTEGER, visible INTEGER, layout_version INTEGER, "
            "PRIMARY KEY (user_id, widget_id))"
        )
        conn.execute(
            "CREATE TABLE layout_notes (user_id INTEGER, widget_id TEXT, "
            "FOREIGN KEY(user_id, widget_id) REFERENCES dashboard_layouts(user_id, widget_id) ON DELETE CASCADE)"
        )
        conn.execute("INSERT INTO dashboard_layouts VALUES (1, 'quality', 0, 0, 0, 2, 2, 1, 1)")
        conn.execute("INSERT INTO layout_notes VALUES (1, 'quality')")
        conn.execute("CREATE VIEW layout_view AS SELECT widget_id FROM dashboard_layouts")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        before = [
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        from app.db.sqlite import migrations

        original = next(m.apply for m in migrations.MIGRATIONS if m.version == 4)

        def fail_after_rebuild(connection: sqlite3.Connection) -> None:
            original(connection)
            raise sqlite3.OperationalError("injected failure")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            tuple(
                migrations.Migration(m.version, m.description, fail_after_rebuild) if m.version == 4 else m
                for m in migrations.MIGRATIONS
            ),
        )
        with pytest.raises(sqlite_db.IncompatibleSchemaError, match="injected failure"):
            migrations.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == 3
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT widget_id FROM layout_notes").fetchone()[0] == "quality"
        assert conn.execute("SELECT widget_id FROM layout_view").fetchone()[0] == "quality"
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ] == before
    finally:
        sqlite_db.close_connection(conn)


def test_v3_layout_migration_rejects_foreign_key_violations_before_commit(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        conn.execute("DROP TABLE dashboard_layouts")
        conn.execute(
            "CREATE TABLE dashboard_layouts (user_id INTEGER, widget_id TEXT, position INTEGER, x INTEGER, "
            "y INTEGER, w INTEGER, h INTEGER, visible INTEGER, layout_version INTEGER, "
            "PRIMARY KEY (user_id, widget_id))"
        )
        conn.execute(
            "CREATE TABLE layout_notes (user_id INTEGER, widget_id TEXT, "
            "FOREIGN KEY(user_id, widget_id) REFERENCES dashboard_layouts(user_id, widget_id) ON DELETE CASCADE)"
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO layout_notes VALUES (999, 'missing')")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite_db.IncompatibleSchemaError, match="Foreign-key violations") as exc:
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        assert sqlite_db.get_schema_version(conn) == 3
        assert conn.execute("SELECT widget_id FROM layout_notes").fetchone()[0] == "missing"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
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
    ("build", "reason", "kind"),
    [
        pytest.param(_old_unversioned, "unversioned", sqlite_db.SCHEMA_UNVERSIONED, id="old-unversioned-tables"),
        pytest.param(
            lambda p: _baseline_with(p, f"PRAGMA user_version = {sqlite_db.SCHEMA_VERSION + 1}"),
            f"schema version is {sqlite_db.SCHEMA_VERSION + 1}",
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


# ─── Migrated databases must stay usable across restarts ─────────────────────


def _alert_rows(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT id, target_id, status, active_incident FROM alerts ORDER BY id")]


def _build_v2_database(path: Path) -> dict[str, object]:
    """Create a realistic schema-v2 file (targets, open/resolved alert history) with plain DDL steps.

    v2 is the current baseline without the v3 objects (alerts.active_incident, alert_dispatch_outbox).
    """
    _init(path)
    conn = sqlite_db.connect(path)
    try:
        down_id = _target(conn, "down")
        up_id = _target(conn, "up")
        conn.commit()
        started = utcnow()
        for alert_id, target_id, offset in (
            ("a-old-resolved", down_id, 30),
            ("a-current-open", down_id, 0),
            ("a-up-history", up_id, 10),
        ):
            sqlite_db.create_alert(
                conn,
                alert_id=alert_id,
                target_id=target_id,
                target_name=f"t{target_id}",
                failed_service="http",
                started_at=started - timedelta(minutes=offset),
                recipients_notified=[],
            )
        sqlite_db.resolve_alert(conn, "a-old-resolved", started - timedelta(minutes=20))
        sqlite_db.resolve_alert(conn, "a-up-history", started - timedelta(minutes=5))
        for target_id, status in ((down_id, "down"), (up_id, "up")):
            conn.execute(
                "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, ?, ?)",
                (target_id, status, started),
            )
        conn.commit()
        conn.execute("DROP TABLE alert_dispatch_outbox")
        conn.execute("ALTER TABLE alerts DROP COLUMN active_incident")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)
    return {"down_id": down_id, "up_id": up_id}


def test_migrated_database_is_accepted_on_restart(db_path: Path):
    ids = _build_v2_database(db_path)
    _init(db_path)  # first start on the new release: v2 -> current
    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        # Upgraded tables keep additive columns at the end; that is a valid current layout.
        assert sqlite_db.check_schema_compatibility(conn) == sqlite_db.SCHEMA_VERSION
        sqlite_db.init_schema(conn)
        assert _alert_rows(conn) == [
            ("a-current-open", ids["down_id"], "open", 1),
            ("a-old-resolved", ids["down_id"], "resolved", 0),
            ("a-up-history", ids["up_id"], "resolved", 0),
        ]
    finally:
        sqlite_db.close_connection(conn)
    _init(db_path)  # second restart with a fresh connection


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}  # noqa: S608


def _structure(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {r[1]: (r[2], r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _schema_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def test_upgrade_preserves_data_ids_and_integrity_across_restarts(db_path: Path, caplog: pytest.LogCaptureFixture):
    ids = _build_v2_database(db_path)
    raw = sqlite3.connect(db_path)
    before_counts = _table_counts(raw)
    before_targets = [tuple(r) for r in raw.execute("SELECT id, name FROM targets ORDER BY id")]
    raw.close()

    with caplog.at_level("INFO", logger="app.db.sqlite"):
        _init(db_path)
        _init(db_path)
        _init(db_path)
    migration_logs = [r.getMessage() for r in caplog.records if "Schema migration" in r.getMessage()]
    assert migration_logs == [
        "Schema migration 2 -> 3 (durable alert dispatch outbox): ok",
        "Schema migration 3 -> 4 (dashboard widget automatic and manual height contract): ok",
        "Schema migration 4 -> 5 (recipients.enable_for_all_targets flag): ok",
        "Schema migration 5 -> 6 (bounded PoP candidate evidence and explicit review metadata): ok",
        "Schema migration 6 -> 7 (UDP configuration and protocol-specific port metadata): ok",
        "Schema migration 7 -> 8 (raw port state, service identity and latency per network port): ok",
        "Schema migration 8 -> 9 (alert summary range indexes and planner statistics): ok",
    ]

    conn = sqlite_db.connect(db_path)
    try:
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(r) for r in conn.execute("SELECT id, name FROM targets ORDER BY id")] == before_targets
        assert {i for (i, _n) in before_targets} == {ids["down_id"], ids["up_id"]}
        after_counts = _table_counts(conn)
        assert after_counts == {**before_counts, "alert_dispatch_outbox": after_counts["alert_dispatch_outbox"]}
        assert after_counts["alert_dispatch_outbox"] == 1  # the one currently active incident
    finally:
        sqlite_db.close_connection(conn)


def _exercise_alert_lifecycle(path: Path, label: str) -> dict[str, object]:
    """Create, deactivate and resolve alerts on *path* and report what the application observes."""
    conn = sqlite_db.connect(path)
    try:
        assert sqlite_db.check_schema_compatibility(conn) == sqlite_db.SCHEMA_VERSION
        target = _target(conn, f"new-{label}")
        conn.commit()
        now = utcnow()
        for alert_id, service in (
            (f"open-{label}", "http"),
            (f"deactivated-{label}", "ping"),
            (f"done-{label}", "tcp"),
        ):
            sqlite_db.create_alert(
                conn,
                alert_id=alert_id,
                target_id=target,
                target_name="n",
                failed_service=service,
                started_at=now,
                recipients_notified=[],
                dispatch_outbox=True,
            )
        created = {
            r["id"]: r["active_incident"]
            for r in conn.execute("SELECT id, active_incident FROM alerts WHERE target_id = ?", (target,))
        }
        sqlite_db.deactivate_alert(conn, f"deactivated-{label}")
        sqlite_db.resolve_alert(conn, f"done-{label}", now + timedelta(minutes=1), dispatch_outbox=True)
        final = {
            r["id"].removesuffix(f"-{label}"): (r["status"], r["active_incident"])
            for r in conn.execute("SELECT id, status, active_incident FROM alerts WHERE target_id = ?", (target,))
        }
        active = [
            r["id"].removesuffix(f"-{label}") for r in sqlite_db.list_active_alerts(conn) if r["target_id"] == target
        ]
        outbox = sorted(
            (r["event_type"], r["failed_service"])
            for r in conn.execute("SELECT * FROM alert_dispatch_outbox WHERE target_id = ?", (target,))
        )
        return {
            "created": {k.removesuffix(f"-{label}"): v for k, v in created.items()},
            "final": final,
            "active": active,
            "outbox": outbox,
        }
    finally:
        sqlite_db.close_connection(conn)


def test_fresh_and_migrated_databases_have_the_same_structure_and_behaviour(tmp_path: Path):
    fresh_path, migrated_path = tmp_path / "fresh" / "justup.db", tmp_path / "migrated" / "justup.db"
    _init(fresh_path)
    _build_v2_database(migrated_path)
    _init(migrated_path)
    structures = []
    for path in (fresh_path, migrated_path):
        conn = sqlite_db.connect(path)
        try:
            structures.append({t: _structure(conn, t) for t in ("alerts", "notification_queue", "targets")})
        finally:
            sqlite_db.close_connection(conn)
    assert structures[0] == structures[1]  # same columns, types, NOT NULL and keys (order excluded)
    fresh, migrated = _exercise_alert_lifecycle(fresh_path, "f"), _exercise_alert_lifecycle(migrated_path, "m")
    assert fresh == migrated
    assert fresh["created"] == {"open": 1, "deactivated": 1, "done": 1}
    assert fresh["final"] == {"open": ("open", 1), "deactivated": ("open", 0), "done": ("resolved", 0)}
    assert fresh["active"] == ["open"]


def test_repeated_init_is_idempotent_across_connections(db_path: Path):
    _build_v2_database(db_path)
    _init(db_path)
    first = _schema_rows(db_path)
    conn = sqlite_db.connect(db_path)
    counts = _table_counts(conn)
    sqlite_db.close_connection(conn)
    for _ in range(3):
        _init(db_path)
    assert _schema_rows(db_path) == first
    conn = sqlite_db.connect(db_path)
    try:
        assert _table_counts(conn) == counts
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    finally:
        sqlite_db.close_connection(conn)


_REAL_MIGRATION_3 = next(m for m in sqlite_db.MIGRATIONS if m.version == 3)  # captured before any patching


def _failing_migration_3(conn: sqlite3.Connection) -> None:
    _REAL_MIGRATION_3.apply(conn)  # real work first, then fail mid-way
    raise sqlite3.OperationalError("injected failure")


def test_failed_migration_rolls_back_schema_data_and_version(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _build_v2_database(db_path)
    before_schema = _schema_rows(db_path)
    raw = sqlite3.connect(db_path)
    before_counts = _table_counts(raw)
    raw.close()
    patched = tuple(
        sqlite_db.Migration(m.version, m.description, _failing_migration_3) if m.version == 3 else m
        for m in sqlite_db.MIGRATIONS
    )
    monkeypatch.setattr(sqlite_db, "MIGRATIONS", patched)
    conn = sqlite_db.connect(db_path)
    try:
        with (
            caplog.at_level("ERROR", logger="app.db.sqlite"),
            pytest.raises(sqlite_db.IncompatibleSchemaError) as exc,
        ):
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        assert "2 -> 3" in str(exc.value) and "injected failure" in str(exc.value)
        assert "Schema migration 2 -> 3" in caplog.text
        assert sqlite_db.get_schema_version(conn) == 2
    finally:
        sqlite_db.close_connection(conn)
    assert _schema_rows(db_path) == before_schema
    raw = sqlite3.connect(db_path)
    assert _table_counts(raw) == before_counts
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()
    monkeypatch.undo()
    _init(db_path)  # the same file upgrades once the cause is gone
    conn = sqlite_db.connect(db_path)
    assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    sqlite_db.close_connection(conn)


def test_migration_that_produces_a_drifted_layout_is_rolled_back(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    _build_v2_database(db_path)
    before_schema = _schema_rows(db_path)

    def drifting(conn: sqlite3.Connection) -> None:
        _REAL_MIGRATION_3.apply(conn)
        conn.execute("ALTER TABLE targets ADD COLUMN surprise TEXT")

    monkeypatch.setattr(
        sqlite_db,
        "MIGRATIONS",
        tuple(
            sqlite_db.Migration(m.version, m.description, drifting) if m.version == 3 else m
            for m in sqlite_db.MIGRATIONS
        ),
    )
    conn = sqlite_db.connect(db_path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError, match="unexpected column"):
            sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == 2
    finally:
        sqlite_db.close_connection(conn)
    assert _schema_rows(db_path) == before_schema


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


def test_migrated_database_passes_backup_validation_and_bootstraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.utils import backup as backup_mod

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "data" / "sqlite" / "justup.db"
    _build_v2_database(db)
    _init(db)  # migrated from v2; additive columns may differ in order from a new database

    backup_mod.check_sqlite_integrity(db, full=True)
    backup_mod.check_app_schema_compatible(db)  # what restore runs on the extracted application database

    restored = tmp_path / "restored"
    (restored / "sqlite").mkdir(parents=True)
    snapshot_target = restored / "sqlite"
    backup_mod.snapshot_sqlite_dir(db.parent, snapshot_target)
    backup_mod.verify_extracted_databases(restored)
    bootstrap.bootstrap(restored / "sqlite" / "justup.db", restored / "tsdb", restored)
    conn = sqlite_db.connect(restored / "sqlite" / "justup.db")
    try:
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        sqlite_db.close_connection(conn)


# ─── Recipient "enable for all targets" ───────────────────────────────────────
def test_v5_migration_adds_flag_off_for_existing_recipients(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        rid = sqlite_db.create_recipient(conn, name="old", email="old@example.org")
        conn.execute("ALTER TABLE recipients DROP COLUMN enable_for_all_targets")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert sqlite_db.get_recipient(conn, rid)["enable_for_all_targets"] == 0
    finally:
        sqlite_db.close_connection(conn)


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


def test_v6_pop_migration_preserves_historical_learned_rows_and_marks_old_reviews_legacy(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, "
            "confidence, priority, created_at, updated_at) "
            "VALUES ('old','old','Old City','DE',50,8,'learned',40,10,'x','x')"
        )
        conn.execute("DROP TABLE pop_candidate_reviews")
        conn.execute("DROP TABLE pop_candidate_observations")
        conn.execute("DROP TABLE pop_city_evidence")
        conn.execute("DROP TABLE pop_alias_candidates")
        conn.execute(
            """CREATE TABLE pop_alias_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_code TEXT NOT NULL UNIQUE,
                canonical_guess TEXT,
                city_guess TEXT,
                country_guess TEXT,
                seen_count INTEGER NOT NULL DEFAULT 1 CHECK(seen_count >= 0),
                last_seen TEXT NOT NULL,
                reviewed INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0, 1)),
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX idx_pop_candidates_count ON pop_alias_candidates(seen_count)")
        conn.execute(
            "INSERT INTO pop_alias_candidates (alias_code, seen_count, last_seen, reviewed, created_at) "
            "VALUES ('abc', 2, 'x', 1, 'x')"
        )
        conn.commit()
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert tuple(
            conn.execute("SELECT source, confidence, retired FROM pop_locations WHERE alias_code='old'").fetchone()
        ) == ("learned", 40, 0)
        status = conn.execute("SELECT status FROM pop_alias_candidates WHERE alias_code='abc'").fetchone()[0]
        assert status == "legacy_reviewed"
        assert conn.execute("SELECT COUNT(*) FROM pop_candidate_reviews").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        sqlite_db.close_connection(conn)


# ─── v9: indexes for the dashboard alert summary ──────────────────────────────

_SUMMARY_INDEXES = {"idx_alerts_status_ended", "idx_alerts_acknowledged_at"}
_SUMMARY_QUERIES = {
    "resolved": (
        "SELECT COUNT(*) FROM alerts WHERE status = 'resolved' AND ended_at >= ? AND ended_at <= ?",
        "idx_alerts_status_ended",
    ),
    "acknowledged": (
        (
            "SELECT COUNT(*) FROM alerts WHERE status = 'resolved' AND is_acknowledged = 1 "
            "AND acknowledged_at >= ? AND acknowledged_at <= ?"
        ),
        "idx_alerts_acknowledged_at",
    ),
}


def _alert_indexes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='alerts'")}


def _plan(conn: sqlite3.Connection, name: str) -> str:
    sql, _ = _SUMMARY_QUERIES[name]
    params = ("2026-11-03T00:00:00", "2026-11-04T00:00:00")
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def _fill_alerts(conn: sqlite3.Connection, count: int = 3000) -> None:
    """Many resolved alerts, a few acknowledged and open ones: the shape that makes a scan expensive."""
    target = _target(conn, "alerts")
    rows = []
    for i in range(count):
        start = f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}T{i % 24:02d}:00:00"
        resolved = i % 50 != 0
        ack = resolved and i % 4 == 0
        rows.append(
            (
                f"a{i}",
                target,
                "alerts",
                "HTTP",
                "resolved" if resolved else "open",
                0 if resolved else 1,
                start,
                start if resolved else None,
                int(ack),
                start if ack else None,
                start,
                start,
            )
        )
    conn.executemany(
        "INSERT INTO alerts (id, target_id, target_name, failed_service, status, active_incident, started_at, "
        "ended_at, is_acknowledged, acknowledged_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _downgrade_to_v8(conn: sqlite3.Connection) -> None:
    for name in _SUMMARY_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone():
        conn.execute("DELETE FROM sqlite_stat1")
    conn.commit()
    conn.execute("PRAGMA user_version = 8")
    conn.commit()


def test_fresh_database_has_the_alert_summary_indexes(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        assert _alert_indexes(conn) >= _SUMMARY_INDEXES
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_alerts_acknowledged_at'").fetchone()[0]
        assert "WHERE is_acknowledged = 1" in sql
    finally:
        sqlite_db.close_connection(conn)


def test_v9_migration_adds_the_indexes_and_the_summary_plan_uses_them(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        _fill_alerts(conn)
        _downgrade_to_v8(conn)
        assert not _SUMMARY_INDEXES & _alert_indexes(conn)
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert _alert_indexes(conn) >= _SUMMARY_INDEXES
        # The planner only picks the acknowledged_at index with statistics: the migration collects them once.
        assert conn.execute("SELECT 1 FROM sqlite_stat1 WHERE tbl='alerts' LIMIT 1").fetchone()
        for name, (_, index) in _SUMMARY_QUERIES.items():
            assert index in _plan(conn, name), f"{name}: {_plan(conn, name)}"
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 3000
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        sqlite_db.close_connection(conn)


def test_v9_migration_is_idempotent_across_connections(db_path: Path):
    _init(db_path)
    conn = sqlite_db.connect(db_path)
    try:
        _downgrade_to_v8(conn)
        sqlite_db.init_schema(conn)
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
    finally:
        sqlite_db.close_connection(conn)
    before = {r[0]: r[1] for r in sqlite3.connect(db_path).execute("SELECT name, sql FROM sqlite_master")}
    _init(db_path)
    after = {r[0]: r[1] for r in sqlite3.connect(db_path).execute("SELECT name, sql FROM sqlite_master")}
    assert before == after
