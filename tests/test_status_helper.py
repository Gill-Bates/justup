#!/usr/bin/env python3
#
# tests/test_status_helper.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""build_target_status with real sqlite3.Row inputs."""

from __future__ import annotations

import sqlite3

from app.utils.status_helper import build_target_status
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _rows(cached_sql: str):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    target = conn.execute(
        "SELECT 1 AS is_enabled, 1 AS enable_ping, 0 AS enable_http_check,"
        " 1 AS enable_tcp_connect, 0 AS enable_cert_expiration"
    ).fetchone()
    return target, conn.execute(cached_sql).fetchone()


_BASE = (
    "SELECT 'up' AS overall_status, 1 AS ping_up, 1 AS tcp_up, NULL AS http_up, NULL AS http_status,"
    " NULL AS cert_days_left, 99.5 AS uptime_percent, 98.0 AS uptime_percent_30d,"
    " '2026-01-01T00:00:00' AS updated_at, NULL AS last_down_at"
)


def test_tcp_port_status_is_parsed_from_row():
    target, cached = _rows(_BASE + ', \'{"80": "up"}\' AS tcp_port_status')
    result = build_target_status(target, cached)
    assert result["tcp_port_status"] == {"80": "up"}
    assert result["tcp_status"] == "up"


def test_missing_or_invalid_tcp_port_status_yields_empty_dict():
    target, cached = _rows(_BASE)
    assert build_target_status(target, cached)["tcp_port_status"] == {}
    target, cached = _rows(_BASE + ", 'not json' AS tcp_port_status")
    assert build_target_status(target, cached)["tcp_port_status"] == {}


def test_cached_uptime_and_timestamps_are_read_by_column_name():
    target, cached = _rows(_BASE)
    result = build_target_status(target, cached)
    assert result["last_check"] == "2026-01-01T00:00:00"
    assert "99.5" in str(result["uptime_percent"])


def _cert_status_for(days: int) -> str:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    target = conn.execute(
        "SELECT 1 AS is_enabled, 0 AS enable_ping, 0 AS enable_http_check,"
        " 0 AS enable_tcp_connect, 1 AS enable_cert_expiration"
    ).fetchone()
    cached = conn.execute(_BASE.replace("NULL AS cert_days_left", f"{days} AS cert_days_left")).fetchone()
    return build_target_status(target, cached)["cert_status"]


def test_cert_status_thresholds():
    assert [_cert_status_for(d) for d in (-1, 0, 3, 7, 8)] == ["expired", "critical", "critical", "warning", "ok"]


def test_templates_color_every_cert_status_the_helper_can_emit():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "templates" / "fragments"
    emitted = {_cert_status_for(d) for d in (-1, 0, 3, 7, 8)}
    for rel in ("targets_table.html", "target_detail/info_cards.html"):
        expr = " ".join(line for line in (root / rel).read_text().splitlines() if "cert_status" in line)
        for status in emitted:
            assert f"'{status}'" in expr, f"{rel} does not handle cert_status {status!r}"
        assert "'expired'" in expr and "'critical'" in expr
