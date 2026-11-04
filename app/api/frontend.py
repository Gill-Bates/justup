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
  - fragment_signal_* are sync def: FastAPI runs their Signal calls in the threadpool
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

import asyncio
import functools
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
from ..models.settings import SettingsUpdate
from ..models.targets import target_row_to_dict
from ..monitor.quality_probe import DEFAULT_QUALITY_TARGETS
from ..services.route_timeline import (
    STATE_LABELS,
    expand_sequence_hops,
    logical_hop_count,
    mapped_hop_numbers,
    timeline_measurement,
)
from ..services.route_timeline import build_route_timeline as _build_route_timeline
from ..utils.auth_helpers import (
    is_password_change_exempt_path,
    is_password_change_pending,
    resolve_user_optional,
    resolve_user_or_htmx_redirect,
)
from ..utils.availability_rules import downtime_series
from ..utils.constants import (
    DATE_FORMAT_ISO,
    DEFAULT_GEOIP_AUTO_UPDATE,
    DEFAULT_METRICS_TIMEOUT_MINUTES,
    DEFAULT_PDF_PAGE_SIZE,
    DEFAULT_PURGE_ENABLED,
    DISPLAY_UNGROUPED_EN,
    MONITORING_LIMITS,
    QUALITY_MIN_TARGETS,
    UI_PAGE_SIZE_ALERTS,
    UI_PAGE_SIZE_DEFAULT,
    UI_PAGE_SIZE_TARGETS,
    UI_PAGE_SIZE_TARGETS_ALL,
    CheckStatus,
    SettingKeys,
)
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.endpoint_view import build_endpoint_views
from ..utils.formatters import format_compact_count, format_percent, row_to_alert_dict
from ..utils.public_url import get_public_hostname
from ..utils.rate_limit import rate_limit
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
from ..utils.tcp_ports import parse_port_list
from ..utils.time import assume_utc, utcnow
from ..utils.uptime_metric import enabled_availability_metrics, uptime_level
from ..utils.version import BUILD_INFO, VERSION
from . import notifications as notifications_api

router = APIRouter(tags=["frontend"])

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_TRACEROUTE_CACHE_TTL_SECONDS = 5 * 60
_TRACEROUTE_CACHE_MAX_ENTRIES = 128
# Epoch-ms header carrying the time of the real measurement; the browser derives its 24 h cache from it.
_TRACEROUTE_MEASURED_AT_HEADER = "X-Measured-At"
# (monotonic expiry, rendered fragment, measurement time in epoch ms)
_traceroute_fragment_cache: OrderedDict[tuple[str, int, str], tuple[float, bytes, int]] = OrderedDict()
_traceroute_fragment_cache_lock = threading.Lock()


def _get_cached_traceroute_fragment(cache_namespace: str, target_id: int, host: str) -> tuple[bytes, int] | None:
    """Return ``(body, measured_at_ms)`` of a cached measurement younger than the TTL."""
    key = (cache_namespace, target_id, host)
    now = time.monotonic()
    with _traceroute_fragment_cache_lock:
        entry = _traceroute_fragment_cache.get(key)
        if entry is None:
            return None
        expires_at, body, measured_at_ms = entry
        if now >= expires_at:
            _traceroute_fragment_cache.pop(key, None)
            return None
        _traceroute_fragment_cache.move_to_end(key)
        return body, measured_at_ms


def _cache_traceroute_fragment(
    cache_namespace: str, target_id: int, host: str, body: bytes, measured_at_ms: int
) -> None:
    key = (cache_namespace, target_id, host)
    with _traceroute_fragment_cache_lock:
        _traceroute_fragment_cache[key] = (
            time.monotonic() + _TRACEROUTE_CACHE_TTL_SECONDS,
            body,
            measured_at_ms,
        )
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


_logical_hop_count = logical_hop_count


def _format_percent_value(value: float) -> str:
    return f"{round(value, 1):g}%"


def _destination_kpis(
    display_hops: list[dict],
    target_ip: str | None,
    destination_geo_point: dict | None,
    destination_reached: bool | None = None,
) -> dict:
    """Derive the KPI row from measured data only; unknown values stay ``None`` (never 0).

    ``destination_reached`` is the single verdict of ``analyze_route``; without it the flags of the
    hops decide (callers outside the fragment). A synthetic destination never supplies latency or loss.
    """
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
    if destination_reached is False:
        measured = None

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
        "destination_reached": measured is not None if destination_reached is None else destination_reached,
        "latency_display": f"{latency_ms:.1f} ms" if latency_ms is not None else None,
        "loss_display": _format_percent_value(loss_pct) if loss_pct is not None else None,
        "loss_pct": loss_pct,
        "city": city or None,
        "country": country or None,
    }


templates.env.globals["version"] = f"v{VERSION}"
templates.env.globals["build_info"] = BUILD_INFO
templates.env.globals["now"] = lambda: datetime.now(UTC)

templates.env.filters["format_percent"] = format_percent
templates.env.filters["format_compact_count"] = format_compact_count


def _regex_replace(value: str, pattern: str, replacement: str) -> str:
    """Replace regex pattern in string."""
    return re.sub(pattern, replacement, value)


templates.env.filters["regex_replace"] = _regex_replace


def _group_dom_id(value: str) -> str:
    """Readable slug plus a name hash, so names that slug alike keep distinct DOM ids."""
    slug = re.sub(r"[^a-z0-9_]", "", str(value).lower().replace(" ", "_")) or "group"
    return f"{slug}-{hashlib.sha1(str(value).encode('utf-8'), usedforsecurity=False).hexdigest()[:8]}"


templates.env.filters["group_dom_id"] = _group_dom_id


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br", "md_in_html"], output_format="html")


# Trust boundary: only the shipped CHANGELOG.md (About page) reaches this unsanitised filter.
templates.env.filters["markdown"] = _render_markdown

_log = logging.getLogger(__name__)

# Responses embedding link secrets (QR codes) must never land in a browser or proxy cache.
_NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}


# --- Inline HTML Helpers (prevent XSS, centralize styling) ---

# Valid status badge tones (prevents class injection)
VALID_BADGE_COLORS = {"secondary", "success", "danger", "warning", "info"}


def _html_badge(text: str, color: str = "secondary", icon: str | None = None) -> str:
    """Generate a status badge with optional Material icon."""
    color = color if color in VALID_BADGE_COLORS else "secondary"
    escaped = html.escape(text)
    icon_html = f'<span class="material-icons me-1">{html.escape(icon)}</span>' if icon else ""
    variant = "neutral" if color == "secondary" else color
    return f'<span class="badge ju-status-badge--{variant}">{icon_html}{escaped}</span>'


def _html_error(text: str) -> str:
    """Generate a small error message."""
    return f'<div class="text-danger small">{html.escape(text)}</div>'


def _get_geoip_build_info() -> int | None:
    """Get GeoIP database build epoch for display in About page."""
    try:
        from ..utils.bootstrap import get_geoip_build_info

        result = get_geoip_build_info()
        if result:
            return result[1]
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


def _scope_digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:16]


def _user_scope_key(user: dict | None) -> str:
    """Opaque, stable, non-secret browser-storage key for one user (empty when signed out)."""
    return _scope_digest("user", user.get("id"), user.get("created_at")) if user else ""


templates.env.filters["user_scope"] = _user_scope_key


# Bump when the rendered traceroute fragment or its geo decision changes; the browser cache drops
# entries of another revision, version or build. The server cache is in memory and dies with the process.
TRACEROUTE_FRAGMENT_REVISION = 2


def _traceroute_cache_scope(user: dict, row) -> dict[str, str]:
    """Non-secret scope keys the browser's traceroute cache is separated by.

    Hashes keep the keys opaque and stable. The target key changes with the MTR destination
    and with a re-created target, so a re-used id or an edited host never shows a stale route.
    """

    return {
        "user": _user_scope_key(user),
        "target": _scope_digest("target", _row_get(row, "id"), _row_get(row, "created_at"), _resolve_target_host(row)),
        "build": _scope_digest("build", VERSION, BUILD_INFO, TRACEROUTE_FRAGMENT_REVISION),
    }


def require_ui_user(request: Request, conn) -> dict | Response:
    """Helper to resolve UI user or return redirect response."""
    user = resolve_user_optional(request, conn, allow_cookie=True)
    if not user:
        return RedirectResponse("/ui/login", status_code=302)
    if is_password_change_pending(user) and not is_password_change_exempt_path(request.url.path):
        return RedirectResponse("/ui/change-password", status_code=302)
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


def _fill_tcp_status_from_series(target: dict, tsdb_dir) -> None:
    """Give a TCP port without any stored state the latest ``tcp_up_<port>`` point, the series the charts plot."""
    stored = {(p["protocol"], p["port"]): p for p in target["network_ports"] if p.get("checked_at")}
    cache = dict(target.get("tcp_port_status") or {})
    try:
        ports = parse_port_list(str(target.get("tcp_ports") or "")) or [80]
    except ValueError:
        return
    for port in ports:
        if ("tcp", port) in stored or cache.get(str(port)):
            continue
        points = tsdb_db.query_latest(tsdb_dir, target_id=target["id"], metric=f"tcp_up_{port}", count=1)
        if points and points[-1].value in (0, 1):
            cache[str(port)] = "up" if points[-1].value == 1 else "down"
    target["tcp_port_status"] = cache


def _target_detail_view(conn, row, target_id: int, tsdb_dir=None) -> dict:
    """Target row merged with its cached status, as the detail page and its KPI fragment render it."""
    target = _row_to_dict(row)
    cached = sqlite_db.get_target_status(conn, target_id)
    if cached:
        target.update(build_target_status(row, cached))
        target["overall_status"] = cached["overall_status"]
    else:
        target.update(get_status_defaults())
        target["overall_status"] = "unknown" if bool(row["is_enabled"]) else "disabled"
    target["network_ports"] = sqlite_db.list_network_ports(conn, target_id)
    if tsdb_dir is not None and target.get("enable_tcp_connect"):
        _fill_tcp_status_from_series(target, tsdb_dir)
    target["endpoints"] = build_endpoint_views(target, target["network_ports"])
    return target


@router.get(
    "/ui/fragments/target-info-cards/{target_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("60/minute"))],
)
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
        {"user": user, "target": _target_detail_view(conn, row, target_id, get_tsdb_dir(request))},
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
    target = _target_detail_view(conn, row, target_id, get_tsdb_dir(request))

    return templates.TemplateResponse(
        request,
        "target_detail.html",
        {
            "active_page": "targets",
            "user": user,
            "target": target,
            "use_utc": use_utc_dashboard(conn),
            "traceroute_cache_scope": _traceroute_cache_scope(user, row),
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
    sender_number = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)

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


def _field_bounds(name: str) -> dict[str, int]:
    bounds: dict[str, int] = {}
    for meta in SettingsUpdate.model_fields[name].metadata:
        for attr, key in (("ge", "min"), ("le", "max"), ("max_length", "max_length")):
            if (value := getattr(meta, attr, None)) is not None:
                bounds[key] = value
    return bounds


# Form bounds come from the validating PATCH model so the settings UI cannot drift from the API.
SETTINGS_LIMITS = {
    name: _field_bounds(name)
    for name in (
        "metrics_timeout_minutes",
        "session_max_days",
        "quality_targets",
        "quality_min_score",
        "quality_rtt_threshold_ms",
        "provider_lookup_cache_ttl_seconds",
    )
}


@router.get("/ui/fragments/settings-general", response_class=HTMLResponse)
def fragment_settings_general(request: Request, conn=Depends(get_conn)):
    """Render General settings tab fragment with pre-populated values."""
    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    general_data = {
        "use_utc_dashboard": sqlite_db.get_setting(conn, "use_utc_dashboard", False),
        "pdf_page_size": sqlite_db.get_setting(conn, SettingKeys.PDF_PAGE_SIZE, DEFAULT_PDF_PAGE_SIZE)
        or DEFAULT_PDF_PAGE_SIZE,
        "metrics_timeout_minutes": sqlite_db.get_setting(
            conn, SettingKeys.METRICS_TIMEOUT_MINUTES, DEFAULT_METRICS_TIMEOUT_MINUTES
        )
        or DEFAULT_METRICS_TIMEOUT_MINUTES,
        "public_hostname": get_public_hostname(conn),
    }

    return templates.TemplateResponse(
        request,
        "settings/general.html",
        {
            "current_user": user,
            "settings_limits": SETTINGS_LIMITS,
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
            "settings_limits": SETTINGS_LIMITS,
            "settings": {
                "geoip_auto_update": sqlite_db.get_setting(
                    conn, SettingKeys.GEOIP_AUTO_UPDATE, DEFAULT_GEOIP_AUTO_UPDATE
                ),
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

    db_path = request.app.state.db_path
    tsdb_dir = get_tsdb_dir(request)
    stats = await run_in_threadpool(compute_storage_stats, db_path, tsdb_dir)

    sql_size_bytes = stats["sql_size_bytes"]
    tsdb_size_bytes = stats["tsdb_size_bytes"]
    total_points = stats["total_points"]

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
                "purge_enabled": sqlite_db.get_setting(conn, SettingKeys.PURGE_ENABLED, DEFAULT_PURGE_ENABLED),
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
    from ..services.notifications.smtp_settings import load_public_smtp_settings
    from ..services.notifications.smtp_transport import IMPLICIT_TLS_PORT

    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Same reader as GET /settings and the send path; only the password flag, never the password.
    smtp_data = load_public_smtp_settings(conn)
    smtp_data["smtp_port"] = smtp_data["smtp_port"] or ""  # empty field instead of "None"

    from ..services.notifications.email_template import load_template_preview

    template_data = load_template_preview(conn)

    return templates.TemplateResponse(
        request,
        "settings/smtp.html",
        {
            "current_user": user,
            "smtp": smtp_data,
            "implicit_tls_port": IMPLICIT_TLS_PORT,
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
            "settings_limits": SETTINGS_LIMITS,
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
    from ..services.notifications.signal_template import load_signal_templates

    user = require_admin_htmx(request, conn)
    if isinstance(user, Response):
        return user

    # Sender number is a DB setting (same source as dispatch and settings API)
    sender_number = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)

    signal_enabled = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)

    signal_template = load_signal_templates(conn)

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


# Versions of installed packages and the vendored Leaflet cannot change while the process runs.
@functools.lru_cache(maxsize=1)
def _installed_dependencies() -> tuple[tuple[str, str], ...]:
    """Key dependency versions (declared in pyproject.toml) plus the vendored Leaflet."""
    from importlib.metadata import version as get_pkg_version

    key_packages = (
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
        "sslyze",
        "starlette",
        "uvicorn",
    )
    dependencies = []
    for pkg_name in key_packages:
        try:
            dependencies.append((pkg_name, get_pkg_version(pkg_name)))
        except Exception:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
            dependencies.append((pkg_name, "?"))

    # Leaflet version comes from its @preserve banner.
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
    return tuple(dependencies)


# signal-cli is an external binary a non-container admin may update; re-probe after a TTL.
# A failed probe is retried sooner, but never on every request.
_SIGNAL_CLI_TTL_OK_SECONDS = 600.0
_SIGNAL_CLI_TTL_FAILED_SECONDS = 60.0
_signal_cli_lock = threading.Lock()
_signal_cli_cache: tuple[float, str] | None = None  # (valid until monotonic time, label)


def _probe_signal_cli() -> tuple[str, float]:
    """Run ``signal-cli --version``; return the label and how long it stays valid."""
    import subprocess

    try:
        result = subprocess.run(
            ["signal-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - degrade gracefully: report unavailable/placeholder instead of failing the page
        return "not available", _SIGNAL_CLI_TTL_FAILED_SECONDS
    if result.returncode == 0 and result.stdout.strip():
        # Output format: "signal-cli 0.13.23"
        return result.stdout.strip().split()[-1], _SIGNAL_CLI_TTL_OK_SECONDS
    return "not found", _SIGNAL_CLI_TTL_FAILED_SECONDS


def _signal_cli_version() -> str:
    """Cached signal-cli version label; the lock keeps concurrent requests to one probe."""
    global _signal_cli_cache
    with _signal_cli_lock:
        now = time.monotonic()
        if _signal_cli_cache is not None and now < _signal_cli_cache[0]:
            return _signal_cli_cache[1]
        label, ttl = _probe_signal_cli()
        _signal_cli_cache = (time.monotonic() + ttl, label)
        return label


@router.get("/ui/about", response_class=HTMLResponse)
def about_page(request: Request, conn=Depends(get_conn)):
    """Render the about page with version, dependencies, and license."""
    import sys

    user = require_ui_user(request, conn)
    if isinstance(user, Response):
        return user

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    dependencies = [*_installed_dependencies(), ("signal-cli", _signal_cli_version())]
    dependencies.sort(key=lambda x: x[0].lower())

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
    status = status if status and status.strip() else None
    from_date = from_date if from_date and from_date.strip() else None
    to_date = to_date if to_date and to_date.strip() else None

    target_id_int: int | None = None
    if target_id and target_id.strip():
        try:
            target_id_int = int(target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target_id") from None
        if target_id_int < 1:
            raise HTTPException(status_code=400, detail="target_id must be >= 1")

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
            to_dt = datetime.strptime(to_date, DATE_FORMAT_ISO).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=UTC
            )
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid to_date format: {to_date} (expected YYYY-MM-DD)"
            ) from None

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

    _, open_total = sqlite_db.list_alerts(conn, status="open", page=1, page_size=1)

    alerts = [row_to_alert_dict(row) for row in alerts_rows]

    all_targets = sqlite_db.list_targets(conn)
    targets = [{"id": t["id"], "name": t["name"]} for t in all_targets]

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


@router.get(
    "/ui/fragments/status-summary", response_class=HTMLResponse, dependencies=[Depends(rate_limit("30/minute"))]
)
def fragment_status_summary(request: Request, conn=Depends(get_conn)):
    """Status summary cards - requires auth."""
    _ = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(_, Response):
        return _

    rows = sqlite_db.list_targets(conn)

    status_cache = {s["target_id"]: s for s in sqlite_db.get_all_target_statuses(conn)}

    stats = {"total": len(rows), "up": 0, "down": 0, "disabled": 0}
    for r in rows:
        if not bool(r["is_enabled"]):
            stats["disabled"] += 1
            continue

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


@router.get("/ui/fragments/targets-table", response_class=HTMLResponse, dependencies=[Depends(rate_limit("30/minute"))])
def fragment_targets_table(
    request: Request,
    page: int = Query(1, ge=1),
    show_all: bool = Query(False, alias="all"),
    conn=Depends(get_conn),
):
    """Targets table - requires auth. ``all=1`` returns every target unpaginated for the client-side search."""
    user = resolve_user_or_htmx_redirect(request, conn)
    if isinstance(user, Response):
        return user

    page_size = UI_PAGE_SIZE_TARGETS_ALL if show_all else UI_PAGE_SIZE_TARGETS
    rows, total = sqlite_db.list_targets_paginated(conn, page=1 if show_all else page, page_size=page_size)
    # Polling keeps the current page; fall back to the last one if it vanished (deleted targets).
    # The search view keeps the requested page number in the carrier so leaving the search returns to it.
    last_page = max(1, (total + page_size - 1) // page_size)
    if not show_all and page > last_page:
        page = last_page
        rows, total = sqlite_db.list_targets_paginated(conn, page=page, page_size=page_size)

    status_cache = {s["target_id"]: s for s in sqlite_db.get_all_target_statuses(conn)}
    stored_ports = sqlite_db.all_network_ports(conn)

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

        t["endpoints"] = build_endpoint_views(t, stored_ports.get(target_id, []))
        targets.append(t)

    # Sorting is already done in SQL (list_targets_paginated).

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
            "show_all": show_all,
            "user": dict(user),
        },
    )


@router.get(
    "/ui/fragments/target-status/{target_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("60/minute"))],
)
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
        safe_username = html.escape(str(user["username"]))
        return Response(content=f"<i class='bi bi-person-circle me-1'></i>{safe_username}", media_type="text/html")
    return Response("")


def _cache_uptime(conn, target_id: int, uptime: float) -> None:
    """Store a repaired uptime value, creating the status row if the scheduler has not yet.

    A concurrent scheduler result takes precedence: only a NULL cell is filled.
    """
    try:
        with sqlite_db.transaction(conn):
            conn.execute(
                "INSERT INTO target_status(target_id, overall_status, updated_at, uptime_percent) "
                "VALUES (?, 'unknown', ?, ?) "
                "ON CONFLICT(target_id) DO UPDATE SET uptime_percent = excluded.uptime_percent "
                "WHERE target_status.uptime_percent IS NULL",
                (target_id, utcnow(), uptime),
            )
    except sqlite3.Error as exc:
        _log.warning("Could not cache uptime for target %s: %s", target_id, exc)


@router.get(
    "/ui/fragments/uptime/{target_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("300/minute"))],
)
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
        metrics = enabled_availability_metrics(row)
        if metrics:
            try:
                up, total = tsdb_db.count_availability(
                    get_tsdb_dir(request),
                    target_id=target_id,
                    metrics=metrics,
                    since=datetime.now(UTC) - timedelta(hours=24),
                )
                if total:
                    uptime = 100.0 * up / total
                    _cache_uptime(conn, target_id, uptime)
            except (OSError, ValueError) as exc:
                _log.warning("Could not read uptime for target %s: %s", target_id, exc)

    if uptime is None:
        return Response(
            "<span class='text-body-secondary' title='No availability measurements in the last 24 hours'>—</span>"
        )

    # Color follows the target's availability SLA; without one, the fixed limits in uptime_level apply.
    level = uptime_level(
        uptime,
        sla_enabled=bool(row["sla_enabled"]),
        sla_availability_pct=row["sla_availability_pct"],
    )
    css_class = f"uptime-value uptime-{level}"
    # Text cue so the state is not conveyed by color alone.
    cue = {"ok": "", "warn": " (warning)", "bad": " (critical)"}[level]
    from ..utils.formatters import format_percent

    hidden = f"<span class='visually-hidden'>{cue}</span>" if cue else ""
    return Response(f"<span class='{css_class}'>{format_percent(uptime)}{hidden}</span>")


_AVAILABILITY_MAX_RANGE_DAYS = 365


@router.get("/ui/fragments/availability-series/{target_id}")
async def fragment_availability_series(
    target_id: int,
    request: Request,
    conn=Depends(get_conn),
    days: int = Query(default=30, ge=1, le=_AVAILABILITY_MAX_RANGE_DAYS),
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
    if until - since > timedelta(days=_AVAILABILITY_MAX_RANGE_DAYS):
        raise HTTPException(status_code=400, detail=f"Date range must not exceed {_AVAILABILITY_MAX_RANGE_DAYS} days")

    enabled_services = set()
    if row["enable_ping"]:
        enabled_services.add("PING")
    if row["enable_http_check"]:
        enabled_services.add("HTTP")
    if row["enable_tcp_connect"]:
        enabled_services.add("TCP")
    udp_ports = None
    if row["enable_udp_scan"]:
        enabled_services.add("UDP")
        try:
            udp_ports = parse_port_list(str(row["udp_ports"] or "")) or None
        except ValueError:
            udp_ports = None

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
        udp_ports=udp_ports,
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
        series=downtime_series(row),
        check_interval_seconds=row["interval_seconds"] or 60,
    )
    unmeasured = await run_in_threadpool(
        tsdb_db.query_unmeasured_periods,
        request.app.state.tsdb_dir,
        target_id=target_id,
        since=query_since,
        until=query_until,
        limit=limit,
        series=downtime_series(row),
        check_interval_seconds=row["interval_seconds"] or 60,
    )
    # Three separate kinds: measured downtime, measurement gap, UNKNOWN result (the last two are no downtime).
    entries = [
        {
            "kind": "down",
            "start": p.start,
            "end": p.end,
            "duration_seconds": p.duration_seconds,
            "services": p.failed_services,
        }
        for p in periods
    ] + [
        {"kind": u.kind, "start": u.start, "end": u.end, "duration_seconds": u.duration_seconds, "services": u.services}
        for u in unmeasured
    ]
    entries.sort(key=lambda entry: entry["start"], reverse=True)

    # Only claim "all up" when the range actually holds measurements.
    has_data = bool(entries) or await run_in_threadpool(
        _has_availability_data,
        request.app.state.tsdb_dir,
        target_id,
        query_since,
        query_until,
        tuple(metric for metric, _label in downtime_series(row)),
    )

    return templates.TemplateResponse(
        request,
        "fragments/downtime_history.html",
        {
            "periods": entries,
            "has_data": has_data,
            "use_utc": use_utc_dashboard(conn),
            "range_label": range_label,
        },
    )


def _has_availability_data(
    tsdb_dir, target_id: int, since: datetime, until: datetime | None, metrics: tuple[str, ...]
) -> bool:
    """True if any of the given (enabled) availability metrics has a point in the range."""
    return any(
        tsdb_db.query(tsdb_dir, target_id=target_id, metric=metric, since=since, until=until, limit=1)
        for metric in metrics
    )


@router.get(
    "/ui/fragments/traceroute/{target_id}", response_class=HTMLResponse, dependencies=[Depends(rate_limit("10/minute"))]
)
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
    results are cached in memory for five minutes to avoid repeating MTR on reload
    and carry the time of the real measurement in ``X-Measured-At`` (epoch ms, also
    on a cache hit); only those responses may be kept by the browser's 24 h cache.
    ``refresh=1`` (only honoured on an HX-Request, so it cannot be triggered by a
    cross-site image or link) forces one new measurement and replaces the cache entry.
    The browser also uses it for an automatic scan after its own 24 h entry expired, so
    that a result still sitting in the five-minute cache is not taken for a new one.
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
            body, measured_at_ms = cached_fragment
            return HTMLResponse(
                body,
                headers={"Cache-Control": "private, no-store", _TRACEROUTE_MEASURED_AT_HEADER: str(measured_at_ms)},
            )
        return await _single_flight_traceroute(request, target_id, lookup_host, cache_namespace)

    refresh_key = (cache_namespace, target_id, lookup_host)
    # A cache-miss measurement already running for this key must not race a forced one (stale overwrite).
    running = _traceroute_inflight.get(refresh_key)
    retry_after = 1.0 if running is not None and not running.done() else _begin_traceroute_refresh(refresh_key)
    if retry_after is not None:
        return HTMLResponse(
            "A new measurement is already running or was just completed. Please wait a moment.",
            status_code=429,
            headers={"Retry-After": str(max(1, int(retry_after) + 1)), "Cache-Control": "private, no-store"},
        )
    try:
        # Same single-flight registry as cache misses, so concurrent misses follow this measurement.
        return await _single_flight_traceroute(request, target_id, lookup_host, cache_namespace)
    finally:
        _end_traceroute_refresh(refresh_key)


# One in-flight measurement per key for plain cache misses; followers reuse its result.
_traceroute_inflight: dict[tuple[str, int, str], asyncio.Task] = {}


async def _single_flight_traceroute(request: Request, target_id: int, lookup_host: str, cache_namespace: str):
    """Run at most one MTR per key; concurrent cache misses wait for it and get its body."""
    key = (cache_namespace, target_id, lookup_host)
    task = _traceroute_inflight.get(key)
    leader = task is None
    if leader:
        task = asyncio.ensure_future(_run_traceroute_fragment(request, target_id, lookup_host, cache_namespace))
        _traceroute_inflight[key] = task
        # Release in a callback so a failure or cancelled caller cannot leave the key reserved.
        task.add_done_callback(
            lambda done: _traceroute_inflight.pop(key, None) if _traceroute_inflight.get(key) is done else None
        )
    response = await asyncio.shield(task)
    if leader:
        return response
    kept = (_TRACEROUTE_MEASURED_AT_HEADER, "cache-control", "hx-redirect")
    headers = {name: value for name, value in response.headers.items() if name.lower() in {h.lower() for h in kept}}
    return HTMLResponse(response.body, status_code=response.status_code, headers=headers)


async def _run_traceroute_fragment(request: Request, target_id: int, lookup_host: str, cache_namespace: str):
    """Run MTR for ``lookup_host``, render the fragment and store it in the cache."""
    # MTR utilities (imports at function level for lazy loading)
    from ..services import route_map_rules
    from ..services.mtr_run import analyze_route, resolve_target_ip, run_mtr
    from ..services.route_interpolator import (
        collapse_interpolated_hops,
        ensure_destination_hop,
        interpolate_route,
    )
    from ..services.traceroute_geo import (
        geolocate_with_hostname_hint,
        is_ipv6_address,
        location_view,
        mtr_hops_to_geo_points,
        route_map_features,
        shorten_ipv6_for_display,
    )

    target_ip = await run_in_threadpool(resolve_target_ip, lookup_host)

    # Keep MTR address family consistent with destination resolution (IPv4 vs IPv6)
    force_ipv6 = None
    if target_ip:
        force_ipv6 = ":" in target_ip
    elif lookup_host and ":" in lookup_host:
        force_ipv6 = True

    # Use 20 hops for international routes
    # Measure the address that was resolved: a host with several A/AAAA records must not be traced to
    # one IP while the result is judged and geolocated against another. The host stays display-only.
    hops = await run_in_threadpool(run_mtr, target_ip or lookup_host, count=3, max_hops=20, force_ipv6=force_ipv6)
    measured_at_ms = time.time_ns() // 1_000_000

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

    route_analysis = analyze_route(hops, target_ip)

    # Get destination geo data for interpolation.
    # Use the hostname hint (POP > GeoIP > online), not plain GeoIP: GeoLite2
    # often has only country + coarse coordinates for the target, so the city
    # has to come from the rDNS/POP inference when available.
    destination_geo = None
    if target_ip:
        destination_geo = await run_in_threadpool(geolocate_with_hostname_hint, target_ip, lookup_host)

    interpolated_hops = interpolate_route(
        hops,
        destination_geo=destination_geo,
    )

    # Ensure destination is in hop list (synthetic if needed)
    if target_ip and not route_analysis["destination_reached"]:
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

    geo_points = mtr_hops_to_geo_points(
        interpolated_hops,
        destination_ip=target_ip,
        destination_geo=destination_geo,
    )

    # Table text and map points come from ONE decision per IP (see location_view): the destination IP
    # uses destination_geo, so the table row and the "D" marker can never name different places.
    mapped_hops = mapped_hop_numbers(geo_points)
    view_by_ip: dict[str, dict] = {}

    enriched_hops = []
    for h in interpolated_hops:
        hop_data = dict(h)
        ip = h.get("ip")
        if ip and ip != "*" and h.get("status") not in ("timeout", "interpolated") and not h.get("_interpolated"):
            if ip not in view_by_ip:
                loc = (
                    destination_geo
                    if target_ip and ip == target_ip and destination_geo
                    else await run_in_threadpool(geolocate_with_hostname_hint, ip, h.get("hostname"))
                )
                view_by_ip[ip] = location_view(loc)
            view = view_by_ip[ip]
            hop_data["city"] = view["city"]
            hop_data["country"] = view["country"]
            hop_data["loc_precision"] = view["precision"]
        try:
            hop_data["on_map"] = int(h.get("hop")) in mapped_hops
        except (TypeError, ValueError):
            hop_data["on_map"] = False

        # IPv6 OPTIMIZATION: Shorten long IPv6 addresses for table display
        ip_raw = hop_data.get("ip", "—")
        hop_data["ip_full"] = ip_raw
        hop_data["ip_display"] = shorten_ipv6_for_display(ip_raw) if ip_raw != "—" else "—"
        hop_data["is_ipv6"] = is_ipv6_address(ip_raw)
        # A hop without reply shows as such, also when the interpolator filled its latency internally.
        silent = timeline_measurement(hop_data) == "timeout"
        hop_data["status_display"] = "timeout" if silent else hop_data.get("status", "timeout")

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

        loss = hop_data.get("loss_pct")
        if loss is not None and loss < 100:
            hop_data["loss_display"] = f"{loss:.0f}%"
        else:
            hop_data["loss_display"] = "–"

        enriched_hops.append(hop_data)

    display_hops = collapse_interpolated_hops(enriched_hops)

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

    sequence_hops = expand_sequence_hops(display_hops)
    route_timeline = _build_route_timeline(sequence_hops, mapped_hops, target_ip, destination_geo_point)

    response = templates.TemplateResponse(
        request,
        "fragments/traceroute_map.html",
        {
            "hops": display_hops,
            "geo_points": geo_points,
            "has_interpolation": has_interpolation,
            "map_features": route_map_features(geo_points),
            "map_style": route_map_rules.map_style(),
            "map_labels": route_map_rules.select_labels(geo_points),
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
            "route_timeline": route_timeline,
            "kpis": _destination_kpis(
                display_hops, target_ip, destination_geo_point, route_analysis["destination_reached"]
            ),
            "mapped_hop_count": len(mapped_hops),
            "destination_on_map": destination_geo_point is not None,
            "labels": STATE_LABELS,
        },
    )
    _cache_traceroute_fragment(cache_namespace, target_id, lookup_host, response.body, measured_at_ms)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers[_TRACEROUTE_MEASURED_AT_HEADER] = str(measured_at_ms)
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

        return HTMLResponse(
            f"""
            <div class="text-center">
                <img src="data:image/png;base64,{qr_b64}"
                     style="max-width: 280px; border-radius: 8px;"
                     alt="Signal QR Code">
                <p class="text-muted small mt-2">
                    <span class="material-icons align-middle me-1" style="font-size: 16px;">phone_android</span>
                    Scan with Signal app to link device
                </p>
            </div>
        """,
            headers=_NO_STORE_HEADERS,
        )
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
                <span class="badge ju-status-badge--success">
                    <span class="material-icons me-1">link</span>Linked ({count} account{"s" if count != 1 else ""})
                </span>
            """)
        return HTMLResponse("""
                <span class="badge ju-status-badge--neutral">
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

    badges = []
    badges.append("""<span class="badge ju-status-badge--success me-1">
        <span class="material-icons align-middle me-1" style="font-size: 14px;">cloud_done</span>Reachable
    </span>""")

    if status.registered:
        badges.append("""<span class="badge ju-status-badge--success me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">verified_user</span>Registered
        </span>""")
    else:
        badges.append("""<span class="badge ju-status-badge--warning me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">person_off</span>Not Registered
        </span>""")

    if status.linked:
        badges.append("""<span class="badge ju-status-badge--success me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">link</span>Linked
        </span>""")
    else:
        badges.append("""<span class="badge ju-status-badge--neutral me-1">
            <span class="material-icons align-middle me-1" style="font-size: 14px;">link_off</span>Not Linked
        </span>""")

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
