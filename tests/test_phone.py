#!/usr/bin/env python3
#
# tests/test_phone.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""E.164 normalization: ASCII digits only, formatting characters tolerated."""

from __future__ import annotations

import pytest

from app.utils.phone import normalize_phone
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+4915112345678", "+4915112345678"),
        ("0049 151 1234-5678", "+4915112345678"),
        ("(0151) 12345678", "+4915112345678"),
        ("+1 (415) 555-0100", "+14155550100"),
    ],
)
def test_ascii_numbers_with_formatting_are_normalized(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "+\u0664\u0661\u0665\u0665\u0665\u0665\u0665\u0660\u0661\u0660\u0660",  # Arabic-Indic only
        "+4915\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668",  # mixed digit systems
        "+49\uff11\uff15\uff11\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",  # fullwidth
    ],
)
def test_non_ascii_digits_are_rejected(raw):
    with pytest.raises(ValueError, match="Non-ASCII"):
        normalize_phone(raw)
    assert normalize_phone(raw, strict=False) == raw


def test_trailing_newline_is_not_part_of_the_number():
    assert normalize_phone("+4915112345678\n") == "+4915112345678"


@pytest.mark.parametrize("raw", ["+0123456789", "+12345", "+1234567890123456", "abc"])
def test_invalid_e164_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "+4915112345678 ext 9",
        "+4915112345678 x9",
        "+4915112345678x9",
        "+49 151 1234 5678 ext. 12",
        "+49151abc12345678",
        "+49151\u00a012345678",  # NBSP is not a listed separator
        "+49151\u200b12345678",  # zero-width space
        "+49151,12345678",
        "+49151;12345678",
        "+49151#12345678",
        "+49151*12345678",
        "+49151\n12345678",
        "0151 1234 5678 p9",
    ],
)
def test_unsupported_characters_are_rejected_not_dropped(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)
    # strict=False stays tolerant: the input is returned unchanged, never silently altered.
    assert normalize_phone(raw, strict=False) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+49 151 1234-5678", "+4915112345678"),
        ("+49.151.12345678", "+4915112345678"),
        ("0151/12345678", "+4915112345678"),
        ("\t+49 151 12345678 ", "+4915112345678"),
    ],
)
def test_listed_separators_remain_accepted(raw, expected):
    assert normalize_phone(raw) == expected
