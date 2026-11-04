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
    owned = (
        "ui",
        "gentelella-layout",
        "dashboard",
        "traceroute-map",
        "target-detail",
        "notifications",
        "target-wizard",
    )
    offenders = [name for name in owned if external.search((css_dir / f"{name}.css").read_text(encoding="utf-8"))]
    assert not offenders, f"external runtime assets in: {offenders}"
