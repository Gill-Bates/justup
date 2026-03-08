#!/usr/bin/env python3
#
# app/api/frontend_shared.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared frontend router primitives and helpers."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..utils.version import BUILD_INFO, VERSION
from .auth import get_current_user_optional

_log = logging.getLogger(__name__)

router = APIRouter(tags=["frontend"])

_templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_path))
templates.env.globals["VERSION"] = VERSION
templates.env.globals["BUILD_INFO"] = BUILD_INFO
templates.env.globals["build_short"] = (BUILD_INFO or "")[:7]
templates.env.globals["current_year"] = datetime.now().year


class RedirectTo(Exception):
	def __init__(self, url: str):
		super().__init__(url)
		self.url = url


async def redirect_to_handler(_: Request, exc: RedirectTo) -> RedirectResponse:
	return RedirectResponse(url=exc.url, status_code=303)


def _get_csrf_token(request: Request) -> str:
	token = getattr(request.state, "csrf_token", None)
	if not token:
		raise HTTPException(status_code=500, detail="Internal server error")
	return token


def _raise_redirect(url: str) -> None:
	raise RedirectTo(url)


def require_user_or_redirect(
	user: Optional[sqlite3.Row] = Depends(get_current_user_optional),
) -> sqlite3.Row:
	if user is None:
		_raise_redirect("/login")
	return user


def require_admin_or_redirect(
	user: sqlite3.Row = Depends(require_user_or_redirect),
) -> sqlite3.Row:
	if not user["is_admin"]:
		_raise_redirect("/ui/dashboard")
	return user
