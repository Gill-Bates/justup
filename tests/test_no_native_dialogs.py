#!/usr/bin/env python3
#
# tests/test_no_native_dialogs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The UI never opens the browser's own dialogs: confirmations use confirmAction, errors sit at the field."""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
# "alert(s)" in a message text is prose, not a call.
NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(?:alert|confirm|prompt)\s*\((?!s\))")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"(?m)(?<![:\"'])//.*$")
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _own_sources() -> list[Path]:
    """Our scripts (marked by the project's copyright header, vendor files are not) and every template."""
    scripts = [
        p
        for p in (APP / "static" / "js").rglob("*.js")
        if ".min." not in p.name and "Gill-Bates" in p.read_text(encoding="utf-8")[:400]
    ]
    return scripts + sorted((APP / "templates").rglob("*.html"))


def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    for pattern in (BLOCK_COMMENT, JINJA_COMMENT, HTML_COMMENT, LINE_COMMENT):
        text = pattern.sub("", text)
    return text


def test_the_scan_sees_our_scripts_and_templates() -> None:
    names = {p.name for p in _own_sources()}
    assert {"confirm-dialog.js", "settings.smtp-template.js", "base.html"} <= names
    assert "htmx.min.js" not in names


def test_no_alert_confirm_or_prompt_in_scripts_or_templates() -> None:
    offenders = [f"{p.relative_to(APP)}: {m.group(0)}" for p in _own_sources() for m in NATIVE.finditer(_code(p))]
    assert offenders == []
