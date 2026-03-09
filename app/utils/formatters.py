#!/usr/bin/env python3
#
# app/utils/formatters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Formatting and normalization helpers for UI/API payloads."""

from datetime import datetime
import json
from typing import Any, Optional
import sqlite3

def normalize_datetime(val: Any) -> Optional[datetime]:
	"""Normalize datetime from DB (may be str or datetime)."""
	if val is None:
		return None
	if isinstance(val, datetime):
		return val
	if isinstance(val, str):
		if not val:
			return None
		try:
			return datetime.fromisoformat(val)
		except ValueError:
			return None
	return None

def format_duration(seconds: Optional[int]) -> Optional[str]:
	"""Format a duration (in seconds) into a short human-readable string."""
	if seconds is None:
		return None
	if seconds < 60:
		return f"{seconds}s"
	elif seconds < 3600:
		return f"{seconds // 60}m"
	else:
		hours = seconds // 3600
		mins = (seconds % 3600) // 60
		return f"{hours}h {mins}m"

def row_to_alert_dict(row: sqlite3.Row) -> dict[str, Any]:
	"""
	Convert alert row to standardized dict.
	Handles type normalization and JSON parsing for recipients.
	Includes both raw datetimes and formatted strings for UI.
	"""
	recipients: list[str] = []
	if row["recipients_notified"]:
		try:
			recipients = json.loads(row["recipients_notified"])
		except (json.JSONDecodeError, TypeError):
			recipients = []
	
	started_at = normalize_datetime(row["started_at"])
	ended_at = normalize_datetime(row["ended_at"])
	acknowledged_at = normalize_datetime(row["acknowledged_at"])
	
	duration_seconds = int(row["duration_seconds"]) if row["duration_seconds"] is not None else None
	
	return {
		"id": str(row["id"]),
		"target_id": int(row["target_id"]),
		"target_name": str(row["target_name"]),
		"failed_service": str(row["failed_service"]),
		"status": row["status"],
		"started_at": started_at,
		"started_at_iso": started_at.isoformat() if started_at else None,
		"started_at_formatted": started_at.strftime("%Y-%m-%d %H:%M") if started_at else "",
		"ended_at": ended_at,
		"ended_at_iso": ended_at.isoformat() if ended_at else None,
		"duration_seconds": duration_seconds,
		"duration_formatted": format_duration(duration_seconds),
		"recipients_notified": recipients,
		"is_acknowledged": bool(row["is_acknowledged"]),
		"acknowledged_by": row["acknowledged_by"],
		"acknowledged_at": acknowledged_at,
		"acknowledged_at_iso": acknowledged_at.isoformat() if acknowledged_at else None,
	}

def format_percent(value: float | None) -> str:
	"""Format a percentage value cleanly.
	
	100.0 or >= 99.95 -> "100%" (rounds up visually)
	>= 10             -> "xx.x%" (1 decimal place)
	< 10              -> "x.xx%" (2 decimal places)
	None              -> "–"
	"""
	if value is None:
		return "–"
	if value >= 99.95:
		return "100%"
	if value >= 10:
		return f"{value:.1f}%"
	return f"{value:.2f}%"
