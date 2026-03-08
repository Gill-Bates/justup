#!/usr/bin/env python3
#
# app/utils/time.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Timezone-aware time utilities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
	"""Return the current UTC time as a timezone-aware datetime."""
	return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
	if dt is None:
		return None
	if dt.tzinfo is None:
		raise ValueError("Naive datetime not allowed - must be timezone-aware")
	return dt.astimezone(timezone.utc)


def parse_utc(s: str) -> Optional[datetime]:
	if not s:
		return None
	try:
		if s.endswith("Z"):
			s = s[:-1] + "+00:00"
		dt = datetime.fromisoformat(s)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc)
	except ValueError:
		return None
