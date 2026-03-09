#!/usr/bin/env python3
#
# app/utils/settings_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized settings cache with TTL-based invalidation.

Implementation is in the compiled Cython extension.
"""

from app._cython.settings_cache import (
	CACHE_MAX_SIZE,
	CACHE_TTL_SECONDS,
	get_feature_flags,
	get_last_backup_at,
	get_pdf_page_size,
	get_setting_cached,
	get_signal_api_base_url,
	get_signal_api_recipient,
	get_theme,
	invalidate_settings_cache,
	is_auth_disabled,
	is_purge_enabled,
	use_utc_dashboard,
)

__all__ = [
	"CACHE_MAX_SIZE",
	"CACHE_TTL_SECONDS",
	"get_feature_flags",
	"get_last_backup_at",
	"get_pdf_page_size",
	"get_setting_cached",
	"get_signal_api_base_url",
	"get_signal_api_recipient",
	"get_theme",
	"invalidate_settings_cache",
	"is_auth_disabled",
	"is_purge_enabled",
	"use_utc_dashboard",
]

