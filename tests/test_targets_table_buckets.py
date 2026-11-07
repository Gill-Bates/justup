#!/usr/bin/env python3
#
# tests/test_targets_table_buckets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The targets table sorts check states into buckets: down, unknown, cert alert, up."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.api.frontend import templates
from tests import _support  # noqa: F401 - imported for its sys.path side effect

_TEMPLATE = "fragments/targets_table.html"


def _target(**overrides):
    base = {
        "id": 1,
        "name": "t",
        "host": "h",
        "http_url": None,
        "tcp_ports": "",
        "udp_ports": "",
        "enable_tcp_connect": False,
        "enable_udp_scan": False,
        "enable_ping": False,
        "enable_http_check": False,
        "enable_cert_expiration": False,
        "ping_status": None,
        "http_status": None,
        "cert_status": None,
        "endpoints": [],
        "uptime_percent": 99.0,
        "uptime_level": "ok",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _render(target) -> str:
    return templates.env.get_template(_TEMPLATE).render(
        grouped_targets={"G": [target]},
        page=1,
        total_pages=1,
        total=1,
        show_all=False,
        user=None,
    )


def _summary(html: str) -> list[str]:
    block = re.search(r'targets-check-summary">(.*?)</div>', html, re.DOTALL).group(1)
    return re.findall(r"<span>(.*?)</span>", block)


def _labels(html: str) -> list[str]:
    block = re.search(r'id="target-checks-1">(.*?)</div>', html, re.DOTALL).group(1)
    return [
        m.strip()
        for m in re.findall(r'aria-hidden="true"></span>(.*?)<span class="visually-hidden">', block, re.DOTALL)
    ]


def _ep(proto: str, port: int, status):
    return {"kind": "port", "protocol": proto, "port": port, "status": status, "state_note": None, "name": ""}


@pytest.mark.parametrize(
    "status,bucket",
    [
        ("up", "Up"),
        ("down", "Down"),
        (None, "Unknown"),
        ("unknown", "Unknown"),
        ("weird", "Unknown"),
    ],
)
@pytest.mark.parametrize("kind", ["ping", "http", "endpoint"])
def test_plain_checks_bucket(kind, status, bucket):
    if kind == "ping":
        t = _target(enable_ping=True, ping_status=status)
    elif kind == "http":
        t = _target(enable_http_check=True, http_status=status)
    else:
        t = _target(endpoints=[SimpleNamespace(**_ep("tcp", 22, status))])
    assert _summary(_render(t)) == ["1 Checks", f"1 {bucket}"]


@pytest.mark.parametrize(
    "status,bucket",
    [
        ("ok", "Up"),
        ("warning", "Cert alert"),
        ("critical", "Cert alert"),
        ("expired", "Cert alert"),
        (None, "Unknown"),
        ("bogus", "Unknown"),
    ],
)
def test_cert_bucket(status, bucket):
    t = _target(enable_cert_expiration=True, cert_status=status)
    assert _summary(_render(t)) == ["1 Checks", f"1 {bucket}"]


def test_order_down_unknown_attention_up_and_overflow_toggle():
    t = _target(
        enable_ping=True,
        ping_status="up",
        enable_http_check=True,
        http_status="down",
        enable_cert_expiration=True,
        cert_status="warning",
        endpoints=[SimpleNamespace(**_ep("tcp", 22, None)), SimpleNamespace(**_ep("udp", 53, "up"))],
    )
    html = _render(t)
    assert _summary(html) == ["5 Checks", "1 Down", "1 Unknown", "1 Cert alert", "2 Up"]
    assert _labels(html) == ["HTTP", "TCP:22", "Cert", "Ping", "UDP:53"]
    assert 'data-more="2"' in html and "+2 more" in html


def test_no_toggle_up_to_three_checks():
    t = _target(
        enable_ping=True,
        ping_status="up",
        enable_http_check=True,
        http_status="up",
        enable_cert_expiration=True,
        cert_status="ok",
    )
    assert "targets-check-toggle" not in _render(t)
