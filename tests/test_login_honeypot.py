#!/usr/bin/env python3
#
# tests/test_login_honeypot.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Login honeypot: hidden form field that bots fill; hits feed the existing IP lockout."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.api import events, users
from app.db import sqlite as sqlite_db
from app.models.users import hash_password
from app.utils import crypto
from app.utils.auth_helpers import resolve_user_optional
from app.utils.deps import get_conn
from app.utils.rate_limit import RateLimitExceededError, rate_limit_exceeded_handler, reset_all
from tests import _support  # noqa: F401 - imported for its sys.path side effect

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "Correct-horse-1"  # noqa: S105 - test fixture


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    sqlite_db.create_user(connection, username="alice", password_hash=hash_password(PASSWORD), is_admin=False)
    reset_all()
    yield connection
    sqlite_db.close_connection(connection)


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
    app.include_router(users.router)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app)


def _attempts(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]


def test_honeypot_hit_rejects_valid_credentials_and_locks_ip(client, conn):
    resp = client.post("/login", json={"username": "alice", "password": PASSWORD, "website": "http://spam"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"
    assert _attempts(conn) == 1
    # The ban persists in SQLite and now also blocks the legitimate credentials.
    assert client.post("/login", json={"username": "alice", "password": PASSWORD}).status_code == 429


def test_empty_or_missing_honeypot_leaves_login_unchanged(client, conn):
    assert client.post("/login", json={"username": "alice", "password": PASSWORD, "website": ""}).status_code == 200
    assert client.post("/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert _attempts(conn) == 0


@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.178.20"])
def test_honeypot_hit_from_private_ip_is_logged_but_never_locks(client, conn, monkeypatch, caplog, ip):
    monkeypatch.setattr(users, "_get_client_ip", lambda _request: ip)
    with caplog.at_level("WARNING", logger=users.__name__):
        resp = client.post("/login", json={"username": "alice", "password": PASSWORD, "website": "x"})
    assert resp.status_code == 401
    assert _attempts(conn) == 0
    assert any(r.levelname == "WARNING" and "LOGIN_HONEYPOT" in r.getMessage() for r in caplog.records)
    assert client.post("/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


@pytest.mark.parametrize("ip", ["172.32.0.1", "172.15.0.1", "8.8.8.8", "not-an-ip"])
def test_honeypot_hit_from_public_ip_locks_with_exponential_backoff(client, conn, monkeypatch, caplog, ip):
    monkeypatch.setattr(users, "_get_client_ip", lambda _request: ip)
    with caplog.at_level("WARNING", logger=users.__name__):
        first = client.post("/login", json={"username": "alice", "password": PASSWORD, "website": "x"})
    assert first.status_code == 401
    assert first.headers["Retry-After"] == "2"
    assert any(r.levelname == "WARNING" and "LOGIN_HONEYPOT" in r.getMessage() for r in caplog.records)
    # Simulate the lockout expiring, then a second hit doubles the lockout.
    conn.execute("UPDATE login_attempts SET locked_until = NULL")
    conn.commit()
    second = client.post("/login", json={"username": "alice", "password": PASSWORD, "website": "x"})
    assert second.headers["Retry-After"] == "4"


def test_login_template_hides_honeypot_from_humans_and_assistive_tech():
    html = (ROOT / "app/templates/login.html").read_text(encoding="utf-8")
    assert 'aria-hidden="true">\n\t\t\t\t\t<label for="website"' in html
    assert 'name="website" tabindex="-1" autocomplete="off"' in html
    assert "'website'" in (ROOT / "app/static/js/login.js").read_text(encoding="utf-8")


def _login_ok(client):
    return client.post("/login", json={"username": "alice", "password": PASSWORD})


def test_new_token_lifetime_follows_session_max_days_setting(client, conn):
    from app.models.users import hash_token

    def lifetime_hours() -> float:
        resp = _login_ok(client)
        assert resp.status_code == 200
        row = conn.execute(
            "SELECT created_at, max_expires_at FROM auth_tokens WHERE token_hash = ?",
            (hash_token(resp.json()["access_token"]),),
        ).fetchone()
        return (row["max_expires_at"] - row["created_at"]).total_seconds() / 3600, resp

    hours, resp = lifetime_hours()
    assert round(hours) == 24  # default unchanged
    assert f"Max-Age={86400}" in resp.headers["set-cookie"]

    sqlite_db.set_setting(conn, "session_max_days", 3)
    conn.execute("DELETE FROM login_attempts")
    hours, resp = lifetime_hours()
    assert round(hours) == 72
    assert f"Max-Age={3 * 86400}" in resp.headers["set-cookie"]

    # Out-of-range stored values are clamped on read.
    sqlite_db.set_setting(conn, "session_max_days", 99)
    assert sqlite_db.get_session_max_days(conn) == 7


def _shift_token_clock(conn, delta: timedelta) -> None:
    """Simulate elapsed time by moving every stored token timestamp into the past."""
    for row in conn.execute("SELECT id, created_at, expires_at, max_expires_at FROM auth_tokens").fetchall():
        conn.execute(
            "UPDATE auth_tokens SET created_at = ?, expires_at = ?, max_expires_at = ? WHERE id = ?",
            (row["created_at"] - delta, row["expires_at"] - delta, row["max_expires_at"] - delta, row["id"]),
        )
    conn.commit()


def _me(client, token: str) -> int:
    return client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code


@pytest.mark.parametrize("days", [1, 7])
def test_session_stays_valid_for_the_configured_days_without_activity(client, conn, days):
    sqlite_db.set_setting(conn, "session_max_days", days)
    resp = _login_ok(client)
    token = resp.json()["access_token"]
    row = conn.execute("SELECT created_at, expires_at, max_expires_at FROM auth_tokens").fetchone()
    # No hidden shorter idle window: the server-side expiry equals the configured lifetime.
    assert row["expires_at"] == row["max_expires_at"]
    assert abs((row["max_expires_at"] - row["created_at"]) - timedelta(days=days)) < timedelta(seconds=1)
    assert f"Max-Age={days * 86400}" in resp.headers["set-cookie"]
    # Idle for longer than the former 6 h window, with no request in between.
    _shift_token_clock(conn, timedelta(days=days) - timedelta(hours=1))
    assert _me(client, token) == 200
    _shift_token_clock(conn, timedelta(hours=2))
    assert _me(client, token) == 401


def test_session_survives_process_restart_with_same_secret_key(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    db_file = tmp_path / "db" / "justup.db"

    def boot():
        monkeypatch.setenv("SECRET_KEY", key)
        monkeypatch.setattr(crypto, "_primary_key", None)  # a new process derives the pepper afresh
        connection = sqlite_db.connect(db_file)
        sqlite_db.init_schema(connection)
        app = FastAPI()
        app.include_router(users.router)
        app.dependency_overrides[get_conn] = lambda: connection
        return connection, TestClient(app)

    first_conn, first_client = boot()
    sqlite_db.create_user(first_conn, username="alice", password_hash=hash_password(PASSWORD), is_admin=False)
    sqlite_db.set_setting(first_conn, "session_max_days", 7)
    reset_all()
    token = _login_ok(first_client).json()["access_token"]
    sqlite_db.close_connection(first_conn)
    second_conn, second_client = boot()
    try:
        assert _me(second_client, token) == 200
        # A different SECRET_KEY invalidates the stored token hashes (documented operator contract).
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.setattr(crypto, "_primary_key", None)
        assert _me(second_client, token) == 401
    finally:
        sqlite_db.close_connection(second_conn)


def test_logout_and_password_change_still_end_a_long_session(client, conn):
    sqlite_db.set_setting(conn, "session_max_days", 7)
    token = _login_ok(client).json()["access_token"]
    assert _me(client, token) == 200
    assert client.post("/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert _me(client, token) == 401
    conn.execute("DELETE FROM login_attempts")
    token = _login_ok(client).json()["access_token"]
    resp = client.post(
        "/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "Another-horse-2"},
    )
    assert resp.status_code == 200, resp.text
    assert _me(client, token) == 401


def test_deactivated_user_loses_a_long_session(client, conn):
    sqlite_db.set_setting(conn, "session_max_days", 7)
    token = _login_ok(client).json()["access_token"]
    conn.execute("UPDATE users SET is_active = 0")
    conn.commit()
    assert _me(client, token) == 401


def _cookie_probe_client(conn) -> TestClient:
    """Test app with one route that resolves the user like the HTML pages do (cookie allowed)."""
    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[get_conn] = lambda: conn

    @app.get("/cookie-probe")
    def cookie_probe(request: Request, db=Depends(get_conn)):
        return {"ok": resolve_user_optional(request, db, allow_cookie=True) is not None}

    return TestClient(app)


@pytest.mark.parametrize("idle", [timedelta(hours=1, minutes=1), timedelta(hours=6, minutes=1), timedelta(hours=23)])
def test_cookie_session_survives_idle_periods_longer_than_one_and_six_hours(conn, idle):
    sqlite_db.set_setting(conn, "session_max_days", 1)
    probe = _cookie_probe_client(conn)
    assert _login_ok(probe).status_code == 200  # the client keeps the HttpOnly session cookie
    assert probe.get("/cookie-probe").json() == {"ok": True}
    _shift_token_clock(conn, idle)
    assert probe.get("/cookie-probe").json() == {"ok": True}
    # The live-event stream validates without renewing and must agree.
    token = probe.cookies.get("token")
    assert events.check_token(conn_path(conn), token) is not None


def conn_path(conn) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()["file"])


def test_expired_cookie_session_is_rejected_after_the_configured_days(conn):
    sqlite_db.set_setting(conn, "session_max_days", 1)
    probe = _cookie_probe_client(conn)
    assert _login_ok(probe).status_code == 200
    _shift_token_clock(conn, timedelta(days=1, minutes=1))
    assert probe.get("/cookie-probe").json() == {"ok": False}


def test_secret_keys_rotation_only_the_primary_key_validates_sessions(tmp_path, monkeypatch):
    old, new = (Fernet.generate_key().decode("ascii") for _ in range(2))

    def boot(secret_keys: str | None, secret_key: str | None):
        for name, value in (("SECRET_KEYS", secret_keys), ("SECRET_KEY", secret_key)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        monkeypatch.setattr(crypto, "_primary_key", None)

    boot(None, old)
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    sqlite_db.create_user(connection, username="alice", password_hash=hash_password(PASSWORD), is_admin=False)
    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[get_conn] = lambda: connection
    client = TestClient(app)
    reset_all()
    try:
        token = _login_ok(client).json()["access_token"]
        assert _me(client, token) == 200
        boot(f"{old},{new}", old)  # old stays primary: sessions keep working
        assert _me(client, token) == 200
        boot(f"{new},{old}", old)  # rotation: the old key only decrypts, sessions end
        assert _me(client, token) == 401
        boot(f"{old},{new}", None)  # SECRET_KEYS alone configures the primary as well
        assert _me(client, token) == 200
    finally:
        sqlite_db.close_connection(connection)


def test_authorization_header_never_falls_back_to_a_valid_session_cookie(conn):
    """A stale localStorage bearer or a proxy-forwarded Basic header beats the cookie and ends in 401."""
    probe = _cookie_probe_client(conn)
    assert _login_ok(probe).status_code == 200
    assert probe.get("/cookie-probe").json() == {"ok": True}
    assert probe.get("/cookie-probe", headers={"Authorization": "Bearer stale-token"}).json() == {"ok": False}
    assert probe.get("/cookie-probe", headers={"Authorization": "Basic dXNlcjpwdw=="}).json() == {"ok": False}
    assert probe.get("/cookie-probe", headers={"Authorization": ""}).json() == {"ok": True}


# --- failed-login lockout: one increment, 2^n Retry-After, distinct reasons ----------------------
def _fail_count(conn) -> int:
    row = conn.execute("SELECT failed_count FROM login_attempts").fetchone()
    return int(row["failed_count"]) if row else 0


@pytest.mark.parametrize("reason", ["user_not_found", "user_inactive", "wrong_password"])
def test_failed_login_records_once_and_doubles_retry_after(client, conn, caplog, reason):
    if reason == "user_inactive":
        conn.execute("UPDATE users SET is_active = 0 WHERE username = 'alice'")
        conn.commit()
    body = {
        "user_not_found": {"username": "nobody", "password": PASSWORD},
        "user_inactive": {"username": "alice", "password": PASSWORD},
        "wrong_password": {"username": "alice", "password": "wrong-Password-9"},
    }[reason]
    with caplog.at_level("INFO", logger=users.__name__):
        first = client.post("/login", json=body)
    assert first.status_code == 401
    assert first.json()["detail"] == "Invalid credentials"
    assert first.headers["Retry-After"] == "2"
    assert _fail_count(conn) == 1
    assert any(f"reason={reason}" in r.getMessage() for r in caplog.records)
    conn.execute("UPDATE login_attempts SET locked_until = NULL")
    conn.commit()
    second = client.post("/login", json=body)
    assert second.headers["Retry-After"] == "4"
    assert _fail_count(conn) == 2


def test_failed_login_lockout_is_capped_at_one_hour(client, conn):
    body = {"username": "nobody", "password": PASSWORD}
    assert client.post("/login", json=body).status_code == 401
    conn.execute("UPDATE login_attempts SET failed_count = 40, locked_until = NULL")
    conn.commit()
    capped = client.post("/login", json=body)
    assert capped.status_code == 401
    assert capped.headers["Retry-After"] == "3600"
