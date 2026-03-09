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


# --- Target/Check Status (aggregate level) ---
class CheckStatus(StrEnum):
    """
    Status values for ping/http/tcp/cert checks at target/aggregate level.
    
    Use for: Target overall status, check results, dashboard display.
    """
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"
    PARTIAL = "partial"      # Some checks pass, some fail
    ERROR = "error"          # Check execution error
    DISABLED = "disabled"    # Target/check explicitly disabled


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


# --- Alert Events (state transitions) ---
class AlertEvent(StrEnum):
    """
    Alert event types - these are state TRANSITIONS, not states.
    
    Use for: Alert records, notification triggers, event logs.
    """
    DOWN = "down"              # Target went down
    UP = "up"                  # Target came up (from down)
    RECOVERED = "recovered"    # Target recovered after degraded
    DEGRADED = "degraded"      # Target partially failing
    CERT_EXPIRING = "cert_expiring"
    CERT_EXPIRED = "cert_expired"


# --- Alert Severity (for filtering/reporting) ---
class AlertSeverity(StrEnum):
    """Severity levels for alerts and notifications."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# --- Notification Channels ---
class NotificationChannel(StrEnum):
    """Notification delivery channels."""
    EMAIL = "email"
    SIGNAL = "signal"
    WEBHOOK = "webhook"


# --- UI Display Constants (EN defaults, i18n-ready) ---
# These are display strings, not domain logic. Mark as _EN for future i18n.
DISPLAY_UNGROUPED_EN = "Ungrouped"
DISPLAY_UNKNOWN_EN = "Unknown"
DISPLAY_DASH = "–"  # En-dash, language-neutral


# --- Date/Time Formats ---
DATE_FORMAT_ISO = "%Y-%m-%d"
DATETIME_FORMAT_ISO = "%Y-%m-%dT%H:%M:%S"
DATETIME_FORMAT_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"  # UTC with Z suffix
DATE_FORMAT_DISPLAY = "%d.%m.%Y"
DATETIME_FORMAT_DISPLAY = "%d.%m.%Y %H:%M"
TIME_FORMAT_DISPLAY = "%H:%M"
TIME_FORMAT_DISPLAY_SECONDS = "%H:%M:%S"


# --- Alert Status (for filtering) ---
class AlertStatus(StrEnum):
    """
    Alert lifecycle states for filtering and display.
    
    Use for: Alert list filtering, status badges.
    Note: Different from AlertEvent (transitions) - these are states.
    """
    OPEN = "open"        # Alert is active, not yet resolved
    CLOSED = "closed"    # Alert has been resolved/acknowledged


# --- UI Pagination & Display Defaults ---
# Centralized to avoid magic numbers scattered across codebase
UI_PAGE_SIZE_ALERTS = 50          # Alerts list pagination
UI_PAGE_SIZE_TARGETS = 50         # Targets list pagination
UI_PAGE_SIZE_DEFAULT = 20         # Generic pagination default

# Uptime percentage thresholds for color coding
UPTIME_THRESHOLD_GREEN = 99.0     # >= 99% = green/success
UPTIME_THRESHOLD_YELLOW = 95.0    # >= 95% = yellow/warning, < 95% = red/danger

# Sorting sentinel for "ungrouped" items (high ASCII pushes to bottom)
# Why "~~~"? Tilde (0x7E) is highest printable ASCII, ensuring ungrouped
# targets sort after all named groups alphabetically.
SORT_SENTINEL_LAST = "~~~"


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
    SIGNAL_RATE_LIMIT_UNTIL = "signal_rate_limit_until"  # ISO timestamp until which Signal is globally rate-limited
    SIGNAL_MESSAGE_TEMPLATE_DOWN = "signal_message_template_down"
    SIGNAL_MESSAGE_TEMPLATE_UP = "signal_message_template_up"
    
    # SMTP Email
    SMTP_ENABLED = "smtp_enabled"
    SMTP_HOST = "smtp_host"
    SMTP_PORT = "smtp_port"
    SMTP_USER = "smtp_user"
    SMTP_PASSWORD = "smtp_password"
    SMTP_USE_TLS = "smtp_use_tls"
    SMTP_FROM = "smtp_from"
    
    # Data Management
    PURGE_ENABLED = "purge_enabled"
    
    # UI Preferences
    USE_UTC_DASHBOARD = "use_utc_dashboard"
    THEME = "theme"
    PDF_PAGE_SIZE = "pdf_page_size"
    METRICS_TIMEOUT_MINUTES = "metrics_timeout_minutes"
    
    # System
    LAST_BACKUP_AT = "last_backup_at"
    AUTH_DISABLED = "auth_disabled"
    SYSTEM_BASE_URL = "system_base_url"
    TELEMETRY_ENABLED = "telemetry_enabled"