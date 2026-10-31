#!/usr/bin/env python3
#
# app/utils/tls_target.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Parse and validate a TLS scan target; pin the scan to one validated address."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .ssrf import IPAddress, resolve_validated

DEFAULT_PORT = 443
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class TlsTargetError(ValueError):
    """The target is malformed or must not be scanned."""


@dataclass(frozen=True)
class TlsTarget:
    hostname: str  # IDNA/ASCII form; also used as SNI for names
    port: int
    ip_address: IPAddress  # the validated address the scan connects to
    is_ip_literal: bool


def _split_host_port(target: str) -> tuple[str, int]:
    target = target.strip()
    if not target:
        raise TlsTargetError("empty target")
    try:
        # A bare IPv6 literal has colons but no port; urlsplit would misread it.
        return str(ipaddress.ip_address(target)), DEFAULT_PORT
    except ValueError:
        pass
    try:
        # urlsplit handles [v6]:port and rejects ports outside 0-65535 on .port access.
        parts = urlsplit(target if "://" in target else f"//{target}")
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise TlsTargetError("malformed target or port") from exc
    if not host:
        raise TlsTargetError("no host in target")
    if port is None:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise TlsTargetError(f"invalid port {port}")
    return host, port


def _normalize_hostname(host: str) -> str:
    host = host.rstrip(".").lower()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise TlsTargetError("invalid hostname") from exc
    if not ascii_host or len(ascii_host) > 253 or not all(_LABEL.match(lbl) for lbl in ascii_host.split(".")):
        raise TlsTargetError("invalid hostname")
    return ascii_host


def resolve_target(target: str, *, allow_private: bool = True) -> TlsTarget:
    """Parse ``target`` (host, host:port, [v6]:port or URL) and resolve it exactly once.

    Raises TlsTargetError for malformed input, unresolvable names and addresses
    blocked by the SSRF guard (loopback, link-local, metadata, ...).
    """
    host, port = _split_host_port(target)
    try:
        ipaddress.ip_address(host)
        is_ip, hostname = True, host
    except ValueError:
        is_ip, hostname = False, _normalize_hostname(host)
    addresses = resolve_validated(hostname, allow_private=allow_private)
    if not addresses:
        raise TlsTargetError("target is unresolvable or not an allowed scan target")
    return TlsTarget(hostname=hostname, port=port, ip_address=addresses[0], is_ip_literal=is_ip)
