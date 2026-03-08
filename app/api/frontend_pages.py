#!/usr/bin/env python3
#
# app/api/frontend_pages.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Frontend page routes (HTML rendering)."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..db.sqlite_monitors import get_all_monitor_statuses
from ..db.sqlite_incidents import get_recent_incidents
from ..utils.deps import get_conn
from .auth import get_current_user_optional
from .frontend_shared import (
	_get_csrf_token,
	require_admin_or_redirect,
	require_user_or_redirect,
	templates,
)

_log = logging.getLogger(__name__)

router = APIRouter(tags=["frontend-pages"])


@router.get("/login")
def login_page(request: Request, user=Depends(get_current_user_optional)):
	if user:
		return RedirectResponse(url="/ui/dashboard", status_code=303)
	csrf_token = _get_csrf_token(request)
	return templates.TemplateResponse("login.html", {"request": request, "csrf_token": csrf_token})


@router.get("/ui/dashboard")
def dashboard_page(
	request: Request,
	user=Depends(require_user_or_redirect),
	conn: sqlite3.Connection = Depends(get_conn),
):
	csrf_token = _get_csrf_token(request)
	monitors = get_all_monitor_statuses(conn)
	incidents = get_recent_incidents(conn, limit=10)
	return templates.TemplateResponse("dashboard.html", {
		"request": request,
		"user": user,
		"csrf_token": csrf_token,
		"monitors": monitors,
		"incidents": incidents,
	})


@router.get("/ui/monitors")
def monitors_page(
	request: Request,
	user=Depends(require_user_or_redirect),
	conn: sqlite3.Connection = Depends(get_conn),
):
	csrf_token = _get_csrf_token(request)
	monitors = get_all_monitor_statuses(conn)
	return templates.TemplateResponse("monitors.html", {
		"request": request,
		"user": user,
		"csrf_token": csrf_token,
		"monitors": monitors,
	})


@router.get("/ui/users")
def users_page(
	request: Request,
	user=Depends(require_admin_or_redirect),
):
	csrf_token = _get_csrf_token(request)
	return templates.TemplateResponse("users.html", {
		"request": request,
		"user": user,
		"csrf_token": csrf_token,
	})


@router.get("/ui/settings")
def settings_page(
	request: Request,
	user=Depends(require_admin_or_redirect),
):
	csrf_token = _get_csrf_token(request)
	return templates.TemplateResponse("settings.html", {
		"request": request,
		"user": user,
		"csrf_token": csrf_token,
	})


@router.get("/ui/about")
def about_page(
	request: Request,
	user=Depends(require_user_or_redirect),
):
	csrf_token = _get_csrf_token(request)
	return templates.TemplateResponse("about.html", {
		"request": request,
		"user": user,
		"csrf_token": csrf_token,
	})
