#!/usr/bin/env python3
#
# app/utils/public_url.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Operator-configured public hostname and the instance URLs derived from it.

Notifications leave the host, so their links must not depend on the request that happens to
trigger them. The admin pins a bare hostname or IP in Settings > General; HTTPS is assumed.
The value is never derived from request headers (Host-header injection).
"""

from __future__ import annotations

import ipaddress
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from ..db import sqlite as sqlite_db
from .constants import SettingKeys
from .time import assume_utc

# Context shown before and after an event; the span matches the target page's 2-year limit.
EVENT_RANGE_MARGIN = timedelta(minutes=30)
MAX_EVENT_RANGE = timedelta(days=730)
_MAX_HOSTNAME_LENGTH = 253  # RFC 1035; the 63-character label cap is enforced below
# fullmatch() also rejects a trailing newline, unlike match() with "$".
_HOSTNAME_CHARSET_RE = re.compile(r"[A-Za-z0-9.-]+")


def normalize_public_hostname(value: str | None) -> str:
    """Validate an operator-provided public hostname or IP.

    Returns the cleaned lower-case host, or ``""`` when unset. Raises ``ValueError`` with a
    user-facing message for anything that is not a bare IP or DNS name.
    """
    host = (value or "").strip(" ")
    if not host:
        return ""
    # A pasted IPv6 URL brings its brackets along: one pair is tolerated and dropped.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if "[" in host or "]" in host:
            raise ValueError("Enter the address once, in a single pair of brackets")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if ":" in host:
        raise ValueError("Enter a hostname or IP only - no scheme, port or path")
    if len(host) > _MAX_HOSTNAME_LENGTH:
        raise ValueError("Hostname is too long")
    if not _HOSTNAME_CHARSET_RE.fullmatch(host):
        raise ValueError("Hostname may only contain letters, digits, dots and hyphens")
    if ".." in host or host.startswith(".") or host.endswith("."):
        raise ValueError("Hostname has an empty label")
    for label in host.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise ValueError("Hostname has an invalid label")
    return host.lower()


def get_public_hostname(conn: sqlite3.Connection) -> str:
    """The stored public hostname, or ``""`` when unset or no longer valid."""
    stored = sqlite_db.get_setting(conn, SettingKeys.PUBLIC_HOSTNAME, "")
    try:
        return normalize_public_hostname(stored if isinstance(stored, str) else "")
    except ValueError:
        return ""


def build_public_base_url(public_hostname: str | None) -> str:
    """``https://<host>`` (IPv6 bracketed) for a stored hostname, or ``""`` when unset or invalid."""
    try:
        host = normalize_public_hostname(public_hostname)
    except ValueError:
        return ""
    if not host:
        return ""
    return f"https://[{host}]" if ":" in host else f"https://{host}"


def get_public_base_url(conn: sqlite3.Connection) -> str:
    """The instance base URL from the single ``public_hostname`` setting, or ``""`` when not set."""
    return build_public_base_url(get_public_hostname(conn))


def _utc_stamp(moment: datetime) -> str:
    """Strict UTC ISO form the target page accepts for ``from``/``to``."""
    return assume_utc(moment).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def target_url(
    base_url: str,
    target_id: int | None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> str:
    """Link to a target's detail page, the dashboard without a target, or ``"#"`` without a base URL.

    With ``start`` the detail page opens on the event's time range: ``start`` to ``end`` (or
    now while the event is open), widened by a margin on both sides. The range is capped to
    the longest span the page accepts.
    """
    if not base_url:
        return "#"
    if target_id is None:
        return f"{base_url}/"
    url = f"{base_url}/ui/targets/{int(target_id)}"
    if start is None:
        return url
    now = datetime.now(UTC)
    begin = assume_utc(start) - EVENT_RANGE_MARGIN
    finish = min(assume_utc(end), now) if end is not None else now
    finish = max(finish + EVENT_RANGE_MARGIN, begin + EVENT_RANGE_MARGIN)
    begin = max(begin, finish - MAX_EVENT_RANGE)
    return f"{url}?{urlencode({'from': _utc_stamp(begin), 'to': _utc_stamp(finish)})}"
