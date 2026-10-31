#!/usr/bin/env python3
#
# app/utils/phone.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Deterministic phone number normalization (E.164)."""

from __future__ import annotations

import re

_E164_RE = re.compile(r"\+[1-9][0-9]{7,14}\Z")


def normalize_phone(
    phone: str,
    default_country: str = "+49",
    *,
    strict: bool = True,
) -> str:
    """
    Normalize a phone number to E.164.

    Rules (deterministic, no heuristics):
    - '+' prefix   -> treated as international
    - '00' prefix  -> converted to '+'
    - otherwise    -> default_country is prepended

    Args:
        phone: Input phone number
        default_country: Country code with leading '+'
        strict: If True, invalid formats raise ValueError

    Returns:
        Phone number in E.164 format

    Raises:
        ValueError: On invalid phone numbers (strict=True)
    """
    if not phone:
        if strict:
            raise ValueError("Empty phone number")
        return phone

    # Non-ASCII digits (e.g. Arabic-Indic) are rejected, never dropped or mapped silently
    if any(ch.isdigit() and not ch.isascii() for ch in phone):
        if strict:
            raise ValueError(f"Non-ASCII digits in phone number: {phone}")
        return phone
    # Keep ASCII digits and '+'
    p = re.sub(r"[^0-9+]", "", phone.strip())

    # '+' must appear only once and only at the beginning
    if p.count("+") > 1 or ("+" in p and not p.startswith("+")):
        if strict:
            raise ValueError(f"Invalid '+' placement: {phone}")
        return phone

    # Convert 00-prefix to +
    if p.startswith("00"):
        p = "+" + p[2:]

    # Local number -> prepend default country
    if not p.startswith("+"):
        if not default_country.startswith("+"):
            raise ValueError("default_country must start with '+'")
        p = default_country + p.lstrip("0")

    # Final E.164 validation
    if not _E164_RE.fullmatch(p):
        if strict:
            raise ValueError(f"Invalid E.164 phone number: {p}")
        return p

    return p


def mask_phone(number: str | None) -> str:
    """Mask a phone number for logs/messages: first 3 + "****" + last 2; short or empty -> "***"."""
    if not number or len(number) < 6:
        return "***"
    return number[:3] + "****" + number[-2:]
