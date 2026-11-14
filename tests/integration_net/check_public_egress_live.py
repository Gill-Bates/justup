#!/usr/bin/env python3
#
# tests/integration_net/check_public_egress_live.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Live check of the public egress lookup against the real internet (never part of the unit suite).

The file name does not match ``test_*.py`` so a normal ``pytest`` run does not collect it. Run it explicitly:

    JUSTUP_INTEGRATION_NET=1 pytest tests/integration_net/check_public_egress_live.py

It compares the lookup with an independent operator (AWS checkip) queried by ``curl`` from the same
network namespace, and asserts that no Docker-bridge, RFC 1918, CGNAT or other non-public address is shown.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess

import pytest

from app.services import public_egress as pe

INDEPENDENT = {"-4": "https://checkip.amazonaws.com", "-6": "https://checkip.amazonaws.com"}


@pytest.fixture(autouse=True)
def _gate():
    if os.environ.get("JUSTUP_INTEGRATION_NET") != "1":
        pytest.fail("Live network check: set JUSTUP_INTEGRATION_NET=1 to run it.", pytrace=False)
    if shutil.which("curl") is None:
        pytest.fail("curl is required for the independent comparison query.", pytrace=False)
    pe.reset_state()


def _independent(flag: str) -> str | None:
    out = subprocess.run(
        ["curl", flag, "-sS", "--noproxy", "*", "-m", "10", INDEPENDENT[flag]],
        capture_output=True,
        text=True,
        check=False,
    )
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        return None
    return str(ipaddress.ip_address(text))


@pytest.mark.parametrize(("family", "flag"), [("ipv4", "-4"), ("ipv6", "-6")])
def test_lookup_matches_an_independent_query_and_is_never_private(family, flag):
    result = pe.get_public_egress()[family]
    reference = _independent(flag)
    if reference is None and family == "ipv4":
        pytest.fail("No IPv4 internet for the independent query; the check needs it.", pytrace=False)
    if reference is None:  # no IPv6 route: the lookup must agree and show no address
        assert result["status"] in {"unavailable", "failed"}, result
        assert result["address"] is None
        return
    assert result["status"] == "available", result
    assert result["address"] == reference
    ip = ipaddress.ip_address(result["address"])
    assert ip.is_global and not ip.is_private and not ip.is_loopback and not ip.is_link_local
    assert ip not in ipaddress.ip_network("172.16.0.0/12")  # Docker bridge range
