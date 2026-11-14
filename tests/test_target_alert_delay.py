#!/usr/bin/env python3
#
# tests/test_target_alert_delay.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Down alert delay per target (targets.down_alert_delay_minutes) replaces the global metrics timeout setting."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import settings as settings_api
from app.api import targets as targets_api
from app.api.auth import get_current_user, require_admin
from app.db import sqlite as sqlite_db
from app.models.settings import Settings, SettingsUpdate
from app.models.targets import TargetCreate, TargetUpdate
from app.models.users import hash_password
from app.monitor.scheduler import MonitorScheduler, TargetStatus
from app.utils import bootstrap, crypto
from app.utils.constants import MONITORING_LIMITS
from app.utils.deps import get_conn
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests._legacy_db import STAMP, build_v1_database, seed_v1_data

OLD_KEY = "metrics_timeout_minutes"
SUPPORTED_STAMPS = [1, sqlite_db.LEGACY_SCHEMA_VERSION]


# ─── helpers ─────────────────────────────────────────────────────────────────
def _sched(db_path: Path, tmp_path: Path) -> MonitorScheduler:
    return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")


def _target(db_path: Path, **values) -> int:
    base = {
        "name": "t",
        "interval_seconds": 60,
        "retention_days": 30,
        "enable_http_check": True,
    }
    base.update(values)
    base.setdefault("host", f"{base['name']}.example")
    base.setdefault("http_url", f"https://{base['host']}/")
    conn = sqlite_db.connect(db_path)
    try:
        return sqlite_db.create_target(conn, base)
    finally:
        sqlite_db.close_connection(conn)


def _alert_count(db_path: Path) -> int:
    conn = sqlite_db.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
    finally:
        sqlite_db.close_connection(conn)


async def _down(sched: MonitorScheduler, tid: int, *, down_for: timedelta) -> None:
    sched._rt[tid].down_since_by_service["http"] = utcnow() - down_for
    await sched._maybe_alert(target_id=tid, target_name="t", overall_ok=False, overall_status=TargetStatus.DOWN)


def _rows(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _sha(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.is_file()}


def _set_old_key(path: Path, raw_value: str | None) -> None:
    """Write the former global setting as the raw stored JSON text (None: no row at all)."""
    if raw_value is None:
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)", (OLD_KEY, raw_value, STAMP))
        conn.commit()
    finally:
        conn.close()


def _init(path: Path) -> None:
    conn = sqlite_db.connect(path)
    try:
        sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)


def _v1_database(path: Path, *, stamp: int = 1, old_value: str | None = None) -> None:
    build_v1_database(path, stamp=stamp)
    seed_v1_data(path)
    _set_old_key(path, old_value)


def _delays(path: Path) -> list[int]:
    return [r[0] for r in _rows(path, "SELECT down_alert_delay_minutes FROM targets ORDER BY id")]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "sqlite" / "justup.db"


# ─── scheduler: the delay is read per target ─────────────────────────────────
def test_two_targets_with_one_and_ten_minutes_alert_at_different_times(db_path, tmp_path):
    fast = _target(db_path, name="fast", down_alert_delay_minutes=1)
    slow = _target(db_path, name="slow", down_alert_delay_minutes=10)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    asyncio.run(_down(sched, slow, down_for=timedelta(minutes=2)))
    assert _alert_count(db_path) == 0 and sched._rt[slow].current_alert_id is None
    asyncio.run(_down(sched, fast, down_for=timedelta(minutes=2)))
    assert sched._rt[fast].current_alert_id is not None and _alert_count(db_path) == 1
    asyncio.run(_down(sched, slow, down_for=timedelta(minutes=11)))
    assert sched._rt[slow].current_alert_id is not None and _alert_count(db_path) == 2


@pytest.mark.parametrize(("seconds", "alerts"), [(299, 0), (301, 1)])
def test_default_delay_of_five_minutes_is_the_boundary(db_path, tmp_path, seconds, alerts):
    tid = _target(db_path)  # no value: default
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    assert sched._targets[tid]["down_alert_delay_minutes"] == 5
    asyncio.run(_down(sched, tid, down_for=timedelta(seconds=seconds)))
    assert _alert_count(db_path) == alerts


def test_changed_delay_takes_effect_at_the_next_refresh(db_path, tmp_path):
    tid = _target(db_path, down_alert_delay_minutes=10)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    asyncio.run(_down(sched, tid, down_for=timedelta(minutes=2)))
    assert _alert_count(db_path) == 0
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.update_target(conn, target_id=tid, patch={"down_alert_delay_minutes": 1})
    finally:
        sqlite_db.close_connection(conn)
    sched._refresh_targets(utcnow())
    assert sched._targets[tid]["down_alert_delay_minutes"] == 1
    asyncio.run(_down(sched, tid, down_for=timedelta(minutes=2)))
    assert _alert_count(db_path) == 1


def test_a_leftover_global_setting_no_longer_matters(db_path, tmp_path):
    tid = _target(db_path, down_alert_delay_minutes=10)
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.set_setting(conn, OLD_KEY, 1)
    finally:
        sqlite_db.close_connection(conn)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    asyncio.run(_down(sched, tid, down_for=timedelta(minutes=2)))
    assert _alert_count(db_path) == 0
    assert not hasattr(sched, "_metrics_timeout_minutes")


# ─── validation at the model boundary ────────────────────────────────────────
def test_limits_are_published_with_the_other_monitoring_limits():
    assert MONITORING_LIMITS["down_alert_delay_minutes"] == {"min": 1, "max": 10}


def test_target_create_defaults_to_five_minutes():
    assert TargetCreate(name="x", host="x.example", enable_ping=True).down_alert_delay_minutes == 5


@pytest.mark.parametrize("value", [1, 5, 10])
def test_models_accept_the_whole_range(value):
    assert TargetCreate(name="x", host="x.example", down_alert_delay_minutes=value).down_alert_delay_minutes == value
    assert TargetUpdate(down_alert_delay_minutes=value).down_alert_delay_minutes == value


@pytest.mark.parametrize("value", [0, 11, -1, "abc", 2.5])
def test_models_reject_values_outside_the_range(value):
    with pytest.raises(ValidationError):
        TargetCreate(name="x", host="x.example", down_alert_delay_minutes=value)
    with pytest.raises(ValidationError):
        TargetUpdate(down_alert_delay_minutes=value)


def test_update_rejects_an_explicit_null():
    with pytest.raises(ValidationError):
        TargetUpdate.model_validate({"down_alert_delay_minutes": None})


# ─── API ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def api(tmp_path: Path):
    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    uid = sqlite_db.create_user(conn, username="admin", password_hash="stub", is_admin=True)  # noqa: S106
    app = FastAPI()
    app.include_router(targets_api.router)
    app.include_router(settings_api.router)
    app.state.tsdb_dir = tmp_path / "db"
    user = dict(sqlite_db.get_user(conn, uid))
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    yield TestClient(app), conn
    sqlite_db.close_connection(conn)


_BODY = {"name": "api", "host": "api.example.com", "interval_seconds": 60, "retention_days": 30, "enable_ping": True}


def test_api_stores_and_returns_the_delay(api):
    client, conn = api
    created = client.post("/targets", json={**_BODY, "down_alert_delay_minutes": 3})
    assert created.status_code in (200, 201), created.text
    tid = created.json()["id"]
    assert created.json()["down_alert_delay_minutes"] == 3
    assert client.get(f"/targets/{tid}").json()["down_alert_delay_minutes"] == 3
    assert sqlite_db.get_target(conn, tid)["down_alert_delay_minutes"] == 3
    patched = client.patch(f"/targets/{tid}", json={"down_alert_delay_minutes": 10})
    assert patched.status_code == 200, patched.text
    assert patched.json()["down_alert_delay_minutes"] == 10
    assert client.patch(f"/targets/{tid}", json={"name": "renamed"}).json()["down_alert_delay_minutes"] == 10


def test_api_default_is_five_without_a_value(api):
    client, _conn = api
    created = client.post("/targets", json=_BODY)
    assert created.status_code in (200, 201), created.text
    assert created.json()["down_alert_delay_minutes"] == 5


@pytest.mark.parametrize("value", [0, 11, -1, "abc", None])
def test_api_rejects_invalid_values_and_changes_nothing(api, value):
    client, conn = api
    tid = client.post("/targets", json={**_BODY, "down_alert_delay_minutes": 4}).json()["id"]
    assert client.patch(f"/targets/{tid}", json={"down_alert_delay_minutes": value}).status_code == 422
    assert sqlite_db.get_target(conn, tid)["down_alert_delay_minutes"] == 4
    if value is not None:
        assert client.post("/targets", json={**_BODY, "down_alert_delay_minutes": value}).status_code == 422


def test_wizard_preview_shows_the_default(api):
    client, _conn = api
    response = client.post("/targets/preview", json={"profile": "website", "address": "example.org"})
    assert response.status_code == 200, response.text
    assert response.json()["values"]["down_alert_delay_minutes"] == 5
    override = client.post(
        "/targets/preview",
        json={"profile": "website", "address": "example.org", "overrides": {"down_alert_delay_minutes": 2}},
    )
    assert override.json()["values"]["down_alert_delay_minutes"] == 2


# ─── the global setting is gone ──────────────────────────────────────────────
def test_settings_patch_with_the_removed_field_is_422(api):
    client, conn = api
    assert client.patch("/settings", json={OLD_KEY: 3}).status_code == 422
    assert sqlite_db.get_setting(conn, OLD_KEY, "<missing>") == "<missing>"


def test_settings_response_has_no_such_field(api):
    client, _conn = api
    response = client.get("/settings")
    assert response.status_code == 200, response.text
    assert OLD_KEY not in response.json()
    assert OLD_KEY not in Settings.model_fields and OLD_KEY not in SettingsUpdate.model_fields


def test_bootstrap_does_not_seed_the_removed_key(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    db.parent.mkdir()
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    assert _rows(db, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []


# ─── migration: the old global value moves into every target ─────────────────
@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_migration_copies_the_global_value_to_every_target_and_drops_the_key(path: Path, stamp: int):
    _v1_database(path, stamp=stamp, old_value="7")
    _set_other = _rows(path, "SELECT key, value FROM settings ORDER BY key")
    counts_before = {t: _rows(path, f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("users", "targets", "recipients")}  # noqa: S608 - constant names
    conn = sqlite_db.connect(path)
    try:
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert sqlite_db._layout(conn) == sqlite_db._expected_layout()
    finally:
        sqlite_db.close_connection(conn)
    assert _delays(path) == [7, 7]
    assert _rows(path, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []
    assert _rows(path, "SELECT key, value FROM settings ORDER BY key") == [r for r in _set_other if r[0] != OLD_KEY]
    assert counts_before["targets"] == 2 == _rows(path, "SELECT COUNT(*) FROM targets")[0][0]
    assert counts_before["users"] == _rows(path, "SELECT COUNT(*) FROM users")[0][0]


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_migration_without_a_stored_value_uses_the_default(path: Path, stamp: int):
    _v1_database(path, stamp=stamp)
    _init(path)
    assert _delays(path) == [5, 5]


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("5", 5),
        ("10", 10),
        ("0", 5),  # falsy: the former runtime fell back to the default
        ("17", 10),  # clamped like the former runtime
        ("-3", 1),
        ('"abc"', 5),
        ('"7"', 7),
        ("null", 5),
        ("2.9", 2),
        ("{not json", 5),
    ],
)
def test_migration_edge_values_follow_the_former_runtime_rules(path: Path, stamp: int, raw: str, expected: int):
    _v1_database(path, stamp=stamp, old_value=raw)
    _init(path)
    assert _delays(path) == [expected, expected]
    assert _rows(path, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []


def test_migration_of_a_database_without_targets(path: Path):
    build_v1_database(path)
    _set_old_key(path, "3")
    _init(path)
    assert _rows(path, "SELECT COUNT(*) FROM targets") == [(0,)]
    assert _rows(path, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []


def test_second_start_changes_nothing(path: Path):
    _v1_database(path, old_value="7")
    _init(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE targets SET down_alert_delay_minutes = 2 WHERE id = 1")
    conn.commit()
    conn.close()
    before = _rows(path, "SELECT * FROM targets ORDER BY id")
    _init(path)
    assert _rows(path, "SELECT * FROM targets ORDER BY id") == before
    assert _delays(path) == [2, 7]


def test_repeated_step_keeps_per_target_values_and_a_new_key(path: Path):
    """Run the step again on a migrated database: the column exists, so neither values nor the key are touched."""
    _v1_database(path, old_value="7")
    _init(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE targets SET down_alert_delay_minutes = 9 WHERE id = 2")
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, '1', ?)", (OLD_KEY, STAMP))
    conn.commit()
    sqlite_db.migrations._add_target_down_alert_delay(conn)
    conn.commit()
    conn.close()
    assert _delays(path) == [7, 9]
    assert _rows(path, "SELECT value FROM settings WHERE key = ?", (OLD_KEY,)) == [("1",)]


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_failed_migration_rolls_back_column_values_and_key(path: Path, stamp: int, monkeypatch: pytest.MonkeyPatch):
    _v1_database(path, stamp=stamp, old_value="7")
    snapshot = _rows(path, "SELECT * FROM targets ORDER BY id")
    settings_before = _rows(path, "SELECT * FROM settings ORDER BY key")
    real = sqlite_db.migrations._add_target_down_alert_delay

    def failing(conn):
        real(conn)  # column added, values copied, key deleted ...
        raise RuntimeError("boom")  # ... and then the migration fails

    monkeypatch.setattr(sqlite_db.migrations, "_add_target_down_alert_delay", failing)
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as caught:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    assert caught.value.kind == sqlite_db.SCHEMA_MIGRATION_FAILED
    assert _rows(path, "PRAGMA user_version") == [(stamp,)]
    assert _rows(path, "SELECT * FROM targets ORDER BY id") == snapshot
    assert "down_alert_delay_minutes" not in {r[1] for r in _rows(path, "PRAGMA table_info(targets)")}
    assert _rows(path, "SELECT * FROM settings ORDER BY key") == settings_before


def test_migration_step_is_part_of_the_one_to_two_migration():
    migration = next(m for m in sqlite_db.migrations.MIGRATIONS if m.version == 2)
    assert "alert delay" in migration.description


def test_fresh_database_has_the_column_with_its_default_and_no_key(path: Path):
    _init(path)
    columns = {r[1]: r for r in _rows(path, "PRAGMA table_info(targets)")}
    assert columns["down_alert_delay_minutes"][2:5] == ("INTEGER", 1, "5")
    assert _rows(path, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO targets(name, interval_seconds, retention_days, host, down_alert_delay_minutes, "
                "created_at, updated_at) VALUES ('x', 60, 30, 'x', 0, ?, ?)",
                (STAMP, STAMP),
            )
    finally:
        sqlite_db.close_connection(conn)


# ─── regression: layout drift of a stamp-2 database stays fail-closed ────────
def test_stamp_two_database_with_the_old_recipients_checks_is_drift_and_untouched(path: Path):
    """A development database stamped 2 but still carrying the version-1 recipients CHECKs is not repaired."""
    build_v1_database(path, stamp=2)
    seed_v1_data(path)
    before = _sha(path.parent)
    conn = sqlite_db.connect(path)
    try:
        with pytest.raises(sqlite_db.IncompatibleSchemaError) as caught:
            sqlite_db.init_schema(conn)
    finally:
        sqlite_db.close_connection(conn)
    assert caught.value.kind == sqlite_db.SCHEMA_DRIFT
    assert _sha(path.parent) == before


# ─── startup ─────────────────────────────────────────────────────────────────
@pytest.fixture
def _app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_primary_key", None)
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    return tmp_path


def _sign_in(client: TestClient) -> dict[str, str]:
    client.get("/ui/login")  # sets the CSRF cookie
    response = client.post(
        "/login",
        json={"username": "admin", "password": "Sup3r-secret!"},
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _start_and_list_targets(db_file: Path) -> list[dict]:
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        app.state.bootstrap_gate_active = False
        conn = sqlite_db.connect(db_file)
        try:
            admin = sqlite_db.get_user_by_username(conn, "admin")
            sqlite_db.update_user_password(
                conn, admin["id"], hash_password("Sup3r-secret!"), must_change_password=False
            )
        finally:
            sqlite_db.close_connection(conn)
        headers = _sign_in(client)
        listing = client.get("/targets", headers=headers)
        assert listing.status_code == 200, listing.text
        settings = client.get("/settings", headers=headers)
        assert OLD_KEY not in settings.json()
        return listing.json()


def test_startup_on_a_fresh_database(_app_env: Path):
    db_file = _app_env / "sqlite" / "justup.db"
    assert _start_and_list_targets(db_file) == []
    assert _rows(db_file, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []
    assert "down_alert_delay_minutes" in {r[1] for r in _rows(db_file, "PRAGMA table_info(targets)")}


@pytest.mark.parametrize("stamp", SUPPORTED_STAMPS)
def test_startup_on_an_old_database_serves_the_migrated_value(_app_env: Path, stamp: int):
    db_file = _app_env / "sqlite" / "justup.db"
    _v1_database(db_file, stamp=stamp, old_value="9")
    listed = _start_and_list_targets(db_file)
    assert [(t["name"], t["down_alert_delay_minutes"]) for t in listed] == [("t1", 9), ("t2", 9)]
    assert _rows(db_file, "SELECT 1 FROM settings WHERE key = ?", (OLD_KEY,)) == []
