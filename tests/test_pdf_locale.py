#!/usr/bin/env python3
#
# tests/test_pdf_locale.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""PDF formatters match browser Intl output without process-global locale state.

Fixtures contain actual Intl profiles and formatToParts output from pdf-export.js,
using a Gregorian calendar and UTC. Python tests require no JavaScript runtime.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from app.services.pdf_locale import PDFLocale, from_request_headers

PROFILES = json.loads((Path(__file__).parent / "fixtures" / "pdf_locale_profiles.json").read_text())
STAMP = datetime(2026, 2, 5, 7, 3, tzinfo=UTC)


@pytest.mark.parametrize("tag", PROFILES)
def test_numbers_and_percent_follow_browser_intl(tag):
    sample = PROFILES[tag]
    locale = PDFLocale.from_browser(sample["profile"])
    assert locale.format_number(1234567.25, max_fraction=2, min_fraction=2) == sample["expected"]["number"]
    assert locale.format_percent(12.5, 1) == sample["expected"]["percent"]
    assert locale.format_number(1000) == sample["expected"]["fourDigits"]


@pytest.mark.parametrize("tag", PROFILES)
@pytest.mark.parametrize("style", ["time", "date", "monthDay", "monthDayTime", "fullDateTime"])
def test_dates_and_times_follow_browser_intl(tag, style):
    sample = PROFILES[tag]
    locale = PDFLocale.from_browser(sample["profile"])
    assert locale.format_datetime(STAMP, style) == sample["expected"]["dates"][style]


@pytest.mark.parametrize("tag", PROFILES)
def test_ascii_header_round_trip_preserves_browser_profile(tag):
    header = quote(json.dumps(PROFILES[tag]["profile"], ensure_ascii=False))
    assert header.isascii()
    locale = from_request_headers(header, "en-US")
    assert locale.tag == tag
    assert locale.format_number(1234567.25, max_fraction=2, min_fraction=2) == PROFILES[tag]["expected"]["number"]


@pytest.mark.parametrize("header", [None, "%FF", "{", "[]", "x" * 8193])
def test_malformed_or_oversized_profiles_use_safe_default(header):
    locale = from_request_headers(header, "en-US,en;q=0.8")
    assert locale.tag == "en-US"
    assert locale.format_number(12.5, max_fraction=1) == "12.5"


def test_parallel_reports_keep_independent_number_and_time_formats():
    def render(tag):
        locale = PDFLocale.from_browser(PROFILES[tag]["profile"])
        return tag, locale.format_number(1234567.25, max_fraction=2, min_fraction=2), locale.format_datetime(STAMP)

    tags = list(PROFILES) * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(render, tags))
    for tag, number, timestamp in results:
        assert number == PROFILES[tag]["expected"]["number"]
        assert timestamp == PROFILES[tag]["expected"]["dates"]["fullDateTime"]
