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
    days = (-1, 0, 3, 4, 7, 8)
    expected = ["expired", "critical", "critical", "warning", "warning", "ok"]
    assert [_cert_status_for(d) for d in days] == expected


def test_templates_color_every_cert_status_the_helper_can_emit():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape(["html"]))
    cert_level = env.get_template("fragments/_state_badge.html").module.cert_level

    emitted = {_cert_status_for(d) for d in (-1, 0, 3, 7, 8)}
    assert emitted == {"expired", "critical", "warning", "ok"}
    tones = {status: str(cert_level(status)) for status in emitted}
    assert tones == {"expired": "danger", "critical": "danger", "warning": "warning", "ok": "success"}
    assert str(cert_level(None)) == "neutral"

    # Both views must colour the status through the shared macro, not through a private copy of the mapping.
    for rel in ("fragments/targets_table.html", "fragments/target_detail/info_cards.html"):
        source = (templates / rel).read_text()
        assert "cert_badge(" in source, f"{rel} does not use the shared cert badge"
        assert "ju-status-badge--" not in source, f"{rel} maps cert statuses to badge colours itself"


def test_uptime_level_uses_sla_goal_without_warn_band_and_falls_back_to_fixed_limits():
    from app.utils.uptime_metric import uptime_level

    assert uptime_level(99.5, sla_enabled=True, sla_availability_pct=99.9) == "bad"
    assert uptime_level(99.9, sla_enabled=True, sla_availability_pct=99.9) == "ok"
    assert uptime_level(None, sla_enabled=True, sla_availability_pct=99.9) == "none"
    assert uptime_level(99.5, sla_enabled=False, sla_availability_pct=99.9) == "ok"
    assert uptime_level(96.0, sla_enabled=True, sla_availability_pct=None) == "warn"
    assert uptime_level(90.0) == "bad"


def test_status_payload_gives_list_and_kpi_the_same_level_as_the_uptime_fragment():
    import sqlite3

    from app.utils.status_helper import build_target_status

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (is_enabled, enable_ping, enable_http_check, enable_tcp_connect, "
        "enable_cert_expiration, sla_enabled, sla_availability_pct)"
    )
    conn.execute("CREATE TABLE s (overall_status, uptime_percent, uptime_percent_30d)")
    conn.execute("INSERT INTO t VALUES (1, 0, 0, 0, 0, 1, 99.9)")
    conn.execute("INSERT INTO s VALUES ('up', 99.5, 99.95)")
    row = conn.execute("SELECT * FROM t").fetchone()
    status = build_target_status(row, conn.execute("SELECT * FROM s").fetchone())
    assert (status["uptime_level"], status["uptime_level_30d"]) == ("bad", "ok")
    assert build_target_status(row, None)["uptime_level"] == "none"
