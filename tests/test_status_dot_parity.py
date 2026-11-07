#!/usr/bin/env python3
#
# tests/test_status_dot_parity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server (Jinja) and client (JuStatusDot) producers of status dots must render the same dot."""

from __future__ import annotations

import html as html_lib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.frontend import templates
from tests import _support  # noqa: F401 - imported for its sys.path side effect

_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
_TONES = ["success", "warning", "danger", "info", "neutral", "bogus"]
_DOT_CLASS = re.compile(r'<span class="([^"]*ju-status-dot[^"]*)"')


def _node(script: str, payload) -> object:
    result = subprocess.run(
        ["node", "-e", script, json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _macro_html(tone: str, label: str, title: str = "") -> str:
    module = templates.env.get_template("fragments/_state_dot.html").module
    return str(module.status_dot(tone, label, title)).strip()


def _classes(html: str) -> set[str]:
    return set(_DOT_CLASS.search(html).group(1).split())


def _label(html: str) -> str:
    return re.search(r'aria-hidden="true"></span>(.*?)</span>$', html, re.DOTALL).group(1)


_DOT_JS = """
const vm = require('node:vm'), fs = require('node:fs');
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(__SRC__, 'utf8'), ctx);
const rows = JSON.parse(process.argv[1]);
console.log(JSON.stringify(rows.map(([tone, label, title]) =>
  ctx.JuStatusDot.html(tone, label, title ? { title } : {}))));
"""


def _js_dots(rows: list[list[str]]) -> list[str]:
    script = _DOT_JS.replace("__SRC__", json.dumps(str(_JS / "utils" / "status-badge.js")))
    return _node(script, rows)


@pytest.mark.parametrize("tone", _TONES)
def test_macro_and_js_helper_render_the_same_classes(tone):
    macro = _macro_html(tone, "Label")
    js = _js_dots([[tone, "Label", ""]])[0]
    assert _classes(macro) == _classes(js)
    # The hollow ring (--unknown) belongs to neutral and to the fallback only.
    expects_ring = tone in ("neutral", "bogus")
    assert ("ju-status-dot--unknown" in _classes(macro)) is expects_ring


def test_label_and_title_are_escaped_by_both_producers():
    label, title = '<b>"x"</b>', 'a"b<c'
    macro = _macro_html("success", label, title)
    js = _js_dots([["success", label, title]])[0]
    for html in (macro, js):
        assert "<b>" not in html
        assert 'title="a"b' not in html
        assert "&lt;b&gt;" in html
    assert html_lib.unescape(_label(macro)) == html_lib.unescape(_label(js)) == label


# SMTP status: settings/smtp.html (server) mirrors updateSmtpStatusBadge (settings.storage.js)

_SMTP_JS = """
const vm = require('node:vm'), fs = require('node:fs');
const dots = fs.readFileSync(%s, 'utf8');
const src = fs.readFileSync(%s, 'utf8');
const m = src.match(/function updateSmtpStatusBadge[\\s\\S]*?\\n\\}\\n/);
const badge = { innerHTML: '' };
const ctx = vm.createContext({ DOM: { smtpStatusBadge: () => badge } });
vm.runInContext(dots, ctx);
vm.runInContext(m[0] + '\\nthis.run = updateSmtpStatusBadge;', ctx);
console.log(JSON.stringify(JSON.parse(process.argv[1]).map((p) => { ctx.run(p); return badge.innerHTML; })));
"""

_SMTP_STATES = [
    {"smtp_enabled": False, "smtp_host": "h", "smtp_from": "f"},
    {"smtp_enabled": True, "smtp_host": "h", "smtp_from": "f"},
    {"smtp_enabled": True, "smtp_host": "", "smtp_from": ""},
]


def _server_smtp_badge(payload: dict) -> str:
    html = templates.env.get_template("settings/smtp.html").render(
        smtp=SimpleNamespace(**payload), template=SimpleNamespace(subject="", html="")
    )
    return re.search(r'id="smtp-status-badge">(.*?)</div>', html, re.DOTALL).group(1).strip()


def test_smtp_status_badge_matches_the_client_update():
    script = _SMTP_JS % (
        json.dumps(str(_JS / "utils" / "status-badge.js")),
        json.dumps(str(_JS / "settings.storage.js")),
    )
    client = _node(script, _SMTP_STATES)
    for payload, client_html in zip(_SMTP_STATES, client, strict=True):
        server_html = _server_smtp_badge(payload)
        assert _classes(server_html) == _classes(client_html), payload
        assert _label(server_html) == _label(client_html), payload
