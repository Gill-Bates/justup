#!/usr/bin/env python3
#
# tests/test_user_recipient_migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Schema migration 1 -> 2 (recipients.user_id) from version 1 and the legacy stamp 10, and startup on both."""

# ruff: noqa: SLF001
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.models.users import hash_password
from app.utils import bootstrap, crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests._legacy_db import build_v1_database, seed_v1_data

SUPPORTED_STAMPS = [1, sqlite_db.LEGACY_SCHEMA_VERSION]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "sqlite" / "justup.db"


def _init(path: Path) -> None:
    conn = sqlite_db.connect(path)
    try:
        sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)


def _rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _data_snapshot(path: Path) -> dict[str, list[tuple]]:
    tables = ("users", "recipients", "targets", "target_recipients", "target_alert_users")
    return {t: _rows(path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables}  # noqa: S608 - constant names


def _sha(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.is_file()}


# ─── E8: migration from every supported version ──────────────────────────────


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_migration_keeps_all_data_and_passes_the_integrity_checks(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    counts = seed_v1_data(path)
    before = _data_snapshot(path)
    conn = sqlite_db.connect(path)
    try:
        assert sqlite_db.check_schema_compatibility(conn) == stamp
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # restored after the rebuild
        assert sqlite_db._layout(conn) == sqlite_db._expected_layout()
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert triggers == {
            "trg_users_create_recipient",
            "trg_recipients_guard_delete",
            "trg_recipients_user_id_immutable",
        }
    finally:
        sqlite_db.close_connection(conn)
    after = _data_snapshot(path)
    # Nothing but the added column and the backfilled user recipients changed.
    assert after["users"] == before["users"]
    assert after["targets"] == [(*row, 5) for row in before["targets"]]  # only the new down_alert_delay_minutes
    assert after["target_recipients"] == before["target_recipients"]  # the DROP did not cascade
    assert after["target_alert_users"] == before["target_alert_users"]
    assert len(after["target_recipients"]) == counts["target_recipients"]
    old = {row[0]: row[:8] for row in before["recipients"]}
    kept = {row[0]: row[:8] for row in after["recipients"] if row[0] in old}
    assert kept == old
    assert all(row[8] is None for row in after["recipients"] if row[0] in old)  # standalone: no user_id


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_migration_adds_the_report_jobs_target_index(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    index = "SELECT sql FROM sqlite_master WHERE name = 'idx_report_jobs_target'"
    assert _rows(path, index) == []  # version 1 never had it
    _init(path)
    assert _rows(path, index) == [("CREATE INDEX idx_report_jobs_target ON report_jobs(target_id)",)]


def test_fresh_database_has_the_report_jobs_target_index(path: Path):
    _init(path)
    assert _rows(path, "SELECT tbl_name FROM sqlite_master WHERE name = 'idx_report_jobs_target'") == [("report_jobs",)]


def test_version1_layout_comes_from_the_frozen_ddl_not_the_current_baseline():
    layout = sqlite_db.layout._version1_layout()
    assert "user_id" not in layout[0]["recipients"].columns
    assert "idx_report_jobs_target" not in layout[1]
    assert "idx_report_jobs_target" in sqlite_db._expected_layout()[1]


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_existing_users_get_a_disabled_channelless_editable_recipient(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    _init(path)
    rows = _rows(
        path,
        "SELECT u.username, r.name, r.phone, r.email, r.is_enabled, r.enable_for_all_targets FROM users u "
        "JOIN recipients r ON r.user_id = u.id ORDER BY u.id",
    )
    assert rows == [(n, n, None, None, 0, 0) for n in ("admin", "alice", "bob")]
    assert _rows(
        path, "SELECT COUNT(*) FROM users u WHERE NOT EXISTS (SELECT 1 FROM recipients WHERE user_id = u.id)"
    ) == [(0,)]
    # Editable: the migrated recipient takes channels through the data layer like any other.
    conn = sqlite_db.connect(path)
    try:
        uid = conn.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
        sqlite_db.update_user_recipient(
            conn, uid, phone="+4915100000009", email=None, is_enabled=True, enable_for_all_targets=False
        )
        recipient = sqlite_db.get_user_recipient(conn, uid)
        assert (recipient["phone"], recipient["is_enabled"]) == ("+4915100000009", 1)
    finally:
        sqlite_db.close_connection(conn)


def test_deleted_recipient_ids_are_not_reused_after_the_rebuild(path: Path):
    build_v1_database(path)
    seed_v1_data(path)  # id 3 was deleted: AUTOINCREMENT counter 3, highest id 2
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("BEGIN")
    sqlite_db.migrations._rebuild_recipients_with_user_id(raw)  # the rebuild alone, without the user backfill
    raw.commit()
    raw.close()
    assert _rows(path, "SELECT seq FROM sqlite_sequence WHERE name = 'recipients'") == [(3,)]
    conn = sqlite_db.connect(path)
    try:
        assert sqlite_db.create_recipient(conn, name="next", email="n@example.org") == 4
    finally:
        sqlite_db.close_connection(conn)


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_second_start_changes_nothing(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    _init(path)
    schema = _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name")
    data = _data_snapshot(path)
    for _ in range(2):
        _init(path)
    assert _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name") == schema
    assert _data_snapshot(path) == data


def test_migration_step_is_repeatable_on_a_migrated_database(path: Path):
    build_v1_database(path)
    seed_v1_data(path)
    _init(path)
    data = _data_snapshot(path)
    conn = sqlite3.connect(path)
    conn.execute("BEGIN")
    sqlite_db.migrations._migrate_v1_to_v2(conn)  # no second rebuild, no second backfill
    conn.commit()
    conn.close()
    assert _data_snapshot(path) == data


def test_legacy_duplicate_phone_numbers_survive_with_the_non_unique_index(path: Path, caplog: pytest.LogCaptureFixture):
    build_v1_database(path)
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX idx_recipients_phone")
    raw.execute("CREATE INDEX idx_recipients_phone ON recipients(phone)")
    for name in ("A", "B"):
        raw.execute(
            "INSERT INTO recipients(name, phone, created_at, updated_at) VALUES (?, '+4915112345678', 't', 't')",
            (name,),
        )
    raw.commit()
    raw.close()
    with caplog.at_level("WARNING"):
        _init(path)
    assert _rows(path, "SELECT COUNT(*) FROM recipients WHERE phone = '+4915112345678'") == [(2,)]
    sql = _rows(path, "SELECT sql FROM sqlite_master WHERE name = 'idx_recipients_phone'")[0][0]
    assert "UNIQUE" not in sql
    assert "phone number" in caplog.text


def test_clean_phone_numbers_get_the_unique_index(path: Path):
    build_v1_database(path)
    seed_v1_data(path)
    _init(path)
    sql = _rows(path, "SELECT sql FROM sqlite_master WHERE name = 'idx_recipients_phone'")[0][0]
    assert "UNIQUE" in sql


# ─── E9: failures and races leave nothing half-migrated ─────────────────────


def test_failure_after_the_table_swap_rolls_everything_back(path: Path, monkeypatch: pytest.MonkeyPatch):
    build_v1_database(path)
    seed_v1_data(path)
    before = _data_snapshot(path)
    schema = _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name")

    def boom():
        raise RuntimeError("injected")

    monkeypatch.setattr(sqlite_db.migrations, "utcnow", boom)  # runs after DROP/RENAME, before the backfill
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        assert sqlite_db.get_schema_version(conn) == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # restored even on failure
    finally:
        sqlite_db.close_connection(conn)
    assert _data_snapshot(path) == before
    assert _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name") == schema


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
@pytest.mark.parametrize("name", ["recipients_new", "RECIPIENTS_NEW"])
def test_foreign_table_with_the_scratch_name_stops_the_migration_untouched(path: Path, stamp: int, name: str):
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    raw = sqlite3.connect(path)
    raw.execute(f"CREATE TABLE {name} (note TEXT)")  # extra tables pass the compatibility check
    raw.execute(f"INSERT INTO {name} VALUES ('keep me')")  # noqa: S608 - parametrized constant
    raw.commit()
    raw.close()
    before = _data_snapshot(path)
    schema = _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name")
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        assert "recipients_new" in str(exc.value)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        sqlite_db.close_connection(conn)
    assert _rows(path, "PRAGMA user_version") == [(stamp,)]
    assert _rows(path, f"SELECT note FROM {name}") == [("keep me",)]  # noqa: S608 - parametrized constant
    assert _data_snapshot(path) == before
    assert _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name") == schema


def test_foreign_keys_that_cannot_be_disabled_stop_the_start_before_any_write(path: Path):
    build_v1_database(path)
    seed_v1_data(path)
    conn = sqlite_db.connect(path)
    try:
        conn.execute("BEGIN")  # PRAGMA foreign_keys is a no-op inside a transaction
        conn.execute("SELECT 1 FROM users").fetchall()
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.migrations._disable_foreign_keys_for(conn, list(sqlite_db.MIGRATIONS), 1)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        conn.rollback()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        sqlite_db.close_connection(conn)
    assert _rows(path, "SELECT COUNT(*) FROM target_recipients") == [(3,)]


def test_parallel_starts_migrate_once_without_duplicate_recipients(path: Path):
    build_v1_database(path)
    seed_v1_data(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def start() -> None:
        conn = sqlite_db.connect(path)
        try:
            barrier.wait()
            sqlite_db.init_schema(conn)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)
        finally:
            sqlite_db.close_connection(conn)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert _rows(path, "PRAGMA user_version") == [(2,)]
    assert _rows(path, "SELECT COUNT(*) FROM recipients") == [(5,)]  # 2 standalone + 3 users
    assert _rows(
        path,
        "SELECT COUNT(*) FROM (SELECT user_id FROM recipients GROUP BY user_id HAVING COUNT(*) > 1 "
        "AND user_id IS NOT NULL)",
    ) == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM target_recipients") == [(3,)]


# ─── Compatibility check keeps detecting drift of the old versions ──────────


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_drifting_old_database_is_refused_and_left_byte_identical(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE recipients ADD COLUMN junk TEXT")
    raw.commit()
    raw.close()
    before = _sha(path.parent)
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    assert exc.value.kind == sqlite_db.SCHEMA_DRIFT
    assert "recipients: unexpected column(s) junk" in str(exc.value)
    assert _sha(path.parent) == before


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_current_layout_with_an_old_stamp_is_refused(path: Path, stamp: int):
    _init(path)
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {stamp}")
    raw.commit()
    raw.close()
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.check_schema_compatibility(conn)
    finally:
        sqlite_db.close_connection(conn)
    assert exc.value.kind == sqlite_db.SCHEMA_DRIFT


# ─── Startup with a fresh and a migrated database ────────────────────────────


@pytest.fixture
def _app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_primary_key", None)
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    return tmp_path


def _sign_in(client: TestClient, username: str, password: str) -> dict[str, str]:
    client.get("/ui/login")  # sets the CSRF cookie
    response = client.post(
        "/login",
        json={"username": username, "password": password},
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_startup_with_a_fresh_database_gives_the_bootstrap_admin_a_recipient(_app_env: Path):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        app.state.bootstrap_gate_active = False
        db_file = _app_env / "sqlite" / "justup.db"
        conn = sqlite_db.connect(db_file)
        try:
            admin = sqlite_db.get_user_by_username(conn, "admin")
            sqlite_db.update_user_password(
                conn, admin["id"], hash_password("Sup3r-secret!"), must_change_password=False
            )
        finally:
            sqlite_db.close_connection(conn)
        headers = _sign_in(client, "admin", "Sup3r-secret!")
        users = client.get("/users", headers=headers).json()
        recipients = client.get("/recipients", headers=headers).json()
    assert [u["username"] for u in users] == ["admin"]
    recipient = users[0]["recipient"]
    assert (recipient["phone"], recipient["email"], recipient["is_enabled"]) == (None, None, False)
    assert [(r["id"], r["is_user_linked"], r["is_enabled"]) for r in recipients] == [(recipient["id"], True, False)]


def test_startup_with_a_migrated_database_keeps_data_and_serves_it(_app_env: Path):
    from app.main import create_app

    db_file = _app_env / "sqlite" / "justup.db"
    build_v1_database(db_file)
    seed_v1_data(db_file)
    raw = sqlite3.connect(db_file)
    raw.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = 'admin'",
        (hash_password("Sup3r-secret!"),),
    )
    raw.commit()
    raw.close()
    app = create_app()
    with TestClient(app) as client:
        headers = _sign_in(client, "admin", "Sup3r-secret!")
        users = client.get("/users", headers=headers).json()
        recipients = client.get("/recipients", headers=headers).json()
    assert [u["username"] for u in users] == ["admin", "alice", "bob"]
    assert all(u["recipient"] is not None and u["recipient"]["is_enabled"] is False for u in users)
    by_name = {r["name"]: r for r in recipients}
    assert {n for n, r in by_name.items() if r["is_user_linked"]} == {"admin", "alice", "bob"}
    assert by_name["ops"]["is_user_linked"] is False and by_name["ops"]["phone"] == "+4915112345678"
    assert [t["name"] for t in by_name["mail only"]["targets"]] == ["t1", "t2"]
    assert _rows(db_file, "PRAGMA user_version") == [(2,)]
    assert _rows(db_file, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == [(1,)]  # no second bootstrap admin


# ─── Deterministic interleaving of two starts ───────────────────────────────


def test_start_that_lost_the_race_rechecks_under_the_write_lock(path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    """B passes its first check on version 1, then A finishes the whole migration, then B takes the lock."""
    build_v1_database(path)
    seed_v1_data(path)
    conn_a, conn_b = sqlite_db.connect(path), sqlite_db.connect(path)
    b_checked, a_done = threading.Event(), threading.Event()
    real_wal = sqlite_db.migrations.ensure_wal_mode  # runs between the first check and the write lock
    errors: list[BaseException] = []

    def wal(conn):
        if conn is conn_b:
            b_checked.set()
            assert a_done.wait(30)
        return real_wal(conn)

    monkeypatch.setattr(sqlite_db.migrations, "ensure_wal_mode", wal)

    def start_b() -> None:
        try:
            sqlite_db.init_schema(conn_b)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=start_b)
    with caplog.at_level("INFO"):
        thread.start()
        assert b_checked.wait(30)
        sqlite_db.init_schema(conn_a)  # B has read version 1 and now waits
        assert sqlite_db.get_schema_version(conn_a) == 2
        a_done.set()
        thread.join(30)
    sqlite_db.close_connection(conn_a)
    sqlite_db.close_connection(conn_b)
    assert errors == []  # no false drift report, no second migration
    assert "while waiting for the write lock" in caplog.text
    assert caplog.text.count("Schema migration 1 -> 2") == 1
    assert _rows(path, "PRAGMA user_version") == [(2,)]
    assert _rows(path, "SELECT COUNT(*) FROM recipients") == [(5,)]
    assert _rows(path, "SELECT COUNT(*) FROM target_recipients") == [(3,)]


def test_compatibility_check_reads_version_and_layout_from_one_snapshot(path: Path):
    build_v1_database(path)
    seed_v1_data(path)
    conn, other = sqlite_db.connect(path), sqlite_db.connect(path)
    try:
        conn.execute("BEGIN")
        assert sqlite_db.get_schema_version(conn) == 1  # the snapshot of `conn` starts here
        sqlite_db.init_schema(other)  # a concurrent start migrates completely
        assert sqlite_db.check_schema_compatibility(conn) == 1  # same snapshot: version 1 with the version-1 layout
        conn.rollback()
        assert sqlite_db.check_schema_compatibility(conn) == 2
        assert not conn.in_transaction
    finally:
        sqlite_db.close_connection(conn)
        sqlite_db.close_connection(other)


# ─── External review: blank legacy channels when the CHECKs get stricter ────────────────────────────────────────
@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_blank_legacy_channels_are_normalised_to_null_without_losing_a_real_one(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    raw = sqlite3.connect(path)
    raw.executemany(
        "INSERT INTO recipients(id, name, phone, email, is_enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 't', 't')",
        [
            (1, "blank phone", "", "a@example.org"),
            (2, "blank mail", "+4915100000011", "  "),
            (3, "spaces phone", " \t ", "c@example.org"),
            (4, "intact", "+4915100000012", "d@example.org"),
        ],
    )
    raw.commit()
    raw.close()
    _init(path)
    assert _rows(path, "SELECT id, phone, email, is_enabled FROM recipients WHERE user_id IS NULL ORDER BY id") == [
        (1, None, "a@example.org", 1),
        (2, "+4915100000011", None, 1),
        (3, None, "c@example.org", 1),
        (4, "+4915100000012", "d@example.org", 1),
    ]


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_legacy_recipient_without_any_real_channel_stops_the_migration_untouched(path: Path, stamp: int):
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO recipients(id, name, phone, email, created_at, updated_at) VALUES (7, 'empty', '', ' ', 't', 't')"
    )
    raw.commit()
    raw.close()
    before = _data_snapshot(path)
    schema = _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name")
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as exc:
            sqlite_db.init_schema(conn)
        assert exc.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
        assert "7" in str(exc.value) and "channel" in str(exc.value)
    finally:
        sqlite_db.close_connection(conn)
    assert _rows(path, "PRAGMA user_version") == [(stamp,)]
    assert _data_snapshot(path) == before
    assert _rows(path, "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name") == schema
