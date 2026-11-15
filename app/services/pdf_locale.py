#!/usr/bin/env python3
#
# app/services/pdf_locale.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-report number and date formatting from a browser Intl profile."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote

_DATE_PARTS = frozenset({"literal", "year", "month", "day", "hour", "minute", "dayPeriod"})
_LOCALE_TAG = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")


@dataclass(frozen=True)
class PDFLocale:
    """Immutable format conventions carried with one export job."""

    tag: str
    digits: tuple[str, ...]
    decimal: str
    group: str
    primary_group: int
    secondary_group: int
    minus: str
    percent_prefix: str
    percent_suffix: str
    hour_cycle: str
    am: str
    pm: str
    patterns: dict[str, tuple[tuple[str, str, bool], ...]]
    minimum_grouping_digits: int = 1

    @classmethod
    def default(cls, tag: str = "en-US") -> PDFLocale:
        return cls(
            tag=tag,
            digits=tuple("0123456789"),
            decimal=".",
            group=",",
            primary_group=3,
            secondary_group=3,
            minus="−",
            percent_prefix="",
            percent_suffix="%",
            hour_cycle="h12",
            am="AM",
            pm="PM",
            patterns={
                "time": (
                    ("hour", "05", True),
                    ("literal", ":", False),
                    ("minute", "43", True),
                    ("literal", " ", False),
                    ("dayPeriod", "PM", False),
                ),
                "date": (
                    ("month", "11", True),
                    ("literal", "/", False),
                    ("day", "21", True),
                    ("literal", "/", False),
                    ("year", "2006", False),
                ),
                "monthDay": (("month", "11", True), ("literal", "/", False), ("day", "21", True)),
                "monthDayTime": (
                    ("month", "11", True),
                    ("literal", "/", False),
                    ("day", "21", True),
                    ("literal", ", ", False),
                    ("hour", "05", True),
                    ("literal", ":", False),
                    ("minute", "43", True),
                    ("literal", " ", False),
                    ("dayPeriod", "PM", False),
                ),
                "fullDateTime": (
                    ("month", "11", True),
                    ("literal", "/", False),
                    ("day", "21", True),
                    ("literal", "/", False),
                    ("year", "2006", False),
                    ("literal", ", ", False),
                    ("hour", "05", True),
                    ("literal", ":", False),
                    ("minute", "43", True),
                    ("literal", " ", False),
                    ("dayPeriod", "PM", False),
                ),
            },
        )

    @classmethod
    def from_browser(cls, payload: Any) -> PDFLocale:
        if not isinstance(payload, dict) or len(payload) > 8:
            raise ValueError("invalid locale profile")
        tag = payload.get("locale")
        if not isinstance(tag, str) or len(tag) > 64 or not _LOCALE_TAG.fullmatch(tag):
            raise ValueError("invalid locale tag")
        number = payload.get("number")
        dates = payload.get("dates")
        if not isinstance(number, dict) or not isinstance(dates, dict):
            raise ValueError("locale profile is incomplete")
        digits = number.get("digits")
        if (
            not isinstance(digits, list)
            or len(digits) != 10
            or any(not isinstance(digit, str) or not digit or len(digit) > 4 for digit in digits)
        ):
            raise ValueError("invalid locale digits")
        integer_groups = number.get("integerGroups")
        minimum_grouping_digits = number.get("minimumGroupingDigits", 1)
        if type(minimum_grouping_digits) is not int or not 1 <= minimum_grouping_digits <= 4:
            raise ValueError("invalid minimum grouping digits")
        if (
            not isinstance(integer_groups, list)
            or not integer_groups
            or len(integer_groups) > 8
            or any(type(size) is not int or size < 1 or size > 4 for size in integer_groups)
        ):
            raise ValueError("invalid locale grouping")
        separators = [number.get(key) for key in ("decimal", "group", "minus", "percentPrefix", "percentSuffix")]
        if any(not isinstance(value, str) or len(value) > 8 for value in separators):
            raise ValueError("invalid locale separators")
        hour_cycle = payload.get("hourCycle")
        if hour_cycle not in {"h11", "h12", "h23", "h24"}:
            raise ValueError("invalid hour cycle")
        periods = payload.get("dayPeriod")
        if not isinstance(periods, dict) or any(
            not isinstance(periods.get(key), str) or len(periods[key]) > 16 for key in ("am", "pm")
        ):
            raise ValueError("invalid day period")
        patterns: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        for name in ("time", "date", "monthDay", "monthDayTime", "fullDateTime"):
            raw = dates.get(name)
            if not isinstance(raw, list) or not raw or len(raw) > 16:
                raise ValueError("invalid date pattern")
            parts = []
            for part in raw:
                if not isinstance(part, dict):
                    raise ValueError("invalid date part")
                kind, sample, pad = part.get("type"), part.get("sample"), part.get("pad", False)
                if kind not in _DATE_PARTS or not isinstance(sample, str) or len(sample) > 16 or type(pad) is not bool:
                    raise ValueError("invalid date part")
                parts.append((kind, sample, pad))
            patterns[name] = tuple(parts)
        return cls(
            tag=tag,
            digits=tuple(digits),
            decimal=separators[0],
            group=separators[1],
            minus=separators[2],
            primary_group=integer_groups[-1],
            secondary_group=integer_groups[-2] if len(integer_groups) > 1 else integer_groups[-1],
            percent_prefix=separators[3],
            percent_suffix=separators[4],
            hour_cycle=hour_cycle,
            am=periods["am"],
            pm=periods["pm"],
            patterns=patterns,
            minimum_grouping_digits=minimum_grouping_digits,
        )

    def format_number(
        self,
        value: int | float | Decimal,
        *,
        max_fraction: int = 0,
        min_fraction: int = 0,
        grouping: bool = True,
        force_sign: bool = False,
    ) -> str:
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                return "n/a"
            quantum = Decimal(1).scaleb(-max_fraction)
            number = number.quantize(quantum, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return "n/a"
        negative = number < 0
        absolute = abs(number)
        raw = f"{absolute:.{max_fraction}f}"
        integer, _, fraction = raw.partition(".")
        fraction = fraction.rstrip("0")
        if len(fraction) < min_fraction:
            fraction += "0" * (min_fraction - len(fraction))
        if grouping and self.group and len(integer) >= self.primary_group + self.minimum_grouping_digits:
            pieces = []
            size = self.primary_group
            while len(integer) > size:
                pieces.append(integer[-size:])
                integer = integer[:-size]
                size = self.secondary_group
            pieces.append(integer)
            integer = self.group.join(reversed(pieces))
        result = integer + (self.decimal + fraction if fraction else "")
        result = result.translate(str.maketrans({str(digit): localized for digit, localized in enumerate(self.digits)}))
        if negative:
            return self.minus + result
        return ("+" if force_sign else "") + result

    def format_percent(self, value: float, places: int = 1, *, force_sign: bool = False) -> str:
        return (
            self.percent_prefix
            + self.format_number(value, max_fraction=places, min_fraction=places, force_sign=force_sign)
            + self.percent_suffix
        )

    def format_datetime(self, value, style: str = "fullDateTime") -> str:
        parts = self.patterns.get(style) or self.default().patterns[style]
        local = value
        output = []
        for kind, sample, pad in parts:
            if kind == "literal":
                output.append(sample)
                continue
            if kind == "year":
                raw = self.format_number(local.year, grouping=False)
            elif kind == "month":
                raw = self.format_number(local.month, grouping=False)
            elif kind == "day":
                raw = self.format_number(local.day, grouping=False)
            elif kind == "hour":
                hour = local.hour
                if self.hour_cycle in {"h11", "h12"}:
                    hour = hour % 12 or (0 if self.hour_cycle == "h11" else 12)
                elif self.hour_cycle == "h24":
                    hour = hour or 24
                raw = self.format_number(hour, grouping=False)
            elif kind == "minute":
                raw = self.format_number(local.minute, grouping=False)
            elif kind == "dayPeriod":
                raw = self.am if local.hour < 12 else self.pm
            else:
                raw = sample
            if pad and kind in {"year", "month", "day", "hour", "minute"} and len(raw) < len(sample):
                raw = self.digits[0] + raw
            output.append(raw)
        return "".join(output)


DEFAULT_PDF_LOCALE = PDFLocale.default()


def from_request_headers(locale_profile: str | None, accept_language: str | None) -> PDFLocale:
    """Load a bounded browser Intl profile; use Accept-Language for the locale tag fallback."""
    fallback = "en-US"
    if accept_language:
        candidate = accept_language.split(",", 1)[0].split(";", 1)[0].strip()
        if len(candidate) <= 64 and _LOCALE_TAG.fullmatch(candidate):
            fallback = candidate
    if locale_profile and len(locale_profile) <= 8192:
        try:
            decoded = unquote(locale_profile, errors="strict")
            if len(decoded) > 4096:
                return PDFLocale.default(fallback)
            return PDFLocale.from_browser(json.loads(decoded))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    return PDFLocale.default(fallback)
