#!/usr/bin/env python3
#
# app/utils/cert_target.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single definition of which host and port the certificate check connects to."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .tcp_ports import parse_port


def _checked_port(value: object, source: str) -> int | None:
    """Return a valid port, or None when the field is unset (None or empty string); 0 is not unset."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return parse_port(value)
    except ValueError as exc:
        raise ValueError(f"Invalid certificate port from {source}: {exc}") from exc


def resolve_cert_target(target: Mapping[str, Any]) -> tuple[str | None, int]:
    """Return ``(host, port)`` of the certificate to check; host is None when none can be derived.

    Explicitly configured certificate fields win, the HTTP settings are fallbacks:
    host: ``cert_host`` > host of ``http_url`` > ``host``.
    port: ``cert_port`` > explicit port in ``http_url`` > ``http_port`` > scheme default (80 for http, else 443).
    Raises ``ValueError`` when a configured port is not a whole number from 1 to 65535.
    """
    parsed = urlparse(str(target["http_url"])) if target.get("http_url") else None
    host = target.get("cert_host") or (parsed.hostname if parsed else None) or target.get("host") or None
    port = _checked_port(target.get("cert_port"), "cert_port")
    if port is None and parsed is not None:
        port = _checked_port(parsed.port, "http_url")  # urllib itself raises ValueError for > 65535
    if port is None:
        port = _checked_port(target.get("http_port"), "http_port")
    if port is None:
        port = 80 if parsed is not None and parsed.scheme == "http" else 443
    return host, port
