#!/usr/bin/env python3
#
# tests/test_metrics_allowlist.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Metric names the series endpoint serves: only series the monitor really writes, TCP and UDP kept apart."""

from __future__ import annotations

from datetime import timedelta
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.metrics import _is_allowed_metric
from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.models.users import hash_password, hash_token, new_token
from app.utils import crypto
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize(
    "metric",
    ["udp_up", "udp_up_53", "udp_up_65535", "tcp_up_53", "tcp_ms_443", "tcp_ms", "tcp_up", "http_ms"],
)
def test_written_metrics_are_allowed(metric: str) -> None:
    assert _is_allowed_metric(metric)


@pytest.mark.parametrize(
    "metric",
    [
        "udp_ms",  # the monitor writes no UDP latency series
        "udp_ms_53",
        "udp_up_0",
        "udp_up_65536",
        "udp_up_",
        "udp_unknown_53",  # internal flag: rollups read it, no client queries it
        "udp_up_53;drop",
        "UDP_up_53",
        "sctp_up_53",
        "nonsense",
    ],
)
def test_other_metric_names_are_refused(metric: str) -> None:
    assert not _is_allowed_metric(metric)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-metrics-") as temp_dir:
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
        now = utcnow()
        conn.execute(
            "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check, "
            "enable_cert_expiration, host, created_at, updated_at) VALUES ('t', 60, 30, 1, 0, 0, 'h.example', ?, ?)",
            (now, now),
        )
        target_id = conn.execute("SELECT id FROM targets").fetchone()[0]
        conn.commit()
        sqlite_db.close_connection(conn)
        at = now - timedelta(minutes=5)
        for metric, value in (("tcp_up_53", 1), ("udp_up_53", 0), ("udp_up", 0)):
            tsdb.append_point(
                app.state.tsdb_dir, target_id=target_id, metric=metric, value=value, retention_days=30, at=at
            )
        c = TestClient(app)
        c.get("/ui/login")
        c.cookies.set("token", token)
        c.target_id = target_id  # type: ignore[attr-defined]
        yield c


def _get(client, metric: str):
    return client.get(f"/targets/{client.target_id}/metrics/{metric}")


@pytest.mark.parametrize("metric", ["udp_up", "udp_up_53"])
def test_udp_availability_series_are_served(client, metric: str) -> None:
    assert _get(client, metric).status_code == 200


@pytest.mark.parametrize("metric", ["udp_ms", "udp_unknown_53", "bogus"])
def test_unknown_or_unwritten_metrics_answer_400(client, metric: str) -> None:
    assert _get(client, metric).status_code == 400


def test_tcp_and_udp_series_of_one_port_stay_separate(client) -> None:
    tcp = _get(client, "tcp_up_53").json()["points"]
    udp = _get(client, "udp_up_53").json()["points"]
    assert [p["value"] for p in tcp] == [1]
    assert [p["value"] for p in udp] == [0]
