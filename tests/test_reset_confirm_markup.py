#!/usr/bin/env python3
#
# tests/test_reset_confirm_markup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Reset metrics and delete target confirm through the shared design-contract dialog, never a native popup."""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP / "templates"
TABLE = (TEMPLATES / "fragments" / "targets_table.html").read_text(encoding="utf-8")
HEADER = (TEMPLATES / "fragments" / "target_detail" / "header.html").read_text(encoding="utf-8")
BASE = (TEMPLATES / "base.html").read_text(encoding="utf-8")


def _buttons(markup: str, verb_url: str) -> list[str]:
    return [tag for tag in re.findall(r"<button\b[^>]*>", markup, re.DOTALL) if verb_url in tag]


def test_no_template_uses_the_native_confirm_popup() -> None:
    offenders = [p.name for p in TEMPLATES.rglob("*.html") if "hx-confirm" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_reset_and_delete_buttons_carry_the_data_for_the_dialog() -> None:
    for name, markup in (("targets table", TABLE), ("target header", HEADER)):
        for url in ("/reset", "hx-delete="):
            tags = _buttons(markup, url)
            assert tags, f"{name}: no button for {url}"
            for tag in tags:
                assert "data-ju-confirm" in tag, f"{name}: {tag}"
                assert 'data-confirm-title="' in tag and 'data-confirm-message="' in tag, f"{name}: {tag}"
                assert 'data-confirm-label="' in tag, f"{name}: {tag}"


def test_dialog_message_names_the_target_and_the_consequence() -> None:
    for markup in (TABLE, HEADER):
        reset = _buttons(markup, "/reset")[0]
        message = re.search(r'data-confirm-message="([^"]*)"', reset).group(1)
        assert "name" in message and "permanently" in message and "target itself stays" in message


def test_the_shared_script_is_loaded_after_the_dialog_it_uses() -> None:
    assert BASE.index("utils/confirm-dialog.js") < BASE.index("utils/htmx-confirm.js")
    assert (APP / "static" / "js" / "utils" / "htmx-confirm.js").exists()
