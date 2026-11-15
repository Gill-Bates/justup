#!/usr/bin/env python3
#
# tests/test_gui_decisions_103.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""1.0.3 decisions D11, D12 and D16: shared marker palette, dashboard label, shared PDF axis style."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services import pdf_report, pdf_route_map
from app.services import route_map_rules as rules
from tests.test_traceroute_map_markup import _render

ROOT = Path(__file__).resolve().parent.parent


def test_marker_palettes_are_central_and_deliberately_different() -> None:
    web, print_ = rules.PALETTE["markers"]["web"], rules.PALETTE["markers"]["print"]
    assert web.keys() == print_.keys() == {"start", "hop", "end"}
    assert web != print_


def test_web_fragment_consumes_the_shared_marker_palette() -> None:
    markup = _render()
    for key, value in rules.PALETTE["markers"]["web"].items():
        assert f"--tr-marker-{key}: {value}" in markup
    css = (ROOT / "app/static/css/traceroute-map.css").read_text(encoding="utf-8")
    assert not re.search(r"--tr-marker-(start|hop|end):\s*#", css), "stylesheet must not redefine the markers"
    assert "var(--tr-marker-start)" in css


def test_pdf_consumes_the_shared_print_marker_palette() -> None:
    print_ = rules.PALETTE["markers"]["print"]
    assert pdf_route_map._START.hexval()  # noqa: SLF001 == "0x" + print_["start"][1:].lower()
    assert pdf_route_map._INK.hexval()  # noqa: SLF001 == "0x" + print_["hop"][1:].lower()
    assert pdf_route_map._DEST.hexval()  # noqa: SLF001 == "0x" + print_["end"][1:].lower()
    source = (ROOT / "app/services/pdf_route_map.py").read_text(encoding="utf-8")
    for value in print_.values():
        assert value not in source, "pdf_route_map must not repeat a marker colour literal"


def test_downtime_widget_label_is_consistent() -> None:
    label = "Downtime overview (24h)"
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    registry = (ROOT / "app/static/js/dashboard-widgets.js").read_text(encoding="utf-8")
    assert f"widget: {label}." in template
    assert f"{label}</span>" in template
    assert f'aria-label="{label} table"' in template
    assert f'aria-label="{label} pages"' in template
    assert f"'{label}'" in registry
    assert "Downtime Overview" not in template + registry


def test_pdf_performance_charts_are_native_reportlab_flowables() -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    points = [{"ts": start.isoformat(), "value": 12.5}, {"ts": end.isoformat(), "value": 18.75}]
    line = pdf_report._create_response_time_chart(points, start, end)  # noqa: SLF001
    uptime = pdf_report._create_uptime_chart(  # noqa: SLF001
        [{"ts": start.isoformat(), "value": 1}, {"ts": end.isoformat(), "value": 0}], start, end
    )
    coverage = pdf_report._create_coverage_bar(  # noqa: SLF001
        [{"ts": start.isoformat(), "value": 1}], start, end
    )
    assert isinstance(line, pdf_report.pdf_charts.ChartFlowable)
    assert isinstance(uptime, pdf_report.pdf_charts.ChartFlowable)
    assert isinstance(coverage, pdf_report.pdf_charts.ChartFlowable)
