#!/usr/bin/env python3
#
# tests/test_availability_format.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Availability is never displayed as 100 % unless it is exactly 100; other percentages are unchanged."""

from __future__ import annotations

import pytest

from app.utils.formatters import format_availability, format_percent
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "–"),
        (100.0, "100%"),
        (99.999, "99.99%"),
        (99.99, "99.99%"),
        (99.96, "99.99%"),
        (99.95, "99.99%"),
        (99.9, "99.9%"),
        (50.0, "50.0%"),
        (5.0, "5.00%"),
    ],
)
def test_format_availability_caps_incomplete_series(value, expected):
    assert format_availability(value) == expected


def test_format_percent_for_other_metrics_is_unchanged():
    assert format_percent(99.96) == "100%"
    assert format_percent(99.9) == "99.9%"
    assert format_percent(None) == "–"
