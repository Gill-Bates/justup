#!/usr/bin/env python3
#
# app/utils/deps.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""FastAPI dependency helpers."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from fastapi import Request

from ..db import sqlite as sqlite_db


def get_conn(request: Request) -> Generator:
	"""Yield a per-request SQLite connection."""
	conn = sqlite_db.connect(request.app.state.db_path)
	try:
		yield conn
	finally:
		conn.close()


def get_tsdb_dir(request: Request) -> Path:
	"""Central helper to get TSDB directory from app state."""
	return request.app.state.tsdb_dir


def is_mobile_device(request: Request) -> bool:
	"""
	Detect if the request comes from a mobile device.
	
	Checks User-Agent header for common mobile device indicators.
	This is used to restrict certain features (like Signal registration)
	to mobile devices only.
	
	Args:
		request: FastAPI Request object
	
	Returns:
		True if the request is from a mobile device, False otherwise
	"""
	ua = request.headers.get("user-agent", "").lower()
	# Common mobile device identifiers in User-Agent strings
	mobile_keywords = (
		"android",
		"iphone",
		"ipad",
		"ipod",
		"mobile",
		"blackberry",
		"windows phone",
		"webos",
	)
	return any(keyword in ua for keyword in mobile_keywords)
