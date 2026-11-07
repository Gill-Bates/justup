#!/usr/bin/env python3
#
# tests/test_notification_category_icons.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification row icons: one category map on the server, mirrored by the bell popover script."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.frontend import NOTIFICATION_CATEGORY_ICONS, NOTIFICATION_TABS, templates
from tests import _support  # noqa: F401 - imported for its sys.path side effect

_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "notifications.js"


def test_icon_map_covers_every_category_tab():
    keys = {key for key, _label, _icon in NOTIFICATION_TABS} - {"all", "unread"}
    assert set(NOTIFICATION_CATEGORY_ICONS) == keys


def test_server_and_script_icon_maps_are_identical():
    body = re.search(r"var CATEGORY_ICONS = \{(.*?)\};", _JS.read_text(encoding="utf-8"), re.DOTALL).group(1)
    assert dict(re.findall(r"(\w+):\s*'([^']+)'", body)) == NOTIFICATION_CATEGORY_ICONS


def _icon_for(category: str) -> str:
    item = SimpleNamespace(
        id=1, category=category, severity="success", title="T", body="", actor="", action=None,
        is_read=True, created_at="2026-11-07T10:00:00+00:00", resource=None,
    )
    template = templates.env.get_template("notifications.html")
    context = template.new_context(
        {
            "items": [item], "tabs": NOTIFICATION_TABS, "category": "all", "severity": "",
            "category_icons": NOTIFICATION_CATEGORY_ICONS, "total": 1, "page": 1, "total_pages": 1,
            "page_size": 20, "query_string": "", "unread_total": 0, "can_close_alerts": False, "user": None,
        }
    )
    html = "".join(template.blocks["content"](context))
    return re.search(r'ju-notif-icon" aria-hidden="true">(\w+)<', html).group(1)


@pytest.mark.parametrize("category", [*NOTIFICATION_CATEGORY_ICONS, "something_else"])
def test_row_icon_follows_the_category_map(category):
    assert _icon_for(category) == NOTIFICATION_CATEGORY_ICONS.get(category, "notifications")
