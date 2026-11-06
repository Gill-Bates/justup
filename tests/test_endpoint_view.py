#!/usr/bin/env python3
#
# tests/test_endpoint_view.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Endpoint view model and the Checks badges: ICMP apart, TCP/UDP unambiguous, UNKNOWN never rendered like DOWN."""

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
        "uptime_percent": None,
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
    views = build_endpoint_views(
        target(tcp_ports="53", udp_ports="53"), [stored("tcp", 53, "up"), stored("udp", 53, "down")]
    )
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
    class Row(dict):  # stands in for sqlite3.Row (keys() and item access)
        pass

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


INFO_CARDS = "fragments/target_detail/info_cards.html"


def render_cards(t: dict) -> str:
    t["endpoints"] = build_endpoint_views(t, t.get("network_ports", []))
    return render(INFO_CARDS, target=t, user={"is_admin": True})


def badge_markup(html: str, label: str) -> str:
    """Markup of the one badge that contains ``label``."""
    match = re.search(
        rf'<span class="badge[^>]*>(?:(?!<span class="badge).)*?{label}.*?</span></span>', html, re.DOTALL
    )
    assert match, f"no badge with {label!r}"
    return match.group(0)


def check_badges(html: str) -> dict[str, str]:
    """Label -> markup of every badge in the Checks hero (CERT included), in document order."""
    hero = re.search(r'<div class="ju-badges target-kpi-badges">(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL).group(
        1
    )
    badges = re.findall(r'<span class="badge.*?</span></span>', hero, re.DOTALL)
    out: dict[str, str] = {}
    for badge in badges:
        text = re.sub(r"<[^>]+>", "", re.sub(r'<span class="material-icons"[^>]*>[^<]*</span>', "", badge))
        out[text.split(":")[0].strip()] = badge
    return out


def assert_no_endpoint_table(html: str) -> None:
    for fragment in (
        "target-endpoints-card",
        "Network endpoints",
        "data-endpoint",
        "<table",
        "Waiting for first measurement",
    ):
        assert fragment not in html, fragment


def test_detail_checks_show_up_down_and_unknown_apart_without_an_endpoint_table() -> None:
    t = target()
    t["network_ports"] = [
        stored("tcp", 25, "up"),
        stored("tcp", 587, "down"),
        stored("udp", 53, "up", raw_state="open"),
        stored("udp", 161, "unknown", raw_state="open|filtered"),
    ]
    html = render_cards(t)
    assert_no_endpoint_table(html)
    up, down, unknown = (badge_markup(html, label) for label in ("TCP:25", "TCP:587", "UDP:161"))
    assert "ju-status-badge--success" in up and "check_circle" in up and ": Up<" in up
    assert "ju-status-badge--danger" in down and "cancel" in down and ": Down<" in down
    assert "ju-status-badge--neutral" in unknown and "help" in unknown and ": Unknown<" in unknown
    assert "ju-state-badge--unknown" in unknown and "ju-state-badge--unknown" not in down
    assert "ju-status-badge--danger" not in unknown and "Down" not in unknown
    assert "uncertain" in unknown, "the Nmap note stays reachable as tooltip"


def test_checks_hero_lists_exactly_the_configured_ports_and_not_the_ping_twice() -> None:
    t = target(enable_udp_scan=False)
    t["network_ports"] = [stored("tcp", 25, "up"), stored("tcp", 587, "down")]
    html = render_cards(t)
    labels = re.findall(r"(PING|TCP:\d+|UDP:\d+|HTTP|CERT)<span class=\"visually-hidden\">", html)
    assert labels == ["PING", "TCP:25", "TCP:587"]
    assert "ICMP" not in html


def test_tcp_and_udp_with_the_same_port_number_are_two_badges() -> None:
    t = target(enable_ping=False, tcp_ports="53", udp_ports="53")
    t["network_ports"] = [stored("tcp", 53, "up"), stored("udp", 53, "down")]
    html = render_cards(t)
    assert html.count("TCP:53") == 1 and html.count("UDP:53") == 1


def test_unmeasured_ports_are_unknown_badges_not_missing() -> None:
    html = render_cards(target(enable_ping=False))
    for label in ("TCP:25", "TCP:587", "UDP:53", "UDP:161"):
        badge = badge_markup(html, label)
        assert ": Unknown<" in badge and "ju-state-badge--unknown" in badge
    assert "Waiting for first measurement" not in html


def test_disabled_checks_and_removed_ports_have_no_badge() -> None:
    t = target(enable_udp_scan=False, enable_ping=False, tcp_ports="25")
    t["network_ports"] = [stored("tcp", 25, "up"), stored("tcp", 9999, "down"), stored("udp", 53, "up")]
    html = render_cards(t)
    assert "TCP:25" in html
    for label in ("TCP:587", "TCP:9999", "UDP:53", "PING"):
        assert label not in html


def test_http_and_cert_badges_are_unchanged() -> None:
    t = target(
        enable_ping=False,
        enable_tcp_connect=False,
        enable_udp_scan=False,
        enable_http_check=True,
        http_status="down",
        enable_cert_expiration=True,
        cert_status="warning",
    )
    html = render_cards(t)
    http = badge_markup(html, "HTTP")
    assert "ju-status-badge--danger" in http and ": Down<" in http
    cert = re.search(r'<span class="badge\s+ju-status-badge--warning\s*">\s*CERT.*?</span>\s*</span>', html, re.DOTALL)
    assert cert and 'CERT<span class="visually-hidden">: warning</span>' in cert.group(0)


def test_target_without_any_check_says_so_and_has_no_table() -> None:
    t = target(enable_ping=False, enable_tcp_connect=False, enable_udp_scan=False)
    html = render_cards(t)
    assert "No checks enabled" in html
    assert_no_endpoint_table(html)


def test_many_ports_are_all_badges_in_one_wrapping_group() -> None:
    ports = ",".join(str(n) for n in range(1000, 1040))
    t = target(enable_ping=False, enable_udp_scan=False, tcp_ports=ports)
    html = render_cards(t)
    assert len(re.findall(r"ju-state-badge--unknown", html)) == 40
    assert 'class="ju-badges target-kpi-badges"' in html


def test_state_badge_escapes_label_and_title() -> None:
    macro = templates.env.from_string(
        '{% from "fragments/_state_badge.html" import state_badge %}{{ state_badge(label, "up", title) }}'
    )
    html = macro.render(label="<img src=x onerror=alert(1)>", title='"><script>alert(1)</script>')
    assert "<img src=x" not in html and "<script>" not in html
    assert "&lt;img" in html and "&lt;script&gt;" in html


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

    def badge(label: str) -> str:
        return badge_markup(html, label)

    down, unknown = badge("TCP:25"), badge("UDP:161")
    assert "Down" in down and "Unknown" in unknown
    assert "cancel" in down and "help" in unknown


def _detail_view(tmp_path, config: dict, *, series: bool = False) -> dict:
    from datetime import UTC, datetime

    from app.api import frontend
    from app.db import sqlite as sqlite_db
    from app.db.tsdb import append_point

    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    try:
        tid = sqlite_db.create_target(
            conn,
            {"name": "mail", "host": "mail.example.org", "interval_seconds": 60, "retention_days": 30} | config,
        )
        if series:
            append_point(
                tmp_path / "tsdb", target_id=tid, metric="tcp_up_25", value=1, retention_days=30, at=datetime.now(UTC)
            )
        return frontend._target_detail_view(  # noqa: SLF001
            conn, sqlite_db.get_target(conn, tid), tid, tsdb_dir=tmp_path / "tsdb" if series else None
        )
    finally:
        sqlite_db.close_connection(conn)


def test_target_without_stored_port_rows_still_shows_its_tcp_badge(tmp_path) -> None:
    # A target saved before the port table existed has no rows; the Checks hero still lists its port.
    view = _detail_view(tmp_path, {"enable_tcp_connect": 1, "tcp_ports": "25"})
    html = render(INFO_CARDS, target=view, user={})
    assert "TCP:25" in html and ": Unknown<" in html
    assert_no_endpoint_table(html)


def test_legacy_target_without_port_rows_takes_its_state_from_the_tcp_status_cache() -> None:
    t = target(enable_ping=False, enable_udp_scan=False, tcp_ports="25", tcp_port_status={"25": "up"})
    badge = check_badges(render_cards(t))["TCP"]
    assert "ju-status-badge--success" in badge and ": Up<" in badge


def test_unmeasured_port_row_defers_to_the_tcp_status_cache() -> None:
    # Migration 7 creates the row empty; the cache holds the pre-upgrade measurement the charts also show.
    t = target(enable_ping=False, enable_udp_scan=False, tcp_ports="25", tcp_port_status={"25": "down"})
    t["network_ports"] = [stored("tcp", 25, None, checked_at=None)]
    [view] = build_endpoint_views(t, t["network_ports"])
    assert (view["status"], view["measured"]) == ("down", True)
    assert "ju-status-badge--danger" in check_badges(render_cards(t))["TCP"]


def test_detail_view_takes_an_unmeasured_tcp_port_state_from_the_series_the_charts_plot(tmp_path) -> None:
    view = _detail_view(tmp_path, {"enable_tcp_connect": 1, "tcp_ports": "25,587"}, series=True)
    states = {v["key"]: (v["status"], v["measured"]) for v in view["endpoints"]}
    assert states == {"tcp:25": ("up", True), "tcp:587": ("unknown", False)}
    badges = render(INFO_CARDS, target=view, user={})
    assert re.search(r"TCP:25.*?: Up<", badges, re.DOTALL) and re.search(r"TCP:587.*?: Unknown<", badges, re.DOTALL)
