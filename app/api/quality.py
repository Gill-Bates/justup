#!/usr/bin/env python3
#
# app/api/quality.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for monitor quality (Quality Gate feature)."""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from datetime import timedelta
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query

from ..db import sqlite as sqlite_db
from ..monitor.quality_probe import get_quality_targets
from ..services.traceroute_geo import geolocate_country
from ..utils.deps import get_conn
from ..utils.time import utcnow
from .auth import get_current_user

router = APIRouter(prefix="", tags=["quality"])
_log = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _target_country(target: str, refresh_period: int) -> str | None:
    """Resolve configured targets using local GeoLite2, with bounded DNS refreshes."""
    del refresh_period
    try:
        try:
            host = str(ipaddress.ip_address(target))
        except ValueError:
            parsed = urlsplit(target if "://" in target else f"//{target}")
            host = parsed.hostname
        if not host:
            return None
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, None)))
        for address in addresses:
            code = geolocate_country(address)
            if isinstance(code, str) and len(code) == 2 and code.isascii() and code.isalpha():
                return code.upper()
    except (OSError, ValueError) as exc:
        _log.debug("Quality target location unavailable: %s", exc)
    return None


def _target_details(targets: list[str]) -> list[dict[str, str | None]]:
    refresh_period = int(time.monotonic() // 600)
    return [{"target": target, "country_code": _target_country(target, refresh_period)} for target in targets]


@router.get("/quality/current")
def get_quality_current(
    conn=Depends(get_conn),
    _user: dict = Depends(get_current_user),
):
    """Get the current monitor quality state, score, and probe targets."""
    row = sqlite_db.get_monitor_quality_latest(conn)

    targets = get_quality_targets()

    if not row:
        return {
            "score": None,
            "state": "unknown",
            "updated_at": None,
            "message": "No quality data available yet",
            "targets": targets,
            "target_details": _target_details(targets),
        }

    return {
        "score": row["score"],
        "state": row["state"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "note": "Quality score is based on recent probe runs",
        "targets": targets,
        "target_details": _target_details(targets),
    }


@router.get("/quality/history")
def get_quality_history(
    conn=Depends(get_conn),
    hours: int = Query(default=24, ge=1, le=168, description="Hours to look back (max 7 days)"),
    _user: dict = Depends(get_current_user),
):
    """Get quality score history for charting."""
    since = utcnow() - timedelta(hours=hours)
    rows = sqlite_db.query_monitor_quality(conn, since=since, limit=500)

    return [
        {
            "ts": row["ts"].isoformat() if row["ts"] else None,
            "score": row["score"],
            "median_latency_ms": row["median_latency_ms"],
            "success_ratio": row["success_ratio"],
            "state": row["state"],
        }
        for row in rows
    ]
