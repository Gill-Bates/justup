#!/usr/bin/env python3
#
# app/models/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for application settings stored in the database."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class Settings(BaseModel):
	"""Full settings payload returned by the settings API."""

	signal_enabled: bool = Field(default=False, description="Global on/off switch for Signal alerting")
	purge_enabled: bool = True
	use_utc_dashboard: bool = False
	downsample_enabled: bool = Field(default=True, description="Enable metric downsampling/rollups")
	geoip_auto_update: bool = Field(default=True, description="Auto-update GeoLite2 database on startup")
	telemetry_enabled: bool = Field(default=True, description="Enable anonymous usage telemetry")
	theme: str = Field(default="system", pattern="^(system|light|dark)$", description="UI theme")
	last_backup_at: Optional[str] = Field(default=None, description="ISO timestamp of last backup")
	auth_disabled: bool = Field(default=False, description="Disable authentication (public access)")
	pdf_page_size: str = Field(default="a4", pattern="^(a4|letter)$", description="PDF report paper size")
	metrics_timeout_minutes: int = Field(default=5, ge=1, le=10, description="Global timeout for metric alerts (minutes)")


class SettingsUpdate(BaseModel):
	"""Patch payload for updating settings."""
	signal_enabled: Optional[bool] = None
	signal_api_sender_number: Optional[str] = Field(default=None, description="Signal sender phone number (E.164 format)")
	purge_enabled: Optional[bool] = None
	use_utc_dashboard: Optional[bool] = None
	downsample_enabled: Optional[bool] = None
	geoip_auto_update: Optional[bool] = None
	telemetry_enabled: Optional[bool] = None
	theme: Optional[str] = Field(default=None, pattern="^(system|light|dark)$")
	pdf_page_size: Optional[str] = Field(default=None, pattern="^(a4|letter)$")
	metrics_timeout_minutes: Optional[int] = Field(default=None, ge=1, le=10)
	# auth_disabled is intentionally omitted and requires a dedicated endpoint.
	
	# SMTP Email settings
	smtp_enabled: Optional[bool] = None
	smtp_host: Optional[str] = None
	smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
	smtp_user: Optional[str] = None
	smtp_password: Optional[str] = None
	smtp_use_tls: Optional[bool] = None
	smtp_from: Optional[str] = None
