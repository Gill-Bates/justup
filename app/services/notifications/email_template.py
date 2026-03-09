#!/usr/bin/env python3
#
# app/services/notifications/email_template.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Email template renderer with secure placeholder replacement.

Supports predefined wildcards for alert notification emails.
Uses explicit whitelist-based replacement:
- Only ALLOWED_PLACEHOLDERS are replaced
- No eval, no Jinja, no runtime code execution
- Unknown/invalid placeholders are replaced with empty strings (safe fallback)
- No access to attributes, methods, or callables

Design Notes:
- DEFAULT_HTML_TEMPLATE uses table-based layout for max email client compatibility
- Works in Gmail, Outlook (including Win32), Apple Mail
- Inline styles only, no external assets
- Status display (emoji, color, text) is auto-mapped from alert status
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any

_log = logging.getLogger(__name__)

# Top-level import (fail-fast at module load, not runtime)
try:
    from ...utils.formatters import format_duration
except ImportError:
    # Fallback for standalone testing or broken package structure
    def format_duration(seconds: int) -> str:
        """Fallback duration formatter."""
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m" if mins else f"{hours}h"


# Logo URL for emails (hosted on GitHub, always reachable, no dependency on instance URL)
LOGO_URL = "https://raw.githubusercontent.com/Gill-Bates/justUp/f41b710f4d4a9bdbfc164f630338c478864631e0/static/img/justup_sw_email.png"

# Logo URL for TinyMCE preview (local static file, works in browser preview)
LOGO_URL_PREVIEW = "/static/justup_1c.png"

# Public API exports
__all__ = [
	"ALLOWED_PLACEHOLDERS",
	"DEFAULT_SUBJECT",
	"DEFAULT_HTML_TEMPLATE",
	"STATUS_DISPLAY_MAP",
	"render_template",
	"build_alert_context",
	"LOGO_URL",
	"LOGO_URL_PREVIEW",
]


# ============================================================================
# Whitelist of allowed placeholders (explicit, no dynamic access)
# ============================================================================

ALLOWED_PLACEHOLDERS = frozenset({
	# Target-related
	"target.name",
	"target.host",
	"target.group",
	"target.check_type",
	"target.check_badges",  # Pre-rendered HTML badges
	# Alert-related
	"alert.status",
	"alert.started_at",
	"alert.resolved_at",
	"alert.duration",
	"alert.reason",
	# Status display (emoji, color, text for templates)
	"status.emoji",
	"status.color",
	"status.text",
	# Downtime
	"downtime.minutes",
	"downtime.human",
	# SLA
	"sla.threshold",
	"sla.actual",
	# System/Meta
	"system.name",
	"system.url",
	"system.timestamp",
	"system.github_url",
	"logo_url",
})

# Pre-compiled regex: only allows alphanumeric, underscores, and dots
# Rejects pipes, spaces, special chars, double-dots, etc.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# Placeholders that contain pre-rendered HTML (not escaped)
_RAW_HTML_PLACEHOLDERS = frozenset({
	"target.check_badges",
})

# Default email subject template (emoji for clear status visibility)
DEFAULT_SUBJECT = "{{status.emoji}} {{target.name}} – {{status.text}}"

# Default HTML email template (table-based for max compatibility)
# Mobile-first responsive design - works in Gmail, Outlook, Apple Mail
# Uses display:block on mobile for stacked layout
DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en"
      xmlns:v="urn:schemas-microsoft-com:vml"
      xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="UTF-8">

  <!-- iOS Mail essentials -->
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">

  <title>Status Notification</title>

  <!--[if mso]>
  <noscript>
    <xml>
      <o:OfficeDocumentSettings>
        <o:PixelsPerInch>96</o:PixelsPerInch>
      </o:OfficeDocumentSettings>
    </xml>
  </noscript>
  <![endif]-->

  <style>
    /* ====== RESETS ====== */
    body, table, td, a {
      -webkit-text-size-adjust: 100%;
      -ms-text-size-adjust: 100%;
    }
    table, td {
      mso-table-lspace: 0pt;
      mso-table-rspace: 0pt;
    }
    img {
      -ms-interpolation-mode: bicubic;
      border: 0; height: auto;
      line-height: 100%; outline: none;
      text-decoration: none;
    }
    body {
      margin: 0; padding: 0;
      width: 100% !important;
      -webkit-font-smoothing: antialiased;
    }

    /* ====== DARK MODE – Apple Mail ====== */
    @media (prefers-color-scheme: dark) {
      body, .mail-bg {
        background-color: #2b2d31 !important;
        color: #f9fafb !important;
      }
      .mail-card {
        background-color: #1f2937 !important;
        color: #f9fafb !important;
      }
      .mail-label { color: #9ca3af !important; }
      .mail-divider { border-top-color: #374151 !important; }
      .mail-footer {
        background-color: #111827 !important;
        color: #9ca3af !important;
      }
      a { color: #93c5fd !important; }
      .badge-check {
        background-color: #166534 !important;
        color: #bbf7d0 !important;
      }
    }

    /* ====== DARK MODE – Outlook.com / Office 365 ====== */
    [data-ogsc] body, [data-ogsc] .mail-bg {
      background-color: #2b2d31 !important;
      color: #f9fafb !important;
    }
    [data-ogsc] .mail-card {
      background-color: #1f2937 !important;
      color: #f9fafb !important;
    }
    [data-ogsc] .mail-label { color: #9ca3af !important; }
    [data-ogsc] .mail-divider { border-top-color: #374151 !important; }
    [data-ogsc] .mail-footer {
      background-color: #111827 !important;
      color: #9ca3af !important;
    }
    [data-ogsc] a { color: #93c5fd !important; }
    [data-ogsc] .badge-check {
      background-color: #166534 !important;
      color: #bbf7d0 !important;
    }

    /* ====== MOBILE ====== */
    @media only screen and (max-width: 600px) {

      /* Card randlos auf Mobile */
      .wrapper-td {
        padding: 0 !important;
      }
      .mail-card {
        border-radius: 0 !important;
      }

      /* weniger Innenabstand in der Card */
      .card-padding {
        padding-left: 14px !important;
        padding-right: 14px !important;
      }

      /* Label / Value Zeilen stapeln */
      .stack .mail-value {
        display: block !important;
        width: 100% !important;
        max-width: 100% !important;
        min-width: 100% !important;
        box-sizing: border-box !important;
        padding-left: 0 !important;
        padding-right: 0 !important;
      }
      .stack .mail-label {
        display: block !important;
        width: 100% !important;
        padding-bottom: 0 !important;
        font-weight: 600 !important;
      }

      /* Badges: wrappen auf Mobile */
      .badge-check {
        font-size: 11px !important;
        padding: 3px 8px !important;
      }

      /* Footer stapeln */
      .footer-stack td {
        display: block !important;
        width: 100% !important;
        text-align: center !important;
        padding: 4px 0 !important;
      }
      .footer-logo {
        margin: 6px auto 0 auto !important;
      }

      /* Top/Bottom Spacer auf Mobile minimal */
      .spacer-top td { height: 8px !important; }
      .spacer-bottom td { height: 8px !important; }
    }
  </style>
</head>

<body style="margin:0;padding:0;word-spacing:normal;background-color:#f5f7fa;">

<!-- OUTER BACKGROUND -->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
       class="mail-bg"
       style="background-color:#f5f7fa;color:#1f2937;">
  <tr>
    <td align="center" style="padding:0;">

      <!-- TOP SPACER (iOS safe-area) -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="spacer-top">
        <tr><td height="20" style="font-size:0;line-height:0;">&nbsp;</td></tr>
      </table>

      <!-- WRAPPER -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
        <tr>
          <td align="center" class="wrapper-td" style="padding:16px;">

            <!-- CARD (max 600 px, shrinks to 100 %) -->
            <!--[if mso]>
            <table role="presentation" align="center" width="600" cellspacing="0" cellpadding="0" border="0">
            <tr><td>
            <![endif]-->
            <table role="presentation" cellspacing="0" cellpadding="0" border="0"
                   class="mail-card"
                   style="max-width:600px;width:100%;background-color:#fafafa;
                          border-radius:8px;overflow:hidden;color:#1f2937;">

              <!-- STATUS BANNER -->
              <tr>
                <td style="height:6px;background-color:{{status.color}};
                           font-size:1px;line-height:1px;">&nbsp;</td>
              </tr>

              <!-- HEADER -->
              <tr>
                <td class="card-padding"
                    style="padding:16px 20px;
                           font-size:18px;font-weight:bold;line-height:1.3;">
                  {{status.emoji}} {{status.text}}
                </td>
              </tr>

              <!-- CONTENT -->
              <tr>
                <td class="card-padding"
                    style="padding:16px 20px;font-size:14px;line-height:1.6;">

                  <!-- ===== TARGET ===== -->
                  <strong style="display:block;margin-bottom:8px;">Target</strong>

                  <table role="presentation" width="100%" cellspacing="0"
                         cellpadding="0" class="stack" style="table-layout:fixed;">
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Name
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{target.name}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Host
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{target.host}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Group
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{target.group}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:6px 0;white-space:nowrap;">
                        Check
                      </td>
                      <td valign="top" class="mail-value" style="padding:4px 0;">
                        {{target.check_badges}}
                      </td>
                    </tr>
                  </table>

                  <hr class="mail-divider"
                      style="border:none;border-top:1px solid #d1d5db;margin:16px 0;">

                  <!-- ===== INCIDENT ===== -->
                  <strong style="display:block;margin-bottom:8px;">Incident</strong>

                  <table role="presentation" width="100%" cellspacing="0"
                         cellpadding="0" class="stack" style="table-layout:fixed;">
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Status
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{alert.status}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Started
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{alert.started_at}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Resolved
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{alert.resolved_at}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Duration
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{alert.duration}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Reason
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{alert.reason}}
                      </td>
                    </tr>
                  </table>

                  <hr class="mail-divider"
                      style="border:none;border-top:1px solid #d1d5db;margin:16px 0;">

                  <!-- ===== SLA ===== -->
                  <strong style="display:block;margin-bottom:8px;">SLA</strong>

                  <table role="presentation" width="100%" cellspacing="0"
                         cellpadding="0" class="stack" style="table-layout:fixed;">
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Downtime
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{downtime.human}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Threshold
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{sla.threshold}}
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" width="140" class="mail-label"
                          style="width:140px;color:#9ca3af;
                                 font-size:13px;padding:4px 0;white-space:nowrap;">
                        Actual
                      </td>
                      <td valign="top" class="mail-value"
                          style="padding:4px 0;font-weight:500;overflow-wrap:break-word;">
                        {{sla.actual}}
                      </td>
                    </tr>
                  </table>

                </td>
              </tr>

              <!-- FOOTER -->
              <tr>
                <td class="mail-footer"
                    style="padding:14px 16px;
                           background-color:#f1f3f6;
                           color:#9ca3af;
                           font-size:12px;
                           line-height:1.6;">
                  <table role="presentation" width="100%" cellspacing="0"
                         cellpadding="0" class="footer-stack">
                    <tr>
                      <td style="text-align:left;vertical-align:middle;">
                        justUp! &copy; 2026 Gill-Bates &middot;
                        <a href="{{system.github_url}}"
                           style="color:#9ca3af;text-decoration:underline;">
                          GitHub
                        </a>
                      </td>
                      <td style="text-align:right;vertical-align:middle;width:60px;">
                        <img src="{{logo_url}}" alt="justUp! Logo"
                             class="footer-logo"
                             style="height:35px;width:auto;display:block;
                                    margin-left:auto;">
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

            </table>
            <!-- /CARD -->
            <!--[if mso]>
            </td></tr></table>
            <![endif]-->

          </td>
        </tr>
      </table>
      <!-- /WRAPPER -->

      <!-- BOTTOM SPACER -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="spacer-bottom">
        <tr><td height="24" style="font-size:0;line-height:0;">&nbsp;</td></tr>
      </table>

    </td>
  </tr>
</table>
<!-- /OUTER -->

</body>
</html>
"""


def render_template(template: str, context: Dict[str, Any]) -> str:
	"""
	Render email template by replacing {{placeholder}} with context values.
	
	Security:
	- Only placeholders in ALLOWED_PLACEHOLDERS are replaced
	- Unknown placeholders are replaced with empty string
	- No code execution, no attribute access
	
	Args:
		template: Email template with {{placeholder}} syntax
		context: Dictionary with placeholder values
		
	Returns:
		Rendered template with placeholders replaced
	"""
	# Early exit: no placeholders to replace
	if "{{" not in template:
		return template
	
	def replace_placeholder(match: re.Match) -> str:
		placeholder = match.group(1).strip()
		
		# DEBUG: Log what's being replaced
		_log.debug(
			"Placeholder: %r, allowed: %s, raw_html: %s",
			placeholder,
			placeholder in ALLOWED_PLACEHOLDERS,
			placeholder in _RAW_HTML_PLACEHOLDERS,
		)
		
		# Whitelist check: reject unknown placeholders
		if placeholder not in ALLOWED_PLACEHOLDERS:
			return ""
		
		# Nested dict access (e.g., "target.name" -> context["target"]["name"])
		parts = placeholder.split(".")
		value = context
		for part in parts:
			if isinstance(value, dict):
				value = value.get(part, "")
			else:
				return ""
		
		raw = str(value) if value is not None else ""
		
		# DEBUG: Log the value
		_log.debug(
			"Placeholder %r → value length: %d, starts with: %r",
			placeholder,
			len(raw),
			raw[:80] if len(raw) > 0 else "",
		)
		
		# Pre-rendered HTML: pass through without escaping
		if placeholder in _RAW_HTML_PLACEHOLDERS:
			_log.debug("Placeholder %r is RAW HTML, not escaping", placeholder)
			return raw
		
		# CSS injection protection for color values
		if placeholder == "status.color":
			if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
				return "#6b7280"  # Fallback to gray
			return raw
		
		# XSS protection: HTML-escape all other values
		_log.debug("Placeholder %r is being escaped", placeholder)
		return html.escape(raw)
	
	return _PLACEHOLDER_RE.sub(replace_placeholder, template)


def _format_timestamp(ts: str | datetime | None, fallback: str = "N/A") -> str:
	"""
	Format a timestamp string or datetime to consistent UTC format.
	
	Args:
		ts: ISO timestamp string, datetime object, or None
		fallback: Value to return if ts is None or unparseable
		
	Returns:
		Formatted timestamp string (YYYY-MM-DD HH:MM:SS UTC)
	"""
	if ts is None:
		return fallback
	
	if isinstance(ts, datetime):
		return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
	
	if isinstance(ts, str):
		try:
			# Handle ISO format with Z suffix
			dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
			return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
		except (ValueError, AttributeError):
			return ts  # Return raw string if unparseable
	
	return fallback


def _format_sla(value: float | str | None) -> str:
	"""
	Format SLA value as percentage string.
	
	Args:
		value: SLA value (float like 99.9 or string like "99.9%")
		
	Returns:
		Formatted percentage string (e.g., "99.90%")
	"""
	if value is None:
		return "N/A"
	
	if isinstance(value, (int, float)):
		return f"{value:.2f}%"
	
	if isinstance(value, str):
		# Already formatted or N/A
		return value
	
	return "N/A"


# Status display mapping: status -> (emoji, color, text)
# Used for visual distinction in email subject and HTML header
STATUS_DISPLAY_MAP: Dict[str, tuple[str, str, str]] = {
	"DOWN":     ("🔴", "#dc2626", "Service DOWN"),
	"OPEN":     ("🔴", "#dc2626", "Service DOWN"),
	"UP":       ("🟢", "#16a34a", "Service UP"),
	"RESOLVED": ("🟢", "#16a34a", "Service RESOLVED"),
	"WARN":     ("🟡", "#ca8a04", "Degraded"),
	"WARNING":  ("🟡", "#ca8a04", "Degraded"),
	"UNKNOWN":  ("⚪", "#6b7280", "Unknown"),
}

# Default fallback for unknown status values
_DEFAULT_STATUS_DISPLAY = ("⚪", "#6b7280", "Unknown")

# Check badge colors: check_type -> (background, text_color)
CHECK_BADGE_COLORS: Dict[str, tuple[str, str]] = {
	"PING":  ("#198754", "#ffffff"),  # Bootstrap bg-success green / white
	"HTTP":  ("#198754", "#ffffff"),  # Bootstrap bg-success
	"HTTPS": ("#198754", "#ffffff"),  # Bootstrap bg-success
	"TCP":   ("#dbeafe", "#1e40af"),  # blue
	"DNS":   ("#fef9c3", "#854d0e"),  # yellow
}
_DEFAULT_BADGE = ("#f3f4f6", "#374151")  # gray


def _render_check_badges(check_type: str) -> str:
	"""
	Render check types as inline HTML badges for email.
	
	Input:  "HTTP + PING"  or  "HTTP,PING"  or  "HTTPS"
	Output: Table-based badge HTML safe for all email clients.
	"""
	# Handle None explicitly (defensive)
	if check_type is None:
		check_type = ""
	
	# Normalize separators
	raw = str(check_type).strip()
	
	# Handle empty, "None" string (from str(None)), or "N/A"
	if not raw or raw.upper() in ("NONE", "N/A"):
		return "<span style='color:#9ca3af;'>N/A</span>"
	
	# Split on common separators: +  ,  ;  whitespace
	parts = re.split(r"[+,;\s]+", raw)
	parts = [p.strip().upper() for p in parts if p.strip()]
	
	if not parts:
		return "<span style='color:#9ca3af;'>N/A</span>"
	
	badges: list[str] = []
	for check in parts:
		bg, fg = CHECK_BADGE_COLORS.get(check, _DEFAULT_BADGE)
		# Each badge is a mini-table (bulletproof in email clients)
		# Use inline-block span wrapped in VML for Outlook fallback
		badge_html = (
			f'<span class="badge-check" style="display:inline-block;'
			f"background-color:{bg};color:{fg};"
			f"font-size:12px;font-weight:bold;line-height:1;"
			f"padding:4px 10px;margin:0 4px 4px 0;"
			f"border-radius:12px;"
			f'mso-line-height-rule:exactly;">'
			f"{html.escape(check)}"
			f"</span>"
		)
		badges.append(badge_html)
	
	# Simple inline wrapper — spans flow naturally
	inner = "\n".join(badges)
	return f'<div style="line-height:1.8;">{inner}</div>'


def _sanitize_reason(reason: str, *, target_host: str, check_type: str) -> str:
	"""
	Make `alert.reason` user-friendly and avoid redundant host repetition.

	Examples:
	- "PING example.com" -> "PING"
	- "TCP example.com:443,8443" -> "TCP 443,8443"
	"""
	reason = str(reason or "").strip()
	if not reason:
		return "N/A"

	check_type_u = str(check_type or "").strip().upper()
	host_norm = str(target_host or "").strip().lower().rstrip(".")

	# PING failures are rendered as "PING <host>" by the scheduler; drop the host.
	if reason.upper().startswith("PING "):
		return "PING"

	# TCP failures are rendered as:
	# - "TCP <host>:<ports>" / "TCP <host>" / "TCP :<ports>"
	if reason.upper().startswith("TCP "):
		rest = reason[4:].strip()
		rest_lower = rest.lower().rstrip(".")

		if host_norm and (rest_lower == host_norm or rest_lower.startswith(host_norm + ":") or rest_lower.startswith(host_norm + ".")):
			after = ""
			# rest may be "<host>" or "<host>:<ports>" or "<host>.<suffix>" (rare).
			if rest_lower.startswith(host_norm + "."):
				after = rest[len(host_norm) + 1 :].strip()
			else:
				after = rest[len(host_norm) :].strip()

			if after.startswith(":"):
				ports = after[1:].strip()
				return f"TCP {ports}" if ports else "TCP"
			return "TCP"

		if rest.startswith(":"):
			ports = rest[1:].strip()
			return f"TCP {ports}" if ports else "TCP"

	# Generic fallback: strip "<CHECK_TYPE> <host>" if it exactly matches target host.
	if check_type_u and host_norm:
		prefix = f"{check_type_u} {host_norm}"
		if reason.strip().upper() == prefix.upper():
			return check_type_u

	return reason


def build_alert_context(alert_data: Dict[str, Any], target_data: Dict[str, Any]) -> Dict[str, Any]:
	"""
	Build template context from alert and target data.
	
	All timestamps are formatted consistently to "YYYY-MM-DD HH:MM:SS UTC".
	SLA values are formatted as percentages (e.g., "99.90%").
	Status display (emoji, color, text) is derived from alert status.
	
	Args:
		alert_data: Alert information (status, started_at, etc.)
		target_data: Target information (name, host, group, etc.)
		
	Returns:
		Context dictionary for template rendering
	"""
	# Parse timestamps for duration calculation
	started_at_raw = alert_data.get("started_at")
	resolved_at_raw = alert_data.get("resolved_at")
	
	# Calculate duration
	duration = ""
	if started_at_raw and resolved_at_raw:
		# Alert resolved: calculate full duration
		try:
			started = datetime.fromisoformat(str(started_at_raw).replace("Z", "+00:00"))
			resolved = datetime.fromisoformat(str(resolved_at_raw).replace("Z", "+00:00"))
			duration_seconds = int((resolved - started).total_seconds())
			duration = format_duration(duration_seconds)
		except (ValueError, AttributeError, TypeError):
			duration = "N/A"
	elif started_at_raw:
		# Alert still active: calculate duration from start until now
		try:
			started = datetime.fromisoformat(str(started_at_raw).replace("Z", "+00:00"))
			now = datetime.now(timezone.utc)
			duration_seconds = int((now - started).total_seconds())
			duration = f"{format_duration(duration_seconds)} (ongoing)"
		except (ValueError, AttributeError, TypeError):
			duration = "Ongoing"
	else:
		duration = "N/A"
	
	# Format timestamps consistently
	started_at = _format_timestamp(started_at_raw)
	resolved_at = _format_timestamp(resolved_at_raw, fallback="Ongoing")
	
	# Calculate downtime
	downtime_minutes = alert_data.get("downtime_minutes", 0) or 0
	downtime_human = format_duration(downtime_minutes * 60) if downtime_minutes > 0 else "None"
	
	# Format SLA values
	sla_threshold = _format_sla(alert_data.get("sla_threshold"))
	sla_actual = _format_sla(alert_data.get("sla_actual"))
	
	# If SLA is disabled (both N/A), hide downtime as well
	if sla_threshold == "N/A" and sla_actual == "N/A":
		downtime_minutes = 0
		downtime_human = "N/A"
	
	# Get status display values (emoji, color, text)
	alert_status = str(alert_data.get("status", "UNKNOWN")).upper()
	status_emoji, status_color, status_text = STATUS_DISPLAY_MAP.get(
		alert_status, _DEFAULT_STATUS_DISPLAY
	)

	# Sanitize reason and render check badges
	reason_raw = alert_data.get("reason", "N/A")
	target_host = str(target_data.get("host", ""))
	check_type_raw = target_data.get("check_type") or ""  # None/NULL → ""
	reason = _sanitize_reason(reason_raw, target_host=target_host, check_type=check_type_raw)
	check_badges = _render_check_badges(check_type_raw)
	
	# Build context with all values pre-formatted
	context = {
		"status": {
			"emoji": status_emoji,
			"color": status_color,
			"text": status_text,
		},
		"target": {
			"name": target_data.get("name", "Unknown"),
			"host": target_data.get("host", "N/A"),
			"group": target_data.get("group", "Default"),
			"check_type": check_type_raw,  # Plain text (for subject)
			"check_badges": check_badges,   # Pre-rendered HTML
		},
		"alert": {
			"status": alert_data.get("status", "UNKNOWN"),
			"started_at": started_at,
			"resolved_at": resolved_at,
			"duration": duration,
			"reason": reason,
		},
		"downtime": {
			"minutes": str(downtime_minutes),
			"human": downtime_human,
		},
		"sla": {
			"threshold": sla_threshold,
			"actual": sla_actual,
		},
		"system": {
			"name": "justUp",
			"url": alert_data.get("system_url", "#"),
			"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
			"github_url": "https://github.com/Gill-Bates/justup",
		},
		"logo_url": LOGO_URL,
	}
	
	return context
