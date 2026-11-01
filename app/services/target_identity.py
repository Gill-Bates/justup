#!/usr/bin/env python3
#
# app/services/target_identity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Semantic identities for monitored endpoints."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit


def normalize_host(value: str | None) -> str | None:
    if not value:
        return None
    host = value.strip().rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.encode("idna").decode("ascii") if host else None


def normalize_url(value: str, *, port_override: int | None = None) -> str:
    parsed = urlsplit(value)
    host = normalize_host(parsed.hostname)
    if not host or parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("A valid HTTP or HTTPS URL is required")
    scheme = parsed.scheme.lower()
    port = parsed.port if parsed.port is not None else port_override
    default_port = 443 if scheme == "https" else 80
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        authority += f":{port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{authority}{path}{query}"


def check_identities(data: dict) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    host = normalize_host(data.get("host"))
    if data.get("enable_ping"):
        result.add(("ping", normalize_host(data.get("ping_host")) or host or ""))
    if data.get("enable_http_check") and data.get("http_url"):
        result.add(("http", normalize_url(str(data["http_url"]), port_override=data.get("http_port"))))
    if data.get("enable_tcp_connect"):
        tcp_host = normalize_host(data.get("tcp_host")) or host or ""
        for port in str(data.get("tcp_ports") or "").split(","):
            if port.strip():
                result.add(("tcp", f"{tcp_host}:{int(port)}"))
    return result


def identity_signature(data: dict) -> str | None:
    """Return a stable signature for the complete set of enabled monitoring checks."""
    identities = check_identities(data)
    return json.dumps(sorted(identities), separators=(",", ":")) if identities else None


def classify_conflicts(data: dict, targets: list, *, exclude_id: int | None = None) -> list[dict]:
    incoming = check_identities(data)
    host = normalize_host(data.get("host"))
    conflicts: list[dict] = []
    for row in targets:
        existing = dict(row)
        if existing["id"] == exclude_id:
            continue
        current = check_identities(existing)
        common = incoming & current
        same_host = host == normalize_host(existing.get("host"))
        if not common and not same_host:
            continue
        classification = "exact" if incoming == current and incoming else "overlap" if common else "related"
        conflicts.append(
            {
                "id": existing["id"],
                "name": existing["name"],
                "classification": classification,
                "check_type": ", ".join(sorted({kind for kind, _ in current})),
            }
        )
    return conflicts
