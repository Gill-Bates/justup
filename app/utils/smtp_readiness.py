#!/usr/bin/env python3
#
# app/utils/smtp_readiness.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single definition of when stored SMTP settings are complete enough to send mail."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from ..db import sqlite as sqlite_db
from .constants import SettingKeys


class SmtpReadiness(StrEnum):
    """Why SMTP can or cannot be used; callers map the state to their own HTTP error."""

    READY = "ready"
    DISABLED = "disabled"
    INCOMPLETE = "incomplete"  # enabled, but host or sender address is missing


def smtp_readiness(conn: sqlite3.Connection) -> SmtpReadiness:
    """Return whether SMTP is enabled and has both a host and a sender address."""
    if not sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False):
        return SmtpReadiness.DISABLED
    if not sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST, None) or not sqlite_db.get_setting(
        conn, SettingKeys.SMTP_FROM, None
    ):
        return SmtpReadiness.INCOMPLETE
    return SmtpReadiness.READY
