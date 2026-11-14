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


def test_picker_announces_progress_politely_and_scan_failures_assertively() -> None:
    html = picker_html()
    status = re.search(r"<p[^>]*data-scan-status[^>]*>", html).group(0)
    assert 'role="status"' in status and 'aria-live="polite"' in status
    error = re.search(r"<p[^>]*data-scan-error[^>]*>", html).group(0)
    assert 'role="alert"' in error and "hidden" in error
    # DC-FORM-11: the manual-port field error is created by JuFieldErrors, never pre-rendered with a role.
    assert "data-manual-error" not in html and "manual-ports-error" not in html
    assert 'aria-hidden="true"' in re.search(r"<span[^>]*data-scan-spinner[^>]*>", html).group(0)


def test_picker_names_nmap_and_offers_discovery_as_a_collapsed_optional_disclosure() -> None:
    html = picker_html()
    assert "Nmap Security Scanner" in html
    assert 'rel="noopener noreferrer"' in re.search(r'<a href="https://nmap.org/"[^>]*>', html).group(0)
    discover = re.search(r"<details[^>]*data-scan-discover[^>]*>", html).group(0)
    assert " open" not in discover, "discovery starts collapsed"
    assert "Discover ports with Nmap" in re.search(r"<summary>.*?</summary>", html, re.DOTALL).group(0)
    # Every scan control lives inside the disclosure: nothing about Nmap competes with the manual entry.
    body = html[html.index("data-scan-discover") : html.index("</details>")]
    for hook in (
        'data-scan-protocol="tcp"',
        'data-scan-protocol="udp"',
        "data-scan-profile",
        'data-scan-action="start"',
        'data-scan-action="cancel"',
    ):
        assert hook in body, hook


def test_picker_has_one_manual_input_and_chips_and_no_second_port_list() -> None:
    html = picker_html()
    assert html.count("data-manual-input") == 1
    assert len(re.findall(r'<input\b[^>]*type="text"', html)) == 1
    assert html.count("data-port-chips>") + html.count("data-port-chips ") == 1


def test_picker_results_are_confirmed_first_and_others_collapsed_without_a_table() -> None:
    html = picker_html()
    assert "<table" not in html and "<th" not in html and "table-responsive" not in html
    assert html.index("data-scan-confirmed ") < html.index("data-scan-other")
    other = re.search(r"<details[^>]*data-scan-other[^>]*>", html).group(0)
    assert " open" not in other, "other results start collapsed"
    assert "not confirmed open" in html
    assert "select-open" not in html, "the preselection replaces the bulk action"
    apply = re.search(r"<button[^>]*data-scan-action=\"apply\"[^>]*>[^<]*</button>", html).group(0)
    assert "Add selected ports (0)" in apply and "btn-primary" in apply
    assert html.count("never selected automatically") == 0


def test_wizard_shows_one_port_input_and_keeps_the_form_fields_as_hidden_backing() -> None:
    text = (TEMPLATES / "target_wizard.html").read_text(encoding="utf-8")
    assert re.search(r'<input id="wizard-ports" name="tcp_ports" type="hidden">', text)
    assert re.search(r'<input id="wizard-udp-ports" name="udp_ports" type="hidden">', text)
    step = text[text.index('data-step="1"') : text.index('data-step="2"')]
    order = [
        step.index(marker)
        for marker in ('id="wizard-address"', 'id="wizard-name"', 'id="wizard-group"', "network_scan_picker.html")
    ]
    assert order == sorted(order), "address, name and group, then ports with discovery"
    visible_text_inputs = re.findall(r'<input\b(?![^>]*type="hidden")[^>]*type="text"[^>]*>', step)
    assert [re.search(r'id="([^"]+)"', tag).group(1) for tag in visible_text_inputs] == [
        "wizard-address",
        "wizard-name",
    ]


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
