#!/usr/bin/env python3
#
# tests/test_status_badge_no_icon.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Central guard (DESIGN.md, "Badges"): a status badge never contains an icon, in Jinja or in JS."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.api.frontend import templates
from tests import _support  # noqa: F401 - imported for its sys.path side effect

APP = Path(__file__).resolve().parent.parent / "app"
ICON = re.compile(r"material-icons|<i\b|<svg\b", re.IGNORECASE)
# A source line that builds a status badge and an icon at once.
STATUS_BADGE = re.compile(r"ju-status-badge|ju-state-badge|ju-status-item|ju-status-dot|StatusBadge")


def test_jinja_state_and_cert_dots_have_no_icon_for_every_status() -> None:
    env = templates.env
    for status in ("up", "down", "unknown", "paused", None, ""):
        html = env.from_string(
            '{% from "fragments/_state_dot.html" import state_dot %}{{ state_dot("TCP:22", status, "tip") }}'
        ).render(status=status)
        assert not ICON.search(html), html
        assert "TCP:22" in html and 'class="visually-hidden">: ' in html
        assert ("ju-status-dot--unknown" in html) == (status not in ("up", "down"))
    for status in ("ok", "warning", "critical", "expired", "unknown", None):
        html = env.from_string(
            '{% from "fragments/_state_dot.html" import cert_dot %}{{ cert_dot("CERT", status) }}'
        ).render(status=status)
        assert not ICON.search(html), html


def _scanned_sources() -> list[Path]:
    """Templates, scripts and Python sources (server-built fragments included; DC-BADGE-4)."""
    sources = [
        *APP.joinpath("templates").rglob("*.html"),
        *APP.joinpath("static", "js").rglob("*.js"),
        *APP.rglob("*.py"),
    ]
    return [path for path in sources if not (path.name.endswith(".min.js") or "bundle" in path.name)]


# A status badge producer or consumer; the macro name `status_badge` of a template is not one.
STATUS_BADGE_USE = re.compile(r"ju-status-badge--|JuStatusBadge")
# DC-BADGE-4 / DC-EX-STATE-2: the helper itself and the port-scan result badges are the only users.
STATUS_BADGE_ALLOWLIST = {"static/js/utils/status-badge.js", "static/js/network-scan-picker.js"}


def test_status_badges_exist_only_in_the_allowlist() -> None:
    users = {
        path.relative_to(APP).as_posix()
        for path in _scanned_sources()
        if STATUS_BADGE_USE.search(path.read_text(encoding="utf-8"))
    }
    assert users <= STATUS_BADGE_ALLOWLIST, (
        f"status badge outside DC-EX-STATE-2: {sorted(users - STATUS_BADGE_ALLOWLIST)}"
    )


def test_templates_and_scripts_never_put_an_icon_into_a_status_badge() -> None:
    offenders = []
    for path in _scanned_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if STATUS_BADGE.search(line) and ICON.search(line):
                offenders.append(f"{path.relative_to(APP)}:{number}")
    assert not offenders, f"status badge with an icon: {offenders}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_shared_js_status_badge_helper_renders_no_icon_and_takes_no_icon_parameter() -> None:
    helper = APP / "static" / "js" / "utils" / "status-badge.js"
    assert helper.is_file(), "the one shared JS status badge helper is missing"
    source = helper.read_text(encoding="utf-8")
    assert not re.search(r"\bicon\b|material", source, re.IGNORECASE), "the helper must not know icons"
    script = (
        "const vm=require('vm'),fs=require('fs');const w={};"
        "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window:w,globalThis:w});"
        "const B=w.JuStatusBadge;const out=[];"
        "for(const tone of ['success','warning','danger','info','neutral','bogus']){"
        "out.push(B.html(tone,'Label<x>',{title:'t\"q',unknown:tone==='neutral',className:'extra'}));"
        "out.push(B.classes(tone,{unknown:true}));}"
        "console.log(JSON.stringify(out));"
    )
    result = subprocess.run(["node", "-e", script, str(helper)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    for markup in json.loads(result.stdout):
        assert not ICON.search(markup), markup
    assert "&lt;x&gt;" in json.loads(result.stdout)[0] and "t&quot;q" in json.loads(result.stdout)[0]
