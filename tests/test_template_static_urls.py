#!/usr/bin/env python3
#
# tests/test_template_static_urls.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Every literal /static/... URL in a template must point at a file under app/static.

A template that references a script or stylesheet before the file exists (or after it was
renamed) only fails in the browser as a 404 console error; this catches it at test time.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP / "templates"
STATIC = APP / "static"

# The URL ends at a quote, whitespace, parenthesis, angle bracket, query string or fragment, so
# src/href attributes, import statements and inline-JS string literals are all matched alike.
_STATIC_URL = re.compile(r"""/static/([^"'\s()<>?#`]*)""")
# A Jinja expression or statement inside the URL makes it dynamic: its value only exists at render
# time, so no file can be resolved statically. Such URLs are exempt, not guessed at.
_DYNAMIC = ("{{", "{%", "{#")


def _references() -> list[tuple[Path, int, str]]:
    found = []
    for template in sorted(TEMPLATES.rglob("*")):
        if not template.is_file():
            continue
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), start=1):
            for match in _STATIC_URL.finditer(line):
                relative = match.group(1)
                # A trailing slash is a prefix that JS concatenates with a runtime value.
                if any(token in relative for token in _DYNAMIC) or relative.endswith("/") or not relative:
                    continue
                found.append((template, number, relative))
    return found


def test_scanner_finds_static_references() -> None:
    # Guards the scanner itself: an empty result would make the existence test below pass vacuously.
    assert len(_references()) > 20


@pytest.mark.parametrize(
    "template,number,relative",
    [pytest.param(*ref, id=f"{ref[0].relative_to(TEMPLATES)}:{ref[1]}") for ref in _references()],
)
def test_static_url_points_at_existing_file(template: Path, number: int, relative: str) -> None:
    target = (STATIC / unquote(relative)).resolve()
    assert target.is_relative_to(STATIC.resolve()), f"{template.name}:{number} escapes app/static: /static/{relative}"
    assert target.is_file(), f"{template.relative_to(APP.parent)}:{number} references missing /static/{relative}"
