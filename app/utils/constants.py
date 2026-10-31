#!/usr/bin/env python3
#
# app/utils/constants.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Centralized constants to avoid magic strings throughout the codebase.

Usage (defensive):
    status = CheckStatus(row["overall_status"])  # Validates value

    # NOT: status = row["overall_status"]  # Silent propagation of invalid values
"""

from enum import StrEnum
from pathlib import PurePosixPath


# --- Target/Check Status (aggregate level) ---
class CheckStatus(StrEnum):
    """
    Status values for ping/http/tcp/cert checks at target/aggregate level.

    Use for: Target overall status, check results, dashboard display.
    """

    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"
    PARTIAL = "partial"  # Some checks pass, some fail
    ERROR = "error"  # Check execution error
    DISABLED = "disabled"  # Target/check explicitly disabled


# --- MTR/Traceroute Hop Status (per-hop level) ---
class HopStatus(StrEnum):
    """
    Status values for individual traceroute hops.

    Use for: MTR hop results, traceroute visualization.
    Note: No PARTIAL here - that's an aggregate concept (CheckStatus).
    """

    OK = "ok"
    TIMEOUT = "timeout"
    INTERPOLATED = "interpolated"


# --- Segment Types for Geo Visualization ---
class SegmentType(StrEnum):
    """Line segment types for map visualization."""

    SOLID = "solid"
    DASHED = "dashed"


# MaxMind GeoLite2 City database, relative to the data directory (own subfolder, not the data root)
GEOIP_DB_RELPATH = PurePosixPath("geoip") / "GeoLite2-City.mmdb"


# --- Geolocation source (how a hop's location was determined) ---
class GeoSource(StrEnum):
    """How a route node's location was resolved, shared by web UI and PDF."""

    HOSTNAME = "hostname"  # Inferred from rDNS/POP code (backbone hint)
    GEOIP = "geoip"  # MaxMind GeoLite2 with city-level coordinates
    GEOIP_COARSE = "geoip_coarse"  # GeoIP with country/region only, no precise coords
    ONLINE = "online"  # ip-api.com fallback (privacy-sensitive, off by default)
    NONE = "none"  # No location could be determined


# --- Geolocation accuracy (whether a node may be plotted as a point) ---
class GeoAccuracy(StrEnum):
    """Trust level of a route node's location.

    Only ``PRECISE`` nodes carry map coordinates; ``APPROXIMATE`` nodes are
    country/region-level and must not be drawn as a precise router position;
    ``UNKNOWN`` nodes have no location at all.
    """

    PRECISE = "precise"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


# --- UI Display Constants (EN defaults, i18n-ready) ---
# These are display strings, not domain logic. Mark as _EN for future i18n.
DISPLAY_UNGROUPED_EN = "Ungrouped"


# --- Date/Time Formats ---
DATE_FORMAT_ISO = "%Y-%m-%d"


# --- Alert Status (for filtering) ---
class AlertStatus(StrEnum):
    """
    Alert lifecycle states for filtering and display.

    Use for: Alert list filtering, status badges.
    Note: these are states, not transitions.
    """

    OPEN = "open"  # Alert is active, not yet resolved
    RESOLVED = "resolved"  # Alert has been resolved (stored and API value)


# --- UI Pagination & Display Defaults ---
# Centralized to avoid magic numbers scattered across codebase
UI_PAGE_SIZE_ALERTS = 50  # Alerts list pagination
UI_PAGE_SIZE_TARGETS = 50  # Targets list pagination
UI_PAGE_SIZE_DEFAULT = 20  # Generic pagination default

# Uptime percentage thresholds for color coding
UPTIME_THRESHOLD_GREEN = 99.0  # >= 99% = green/success
UPTIME_THRESHOLD_YELLOW = 95.0  # >= 95% = yellow/warning, < 95% = red/danger


# --- Database Setting Keys ---
class SettingKeys(StrEnum):
    """
    Database setting keys (centralized to avoid magic strings).

    All application settings are stored in the SQLite 'settings' table.
    Use these keys for get_setting/set_setting calls.
    """

    # Signal Messenger (local signal-cli)
    SIGNAL_CHANNEL_ENABLED = "signal_enabled"
    SIGNAL_API_SENDER_NUMBER = "signal_api_sender_number"  # Phone number from device link
    SIGNAL_MESSAGE_TEMPLATE_DOWN = "signal_message_template_down"
    SIGNAL_MESSAGE_TEMPLATE_UP = "signal_message_template_up"

    # SMTP Email
    SMTP_ENABLED = "smtp_enabled"
    SMTP_HOST = "smtp_host"
    SMTP_PORT = "smtp_port"
    SMTP_USER = "smtp_user"
    SMTP_PASSWORD = "smtp_password"  # noqa: S105 - settings key name, not a secret value
    SMTP_USE_TLS = "smtp_use_tls"
    SMTP_FROM = "smtp_from"

    # Data Management
    PURGE_ENABLED = "purge_enabled"
    GEOIP_AUTO_UPDATE = "geoip_auto_update"

    # UI Preferences
    USE_UTC_DASHBOARD = "use_utc_dashboard"
    THEME = "theme"
    PDF_PAGE_SIZE = "pdf_page_size"
    METRICS_TIMEOUT_MINUTES = "metrics_timeout_minutes"

    # Security
    SESSION_MAX_DAYS = "session_max_days"
    RATE_LIMIT_AUTH = "rate_limit_auth"

    # Network quality probe
    QUALITY_TARGETS = "quality_targets"  # comma-separated; empty = built-in defaults
    QUALITY_MIN_SCORE = "quality_min_score"
    QUALITY_RTT_THRESHOLD_MS = "quality_rtt_threshold_ms"

    # Provider / geo lookup
    PROVIDER_LOOKUP_ENABLED = "provider_lookup_enabled"
    PROVIDER_LOOKUP_CACHE_TTL_SECONDS = "provider_lookup_cache_ttl_seconds"
    IP_API_KEY = "ip_api_key"  # stored encrypted
    ONLINE_GEO_ENABLED = "online_geo_enabled"

    # System
    LAST_BACKUP_AT = "last_backup_at"
    AUTH_DISABLED = "auth_disabled"
    SYSTEM_BASE_URL = "system_base_url"
    RELEASE_CHECK_STATE = "release_check_state"  # JSON: schedule, ETag and failure streak of the release check


# Absolute session lifetime (days, counted from login); default keeps the former fixed 24 h.
SESSION_MAX_DAYS_MIN = 1
SESSION_MAX_DAYS_MAX = 7
SESSION_MAX_DAYS_DEFAULT = 1

# Valid page sizes for PDF reports (shared by settings validation and report API)
VALID_PDF_PAGE_SIZES = frozenset({"a4", "letter"})

# Defaults seeded at bootstrap and used when a stored setting is missing
DEFAULT_PURGE_ENABLED = True
DEFAULT_GEOIP_AUTO_UPDATE = True
DEFAULT_PDF_PAGE_SIZE = "a4"
DEFAULT_METRICS_TIMEOUT_MINUTES = 5

# --- Runtime setting defaults and bounds (formerly ENV-driven) ---
RATE_LIMIT_AUTH_DEFAULT = "10/minute"
QUALITY_MIN_SCORE_DEFAULT = 70
QUALITY_RTT_THRESHOLD_MS_DEFAULT = 500
QUALITY_MIN_TARGETS = 3
PROVIDER_LOOKUP_CACHE_TTL_DEFAULT = 1800
PROVIDER_LOOKUP_CACHE_TTL_MIN = 60
PROVIDER_LOOKUP_CACHE_TTL_MAX = 86400

# --- Target monitoring limits (shared by API models and UI forms) ---
TARGET_INTERVAL_SECONDS_MIN = 5
TARGET_INTERVAL_SECONDS_MAX = 86_400
TARGET_MAX_RETRIES_MIN = 0
TARGET_MAX_RETRIES_MAX = 10
TARGET_RETENTION_DAYS_MIN = 1
TARGET_RETENTION_DAYS_MAX = 3650
MONITORING_LIMITS = {
    "interval_seconds": {"min": TARGET_INTERVAL_SECONDS_MIN, "max": TARGET_INTERVAL_SECONDS_MAX},
    "max_retries": {"min": TARGET_MAX_RETRIES_MIN, "max": TARGET_MAX_RETRIES_MAX},
    "retention_days": {"min": TARGET_RETENTION_DAYS_MIN, "max": TARGET_RETENTION_DAYS_MAX},
}
