#!/usr/bin/env python3
#
# app/services/target_profiles.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Deterministic starting settings for the target creation wizard."""

from __future__ import annotations

from urllib.parse import urlsplit

from ..models.targets import TargetCreate, validate_host
from .target_identity import normalize_host

PROFILE_LABELS = {
    "website": "Website",
    "api": "API / health endpoint",
    "server": "Server / host",
    "tcp": "TCP service",
    "custom": "Custom",
}


def build_profile(
    profile: str,
    address: str,
    *,
    name: str | None = None,
    group: str | None = None,
    tcp_ports: str | None = None,
    overrides: dict | None = None,
) -> tuple[TargetCreate, bool]:
    if profile not in PROFILE_LABELS:
        raise ValueError("Unknown target profile")
    address = address.strip()
    if not address:
        raise ValueError("Address is required")
    is_http = profile in {"website", "api"} or (profile == "custom" and "://" in address)
    suggested_https = is_http and "://" not in address
    values: dict = {
        "name": name or address,
        "group": group,
        "interval_seconds": 60,
        "retention_days": 365,
        "max_retries": 2,
        "auto_close_alert": True,
        "http_prefer_head": False,
    }
    if is_http:
        url = address if "://" in address else f"https://{address}"
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("A valid HTTP or HTTPS URL is required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials must not be embedded in the URL")
        values.update(
            host=normalize_host(parsed.hostname),
            enable_http_check=True,
            http_url=url,
            http_success_codes="200-299" if profile == "api" else "200-399",
            enable_cert_expiration=parsed.scheme.lower() == "https",
        )
    else:
        host = validate_host(address)
        if not host:
            raise ValueError("Host is required")
        values["host"] = host
        if profile == "tcp":
            if not tcp_ports or not tcp_ports.strip():
                raise ValueError("TCP ports must be selected explicitly")
            values.update(enable_tcp_connect=True, tcp_ports=tcp_ports)
        else:
            values["enable_ping"] = True
    if overrides:
        forbidden = {"http_password", "clear_http_password", "alert_recipient_ids"}
        if forbidden & overrides.keys():
            raise ValueError("Secrets and recipients cannot be sent in preview overrides")
        allowed = TargetCreate.model_fields.keys()
        unknown = overrides.keys() - allowed
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        values.update(overrides)
    if values.get("enable_tcp_connect"):
        selected_ports = values.get("tcp_ports") or tcp_ports
        if not selected_ports or not str(selected_ports).strip():
            raise ValueError("TCP ports must be selected explicitly")
        # Every profile derives the TCP host from the target unless a valid override is given
        if not values.get("tcp_host"):
            values["tcp_host"] = values["host"]
        values["tcp_ports"] = selected_ports
    return TargetCreate.model_validate(values), suggested_https
