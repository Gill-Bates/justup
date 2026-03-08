#!/usr/bin/env python3
#
# app/models/monitors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitor-related Pydantic models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
	v = v.strip()
	if not v or len(v) > 253 or not _HOSTNAME_RE.fullmatch(v):
		raise ValueError("Invalid hostname")
	return v


class MonitorCreate(BaseModel):
	name: str = Field(..., min_length=1, max_length=200)
	monitor_type: Literal["http", "tcp", "ping", "dns", "keyword"] = "http"
	url: str | None = Field(None, max_length=2048)
	hostname: str | None = Field(None, max_length=255)
	port: int | None = Field(None, ge=1, le=65535)
	method: str = Field("GET", max_length=10)
	expected_status_code: int = Field(200, ge=100, le=599)
	keyword: str | None = Field(None, max_length=500)
	keyword_match_type: str = "contains"
	headers_json: str | None = None
	body: str | None = None
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

	@field_validator("name")
	@classmethod
	def validate_name(cls, v: str) -> str:
		return _validate_name(v)

	@field_validator("url")
	@classmethod
	def validate_url(cls, v: str | None) -> str | None:
		if v is not None:
			v = v.strip()
			if not v.startswith(("http://", "https://")):
				raise ValueError("URL must start with http:// or https://")
		return v

	@field_validator("hostname")
	@classmethod
	def validate_hostname(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_hostname(v)
		return v


class MonitorUpdate(BaseModel):
	name: str | None = Field(None, min_length=1, max_length=200)
	monitor_type: Literal["http", "tcp", "ping", "dns", "keyword"] | None = None
	url: str | None = Field(None, max_length=2048)
	hostname: str | None = Field(None, max_length=255)
	port: int | None = Field(None, ge=1, le=65535)
	method: str | None = Field(None, max_length=10)
	expected_status_code: int | None = Field(None, ge=100, le=599)
	keyword: str | None = Field(None, max_length=500)
	timeout_seconds: int | None = Field(None, ge=1, le=120)
	interval_seconds: int | None = Field(None, ge=10, le=86400)
	retries: int | None = Field(None, ge=0, le=10)
	retry_interval_seconds: int | None = Field(None, ge=1, le=300)
	verify_ssl: bool | None = None
	follow_redirects: bool | None = None
	description: str | None = Field(None, max_length=1000)
	tags: str | None = Field(None, max_length=500)
	notification_group_id: int | None = None
	is_active: bool | None = None

	@field_validator("name")
	@classmethod
	def validate_name(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_name(v)
		return v

	@field_validator("hostname")
	@classmethod
	def validate_hostname(cls, v: str | None) -> str | None:
		if v is not None:
			return _validate_hostname(v)
		return v


class MonitorPublic(BaseModel):
	id: int
	name: str
	monitor_type: str
	url: str | None = None
	hostname: str | None = None
	port: int | None = None
	interval_seconds: int
	is_active: bool
	status: str | None = None
	last_check_at: datetime | None = None
	last_response_time_ms: float | None = None
	uptime_pct_24h: float | None = None
	uptime_pct_7d: float | None = None
	uptime_pct_30d: float | None = None
	consecutive_failures: int = 0
