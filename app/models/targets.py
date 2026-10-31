#!/usr/bin/env python3
#
# app/models/targets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for monitored targets and their configuration."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from ..utils.constants import (
    TARGET_INTERVAL_SECONDS_MAX,
    TARGET_INTERVAL_SECONDS_MIN,
    TARGET_MAX_RETRIES_MAX,
    TARGET_MAX_RETRIES_MIN,
    TARGET_RETENTION_DAYS_MAX,
    TARGET_RETENTION_DAYS_MIN,
)
from ..utils.http_status import DEFAULT_HTTP_SUCCESS_CODES, parse_status_code_spec

# ─── Constants ────────────────────────────────────────────────────────────────
# Default TCP port for new targets (HTTP). Users should explicitly set 443 for HTTPS-only.
DEFAULT_TCP_PORTS = "80"


# ─── Helper Functions ─────────────────────────────────────────────────────────


def _validate_http_codes(v) -> str | None:
    """Validate and normalize HTTP status code specification.

    Shared validator for both TargetBase and TargetUpdate.
    Returns None for empty input, raises ValueError on invalid spec.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        parse_status_code_spec(s)
    except ValueError as e:
        raise ValueError(f"Invalid http_success_codes: {e}") from e
    return s


_HOST_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def validate_host(value: str | None) -> str | None:
    """Validate a hostname (RFC 1123) or IP literal; return the stripped value.

    Free text is rejected so a host can never be read as an option by ping/mtr.
    """
    if value is None:
        return None
    host = str(value).strip()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"Invalid host '{host}': invalid internationalized domain") from exc
    name = host.removesuffix(".")
    if len(name) > 253 or not all(_HOST_LABEL_RE.match(label) for label in name.split(".")):
        raise ValueError(f"Invalid host '{host}': expected an RFC 1123 hostname or an IP literal")
    return host


def normalize_ports(value, *, default: str | None = None) -> str | None:
    """
    Normalize TCP ports input to a canonical comma-separated string.

    Accepts:
      - int: single port number
      - list/tuple: multiple port numbers
      - str: comma or space-separated ports
      - None or empty: returns `default`

    Args:
            value: The input value to normalize
            default: Value to return for None/empty input.
                     Use DEFAULT_TCP_PORTS for Create (new target needs valid port),
                     Use None for Update (PATCH semantics: None = no change).

    Returns:
            Normalized comma-separated port string, or default if input is empty.
            Duplicates are removed while preserving order.

    Raises:
            ValueError: If any port is out of range (1-65535) or not a valid integer.
    """
    if value is None or value == "":
        return default

    # Reject bool before int check (bool is subclass of int in Python)
    if isinstance(value, bool):
        raise ValueError(f"Invalid TCP port: boolean not allowed (got {value})")

    if isinstance(value, int):
        if not 1 <= value <= 65535:
            raise ValueError(f"Invalid TCP port: {value} out of range (1-65535)")
        return str(value)

    if isinstance(value, (list, tuple)):
        seen: set[int] = set()
        ports: list[str] = []
        for p in value:
            try:
                p_int = int(p)
            except (ValueError, TypeError) as e:
                raise ValueError(f'Invalid TCP port: "{p}" is not a valid integer') from e
            if not 1 <= p_int <= 65535:
                raise ValueError(f"Invalid TCP port: {p_int} out of range (1-65535)")
            if p_int not in seen:
                seen.add(p_int)
                ports.append(str(p_int))
        return ",".join(ports) if ports else default

    # String: split by comma or space
    v_str = str(value).strip()
    if not v_str:
        return default

    parts = re.split(r"[,\s]+", v_str)
    seen: set[int] = set()
    ports: list[str] = []
    for raw_p in parts:
        p = raw_p.strip()
        if not p:
            continue
        try:
            p_int = int(p)
            if not 1 <= p_int <= 65535:
                raise ValueError(f"Invalid TCP port: {p_int} out of range (1-65535)")
            if p_int not in seen:
                seen.add(p_int)
                ports.append(str(p_int))
        except ValueError as e:
            if "Invalid TCP port" in str(e):
                raise
            raise ValueError(f"Invalid TCP port: {p}") from e

    return ",".join(ports) if ports else default


class TargetBase(BaseModel):
    """Base model for target configuration (create/read)."""

    name: str = Field(min_length=1, max_length=200)
    group: str | None = Field(default=None, max_length=100)
    is_enabled: bool = True
    host: str = Field(min_length=1, max_length=255, description="Central host domain or IP for all checks")

    # Scheduling / retention
    interval_seconds: int = Field(default=60, ge=TARGET_INTERVAL_SECONDS_MIN, le=TARGET_INTERVAL_SECONDS_MAX)
    retention_days: int = Field(default=365, ge=TARGET_RETENTION_DAYS_MIN, le=TARGET_RETENTION_DAYS_MAX)

    # Which checks
    enable_ping: bool = False
    enable_http_check: bool = Field(default=False)
    enable_cert_expiration: bool = False
    enable_tcp_connect: bool = False

    # Check params
    ping_host: str | None = Field(default=None, max_length=255)
    http_url: HttpUrl | None = None
    http_username: str | None = Field(default=None, max_length=128)
    http_password: SecretStr | None = Field(default=None, description="HTTP Basic Auth password (encrypted at rest)")
    clear_http_password: bool = Field(default=False, description="Set to true to explicitly clear stored credentials")
    http_prefer_head: bool = Field(
        default=True, description="Use HEAD request instead of GET (avoids affecting visitor counters)"
    )
    http_basic_auth_enabled: bool = Field(default=False, description="If true, send stored HTTP Basic Auth credentials")
    http_port: int | None = Field(
        default=None, ge=1, le=65535, description="Optional request port override (only used if URL has no port)"
    )
    http_success_codes: str | None = Field(
        default=None,
        max_length=200,
        description=f"HTTP success status codes (e.g. '{DEFAULT_HTTP_SUCCESS_CODES}', '2xx,3xx', '200-499')",
    )
    cert_host: str | None = Field(default=None, max_length=255)
    cert_port: int | None = Field(
        default=None, ge=1, le=65535, description="SSL port; auto-detected from URL scheme if empty"
    )
    ignore_cert_errors: bool = Field(default=False, description="Ignore SSL certificate validation errors")
    tcp_host: str | None = Field(default=None, max_length=255)
    tcp_ports: str = Field(
        default=DEFAULT_TCP_PORTS, max_length=200, description="Comma-separated list of TCP ports (e.g. '80,443,8080')"
    )
    max_retries: int = Field(
        default=5,
        ge=TARGET_MAX_RETRIES_MIN,
        le=TARGET_MAX_RETRIES_MAX,
        description="Max check retries within one check cycle before marking target DOWN",
    )

    # Alert behavior
    auto_close_alert: bool = Field(default=True, description="Automatically close alert when target recovers")

    @field_validator("host", "ping_host", "cert_host", "tcp_host", mode="before")
    @classmethod
    def validate_hosts(cls, v):
        """Enforce the host contract (hostname or IP literal)."""
        return validate_host(v)

    @field_validator("group", mode="before")
    @classmethod
    def normalize_group(cls, v):
        """Normalize empty group to None for consistency with name min_length."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return v

    @field_validator("tcp_ports", mode="before")
    @classmethod
    def normalize_tcp_ports(cls, v):
        """Normalize TCP ports. For Create, empty → DEFAULT_TCP_PORTS (new target needs valid port)."""
        return normalize_ports(v, default=DEFAULT_TCP_PORTS)

    @field_validator("http_success_codes", mode="before")
    @classmethod
    def validate_http_success_codes(cls, v):
        return _validate_http_codes(v)

    # SLA Targets (optional)
    sla_enabled: bool = Field(default=False, description="Enable SLA targets for this monitor")
    sla_availability_pct: float | None = Field(
        default=None, ge=0, le=100, description="SLA target: availability percentage (yearly average)"
    )
    sla_response_time_ms: float | None = Field(
        default=None, ge=0, description="SLA target: HTTP response time in ms (yearly average)"
    )
    sla_ping_latency_ms: float | None = Field(
        default=None, ge=0, description="SLA target: ping latency in ms (yearly average)"
    )

    @model_validator(mode="after")
    def validate_consistency(self):
        """
        Validate cross-field consistency for target creation.

        INTENTIONALLY PERMISSIVE: This validator performs basic sanity checks.
        The API layer (_validate_target_consistency) is STRICTER and enforces
        additional business rules after normalization. See that function's
        docstring for the full list of differences.

        Model checks (here):
        - Basic field presence for enabled checks
        - Credential consistency (auth enabled → creds present)
        - Password clear/set conflict

        API checks (stricter):
        - HTTP check requires http_url (not just host fallback)
        - Cert expiration requires HTTP+HTTPS and remains user-selectable
        - Full validation after hostname normalization
        """
        host = self.host

        # Check-specific host validations (host acts as universal fallback)
        if self.enable_http_check and not (self.http_url or host):
            raise ValueError("HTTP check enabled but no http_url or host configured")

        if self.enable_ping and not (self.ping_host or host):
            raise ValueError("Ping check enabled but no ping_host or host configured")

        if self.enable_cert_expiration and not (self.cert_host or self.http_url or host):
            raise ValueError("Cert check enabled but no cert_host, http_url or host configured")

        if self.enable_tcp_connect and not (self.tcp_host or host):
            raise ValueError("TCP check enabled but no tcp_host or host configured")

        # HTTP Basic Auth: credentials required if enabled
        if self.http_basic_auth_enabled:
            if not self.http_username:
                raise ValueError("HTTP Basic Auth enabled but username missing")
            # SecretStr("") is truthy - check actual value
            if not self.http_password or not self.http_password.get_secret_value():
                raise ValueError("HTTP Basic Auth enabled but password missing")

        # Cannot set and clear password simultaneously
        if self.clear_http_password and self.http_password:
            raise ValueError("Cannot set and clear http_password at the same time")

        # SLA: at least one target required if enabled
        if self.sla_enabled:
            targets = [self.sla_availability_pct, self.sla_response_time_ms, self.sla_ping_latency_ms]
            if not any(t is not None for t in targets):
                raise ValueError("SLA enabled but no SLA targets configured")

        return self


class TargetCreate(TargetBase):
    """Request model for creating a target."""

    alert_recipient_ids: list[int] = Field(default_factory=list, max_length=200)


class TargetWizardPreview(BaseModel):
    """Address and optional settings used to preview a wizard profile."""

    profile: Literal["website", "api", "server", "tcp", "custom"]
    address: str = Field(min_length=1, max_length=2048)
    name: str | None = Field(default=None, max_length=200)
    group: str | None = Field(default=None, max_length=100)
    tcp_ports: str | None = Field(default=None, max_length=200)
    overrides: dict = Field(default_factory=dict)


class TargetUpdate(BaseModel):
    """Request model for partial target updates (PATCH).

    PATCH Semantics:
    - Only fields in model_fields_set were explicitly sent by client.
    - Use model_fields_set to distinguish "field not sent" from "field=null".
    - API layer merges with DB values before validation.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    group: str | None = Field(default=None, max_length=100)
    is_enabled: bool | None = None
    host: str | None = Field(default=None, max_length=255)

    interval_seconds: int | None = Field(default=None, ge=TARGET_INTERVAL_SECONDS_MIN, le=TARGET_INTERVAL_SECONDS_MAX)
    retention_days: int | None = Field(default=None, ge=TARGET_RETENTION_DAYS_MIN, le=TARGET_RETENTION_DAYS_MAX)

    enable_ping: bool | None = None
    enable_http_check: bool | None = None
    enable_cert_expiration: bool | None = None
    enable_tcp_connect: bool | None = None

    ping_host: str | None = Field(default=None, max_length=255)
    http_url: HttpUrl | None = None
    http_username: str | None = Field(default=None, max_length=128)
    http_password: SecretStr | None = Field(default=None, description="HTTP Basic Auth password (encrypted at rest)")
    clear_http_password: bool = Field(default=False, description="Set to true to explicitly clear stored credentials")
    http_prefer_head: bool | None = None
    http_basic_auth_enabled: bool | None = None
    http_port: int | None = Field(default=None, ge=1, le=65535)
    http_success_codes: str | None = Field(default=None, max_length=200)
    cert_host: str | None = Field(default=None, max_length=255)
    cert_port: int | None = Field(default=None, ge=1, le=65535)
    ignore_cert_errors: bool | None = None
    tcp_host: str | None = Field(default=None, max_length=255)
    tcp_ports: str | None = Field(default=None, max_length=200, description="Comma-separated list of TCP ports")
    max_retries: int | None = Field(default=None, ge=TARGET_MAX_RETRIES_MIN, le=TARGET_MAX_RETRIES_MAX)

    # Alert behavior
    auto_close_alert: bool | None = None

    @field_validator("host", "ping_host", "cert_host", "tcp_host", mode="before")
    @classmethod
    def validate_hosts(cls, v):
        """Enforce the host contract (hostname or IP literal)."""
        return validate_host(v)

    @field_validator("group", mode="before")
    @classmethod
    def normalize_group(cls, v):
        """Normalize empty group to None for consistency."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return v

    @field_validator("tcp_ports", mode="before")
    @classmethod
    def normalize_tcp_ports(cls, v):
        """Normalize TCP ports. For Update, empty → None (PATCH semantics: no change)."""
        return normalize_ports(v, default=None)

    @field_validator("http_success_codes", mode="before")
    @classmethod
    def validate_http_success_codes(cls, v):
        return _validate_http_codes(v)

    # SLA Targets
    sla_enabled: bool | None = None
    sla_availability_pct: float | None = Field(default=None, ge=0, le=100)
    sla_response_time_ms: float | None = Field(default=None, ge=0)
    sla_ping_latency_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_update_consistency(self):
        """
        Validate cross-field consistency for partial updates.

        Note: PATCH semantics - we only validate fields that are explicitly set.
        Full consistency (e.g., SLA enabled with targets) is validated server-side
        after merging with existing DB values.
        """
        # Cannot set and clear password simultaneously
        if self.clear_http_password and self.http_password:
            raise ValueError("Cannot set and clear http_password at the same time")

        # If explicitly enabling basic auth, credentials are expected in the same
        # request, but only validate when explicitly enabling (not when updating
        # other fields). Allow the empty case: the user might be relying on
        # existing credentials, and server-side validation catches missing
        # credentials after merge.
        if self.http_basic_auth_enabled is True and not self.http_username and not self.http_password:
            pass

        return self


class Target(TargetBase):
    """Internal model with all fields including secrets."""

    id: int
    created_at: datetime
    updated_at: datetime


class TargetPublic(BaseModel):
    """Public response model - never exposes secrets like http_password."""

    id: int
    name: str
    group: str | None = None
    is_enabled: bool
    host: str
    interval_seconds: int
    retention_days: int
    enable_ping: bool
    enable_http_check: bool
    enable_cert_expiration: bool
    enable_tcp_connect: bool
    ping_host: str | None = None
    http_url: HttpUrl | None = None
    http_username: str | None = None
    has_http_password: bool = Field(default=False, description="True if HTTP Basic Auth password is configured")
    http_prefer_head: bool = True
    http_basic_auth_enabled: bool = False
    http_port: int | None = None
    http_success_codes: str | None = None
    cert_host: str | None = None
    cert_port: int | None = None
    ignore_cert_errors: bool = False
    tcp_host: str | None = None
    tcp_ports: str = DEFAULT_TCP_PORTS
    max_retries: int = 5
    # Alert behavior
    auto_close_alert: bool = True
    # SLA Targets
    sla_enabled: bool = False
    sla_availability_pct: float | None = None
    sla_response_time_ms: float | None = None
    sla_ping_latency_ms: float | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("http_url", when_used="json")
    def serialize_http_url(self, url: HttpUrl | None) -> str | None:
        """Serialize HttpUrl to string for JSON responses."""
        return str(url) if url else None


def target_row_to_dict(row) -> dict:
    """Map a ``targets`` DB row to primitives shared by the API model and UI templates.

    The HTTP password never leaves the row; only ``has_http_password`` is exposed.
    """
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "group": row["group_name"],
        "is_enabled": bool(row["is_enabled"]),
        "host": row["host"],
        "interval_seconds": int(row["interval_seconds"]),
        "retention_days": int(row["retention_days"]),
        "enable_ping": bool(row["enable_ping"]),
        "enable_http_check": bool(row["enable_http_check"]),
        "enable_cert_expiration": bool(row["enable_cert_expiration"]),
        "enable_tcp_connect": bool(row["enable_tcp_connect"]),
        "ping_host": row["ping_host"],
        "http_url": row["http_url"],
        "http_username": row["http_username"],
        "has_http_password": bool(row["http_password"]),
        "http_prefer_head": bool(row["http_prefer_head"]),
        "http_basic_auth_enabled": bool(row["http_basic_auth_enabled"]),
        "http_port": int(row["http_port"]) if row["http_port"] else None,
        "http_success_codes": str(row["http_success_codes"] or DEFAULT_HTTP_SUCCESS_CODES),
        "cert_host": row["cert_host"],
        "cert_port": int(row["cert_port"]) if row["cert_port"] else None,
        "ignore_cert_errors": bool(row["ignore_cert_errors"]),
        "tcp_host": row["tcp_host"],
        "tcp_ports": str(row["tcp_ports"] or DEFAULT_TCP_PORTS),
        "max_retries": int(row["max_retries"]),
        "auto_close_alert": bool(row["auto_close_alert"]),
        "sla_enabled": bool(row["sla_enabled"]),
        "sla_availability_pct": float(row["sla_availability_pct"]) if row["sla_availability_pct"] is not None else None,
        "sla_response_time_ms": float(row["sla_response_time_ms"]) if row["sla_response_time_ms"] is not None else None,
        "sla_ping_latency_ms": float(row["sla_ping_latency_ms"]) if row["sla_ping_latency_ms"] is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
