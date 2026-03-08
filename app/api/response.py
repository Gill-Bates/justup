#!/usr/bin/env python3
#
# app/api/response.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Common API response helpers."""

from __future__ import annotations

from typing import Any


def ok_response(*, message: str | None = None, data: Any = None, **extra: Any) -> dict[str, Any]:
	payload: dict[str, Any] = {"status": "ok"}
	if message is not None:
		payload["message"] = message
	if data is not None:
		payload["data"] = data
	if extra:
		payload.update(extra)
	return payload
