#!/usr/bin/env python3
#
# app/utils/auth_helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized helpers for resolving the current user.

Keeps auth behavior consistent across API, frontend, and HTMX routes.
Implementation is in the compiled Cython extension.
"""

from app._cython.auth_helpers import (
	User,
	get_fallback_admin,
	get_user_by_token,
	resolve_user_optional,
	resolve_user_or_htmx_redirect,
	resolve_user_or_redirect,
)

__all__ = [
	"User",
	"get_fallback_admin",
	"get_user_by_token",
	"resolve_user_optional",
	"resolve_user_or_htmx_redirect",
	"resolve_user_or_redirect",
]

