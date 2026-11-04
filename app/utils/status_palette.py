#!/usr/bin/env python3
#
# app/utils/status_palette.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared status colors for generated PDF and email output."""

from typing import NamedTuple


class StatusColor(NamedTuple):
    background: str
    foreground: str
    text: str


STATUS_PALETTE = {
    "success": StatusColor("#198754", "#ffffff", "#146c43"),
    "warning": StatusColor("#ffc107", "#000000", "#8a5a00"),
    "danger": StatusColor("#dc3545", "#ffffff", "#b02a37"),
    "info": StatusColor("#0dcaf0", "#000000", "#055160"),
    "neutral": StatusColor("#6c757d", "#ffffff", "#5b6b7b"),
}
