#!/usr/bin/env python3
#
# tests/test_alerts_view_services.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Alerts view: the service badges list every enabled check of the target, UDP included."""

from __future__ import annotations

import re
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.models.users import hash_password, hash_token, new_token
from app.utils import crypto
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _target(conn, name: str, **checks: int) -> int:
    now = utcnow()
    values = {"enable_ping": 0, "enable_http_check": 0, "enable_cert_expiration": 0, **checks}
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO targets (name, interval_seconds, retention_days, host, created_at, updated_at, {columns}) "  # noqa: S608 - constant column names
        f"VALUES (?, 60, 30, ?, ?, ?, {marks})",
        (name, f"{name}.example.org", now, now, *values.values()),
    )
    return int(cur.lastrowid)


def _alert(conn, alert_id: str, target_id: int, name: str, failed: str) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO alerts (id, target_id, target_name, failed_service, status, active_incident, started_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'open', 1, ?, ?, ?)",
        (alert_id, target_id, name, failed, now, now, now),
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-alerts-view-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.delenv("SECRET_KEYS", raising=False)
        monkeypatch.setattr(crypto, "_primary_key", None)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        sqlite_db.init_schema(conn)
        uid = sqlite_db.create_user(conn, username="boss", password_hash=hash_password("Passw0rd!x1"), is_admin=True)
        token, expires, max_expires = new_token()
        sqlite_db.issue_token(
            conn, user_id=uid, token_hash=hash_token(token), expires_at=expires, max_expires_at=max_expires
        )
        udp_only = _target(conn, "udponly", enable_udp_scan=1)
        mixed = _target(conn, "mixed", enable_ping=1, enable_http_check=1, enable_tcp_connect=1, enable_udp_scan=1)
        _alert(conn, "a-udp", udp_only, "udponly", "UDP")
        _alert(conn, "a-mixed", mixed, "mixed", "UDP")
        conn.commit()
        sqlite_db.close_connection(conn)
        c = TestClient(app)
        c.get("/ui/login")
        c.cookies.set("token", token)
        yield c


def _services(html: str, target_name: str) -> list[tuple[str, str]]:
    """(label, kind) of each service in the alert row of ``target_name``.

    A failed one is a DOWN dot (S1), the others count badges (DC-BADGE-5).
    """
    rows = [r for r in re.split(r"(?i)<tr\b", html) if 'data-label="Service"' in r and f"{target_name}\n" in r]
    assert len(rows) == 1, f"alert row for {target_name} not found"
    cell = rows[0].split('data-label="Service"', 1)[1].split("</td>", 1)[0]
    assert "ju-status-badge--" not in cell, "a service is never a status badge"
    found = re.findall(
        r'ju-status-dot--danger ju-status-dot--down" aria-hidden="true"></span>([^<]+)<|badge badge-muted">([^<]+)<',
        cell,
    )
    return [(down.strip(), "danger") if down else (other.strip(), "neutral") for down, other in found]


def test_udp_only_target_shows_its_udp_service(client) -> None:
    html = client.get("/ui/notifications?category=monitoring").text
    assert _services(html, "udponly") == [("UDP", "danger")]


def test_all_enabled_services_are_listed_and_only_the_failed_one_is_marked(client) -> None:
    html = client.get("/ui/notifications?category=monitoring").text
    assert _services(html, "mixed") == [("HTTP", "neutral"), ("PING", "neutral"), ("TCP", "neutral"), ("UDP", "danger")]
