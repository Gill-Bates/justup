#!/usr/bin/env python3
#
# tests/test_unknown_status_display.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""UNKNOWN is not DOWN on the dashboard and the detail page, and it hides no measured check."""

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


def _target(conn, name: str) -> int:
    now = utcnow()
    cursor = conn.execute(
        "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, enable_udp_scan, udp_ports, host, created_at, updated_at) "
        "VALUES (?, 60, 30, 1, 0, 0, 1, '161', ?, ?, ?)",
        (name, f"{name}.example.org", now, now),
    )
    target_id = int(cursor.lastrowid)
    sqlite_db.sync_network_ports(conn, target_id, tcp_ports=[], udp_ports=[161])
    return target_id


def _status(conn, target_id: int, *, ping_up: int | None, overall: str) -> None:
    sqlite_db.set_target_status_snapshot(
        conn,
        target_id,
        ping_up=ping_up,
        ping_latency_ms=None,
        http_up=None,
        http_status=None,
        http_latency_ms=None,
        tcp_up=None,
        tcp_latency_ms=None,
        cert_days_left=None,
        uptime_percent=None,
        overall_status=overall,
    )


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-unknown-") as temp_dir:
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
        ids = {"down": _target(conn, "down"), "unknown": _target(conn, "unknown"), "up": _target(conn, "up")}
        _status(conn, ids["down"], ping_up=0, overall="down")
        _status(conn, ids["unknown"], ping_up=1, overall="unknown")  # ping answers, the UDP port is unconfirmed
        _status(conn, ids["up"], ping_up=1, overall="up")
        sqlite_db.update_network_port_status(conn, ids["unknown"], "udp", 161, "unknown", raw_state="open|filtered")
        conn.commit()
        sqlite_db.close_connection(conn)
        client = TestClient(app)
        client.get("/ui/login")
        client.cookies.set("token", token)
        yield client, ids


def test_dashboard_counts_an_unknown_target_neither_online_nor_offline(seeded) -> None:
    client, _ids = seeded
    html = client.get("/ui/fragments/status-summary").text

    def value(name: str) -> int:
        match = re.search(
            rf'id="dashboard-status-{name}".*?<span class="visually-hidden">(\d+)</span>', html, re.DOTALL
        )
        return int(match.group(1))

    assert (value("online"), value("offline"), value("total")) == (1, 1, 3)


def test_status_icon_of_unknown_differs_in_shape_and_name_from_down(seeded) -> None:
    client, ids = seeded
    down = client.get(f"/ui/fragments/target-status/{ids['down']}").text
    unknown = client.get(f"/ui/fragments/target-status/{ids['unknown']}").text
    assert "Status: Offline" in down and "cancel" in down
    assert "Status: Unknown" in unknown and "help" in unknown
    assert "Offline" not in unknown and "cancel" not in unknown
    unknown_pill = client.get(f"/ui/fragments/target-status/{ids['unknown']}?labelled=1").text
    down_pill = client.get(f"/ui/fragments/target-status/{ids['down']}?labelled=1").text
    assert "target-status-pill--unknown" in unknown_pill and ">Unknown<" in unknown_pill
    assert "target-status-pill--down" in down_pill and ">Offline<" in down_pill


def test_unknown_overall_status_keeps_the_measured_ping_and_shows_the_udp_port_as_unknown_badge(seeded) -> None:
    client, ids = seeded
    html = client.get(f"/ui/fragments/target-info-cards/{ids['unknown']}").text
    ping = re.search(
        r'<span class="ju-status-item[^>]*>(?:(?!<span class="ju-status-item).)*?PING.*?</span></span>', html, re.DOTALL
    ).group(0)
    assert "ju-status-dot--success" in ping and "Up" in ping and "Unknown" not in ping
    udp = re.search(
        r'<span class="ju-status-item[^>]*>(?:(?!<span class="ju-status-item).)*?UDP:161.*?</span></span>',
        html,
        re.DOTALL,
    ).group(0)
    assert "ju-status-dot--unknown" in udp and ": Unknown<" in udp and "material-icons" not in udp
    assert "open|filtered" in udp and "Down" not in udp
    assert "ju-status-dot--danger" not in udp
    assert "<table" not in html and "data-endpoint" not in html and "Network endpoints" not in html


def test_targets_table_marks_the_unknown_endpoint_apart_from_down(seeded) -> None:
    client, ids = seeded
    html = client.get("/ui/fragments/targets-table?all=1").text
    rows = {m.group(1): m.group(0) for m in re.finditer(r'<tr[^>]*data-target-id="(\d+)".*?</tr>', html, re.DOTALL)}
    unknown_row = rows[str(ids["unknown"])]
    assert "UDP:161" in unknown_row and "ju-status-dot--unknown" in unknown_row
    assert "ju-status-dot--danger" not in unknown_row
