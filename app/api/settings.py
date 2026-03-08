#!/usr/bin/env python3
#
# app/api/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Settings API routes (admin-only)."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from ..db.sqlite_settings import (
	_ALLOWED_SETTINGS,
	_BOOLEAN_SETTINGS,
	get_all_settings,
	set_setting,
)
from .auth import require_admin
from ..utils.deps import get_conn
from .response import ok_response

_log = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


@router.get("")
def get_settings(
	_=Depends(require_admin),
	conn: sqlite3.Connection = Depends(get_conn),
):
	return ok_response(data=get_all_settings(conn))


@router.put("")
def update_settings(
	payload: dict,
	_=Depends(require_admin),
	conn: sqlite3.Connection = Depends(get_conn),
):
	updated = []
	for key, value in payload.items():
		if key not in _ALLOWED_SETTINGS:
			raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
		value_str = str(value).strip()
		if key in _BOOLEAN_SETTINGS:
			if value_str.lower() not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
				raise HTTPException(status_code=400, detail=f"Invalid boolean value for {key}")
		set_setting(conn, key, value_str)
		updated.append(key)

	return ok_response(data={"updated": updated})
