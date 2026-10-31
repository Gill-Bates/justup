#!/usr/bin/env python3
#
# tests/test_theme_preference.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Theme schema and cookie-session contracts."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.api import users as users_api
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.middleware.csrf import CSRFMiddleware
from app.models.users import hash_token, new_token
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _create_user(conn: sqlite3.Connection, username: str) -> int:
    return sqlite_db.create_user(conn, username=username, password_hash=secrets.token_hex(16), is_admin=False)


def test_fresh_schema_has_system_theme_and_rejects_invalid_values() -> None:
    with TemporaryDirectory(prefix="justup-theme-") as temp_dir:
        conn = sqlite_db.connect(Path(temp_dir) / "fresh.sqlite3")
        try:
            sqlite_db.init_schema(conn)
            theme_column = next(row for row in conn.execute("PRAGMA table_info(users)") if row["name"] == "theme")
            assert theme_column["dflt_value"] == "'system'"
            user_id = _create_user(conn, "first")
            assert sqlite_db.get_user_theme(conn, user_id) == "system"
            try:
                sqlite_db.set_user_theme(conn, user_id, "sepia")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("Invalid theme bypassed the database constraint")
            assert sqlite_db.get_user_theme(conn, user_id) == "system"
        finally:
            sqlite_db.close_connection(conn)


@pytest.mark.asyncio
async def test_cookie_theme_route_requires_csrf_and_isolates_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    with TemporaryDirectory(prefix="justup-theme-") as temp_dir:
        conn = sqlite_db.connect(Path(temp_dir) / "sessions.sqlite3")
        try:
            sqlite_db.init_schema(conn)
            first = _create_user(conn, "first")
            second = _create_user(conn, "second")
            token, expires_at, max_expires_at = new_token()
            sqlite_db.issue_token(
                conn,
                user_id=first,
                token_hash=hash_token(token),
                expires_at=expires_at,
                max_expires_at=max_expires_at,
            )

            def request(path: str, *, method: str = "GET", csrf: str | None = None) -> Request:
                headers = [(b"cookie", f"token={token}; csrf_token=csrf-value".encode()), (b"host", b"testserver")]
                if csrf:
                    headers.append((b"x-csrf-token", csrf.encode()))
                return Request(
                    {
                        "type": "http",
                        "method": method,
                        "path": path,
                        "headers": headers,
                        "query_string": b"",
                        "server": ("testserver", 80),
                        "scheme": "http",
                    }
                )

            user = get_current_user(request("/me/theme"), credentials=None, conn=conn)
            assert users_api.get_user_theme(user=user, conn=conn) == {"theme": "system"}
            with pytest.raises(HTTPException) as denied:
                get_current_user(request("/me"), credentials=None, conn=conn)
            assert denied.value.status_code == 401

            async def ignored_asgi_app(_scope, _receive, _send) -> None:
                return None

            middleware = CSRFMiddleware(ignored_asgi_app)

            async def continue_request(_request: Request) -> JSONResponse:
                return JSONResponse({"ok": True})

            denied_patch = await middleware.dispatch(request("/me/theme", method="PATCH"), continue_request)
            assert denied_patch.status_code == 403
            allowed_patch = await middleware.dispatch(
                request("/me/theme", method="PATCH", csrf="csrf-value"), continue_request
            )
            assert allowed_patch.status_code == 200
            assert users_api.update_user_theme({"theme": "dark"}, user=user, conn=conn) == {"theme": "dark"}
            assert sqlite_db.get_user_theme(conn, first) == "dark"
            assert sqlite_db.get_user_theme(conn, second) == "system"

            with pytest.raises(HTTPException) as invalid:
                users_api.update_user_theme({"theme": "sepia"}, user=user, conn=conn)
            assert invalid.value.status_code == 400
            assert sqlite_db.get_user_theme(conn, first) == "dark"
            bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
            assert get_current_user(request("/me"), credentials=bearer, conn=conn)["id"] == first
        finally:
            sqlite_db.close_connection(conn)
