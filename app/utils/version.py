#!/usr/bin/env python3
#
# app/utils/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Single source of truth for application version.

Reads from VERSION file at project root.
"""

from __future__ import annotations

from pathlib import Path

_VERSION_CACHE: str | None = None
_BUILD_INFO_CACHE: str | None = None
APP_NAME = "justUp!"


def get_build_info() -> str:
    """
    Get build info (Git commit hash) from BUILD_INFO file.
    
    Falls back to 'dev' if file is missing or unreadable.
    Result is cached for performance.
    """
    global _BUILD_INFO_CACHE
    
    if _BUILD_INFO_CACHE is not None:
        return _BUILD_INFO_CACHE
    
    try:
        # app/utils/version.py -> app/utils -> app -> project root
        build_file = Path(__file__).resolve().parent.parent.parent / "BUILD_INFO"
        
        if not build_file.exists():
            # Fallback for Docker: /app/BUILD_INFO
            build_file = Path("/app/BUILD_INFO")
        
        if build_file.exists():
            _BUILD_INFO_CACHE = build_file.read_text(encoding="utf-8").strip()
        else:
            _BUILD_INFO_CACHE = "dev"
    except Exception:
        _BUILD_INFO_CACHE = "dev"
    
    return _BUILD_INFO_CACHE


def get_version() -> str:
    """
    Get application version from VERSION file.
    
    Falls back to 'dev' if file is missing or unreadable.
    Result is cached for performance.
    """
    global _VERSION_CACHE
    
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE
    
    try:
        # app/utils/version.py -> app/utils -> app -> project root
        version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
        
        if not version_file.exists():
            # Fallback for Docker: /app/VERSION
            version_file = Path("/app/VERSION")
        
        if version_file.exists():
            _VERSION_CACHE = version_file.read_text(encoding="utf-8").strip()
        else:
            _VERSION_CACHE = "dev"
    except Exception:
        _VERSION_CACHE = "dev"
    
    return _VERSION_CACHE


# Module-level constants for convenient import
VERSION = get_version()
BUILD_INFO = get_build_info()
