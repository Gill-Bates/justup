#!/usr/bin/env python3
#
# tests/test_ui_layout_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static CSS contracts shared with the browser layout suite."""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def test_application_css_loads_no_external_assets() -> None:
    """DESIGN.md: CSS, fonts and icons are local; no stylesheet fetches an asset at runtime."""
    css_dir = _REPO / "app" / "static" / "css"
    external = re.compile(r"(?:url\(\s*|@import\s+)[\"']?(?:https?:)?//", re.IGNORECASE)
    sheets = [p for p in css_dir.glob("*.css") if ".min." not in p.name]
    assert sheets, "no application stylesheet found"
    offenders = [p.name for p in sheets if external.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"external runtime assets in: {offenders}"


def test_dashboard_downtime_link_preset_exists_on_target_detail() -> None:
    """The widget's range preset must be one the target page's time-range select (and parser) offers."""
    widget = (_REPO / "app/static/js/dashboard.js").read_text(encoding="utf-8")
    selector = (_REPO / "app/templates/fragments/target_detail/time_range_selector.html").read_text(encoding="utf-8")
    match = re.search(r"LOOKBACK_RANGE_PRESET = '([a-z0-9]+)'", widget)
    assert match is not None, "dashboard.js must name the preset its downtime links carry"
    assert f'<option value="{match.group(1)}"' in selector
