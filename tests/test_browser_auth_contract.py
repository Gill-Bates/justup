#!/usr/bin/env python3
#
# tests/test_browser_auth_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Browser auth contract: session cookie + Origin + CSRF on every browser route class, bearer unchanged."""

from __future__ import annotations

from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.models.users import hash_token, new_token
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect

# One representative write per browser route class (JS apiFetch, HTMX, form-less JSON, admin action).
WRITES = [
    pytest.param("PATCH", "/me/theme", {"theme": "dark"}, 200, id="json-patch-me-theme"),
    pytest.param("POST", "/ui/notifications/read-all", None, 200, id="json-post-notifications"),
    pytest.param("DELETE", "/recipients/999999", None, 404, id="json-delete-recipient"),
    pytest.param("DELETE", "/targets/999999", None, 404, id="htmx-delete-target"),
    pytest.param("POST", "/settings/smtp/template/reset", None, 200, id="admin-post-smtp-template"),
    pytest.param("POST", "/logout", None, 200, id="logout"),
]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    with TemporaryDirectory(prefix="justup-authcontract-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(conn)
            user_id = sqlite_db.create_user(conn, username="boss", password_hash="stub-hash", is_admin=True)  # noqa: S106
            token, expires_at, max_expires_at = new_token()
            sqlite_db.issue_token(
                conn,
                user_id=user_id,
                token_hash=hash_token(token),
                expires_at=expires_at,
                max_expires_at=max_expires_at,
            )
        finally:
            sqlite_db.close_connection(conn)
        yield app, token


def _browser(app, token: str) -> TestClient:
    """A browser-like client: session cookie plus the csrf_token cookie a page load sets."""
    client = TestClient(app, base_url="http://testserver")
    client.get("/ui/login")
    client.cookies.set("token", token)
    return client


def _send(client: TestClient, method: str, url: str, body, *, csrf: bool = True, origin: bool = True):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = client.cookies.get("csrf_token")
    if origin:
        headers["Origin"] = "http://testserver"
    return client.request(method, url, json=body, headers=headers)


@pytest.mark.parametrize(("method", "url", "body", "ok"), WRITES)
def test_cookie_write_needs_origin_and_csrf_and_works_with_both(env, method, url, body, ok):
    app, token = env
    for csrf, origin in ((False, True), (True, False), (False, False)):
        blocked = _send(_browser(app, token), method, url, body, csrf=csrf, origin=origin)
        assert blocked.status_code == 403, (csrf, origin)
    wrong = _browser(app, token)
    assert (
        wrong.request(method, url, json=body, headers={"X-CSRF-Token": "x", "Origin": "http://testserver"}).status_code
        == 403
    )
    cross = _browser(app, token)
    assert (
        cross.request(
            method,
            url,
            json=body,
            headers={"X-CSRF-Token": cross.cookies.get("csrf_token"), "Origin": "http://evil.example"},
        ).status_code
        == 403
    )
    assert _send(_browser(app, token), method, url, body).status_code == ok


# /ui/*, /me/theme and /logout are browser-only paths: CSRF applies to them whatever the credential.
API_ONLY = {"json-delete-recipient", "htmx-delete-target", "admin-post-smtp-template"}


@pytest.mark.parametrize(("method", "url", "body", "ok"), [p.values for p in WRITES if p.id in API_ONLY])
def test_bearer_client_needs_neither_csrf_nor_origin(env, method, url, body, ok):
    app, token = env
    client = TestClient(app)
    assert client.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"}).status_code == ok


def test_invalid_bearer_never_falls_back_to_a_valid_session_cookie(env):
    app, token = env
    client = _browser(app, token)
    resp = client.request(
        "PATCH",
        "/me/theme",
        json={"theme": "dark"},
        headers={"Authorization": "Bearer invalid", "X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert resp.status_code == 401


def test_cookie_reads_need_no_csrf_and_writes_keep_the_token_stable(env):
    app, token = env
    client = _browser(app, token)
    assert client.get("/me").status_code == 200
    assert client.get("/ui/notifications/summary").status_code == 200
    assert client.get("/ui/events/session").status_code == 200
    before = client.cookies.get("csrf_token")
    assert _send(client, "PATCH", "/me/theme", {"theme": "light"}).status_code == 200
    assert client.cookies.get("csrf_token") == before
    assert _send(client, "PATCH", "/me/theme", {"theme": "dark"}).status_code == 200
    assert client.cookies.get("csrf_token") == before


def test_login_requires_csrf_but_not_a_session(env):
    app, _ = env
    client = TestClient(app, base_url="http://testserver")
    client.get("/ui/login")
    body = {"username": "boss", "password": "wrong"}
    assert client.post("/login", json=body, headers={"Origin": "http://testserver"}).status_code == 403
    headers = {"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("csrf_token")}
    assert client.post("/login", json=body, headers=headers).status_code == 401
