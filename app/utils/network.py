#!/usr/bin/env python3
#
# app/utils/network.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Network utility helpers."""

from __future__ import annotations

import ipaddress


def parse_ip_str(value: str | None) -> str | None:
	if not value:
		return None
	raw = value.strip()
	if not raw:
		return None
	try:
		return str(ipaddress.ip_address(raw))
	except ValueError:
		return None
