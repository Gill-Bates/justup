#!/usr/bin/env python3
#
# tests/test_targets_patch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""PATCH /targets contract: null on NOT NULL fields and empty tcp_ports."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import targets
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.utils.deps import get_conn
from tests import _support  # noqa: F401 - imported for its sys.path side effect

NON_NULLABLE = {
    "name": "renamed",
    "host": "other.example.com",
    "is_enabled": False,
    "interval_seconds": 120,
    "retention_days": 60,
    "enable_ping": True,
    "enable_http_check": False,
    "enable_cert_expiration": False,
    "enable_tcp_connect": False,
    "enable_udp_scan": False,
    "http_prefer_head": False,
    "http_basic_auth_enabled": False,
    "ignore_cert_errors": True,
    "max_retries": 3,
    "auto_close_alert": False,
    "sla_enabled": False,
}


@pytest.fixture
def env(tmp_path: Path):
    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    uid = sqlite_db.create_user(conn, username="admin", password_hash="stub", is_admin=True)  # noqa: S106
    app = FastAPI()
    app.include_router(targets.router)
    app.state.tsdb_dir = tmp_path / "db"
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: dict(sqlite_db.get_user(conn, uid))
    tid = sqlite_db.create_target(
        conn,
        {
            "name": "t",
            "host": "t.example.com",
            "interval_seconds": 60,
            "retention_days": 30,
            "tcp_ports": "22,443",
            "enable_ping": True,
        },
    )
    yield TestClient(app), conn, tid
    sqlite_db.close_connection(conn)


def _row(conn, tid: int) -> dict:
    return dict(sqlite_db.get_target(conn, tid))


@pytest.mark.parametrize("field", list(NON_NULLABLE))
def test_patch_null_on_required_field_is_422_and_changes_nothing(env, field):
    client, conn, tid = env
    before = _row(conn, tid)
    assert client.patch(f"/targets/{tid}", json={field: None}).status_code == 422
    assert _row(conn, tid) == before


@pytest.mark.parametrize("field", list(NON_NULLABLE))
def test_patch_required_field_omitted_or_valid(env, field):
    client, conn, tid = env
    before = _row(conn, tid)
    assert client.patch(f"/targets/{tid}", json={}).status_code == 200
    assert _row(conn, tid)[field] == before[field]
    resp = client.patch(f"/targets/{tid}", json={field: NON_NULLABLE[field]})
    assert resp.status_code == 200, resp.text
    assert resp.json()[field] == NON_NULLABLE[field]


def test_patch_nullable_field_keeps_null_semantics(env):
    client, _conn, tid = env
    assert client.patch(f"/targets/{tid}", json={"group": None, "http_username": None}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"tcp_ports": None}, {"tcp_ports": ""}])
@pytest.mark.parametrize("tcp_on", [True, False])
def test_patch_empty_tcp_ports_is_noop(env, body, tcp_on):
    client, conn, tid = env
    resp = client.patch(f"/targets/{tid}", json={"enable_tcp_connect": tcp_on, "tcp_host": "t.example.com"})
    assert resp.status_code == 200, resp.text
    before = _row(conn, tid)
    assert client.patch(f"/targets/{tid}", json=body).status_code == 200
    assert _row(conn, tid)["tcp_ports"] == before["tcp_ports"] == "22,443"


def test_patch_valid_tcp_ports_is_applied_and_normalized(env):
    client, conn, tid = env
    assert client.patch(f"/targets/{tid}", json={"tcp_ports": "8080, 80"}).status_code == 200
    assert _row(conn, tid)["tcp_ports"] == "80,8080"
