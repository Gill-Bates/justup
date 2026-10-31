#!/usr/bin/env python3
#
# tests/test_login_honeypot.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Login honeypot: hidden form field that bots fill; hits feed the existing IP lockout."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import users
from app.db import sqlite as sqlite_db
from app.models.users import hash_password
from app.utils import crypto
from app.utils.deps import get_conn
from app.utils.rate_limit import limiter
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
    limiter.reset()
    yield connection
    sqlite_db.close_connection(connection)


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.state.limiter = limiter
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
