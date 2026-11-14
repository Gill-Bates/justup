#!/usr/bin/env python3
#
# tests/test_shared_duration_and_ports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import UTC, datetime

from app.services.target_identity import check_identities
from app.utils.time import elapsed_seconds


def test_elapsed_seconds_truncates_toward_zero():
    a = datetime(2026, 1, 1, tzinfo=UTC)
    assert elapsed_seconds(a, a.replace(second=59, microsecond=900000)) == 59
    assert elapsed_seconds(a.replace(second=10), a) == -10


def test_check_identities_uses_the_shared_port_rule():
    data = {
        "host": "example.org",
        "enable_tcp_connect": True,
        "tcp_ports": "443, 80 80",
        "enable_udp_scan": True,
        "udp_ports": "53",
    }
    assert check_identities(data) == {
        ("tcp", "example.org:80"),
        ("tcp", "example.org:443"),
        ("udp", "example.org:53"),
    }
