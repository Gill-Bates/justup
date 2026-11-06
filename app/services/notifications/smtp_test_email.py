#!/usr/bin/env python3
#
# app/services/notifications/smtp_test_email.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Welcome / setup message sent by Settings > SMTP > "Send Test Email".

It is not an alert: it never reads the saved alert template and carries no incident data.
The layout reuses the shared blocks of the alert mail (head, logo header, accent line, CTA,
footer) so both stay one visual design; only the body sections are specific to this message.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any, NamedTuple

from ...utils.status_palette import STATUS_PALETTE
from .email_template import (
    LOGO_URL,
    MAIL_BLOCK_CTA,
    MAIL_BLOCK_FOOTER,
    MAIL_BLOCK_HEAD,
    MAIL_BLOCK_SHELL_OPEN,
    _format_timestamp,
    _is_valid_cta_url,
    render_template,
)

__all__ = ["SMTP_TEST_SUBJECT", "SmtpTestEmail", "build_smtp_test_email"]

SMTP_TEST_SUBJECT = "[justUp] Welcome \u2013 Email test"
_TITLE = "Welcome to justUp!"
_PILL = "TEST"
_DESCRIPTION = "This is a test email sent while configuring notifications in justUp!. No service incident has occurred."
_RECEIPT_NOTE = "If this message reached your mailbox, the SMTP settings work for this recipient."
_NEXT_STEPS_INTRO = "When you are ready, an administrator can:"
_NEXT_STEPS = (
    ("Add monitoring targets", "the hosts, URLs and ports to watch."),
    ("Select checks", "choose which checks run for each target."),
    ("Assign alert recipients", "decide who is notified when a target changes state."),
)
_PREHEADER = "Test email \u2013 no service incident has occurred."

# Informational accent from the shared status palette: this is a notice, not a state change.
_INFO = STATUS_PALETTE["info"]

_PANEL_OPEN = (
    "        <tr>\n"
    '          <td class="ju-pad" style="padding:{pad};">\n'
    '            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="ju-panel"\n'
    '                   style="background-color:#f7f9fb;border:1px solid #e9eef4;border-radius:10px;">\n'
    '              <tr><td style="padding:14px 16px 6px 16px;">\n'
    '                <span style="color:{icon_color};font-weight:700;">{icon}</span>\n'
    '                <span class="ju-title"\n'
    '                      style="padding-left:6px;font-size:15px;font-weight:700;color:#1b2733;">{heading}</span>\n'
    "              </td></tr>\n"
    '              <tr><td style="padding:0 16px 10px 16px;">\n'
    '                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"\n'
    '                       style="font-size:14px;">\n'
)
_PANEL_CLOSE = (
    "                </table>\n              </td></tr>\n            </table>\n          </td>\n        </tr>\n\n"
)


class SmtpTestEmail(NamedTuple):
    """Rendered message parts, ready for the spooler payload (subject, text_body, html_body)."""

    subject: str
    html_body: str
    text_body: str


def _panel(heading: str, icon: str, icon_color: str, rows: str, *, pad: str) -> str:
    return _PANEL_OPEN.format(pad=pad, icon=icon, icon_color=icon_color, heading=heading) + rows + _PANEL_CLOSE


def _info_row(label: str, value: str) -> str:
    """One label/value row of an information panel; ``value`` must already be escaped."""
    return (
        "                  <tr>\n"
        '                    <td class="ju-label"\n'
        f'                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">{label}</td>\n'
        '                    <td class="ju-value" align="right"\n'
        '                        style="padding:7px 0;font-weight:600;color:#1b2733;\n'
        '                               border-top:1px solid #e9eef4;overflow-wrap:break-word;word-break:break-word;">'
        f"{value}</td>\n"
        "                  </tr>\n"
    )


def _step_row(number: int, title: str, detail: str) -> str:
    return (
        "                  <tr>\n"
        '                    <td class="ju-label" valign="top" width="24"\n'
        f'                        style="padding:7px 0;color:#5b6b7b;border-top:1px solid #e9eef4;">{number}.</td>\n'
        '                    <td class="ju-value" valign="top"\n'
        '                        style="padding:7px 0;color:#1b2733;border-top:1px solid #e9eef4;\n'
        '                               overflow-wrap:break-word;">'
        f'<span style="font-weight:600;">{html.escape(title)}</span> \u2013 {html.escape(detail)}</td>\n'
        "                  </tr>\n"
    )


def _main_body(recipient: str, sent: str) -> str:
    """Title row, description and the two information panels; all dynamic values are escaped."""
    pill = (
        '<span style="display:inline-block;font-size:12px;font-weight:700;\n'
        f"                               letter-spacing:0.04em;color:{_INFO.text};\n"
        f"                               border:1px solid {_INFO.text};border-radius:999px;\n"
        f'                               padding:4px 12px;white-space:nowrap;">{_PILL}</span>'
    )
    head = (
        "        <!-- WELCOME -->\n"
        "        <tr>\n"
        '          <td class="ju-pad" style="padding:20px 28px 6px 28px;">\n'
        '            <table role="presentation" width="100%" cellspacing="0" cellpadding="0">\n'
        "              <tr>\n"
        '                <td valign="middle" style="padding:0;">\n'
        '                  <span style="display:inline-block;width:12px;height:12px;border-radius:50%;\n'
        f'                               background-color:{_INFO.background};vertical-align:middle;">&nbsp;</span>\n'
        '                  <span class="ju-title"\n'
        '                        style="vertical-align:middle;padding-left:10px;font-size:24px;\n'
        '                               font-weight:700;color:#1b2733;letter-spacing:-0.01em;">\n'
        f"                    {html.escape(_TITLE)}\n"
        "                  </span>\n"
        "                </td>\n"
        '                <td valign="middle" align="right" style="padding:0;">\n'
        f"                  {pill}\n"
        "                </td>\n"
        "              </tr>\n"
        "            </table>\n"
        '            <div class="ju-muted"\n'
        '                 style="padding-top:8px;font-size:14px;color:#5b6b7b;\n'
        '                        line-height:1.5;overflow-wrap:break-word;">\n'
        f"              {html.escape(_DESCRIPTION)}<br>\n"
        f"              {html.escape(_RECEIPT_NOTE)}\n"
        "            </div>\n"
        "          </td>\n"
        "        </tr>\n\n"
    )
    test_rows = "".join(
        (
            _info_row("Channel", "Email"),
            _info_row("Purpose", "SMTP setup verification"),
            _info_row("Recipient", html.escape(recipient)),
            _info_row("Sent", html.escape(sent)),
            _info_row("Action required", "None"),
        )
    )
    step_rows = "".join(_step_row(i, title, detail) for i, (title, detail) in enumerate(_NEXT_STEPS, start=1))
    next_intro = (
        '                  <tr><td colspan="2" class="ju-muted"\n'
        '                          style="padding:0 0 8px 0;font-size:14px;color:#5b6b7b;line-height:1.5;">'
        f"{html.escape(_NEXT_STEPS_INTRO)}</td></tr>\n"
    )
    return (
        head
        + _panel("Email Test", "&#9993;", _INFO.text, test_rows, pad="14px 28px 4px 28px")
        + _panel("Next steps", "&#9678;", "#5b6b7b", next_intro + step_rows, pad="8px 28px 4px 28px")
    )


def _text_body(recipient: str, sent: str, cta_url: str) -> str:
    """Plain-text alternative with the same facts as the HTML part (no layout-derived noise)."""
    # Header-like values must stay on one line.
    recipient = " ".join(recipient.split())
    lines = [
        f"{_TITLE} [{_PILL}]",
        "",
        _DESCRIPTION,
        _RECEIPT_NOTE,
        "",
        "Email Test",
        "- Channel: Email",
        "- Purpose: SMTP setup verification",
        f"- Recipient: {recipient}",
        f"- Sent: {sent}",
        "- Action required: None",
        "",
        "Next steps",
        _NEXT_STEPS_INTRO,
        *(f"{i}. {title} - {detail}" for i, (title, detail) in enumerate(_NEXT_STEPS, start=1)),
    ]
    if cta_url:
        lines += ["", f"Open in justUp!: {cta_url}"]
    lines += ["", "Monitoring and uptime for your services.", "GitHub: https://github.com/Gill-Bates/justup"]
    return "\n".join(lines)


def build_smtp_test_email(
    recipient: str,
    *,
    system_url: str | None = None,
    now: datetime | None = None,
) -> SmtpTestEmail:
    """Render the setup test mail for ``recipient``.

    ``system_url`` is the instance URL for the "Open in justUp!" button; it is dropped
    (without leaving a gap) unless it is a valid absolute http(s) URL, so a mail to a real
    recipient never links to a placeholder host. ``now`` keeps the timestamp testable.
    """
    sent = _format_timestamp(now or datetime.now(UTC))
    url = system_url or ""
    context: dict[str, Any] = {
        "status": {
            "accent_color": _INFO.background,
            "foreground_color": _INFO.foreground,
            "text_color": _INFO.text,
            "label": _PILL,
            "text": _TITLE,
        },
        "system": {"url": url, "github_url": "https://github.com/Gill-Bates/justup"},
        "logo_url": LOGO_URL,
        "flags": {"cta": _is_valid_cta_url(url)},
    }
    preheader = (
        "<!-- preheader: shown in the inbox list, hidden in the body -->\n"
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;\n'
        '            font-size:1px;line-height:1px;color:#eef1f5;">\n'
        f"  {html.escape(_PREHEADER)}\n</div>\n\n"
    )
    # Shared blocks go through the whitelist renderer; the body is built from escaped values
    # outside it, so a recipient containing braces is never treated as a placeholder.
    html_body = (
        render_template(MAIL_BLOCK_HEAD, context)
        + preheader
        + render_template(MAIL_BLOCK_SHELL_OPEN, context)
        + _main_body(recipient, sent)
        + render_template(MAIL_BLOCK_CTA, context)
        + render_template(MAIL_BLOCK_FOOTER, context)
    )
    text_body = _text_body(recipient, sent, url if context["flags"]["cta"] else "")
    return SmtpTestEmail(SMTP_TEST_SUBJECT, html_body, text_body)
