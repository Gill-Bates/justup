#!/usr/bin/env python3
#
# tests/test_endpoint_view.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Endpoint view model and its markup: ICMP apart, TCP/UDP unambiguous, UNKNOWN never rendered like DOWN."""

from __future__ import annotations

import re
from pathlib import Path

from app.api.frontend import templates
from app.utils.endpoint_view import build_endpoint_views, normalise_status
from app.utils.status_helper import build_target_status, get_status_defaults
from tests import _support  # noqa: F401 - imported for its sys.path side effect

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def target(**overrides) -> dict:
    base = {
        "id": 7,
        "name": "mail",
        "host": "mail.example.org",
        "is_enabled": True,
        "interval_seconds": 60,
        "retention_days": 30,
        "max_retries": 2,
        "enable_ping": True,
        "enable_http_check": False,
        "enable_cert_expiration": False,
        "enable_tcp_connect": True,
        "tcp_ports": "25,587",
        "enable_udp_scan": True,
        "udp_ports": "53,161",
        "ping_status": "up",
        "http_status": "unknown",
        "cert_status": "unknown",
        "tcp_status": "up",
        "tcp_port_status": {},
        "uptime_level": "none",
        "uptime_level_30d": "none",
        "uptime_percent_display": "–",
        "overall_status": "unknown",
        "last_check": None,
        "group": None,
    }
    return base | overrides


def stored(protocol: str, port: int, status: str | None, **extra) -> dict:
    return {
        "protocol": protocol,
        "port": port,
        "status": status,
        "checked_at": "2026-11-04T10:00:00+00:00",
        "last_up_at": None,
        "service_name": None,
        "service_product": None,
        "service_version": None,
        "method": None,
        "service_identity": None,
        "raw_state": None,
        "detail": None,
    } | extra


def render(rel: str, **context) -> str:
    return templates.env.get_template(rel).render(**context)


def test_icmp_is_listed_apart_and_tcp_and_udp_are_told_apart_by_protocol() -> None:
    views = build_endpoint_views(target(tcp_ports="53", udp_ports="53"), [stored("tcp", 53, "up"), stored("udp", 53, "down")])
    assert [(v["kind"], v["name"], v["status"]) for v in views] == [
        ("icmp", "ICMP", "up"),
        ("port", "TCP 53", "up"),
        ("port", "UDP 53", "down"),
    ]


def test_configured_ports_without_a_measurement_are_unknown_not_missing() -> None:
    views = build_endpoint_views(target(), [])
    assert [(v["key"], v["status"]) for v in views] == [
        ("icmp", "up"),
        ("tcp:25", "unknown"),
        ("tcp:587", "unknown"),
        ("udp:53", "unknown"),
        ("udp:161", "unknown"),
    ]


def test_disabled_checks_and_removed_ports_are_not_listed() -> None:
    views = build_endpoint_views(
        target(enable_udp_scan=False, enable_ping=False), [stored("tcp", 25, "up"), stored("tcp", 9999, "down")]
    )
    assert [v["key"] for v in views] == ["tcp:25", "tcp:587"]


def test_unrecognised_status_is_unknown_never_down() -> None:
    assert normalise_status(None) == "unknown"
    assert normalise_status("open|filtered") == "unknown"
    assert normalise_status("DOWN") == "down"


def test_uncertain_udp_state_is_labelled_and_service_hint_is_marked() -> None:
    row = stored("udp", 161, "unknown", raw_state="open|filtered", service_name="snmp", method="table")
    [_, _, _, _, udp161] = build_endpoint_views(target(), [row])
    assert udp161["status"] == "unknown"
    assert "uncertain" in udp161["state_note"]
    assert (udp161["service"], udp161["service_note"]) == ("snmp", "table hint, not verified")


def test_status_helper_keeps_measured_up_checks_while_the_overall_status_is_unknown() -> None:
    class Row(dict):
        def keys(self):  # sqlite3.Row API
            return super().keys()

    row = Row(
        is_enabled=1,
        enable_ping=1,
        enable_http_check=1,
        enable_tcp_connect=0,
        enable_cert_expiration=0,
        sla_enabled=0,
        sla_availability_pct=None,
    )
    cached = Row(
        overall_status="unknown",
        ping_up=1,
        ping_checked_at="2026-11-04T10:00:00+00:00",
        http_up=1,
        http_status=200,
        tcp_up=None,
        cert_days_left=None,
        uptime_percent=None,
        uptime_percent_30d=None,
        last_down_at=None,
        updated_at="2026-11-04T10:00:00+00:00",
    )
    status = build_target_status(row, cached)
    assert (status["ping_status"], status["http_status"]) == ("up", "up")
    assert status["ping_checked_at"] == "2026-11-04T10:00:00+00:00"
    assert get_status_defaults()["ping_checked_at"] is None


def endpoint_table(html: str) -> str:
    match = re.search(r'<table[^>]*aria-label="[^"]*">.*?</table>|<section[^>]*network-endpoints.*?</section>', html, re.S)
    assert match, "endpoint table missing"
    return match.group(0)


def test_detail_endpoint_table_lists_icmp_tcp_and_udp_with_distinct_unknown_and_down() -> None:
    t = target()
    t["network_ports"] = [
        stored("tcp", 25, "up", last_up_at="2026-11-04T10:00:00+00:00", service_name="smtp", method="probed"),
        stored("tcp", 587, "down"),
        stored("udp", 53, "up", raw_state="open"),
        stored("udp", 161, "unknown", raw_state="open|filtered"),
    ]
    t["endpoints"] = build_endpoint_views(t, t["network_ports"])
    html = render("fragments/target_detail/info_cards.html", target=t, user={"is_admin": True})
    table = endpoint_table(html)
    rows = {m.group(1): m.group(0) for m in re.finditer(r'<tr[^>]*data-endpoint="([^"]+)".*?</tr>', table, re.S)}
    assert list(rows) == ["icmp", "tcp:25", "tcp:587", "udp:53", "udp:161"]
    down, unknown = rows["tcp:587"], rows["udp:161"]
    assert "Down" in down and "Unknown" not in down
    assert "Unknown" in unknown and "Down" not in unknown
    assert "cancel" in down and "help" in unknown, "icon shape differs, not only the colour"
    assert "ju-status-badge--danger" in down and "ju-status-badge--danger" not in unknown
    assert "smtp" in rows["tcp:25"]
    assert "uncertain" in unknown
    assert re.search(r"<th[^>]*>Last successful check</th>", table)
    assert "<th" in rows["icmp"] and "ICMP" in rows["icmp"]


def test_every_enabled_endpoint_has_a_status_badge_with_text_for_unknown() -> None:
    t = target()
    t["network_ports"] = [stored("udp", 161, "unknown", raw_state="open|filtered")]
    t["endpoints"] = build_endpoint_views(t, t["network_ports"])
    html = render("fragments/target_detail/info_cards.html", target=t, user={})
    badges = re.search(r'<div class="ju-badges target-kpi-badges">(.*?)</div>', html, re.S).group(1)
    assert "UDP:161" in badges and "TCP:25" in badges
    udp161 = re.search(r"<span[^>]*>(?:(?!</span>\s*<span class=\"badge).)*?UDP:161.*?</span>\s*</span>", badges, re.S)
    assert udp161 and "Unknown" in udp161.group(0)


def test_dashboard_table_shows_endpoint_status_with_text_not_only_colour() -> None:
    t = target(overall_status="unknown")
    t["network_ports"] = [stored("tcp", 25, "down"), stored("udp", 161, "unknown", raw_state="open|filtered")]
    t["endpoints"] = build_endpoint_views(t, t["network_ports"])
    html = render(
        "fragments/targets_table.html",
        targets=[t],
        grouped_targets={"Ungrouped": [t]},
        user={"is_admin": True},
        use_utc=False,
        page=1,
        total_pages=1,
        total=1,
        show_all=False,
    )
    down = re.search(r"TCP:25.*?</span>\s*</span>", html, re.S).group(0)
    unknown = re.search(r"UDP:161.*?</span>\s*</span>", html, re.S).group(0)
    assert "Down" in down and "Unknown" in unknown
    assert "cancel" in down and "help" in unknown


def test_endpoint_markup_never_inserts_scanner_text_as_html() -> None:
    row = stored("tcp", 25, "up", service_name="<img src=x onerror=alert(1)>", method="probed")
    t = target(enable_udp_scan=False, enable_ping=False, tcp_ports="25")
    t["network_ports"] = [row]
    t["endpoints"] = build_endpoint_views(t, t["network_ports"])
    html = render("fragments/target_detail/info_cards.html", target=t, user={})
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_downtime_history_has_three_separate_presentations() -> None:
    from datetime import UTC, datetime

    start = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    end = datetime(2026, 11, 4, 10, 30, tzinfo=UTC)
    items = [
        {"kind": "down", "start": start, "end": end, "duration_seconds": 1800, "services": ["TCP:25"]},
        {"kind": "gap", "start": start, "end": end, "duration_seconds": 1800, "services": ["HTTP"]},
        {"kind": "unknown", "start": start, "end": end, "duration_seconds": 1800, "services": ["UDP:161"]},
    ]
    html = render("fragments/downtime_history.html", periods=items, has_data=True, use_utc=True, range_label="30d")
    entries = {m.group(1): m.group(0) for m in re.finditer(r'<li class="downtime-item downtime-item--(\w+)[^"]*".*?</li>', html, re.S)}
    assert set(entries) == {"down", "gap", "unknown"}
    assert "Downtime" in entries["down"] and "cancel" in entries["down"]
    assert "No measurements" in entries["gap"] and "timer_off" in entries["gap"]
    assert "Unknown" in entries["unknown"] and "help" in entries["unknown"]
    assert "not counted as downtime" in entries["gap"] and "not counted as downtime" in entries["unknown"]
    assert "Downtime" not in entries["gap"].replace("not counted as downtime", "")
