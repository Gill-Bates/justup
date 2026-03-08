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

from ..db.sqlite_runtime import close_connection, connect


def get_conn(request: Request) -> Generator:
	"""Yield a per-request SQLite connection."""
	conn = connect(request.app.state.db_path)
	try:
		yield conn
	finally:
		close_connection(conn)


def get_tsdb_dir(request: Request) -> Path:
	return request.app.state.tsdb_dir


def get_config(request: Request):
	return request.app.state.cfg
