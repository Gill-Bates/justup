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
- Optional sections use named, whitelisted conditional blocks (no expressions)

Design Notes:
- DEFAULT_HTML_TEMPLATE uses table-based layout for max email client compatibility
- Works in Gmail, Outlook (including Win32), Apple Mail
- Native design is LIGHT; colours are chosen so client auto-dark-mode stays legible
  (``color-scheme``/``supported-color-schemes`` are set, see the template head)
- The logo is embedded in the message as an inline image referenced by ``cid:`` so it
  renders without any external server; see LOGO_CID / build_email_message()
- Status display (colour, text) is auto-mapped from alert status; never colour alone
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from ...utils.formatters import format_duration
from ...utils.status_palette import STATUS_PALETTE

_log = logging.getLogger(__name__)

# ── Inline (CID) logo embedding ──────────────────────────────────────────────
# The real message embeds the logo as a related MIME part referenced by this
# Content-ID. {{logo_url}} resolves to LOGO_SRC at render time, so the sent mail
# never depends on GitHub, the instance URL or a client's permission to load
# remote images. build_email_message() attaches the asset when the rendered HTML
# references it.
LOGO_CID = "justup-logo"
LOGO_SRC = f"cid:{LOGO_CID}"

# Inline logo asset: white wordmark baked onto a dark brand chip so it stays legible
# both on the light card and after a mail client recolours the background for dark mode.
LOGO_ASSET_PATH = Path(__file__).resolve().parent.parent.parent / "static" / "img" / "justup_email_logo.png"

# Browser-editor preview of {{logo_url}}: a local static file the preview iframe can load.
LOGO_URL_PREVIEW = "/static/img/justup_email_logo.png"

# Backwards-compatible export: the send path no longer depends on this external URL,
# but the name stays importable. It now points at the inline reference.
LOGO_URL = LOGO_SRC


def template_to_preview(text: str) -> str:
    """Rewrite logo references to the local preview URL for the browser editor.

    Replaces both the ``{{logo_url}}`` placeholder and an already-rendered ``cid:``
    reference, so the editor preview shows the logo without a mail client.
    """
    return text.replace("{{logo_url}}", LOGO_URL_PREVIEW).replace(LOGO_SRC, LOGO_URL_PREVIEW)


# Setting keys of the stored SMTP mail template
EMAIL_TEMPLATE_SUBJECT_KEY = "smtp_mail_template_subject"
EMAIL_TEMPLATE_HTML_KEY = "smtp_mail_template_html"


def load_email_templates(conn) -> dict[str, str]:
    """Return the stored ``{"subject", "html"}`` SMTP mail template, falling back to the defaults."""
    from ...db import sqlite as sqlite_db

    return {
        "subject": sqlite_db.get_setting(conn, EMAIL_TEMPLATE_SUBJECT_KEY, None) or DEFAULT_SUBJECT,
        "html": sqlite_db.get_setting(conn, EMAIL_TEMPLATE_HTML_KEY, None) or DEFAULT_HTML_TEMPLATE,
    }


def load_template_preview(conn) -> dict[str, str]:
    """Stored (or default) SMTP mail template as editor-ready ``{"subject", "html"}`` preview text."""
    return {key: template_to_preview(text) for key, text in load_email_templates(conn).items()}


# Public API exports
__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "DEFAULT_HTML_TEMPLATE",
    "DEFAULT_SUBJECT",
    "EMAIL_TEMPLATE_HTML_KEY",
    "EMAIL_TEMPLATE_SUBJECT_KEY",
    "LOGO_ASSET_PATH",
    "LOGO_CID",
    "LOGO_SRC",
    "LOGO_URL",
    "LOGO_URL_PREVIEW",
    "STATUS_DISPLAY_MAP",
    "build_alert_context",
    "build_email_message",
    "build_test_alert_sample",
    "html_to_text",
    "load_email_templates",
    "load_template_preview",
    "render_template",
    "template_to_preview",
]


# ============================================================================
# Whitelist of allowed placeholders (explicit, no dynamic access)
# ============================================================================

ALLOWED_PLACEHOLDERS = frozenset(
    {
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
        # Status display (colour, text, short label for templates)
        "status.emoji",
        "status.color",
        "status.accent_color",
        "status.foreground_color",
        "status.text_color",
        "status.text",
        "status.label",  # short status word for a redundant text pill (DOWN / RESOLVED / ...)
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
    }
)

# Pre-compiled regex: only allows alphanumeric, underscores, and dots
# Rejects pipes, spaces, special chars, double-dots, etc.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# Placeholders that contain pre-rendered HTML (not escaped)
_RAW_HTML_PLACEHOLDERS = frozenset(
    {
        "target.check_badges",
    }
)

# ── Conditional sections ─────────────────────────────────────────────────────
# Optional parts of the template (SLA panel, call-to-action, resolved/ongoing row,
# group row) are wrapped in named blocks of the form "open-if NAME ... close-if NAME",
# written with the same double-brace markers as placeholders (see _CONDITIONAL_RE).
# This is NOT a general template engine: the flag name is matched against a fixed
# whitelist, there are no expressions, no negation and no nesting of the same flag.
# A block whose flag is truthy in the context's "flags" dict is kept (its inner
# text is still placeholder-rendered); otherwise the whole block is removed. An
# unknown flag removes the block (safe default) and is logged.
CONDITIONAL_FLAGS = frozenset(
    {
        "sla",  # SLA panel (threshold/actual available and relevant)
        "cta",  # "Open in justUp!" button (valid instance URL)
        "resolved",  # alert has an end time (RESOLVED); else the "Ongoing" row shows
        "group",  # target has a non-default group
    }
)

_CONDITIONAL_RE = re.compile(
    r"\{\{#if\s+([a-zA-Z0-9_]+)\s*\}\}(.*?)\{\{/if\s+\1\s*\}\}",
    re.DOTALL,
)


def _apply_conditionals(template: str, flags: dict[str, bool]) -> str:
    """Keep or drop each ``{{#if name}}...{{/if name}}`` block by its whitelisted flag.

    Runs before placeholder replacement. Unknown flags drop their block.
    """
    if "{{#if" not in template:
        return template

    def repl(match: re.Match) -> str:
        name = match.group(1)
        body = match.group(2)
        if name not in CONDITIONAL_FLAGS:
            _log.debug("Unknown conditional flag %r; dropping block", name)
            return ""
        return body if flags.get(name) else ""

    # One pass is enough: no same-flag nesting is supported by design.
    return _CONDITIONAL_RE.sub(repl, template)


# Default email subject template (emoji for clear status visibility)
DEFAULT_SUBJECT = "{{status.emoji}} {{target.name}} – {{status.text}}"

# Default HTML email template (table-based for max compatibility)
# Mobile-first responsive design - works in Gmail, Outlook, Apple Mail
# Uses display:block on mobile for stacked layout
DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml"
      xmlns:v="urn:schemas-microsoft-com:vml"
      xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <!-- Native design is LIGHT. Declaring light tells well-behaved clients not to
       auto-invert; colours below are still chosen to survive clients that do. -->
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>justUp! Monitoring alert</title>

  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings>
    <o:PixelsPerInch>96</o:PixelsPerInch>
  </o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->

  <style>
    :root { color-scheme: light; supported-color-schemes: light; }
    body, table, td, a { -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
    table, td { mso-table-lspace:0pt; mso-table-rspace:0pt; }
    img {
      -ms-interpolation-mode:bicubic; border:0; line-height:100%;
      outline:none; text-decoration:none;
    }
    body { margin:0; padding:0; width:100% !important; -webkit-font-smoothing:antialiased; }
    a { color:#0e7a65; }

    /* Keep the dark logo chip and status colours stable if a client recolours for
       dark mode: the surfaces that must stay as designed are pinned. */
    @media (prefers-color-scheme: dark) {
      .ju-card { background-color:#ffffff !important; }
      .ju-panel { background-color:#f7f9fb !important; }
      .ju-title, .ju-value { color:#1b2733 !important; }
      .ju-muted, .ju-label { color:#5b6b7b !important; }
    }
    [data-ogsc] .ju-card { background-color:#ffffff !important; }
    [data-ogsc] .ju-panel { background-color:#f7f9fb !important; }
    [data-ogsc] .ju-title, [data-ogsc] .ju-value { color:#1b2733 !important; }
    [data-ogsc] .ju-muted, [data-ogsc] .ju-label { color:#5b6b7b !important; }

    @media only screen and (max-width:600px) {
      .ju-wrap-td { padding:0 !important; }
      .ju-card { border-radius:0 !important; }
      .ju-pad { padding-left:18px !important; padding-right:18px !important; }
      .ju-stack, .ju-stack tr, .ju-stack td { display:block !important; width:100% !important; }
      .ju-stack .ju-col { padding:10px 0 !important; border:0 !important; }
      .ju-sep-v { display:none !important; }
      .ju-cta a { display:block !important; }
    }
  </style>
</head>

<body class="ju-bg" style="margin:0;padding:0;word-spacing:normal;background-color:#eef1f5;">

<!-- preheader: shown in the inbox list, hidden in the body -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;
            font-size:1px;line-height:1px;color:#eef1f5;">
  {{status.text}} &ndash; {{target.name}} ({{target.host}}) &middot; {{alert.reason}}
</div>

<table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-bg"
       style="background-color:#eef1f5;">
  <tr>
    <td align="center" style="padding:24px 12px;">

      <!--[if mso]>
      <table role="presentation" align="center" width="600" cellspacing="0" cellpadding="0" border="0">
      <tr><td>
      <![endif]-->
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" class="ju-card"
             style="max-width:600px;width:100%;background-color:#ffffff;
                    border:1px solid #e2e7ee;border-radius:14px;overflow:hidden;
                    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                    color:#1b2733;">

        <!-- HEADER: logo + Monitoring alert -->
        <tr>
          <td class="ju-pad" style="padding:22px 28px 16px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
              <tr>
                <td valign="middle" style="padding:0;">
                  <img src="{{logo_url}}" width="150" alt="justUp!"
                       style="display:inline-block;height:auto;width:150px;
                              max-width:60vw;vertical-align:middle;">
                </td>
                <td valign="middle" align="right" class="ju-muted"
                    style="padding:0;font-size:13px;color:#5b6b7b;white-space:nowrap;">
                  <span style="display:inline-block;border-left:1px solid #e2e7ee;padding-left:14px;">
                    &#128276; Monitoring alert
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- STATUS ACCENT LINE -->
        <tr><td style="padding:0 28px;">
          <div style="height:4px;background-color:{{status.accent_color}};border-radius:4px;
                      font-size:1px;line-height:1px;">&nbsp;</div>
        </td></tr>

        <!-- PRIMARY EVENT -->
        <tr>
          <td class="ju-pad" style="padding:20px 28px 6px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
              <tr>
                <td valign="middle" style="padding:0;">
                  <span style="display:inline-block;width:12px;height:12px;border-radius:50%;
                               background-color:{{status.accent_color}};vertical-align:middle;">&nbsp;</span>
                  <span class="ju-title"
                        style="vertical-align:middle;padding-left:10px;font-size:24px;
                               font-weight:700;color:#1b2733;letter-spacing:-0.01em;">
                    {{status.text}}
                  </span>
                </td>
                <td valign="middle" align="right" style="padding:0;">
                  <span style="display:inline-block;font-size:12px;font-weight:700;
                               letter-spacing:0.04em;color:{{status.text_color}};
                               border:1px solid {{status.text_color}};border-radius:999px;
                               padding:4px 12px;white-space:nowrap;">{{status.label}}</span>
                </td>
              </tr>
            </table>
            <div class="ju-muted"
                 style="padding-top:8px;font-size:14px;color:#5b6b7b;
                        line-height:1.5;overflow-wrap:break-word;">
              {{alert.reason}}
            </div>
          </td>
        </tr>

        <!-- TARGET / HOST -->
        <tr>
          <td class="ju-pad" style="padding:14px 28px 4px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                   class="ju-stack" style="table-layout:fixed;">
              <tr>
                <td valign="top" width="50%" class="ju-col"
                    style="padding:4px 16px 4px 0;vertical-align:top;">
                  <div class="ju-label"
                       style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                              color:#8a98a8;text-transform:uppercase;">Target</div>
                  <div class="ju-value"
                       style="padding-top:3px;font-size:16px;font-weight:600;color:#1b2733;
                              overflow-wrap:break-word;word-break:break-word;">{{target.name}}</div>
                </td>
                <td class="ju-sep-v" width="1"
                    style="width:1px;background-color:#e2e7ee;font-size:1px;line-height:1px;">&nbsp;</td>
                <td valign="top" width="50%" class="ju-col"
                    style="padding:4px 0 4px 16px;vertical-align:top;">
                  <div class="ju-label"
                       style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                              color:#8a98a8;text-transform:uppercase;">Host</div>
                  <div class="ju-value"
                       style="padding-top:3px;font-size:16px;font-weight:600;color:#1b2733;
                              overflow-wrap:break-word;word-break:break-word;">{{target.host}}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- INCIDENT DETAILS -->
        <tr>
          <td class="ju-pad" style="padding:14px 28px 4px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-panel"
                   style="background-color:#f7f9fb;border:1px solid #e9eef4;border-radius:10px;">
              <tr><td style="padding:14px 16px 6px 16px;">
                <span style="color:{{status.text_color}};font-weight:700;">&#9650;</span>
                <span class="ju-title"
                      style="padding-left:6px;font-size:15px;font-weight:700;color:#1b2733;">
                  Incident Details
                </span>
              </td></tr>
              <tr><td style="padding:0 16px 10px 16px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                       style="font-size:14px;">
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Status</td>
                    <td align="right" style="padding:7px 0;border-top:1px solid #e9eef4;">
                      <span style="display:inline-block;font-size:12px;font-weight:700;
                                   color:{{status.foreground_color}};background-color:{{status.accent_color}};
                                   border-radius:999px;padding:3px 11px;">{{alert.status}}</span>
                    </td>
                  </tr>
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Started</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;overflow-wrap:break-word;">{{alert.started_at}}</td>
                  </tr>
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Resolved</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;overflow-wrap:break-word;">{{alert.resolved_at}}</td>
                  </tr>
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Duration</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;">{{alert.duration}}</td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td>
        </tr>

        <!-- TARGET DETAILS -->
        <tr>
          <td class="ju-pad" style="padding:8px 28px 4px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-panel"
                   style="background-color:#f7f9fb;border:1px solid #e9eef4;border-radius:10px;">
              <tr><td style="padding:14px 16px 6px 16px;">
                <span style="color:#5b6b7b;font-weight:700;">&#9678;</span>
                <span class="ju-title"
                      style="padding-left:6px;font-size:15px;font-weight:700;color:#1b2733;">
                  Target Details
                </span>
              </td></tr>
              <tr><td style="padding:0 16px 10px 16px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                       style="font-size:14px;">
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Check</td>
                    <td align="right"
                        style="padding:7px 0;border-top:1px solid #e9eef4;">{{target.check_badges}}</td>
                  </tr>
                  {{#if group}}
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Group</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;overflow-wrap:break-word;">{{target.group}}</td>
                  </tr>
                  {{/if group}}
                </table>
              </td></tr>
            </table>
          </td>
        </tr>

        {{#if sla}}
        <!-- SLA -->
        <tr>
          <td class="ju-pad" style="padding:8px 28px 4px 28px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-panel"
                   style="background-color:#f7f9fb;border:1px solid #e9eef4;border-radius:10px;">
              <tr><td style="padding:14px 16px 6px 16px;">
                <span style="color:#5b6b7b;font-weight:700;">&#9707;</span>
                <span class="ju-title"
                      style="padding-left:6px;font-size:15px;font-weight:700;color:#1b2733;">SLA</span>
              </td></tr>
              <tr><td style="padding:0 16px 10px 16px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                       style="font-size:14px;">
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Downtime</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;">{{downtime.human}}</td>
                  </tr>
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Threshold</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;">{{sla.threshold}}</td>
                  </tr>
                  <tr>
                    <td class="ju-label"
                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">Actual</td>
                    <td class="ju-value" align="right"
                        style="padding:7px 0;font-weight:600;color:#1b2733;
                               border-top:1px solid #e9eef4;">{{sla.actual}}</td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td>
        </tr>
        {{/if sla}}

        {{#if cta}}
        <!-- CALL TO ACTION -->
        <tr>
          <td class="ju-pad ju-cta" align="center" style="padding:20px 28px 8px 28px;">
            <!--[if mso]>
            <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml"
              xmlns:w="urn:schemas-microsoft-com:office:word" href="{{system.url}}"
              style="height:46px;v-text-anchor:middle;width:280px;" arcsize="26%"
              fillcolor="#16815c" stroke="f">
              <center style="color:#ffffff;font-family:sans-serif;font-size:16px;font-weight:bold;">
                Open in justUp!
              </center>
            </v:roundrect>
            <![endif]-->
            <!--[if !mso]><!-- -->
            <a href="{{system.url}}"
               style="display:inline-block;background-color:#16815c;color:#ffffff;
                      text-decoration:none;font-size:16px;font-weight:700;line-height:46px;
                      border-radius:10px;padding:0 28px;">
              Open in justUp!
            </a>
            <!--<![endif]-->
          </td>
        </tr>
        {{/if cta}}

        <!-- FOOTER -->
        <tr>
          <td class="ju-pad" style="padding:18px 28px 22px 28px;">
            <div style="border-top:1px solid #e9eef4;padding-top:16px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-stack">
                <tr>
                  <td valign="middle" class="ju-muted ju-col"
                      style="font-size:12px;color:#8a98a8;line-height:1.6;">
                    Monitoring and uptime for your services. &middot;
                    <a href="{{system.github_url}}"
                       style="color:#5b6b7b;text-decoration:underline;">GitHub</a>
                  </td>
                  <td valign="middle" align="right" class="ju-col" style="width:120px;">
                    <img src="{{logo_url}}" width="96" alt="justUp!"
                         style="display:inline-block;height:auto;width:96px;vertical-align:middle;">
                  </td>
                </tr>
              </table>
            </div>
          </td>
        </tr>

      </table>
      <!--[if mso]></td></tr></table><![endif]-->

    </td>
  </tr>
</table>

</body>
</html>
"""


def render_template(template: str, context: dict[str, Any], *, mode: str = "html") -> str:
    """
    Render email template by replacing {{placeholder}} with context values.

    Security:
    - Only placeholders in ALLOWED_PLACEHOLDERS are replaced
    - Unknown placeholders are replaced with empty string
    - No code execution, no attribute access

    Args:
            template: Email template with {{placeholder}} syntax
            context: Dictionary with placeholder values
            mode: "html" escapes values for HTML; "plain_text" (Signal) inserts them verbatim

    Returns:
            Rendered template with placeholders replaced
    """
    if mode not in ("html", "plain_text"):
        raise ValueError(f"Unknown render mode: {mode!r}")
    plain = mode == "plain_text"

    # Keep or drop named optional sections before any placeholder replacement, so a
    # dropped block never leaves an unreplaced {{placeholder}} behind.
    flags = context.get("flags") if isinstance(context, dict) else None
    template = _apply_conditionals(template, flags if isinstance(flags, dict) else {})

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

        # Never log the value itself: it can carry internal hosts or URL secrets.
        _log.debug("Placeholder %r → value length: %d", placeholder, len(raw))

        # Pre-rendered HTML: pass through without escaping
        if placeholder in _RAW_HTML_PLACEHOLDERS:
            if plain:
                return html.unescape(re.sub(r"<[^>]+>", "", raw))
            _log.debug("Placeholder %r is RAW HTML, not escaping", placeholder)
            return raw

        # CSS injection protection for color values
        if placeholder in {"status.color", "status.accent_color", "status.foreground_color", "status.text_color"}:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
                return {
                    "status.color": "#6b7280",
                    "status.accent_color": "#6c757d",
                    "status.foreground_color": "#ffffff",
                    "status.text_color": "#5b6b7b",
                }[placeholder]
            return raw

        if plain:
            return raw
        # XSS protection: HTML-escape all other values
        _log.debug("Placeholder %r is being escaped", placeholder)
        return html.escape(raw)

    return _PLACEHOLDER_RE.sub(replace_placeholder, template)


def _to_utc(dt: datetime) -> datetime:
    """Convert aware datetimes to UTC; naive values are stored as UTC by convention."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt
    return dt.astimezone(UTC)


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
        return _to_utc(ts).strftime("%Y-%m-%d %H:%M:%S UTC")

    if isinstance(ts, str):
        try:
            return _to_utc(datetime.fromisoformat(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
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
# Used for visual distinction in email subject and HTML header. Colours match the app
# status palette; status is never conveyed by colour alone (text + short label carry it).
STATUS_DISPLAY_MAP: dict[str, tuple[str, str, str]] = {
    "DOWN": ("🔴", STATUS_PALETTE["danger"].text, "Service DOWN"),
    "OPEN": ("🔴", STATUS_PALETTE["danger"].text, "Service DOWN"),
    "UP": ("🟢", STATUS_PALETTE["success"].text, "Service UP"),
    "RESOLVED": ("🟢", STATUS_PALETTE["success"].text, "Service RESOLVED"),
    "WARN": ("🟡", STATUS_PALETTE["warning"].text, "Degraded"),
    "WARNING": ("🟡", STATUS_PALETTE["warning"].text, "Degraded"),
    "UNKNOWN": ("⚪", STATUS_PALETTE["neutral"].text, "Unknown"),
}

# Default fallback for unknown status values
_DEFAULT_STATUS_DISPLAY = ("⚪", STATUS_PALETTE["neutral"].text, "Unknown")

_STATUS_VARIANTS = {
    "DOWN": "danger",
    "OPEN": "danger",
    "UP": "success",
    "RESOLVED": "success",
    "WARN": "warning",
    "WARNING": "warning",
    "UNKNOWN": "neutral",
}

# Short, upper-case status word for the redundant text pill (never colour alone).
STATUS_LABEL_MAP: dict[str, str] = {
    "DOWN": "DOWN",
    "OPEN": "DOWN",
    "UP": "UP",
    "RESOLVED": "RESOLVED",
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "UNKNOWN": "UNKNOWN",
}

# Check types are metadata, never status: every type badge uses the neutral palette entry,
# in every mail state. This is the single place that decides the colour of the detail rows.
_NEUTRAL = STATUS_PALETTE["neutral"]
CHECK_BADGE_COLORS: tuple[str, str] = (_NEUTRAL.background, _NEUTRAL.foreground)


def _render_check_badges(check_type: str) -> str:
    """
    Render check types as inline HTML badges for email.

    Input:  "HTTP + PING"  or  "HTTP,PING"  or  "HTTPS"
    Output: Table-based badge HTML safe for all email clients.
    """
    if check_type is None:
        check_type = ""

    raw = str(check_type).strip()

    # Handle empty, "None" string (from str(None)), or "N/A"
    if not raw or raw.upper() in ("NONE", "N/A"):
        return f"<span style='color:{_NEUTRAL.text};'>N/A</span>"

    # Split on common separators: +  ,  ;  whitespace
    parts = re.split(r"[+,;\s]+", raw)
    parts = [p.strip().upper() for p in parts if p.strip()]

    if not parts:
        return f"<span style='color:{_NEUTRAL.text};'>N/A</span>"

    badges: list[str] = []
    for check in parts:
        bg, fg = CHECK_BADGE_COLORS
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

    # TCP failures are rendered as "TCP host:ports", "TCP host", or "TCP :ports".
    if reason.upper().startswith("TCP "):
        rest = reason[4:].strip()
        rest_lower = rest.lower().rstrip(".")

        if host_norm and (rest_lower == host_norm or rest_lower.startswith((host_norm + ":", host_norm + "."))):
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


def resolve_sla_values(
    sla_enabled: bool, sla_availability_pct: float | None, uptime_percent: float | None
) -> tuple[float | None, float | None]:
    """Return (threshold, actual) in percent (0-100) for the alert context.

    The threshold is the per-target availability SLA; a global setting no longer exists.
    """
    if not sla_enabled:
        return None, None
    threshold = float(sla_availability_pct) if sla_availability_pct is not None else None
    actual = float(uptime_percent) if uptime_percent is not None else None
    return threshold, actual


def build_alert_context(alert_data: dict[str, Any], target_data: dict[str, Any]) -> dict[str, Any]:
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
    started_at_raw = alert_data.get("started_at")
    resolved_at_raw = alert_data.get("resolved_at")

    duration = ""
    if started_at_raw and resolved_at_raw:
        # Alert resolved: calculate full duration
        try:
            started = datetime.fromisoformat(str(started_at_raw))
            resolved = datetime.fromisoformat(str(resolved_at_raw))
            duration_seconds = int((resolved - started).total_seconds())
            duration = format_duration(duration_seconds)
        except (ValueError, AttributeError, TypeError):
            duration = "N/A"
    elif started_at_raw:
        # Alert still active: calculate duration from start until now
        try:
            started = datetime.fromisoformat(str(started_at_raw))
            now = datetime.now(UTC)
            duration_seconds = int((now - started).total_seconds())
            duration = f"{format_duration(duration_seconds)} (ongoing)"
        except (ValueError, AttributeError, TypeError):
            duration = "Ongoing"
    else:
        duration = "N/A"

    started_at = _format_timestamp(started_at_raw)
    resolved_at = _format_timestamp(resolved_at_raw, fallback="Ongoing")

    downtime_minutes = alert_data.get("downtime_minutes", 0) or 0
    downtime_human = format_duration(downtime_minutes * 60) if downtime_minutes > 0 else "None"

    sla_threshold = _format_sla(alert_data.get("sla_threshold"))
    sla_actual = _format_sla(alert_data.get("sla_actual"))

    # If SLA is disabled (both N/A), hide downtime as well
    if sla_threshold == "N/A" and sla_actual == "N/A":
        downtime_minutes = 0
        downtime_human = "N/A"

    # Get status display values (emoji, color, text, short label)
    alert_status = str(alert_data.get("status", "UNKNOWN")).upper()
    status_emoji, status_color, status_text = STATUS_DISPLAY_MAP.get(alert_status, _DEFAULT_STATUS_DISPLAY)
    status_label = STATUS_LABEL_MAP.get(alert_status, "UNKNOWN")
    status_palette = STATUS_PALETTE[_STATUS_VARIANTS.get(alert_status, "neutral")]

    # Sanitize reason and render check badges
    reason_raw = alert_data.get("reason", "N/A")
    target_host = str(target_data.get("host", ""))
    check_type_raw = target_data.get("check_type") or ""  # None/NULL → ""
    reason = _sanitize_reason(reason_raw, target_host=target_host, check_type=check_type_raw)
    check_badges = _render_check_badges(check_type_raw)

    group_name = target_data.get("group", "Default")
    system_url = alert_data.get("system_url", "#")

    # Flags for the template's optional sections. No user input decides HTML structure here;
    # each flag is a plain presence/relevance check and is whitelisted in CONDITIONAL_FLAGS.
    sla_visible = not (sla_threshold == "N/A" and sla_actual == "N/A")
    flags = {
        "sla": sla_visible,
        "cta": _is_valid_cta_url(system_url),
        "resolved": bool(resolved_at_raw),
        "group": bool(group_name) and str(group_name).strip().lower() not in ("", "default", "n/a"),
    }

    return {
        "status": {
            "emoji": status_emoji,
            "color": status_color,
            "accent_color": status_palette.background,
            "foreground_color": status_palette.foreground,
            "text_color": status_palette.text,
            "text": status_text,
            "label": status_label,
        },
        "target": {
            "name": target_data.get("name", "Unknown"),
            "host": target_data.get("host", "N/A"),
            "group": group_name,
            "check_type": check_type_raw,  # Plain text (for subject)
            "check_badges": check_badges,  # Pre-rendered HTML
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
            "url": system_url,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "github_url": "https://github.com/Gill-Bates/justup",
        },
        "logo_url": LOGO_URL,
        "flags": flags,
    }


# ── Call-to-action URL validation ────────────────────────────────────────────
# Only a real http(s) instance URL earns the "Open in justUp!" button. A missing,
# placeholder ("#") or non-http URL drops the button rather than linking nowhere.
_CTA_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _is_valid_cta_url(url: str | None) -> bool:
    """True only for a plausible absolute http(s) URL (not ``#``, empty or a scheme-only value)."""
    if not url:
        return False
    return bool(_CTA_URL_RE.match(str(url).strip()))


_HTTP_LINK_RE = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*"(https?://[^"\s]+)"[^>]*>(.*?)</a>""")


def _link_with_url(match: re.Match) -> str:
    url, label = match.group(1), match.group(2)
    return f"{label.strip()} ({url})"


def html_to_text(html_body: str) -> str:
    """Derive a readable plain-text part from rendered HTML.

    Not a full HTML renderer: it drops head/script/style, turns block boundaries into
    newlines, unescapes entities and collapses whitespace. Good enough for the
    text/plain alternative so a client without HTML still shows a sensible message.
    """
    text = html_body or ""
    # Drop non-content elements entirely (including the hidden preheader's siblings are kept).
    text = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", " ", text)
    # Outlook-only fallback (VML button) would repeat the button label in the text part.
    text = re.sub(r"(?is)<!--\[if mso\]>.*?<!\[endif\]-->", " ", text)
    # An http(s) link keeps its URL as visible text; the href alone would vanish with the tags.
    text = _HTTP_LINK_RE.sub(_link_with_url, text)
    # Block-level boundaries become line breaks.
    text = re.sub(r"(?i)<(br|/p|/div|/tr|/table|/h[1-6]|/li)\s*>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "- ", text)
    # Strip every remaining tag, then unescape entities.
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    # Collapse runs of spaces/tabs, trim each line, drop empty-line runs.
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def build_email_message(
    *,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    from_addr: str,
    to_addr: str,
    message_id: str | None = None,
    logo_path: Path | None = None,
) -> EmailMessage:
    """Compose the alert email as a single MIME message, shared by every send path.

    Structure (RFC 2387 / 2046):
    - ``multipart/alternative`` carrying a text/plain part and a text/html part, so a
      client without HTML still shows the plain-text version.
    - When the HTML references the inline logo (``cid:LOGO_CID``) and the asset exists,
      the whole alternative is wrapped in ``multipart/related`` and the PNG is attached
      as a related part with ``Content-ID: <LOGO_CID>`` and ``Content-Disposition: inline``.

    The text part is derived from the HTML when no explicit ``text_body`` is given, so the
    message never ships an empty text/plain part.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if message_id:
        msg["Message-ID"] = message_id

    text = text_body if (text_body and text_body.strip()) else html_to_text(html_body)
    msg.set_content(text)
    msg.add_alternative(html_body or "", subtype="html")

    # Attach the inline logo only when the HTML actually references it and the asset is present.
    asset = logo_path or LOGO_ASSET_PATH
    if html_body and LOGO_SRC in html_body:
        try:
            data = asset.read_bytes()
        except OSError as exc:
            # A missing asset must not fail the send: the HTML keeps its alt text and the
            # client shows the broken-image placeholder, which is still a deliverable mail.
            _log.warning("Inline logo asset unavailable (%s); sending without embedded logo", exc)
        else:
            # add_related() on the HTML part promotes it to multipart/related and attaches the
            # image with the given Content-ID. disposition="inline" marks it as an embedded image
            # (referenced by cid:) rather than a download attachment.
            html_part = msg.get_payload()[1]
            html_part.add_related(
                data,
                maintype="image",
                subtype="png",
                cid=f"<{LOGO_CID}>",
                disposition="inline",
                filename="justup-logo.png",
            )

    return msg


# ── Test-mail sample data ────────────────────────────────────────────────────
# Realistic, self-consistent example for the "Send Test Email" button and the
# template preview: a test outage that opened ~15 minutes ago and is still open,
# clearly marked as a test notification. The outage age is derived from ``now`` so
# the duration always reads "15m (ongoing)" and never a stale hard-coded date; the
# ``now`` parameter keeps automated tests deterministic. Production alert data is
# built from live targets and is not affected by this helper.
TEST_SAMPLE_DOWNTIME_MINUTES = 15


def build_test_alert_sample(now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(alert_data, target_data)`` for a deterministic test notification.

    The outage started ``TEST_SAMPLE_DOWNTIME_MINUTES`` before ``now`` (default: current
    UTC time) and is still open, so Started/Duration/Downtime stay consistent.
    """
    reference = now or datetime.now(UTC)
    started_at = reference - timedelta(minutes=TEST_SAMPLE_DOWNTIME_MINUTES)

    alert_data = {
        "status": "OPEN",
        "started_at": started_at.isoformat(),
        "resolved_at": None,
        "reason": "HTTP 503 Service Unavailable (justUp! test notification)",
        "downtime_minutes": TEST_SAMPLE_DOWNTIME_MINUTES,
        "sla_threshold": 99.9,
        "sla_actual": 99.7,
        # Example instance URL so the sample shows the complete template incl. the CTA button
        # (matches the mock). Real alerts only get a button when a real instance URL is stored.
        "system_url": "https://justup.example.com",
    }
    target_data = {
        "name": "Test Target",
        "host": "example.com",
        "group": "Production",
        "check_type": "HTTP",
    }
    return alert_data, target_data
