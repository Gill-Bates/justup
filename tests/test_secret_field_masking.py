#!/usr/bin/env python3
#
# tests/test_secret_field_masking.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write-only secret fields: the UI shows a fixed mask, the stored value never reaches the browser."""

# ruff: noqa: ARG005
from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import frontend, settings
from app.api.auth import require_admin
from app.db import sqlite as sqlite_db
from app.utils import crypto
from app.utils.constants import SettingKeys
from app.utils.crypto import decrypt_secret, encrypt_secret
from app.utils.deps import get_conn
from tests import _support  # noqa: F401 - imported for its sys.path side effect

DUMMY = "Zq7-dummy-secret-Xk9"
MASK = "\u2022" * 8


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture
def conn(tmp_path: Path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


@pytest.fixture
def client(conn, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda request, conn: {"id": 1, "is_admin": 1})
    app = FastAPI()
    app.include_router(settings.router)
    app.include_router(frontend.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "is_admin": 1}
    return TestClient(app)


def _input_tag(html: str, field_id: str) -> str:
    return re.search(rf'<input[^>]*id="{field_id}"[^>]*>', html).group(0)


def _clear_button(html: str, button_id: str) -> str:
    return re.search(rf'<button[^>]*id="{button_id}"[^>]*>', html).group(0)


def _stored_smtp_password(conn) -> str | None:
    stored = sqlite_db.get_setting(conn, SettingKeys.SMTP_PASSWORD)
    return decrypt_secret(stored) if stored else None


def test_stored_smtp_password_is_masked_and_never_rendered(conn, client):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))
    stored = sqlite_db.get_setting(conn, SettingKeys.SMTP_PASSWORD)

    resp = client.get("/ui/fragments/settings-smtp")

    assert resp.status_code == 200
    assert DUMMY not in resp.text and stored not in resp.text
    field = _input_tag(resp.text, "smtp_password")
    assert 'type="password"' in field and 'value=""' in field
    assert f'placeholder="{MASK}"' in field
    assert 'autocomplete="new-password"' in field
    assert 'aria-describedby="smtp_password-help"' in field
    assert "A password is stored." in resp.text
    assert "hidden" not in _clear_button(resp.text, "clear-smtp-password")


def test_mask_does_not_depend_on_password_length(conn, client):
    bodies = []
    for secret in ("a", DUMMY * 8):
        sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(secret))
        bodies.append(_input_tag(client.get("/ui/fragments/settings-smtp").text, "smtp_password"))
    assert bodies[0] == bodies[1]


def test_no_stored_smtp_password_renders_empty_field_without_mask(client):
    resp = client.get("/ui/fragments/settings-smtp")

    field = _input_tag(resp.text, "smtp_password")
    assert 'placeholder=""' in field and MASK not in resp.text
    assert "No password stored." in resp.text
    assert "hidden" in _clear_button(resp.text, "clear-smtp-password")


def test_ip_api_key_uses_the_same_mask_and_never_renders_the_key(conn, client):
    sqlite_db.set_setting(conn, SettingKeys.IP_API_KEY, encrypt_secret(DUMMY))

    resp = client.get("/ui/fragments/settings-network")

    assert resp.status_code == 200, resp.text
    assert DUMMY not in resp.text
    assert f'placeholder="{MASK}"' in _input_tag(resp.text, "ip_api_key")
    assert "hidden" not in _clear_button(resp.text, "clear-ip-api-key")


def test_patch_without_password_keeps_it(conn, client):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))

    assert client.patch("/settings", json={"smtp_host": "smtp.example.com"}).status_code == 200

    assert _stored_smtp_password(conn) == DUMMY


def test_patch_with_new_password_replaces_and_response_hides_it(conn, client):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))

    resp = client.patch("/settings", json={"smtp_password": "new-" + DUMMY})

    assert resp.status_code == 200
    assert DUMMY not in resp.text
    assert _stored_smtp_password(conn) == "new-" + DUMMY


def test_patch_with_empty_password_removes_it(conn, client):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))

    assert client.patch("/settings", json={"smtp_password": ""}).status_code == 200

    assert _stored_smtp_password(conn) is None
    assert "No password stored." in client.get("/ui/fragments/settings-smtp").text


def test_password_made_of_mask_characters_is_stored_verbatim(conn, client):
    assert client.patch("/settings", json={"smtp_password": MASK}).status_code == 200

    assert _stored_smtp_password(conn) == MASK


def test_test_mail_does_not_touch_stored_password(conn, client, monkeypatch):
    for key, value in (
        (SettingKeys.SMTP_ENABLED, True),
        (SettingKeys.SMTP_HOST, "smtp.example.com"),
        (SettingKeys.SMTP_FROM, "alerts@example.com"),
    ):
        sqlite_db.set_setting(conn, key, value)
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))
    queued = []

    class FakeSpooler:
        def enqueue(self, *args, **kwargs):
            queued.append((args, kwargs))
            return 1

    from app.services.notifications import spooler as spooler_module

    monkeypatch.setattr(spooler_module, "get_spooler", lambda _factory: FakeSpooler())
    client.app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    resp = client.post("/settings/smtp/test-mail", json={"recipient": "you@example.com"})

    assert resp.status_code == 200, resp.text
    assert DUMMY not in resp.text
    assert _stored_smtp_password(conn) == DUMMY
