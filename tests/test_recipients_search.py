#!/usr/bin/env python3
#
# tests/test_recipients_search.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Recipients tab search: field markup, live region, empty state and shared filter core."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "app" / "templates" / "settings" / "recipients.html").read_text(encoding="utf-8")
SETTINGS = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "app" / "static" / "js" / "settings.recipients.js").read_text(encoding="utf-8")


def test_search_field_has_label_and_shared_component_classes() -> None:
    assert re.search(r'<div class="ui-search"[^>]*role="search"', TEMPLATE)
    assert re.search(r'<label class="visually-hidden" for="recipients-search">Search recipients</label>', TEMPLATE)
    field = re.search(r'<input[^>]*id="recipients-search"[^>]*>', TEMPLATE, re.S)
    assert field is not None
    for attr in ('type="search"', 'class="form-control ui-search-input"', 'maxlength="100"', 'autocomplete="off"'):
        assert attr in field.group(0)
    assert 'id="recipients-search-clear"' in TEMPLATE
    assert 'aria-label="Clear search"' in TEMPLATE


def test_live_region_and_empty_state_with_reset() -> None:
    assert re.search(r'id="recipients-search-status"[^>]*role="status"[^>]*aria-live="polite"', TEMPLATE) or re.search(
        r'role="status"[^>]*aria-live="polite"[^>]*id="recipients-search-status"', TEMPLATE
    )
    assert re.search(r'class="ui-empty[^"]*"[^>]*id="recipients-search-empty"[^>]*hidden', TEMPLATE)
    assert "No recipients match" in TEMPLATE
    assert 'id="recipients-search-reset"' in TEMPLATE


def test_rows_carry_search_text_and_use_the_shared_filter_core() -> None:
    assert "data-search=" in SCRIPT
    assert "JuListFilter" in SCRIPT
    assert "filterRows" in SCRIPT
    # The core is loaded before the recipients script, and the page keeps no second copy of the matching.
    assert SETTINGS.index("utils/list-filter.js") < SETTINGS.index("settings.recipients.js")
    assert "normalize(" not in SCRIPT


def test_search_text_is_not_written_to_url_or_storage() -> None:
    block = SCRIPT[SCRIPT.index("Recipients search") :]
    assert "localStorage" not in block
    assert "history." not in block
    assert "location" not in block
