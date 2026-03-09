#!/usr/bin/env python3
#
# app/monitor/checks.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Network check implementations (ping, HTTP, TCP, certificate) with SSRF safeguards.

Implementation is in the compiled Cython extension.
"""

from app._cython.checks import (
	BLOCKED_HOSTNAMES,
	BLOCKED_NETWORKS,
	CheckResult,
	ConnectivityStatus,
	DEFAULT_USER_AGENT,
	DNS_NEG_CACHE_MAX_SIZE,
	MAX_CONCURRENT_DNS,
	MAX_CONCURRENT_PINGS,
	MAX_CONCURRENT_TCP,
	MAX_ERROR_DETAIL_LENGTH,
	cert_expiration,
	check_connectivity,
	has_connectivity,
	http_head,
	ping,
	tcp_connect,
)

__all__ = [
	"BLOCKED_HOSTNAMES",
	"BLOCKED_NETWORKS",
	"CheckResult",
	"ConnectivityStatus",
	"DEFAULT_USER_AGENT",
	"DNS_NEG_CACHE_MAX_SIZE",
	"MAX_CONCURRENT_DNS",
	"MAX_CONCURRENT_PINGS",
	"MAX_CONCURRENT_TCP",
	"MAX_ERROR_DETAIL_LENGTH",
	"cert_expiration",
	"check_connectivity",
	"has_connectivity",
	"http_head",
	"ping",
	"tcp_connect",
]
