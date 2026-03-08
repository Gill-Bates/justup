#!/usr/bin/env python3
#
# app/models/monitors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitor-related Pydantic models."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^[\w][\w .\-/()]{0,198}[\w)]$")
_HOSTNAME_RE = re.compile(
	r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
	r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _validate_name(v: str) -> str:
	v = v.strip()
	if not v or not _NAME_RE.fullmatch(v):
		raise ValueError(
			"Name must be 1-200 chars and contain only letters, digits, "
			"spaces, hyphens, underscores, dots, slashes, or parentheses"
		)
	return v


def _validate_hostname(v: str) -> str:
	"""Validate hostname or IP address."""
	v = v.strip()
	if not v or len(v) > 253:
		raise ValueError("Invalid hostname")
	# Allow IPv4/IPv6 literals
	try:
		ipaddress.ip_address(v)
		return v
	except ValueError:
		pass
	# Validate as hostname
	if not _HOSTNAME_RE.fullmatch(v):
		raise ValueError("Invalid hostname format")
	return v


class _MonitorFieldValidators:
	"""Shared validators for MonitorCreate and MonitorUpdate."""

	@field_validator("name")
	@classmethod
	def validate_name(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_name(v)
		return v

	@field_validator("url")
	@classmethod
	def validate_url(cls, v: str | None) -> str | None:
		if v is not None:
			from urllib.parse import urlparse
			v = v.strip()
			parsed = urlparse(v)
			if parsed.scheme not in ("http", "https"):
				raise ValueError("URL must use http or https scheme")
			if not parsed.hostname:
				raise ValueError("URL must include a valid hostname")
		return v

	@field_validator("hostname")
	@classmethod
	def validate_hostname(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_hostname(v)
		return v

	@field_validator("headers_json")
	@classmethod
	def validate_headers_json(cls, v: str | None) -> str | None:
		if v is None:
			return v
		try:
			parsed = json.loads(v)
		except (json.JSONDecodeError, TypeError) as exc:
			raise ValueError("headers_json must be valid JSON") from exc
		if not isinstance(parsed, dict):
			raise ValueError("headers_json must be a JSON object")
		if not all(
			isinstance(k, str) and isinstance(val, str)
			for k, val in parsed.items()
		):
			raise ValueError("All header keys and values must be strings")
		return v

	@field_validator("method")
	@classmethod
	def validate_method(cls, v: str | None) -> str | None:
		if v is None:
			return v
		v = v.strip().upper()
		allowed = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
		if v not in allowed:
			raise ValueError(f"Unsupported HTTP method: {v}")
		return v


class MonitorCreate(_MonitorFieldValidators, BaseModel):
	name: str = Field(..., min_length=1, max_length=200)
	monitor_type: Literal["http", "tcp", "ping", "dns", "keyword"] = "http"
	url: str | None = Field(None, max_length=2048)
	hostname: str | None = Field(None, max_length=255)
	port: int | None = Field(None, ge=1, le=65535)
	method: str = Field("GET", max_length=10)
	expected_status_code: int = Field(200, ge=100, le=599)
	keyword: str | None = Field(None, max_length=500)
	keyword_match_type: Literal["contains", "not_contains", "regex"] = "contains"
	headers_json: str | None = None
	body: str | None = Field(None, max_length=65536)
	timeout_seconds: int = Field(30, ge=1, le=120)
	interval_seconds: int = Field(60, ge=10, le=86400)
	retries: int = Field(3, ge=0, le=10)
	retry_interval_seconds: int = Field(10, ge=1, le=300)
	verify_ssl: bool = True
	follow_redirects: bool = True
	max_redirects: int = Field(10, ge=0, le=20)
	description: str | None = Field(None, max_length=1000)
	tags: str | None = Field(None, max_length=500)
	notification_group_id: int | None = None
	is_active: bool = True

	@model_validator(mode="after")
	def check_required_fields_by_type(self) -> MonitorCreate:
		"""Validate that required fields are present for each monitor type."""
		t = self.monitor_type
		if t in ("http", "keyword"):
			if not self.url:
				raise ValueError(f"'url' is required for monitor type '{t}'")
		if t in ("tcp", "ping", "dns"):
			if not self.hostname:
				raise ValueError(f"'hostname' is required for monitor type '{t}'")
		if t == "tcp" and self.port is None:
			raise ValueError("'port' is required for monitor type 'tcp'")
		if t == "keyword" and not self.keyword:
			raise ValueError("'keyword' is required for monitor type 'keyword'")
		return self


class MonitorUpdate(_MonitorFieldValidators, BaseModel):
	name: str | None = Field(None, min_length=1, max_length=200)
	monitor_type: Literal["http", "tcp", "ping", "dns", "keyword"] | None = None
	url: str | None = Field(None, max_length=2048)
	hostname: str | None = Field(None, max_length=255)
	port: int | None = Field(None, ge=1, le=65535)
	method: str | None = Field(None, max_length=10)
	expected_status_code: int | None = Field(None, ge=100, le=599)
	keyword: str | None = Field(None, max_length=500)
	keyword_match_type: Literal["contains", "not_contains", "regex"] | None = None
	headers_json: str | None = None
	body: str | None = Field(None, max_length=65536)
	timeout_seconds: int | None = Field(None, ge=1, le=120)
	interval_seconds: int | None = Field(None, ge=10, le=86400)
	retries: int | None = Field(None, ge=0, le=10)
	retry_interval_seconds: int | None = Field(None, ge=1, le=300)
	verify_ssl: bool | None = None
	follow_redirects: bool | None = None
	max_redirects: int | None = Field(None, ge=0, le=20)
	description: str | None = Field(None, max_length=1000)
	tags: str | None = Field(None, max_length=500)
	notification_group_id: int | None = None
	is_active: bool | None = None


class MonitorPublic(BaseModel):
	id: int
	name: str
	monitor_type: Literal["http", "tcp", "ping", "dns", "keyword"]
	url: str | None = None
	hostname: str | None = None
	port: int | None = None
	interval_seconds: int
	is_active: bool
	status: Literal["up", "down", "pending", "paused"] | None = None
	last_check_at: datetime | None = None
	last_response_time_ms: float | None = None
	uptime_pct_24h: float | None = None
	uptime_pct_7d: float | None = None
	uptime_pct_30d: float | None = None
	consecutive_failures: int = 0
