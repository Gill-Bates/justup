#!/usr/bin/env python3
#
# tests/test_default_tcp_port.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""One default TCP port for the model, the scheduler and the PDF report; each keeps its own handling of bad input."""

from __future__ import annotations

import pytest

from app.models import targets as target_models
from app.monitor import scheduler
from app.services import pdf_worker
from app.utils import tcp_ports
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
