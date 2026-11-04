#!/usr/bin/env python3
#
# tests/test_status_palette.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001

from __future__ import annotations

import pytest

from app.services import pdf_report
from app.utils.status_palette import STATUS_PALETTE


@pytest.mark.parametrize(
    ("kind", "variant"),
    [
        ("pass", "success"),
        ("ok", "success"),
        ("resolved", "success"),
        ("fail", "danger"),
        ("down", "danger"),
        ("warn", "warning"),
        ("partial", "warning"),
        ("info", "info"),
        ("unknown", "neutral"),
        ("unrecognized", "neutral"),
    ],
)
def test_pdf_status_badge_uses_shared_status_palette(kind: str, variant: str) -> None:
    background, foreground = pdf_report._pdf_status_badge_colors(kind)
    palette = STATUS_PALETTE[variant]

    assert background.hexval() == "0x" + palette.background.removeprefix("#")
    assert foreground.hexval() == "0x" + palette.foreground.removeprefix("#")


@pytest.mark.parametrize("variant", STATUS_PALETTE)
def test_pdf_status_color_constants_match_shared_palette(variant: str) -> None:
    assert pdf_report.COLORS[variant].hexval() == "0x" + STATUS_PALETTE[variant].background.removeprefix("#")
    assert pdf_report.COLORS[f"{variant}_text"].hexval() == "0x" + STATUS_PALETTE[variant].text.removeprefix("#")
