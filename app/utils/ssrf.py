#!/usr/bin/env python3
#
# app/utils/ssrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared SSRF guard for outbound scanners: resolve once, validate, return the address to use."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_METADATA_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def _unwrap(ip: IPAddress) -> IPAddress:
    """Judge an IPv4-mapped IPv6 address (::ffff:a.b.c.d) by its IPv4 part."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_blocked_ip(ip: IPAddress, *, allow_private: bool) -> bool:
    """True if the address must not be contacted.

    Always blocked: loopback, link-local (incl. cloud metadata), multicast, reserved, unspecified.
    With ``allow_private=False`` only globally routable addresses pass (RFC 1918, CGNAT, ULA, ... blocked).
    """
    ip = _unwrap(ip)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return not allow_private and not ip.is_global


def resolve_validated(host: str, *, allow_private: bool) -> list[IPAddress] | None:
    """Resolve ``host`` once and return its addresses, or None if any address is blocked.

    The caller must connect to a returned address, not re-resolve the name.
    Unresolvable hosts return None (fail closed).
    """
    host = host.strip().strip("[]").rstrip(".")
    if not host or host.lower() in _METADATA_HOSTNAMES:
        return None
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            return None
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip not in addresses:
                addresses.append(ip)
    if not addresses or any(is_blocked_ip(ip, allow_private=allow_private) for ip in addresses):
        return None
    return addresses


def redact_url(url: str, *, keep_path: bool = False) -> str:
    """Return scheme://host[:port] for logs; userinfo, query and fragment may carry secrets.

    The path is dropped too, because it can hold tokens (webhook URLs); opt in with ``keep_path``.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path if keep_path else ""
        return f"{parsed.scheme}://{host}{port}{path}"
    except ValueError:
        return "<unparseable-url>"
