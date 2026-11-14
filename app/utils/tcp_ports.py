#!/usr/bin/env python3
#
# app/utils/tcp_ports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The one strict rule for TCP port lists: validated on write, executed by the scheduler."""

from __future__ import annotations

import re
from decimal import Decimal
from fractions import Fraction
from typing import Any

PORT_MIN = 1
PORT_MAX = 65535


def parse_port(token: object) -> int:
    """Return one valid port or raise ``ValueError``.

    Only ``int``, ``str`` and integral ``float``/``Decimal``/``Fraction`` values are accepted; bool,
    fractions, non-finite numbers, non-ASCII digits and other types are rejected before any ``int()``
    conversion could truncate them.
    """
    if isinstance(token, bool):
        raise ValueError(f"Invalid TCP port: {token!r} is not an integer")
    if isinstance(token, str):
        text = token.strip()
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"Invalid TCP port: '{text}' (must be a number)")
        port = int(text)
    elif isinstance(token, int):
        port = token
    elif isinstance(token, (float, Decimal, Fraction)):
        if not _is_whole(token):
            raise ValueError(f"Invalid TCP port: {token!r} is not an integer")
        port = int(token)
    else:
        raise ValueError(f"Invalid TCP port: unsupported type {type(token).__name__}")
    if not PORT_MIN <= port <= PORT_MAX:
        raise ValueError(f"Invalid TCP port: {port} out of range ({PORT_MIN}-{PORT_MAX})")
    return port


def _is_whole(number: float | Decimal | Fraction) -> bool:
    if isinstance(number, Fraction):
        return number.denominator == 1
    if isinstance(number, Decimal):
        return number.is_finite() and number == number.to_integral_value()
    return number.is_integer()  # False for nan and inf


# The one default TCP port: new targets, the check scheduler (empty value) and the PDF report all use it.
DEFAULT_TCP_PORTS = "80"


def tcp_ports_or_default(value: Any) -> list[int]:
    """Parsed TCP ports, or the default port when the value is empty; invalid entries raise ``ValueError``."""
    return parse_port_list(value) or parse_port_list(DEFAULT_TCP_PORTS)


def protocol_scan_hosts(target: Any) -> dict[str, str]:
    """Return the host each protocol is probed on: TCP uses ``tcp_host`` (else ``host``), UDP uses ``host``.

    ``target`` may be a mapping or ``sqlite3.Row``; a missing or empty value gives an empty string.
    """

    def field(key: str) -> str:
        try:
            return str(target[key] or "")
        except (KeyError, IndexError):
            return ""

    host = field("host")
    return {"tcp": field("tcp_host") or host, "udp": host}


def parse_port_list(value: Any) -> list[int]:
    """Parse ports from an int, a list or a comma/space separated string.

    Returns the unique ports in ascending order; None or an empty value gives ``[]`` (the caller picks the
    default). Any invalid entry raises ``ValueError``: a port list is never silently shortened or replaced.
    """
    if value is None:
        return []
    if isinstance(value, bool):
        raise ValueError(f"Invalid TCP port: boolean not allowed (got {value})")
    if isinstance(value, (list, tuple)):
        tokens: list[object] = list(value)
    elif isinstance(value, int):
        tokens = [value]
    else:
        tokens = [t for t in re.split(r"[,\s]+", str(value).strip()) if t]
    return sorted({parse_port(t) for t in tokens})
