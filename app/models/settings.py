#!/usr/bin/env python3
#
# app/models/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for application settings stored in the database."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..utils.constants import (
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    PROVIDER_LOOKUP_CACHE_TTL_MAX,
    PROVIDER_LOOKUP_CACHE_TTL_MIN,
    QUALITY_MIN_SCORE_DEFAULT,
    QUALITY_RTT_THRESHOLD_MS_DEFAULT,
    RATE_LIMIT_AUTH_DEFAULT,
    SESSION_MAX_DAYS_DEFAULT,
    SESSION_MAX_DAYS_MAX,
    SESSION_MAX_DAYS_MIN,
)


class Settings(BaseModel):
    """Full settings payload returned by the settings API."""

    signal_enabled: bool = Field(default=False, description="Global on/off switch for Signal alerting")
    purge_enabled: bool = True
    use_utc_dashboard: bool = False
    geoip_auto_update: bool = Field(default=True, description="Auto-update GeoLite2 database on startup")
    theme: str = Field(default="system", pattern="^(system|light|dark)$", description="UI theme")
    last_backup_at: str | None = Field(default=None, description="ISO timestamp of last backup")
    auth_disabled: bool = Field(default=False, description="Disable authentication (public access)")
    pdf_page_size: str = Field(default="a4", pattern="^(a4|letter)$", description="PDF report paper size")
    metrics_timeout_minutes: int = Field(
        default=5, ge=1, le=10, description="Global timeout for metric alerts (minutes)"
    )
    session_max_days: int = Field(
        default=SESSION_MAX_DAYS_DEFAULT,
        ge=SESSION_MAX_DAYS_MIN,
        le=SESSION_MAX_DAYS_MAX,
        description="Absolute session lifetime in days, counted from login",
    )
    rate_limit_auth: str = Field(default=RATE_LIMIT_AUTH_DEFAULT, description="Login/auth rate limit, e.g. 10/minute")
    quality_targets: str = Field(default="", description="Comma-separated quality probe targets; empty = defaults")
    quality_min_score: int = Field(default=QUALITY_MIN_SCORE_DEFAULT, ge=0, le=100)
    quality_rtt_threshold_ms: int = Field(default=QUALITY_RTT_THRESHOLD_MS_DEFAULT, ge=1, le=10000)
    provider_lookup_enabled: bool = True
    provider_lookup_cache_ttl_seconds: int = Field(
        default=PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
        ge=PROVIDER_LOOKUP_CACHE_TTL_MIN,
        le=PROVIDER_LOOKUP_CACHE_TTL_MAX,
    )
    ip_api_key_set: bool = Field(default=False, description="Whether an ip-api key is stored (value is never returned)")
    online_geo_enabled: bool = False


class SettingsUpdate(BaseModel):
    """Patch payload for updating settings.

    Unknown fields are rejected (``extra="forbid"``) so a stale client that still sends the
    removed POP refresh fields gets a 422 instead of having them silently accepted.
    """

    model_config = ConfigDict(extra="forbid")

    signal_enabled: bool | None = None
    signal_api_sender_number: str | None = Field(default=None, description="Signal sender phone number (E.164 format)")
    purge_enabled: bool | None = None
    use_utc_dashboard: bool | None = None
    geoip_auto_update: bool | None = None
    theme: str | None = Field(default=None, pattern="^(system|light|dark)$")
    pdf_page_size: str | None = Field(default=None, pattern="^(a4|letter)$")
    metrics_timeout_minutes: int | None = Field(default=None, ge=1, le=10)
    session_max_days: int | None = Field(default=None, ge=SESSION_MAX_DAYS_MIN, le=SESSION_MAX_DAYS_MAX)
    rate_limit_auth: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,5}/(second|minute|hour|day)$")
    quality_targets: str | None = Field(default=None, max_length=2000)
    quality_min_score: int | None = Field(default=None, ge=0, le=100)
    quality_rtt_threshold_ms: int | None = Field(default=None, ge=1, le=10000)
    provider_lookup_enabled: bool | None = None
    provider_lookup_cache_ttl_seconds: int | None = Field(
        default=None, ge=PROVIDER_LOOKUP_CACHE_TTL_MIN, le=PROVIDER_LOOKUP_CACHE_TTL_MAX
    )
    ip_api_key: str | None = Field(default=None, max_length=256)  # write-only; empty string clears
    online_geo_enabled: bool | None = None
    # auth_disabled is intentionally omitted and requires a dedicated endpoint.
    # downsample_enabled was removed: rollups and compression always run (stale clients get a 422).
    # pop_refresh_interval_seconds / pop_refresh_initial_delay_seconds were removed: POP refresh is
    # now self-managed from the pop_import_runs history. Old payloads carrying these fields are
    # rejected as unknown input rather than silently stored (see model_config below).

    # SMTP Email settings
    smtp_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool | None = None
    smtp_from: str | None = None
