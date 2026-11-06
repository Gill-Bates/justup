#!/usr/bin/env python3
#
# tests/test_default_tcp_port.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""One default TCP port for the model, the scheduler and the PDF report; each keeps its own handling of bad input."""

from __future__ import annotations

import pytest

from app.api import frontend
from app.models import targets as target_models
from app.monitor import scheduler
from app.services import pdf_worker
from app.utils import availability_rules, endpoint_view, tcp_ports
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def test_the_default_is_defined_once() -> None:
    assert tcp_ports.DEFAULT_TCP_PORTS == "80"
    assert target_models.DEFAULT_TCP_PORTS is tcp_ports.DEFAULT_TCP_PORTS


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_scheduler_uses_the_default_for_missing_or_empty_ports(empty) -> None:
    assert scheduler._stored_tcp_ports(empty) == tcp_ports.parse_port_list(tcp_ports.DEFAULT_TCP_PORTS)  # noqa: SLF001


@pytest.mark.parametrize("bad", ["80,abc", "0", "70000", "80;443"])
def test_scheduler_never_replaces_an_invalid_list_by_the_default(bad) -> None:
    assert scheduler._stored_tcp_ports(bad) == []  # noqa: SLF001


@pytest.mark.parametrize(("stored", "expected"), [("443", [443]), ("8080 80", [80, 8080]), (22, [22])])
def test_scheduler_keeps_a_manual_list(stored, expected) -> None:
    assert scheduler._stored_tcp_ports(stored) == expected  # noqa: SLF001


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ({}, [80]),
        ({"tcp_ports": None}, [80]),
        ({"tcp_ports": ""}, [80]),
        ({"tcp_ports": "443,8443"}, [443, 8443]),
        ({"tcp_ports": "22"}, [22]),
        ({"tcp_ports": "abc"}, []),  # the report reads tolerantly: nothing valid, nothing charted
    ],
)
def test_pdf_report_ports_use_the_shared_default(target, expected) -> None:
    assert pdf_worker._report_tcp_ports(target) == expected  # noqa: SLF001


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ({"host": "a.example", "tcp_host": "t.example"}, {"tcp": "t.example", "udp": "a.example"}),
        ({"host": "a.example", "tcp_host": None}, {"tcp": "a.example", "udp": "a.example"}),
        ({"host": "a.example"}, {"tcp": "a.example", "udp": "a.example"}),
        ({"host": "", "tcp_host": ""}, {"tcp": "", "udp": ""}),
        ({"host": None, "tcp_host": "t.example"}, {"tcp": "t.example", "udp": ""}),
    ],
)
def test_protocol_scan_hosts(target, expected) -> None:
    assert tcp_ports.protocol_scan_hosts(target) == expected


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_shared_fallback_gives_the_default_for_empty_values(empty) -> None:
    assert tcp_ports.tcp_ports_or_default(empty) == [80]


@pytest.mark.parametrize("bad", ["80,abc", "0", "70000"])
def test_shared_fallback_rejects_an_invalid_list_instead_of_defaulting(bad) -> None:
    with pytest.raises(ValueError):
        tcp_ports.tcp_ports_or_default(bad)


def test_display_paths_take_the_default_from_the_shared_constant(monkeypatch) -> None:
    # A distinct sentinel proves each path reads DEFAULT_TCP_PORTS instead of a literal 80.
    monkeypatch.setattr(tcp_ports, "DEFAULT_TCP_PORTS", "8081")

    series = availability_rules.downtime_series({"enable_tcp_connect": True, "tcp_ports": ""})
    assert series == [("tcp_up_8081", "TCP:8081")]

    views = endpoint_view.build_endpoint_views({"enable_tcp_connect": True, "tcp_ports": ""}, [])
    assert [view["port"] for view in views] == [8081]

    seen: list[str] = []

    def fake_latest(_dir, *, metric, **_kwargs):
        seen.append(metric)
        return []

    monkeypatch.setattr(frontend.tsdb_db, "query_latest", fake_latest)
    frontend._fill_tcp_status_from_series({"id": 1, "network_ports": [], "tcp_ports": ""}, None)  # noqa: SLF001
    assert seen == ["tcp_up_8081"]
