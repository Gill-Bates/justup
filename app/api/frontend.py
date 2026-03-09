#!/usr/bin/env python3
#
# app/api/frontend.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""HTML frontend routes (server-rendered templates).

Auth Strategy:
- Full pages (dashboard, targets, settings): use require_ui_user() -> redirect to login
- HTMX fragments: use resolve_user_or_htmx_redirect() -> HX-Redirect header
- Public fragments (login): no auth required

Async/Sync Strategy:
- Most UI endpoints are SYNC: Simple DB reads, template rendering (SQLite is fast)
- ASYNC endpoints: Expensive I/O operations only (MTR, external API calls)
  - fragment_traceroute: MTR subprocess + geolocation
  - fragment_downtime_history: TSDB range queries
  - fragment_signal_*: External Signal API calls
- Rationale: Sync is simpler, async overhead not justified for <10ms DB queries

HTTP Status Codes:
- 200: Success with content
- 302: Redirect to login (full pages)
- 400: Invalid user input (bad date format, invalid filter)
- 403: Insufficient privileges (admin-only endpoints)
- 404: Resource not found
- 503: External service unavailable (Signal API down)
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import markdown
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.formatters import row_to_alert_dict, format_percent
from ..utils.http_status import DEFAULT_HTTP_SUCCESS_CODES
from ..utils.settings_cache import (

    get_pdf_page_size,
    use_utc_dashboard,
)
from ..utils.status_helper import get_status_defaults, build_target_status, get_simple_status
from ..utils.version import VERSION, BUILD_INFO
from ..utils.rate_limit import limiter
from ..utils.auth_helpers import (
    resolve_user_optional,
    resolve_user_or_redirect,
    resolve_user_or_htmx_redirect,
)
router = APIRouter(tags=["frontend"])

# Templates
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Add version to all template contexts
templates.env.globals["version"] = f"v{VERSION}"
templates.env.globals["build_info"] = BUILD_INFO

# Add format_percent filter for consistent percentage formatting
templates.env.filters["format_percent"] = format_percent

# Add regex_replace filter for slugifying group IDs
def _regex_replace(value: str, pattern: str, replacement: str) -> str:
    """Replace regex pattern in string."""
    return re.sub(pattern, replacement, value)
templates.env.filters["regex_replace"] = _regex_replace

# Add markdown filter for rendering markdown to HTML
def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br", "md_in_html"],
        output_format="html"
    )
templates.env.filters["markdown"] = _render_markdown

# Import centralized constants
from ..utils.constants import (
    DISPLAY_UNGROUPED_EN,
    DATE_FORMAT_ISO,
    CheckStatus,
    AlertStatus,
    UI_PAGE_SIZE_ALERTS,
    UI_PAGE_SIZE_TARGETS,
    UPTIME_THRESHOLD_GREEN,
    UPTIME_THRESHOLD_YELLOW,
    SORT_SENTINEL_LAST,
)

_log = logging.getLogger(__name__)


# --- Inline HTML Helpers (prevent XSS, centralize styling) ---

# Valid Bootstrap badge colors (prevents class injection)
VALID_BADGE_COLORS = {"primary", "secondary", "success", "danger", "warning", "info", "light", "dark"}

def _html_badge(text: str, color: str = "secondary", icon: str | None = None) -> str:
	"""Generate a Bootstrap badge with optional Material icon."""
	# Validate color to prevent class injection
	color = color if color in VALID_BADGE_COLORS else "secondary"
	escaped = html.escape(text)
	icon_html = f'<span class="material-icons me-1">{html.escape(icon)}</span>' if icon else ""
	return f'<span class="badge bg-{color}">{icon_html}{escaped}</span>'


def _html_error(text: str) -> str:
	"""Generate a small error message."""
	return f'<div class="text-danger small">{html.escape(text)}</div>'


def _get_geoip_build_info() -> int | None:
	"""Get GeoIP database build epoch for display in About page."""
	try:
		from ..utils.bootstrap import get_geoip_build_info
		result = get_geoip_build_info()
		if result:
			return result[1]  # Return build_epoch only
		return None
	except Exception:
		return None


def _html_muted(text: str) -> str:
	"""Generate a muted/gray text span."""
	return f'<span class="text-muted">{html.escape(text)}</span>'


def _row_get(row, key: str, default=None):
	"""Safely get value from sqlite3.Row object (which doesn't have .get() method)."""
	try:
		return row[key]
	except (KeyError, IndexError):
		return default


def _resolve_target_host(row) -> str | None:
	"""Extract best host from target row (http_url → ping_host → host → cert_host)."""
	from urllib.parse import urlparse
	
	http_url = _row_get(row, "http_url") or ""
	if http_url:
		try:
			parsed = urlparse(http_url)
			if parsed.hostname:
				return parsed.hostname
		except Exception:
			pass
	
	return _row_get(row, "ping_host") or _row_get(row, "host") or _row_get(row, "cert_host")


def _compute_storage_stats(db_path: Path, tsdb_dir: Path) -> dict:
	"""Compute storage statistics (blocking I/O, run in threadpool)."""
	sql_size_bytes = db_path.stat().st_size if db_path.exists() else 0
	
	tsdb_size_bytes = 0
	total_points = 0
	
	if tsdb_dir.exists():
		for jsonl_file in tsdb_dir.rglob("*.jsonl"):
			tsdb_size_bytes += jsonl_file.stat().st_size
			try:
				with open(jsonl_file, "r") as f:
					total_points += sum(1 for _ in f)
			except Exception:
				pass
	
	return {
		"sql_size_bytes": sql_size_bytes,
		"tsdb_size_bytes": tsdb_size_bytes,
		"total_points": total_points,
	}


def require_ui_user(request: Request, conn) -> dict | Response:
	"""Helper to resolve UI user or return redirect response."""
	user = resolve_user_optional(request, conn, allow_cookie=True)
	if not user:
		return RedirectResponse("/ui/login", status_code=302)
	# Convert sqlite3.Row to dict for .get() support
	return dict(user)


def require_admin_htmx(request: Request, conn) -> dict | Response:
	"""Helper to resolve admin user or return HTMX-compatible error."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	if not bool(user.get("is_admin")):
		return HTMLResponse(_html_error("Admin privileges required"), status_code=403)
	return dict(user)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_conn)):
	"""Render the dashboard page."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user

	return templates.TemplateResponse("dashboard.html", {
		"request": request,
		"active_page": "dashboard",
		"user": user,
	})


@router.get("/ui/login", response_class=HTMLResponse)
def login_page(request: Request):
	"""Render the login page."""
	return templates.TemplateResponse("login.html", {
		"request": request,
	})


@router.get("/ui/targets", response_class=HTMLResponse)
def targets_page(request: Request, conn=Depends(get_conn)):
	"""Render the targets list page."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	return templates.TemplateResponse("targets.html", {
		"request": request,
		"active_page": "targets",
		"user": user,
	})


@router.get("/ui/targets/new", response_class=HTMLResponse)
def target_new_page(request: Request, conn=Depends(get_conn)):
	"""Render the create-target form."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	groups = [dict(row) for row in sqlite_db.list_groups(conn)]
	return templates.TemplateResponse("target_form.html", {
		"request": request,
		"active_page": "targets",
		"user": user,
		"target": None,
		"groups": groups,
	})


@router.get("/ui/targets/{target_id}", response_class=HTMLResponse)
def target_detail_page(target_id: int, request: Request, conn=Depends(get_conn)):
	"""Render the target detail page."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		raise HTTPException(status_code=404, detail="Target not found")
	target = _row_to_dict(row)

	cached = sqlite_db.get_target_status(conn, target_id)
	if cached:
		target.update(build_target_status(row, cached))
	else:
		target.update(get_status_defaults())

	return templates.TemplateResponse("target_detail.html", {
		"request": request,
		"active_page": "targets",
		"user": user,
		"target": target,
		"use_utc": use_utc_dashboard(conn),
		"pdf_page_size": get_pdf_page_size(conn),
		"has_recipients": len(sqlite_db.get_target_recipient_ids(conn, target_id)) > 0,
		"alert_recipients": [dict(row) for row in sqlite_db.get_target_recipients(conn, target_id)],
	})


@router.get("/ui/targets/{target_id}/edit", response_class=HTMLResponse)
def target_edit_page(target_id: int, request: Request, conn=Depends(get_conn)):
	"""Render the edit-target form."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		raise HTTPException(status_code=404, detail="Target not found")
	target = _row_to_dict(row)
	groups = [dict(row) for row in sqlite_db.list_groups(conn)]
	return templates.TemplateResponse("target_form.html", {
		"request": request,
		"active_page": "targets",
		"user": user,
		"target": target,
		"groups": groups,
	})


@router.get("/ui/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(get_conn)):
	"""Render the settings page."""
	import os
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	
	# Only admins can access settings (return 403 per docstring)
	if not user.get("is_admin"):
		raise HTTPException(status_code=403, detail="Admin privileges required")
	
	# Get sender number from env (new subprocess-based design)
	sender_number = os.environ.get("SIGNAL_SENDER_NUMBER")
	
	return templates.TemplateResponse("settings.html", {
		"request": request,
		"active_page": "settings",
		"user": user,
		"signal_sender_number": sender_number,
	})


# ─── Settings Tab Fragments (HTMX lazy loading) ─────────────────────────────────

@router.get("/ui/fragments/settings-general", response_class=HTMLResponse)
def fragment_settings_general(request: Request, conn=Depends(get_conn)):
	"""Render General settings tab fragment with pre-populated values."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	
	# Load general settings from database
	general_data = {
		"purge_enabled": sqlite_db.get_setting(conn, "purge_enabled", True),
		"use_utc_dashboard": sqlite_db.get_setting(conn, "use_utc_dashboard", False),
		"downsample_enabled": sqlite_db.get_setting(conn, "downsample_enabled", True),
		"geoip_auto_update": sqlite_db.get_setting(conn, "geoip_auto_update", True),
		"telemetry_enabled": sqlite_db.get_setting(conn, "telemetry_enabled", True),
		"pdf_page_size": sqlite_db.get_setting(conn, "pdf_page_size", "a4") or "a4",
		"auth_disabled": sqlite_db.get_setting(conn, "auth_disabled", False),
		"metrics_timeout_minutes": sqlite_db.get_setting(conn, "metrics_timeout_minutes", 5) or 5,
	}
	
	return templates.TemplateResponse("settings/general.html", {
		"request": request,
		"current_user": user,
		"settings": general_data,
	})


@router.get("/ui/fragments/settings-storage", response_class=HTMLResponse)
async def fragment_settings_storage(request: Request, conn=Depends(get_conn)):
	"""Render Storage settings tab fragment with pre-computed stats."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	
	# Compute storage stats in threadpool (blocking I/O)
	db_path = request.app.state.db_path
	tsdb_dir = get_tsdb_dir(request)
	stats = await run_in_threadpool(_compute_storage_stats, db_path, tsdb_dir)
	
	sql_size_bytes = stats["sql_size_bytes"]
	tsdb_size_bytes = stats["tsdb_size_bytes"]
	total_points = stats["total_points"]
	
	# Format for display
	def format_bytes(b):
		if b < 1024:
			return f"{b} B"
		elif b < 1024 * 1024:
			return f"{b / 1024:.1f} KB"
		elif b < 1024 * 1024 * 1024:
			return f"{b / (1024 * 1024):.1f} MB"
		else:
			return f"{b / (1024 * 1024 * 1024):.2f} GB"
	
	def format_number(n):
		if n >= 1_000_000:
			return f"{n / 1_000_000:.2f}M"
		elif n >= 1_000:
			return f"{n / 1_000:.0f}k"
		return str(n)
	
	return templates.TemplateResponse("settings/storage.html", {
		"request": request,
		"current_user": user,
		"storage": {
			"sql_size": format_bytes(sql_size_bytes),
			"tsdb_size": format_bytes(tsdb_size_bytes),
			"total_points": format_number(total_points),
		},
	})


@router.get("/ui/fragments/settings-backup", response_class=HTMLResponse)
def fragment_settings_backup(request: Request, conn=Depends(get_conn)):
	"""Render Backup settings tab fragment."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	return templates.TemplateResponse("settings/backup.html", {
		"request": request,
		"current_user": user,
	})


@router.get("/ui/fragments/settings-smtp", response_class=HTMLResponse)
def fragment_settings_smtp(request: Request, conn=Depends(get_conn)):
	"""Render SMTP settings tab fragment with pre-populated values."""
	from ..utils.constants import SettingKeys
	
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	
	# Load SMTP settings from database (using SettingKeys for consistency)
	smtp_data = {
		"smtp_enabled": sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False),
		"smtp_host": sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST, "") or "",
		"smtp_port": sqlite_db.get_setting(conn, SettingKeys.SMTP_PORT, "") or "",
		"smtp_from": sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM, "") or "",
		"smtp_user": sqlite_db.get_setting(conn, SettingKeys.SMTP_USER, "") or "",
		"smtp_use_tls": sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, False),
	}
	
	# Load email template settings
	from ..services.notifications.email_template import DEFAULT_SUBJECT, DEFAULT_HTML_TEMPLATE, LOGO_URL_PREVIEW
	
	subject_db = sqlite_db.get_setting(conn, "smtp_mail_template_subject", None) or DEFAULT_SUBJECT
	html_db = sqlite_db.get_setting(conn, "smtp_mail_template_html", None) or DEFAULT_HTML_TEMPLATE
	
	# Replace {{logo_url}} placeholder with local preview URL for TinyMCE
	template_data = {
		"subject": subject_db.replace("{{logo_url}}", LOGO_URL_PREVIEW),
		"html": html_db.replace("{{logo_url}}", LOGO_URL_PREVIEW),
	}
	
	return templates.TemplateResponse("settings/smtp.html", {
		"request": request,
		"current_user": user,
		"smtp": smtp_data,
		"template": template_data,
	})


@router.get("/ui/fragments/settings-users", response_class=HTMLResponse)
def fragment_settings_users(request: Request, conn=Depends(get_conn)):
	"""Render Users settings tab fragment."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	return templates.TemplateResponse("settings/users.html", {
		"request": request,
		"current_user": user,
	})


@router.get("/ui/fragments/settings-recipients", response_class=HTMLResponse)
def fragment_settings_recipients(request: Request, conn=Depends(get_conn)):
	"""Render Recipients settings tab fragment."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	return templates.TemplateResponse("settings/recipients.html", {
		"request": request,
		"current_user": user,
	})


@router.get("/ui/fragments/settings-spooler", response_class=HTMLResponse)
def fragment_settings_spooler(request: Request, conn=Depends(get_conn)):
	"""Render Spooler settings tab fragment."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	return templates.TemplateResponse("settings/spooler.html", {
		"request": request,
		"current_user": user,
	})


@router.get("/ui/fragments/settings-signal", response_class=HTMLResponse)
def fragment_settings_signal(request: Request, conn=Depends(get_conn)):
	"""Render Signal settings tab fragment."""
	import os
	from ..utils.constants import SettingKeys
	from ..services.notifications.signal_template import DEFAULT_SIGNAL_TEMPLATE_DOWN, DEFAULT_SIGNAL_TEMPLATE_UP

	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	
	# Get sender number from env (new subprocess-based design)
	sender_number = os.environ.get("SIGNAL_SENDER_NUMBER")
	
	# Get signal_enabled setting
	signal_enabled = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)

	# Load Signal message templates (fallback to defaults)
	signal_template = {
		"down": sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, None) or DEFAULT_SIGNAL_TEMPLATE_DOWN,
		"up": sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, None) or DEFAULT_SIGNAL_TEMPLATE_UP,
	}
	
	return templates.TemplateResponse("settings/signal.html", {
		"request": request,
		"current_user": user,
		"signal_sender_number": sender_number,
		"signal_enabled": signal_enabled,
		"signal_template": signal_template,
	})


@router.get("/ui/about", response_class=HTMLResponse)
def about_page(request: Request, conn=Depends(get_conn)):
	"""Render the about page with version, dependencies, and license."""
	import sys
	
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	
	# Get Python version
	python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
	
	# Parse requirements.txt for versions (works in Docker and standalone installs)
	requirements_versions: dict[str, str] = {}
	requirements_paths = [
		Path(__file__).resolve().parent.parent.parent / "requirements.txt",
		Path("/app/requirements.txt"),
	]
	for req_path in requirements_paths:
		if req_path.exists():
			try:
				for line in req_path.read_text(encoding="utf-8").splitlines():
					line = line.strip()
					if not line or line.startswith("#"):
						continue
					# Parse "package==1.2.3" or "package>=1.2.3" etc.
					for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
						if sep in line:
							pkg, ver = line.split(sep, 1)
							pkg = pkg.strip().lower()
							ver = ver.split("#")[0].strip()  # Remove inline comments
							requirements_versions[pkg] = ver
							break
				break
			except Exception:
				pass
	
	# Key dependencies with versions (all from requirements.txt, sorted alphabetically)
	key_packages = [
		"adjusttext", "cartopy", "cryptography", "dnspython", "email-validator",
		"fastapi", "geoip2", "httpx", "jinja2", "markdown", "matplotlib",
		"pydantic", "pyproj", "python-multipart", "qrcode", "reportlab",
		"shapely", "slowapi", "sslyze", "uvicorn",
	]
	
	dependencies = []
	for pkg_name in key_packages:
		# First try requirements.txt (reliable in container images)
		ver = requirements_versions.get(pkg_name.lower())
		if ver:
			dependencies.append((pkg_name, ver))
		else:
			# Fallback to importlib.metadata (works in dev environment)
			try:
				from importlib.metadata import version as get_pkg_version
				ver = get_pkg_version(pkg_name)
				dependencies.append((pkg_name, ver))
			except Exception:
				dependencies.append((pkg_name, "?"))
	
	# Add signal-cli version (external binary, not a Python package)
	try:
		import subprocess
		result = subprocess.run(
			["signal-cli", "--version"],
			capture_output=True,
			text=True,
			timeout=5,
		)
		if result.returncode == 0 and result.stdout.strip():
			# Output format: "signal-cli 0.13.23"
			version_str = result.stdout.strip().split()[-1]  # Get version number
			dependencies.append(("signal-cli", version_str))
		else:
			dependencies.append(("signal-cli", "not found"))
	except Exception:
		dependencies.append(("signal-cli", "not available"))
	
	# Sort dependencies alphabetically
	dependencies.sort(key=lambda x: x[0].lower())
	
	# Read license file
	license_text = ""
	license_paths = [
		Path(__file__).resolve().parent.parent.parent / "LICENSE",
		Path("/app/LICENSE"),
	]
	for license_path in license_paths:
		if license_path.exists():
			try:
				license_text = license_path.read_text(encoding="utf-8")
				break
			except Exception:
				pass
	
	if not license_text:
		license_text = "License file not found."
	
	# Read changelog file
	changelog_text = ""
	changelog_paths = [
		Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md",
		Path("/app/CHANGELOG.md"),
	]
	for changelog_path in changelog_paths:
		if changelog_path.exists():
			try:
				changelog_text = changelog_path.read_text(encoding="utf-8")
				break
			except Exception:
				pass
	
	if not changelog_text:
		changelog_text = "Changelog not found."
	
	return templates.TemplateResponse("about.html", {
		"request": request,
		"active_page": "about",
		"user": user,
		"version": VERSION,
		"build_info": BUILD_INFO,
		"python_version": python_version,
		"geoip_build_info": _get_geoip_build_info(),
		"dependencies": dependencies,
		"license_text": license_text,
		"changelog_text": changelog_text,
	})


@router.get("/ui/groups", response_class=HTMLResponse)
def groups_page(request: Request, conn=Depends(get_conn)):
	"""Render the groups page."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user

	groups_rows = sqlite_db.list_groups_with_counts(conn)
	groups = [dict(row) for row in groups_rows]

	return templates.TemplateResponse("groups.html", {
		"request": request,
		"active_page": "groups",
		"user": user,
		"groups": groups,
	})


@router.get("/ui/alerts", response_class=HTMLResponse)
def alerts_page(
	request: Request,
	target_id: Optional[str] = Query(None),
	status: Optional[str] = Query(None),
	from_date: Optional[str] = Query(None),
	to_date: Optional[str] = Query(None),
	page: int = Query(1, ge=1),
	conn=Depends(get_conn),
):
	"""Alerts page with filtering and pagination."""
	user = require_ui_user(request, conn)
	if isinstance(user, Response):
		return user
	
	# Normalize empty strings to None
	status = status if status and status.strip() else None
	from_date = from_date if from_date and from_date.strip() else None
	to_date = to_date if to_date and to_date.strip() else None
	
	# Parse target_id (form sends "" for "All targets")
	target_id_int: Optional[int] = None
	if target_id and target_id.strip():
		try:
			target_id_int = int(target_id)
		except ValueError:
			raise HTTPException(status_code=400, detail="Invalid target_id")
	
	# Parse dates - return 400 for invalid input (per docstring)
	from_dt = None
	to_dt = None
	if from_date:
		try:
			from_dt = datetime.strptime(from_date, DATE_FORMAT_ISO).replace(tzinfo=timezone.utc)
		except ValueError:
			raise HTTPException(status_code=400, detail=f"Invalid from_date format: {from_date} (expected YYYY-MM-DD)")
	if to_date:
		try:
			to_dt = datetime.strptime(to_date, DATE_FORMAT_ISO).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
		except ValueError:
			raise HTTPException(status_code=400, detail=f"Invalid to_date format: {to_date} (expected YYYY-MM-DD)")
	
	# Get alerts (page size from constants)
	page_size = UI_PAGE_SIZE_ALERTS
	alerts_rows, total = sqlite_db.list_alerts(
		conn,
		target_id=target_id_int,
		from_date=from_dt,
		to_date=to_dt,
		status=status,
		page=page,
		page_size=page_size,
	)
	
	# Count open alerts
	_, open_total = sqlite_db.list_alerts(conn, status="open", page=1, page_size=1)
	
	# Format alerts for template
	alerts = [row_to_alert_dict(row) for row in alerts_rows]
	
	# Get all targets for filter dropdown and for service flags lookup
	all_targets = sqlite_db.list_targets(conn)
	targets = [{"id": t["id"], "name": t["name"]} for t in all_targets]
	
	# Build target service flags lookup for alert badge coloring
	target_services: dict[int, list[str]] = {}
	for t in all_targets:
		services = []
		if t["enable_http_check"]:
			services.append("HTTP")
		if t["enable_ping"]:
			services.append("PING")
		if t["enable_tcp_connect"]:
			services.append("TCP")
		target_services[t["id"]] = services
	
	# Enrich alerts with target's enabled services
	for alert in alerts:
		tid = alert["target_id"]
		alert["target_services"] = target_services.get(tid, [])
	
	total_pages = (total + page_size - 1) // page_size
	
	# Build query string for pagination (excluding page) - URL-encoded for safety
	from urllib.parse import urlencode
	query_string = urlencode({k: v for k, v in {
		"target_id": target_id_int,
		"status": status,
		"from_date": from_date,
		"to_date": to_date,
	}.items() if v is not None})
	
	return templates.TemplateResponse("alerts.html", {
		"request": request,
		"active_page": "alerts",
		"user": user,
		"alerts": alerts,
		"targets": targets,
		"total": total,
		"open_count": open_total,
		"page": page,
		"page_size": page_size,
		"total_pages": total_pages,
		"selected_target_id": target_id_int,
		"selected_status": status,
		"from_date": from_date or "",
		"to_date": to_date or "",
		"query_string": query_string,
	})


# ─────────────────────────────────────────────────────────────
# HTMX Fragments (all require authentication)
# ─────────────────────────────────────────────────────────────

@router.get("/ui/fragments/status-summary", response_class=HTMLResponse)
@limiter.limit("30/minute")  # Task 7: Summary polling
def fragment_status_summary(request: Request, conn=Depends(get_conn)):
	"""Status summary cards - requires auth."""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	rows = sqlite_db.list_targets(conn)
	
	# Get all cached statuses (single query)
	status_cache = {s["target_id"]: s for s in sqlite_db.get_all_target_statuses(conn)}
	
	stats = {"total": len(rows), "up": 0, "down": 0, "disabled": 0}
	for r in rows:
		if not bool(r["is_enabled"]):
			stats["disabled"] += 1
			continue
		
		# Use cached overall_status if available
		cached = status_cache.get(int(r["id"]))
		if cached and cached["overall_status"] and cached["overall_status"] != CheckStatus.UNKNOWN:
			status = cached["overall_status"]
		else:
			# No cache = unknown (no TSDB fallback in UI)
			status = CheckStatus.UNKNOWN
		
		if status == CheckStatus.UP:
			stats["up"] += 1
		elif status == CheckStatus.DOWN:
			stats["down"] += 1
	return templates.TemplateResponse("fragments/status_summary.html", {
		"request": request,
		"stats": stats,
	})


@router.get("/ui/fragments/targets-table", response_class=HTMLResponse)
@limiter.limit("30/minute")  # Task 7: Table refresh
def fragment_targets_table(
	request: Request,
	page: int = Query(1, ge=1),
	conn=Depends(get_conn)
):
	"""Targets table - requires auth."""
	user = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(user, Response):
		return user
	
	page_size = UI_PAGE_SIZE_TARGETS
	rows, total = sqlite_db.list_targets_paginated(conn, page=page, page_size=page_size)
	
	# Get all cached statuses (single query)
	status_cache = {s["target_id"]: s for s in sqlite_db.get_all_target_statuses(conn)}
	
	targets = []
	for r in rows:
		t = _row_to_dict(r)
		target_id = int(r["id"])
		cached = status_cache.get(target_id)
		
		# Use cached status if available, otherwise show unknown (no TSDB in UI)
		if cached:
			t.update(build_target_status(r, cached))
		else:
			# No cache = show defaults (scheduler hasn't run yet)
			t.update(get_status_defaults())
		
		targets.append(t)
	
	# Sortierung wurde bereits durch SQL erledigt (list_targets_paginated)
	
	# Group targets by group name
	from collections import OrderedDict
	grouped_targets = OrderedDict()
	for t in targets:
		group_name = t["group"] or DISPLAY_UNGROUPED_EN
		if group_name not in grouped_targets:
			grouped_targets[group_name] = []
		grouped_targets[group_name].append(t)
	
	total_pages = (total + page_size - 1) // page_size
	
	return templates.TemplateResponse("fragments/targets_table.html", {
		"request": request,
		"targets": targets,
		"grouped_targets": grouped_targets,
		"use_utc": use_utc_dashboard(conn),
		"page": page,
		"total_pages": total_pages,
		"total": total,
		"user": dict(user),
	})


@router.get("/ui/fragments/target-status/{target_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")  # Task 7: High-frequency status polling
def fragment_target_status(target_id: int, request: Request, conn=Depends(get_conn)):
	"""Single target status icon - requires auth."""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		return Response("<i class='bi bi-question-circle-fill status-unknown'></i>")
	status = get_simple_status(row, conn)
	return templates.TemplateResponse("fragments/target_status.html", {
		"request": request,
		"status": status,
	})


@router.get("/ui/fragments/user-info", response_class=HTMLResponse)
def fragment_user_info(request: Request, conn=Depends(get_conn)):
	"""User info badge - XSS-safe via html.escape."""
	user = resolve_user_optional(request, conn, allow_cookie=True)
	if user:
		# Escape username to prevent XSS
		safe_username = html.escape(str(user["username"]))
		return Response(
			content=f"<i class='bi bi-person-circle me-1'></i>{safe_username}",
			media_type="text/html"
		)
	return Response("")


@router.get("/ui/fragments/uptime/{target_id}", response_class=HTMLResponse)
def fragment_uptime(target_id: int, request: Request, conn=Depends(get_conn)):
	"""
	Lazy-loaded uptime percentage for a single target.
	
	This endpoint is called on-demand by the frontend via HTMX,
	avoiding expensive TSDB queries on initial page load.
	"""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		return Response("<span class='text-muted'>—</span>")
	
	cached = sqlite_db.get_target_status(conn, target_id)
	uptime = None
	if cached is not None and "uptime_percent" in cached.keys():
		uptime = cached["uptime_percent"]
	
	if uptime is None:
		return Response("<span class='text-muted'>—</span>")
	
	# Color based on uptime percentage (thresholds from constants)
	if uptime >= UPTIME_THRESHOLD_GREEN:
		css_class = "text-success"
	elif uptime >= UPTIME_THRESHOLD_YELLOW:
		css_class = "text-warning"
	else:
		css_class = "text-danger"
	
	from ..utils.formatters import format_percent
	return Response(f"<span class='{css_class}'>{format_percent(uptime)}</span>")


@router.get("/ui/fragments/availability-series/{target_id}")
async def fragment_availability_series(
	target_id: int,
	request: Request,
	conn=Depends(get_conn),
	days: int = Query(default=30, ge=1, le=365),
	limit: int = Query(default=10000, ge=100, le=50000),
):
	"""
	Return per-service availability time series (not aggregated).
	
	This endpoint provides separate up/down timelines for each enabled service
	(PING, HTTP, TCP) to allow accurate visualization without phantom downtimes
	caused by overall_status aggregation.
	
	Response format:
	{
	  "target_id": 123,
	  "series": {
	    "PING": [{"ts": "2026-02-08T10:00:00Z", "up": 1}, ...],
	    "HTTP": [{"ts": "2026-02-08T10:00:00Z", "up": 1}, ...],
	    "TCP": [{"ts": "2026-02-08T10:00:00Z", "up": 0}, ...]
	  }
	}
	"""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		raise HTTPException(status_code=404, detail="Target not found")
	
	since = datetime.now(timezone.utc) - timedelta(days=days)
	
	# Filter by currently enabled services only
	enabled_services = set()
	if row["enable_ping"]:
		enabled_services.add("PING")
	if row["enable_http_check"]:
		enabled_services.add("HTTP")
	if row["enable_tcp_connect"]:
		enabled_services.add("TCP")
	
	# Parse TCP ports from target config
	tcp_ports = None
	if row["enable_tcp_connect"] and row["tcp_ports"]:
		try:
			tcp_ports = [int(p.strip()) for p in str(row["tcp_ports"]).split(",") if p.strip().isdigit()]
		except (ValueError, AttributeError):
			tcp_ports = None
	
	series = tsdb_db.query_availability_series(
		get_tsdb_dir(request),
		target_id=target_id,
		since=since,
		until=None,
		limit=limit,
		enabled_services=enabled_services,
		tcp_ports=tcp_ports,
	)
	
	return {
		"target_id": target_id,
		"series": series,
	}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
	"""Convert SQLite Row to dict for template rendering.
	
	NOTE: Keep in sync with TargetPublic (API model) in api/targets.py
	All columns are guaranteed to exist via init_schema() + migrations.
	"""
	return {
		"id": int(row["id"]),
		"name": row["name"],
		"group": row["group_name"],
		"is_enabled": bool(row["is_enabled"]),
		"host": row["host"],
		"interval_seconds": int(row["interval_seconds"]),
		"retention_days": int(row["retention_days"]),
		"enable_ping": bool(row["enable_ping"]),
		"enable_http_check": bool(row["enable_http_check"]),
		"enable_cert_expiration": bool(row["enable_cert_expiration"]),
		"enable_tcp_connect": bool(row["enable_tcp_connect"]),
		"ping_host": row["ping_host"],
		"http_url": row["http_url"],
		"http_username": row["http_username"],
		"has_http_password": bool(row["http_password"]),  # Indicator only, never expose actual password
		"http_prefer_head": bool(row["http_prefer_head"]),
		"http_basic_auth_enabled": bool(row["http_basic_auth_enabled"]),
		"http_port": int(row["http_port"]) if row["http_port"] else None,
		"http_success_codes": str(row["http_success_codes"] or DEFAULT_HTTP_SUCCESS_CODES),
		"cert_host": row["cert_host"],
		"cert_port": int(row["cert_port"]) if row["cert_port"] else None,
		"ignore_cert_errors": bool(row["ignore_cert_errors"]),
		"tcp_host": row["tcp_host"],
		"tcp_ports": str(row["tcp_ports"] or "80"),
		"max_retries": int(row["max_retries"]),
		"auto_close_alert": bool(row["auto_close_alert"]),
		"sla_enabled": bool(row["sla_enabled"]),
		"sla_availability_pct": float(row["sla_availability_pct"]) if row["sla_availability_pct"] is not None else None,
		"sla_response_time_ms": float(row["sla_response_time_ms"]) if row["sla_response_time_ms"] is not None else None,
		"sla_ping_latency_ms": float(row["sla_ping_latency_ms"]) if row["sla_ping_latency_ms"] is not None else None,
		"created_at": row["created_at"],
		"updated_at": row["updated_at"],
	}

@router.get("/ui/fragments/downtime-history/{target_id}", response_class=HTMLResponse)
async def fragment_downtime_history(
	target_id: int,
	request: Request,
	conn=Depends(get_conn),
	days: int = Query(default=30, ge=1, le=3650),
	since: Optional[datetime] = Query(default=None),
	until: Optional[datetime] = Query(default=None),
):
	"""
	HTMX fragment: Recent downtime periods for a target.
	"""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _

	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		return HTMLResponse(_html_muted("Target not found"), status_code=404)

	# Use explicit since/until if provided, otherwise fall back to days
	if since:
		query_since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
	else:
		query_since = datetime.now(tz=timezone.utc) - timedelta(days=days)

	query_until = None
	if until:
		query_until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until

	# Calculate range label for display
	if since and until:
		range_days = (query_until - query_since).days
		if range_days <= 1:
			range_label = "Selected Range"
		elif range_days <= 7:
			range_label = f"{range_days}d"
		elif range_days <= 31:
			range_label = "30d"
		elif range_days <= 93:
			range_label = "Quarter"
		else:
			range_label = "Year"
	else:
		range_label = f"{days}d"

	# For longer ranges, increase limit
	limit = 50 if (since and until and (query_until - query_since).days > 30) else 20

	periods = await run_in_threadpool(
		tsdb_db.query_downtime_periods,
		request.app.state.tsdb_dir,
		target_id=target_id,
		since=query_since,
		until=query_until,
		limit=limit,
	)

	return templates.TemplateResponse("fragments/downtime_history.html", {
		"request": request,
		"periods": periods,
		"use_utc": use_utc_dashboard(conn),
		"range_label": range_label,
	})


@router.get("/ui/fragments/traceroute/{target_id}", response_class=HTMLResponse)
@limiter.limit("10/minute")  # Task 7: Expensive MTR operation
async def fragment_traceroute(
	target_id: int,
	request: Request,
	conn=Depends(get_conn),
):
	"""
	HTMX fragment: On-demand MTR network path visualization.
	
	Runs MTR, geolocates hops, handles route interpolation for ICMP-blocked
	segments, and returns data for Leaflet map with dashed lines.
	This is intentionally lazy-loaded since MTR is expensive.
	"""
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	row = sqlite_db.get_target(conn, target_id)
	if row is None:
		return templates.TemplateResponse("fragments/traceroute_map.html", {
			"request": request,
			"geo_points": None,
			"geo_summary": None,
			"hops": [],
			"has_interpolation": False,
			"geo_unavailable_reason": None,
			"unavailable_reason": "Target not found",
		})
	
	# Determine host for MTR (DRY: use shared helper)
	lookup_host = _resolve_target_host(row)
	
	if not lookup_host:
		return templates.TemplateResponse("fragments/traceroute_map.html", {
			"request": request,
			"geo_points": None,
			"geo_summary": None,
			"hops": [],
			"has_interpolation": False,
			"geo_unavailable_reason": None,
			"unavailable_reason": "No host configured for MTR",
		})

	# MTR utilities (imports at function level for lazy loading)
	from ..services.mtr_run import run_mtr, resolve_target_ip, analyze_route
	from ..services.traceroute_geo import (
		mtr_hops_to_geo_points,
		geolocate_ip,
		geolocate_with_hostname_hint,
		is_ipv6_address,
		shorten_ipv6_for_display,
	)
	from ..services.route_interpolator import (
		interpolate_route,
		ensure_destination_hop,
		collapse_interpolated_hops,
	)
	
	# Resolve target IP for destination matching
	target_ip = await run_in_threadpool(resolve_target_ip, lookup_host)
	
	# Keep MTR address family consistent with destination resolution (IPv4 vs IPv6)
	force_ipv6 = None
	if target_ip:
		force_ipv6 = ":" in target_ip
	elif lookup_host and ":" in lookup_host:
		# IP literal provided
		force_ipv6 = True
	
	# Run MTR in thread pool (blocking I/O)
	# Use 20 hops for international routes
	hops = await run_in_threadpool(run_mtr, lookup_host, count=3, max_hops=20, force_ipv6=force_ipv6)
	
	if not hops:
		return templates.TemplateResponse("fragments/traceroute_map.html", {
			"request": request,
			"hops": [],
			"geo_points": None,
			"geo_summary": None,
			"has_interpolation": False,
			"geo_unavailable_reason": None,
			"unavailable_reason": "MTR returned no hops",
		})
	
	# Analyze route for ICMP blocking patterns
	route_analysis = analyze_route(hops, target_ip)
	
	# Get destination geo data for interpolation
	destination_geo = None
	if target_ip:
		destination_geo = await run_in_threadpool(geolocate_ip, target_ip)
	
	# Apply route interpolation for blocked segments
	interpolated_hops = interpolate_route(
		hops,
		destination_geo=destination_geo,
	)
	
	# Ensure destination is in hop list (synthetic if needed)
	if target_ip and not route_analysis["destination_reached"]:
		# Get final latency from last known hop if available
		final_latency = None
		if route_analysis["last_responding_hop"] > 0:
			for h in hops:
				if h.get("hop") == route_analysis["last_responding_hop"]:
					lat = h.get("latency_ms")
					if lat and isinstance(lat, dict):
						final_latency = lat.get("avg")
					break
		
		interpolated_hops = ensure_destination_hop(
			interpolated_hops,
			destination_ip=target_ip,
			destination_hostname=lookup_host,
			destination_geo=destination_geo,
			final_latency_ms=final_latency,
		)
	
	# Generate geo points for map
	geo_points = mtr_hops_to_geo_points(
		interpolated_hops,
		destination_ip=target_ip,
		destination_geo=destination_geo,
	)
	
	# Merge geo data back into hops for display
	# Note: geo_points are deduplicated for the map, but we need geo per hop for the table
	geo_by_ip = {}
	for p in geo_points:
		geo_by_ip.setdefault(p["ip"], p)
	
	enriched_hops = []
	for h in interpolated_hops:
		hop_data = dict(h)
		geo = geo_by_ip.get(h.get("ip"))
		
		# If not in geo_points (deduplicated), resolve directly
		if not geo and h.get("ip") and h.get("ip") != "*":
			geo = geolocate_with_hostname_hint(h.get("ip"), h.get("hostname"))
		
		if geo:
			hop_data["lat"] = geo.get("lat")
			hop_data["lon"] = geo.get("lon")
			hop_data["city"] = geo.get("city")
			hop_data["country"] = geo.get("country")
		
		# Add display fields for template
		# IPv6 OPTIMIZATION: Shorten long IPv6 addresses for table display
		ip_raw = hop_data.get("ip", "—")
		hop_data["ip_full"] = ip_raw  # Store full IP for tooltips
		hop_data["ip_display"] = shorten_ipv6_for_display(ip_raw) if ip_raw != "—" else "—"
		hop_data["is_ipv6"] = is_ipv6_address(ip_raw)
		hop_data["status_display"] = hop_data.get("status", "timeout")
		
		# Format latency for display
		lat = hop_data.get("latency_ms")
		if lat:
			if isinstance(lat, dict):
				avg = lat.get("avg")
				hop_data["latency_display"] = f"{avg:.1f} ms" if avg is not None else "–"
			elif isinstance(lat, (int, float)):
				hop_data["latency_display"] = f"{lat:.1f} ms"
			else:
				hop_data["latency_display"] = "–"
		else:
			hop_data["latency_display"] = "–"
		
		# Format loss for display
		loss = hop_data.get("loss_pct")
		if loss is not None and loss < 100:
			hop_data["loss_display"] = f"{loss:.0f}%"
		else:
			hop_data["loss_display"] = "–"
		
		enriched_hops.append(hop_data)
	
	# Strip trailing timeouts (keep interpolated hops)
	while (
		enriched_hops
		and enriched_hops[-1].get("status") == "timeout"
		and not enriched_hops[-1].get("_interpolated")
	):
		enriched_hops.pop()
	
	# Collapse consecutive interpolated hops for cleaner table
	display_hops = collapse_interpolated_hops(enriched_hops)
	
	# Check if any interpolation was applied
	has_interpolation = any(
		h.get("status") == "interpolated" or h.get("_interpolated")
		for h in enriched_hops
	)
	
	# Build summary info
	geo_summary = None
	geo_unavailable_reason = None

	if geo_points:
		if len(geo_points) >= 2:
			first = geo_points[0]
			last = geo_points[-1]
			geo_summary = {
				"source_city": first.get("city"),
				"source_country": first.get("country"),
				"dest_city": last.get("city"),
				"dest_country": last.get("country"),
				"hops": len(enriched_hops),
			}
		else:
			# Single-hop: render marker only
			geo_summary = {
				"dest_city": geo_points[0].get("city"),
				"dest_country": geo_points[0].get("country"),
				"hops": len(enriched_hops),
			}
	elif enriched_hops:
		geo_summary = {"hops": len(enriched_hops)}

	if not geo_points:
		geo_unavailable_reason = "No public geolocated hops available"
	elif len(geo_points) == 1:
		geo_unavailable_reason = "All hops resolve to same location (no route path)"
	
	return templates.TemplateResponse("fragments/traceroute_map.html", {
		"request": request,
		"hops": display_hops,
		"geo_points": geo_points,
		"geo_summary": geo_summary,
		"has_interpolation": has_interpolation,
		"geo_unavailable_reason": geo_unavailable_reason,
		"unavailable_reason": None,
	})


# ─── Signal Registration UI Fragments ───────────────────────────────────────────

@router.get("/ui/fragments/signal-qr", response_class=HTMLResponse)
def fragment_signal_qr(request: Request, conn=Depends(get_conn)):
	"""Render Signal QR code for device linking.
	
	Uses signal_cli_backend directly (local subprocess, no HTTP).
	"""
	import base64
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalUnavailable, SignalTimeout
	
	user = require_admin_htmx(request, conn)
	if isinstance(user, Response):
		return user
	
	try:
		backend = get_signal_backend()
		png_bytes = backend.get_link_qr(device_name="justUp")
		
		if not png_bytes:
			return HTMLResponse('''
				<div class="text-muted small text-center py-2">
					<span class="material-icons align-middle me-1" style="font-size: 16px;">info</span>
					QR linking not available
				</div>
			''')
		
		qr_b64 = base64.b64encode(png_bytes).decode("ascii")
		
		return HTMLResponse(f'''
			<div class="text-center">
				<img src="data:image/png;base64,{qr_b64}" 
				     style="max-width: 280px; border-radius: 8px;" 
				     alt="Signal QR Code">
				<p class="text-muted small mt-2">
					<span class="material-icons align-middle me-1" style="font-size: 16px;">phone_android</span>
					Scan with Signal app to link device
				</p>
			</div>
		''')
	except (SignalUnavailable, SignalTimeout) as e:
		_log.debug("QR fragment error: %s", e)
		return HTMLResponse(_html_error("signal-cli unavailable"))
	except Exception as e:
		_log.debug("QR fragment error: %s", e)
		return HTMLResponse(_html_error("QR not available"))


@router.get("/ui/fragments/signal-link-status", response_class=HTMLResponse)
def fragment_signal_link_status(request: Request, conn=Depends(get_conn)):
	"""Render Signal device link status badge.
	
	Uses signal_cli_backend directly (local subprocess, no HTTP).
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalUnavailable
	
	_ = resolve_user_or_htmx_redirect(request, conn)
	if isinstance(_, Response):
		return _
	
	try:
		backend = get_signal_backend()
		status = backend.status()
		
		if not status.reachable:
			return HTMLResponse(_html_badge("Status unavailable"))
		
		if status.linked:
			count = len(status.accounts)
			return HTMLResponse(f'''
				<span class="badge bg-success">
					<span class="material-icons me-1">link</span>Linked ({count} account{"s" if count != 1 else ""})
				</span>
			''')
		else:
			return HTMLResponse('''
				<span class="badge bg-secondary">
					<span class="material-icons me-1">link_off</span>Not Linked
				</span>
			''')
	except SignalUnavailable:
		return HTMLResponse(_html_badge("signal-cli unavailable"))
	except Exception as e:
		_log.warning("Failed to get Signal link status: %s", e)
		return HTMLResponse(_html_badge("Status unavailable"))


@router.get("/ui/fragments/signal-status", response_class=HTMLResponse)
def fragment_signal_status(request: Request, conn=Depends(get_conn)):
	"""
	Render comprehensive Signal status card.
	
	Uses signal_cli_backend directly (local subprocess, no HTTP).
	Shows: reachable, registered, linked
	"""
	from ..services.notifications.signal_backend import get_signal_backend
	from ..services.notifications.signal_errors import SignalUnavailable
	
	user = require_admin_htmx(request, conn)
	if isinstance(user, Response):
		return user
	
	# Get status directly from backend
	try:
		backend = get_signal_backend()
		status = backend.status()
	except SignalUnavailable as e:
		return HTMLResponse(f'''
			<div class="d-flex align-items-center text-danger">
				<span class="material-icons me-2">cloud_off</span>
				<span>{html.escape(str(e))}</span>
			</div>
		''')
	except Exception as e:
		_log.warning("Signal status check failed: %s", e)
		return HTMLResponse('''
			<div class="d-flex align-items-center text-danger">
				<span class="material-icons me-2">cloud_off</span>
				<span>Status check failed</span>
			</div>
		''')
	
	if not status.reachable:
		return HTMLResponse('''
			<div class="d-flex align-items-center text-danger">
				<span class="material-icons me-2">cloud_off</span>
				<span>signal-cli not available</span>
			</div>
		''')
	
	# Build status badges from SignalStatus dataclass
	badges = []
	badges.append('''<span class="badge bg-success me-1">
		<span class="material-icons align-middle me-1" style="font-size: 14px;">cloud_done</span>Reachable
	</span>''')
	
	if status.registered:
		badges.append('''<span class="badge bg-success me-1">
			<span class="material-icons align-middle me-1" style="font-size: 14px;">verified_user</span>Registered
		</span>''')
	else:
		badges.append('''<span class="badge bg-warning text-dark me-1">
			<span class="material-icons align-middle me-1" style="font-size: 14px;">person_off</span>Not Registered
		</span>''')
	
	if status.linked:
		badges.append('''<span class="badge bg-success me-1">
			<span class="material-icons align-middle me-1" style="font-size: 14px;">link</span>Linked
		</span>''')
	else:
		badges.append('''<span class="badge bg-secondary me-1">
			<span class="material-icons align-middle me-1" style="font-size: 14px;">link_off</span>Not Linked
		</span>''')
	
	# Account info from SignalStatus
	accounts = status.accounts
	acct_list = ', '.join(html.escape(str(a)) for a in accounts[:3]) + ('...' if len(accounts) > 3 else '')
	acct_info = f'<small class="text-muted d-block mt-1">Accounts: {acct_list}</small>' if accounts else ''
	
	return HTMLResponse(f'''
		<div>
			{''.join(badges)}
			{acct_info}
		</div>
	''')


@router.get("/ui/fragments/signal-mobile-check", response_class=HTMLResponse)
def fragment_signal_mobile_check(request: Request, conn=Depends(get_conn)):
	"""
	Check if the request is from a mobile device and render appropriate UI state.
	
	Returns JSON-like attributes for the register button:
	- data-is-mobile="true|false"
	- Updates button state (enabled/disabled) based on device type
	- In DEBUG mode (LOG_LEVEL=DEBUG), registration is also enabled on desktop
	"""
	user = require_admin_htmx(request, conn)
	if isinstance(user, Response):
		return user

	# Always allow registration, regardless of device type or debug mode
	return HTMLResponse('''
		<button type="button" class="btn btn-outline-secondary text-start" 
			id="signal-mode-register-btn" onclick="signalSelectMode('register')"
			data-is-mobile="true"
			data-debug-mode="true">
			<span class="d-flex align-items-center">
				<span class="material-icons me-2">phone</span>
				<span>
					<strong>Register a new number</strong>
					<small class="d-block opacity-75">For dedicated notification number</small>
				</span>
			</span>
		</button>
	''')
