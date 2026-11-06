#!/usr/bin/env python3
#
# tests/test_smtp_tls_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SMTP encryption is the default: one reader of smtp_use_tls for UI, API and the send path.

Only an explicitly stored ``false`` is a decision against STARTTLS. A missing, null, invalid or
unparsable value never switches encryption off, and incomplete or undecryptable credentials
never degrade to an anonymous send.
"""

# ruff: noqa: ARG005
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import frontend, settings
from app.api.auth import get_current_user, require_admin
from app.db import sqlite as sqlite_db
from app.services.notifications import smtp_settings, smtp_transport
from app.utils import crypto
from app.utils.constants import DEFAULT_SMTP_USE_TLS, SettingKeys
from app.utils.crypto import encrypt_secret
from app.utils.deps import get_conn
from tests import _support  # noqa: F401 - imported for its sys.path side effect

DUMMY = "Zq7-dummy-secret-Xk9"
APP_ROOT = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    crypto.reset_fernet_cache()
    yield
    crypto.reset_fernet_cache()


@pytest.fixture
def conn(db_path: Path):
    connection = sqlite_db.connect(db_path)
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


def _store_raw(conn, value_json: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?, ?, 'x')",
        (SettingKeys.SMTP_USE_TLS.value, value_json),
    )
    conn.commit()


def _tls_input(html: str) -> str:
    return re.search(r'<input[^>]*id="smtp_use_tls"[^>]*>', html, re.DOTALL).group(0)


# --- effective default -------------------------------------------------------------------------


def test_default_is_enabled():
    assert DEFAULT_SMTP_USE_TLS is True


def test_new_database_without_key_requires_starttls(conn):
    assert smtp_settings.resolve_use_tls(conn) is True


def test_stored_true_requires_starttls(conn):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_USE_TLS, True)
    assert smtp_settings.resolve_use_tls(conn) is True


def test_explicit_false_survives_restart(db_path):
    first = sqlite_db.connect(db_path)
    sqlite_db.set_setting(first, SettingKeys.SMTP_USE_TLS, False)
    sqlite_db.close_connection(first)
    second = sqlite_db.connect(db_path)  # new process-like connection: nothing is re-seeded
    try:
        assert smtp_settings.resolve_use_tls(second) is False
    finally:
        sqlite_db.close_connection(second)


@pytest.mark.parametrize("raw", ["null", '"false"', '"off"', "0", "1", "[]", "{not json"])
def test_unusable_stored_value_is_never_a_decision_against_encryption(conn, raw, caplog):
    _store_raw(conn, raw)
    with caplog.at_level(logging.WARNING):
        assert smtp_settings.resolve_use_tls(conn) is True
    assert "STARTTLS stays required" in caplog.text


# --- API contract ------------------------------------------------------------------------------


def test_get_settings_reports_effective_tls_default_and_no_secret(client, conn):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_HOST, "mail.example.com")
    sqlite_db.set_setting(conn, SettingKeys.SMTP_USER, "alerts")
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, encrypt_secret(DUMMY))
    body = client.get("/settings").json()
    assert body["smtp_use_tls"] is True
    assert body["smtp_host"] == "mail.example.com"
    assert body["smtp_user"] == "alerts"
    assert body["smtp_password_set"] is True
    assert "smtp_password" not in body
    assert DUMMY not in client.get("/settings").text


def test_get_settings_and_fragment_agree_on_tls_state(client, conn):
    for stored, expect in ((None, True), (True, True), (False, False)):
        if stored is not None:
            sqlite_db.set_setting(conn, SettingKeys.SMTP_USE_TLS, stored)
        tag = _tls_input(client.get("/ui/fragments/settings-smtp").text)
        assert client.get("/settings").json()["smtp_use_tls"] is expect
        assert ("checked" in tag) is expect
        assert f'data-stored-value="{str(expect).lower()}"' in tag


def test_fragment_defaults_to_on_with_a_plain_starttls_label(client):
    html = client.get("/ui/fragments/settings-smtp").text
    assert "checked" in _tls_input(html)
    assert re.search(r'id="smtp_use_tls_label"[^>]*>\s*Use STARTTLS\s*</label>', html)


def test_fragment_on_port_465_labels_implicit_tls_locked_even_with_stored_false(client, conn):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_USE_TLS, False)
    sqlite_db.set_setting(conn, SettingKeys.SMTP_PORT, 465)
    html = client.get("/ui/fragments/settings-smtp").text
    tag = _tls_input(html)
    assert "checked" in tag
    assert "disabled" in tag
    assert 'data-stored-value="false"' in tag  # the saved choice returns when the port changes back
    assert re.search(r'id="smtp_use_tls_label"[^>]*>\s*Implicit TLS \(SMTPS\)\s*</label>', html)


def test_patch_false_is_stored_and_read_back_everywhere(client, conn):
    assert client.patch("/settings", json={"smtp_use_tls": False}).json()["smtp_use_tls"] is False
    assert sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS) is False
    assert client.get("/settings").json()["smtp_use_tls"] is False
    assert "checked" not in _tls_input(client.get("/ui/fragments/settings-smtp").text)


@pytest.mark.parametrize(
    "payload",
    [
        {"smtp_use_tls": None},
        {"smtp_use_tls": "maybe"},
        {"smtp_use_tls": []},
        {"smtp_use_tls": "off"},
        {"smtp_use_tls": 0},
        {"smtp_use_tls": "false"},
    ],
)
def test_rejected_patch_leaves_stored_state_unchanged(client, conn, payload):
    sqlite_db.set_setting(conn, SettingKeys.SMTP_USE_TLS, True)
    assert client.patch("/settings", json=payload).status_code == 422
    assert sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS) is True
    assert client.get("/settings").json()["smtp_use_tls"] is True


def test_patch_null_does_not_create_a_disabling_value(client, conn):
    assert client.patch("/settings", json={"smtp_use_tls": None}).status_code == 422
    assert sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, "absent") == "absent"
    assert smtp_settings.resolve_use_tls(conn) is True


def test_non_admin_cannot_disable_tls(conn):
    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: {"id": 2, "is_admin": 0}
    resp = TestClient(app).patch("/settings", json={"smtp_use_tls": False})
    assert resp.status_code == 403
    assert sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, "absent") == "absent"


def test_unauthenticated_cannot_read_or_change_smtp_settings(conn):
    def _deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = _deny
    anon = TestClient(app)
    assert anon.get("/settings").status_code == 401
    assert anon.patch("/settings", json={"smtp_use_tls": False}).status_code == 401


# --- send path ---------------------------------------------------------------------------------


def _configure_smtp(conn, **extra) -> None:
    sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, True)
    sqlite_db.set_setting(conn, SettingKeys.SMTP_HOST, "mail.example.com")
    sqlite_db.set_setting(conn, SettingKeys.SMTP_FROM, "alerts@example.com")
    for key, value in extra.items():
        sqlite_db.set_setting(conn, SettingKeys(key), value)


def test_send_path_builds_a_connection_with_starttls_required_by_default(conn):
    _configure_smtp(conn)
    config, sender = smtp_settings.load_connection(conn)
    assert (config.host, config.port, config.use_tls, sender) == ("mail.example.com", 587, True, "alerts@example.com")


def test_send_path_honours_the_explicit_compatibility_exception(conn):
    _configure_smtp(conn, smtp_use_tls=False, smtp_port=25)
    config, _ = smtp_settings.load_connection(conn)
    assert (config.port, config.use_tls, config.user, config.password) == (25, False, None, None)


def test_undecryptable_password_stops_the_send_instead_of_going_anonymous(conn, monkeypatch):
    stored = encrypt_secret(DUMMY)
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))  # key rotated away
    crypto.reset_fernet_cache()
    _configure_smtp(conn, smtp_use_tls=False, smtp_user="alerts", smtp_password=stored)
    with pytest.raises(smtp_transport.SMTPConfigurationError) as exc:
        smtp_settings.load_connection(conn)
    assert DUMMY not in str(exc.value)
    assert stored not in str(exc.value)


def test_user_without_stored_password_is_refused_even_with_tls_off(conn):
    _configure_smtp(conn, smtp_use_tls=False, smtp_port=25, smtp_user="alerts")
    config, _ = smtp_settings.load_connection(conn)
    with pytest.raises(smtp_transport.SMTPConfigurationError):
        smtp_transport.send_message(_message(), config)


@pytest.mark.parametrize(
    ("stored", "expected"), [(1, 1), (25, 25), (587, 587), (65535, 65535), ("2525", 2525), (587.0, 587)]
)
def test_stored_smtp_port_accepts_whole_numbers_in_range(conn, stored, expected):
    _configure_smtp(conn, smtp_port=stored)
    config, _ = smtp_settings.load_connection(conn)
    assert config.port == expected
    assert smtp_settings.load_public_smtp_settings(conn)["smtp_port"] == expected


@pytest.mark.parametrize("raw", [None, "null", '""', '"  "'])
def test_missing_smtp_port_uses_the_default(conn, raw):
    _configure_smtp(conn)
    if raw is not None:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?, ?, 'x')",
            (SettingKeys.SMTP_PORT.value, raw),
        )
        conn.commit()
    config, _ = smtp_settings.load_connection(conn)
    assert config.port == smtp_settings.DEFAULT_SMTP_PORT
    assert smtp_settings.load_public_smtp_settings(conn)["smtp_port"] is None


@pytest.mark.parametrize("stored", [0, 65536, -1, -587, True, False, 587.5, "abc", "5 87", "587.5", [587]])
def test_invalid_stored_smtp_port_is_a_configuration_error_not_a_silent_default(conn, stored, caplog):
    _configure_smtp(conn, smtp_port=stored)
    with pytest.raises(smtp_transport.SMTPConfigurationError, match="SMTP port"):
        smtp_settings.load_connection(conn)
    with caplog.at_level(logging.WARNING, logger=smtp_settings.__name__):
        assert smtp_settings.load_public_smtp_settings(conn)["smtp_port"] is None  # the UI keeps rendering
    assert "smtp_port is invalid" in caplog.text


def test_disabled_or_incomplete_smtp_is_not_activated_by_the_tls_default(conn):
    with pytest.raises(RuntimeError, match="not enabled"):
        smtp_settings.load_connection(conn)
    assert sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, "absent") == "absent"
    sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, True)
    with pytest.raises(RuntimeError, match="not configured"):
        smtp_settings.load_connection(conn)


def _message():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "s", "from@example.com", "to@example.com"
    msg.set_content("body")
    return msg


def test_every_smtp_connection_and_tls_decision_goes_through_the_shared_modules():
    """Alert mail and test mail share one spooler closure; no other module may open SMTP or re-decide TLS."""
    # Importing smtplib for its exception types is fine; constructing a client or importing names from it is not.
    opener = re.compile(
        r"^\s*from smtplib import|^\s*import smtplib\b(?=[\s\S]*\b(?:SMTP(?:_SSL)?|LMTP)\s*\()",
        re.MULTILINE,
    )
    tls_key = re.compile(r"SettingKeys\.SMTP_USE_TLS")
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel != "services/notifications/smtp_transport.py" and opener.search(text):
            offenders.append(f"{rel}: opens SMTP itself")
        if rel not in {"services/notifications/smtp_settings.py", "api/settings.py"} and tls_key.search(text):
            offenders.append(f"{rel}: reads smtp_use_tls itself")
    assert offenders == []
