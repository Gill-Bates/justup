#!/usr/bin/env python3
#
# tests/test_bootstrap_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bootstrap admin: one-time console password, forced change and first-boot gate."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app.api import users as users_api
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.models.users import RequiredPasswordChangeRequest, hash_password, hash_token, new_token, verify_password
from app.utils import bootstrap, bootstrap_credentials, crypto
from app.utils.backup import BACKUP_DIRECTORIES
from tests import _support  # noqa: F401 - imported for its sys.path side effect

_STUB_HASH = "stub-hash"  # not a credential; never verified
_OTHER_PASSWORD = "Another-pass-9"  # noqa: S105 - throwaway test value


@pytest.fixture
def conn(tmp_path: Path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture
def pending_file(tmp_path: Path):
    """Configure a private credential location for one test and reset it afterwards."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = bootstrap_credentials.configure(data_dir)
    yield path
    bootstrap_credentials.configure(None)


def _shown_password(out: str) -> str | None:
    line = next((ln for ln in out.splitlines() if "Password:" in ln), None)
    return line.split("Password:", 1)[1].strip() if line else None


def _request(path: str, *, state: SimpleNamespace | None = None, method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("127.0.0.1", 1234),
    }
    if state is not None:
        scope["app"] = SimpleNamespace(state=state)
    return Request(scope)


def _issue(conn: sqlite3.Connection, user_id: int) -> HTTPAuthorizationCredentials:
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_bootstrap_prints_password_once_and_never_logs_or_stores_it(conn, tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    with caplog.at_level(logging.DEBUG):
        assert sqlite_db.ensure_default_admin(conn) is True

    out = capsys.readouterr().out
    password_line = next(line for line in out.splitlines() if "Password:" in line)
    secret = password_line.split("Password:", 1)[1].strip()
    assert secret
    assert "Username: admin" in out

    combined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret not in combined_logs
    assert {p.name for p in tmp_path.iterdir()} == before
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix not in {".db", ".db-wal", ".db-shm"}:
            assert secret.encode() not in path.read_bytes()

    admin = sqlite_db.get_user_by_username(conn, "admin")
    assert admin["is_admin"] == 1
    assert admin["is_active"] == 1
    assert admin["must_change_password"] == 1
    assert verify_password(secret, admin["password_hash"])
    assert secret not in admin["password_hash"]

    # A second call finds an existing user and prints nothing.
    assert sqlite_db.ensure_default_admin(conn) is False
    assert capsys.readouterr().out == ""


def test_no_admin_is_created_when_a_user_exists(conn, capsys):
    sqlite_db.create_user(conn, username="someone", password_hash=_STUB_HASH, is_admin=False)
    assert sqlite_db.ensure_default_admin(conn) is False
    assert sqlite_db.get_user_by_username(conn, "admin") is None
    assert capsys.readouterr().out == ""


def test_admin_password_env_is_ignored(conn, monkeypatch, capsys):
    monkeypatch.setenv("UPMON_ADMIN_PASSWORD", "from-env-password")
    monkeypatch.setenv("JUSTUP_ADMIN_PASSWORD", "from-env-password")
    assert sqlite_db.ensure_default_admin(conn) is True
    admin = sqlite_db.get_user_by_username(conn, "admin")
    assert not verify_password("from-env-password", admin["password_hash"])
    assert "from-env-password" not in capsys.readouterr().out


def test_generated_password_pattern_and_policy():
    for _ in range(200):
        pw = sqlite_db._generate_pronounceable_password()  # noqa: SLF001 - the generator is private by design; its output pattern is the contract
        assert re.fullmatch(r"[A-Z][a-z]{5}\d{2}[!?#$%&*+\-=@]", pw)
        # Must satisfy the same length policy the change endpoint enforces.
        RequiredPasswordChangeRequest(new_password=pw)


def test_fresh_schema_has_must_change_password_default_zero(conn):
    column = next(row for row in conn.execute("PRAGMA table_info(users)") if row["name"] == "must_change_password")
    assert column["notnull"] == 1
    assert column["dflt_value"] == "0"
    user_id = sqlite_db.create_user(conn, username="regular", password_hash=_STUB_HASH, is_admin=False)
    assert sqlite_db.get_user_by_id(conn, user_id)["must_change_password"] == 0


def test_pending_change_blocks_api_but_not_change_or_logout(conn):
    user_id = sqlite_db.create_user(
        conn, username="pending", password_hash=hash_password("Temp-pass-1"), is_admin=False, must_change_password=True
    )
    creds = _issue(conn, user_id)
    with pytest.raises(HTTPException) as blocked:
        get_current_user(_request("/me"), credentials=creds, conn=conn)
    assert blocked.value.status_code == 428
    for path in ("/me/complete-required-change", "/logout"):
        assert get_current_user(_request(path), credentials=creds, conn=conn)["id"] == user_id


def test_complete_required_change_flow(conn):
    old = "Temp-pass-1"
    user_id = sqlite_db.create_user(
        conn, username="pending", password_hash=hash_password(old), is_admin=False, must_change_password=True
    )
    creds = _issue(conn, user_id)
    state = SimpleNamespace(bootstrap_gate_active=True, bootstrap_gate_release=None)
    released = []
    state.bootstrap_gate_release = lambda: released.append(True)
    handler = users_api.complete_required_password_change.__wrapped__

    def call(new_password: str):
        user = get_current_user(_request("/me/complete-required-change"), credentials=creds, conn=conn)
        return handler(
            _request("/me/complete-required-change", state=state, method="POST"),
            RequiredPasswordChangeRequest(new_password=new_password),
            user,
            conn,
        )

    with pytest.raises(HTTPException) as same:
        call(old)
    assert same.value.status_code == 422
    assert not released

    response = call("Brand-new-pass-2")
    assert response.status_code == 200
    assert released == [True]
    row = sqlite_db.get_user_by_id(conn, user_id)
    assert row["must_change_password"] == 0
    assert verify_password("Brand-new-pass-2", row["password_hash"])
    # All sessions, including the one that did the change, are revoked.
    with pytest.raises(HTTPException) as revoked:
        get_current_user(_request("/me"), credentials=creds, conn=conn)
    assert revoked.value.status_code == 401


def test_complete_required_change_rejected_without_flag(conn):
    user_id = sqlite_db.create_user(
        conn, username="regular", password_hash=hash_password("Old-pass-123"), is_admin=False
    )
    creds = _issue(conn, user_id)
    user = get_current_user(_request("/me"), credentials=creds, conn=conn)
    with pytest.raises(HTTPException) as exc:
        users_api.complete_required_password_change.__wrapped__(
            _request("/me/complete-required-change", state=SimpleNamespace(), method="POST"),
            RequiredPasswordChangeRequest(new_password=_OTHER_PASSWORD),
            user,
            conn,
        )
    assert exc.value.status_code == 403


def test_admin_password_reset_sets_must_change(conn):
    user_id = sqlite_db.create_user(
        conn, username="member", password_hash=hash_password("Old-pass-123"), is_admin=False
    )
    assert sqlite_db.update_user_password(conn, user_id, hash_password("Reset-pass-1"), must_change_password=True)
    assert sqlite_db.get_user_by_id(conn, user_id)["must_change_password"] == 1


def test_update_user_password_compare_and_swap(conn):
    user_id = sqlite_db.create_user(conn, username="member", password_hash=_STUB_HASH, is_admin=False)
    assert sqlite_db.update_user_password(conn, user_id, "h2", expected_hash="stale") is False
    assert sqlite_db.update_user_password(conn, user_id, "h2", expected_hash=_STUB_HASH) is True


def test_login_response_carries_must_change_flag(monkeypatch):
    with TemporaryDirectory(prefix="justup-login-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        app = create_app()
        connection = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(connection)
            sqlite_db.create_user(
                connection,
                username="pending",
                password_hash=hash_password("Temp-pass-1"),
                is_admin=False,
                must_change_password=True,
            )
            sqlite_db.create_user(
                connection, username="regular", password_hash=hash_password("Reg-pass-123"), is_admin=False
            )
        finally:
            sqlite_db.close_connection(connection)

        client = TestClient(app)
        client.get("/ui/login")
        csrf = client.cookies.get("csrf_token")
        headers = {"X-CSRF-Token": csrf}
        pending = client.post("/login", json={"username": "pending", "password": "Temp-pass-1"}, headers=headers)
        assert pending.status_code == 200
        assert pending.json()["must_change_password"] is True
        regular = client.post(
            "/login",
            json={"username": "regular", "password": "Reg-pass-123"},
            headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        )
        assert regular.status_code == 200
        assert "must_change_password" not in regular.json()


def test_bootstrap_gate_returns_423_until_released(monkeypatch):
    with TemporaryDirectory(prefix="justup-gate-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import _release_bootstrap_gate, create_app

        app = create_app()
        app.state.bootstrap_gate_active = True
        app.state.bootstrap_gate_completed = False
        app.state.bootstrap_gate_release = _release_bootstrap_gate(app)
        connection = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(connection)
        finally:
            sqlite_db.close_connection(connection)

        client = TestClient(app)
        blocked = client.get("/api/meta")
        assert blocked.status_code == 423
        assert blocked.headers["Retry-After"]
        ui = client.get("/ui/targets", follow_redirects=False)
        assert ui.status_code == 302
        assert ui.headers["location"] == "/ui/login"
        assert client.get("/ui/login").status_code == 200
        assert client.get("/health").status_code != 423

        app.state.bootstrap_gate_release()
        assert app.state.bootstrap_gate_active is False
        assert client.get("/api/meta").status_code != 423


# --- initial password is shown on every start until it was changed ------------------------------


def test_credentials_file_is_private_and_banner_repeats_every_start(conn, pending_file, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        assert sqlite_db.ensure_default_admin(conn) is True
        first = _shown_password(capsys.readouterr().out)
        assert first
        assert pending_file.stat().st_mode & 0o777 == 0o600
        for _ in range(2):  # two further starts, password still unchanged
            assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is True
            out = capsys.readouterr().out
            assert _shown_password(out) == first
            assert "disappears for good after the password was changed" in out
    assert first not in "\n".join(r.getMessage() for r in caplog.records)
    row = sqlite_db.get_user_by_username(conn, "admin")
    assert first not in str(tuple(row))  # the database holds only the hash
    assert pending_file.name not in BACKUP_DIRECTORIES
    assert pending_file.parent.name == "data"  # data root, outside sqlite/tsdb/pdf_reports


@pytest.mark.usefixtures("pending_file")
def test_login_alone_keeps_showing_the_password(conn, capsys):
    sqlite_db.ensure_default_admin(conn)
    secret = _shown_password(capsys.readouterr().out)
    admin = sqlite_db.get_user_by_username(conn, "admin")
    _issue(conn, int(admin["id"]))
    sqlite_db.update_user_last_login(conn, int(admin["id"]), "127.0.0.1")
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is True
    assert _shown_password(capsys.readouterr().out) == secret


def test_required_password_change_through_the_api_removes_the_file(conn, pending_file, capsys):
    sqlite_db.ensure_default_admin(conn)
    capsys.readouterr()
    admin = sqlite_db.get_user_by_username(conn, "admin")
    creds = _issue(conn, int(admin["id"]))
    user = get_current_user(_request("/me/complete-required-change"), credentials=creds, conn=conn)
    response = users_api.complete_required_password_change.__wrapped__(
        _request("/me/complete-required-change", state=SimpleNamespace(bootstrap_gate_release=None), method="POST"),
        RequiredPasswordChangeRequest(new_password=_OTHER_PASSWORD),
        user,
        conn,
    )
    assert response.status_code == 200
    assert not pending_file.exists()
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert capsys.readouterr().out == ""


def test_password_change_by_other_means_is_not_shown_and_cleans_up(conn, pending_file, capsys):
    sqlite_db.ensure_default_admin(conn)
    capsys.readouterr()
    admin = sqlite_db.get_user_by_username(conn, "admin")
    # justup-cli style reset: raw SQL, no application hook runs
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(_OTHER_PASSWORD), admin["id"]))
    conn.commit()
    assert pending_file.exists()
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert capsys.readouterr().out == ""
    assert not pending_file.exists()


def test_update_user_password_removes_the_pending_file(conn, pending_file, capsys):
    sqlite_db.ensure_default_admin(conn)
    capsys.readouterr()
    admin = sqlite_db.get_user_by_username(conn, "admin")
    other = sqlite_db.create_user(conn, username="bob", password_hash=hash_password("Bob-pass-123"), is_admin=False)
    sqlite_db.update_user_password(conn, other, hash_password("Bob-pass-456"))
    assert pending_file.exists()  # another account does not touch it
    sqlite_db.update_user_password(conn, int(admin["id"]), hash_password(_OTHER_PASSWORD))
    assert not pending_file.exists()


def test_untrusted_credential_files_are_never_printed(conn, pending_file, capsys):
    sqlite_db.ensure_default_admin(conn)
    capsys.readouterr()
    pending_file.chmod(0o644)  # readable by others: not trusted
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert not pending_file.exists()

    pending_file.write_text("not json")
    pending_file.chmod(0o600)
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert capsys.readouterr().out == ""
    assert not pending_file.exists()


def test_symlinked_credential_file_is_not_followed(conn, pending_file, tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.write_text('{"username": "admin", "password": "x"}')
    target.chmod(0o600)
    pending_file.symlink_to(target)
    assert bootstrap_credentials.load() is None
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert capsys.readouterr().out == ""


def test_stale_file_without_a_matching_account_is_removed(conn, pending_file, capsys):
    assert bootstrap_credentials.store("admin", "Orphan12!")  # e.g. the account insert never committed
    assert sqlite_db.reprint_pending_bootstrap_credentials(conn) is False
    assert not pending_file.exists()
    assert capsys.readouterr().out == ""


def test_full_start_sequence_shows_until_the_password_is_changed(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    data_dir = tmp_path / "data"
    db_path, tsdb_dir = data_dir / "sqlite" / "justup.db", data_dir / "tsdb"
    db_path.parent.mkdir(parents=True)
    try:
        assert bootstrap.bootstrap(db_path, tsdb_dir, data_dir) is True
        secret = _shown_password(capsys.readouterr().out)
        assert secret and (data_dir / bootstrap_credentials.FILE_NAME).stat().st_mode & 0o777 == 0o600
        for _ in range(2):  # restarts without a change
            assert bootstrap.bootstrap(db_path, tsdb_dir, data_dir) is False
            assert _shown_password(capsys.readouterr().out) == secret
        conn = sqlite_db.connect(db_path)
        try:
            admin = sqlite_db.get_user_by_username(conn, "admin")
            sqlite_db.update_user_password(
                conn, int(admin["id"]), hash_password(_OTHER_PASSWORD), must_change_password=False
            )
        finally:
            sqlite_db.close_connection(conn)
        assert not (data_dir / bootstrap_credentials.FILE_NAME).exists()
        assert bootstrap.bootstrap(db_path, tsdb_dir, data_dir) is False
        assert _shown_password(capsys.readouterr().out) is None
    finally:
        bootstrap_credentials.configure(None)


def test_leftover_temp_file_of_a_killed_store_is_cleaned_and_does_not_block(pending_file):
    leftover = pending_file.with_name(f"{pending_file.name}.deadbeef.tmp")
    leftover.write_text("secret")
    assert bootstrap_credentials.store("admin", "Fresh12!")  # a stale temp name never blocks a new store
    bootstrap_credentials.discard()
    assert not pending_file.exists() and not leftover.exists()


def test_store_removes_temp_file_when_replace_fails(tmp_path, monkeypatch, caplog):
    target = tmp_path / bootstrap_credentials.FILE_NAME

    def _boom(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", _boom)
    with caplog.at_level(logging.WARNING):
        assert bootstrap_credentials.store("admin", "s3cret-SENTINEL", target) is False
    assert list(tmp_path.iterdir()) == []
    assert "SENTINEL" not in caplog.text


def test_cli_reset_prints_no_password_when_the_transaction_fails(tmp_path, monkeypatch, capsys):
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    from pathlib import Path

    from app.db import sqlite as sqlite_db

    db_path = tmp_path / "justup.db"
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    sqlite_db.create_user(conn, username="root", password_hash="x", is_admin=True)  # noqa: S106
    conn.commit()
    conn.close()
    cli_path = Path(__file__).resolve().parents[1] / "app" / "justup-cli"
    loader = SourceFileLoader("justup_cli_failure_test", str(cli_path))
    cli = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("commit path failed")

    monkeypatch.setattr(cli.notifications, "notify_admin_password_changed", boom)
    with pytest.raises(RuntimeError):
        cli.reset_password("admin")
    assert "New password" not in capsys.readouterr().out
