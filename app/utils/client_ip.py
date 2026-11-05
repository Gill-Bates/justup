#!/usr/bin/env python3
#
# app/utils/client_ip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Trusted-proxy aware client IP resolution shared by auth and audit logging."""

from __future__ import annotations

import ipaddress
import os
import re
from functools import lru_cache

from starlette.requests import Request

TRUSTED_PROXIES_ENV = "TRUSTED_PROXIES"
_NODE_PORT_RE = re.compile(r":(?:[0-9]{1,5}|_[A-Za-z0-9._-]+)")


@lru_cache(maxsize=8)
def _parse_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # Misconfigured entry: ignore it, which fails closed (no trust).
            continue
    return tuple(nets)


def trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks whose peers may set proxy headers (re-read per call; parsing is cached)."""
    return _parse_networks(os.getenv(TRUSTED_PROXIES_ENV, "").strip())


def _is_trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``addr`` is in a trusted network; an IPv4-mapped IPv6 address matches as IPv4."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in trusted_proxy_networks())


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return None


def _split_quoted(text: str, sep: str) -> list[str]:
    """Split on ``sep`` outside RFC 7230 quoted strings (backslash escapes honored)."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
        elif in_quotes and ch == "\\":
            buf.append(ch)
            escaped = True
        elif ch == '"':
            buf.append(ch)
            in_quotes = not in_quotes
        elif ch == sep and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def _node_ip(node: str) -> str | None:
    """Parse an RFC 7239 node (``ip[:port]``, ``[ipv6][:port]``); None for unknown/obfuscated."""
    if node.startswith("["):
        host, sep, rest = node[1:].partition("]")
        if not sep or (rest and not _NODE_PORT_RE.fullmatch(rest)):
            return None
        return _valid_ip(host)
    if node.count(":") == 1:
        node, _, port = node.partition(":")
        if not _NODE_PORT_RE.fullmatch(":" + port):
            return None
    return _valid_ip(node)


def _forwarded_for_entries(header: str) -> list[str | None]:
    """One entry per Forwarded element, left to right: its ``for`` IP or None if unusable."""
    entries: list[str | None] = []
    for element in _split_quoted(header, ","):
        if not element.strip():
            continue
        found: str | None = None
        for pair in _split_quoted(element, ";"):
            name, sep, raw = pair.partition("=")
            if sep and name.strip().lower() == "for":
                found = _node_ip(_unquote(raw))
                break
        entries.append(found)
    return entries


def _client_from_chain(entries: list[str | None]) -> str | None:
    """Walk a proxy chain right to left past trusted hops; None if an entry is invalid.

    Proxies that only append leave the left side client-controlled, so the first
    untrusted hop from the right is the client. If every hop is trusted, the leftmost wins.
    """
    candidate: str | None = None
    for candidate in reversed(entries):
        if candidate is None:
            return None
        if not _is_trusted(ipaddress.ip_address(candidate)):
            return candidate
    return candidate


def _client_from_xff(header: str) -> str | None:
    """Resolve the client from X-Forwarded-For (see ``_client_from_chain``)."""
    return _client_from_chain([_valid_ip(part) for part in header.split(",") if part.strip()])


def resolve_trusted_client_ip(request: Request) -> str:
    """Return the client IP, honoring proxy headers only from a trusted immediate peer.

    An unparsable peer is returned as-is and a missing peer yields ``"unknown"``.
    """
    peer = request.client.host if request.client and request.client.host else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not _is_trusted(peer_ip):
        return str(peer_ip)

    xff_values = request.headers.getlist("X-Forwarded-For")
    if xff_values:
        # Fail safe: an unusable chain never falls through to weaker headers.
        return _client_from_xff(",".join(xff_values)) or str(peer_ip)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and (candidate := _valid_ip(real_ip)):
        return candidate
    forwarded_values = request.headers.getlist("Forwarded")
    if forwarded_values:
        return _client_from_chain(_forwarded_for_entries(",".join(forwarded_values))) or str(peer_ip)
    return str(peer_ip)


def normalize_ip_bucket(value: str) -> str:
    """Reduce a client address to its rate-limit bucket.

    IPv4 stays as is, an IPv4-mapped IPv6 address becomes its IPv4 form, and any other
    IPv6 address becomes its /64 prefix so rotating inside an allocation gains nothing.
    Non-IP values (``"unknown"``, an unparsable peer) are returned unchanged.

    ``"2001:db8::1" -> "2001:db8::/64"``, ``"::ffff:192.0.2.1" -> "192.0.2.1"``.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    if addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(ipaddress.IPv6Network((addr, 64), strict=False))


def audit_client_ip(request: Request) -> str:
    """Client IP for audit logs: a valid address or ``"unknown"``."""
    return _valid_ip(resolve_trusted_client_ip(request)) or "unknown"


def request_is_https(request: Request) -> bool:
    """True if the request is HTTPS, trusting X-Forwarded-Proto only from a trusted peer."""
    peer = request.client.host if request.client and request.client.host else ""
    try:
        trusted = _is_trusted(ipaddress.ip_address(peer))
    except ValueError:
        trusted = False
    if trusted:
        # Rightmost value: the one set by the closest proxy, like the right-to-left X-Forwarded-For walk.
        proto = ",".join(request.headers.getlist("X-Forwarded-Proto")).split(",")[-1].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"
