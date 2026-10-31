#!/usr/bin/env python3
#
# app/utils/runtime_fields.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Load and validate the UI-managed runtime settings (formerly ENV-driven)."""

from __future__ import annotations

import re

from fastapi import HTTPException

from ..db import sqlite as sqlite_db
from .constants import (
    PROVIDER_LOOKUP_CACHE_TTL_DEFAULT,
    QUALITY_MIN_SCORE_DEFAULT,
    QUALITY_MIN_TARGETS,
    QUALITY_RTT_THRESHOLD_MS_DEFAULT,
    RATE_LIMIT_AUTH_DEFAULT,
    SettingKeys,
)

# Keys whose stored value must never be null (ip_api_key may be cleared with null/empty)
NON_NULL_RUNTIME_KEYS = frozenset(
    {
        SettingKeys.RATE_LIMIT_AUTH,
        SettingKeys.QUALITY_TARGETS,
        SettingKeys.QUALITY_MIN_SCORE,
        SettingKeys.QUALITY_RTT_THRESHOLD_MS,
        SettingKeys.PROVIDER_LOOKUP_ENABLED,
        SettingKeys.PROVIDER_LOOKUP_CACHE_TTL_SECONDS,
        SettingKeys.ONLINE_GEO_ENABLED,
    }
)

# Also handed to the settings UI (tag input) so client and server validate alike.
QUALITY_TARGET_PATTERN = r"^(https?://)?[A-Za-z0-9][A-Za-z0-9.:\[\]-]{0,252}(/[^\s]*)?$"
QUALITY_TARGET_MAX_LENGTH = 300
_TARGET_RE = re.compile(QUALITY_TARGET_PATTERN)


def validate_quality_targets(raw: str) -> str:
    """Normalise a comma-separated target list; empty means built-in defaults."""
    items = [t.strip() for t in raw.split(",") if t.strip()]
    if not items:
        return ""
    if len(items) < QUALITY_MIN_TARGETS:
        raise HTTPException(status_code=422, detail=f"quality_targets needs at least {QUALITY_MIN_TARGETS} targets")
    for item in items:
        if not _TARGET_RE.match(item) or len(item) > QUALITY_TARGET_MAX_LENGTH:
            raise HTTPException(status_code=422, detail="quality_targets contains an invalid entry")
    return ",".join(items)


def load_runtime_fields(conn) -> dict:
    """Return the runtime settings as Settings-model kwargs (never the ip-api key itself)."""
    get = sqlite_db.get_setting
    return {
        "rate_limit_auth": get(conn, SettingKeys.RATE_LIMIT_AUTH, RATE_LIMIT_AUTH_DEFAULT),
        "quality_targets": get(conn, SettingKeys.QUALITY_TARGETS, "") or "",
        "quality_min_score": get(conn, SettingKeys.QUALITY_MIN_SCORE, QUALITY_MIN_SCORE_DEFAULT),
        "quality_rtt_threshold_ms": get(conn, SettingKeys.QUALITY_RTT_THRESHOLD_MS, QUALITY_RTT_THRESHOLD_MS_DEFAULT),
        "provider_lookup_enabled": bool(get(conn, SettingKeys.PROVIDER_LOOKUP_ENABLED, True)),
        "provider_lookup_cache_ttl_seconds": get(
            conn, SettingKeys.PROVIDER_LOOKUP_CACHE_TTL_SECONDS, PROVIDER_LOOKUP_CACHE_TTL_DEFAULT
        ),
        "ip_api_key_set": bool(get(conn, SettingKeys.IP_API_KEY, None)),
        "online_geo_enabled": bool(get(conn, SettingKeys.ONLINE_GEO_ENABLED, False)),
    }
