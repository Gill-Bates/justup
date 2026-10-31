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

import hashlib
import html
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import markdown
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from ..db import notifications as inbox
from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..models.targets import target_row_to_dict
from ..monitor.quality_probe import DEFAULT_QUALITY_TARGETS
from ..utils.auth_helpers import (
    is_password_change_exempt_path,
    is_password_change_pending,
    resolve_user_optional,
    resolve_user_or_htmx_redirect,
)
from ..utils.constants import (
    DATE_FORMAT_ISO,
    DISPLAY_UNGROUPED_EN,
    MONITORING_LIMITS,
    QUALITY_MIN_TARGETS,
    UI_PAGE_SIZE_ALERTS,
    UI_PAGE_SIZE_DEFAULT,
    UI_PAGE_SIZE_TARGETS,
    UPTIME_THRESHOLD_GREEN,
    UPTIME_THRESHOLD_YELLOW,
    CheckStatus,
)
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.formatters import format_compact_count, format_percent, row_to_alert_dict
from ..utils.rate_limit import limiter
from ..utils.runtime_fields import (
    QUALITY_TARGET_MAX_LENGTH,
    QUALITY_TARGET_PATTERN,
    load_runtime_fields,
)
from ..utils.settings_cache import (
    get_pdf_page_size,
    use_utc_dashboard,
)
from ..utils.status_helper import (
    build_target_status,
    get_simple_status,
    get_status_defaults,
)
from ..utils.storage_stats import compute_storage_stats
from ..utils.time import assume_utc
from ..utils.uptime_metric import select_uptime_metric
from ..utils.version import BUILD_INFO, VERSION
from . import notifications as notifications_api

router = APIRouter(tags=["frontend"])

# Templates
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_TRACEROUTE_CACHE_TTL_SECONDS = 5 * 60
_TRACEROUTE_CACHE_MAX_ENTRIES = 128
_traceroute_fragment_cache: OrderedDict[tuple[str, int, str], tuple[float, bytes]] = OrderedDict()
_traceroute_fragment_cache_lock = threading.Lock()


def _get_cached_traceroute_fragment(cache_namespace: str, target_id: int, host: str) -> bytes | None:
    key = (cache_namespace, target_id, host)
    now = time.monotonic()
    with _traceroute_fragment_cache_lock:
        entry = _traceroute_fragment_cache.get(key)
        if entry is None:
            return None
        expires_at, body = entry
        if now >= expires_at:
            _traceroute_fragment_cache.pop(key, None)
            return None
        _traceroute_fragment_cache.move_to_end(key)
        return body


def _cache_traceroute_fragment(cache_namespace: str, target_id: int, host: str, body: bytes) -> None:
    key = (cache_namespace, target_id, host)
    with _traceroute_fragment_cache_lock:
        _traceroute_fragment_cache[key] = (time.monotonic() + _TRACEROUTE_CACHE_TTL_SECONDS, body)
        _traceroute_fragment_cache.move_to_end(key)
        while len(_traceroute_fragment_cache) > _TRACEROUTE_CACHE_MAX_ENTRIES:
            _traceroute_fragment_cache.popitem(last=False)


# Explicit "Run again": one measurement per key at a time, plus a short cooldown after it.
_TRACEROUTE_REFRESH_COOLDOWN_SECONDS = 15.0
_TRACEROUTE_REFRESH_STATE_MAX = 256
_traceroute_refresh_lock = threading.Lock()
_traceroute_refresh_inflight: set[tuple[str, int, str]] = set()
_traceroute_refresh_finished: OrderedDict[tuple[str, int, str], float] = OrderedDict()


def _begin_traceroute_refresh(key: tuple[str, int, str]) -> float | None:
    """Reserve a forced measurement; return ``None`` when allowed, else seconds to wait."""
    now = time.monotonic()
    with _traceroute_refresh_lock:
        if key in _traceroute_refresh_inflight:
            return _TRACEROUTE_REFRESH_COOLDOWN_SECONDS
        finished = _traceroute_refresh_finished.get(key)
        if finished is not None and now - finished < _TRACEROUTE_REFRESH_COOLDOWN_SECONDS:
            return _TRACEROUTE_REFRESH_COOLDOWN_SECONDS - (now - finished)
        _traceroute_refresh_inflight.add(key)
        return None


def _end_traceroute_refresh(key: tuple[str, int, str]) -> None:
    with _traceroute_refresh_lock:
        _traceroute_refresh_inflight.discard(key)
        _traceroute_refresh_finished[key] = time.monotonic()
        _traceroute_refresh_finished.move_to_end(key)
        while len(_traceroute_refresh_finished) > _TRACEROUTE_REFRESH_STATE_MAX:
            _traceroute_refresh_finished.popitem(last=False)


def _logical_hop_count(display_hops: list[dict]) -> int:
    """Count route hops, expanding collapsed ICMP-blocked rows into the hops they stand for."""
    total = 0
    for hop in display_hops:
        raw = hop.get("_raw_hops")
        total += len(raw) if hop.get("_collapsed") and raw else 1
    return total


def _format_percent_value(value: float) -> str:
    return f"{round(value, 1):g}%"


def _destination_kpis(
    display_hops: list[dict],
    target_ip: str | None,
    destination_geo_point: dict | None,
) -> dict:
    """Derive the KPI row from measured data only; unknown values stay ``None`` (never 0)."""
    destination = next(
        (
            hop
            for hop in reversed(display_hops)
            if not hop.get("_collapsed")
            and (
                bool(target_ip and hop.get("ip") == target_ip)
                or hop.get("_destination")
                or hop.get("_synthetic_destination")
            )
        ),
        None,
    )
    measured = destination if destination is not None and not destination.get("_synthetic_destination") else None

    latency_ms = None
    loss_pct = None
    if measured is not None:
        latency = measured.get("latency_ms")
        raw_latency = latency.get("avg") if isinstance(latency, dict) else latency
        if isinstance(raw_latency, (int, float)) and not isinstance(raw_latency, bool):
            latency_ms = float(raw_latency)
        raw_loss = measured.get("loss_pct")
        if isinstance(raw_loss, (int, float)) and not isinstance(raw_loss, bool):
            loss_pct = float(raw_loss)

    place = destination_geo_point or destination or {}
    city = place.get("city") or (destination or {}).get("city")
    country = place.get("country") or (destination or {}).get("country")
    return {
        "hop_count": _logical_hop_count(display_hops),
        "destination_found": destination is not None,
        "destination_reached": measured is not None,
        "latency_display": f"{latency_ms:.1f} ms" if latency_ms is not None else None,
        "loss_display": _format_percent_value(loss_pct) if loss_pct is not None else None,
        "loss_pct": loss_pct,
        "city": city or None,
        "country": country or None,
    }


# Add version to all template contexts
templates.env.globals["version"] = f"v{VERSION}"
templates.env.globals["build_info"] = BUILD_INFO
templates.env.globals["now"] = lambda: datetime.now(UTC)

# Add format_percent filter for consistent percentage formatting
templates.env.filters["format_percent"] = format_percent
templates.env.filters["format_compact_count"] = format_compact_count


# Add regex_replace filter for slugifying group IDs
def _regex_replace(value: str, pattern: str, replacement: str) -> str:
    """Replace regex pattern in string."""
    return re.sub(pattern, replacement, value)


templates.env.filters["regex_replace"] = _regex_replace


def _group_dom_id(value: str) -> str:
    """Readable slug plus a name hash, so names that slug alike keep distinct DOM ids."""
    slug = re.sub(r"[^a-z0-9_]", "", str(value).lower().replace(" ", "_")) or "group"
    return f"{slug}-{hashlib.sha1(str(value).encode('utf-8'), usedforsecurity=False).hexdigest()[:8]}"


templates.env.filters["group_dom_id"] = _group_dom_id


# Add markdown filter for rendering markdown to HTML
def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br", "md_in_html"], output_format="html")


templates.env.filters["markdown"] = _render_markdown

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
    except Exception:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
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
        except ValueError:
            pass

    return _row_get(row, "ping_host") or _row_get(row, "host") or _row_get(row, "cert_host")


def require_ui_user(request: Request, conn) -> dict | Response:
    """Helper to resolve UI user or return redirect response."""
    user = resolve_user_optional(request, conn, allow_cookie=True)
    if not user:
        return RedirectResponse("/ui/login", status_code=302)
    if is_password_change_pending(user) and not is_password_change_exempt_path(request.url.path):
        return RedirectResponse("/ui/change-password", status_code=302)
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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_page": "dashboard",
            "user": user,
        },
    )


@router.get("/ui/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Render the login page."""
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/ui/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, conn=Depends(get_conn)):
    """Render the mandatory password change page (first login or admin reset)."""
    user = resolve_user_optional(request, conn, allow_cookie=True)
    if not user:
        return RedirectResponse("/ui/login", status_code=302)
    if not is_password_change_pending(user):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "change_password.html", {"user": dict(user)})


@router.get("/ui/targets", response_class=HTMLResponse)
def targets_page(request: Request, conn=Depends(get_conn)):
    """Render the targets list page."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user
    return templates.TemplateResponse(
        request,
        "targets.html",
        {
            "active_page": "targets",
            "user": user,
        },
    )


@router.get("/ui/targets/new", response_class=HTMLResponse)
def target_new_page(request: Request, conn=Depends(get_conn)):
    """Render the target creation wizard."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    groups = [dict(row) for row in sqlite_db.list_groups(conn)]
    return templates.TemplateResponse(
        request,
        "target_wizard.html",
        {
            "active_page": "targets",
            "user": user,
            "groups": groups,
            "limits": MONITORING_LIMITS,
        },
    )


def _target_detail_view(conn, row, target_id: int) -> dict:
    """Target row merged with its cached status, as the detail page and its KPI fragment render it."""
    target = _row_to_dict(row)
    cached = sqlite_db.get_target_status(conn, target_id)
    if cached:
        target.update(build_target_status(row, cached))
        target["overall_status"] = cached["overall_status"] or "unknown"
    else:
        target.update(get_status_defaults())
        target["overall_status"] = "unknown" if bool(row["is_enabled"]) else "disabled"
    return target


@router.get("/ui/fragments/target-info-cards/{target_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")
def fragment_target_info_cards(target_id: int, request: Request, conn=Depends(get_conn)):
    """KPI cards (uptime, check badges, interval) for live refresh of the target detail page."""
    user = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(user, Response):
        return user
    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    response = templates.TemplateResponse(
        request,
        "fragments/target_detail/info_cards.html",
        {"user": user, "target": _target_detail_view(conn, row, target_id)},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/ui/targets/{target_id}", response_class=HTMLResponse)
def target_detail_page(target_id: int, request: Request, conn=Depends(get_conn)):
    """Render the target detail page."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user
    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    target = _target_detail_view(conn, row, target_id)

    return templates.TemplateResponse(
        request,
        "target_detail.html",
        {
            "active_page": "targets",
            "user": user,
            "target": target,
            "use_utc": use_utc_dashboard(conn),
            "pdf_page_size": get_pdf_page_size(conn),
            "has_recipients": len(sqlite_db.get_target_recipient_ids(conn, target_id)) > 0,
            "alert_recipients": [dict(row) for row in sqlite_db.get_target_recipients(conn, target_id)],
        },
    )


@router.get("/ui/targets/{target_id}/edit", response_class=HTMLResponse)
def target_edit_page(target_id: int, request: Request, conn=Depends(get_conn)):
    """Render the edit-target form."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    target = _row_to_dict(row)
    groups = [dict(row) for row in sqlite_db.list_groups(conn)]
    return templates.TemplateResponse(
        request,
        "target_form.html",
        {
            "active_page": "targets",
            "user": user,
            "target": target,
            "groups": groups,
            "limits": MONITORING_LIMITS,
        },
    )


@router.get("/ui/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(get_conn)):
    """Render the settings page."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user

    # Only admins can access settings (return 403 per docstring)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")

    # Sender number is a DB setting (same source as dispatch and settings API)
    sender_number = sqlite_db.get_setting(conn, "signal_api_sender_number", None)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_page": "settings",
            "user": user,
            "signal_sender_number": sender_number,
        },
    )


# ─── Settings Tab Fragments (HTMX lazy loading) ─────────────────────────────────


@router.get("/ui/fragments/settings-general", response_class=HTMLResponse)
def fragment_settings_general(request: Request, conn=Depends(get_conn)):
    """Render General settings tab fragment with pre-populated values."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Load general settings from database
    general_data = {
        "use_utc_dashboard": sqlite_db.get_setting(conn, "use_utc_dashboard", False),
        "pdf_page_size": sqlite_db.get_setting(conn, "pdf_page_size", "a4") or "a4",
        "metrics_timeout_minutes": sqlite_db.get_setting(conn, "metrics_timeout_minutes", 5) or 5,
    }

    return templates.TemplateResponse(
        request,
        "settings/general.html",
        {
            "current_user": user,
            "settings": general_data,
        },
    )


@router.get("/ui/fragments/settings-network", response_class=HTMLResponse)
def fragment_settings_network(request: Request, conn=Depends(get_conn)):
    """Render network settings as one tab with separate quality, provider, and geo areas."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    return templates.TemplateResponse(
        request,
        "settings/network.html",
        {
            "current_user": user,
            "settings": {
                "geoip_auto_update": sqlite_db.get_setting(conn, "geoip_auto_update", True),
                **load_runtime_fields(conn),
            },
            "quality_default_targets": DEFAULT_QUALITY_TARGETS,
            "quality_target_pattern": QUALITY_TARGET_PATTERN,
            "quality_target_max_length": QUALITY_TARGET_MAX_LENGTH,
            "quality_min_targets": QUALITY_MIN_TARGETS,
        },
    )


@router.get("/ui/fragments/settings-storage", response_class=HTMLResponse)
async def fragment_settings_storage(request: Request, conn=Depends(get_conn)):
    """Render Storage settings tab fragment with pre-computed stats."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Compute storage stats in threadpool (blocking I/O)
    db_path = request.app.state.db_path
    tsdb_dir = get_tsdb_dir(request)
    stats = await run_in_threadpool(compute_storage_stats, db_path, tsdb_dir)

    sql_size_bytes = stats["sql_size_bytes"]
    tsdb_size_bytes = stats["tsdb_size_bytes"]
    total_points = stats["total_points"]

    # Format for display
    def format_bytes(b):
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        if b < 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024):.1f} MB"
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

    def format_number(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}k"
        return str(n)

    return templates.TemplateResponse(
        request,
        "settings/storage.html",
        {
            "current_user": user,
            "settings": {
                "purge_enabled": sqlite_db.get_setting(conn, "purge_enabled", True),
            },
            "storage": {
                "sql_size": format_bytes(sql_size_bytes),
                "tsdb_size": format_bytes(tsdb_size_bytes),
                "total_points": format_number(total_points),
                "tsdb_saved": format_bytes(stats["tsdb_saved_bytes"]) if stats["tsdb_saved_bytes"] else "",
            },
        },
    )


@router.get("/ui/fragments/settings-backup", response_class=HTMLResponse)
def fragment_settings_backup(request: Request, conn=Depends(get_conn)):
    """Render Backup settings tab fragment."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user
    return templates.TemplateResponse(
        request,
        "settings/backup.html",
        {
            "current_user": user,
        },
    )


@router.get("/ui/fragments/settings-smtp", response_class=HTMLResponse)
def fragment_settings_smtp(request: Request, conn=Depends(get_conn)):
    """Render SMTP settings tab fragment with pre-populated values."""
    from ..utils.constants import SettingKeys

    user = require_admin_htmx(request, conn)
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
    from ..services.notifications.email_template import load_template_preview

    template_data = load_template_preview(conn)

    return templates.TemplateResponse(
        request,
        "settings/smtp.html",
        {
            "current_user": user,
            "smtp": smtp_data,
            "template": template_data,
        },
    )


@router.get("/ui/fragments/settings-users", response_class=HTMLResponse)
def fragment_settings_users(request: Request, conn=Depends(get_conn)):
    """Render Users settings tab fragment."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user
    return templates.TemplateResponse(
        request,
        "settings/users.html",
        {
            "current_user": user,
            "settings": {
                "auth_disabled": sqlite_db.get_setting(conn, "auth_disabled", False),
                "session_max_days": sqlite_db.get_session_max_days(conn),
                **load_runtime_fields(conn),
            },
        },
    )


@router.get("/ui/fragments/settings-recipients", response_class=HTMLResponse)
def fragment_settings_recipients(request: Request, conn=Depends(get_conn)):
    """Render Recipients settings tab fragment."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user
    return templates.TemplateResponse(
        request,
        "settings/recipients.html",
        {
            "current_user": user,
        },
    )


@router.get("/ui/fragments/settings-spooler", response_class=HTMLResponse)
def fragment_settings_spooler(request: Request, conn=Depends(get_conn)):
    """Render Spooler settings tab fragment."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user
    return templates.TemplateResponse(
        request,
        "settings/spooler.html",
        {
            "current_user": user,
        },
    )


@router.get("/ui/fragments/settings-signal", response_class=HTMLResponse)
def fragment_settings_signal(request: Request, conn=Depends(get_conn)):
    """Render Signal settings tab fragment."""
    from ..services.notifications.signal_template import (
        DEFAULT_SIGNAL_TEMPLATE_DOWN,
        DEFAULT_SIGNAL_TEMPLATE_UP,
    )
    from ..utils.constants import SettingKeys

    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Sender number is a DB setting (same source as dispatch and settings API)
    sender_number = sqlite_db.get_setting(conn, "signal_api_sender_number", None)

    # Get signal_enabled setting
    signal_enabled = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)

    # Load Signal message templates (fallback to defaults)
    signal_template = {
        "down": sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, None)
        or DEFAULT_SIGNAL_TEMPLATE_DOWN,
        "up": sqlite_db.get_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, None) or DEFAULT_SIGNAL_TEMPLATE_UP,
    }

    return templates.TemplateResponse(
        request,
        "settings/signal.html",
        {
            "current_user": user,
            "signal_sender_number": sender_number,
            "signal_enabled": signal_enabled,
            "signal_template": signal_template,
        },
    )


@router.get("/ui/about", response_class=HTMLResponse)
def about_page(request: Request, conn=Depends(get_conn)):
    """Render the about page with version, dependencies, and license."""
    import sys

    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user

    # Get Python version
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Key dependencies with versions (declared in pyproject.toml, sorted alphabetically)
    key_packages = [
        "cryptography",
        "dnspython",
        "email-validator",
        "fastapi",
        "geoip2",
        "httpx",
        "jinja2",
        "markdown",
        "matplotlib",
        "pillow",
        "pydantic",
        "python-multipart",
        "qrcode",
        "reportlab",
        "slowapi",
        "sslyze",
        "starlette",
        "uvicorn",
    ]

    # Read versions from installed package metadata (pyproject.toml is the
    # single manifest; the packages are installed in Docker and dev alike).
    from importlib.metadata import version as get_pkg_version

    dependencies = []
    for pkg_name in key_packages:
        try:
            ver = get_pkg_version(pkg_name)
            dependencies.append((pkg_name, ver))
        except Exception:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
            dependencies.append((pkg_name, "?"))

    # Add signal-cli version (external binary, not a Python package)
    try:
        import subprocess

        result = subprocess.run(
            ["signal-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output format: "signal-cli 0.13.23"
            version_str = result.stdout.strip().split()[-1]  # Get version number
            dependencies.append(("signal-cli", version_str))
        else:
            dependencies.append(("signal-cli", "not found"))
    except Exception:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
        dependencies.append(("signal-cli", "not available"))

    # Vendored Leaflet (web map renderer): version from its @preserve banner
    leaflet_version = "?"
    try:
        leaflet_js = Path(__file__).resolve().parent.parent / "static" / "js" / "leaflet.js"
        with leaflet_js.open(encoding="utf-8") as fh:
            match = re.search(r"Leaflet (\d+\.\d+\.\d+)", fh.read(1024))
        if match:
            leaflet_version = match.group(1)
    except OSError:
        _log.debug("leaflet.js not readable for About page", exc_info=True)
    dependencies.append(("leaflet", leaflet_version))

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
            except OSError:
                pass

    if not license_text:
        license_text = "License file not found."

    # Short license label for the About page: the first non-empty line (e.g. "MIT License").
    license_name = next((line.strip() for line in license_text.splitlines() if line.strip()), "Unknown")

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
            except OSError:
                pass

    if not changelog_text:
        changelog_text = "Changelog not found."

    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "active_page": "about",
            "user": user,
            "version": VERSION,
            "build_info": BUILD_INFO,
            "python_version": python_version,
            "geoip_build_info": _get_geoip_build_info(),
            "dependencies": dependencies,
            "license_text": license_text,
            "license_name": license_name,
            "changelog_text": changelog_text,
        },
    )


@router.get("/ui/groups", response_class=HTMLResponse)
def groups_page(request: Request, conn=Depends(get_conn)):
    """Render the groups page."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user

    groups_rows = sqlite_db.list_groups_with_counts(conn)
    groups = [dict(row) for row in groups_rows]

    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "active_page": "groups",
            "user": user,
            "groups": groups,
        },
    )


def _alerts_panel_context(
    conn,
    *,
    target_id: str | None,
    status: str | None,
    from_date: str | None,
    to_date: str | None,
    page: int,
) -> dict:
    """Template context of the monitoring alert management (filters, history, acknowledge)."""
    # Normalize empty strings to None
    status = status if status and status.strip() else None
    from_date = from_date if from_date and from_date.strip() else None
    to_date = to_date if to_date and to_date.strip() else None

    # Parse target_id (form sends "" for "All targets")
    target_id_int: int | None = None
    if target_id and target_id.strip():
        try:
            target_id_int = int(target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target_id") from None
        if target_id_int < 1:
            raise HTTPException(status_code=400, detail="target_id must be >= 1")

    # Parse dates - return 400 for invalid input (per docstring)
    from_dt = None
    to_dt = None
    if from_date:
        try:
            from_dt = datetime.strptime(from_date, DATE_FORMAT_ISO).replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid from_date format: {from_date} (expected YYYY-MM-DD)"
            ) from None
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, DATE_FORMAT_ISO).replace(hour=23, minute=59, second=59, tzinfo=UTC)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid to_date format: {to_date} (expected YYYY-MM-DD)"
            ) from None

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
    query_string = urlencode(
        {
            k: v
            for k, v in {
                "target_id": target_id_int,
                "status": status,
                "from_date": from_date,
                "to_date": to_date,
                "category": "monitoring",
            }.items()
            if v is not None
        }
    )

    return {
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
    }


NOTIFICATION_TABS = (
    ("all", "All", "inbox"),
    ("unread", "Unread", "mark_email_unread"),
    ("monitoring", "Monitoring", "monitor_heart"),
    ("reports", "Reports", "picture_as_pdf"),
    ("delivery", "Delivery", "forward_to_inbox"),
    ("security", "Security", "shield"),
    ("activity", "Activity", "history"),
)
_NOTIFICATION_TAB_KEYS = {key for key, _label, _icon in NOTIFICATION_TABS}


@router.get("/ui/alerts", response_class=HTMLResponse)
def alerts_redirect(request: Request):
    """Legacy URL: the alert management now lives on the Notifications page (Monitoring tab)."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "category"]
    query = urlencode([("category", "monitoring"), *params])
    return RedirectResponse(f"/ui/notifications?{query}", status_code=302)


@router.get("/ui/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    category: str = Query("all"),
    severity: str | None = Query(None),
    target_id: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    conn=Depends(get_conn),
):
    """Notification center: inbox tabs plus the monitoring alert management."""
    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user

    category = category if category in _NOTIFICATION_TAB_KEYS else "all"
    severity = severity if severity in inbox.SEVERITIES else None
    context: dict = {
        "active_page": "notifications",
        "user": user,
        "tabs": NOTIFICATION_TABS,
        "category": category,
        "severity": severity or "",
        "unread_total": inbox.count_unread(conn, user),
        "can_close_alerts": bool(user.get("is_admin") or user.get("can_close_alerts")),
    }
    if category == "monitoring":
        context.update(
            _alerts_panel_context(
                conn, target_id=target_id, status=status, from_date=from_date, to_date=to_date, page=page
            )
        )
        context["monitoring_unread"] = inbox.count_unread(conn, user, category="monitoring")
    else:
        page_size = UI_PAGE_SIZE_DEFAULT
        items, total = inbox.list_notifications(
            conn,
            user,
            category=None if category in ("all", "unread") else category,
            unread_only=category == "unread",
            severity=severity,
            page=page,
            page_size=page_size,
        )
        context.update(
            {
                "items": notifications_api.decorate_items(conn, user, items),
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "query_string": urlencode({k: v for k, v in {"category": category, "severity": severity}.items() if v}),
            }
        )
    response = templates.TemplateResponse(request, "notifications.html", context)
    response.headers["Cache-Control"] = "private, no-store"
    return response


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
    return templates.TemplateResponse(
        request,
        "fragments/status_summary.html",
        {
            "stats": stats,
        },
    )


@router.get("/ui/fragments/targets-table", response_class=HTMLResponse)
@limiter.limit("30/minute")  # Task 7: Table refresh
def fragment_targets_table(request: Request, page: int = Query(1, ge=1), conn=Depends(get_conn)):
    """Targets table - requires auth."""
    user = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(user, Response):
        return user

    page_size = UI_PAGE_SIZE_TARGETS
    rows, total = sqlite_db.list_targets_paginated(conn, page=page, page_size=page_size)
    # Polling keeps the current page; fall back to the last one if it vanished (deleted targets).
    last_page = max(1, (total + page_size - 1) // page_size)
    if page > last_page:
        page = last_page
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

    return templates.TemplateResponse(
        request,
        "fragments/targets_table.html",
        {
            "targets": targets,
            "grouped_targets": grouped_targets,
            "use_utc": use_utc_dashboard(conn),
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "user": dict(user),
        },
    )


@router.get("/ui/fragments/target-status/{target_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")  # Task 7: High-frequency status polling
def fragment_target_status(target_id: int, request: Request, labelled: bool = False, conn=Depends(get_conn)):
    """Single target status icon (or labelled pill with ``labelled=1``) - requires auth."""
    _ = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(_, Response):
        return _

    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        return Response("<i class='bi bi-question-circle-fill status-unknown'></i>")
    status = get_simple_status(row, conn)
    return templates.TemplateResponse(
        request,
        "fragments/target_status.html",
        {
            "status": status,
            "labelled": labelled,
        },
    )


@router.get("/ui/fragments/user-info", response_class=HTMLResponse)
def fragment_user_info(request: Request, conn=Depends(get_conn)):
    """User info badge - XSS-safe via html.escape."""
    user = resolve_user_optional(request, conn, allow_cookie=True)
    if user:
        # Escape username to prevent XSS
        safe_username = html.escape(str(user["username"]))
        return Response(content=f"<i class='bi bi-person-circle me-1'></i>{safe_username}", media_type="text/html")
    return Response("")


@router.get("/ui/fragments/uptime/{target_id}", response_class=HTMLResponse)
def fragment_uptime(target_id: int, request: Request, conn=Depends(get_conn)):
    """
    Lazy-loaded uptime percentage for a single target, repairing a missing cache value from TSDB.
    """
    _ = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(_, Response):
        return _

    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        return Response("<span class='text-muted'>—</span>")

    cached = sqlite_db.get_target_status(conn, target_id)
    uptime = None
    if cached is not None and "uptime_percent" in cached.keys():  # noqa: SIM118 - sqlite3.Row `in` tests values
        uptime = cached["uptime_percent"]

    if uptime is None:
        metric = select_uptime_metric(row)
        if metric:
            try:
                points = tsdb_db.query(
                    get_tsdb_dir(request),
                    target_id=target_id,
                    metric=metric,
                    since=datetime.now(UTC) - timedelta(hours=24),
                    limit=500_000,
                    latest=True,
                )
                valid = [point for point in points if point.value in (0, 1)]
                if valid:
                    uptime = 100.0 * sum(point.value == 1 for point in valid) / len(valid)
                    # Fill only a missing cache cell; a concurrent scheduler result takes precedence.
                    try:
                        with sqlite_db.transaction(conn):
                            conn.execute(
                                "UPDATE target_status SET uptime_percent = ? "
                                "WHERE target_id = ? AND uptime_percent IS NULL",
                                (uptime, target_id),
                            )
                    except sqlite3.Error as exc:
                        _log.warning("Could not cache uptime for target %s: %s", target_id, exc)
            except (OSError, ValueError) as exc:
                _log.warning("Could not read uptime for target %s: %s", target_id, exc)

    if uptime is None:
        return Response(
            "<span class='text-body-secondary' title='No availability measurements in the last 24 hours'>—</span>"
        )

    # Color based on uptime percentage (thresholds from constants)
    if uptime >= UPTIME_THRESHOLD_GREEN:
        css_class = "uptime-value uptime-ok"
    elif uptime >= UPTIME_THRESHOLD_YELLOW:
        css_class = "uptime-value uptime-warn"
    else:
        css_class = "uptime-value uptime-bad"

    from ..utils.formatters import format_percent

    return Response(f"<span class='{css_class}'>{format_percent(uptime)}</span>")


@router.get("/ui/fragments/availability-series/{target_id}")
async def fragment_availability_series(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    days: int = Query(default=30, ge=1, le=365),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
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

    until = assume_utc(until) if until else datetime.now(UTC)
    since = assume_utc(since) if since else until - timedelta(days=days)
    if since >= until:
        raise HTTPException(status_code=400, detail="'since' must be before 'until'")

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

    series = await run_in_threadpool(
        tsdb_db.query_availability_series,
        get_tsdb_dir(request),
        target_id=target_id,
        since=since,
        until=until,
        limit=limit,
        enabled_services=enabled_services,
        tcp_ports=tcp_ports,
        check_interval_seconds=row["interval_seconds"] or 60,
    )

    return {
        "target_id": target_id,
        "series": series,
    }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


_row_to_dict = target_row_to_dict


@router.get("/ui/fragments/downtime-history/{target_id}", response_class=HTMLResponse)
async def fragment_downtime_history(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    days: int = Query(default=30, ge=1, le=3650),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
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

    if since and until and assume_utc(since) > assume_utc(until):
        return HTMLResponse(_html_muted("'since' must be <= 'until'"), status_code=400)

    # Use explicit since/until if provided, otherwise fall back to days
    if since:
        query_since = assume_utc(since)
    else:
        query_since = datetime.now(tz=UTC) - timedelta(days=days)

    query_until = None
    if until:
        query_until = assume_utc(until)

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

    # Only claim "all up" when the range actually holds measurements.
    has_data = bool(periods) or await run_in_threadpool(
        _has_availability_data, request.app.state.tsdb_dir, target_id, query_since, query_until
    )

    return templates.TemplateResponse(
        request,
        "fragments/downtime_history.html",
        {
            "periods": periods,
            "has_data": has_data,
            "use_utc": use_utc_dashboard(conn),
            "range_label": range_label,
        },
    )


def _has_availability_data(tsdb_dir, target_id: int, since: datetime, until: datetime | None) -> bool:
    """True if any availability metric has a point in the range (cheap: one point per series)."""
    return any(
        tsdb_db.query(tsdb_dir, target_id=target_id, metric=metric, since=since, until=until, limit=1)
        for metric in ("http_up", "ping_up", "tcp_up")
    )


@router.get("/ui/fragments/traceroute/{target_id}", response_class=HTMLResponse)
@limiter.limit("10/minute")  # Task 7: Expensive MTR operation
async def fragment_traceroute(
    target_id: int,
    request: Request,
    refresh: bool = False,
    conn=Depends(get_conn),
):
    """
    HTMX fragment: On-demand MTR network path visualization.

    Runs MTR, geolocates hops, handles route interpolation for ICMP-blocked
    segments, and returns data for Leaflet map with dashed lines. Successful
    results are cached in memory for five minutes to avoid repeating MTR on reload.
    ``refresh=1`` (only honoured on an HX-Request, so it cannot be triggered by a
    cross-site image or link) forces one new measurement and replaces the cache entry.
    """
    _ = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(_, Response):
        return _

    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        return templates.TemplateResponse(
            request,
            "fragments/traceroute_map.html",
            {
                "geo_points": None,
                "geo_summary": None,
                "hops": [],
                "has_interpolation": False,
                "geo_unavailable_reason": None,
                "unavailable_reason": "Target not found",
            },
        )

    # Determine host for MTR (DRY: use shared helper)
    lookup_host = _resolve_target_host(row)

    if not lookup_host:
        return templates.TemplateResponse(
            request,
            "fragments/traceroute_map.html",
            {
                "geo_points": None,
                "geo_summary": None,
                "hops": [],
                "has_interpolation": False,
                "geo_unavailable_reason": None,
                "unavailable_reason": "No host configured for MTR",
            },
        )

    cache_namespace = str(request.app.state.db_path)
    force_refresh = refresh and request.headers.get("HX-Request") == "true"
    if not force_refresh:
        cached_fragment = _get_cached_traceroute_fragment(cache_namespace, target_id, lookup_host)
        if cached_fragment is not None:
            return HTMLResponse(cached_fragment, headers={"Cache-Control": "private, no-store"})
        return await _run_traceroute_fragment(request, target_id, lookup_host, cache_namespace)

    refresh_key = (cache_namespace, target_id, lookup_host)
    retry_after = _begin_traceroute_refresh(refresh_key)
    if retry_after is not None:
        return HTMLResponse(
            "A new measurement is already running or was just completed. Please wait a moment.",
            status_code=429,
            headers={"Retry-After": str(max(1, int(retry_after) + 1)), "Cache-Control": "private, no-store"},
        )
    try:
        return await _run_traceroute_fragment(request, target_id, lookup_host, cache_namespace)
    finally:
        _end_traceroute_refresh(refresh_key)


async def _run_traceroute_fragment(request: Request, target_id: int, lookup_host: str, cache_namespace: str):
    """Run MTR for ``lookup_host``, render the fragment and store it in the cache."""
    # MTR utilities (imports at function level for lazy loading)
    from ..services.mtr_run import analyze_route, resolve_target_ip, run_mtr
    from ..services.route_interpolator import (
        collapse_interpolated_hops,
        ensure_destination_hop,
        interpolate_route,
    )
    from ..services.traceroute_geo import (
        geolocate_with_hostname_hint,
        is_ipv6_address,
        mtr_hops_to_geo_points,
        shorten_ipv6_for_display,
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
        return templates.TemplateResponse(
            request,
            "fragments/traceroute_map.html",
            {
                "hops": [],
                "geo_points": None,
                "geo_summary": None,
                "has_interpolation": False,
                "geo_unavailable_reason": None,
                "unavailable_reason": "MTR returned no hops",
            },
        )

    # Analyze route for ICMP blocking patterns
    route_analysis = analyze_route(hops, target_ip)

    # Get destination geo data for interpolation.
    # Use the hostname hint (POP > GeoIP > online), not plain GeoIP: GeoLite2
    # often has only country + coarse coordinates for the target, so the city
    # has to come from the rDNS/POP inference when available.
    destination_geo = None
    if target_ip:
        destination_geo = await run_in_threadpool(geolocate_with_hostname_hint, target_ip, lookup_host)

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
            geo = await run_in_threadpool(geolocate_with_hostname_hint, h.get("ip"), h.get("hostname"))

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
    while enriched_hops and enriched_hops[-1].get("status") == "timeout" and not enriched_hops[-1].get("_interpolated"):
        enriched_hops.pop()

    # Collapse consecutive interpolated hops for cleaner table
    display_hops = collapse_interpolated_hops(enriched_hops)

    # Check if any interpolation was applied
    has_interpolation = any(h.get("status") == "interpolated" or h.get("_interpolated") for h in enriched_hops)

    geo_unavailable_reason = None

    if not geo_points:
        geo_unavailable_reason = "No public geolocated hops available"
    elif len(geo_points) == 1:
        geo_unavailable_reason = "All hops resolve to same location (no route path)"

    destination_geo_point = next((point for point in reversed(geo_points) if point.get("_destination")), None)
    if destination_geo_point:
        destination_geo_point["destination_host"] = lookup_host
        if target_ip:
            destination_geo_point["destination_ip"] = target_ip

    route_start = display_hops[0] if display_hops else {}
    route_end = display_hops[-1] if display_hops else {}
    route_start_is_mapped = False
    if route_start and geo_points:
        try:
            start_hop_number = int(route_start.get("hop"))
            first_mapped = geo_points[0]
            hop_range = first_mapped.get("_hop_range")
            if hop_range:
                range_start, range_end = (int(value) for value in hop_range.split("-", maxsplit=1))
                route_start_is_mapped = range_start <= start_hop_number <= range_end
            else:
                route_start_is_mapped = int(first_mapped.get("hop")) == start_hop_number
        except (TypeError, ValueError):
            route_start_is_mapped = False

    mapped_hops: set[int] = set()
    for point in geo_points:
        try:
            if point.get("_hop_range"):
                start, end = (int(value) for value in point["_hop_range"].split("-", maxsplit=1))
                mapped_hops.update(range(start, end + 1))
            elif point.get("hop") is not None:
                mapped_hops.add(int(point["hop"]))
        except (TypeError, ValueError):
            continue

    sequence_hops = []
    for hop in display_hops:
        raw_hops = hop.get("_raw_hops") if hop.get("_collapsed") else None
        if raw_hops:
            sequence_hops.extend(raw_hops)
        else:
            sequence_hops.append(hop)

    route_sequence = []
    destination_in_sequence = False
    for index, hop in enumerate(sequence_hops):
        hop_number = hop.get("hop")
        hop_label = hop.get("hop_display") or str(hop_number or "?")
        try:
            hop_is_mapped = int(hop_number) in mapped_hops
        except (TypeError, ValueError):
            hop_is_mapped = False
        is_start = index == 0
        is_destination = bool(target_ip and hop.get("ip") == target_ip) or bool(hop.get("_destination"))
        kind = (
            "both"
            if is_start and is_destination
            else (
                "start" if is_start else ("destination" if is_destination else ("hop" if hop_is_mapped else "unmapped"))
            )
        )
        role_label = "Start" if is_start else ("Destination" if is_destination else "Hop")
        if hop.get("_collapsed"):
            state, state_label = "collapsed", "ICMP blocked, interpolated"
        elif hop.get("status") == "interpolated" or hop.get("_interpolated"):
            state, state_label = "interpolated", "interpolated"
        elif hop.get("status") == "timeout" or hop.get("_synthetic_destination"):
            state, state_label = "unreachable", "no reply"
        else:
            state, state_label = "normal", ""
        route_sequence.append(
            {
                "kind": kind,
                "state": state,
                "label": hop_label,
                "country": hop.get("country") or "",
                "aria_label": f"{role_label} {hop_label}" + (f", {state_label}" if state_label else ""),
            }
        )
        destination_in_sequence = destination_in_sequence or is_destination

    if display_hops and not destination_in_sequence:
        destination_number = None
        if destination_geo_point:
            destination_number = destination_geo_point.get("_hop_range") or destination_geo_point.get("hop")
        route_sequence.append(
            {
                "kind": "destination",
                "state": "normal",
                "label": str(destination_number or "Z"),
                "country": (destination_geo_point or {}).get("country") or "",
                "aria_label": f"Destination hop {destination_number}" if destination_number else "Destination",
            }
        )

    response = templates.TemplateResponse(
        request,
        "fragments/traceroute_map.html",
        {
            "hops": display_hops,
            "geo_points": geo_points,
            "has_interpolation": has_interpolation,
            "geo_unavailable_reason": geo_unavailable_reason,
            "unavailable_reason": None,
            "route_start_hop": route_start.get("hop_display") or route_start.get("hop"),
            "route_start_is_mapped": route_start_is_mapped,
            "route_start_ip": route_start.get("ip_display") or route_start.get("ip"),
            "route_start_host": route_start.get("hostname_display") or route_start.get("hostname"),
            "route_destination_hop": (
                destination_geo_point.get("_hop_range") or destination_geo_point.get("hop")
                if destination_geo_point
                else (route_end.get("hop_display") or route_end.get("hop"))
            ),
            "route_destination_ip": target_ip or route_end.get("ip_display") or route_end.get("ip"),
            "route_destination_host": lookup_host,
            "first_mapped_hop": (geo_points[0].get("_hop_range") or geo_points[0].get("hop")) if geo_points else None,
            "route_sequence": route_sequence,
            "kpis": _destination_kpis(display_hops, target_ip, destination_geo_point),
            "mapped_hop_count": len(mapped_hops),
        },
    )
    _cache_traceroute_fragment(cache_namespace, target_id, lookup_host, response.body)
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ─── Signal Registration UI Fragments ───────────────────────────────────────────


@router.get("/ui/fragments/signal-qr", response_class=HTMLResponse)
def fragment_signal_qr(request: Request, conn=Depends(get_conn)):
    """Render Signal QR code for device linking.

    Uses signal_cli_backend directly (local subprocess, no HTTP).
    """
    import base64

    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import SignalTimeout, SignalUnavailable

    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    try:
        backend = get_signal_backend()
        png_bytes, _session_id = backend.get_link_qr(device_name="justUp")

        if not png_bytes:
            return HTMLResponse("""
                <div class="text-muted small text-center py-2">
                    <span class="material-icons align-middle me-1" style="font-size: 16px;">info</span>
                    QR linking not available
                </div>
            """)

        qr_b64 = base64.b64encode(png_bytes).decode("ascii")

        return HTMLResponse(f"""
            <div class="text-center">
                <img src="data:image/png;base64,{qr_b64}"
                     style="max-width: 280px; border-radius: 8px;"
                     alt="Signal QR Code">
                <p class="text-muted small mt-2">
                    <span class="material-icons align-middle me-1" style="font-size: 16px;">phone_android</span>
                    Scan with Signal app to link device
                </p>
            </div>
        """)
    except (SignalUnavailable, SignalTimeout) as e:
        _log.debug("QR fragment error: %s", e)
        return HTMLResponse(_html_error("signal-cli unavailable"))
    except Exception as e:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
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
            return HTMLResponse(f"""
                <span class="badge bg-success">
                    <span class="material-icons me-1">link</span>Linked ({count} account{"s" if count != 1 else ""})
                </span>
            """)
        return HTMLResponse("""
                <span class="badge bg-secondary">
                    <span class="material-icons me-1">link_off</span>Not Linked
                </span>
            """)
    except SignalUnavailable:
        return HTMLResponse(_html_badge("signal-cli unavailable"))
    except Exception as e:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
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
        _log.warning("Signal status unavailable: %s", e)
        return HTMLResponse("""
            <div class="d-flex align-items-center text-danger">
                <span class="material-icons me-2">cloud_off</span>
                <span>signal-cli unavailable</span>
            </div>
        """)
    except Exception as e:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
        _log.warning("Signal status check failed: %s", e)
        return HTMLResponse("""
            <div class="d-flex align-items-center text-danger">
                <span class="material-icons me-2">cloud_off</span>
                <span>Status check failed</span>
            </div>
        """)

    if not status.reachable:
        return HTMLResponse("""
            <div class="d-flex align-items-center text-danger">
                <span class="material-icons me-2">cloud_off</span>
                <span>signal-cli not available</span>
            </div>
        """)

    # Build status badges from SignalStatus dataclass
    badges = []
    badges.append("""<span class="badge bg-success me-1">
        <span class="material-icons align-middle me-1" style="font-size: 14px;">cloud_done</span>Reachable
    </span>""")

    if status.registered:
        badges.append("""<span class="badge bg-success me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">verified_user</span>Registered
        </span>""")
    else:
        badges.append("""<span class="badge bg-warning text-dark me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">person_off</span>Not Registered
        </span>""")

    if status.linked:
        badges.append("""<span class="badge bg-success me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">link</span>Linked
        </span>""")
    else:
        badges.append("""<span class="badge bg-secondary me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">link_off</span>Not Linked
        </span>""")

    # Account info from SignalStatus
    accounts = status.accounts
    acct_list = ", ".join(html.escape(str(a)) for a in accounts[:3]) + ("..." if len(accounts) > 3 else "")
    acct_info = f'<small class="text-muted d-block mt-1">Accounts: {acct_list}</small>' if accounts else ""

    return HTMLResponse(f"""
        <div>
            {"".join(badges)}
            {acct_info}
        </div>
    """)


@router.get("/ui/fragments/signal-mobile-check", response_class=HTMLResponse)
def fragment_signal_mobile_check(request: Request, conn=Depends(get_conn)):
    """
    Render the Signal "register a new number" button.

    Registration is always allowed, so the button is returned enabled with
    data-is-mobile="true" and data-debug-mode="true" regardless of device or log level.
    """
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Always allow registration, regardless of device type or debug mode
    return HTMLResponse("""
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
    """)
