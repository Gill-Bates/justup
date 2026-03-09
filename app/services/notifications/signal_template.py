#!/usr/bin/env python3
#
# app/services/notifications/signal_template.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Signal message templates (plain text).

These templates use the same safe placeholder renderer as email templates:
- {{...}} placeholders are replaced via a strict whitelist (no Jinja/eval).
- Unknown placeholders are replaced with an empty string.
"""

from __future__ import annotations


__all__ = [
	"DEFAULT_SIGNAL_TEMPLATE_DOWN",
	"DEFAULT_SIGNAL_TEMPLATE_UP",
]


DEFAULT_SIGNAL_TEMPLATE_DOWN = """{{status.emoji}} [ {{target.name}} ]  {{status.text}}
————————————————————
Service:   {{alert.reason}}
Started:   {{alert.started_at}}
"""

DEFAULT_SIGNAL_TEMPLATE_UP = """{{status.emoji}} [ {{target.name}} ]  {{status.text}}
————————————————————
Host:         {{target.host}}
Service:     {{alert.reason}}
Started:     {{alert.started_at}}
Resolved:  {{alert.resolved_at}}
Duration:   {{alert.duration}}
"""

