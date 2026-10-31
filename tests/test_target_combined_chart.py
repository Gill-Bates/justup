#!/usr/bin/env python3
#
# tests/test_target_combined_chart.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Contracts of the combined response-time / status-code chart on the target detail page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.metrics import _is_allowed_metric
from app.utils.uptime_metric import _PRIORITY

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "templates" / "fragments" / "target_detail" / "charts_and_downtime.html"
PAGE = ROOT / "app" / "templates" / "target_detail.html"
DATA_JS = ROOT / "app" / "static" / "js" / "charts" / "combined-chart-data.js"


@pytest.mark.parametrize(
    "metric",
    ["http_ms", "http_up", "http_status", "ping_ms", "ping_up", "tcp_ms", "tcp_up"],
)
def test_metrics_used_by_the_combined_chart_are_served(metric: str) -> None:
    assert _is_allowed_metric(metric)


def test_latency_source_priority_matches_uptime_metric_selection() -> None:
    js = DATA_JS.read_text(encoding="utf-8")
    js_up = re.findall(r"upMetric: '([a-z_]+)'", js)
    assert js_up == [metric for _flag, metric in _PRIORITY]


def test_canvas_has_image_role_accessible_name_and_text_alternatives() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    canvas = re.search(r'<canvas\b[^>]*id="chart-combined"[^>]*>', html)
    assert canvas, "combined chart canvas missing"
    assert 'role="img"' in canvas.group(0) and "aria-label=" in canvas.group(0)
    for anchor in ('id="combined-summary"', 'id="combined-data-details"', 'id="combined-table-body"'):
        assert anchor in html
    assert 'role="group"' in html and 'id="combined-legend"' in html


def test_empty_states_are_rendered_for_targets_without_checks() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="combined-empty"' in html
    assert "/ui/targets/{{ target.id }}/edit" in html
    assert "{% else %}" in html and "No checks are enabled" in html


def test_page_loads_combined_chart_scripts_after_the_chart_builder() -> None:
    page = PAGE.read_text(encoding="utf-8")
    names = ("chart-builder.js", "combined-chart-data.js", "combined-chart.js", "chart-theme.js")
    order = [page.index(name) for name in names]
    assert order == sorted(order)


def test_performance_details_tile_is_gone_and_donut_sits_next_to_the_chart() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "target-performance" not in html
    for removed in ("chart-response-time", "chart-ping", "chart-tcp"):
        assert f'id="{removed}"' not in html
    canvas = re.search(r'<canvas\b[^>]*id="chart-status-code"[^>]*>', html)
    assert canvas and 'role="img"' in canvas.group(0) and "aria-label=" in canvas.group(0)
    assert re.search(r"enable_http_check %\}\s*<section", html)
    assert 'id="status-donut-empty"' in html and "combined-layout--donut" in html


def test_removed_performance_chart_code_is_not_referenced_anywhere() -> None:
    for name in ("target-detail.js", "charts/chart-builder.js"):
        text = (ROOT / "app" / "static" / "js" / name).read_text(encoding="utf-8")
        assert "target-performance-details" not in text
        assert "buildMultiPortTcpChart" not in text
        assert "function buildChart(" not in text
    css = (ROOT / "app" / "static" / "css" / "target-detail.css").read_text(encoding="utf-8")
    assert "target-performance" not in css
