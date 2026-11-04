#!/usr/bin/env python3
#
# tests/test_port_picker_markup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Port picker markup contract: accessible controls, live regions, script order, no HTML sinks for scanner text."""

from __future__ import annotations

import re
from pathlib import Path

from app.api.frontend import templates
from tests import _support  # noqa: F401 - imported for its sys.path side effect

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "app" / "static" / "js"
TEMPLATES = ROOT / "app" / "templates"


def picker_html(prefix: str = "edit") -> str:
    return templates.env.get_template("fragments/network_scan_picker.html").render(
        scan_prefix=prefix,
        scan_host_id="host",
        scan_tcp_id="tcp",
        scan_udp_id="udp",
        scan_tcp_toggle_id="tcp-on",
        scan_udp_toggle_id="udp-on",
    )


def test_picker_controls_have_types_names_and_unique_ids() -> None:
    html = picker_html()
    for tag in re.findall(r"<button\b[^>]*>", html):
        assert 'type="button"' in tag, f"a button inside a form must not submit it: {tag}"
    ids = re.findall(r'(?<![\w-])id="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate ids"
    for control in re.findall(r'<(?:input|select)\b[^>]*id="([^"]+)"', html):
        assert re.search(rf'for="{control}"', html), f"{control} has no label"
    assert html.count('name="network_scan_id"') == 1
    assert 'type="hidden" name="network_scan_id"' in html


def test_picker_announces_progress_politely_and_errors_assertively() -> None:
    html = picker_html()
    status = re.search(r"<p[^>]*data-scan-status[^>]*>", html).group(0)
    assert 'role="status"' in status and 'aria-live="polite"' in status
    error = re.search(r"<p[^>]*data-scan-error[^>]*>", html).group(0)
    assert 'role="alert"' in error and "hidden" in error
    assert re.search(r'data-manual-error[^>]*role="alert"|role="alert"[^>]*data-manual-error', html)
    assert 'aria-hidden="true"' in re.search(r"<span[^>]*data-scan-spinner[^>]*>", html).group(0)


def test_picker_names_nmap_and_marks_uncertain_states_in_the_result_table() -> None:
    html = picker_html()
    assert "Nmap Security Scanner" in html
    assert 'rel="noopener noreferrer"' in re.search(r'<a href="https://nmap.org/"[^>]*>', html).group(0)
    heads = re.findall(r'<th scope="col">([^<]+)</th>', html)
    assert heads == ["Select", "Protocol", "Port", "Service", "Status"]
    assert "<caption" in html
    assert "never selected automatically" in html


def test_picker_prefixes_keep_the_two_instances_apart() -> None:
    wizard, edit = picker_html("wizard"), picker_html("edit")
    assert set(re.findall(r'(?<![\w-])id="([^"]+)"', wizard)).isdisjoint(re.findall(r'(?<![\w-])id="([^"]+)"', edit))


def test_both_pages_load_the_pure_rules_before_the_picker() -> None:
    for page in ("target_form.html", "target_wizard.html"):
        text = (TEMPLATES / page).read_text(encoding="utf-8")
        core = text.index("utils/port-scan-core.js")
        assert core < text.index("network-scan-picker.js"), page


def test_scanner_text_never_reaches_an_html_sink() -> None:
    for name in ("network-scan-picker.js", "utils/port-scan-core.js"):
        source = (STATIC_JS / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in source, f"{name} uses {sink}"
