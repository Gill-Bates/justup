#!/usr/bin/env python3
#
# tests/test_ui_accessibility.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static accessibility contracts for Bootstrap modals in the Jinja templates."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
MODAL_RE = re.compile(r'<div\b[^>]*\bclass="modal(?:\s[^"]*)?"[^>]*>', re.DOTALL)


def _modal_templates() -> list[Path]:
    return [p for p in TEMPLATES.rglob("*.html") if 'class="modal ' in p.read_text(encoding="utf-8")]


def test_every_modal_is_viewport_centered_and_scrollable() -> None:
    paths = _modal_templates()
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for dialog in re.findall(r'<div\b[^>]*\bclass="modal-dialog\b[^"]*"', text):
            assert "modal-dialog-centered" in dialog, f"{path.name}: {dialog}"
            assert "modal-dialog-scrollable" in dialog, f"{path.name}: {dialog}"


def test_every_modal_has_an_existing_accessible_title() -> None:
    for path in _modal_templates():
        text = path.read_text(encoding="utf-8")
        for tag in MODAL_RE.findall(text):
            match = re.search(r'aria-labelledby="([^"]+)"', tag)
            assert match, f"{path.name}: modal without aria-labelledby: {tag}"
            assert f'id="{match.group(1)}"' in text, f"{path.name}: missing title id {match.group(1)}"


def test_modal_close_buttons_have_an_accessible_name() -> None:
    for path in _modal_templates():
        text = path.read_text(encoding="utf-8")
        for button in re.findall(r"<button\b[^>]*\bbtn-close\b[^>]*>", text, re.DOTALL):
            assert "aria-label=" in button, f"{path.name}: {button}"


def test_sidebar_toggle_and_groups_are_disclosure_buttons() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    toggle = re.search(r'<button\b[^>]*id="sidebar-toggle"[^>]*>', base, re.DOTALL)
    assert toggle, "sidebar toggle must be a button"
    assert "aria-expanded=" in toggle.group(0) and 'aria-controls="app-sidebar"' in toggle.group(0)
    assert 'id="app-sidebar"' in base
    for group in re.findall(r"<button\b[^>]*ju-nav-group-toggle[^>]*>", base, re.DOTALL):
        controls = re.search(r'aria-controls="([^"]+)"', group)
        assert controls and "aria-expanded=" in group
        assert f'id="{controls.group(1)}"' in base


def test_target_detail_uses_accessible_dashboard_and_network_tabs() -> None:
    page = (TEMPLATES / "target_detail.html").read_text(encoding="utf-8")
    diagnostics = (TEMPLATES / "fragments/target_detail/network_diagnostics.html").read_text(encoding="utf-8")
    scripts = (TEMPLATES.parent / "static/js/target-detail.js").read_text(encoding="utf-8")

    dashboard_tab = re.search(r'<button\b[^>]*id="target-dashboard-tab"[^>]*>', page, re.DOTALL)
    diagnostics_tab = re.search(r'<button\b[^>]*id="target-network-diagnostics-tab"[^>]*>', page, re.DOTALL)
    assert dashboard_tab and 'aria-selected="true"' in dashboard_tab.group(0)
    assert diagnostics_tab and 'aria-selected="false"' in diagnostics_tab.group(0)
    dashboard_panel = re.search(r'<section\b[^>]*id="target-dashboard-pane"[^>]*>', page, re.DOTALL)
    diagnostics_panel = re.search(r'<section\b[^>]*id="target-network-diagnostics-pane"[^>]*>', page, re.DOTALL)
    assert dashboard_panel and 'role="tabpanel"' in dashboard_panel.group(0)
    assert 'aria-labelledby="target-dashboard-tab"' in dashboard_panel.group(0)
    assert diagnostics_panel and 'role="tabpanel"' in diagnostics_panel.group(0)
    assert 'id="traceroute-container"' in diagnostics
    assert 'data-url="/ui/fragments/traceroute/{{ target.id }}"' in diagnostics
    assert "shown.bs.tab" in scripts and "_setupNetworkDiagnostics" in scripts


def test_template_element_ids_are_unique_per_file() -> None:
    text = (TEMPLATES / "settings" / "modals.html").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"{]+)"', text)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, duplicates


def test_status_icons_with_accessible_name_are_not_aria_hidden() -> None:
    for rel in ("fragments/target_status.html", "notifications/_alerts_panel.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        for tag in re.findall(r"<span\b[^>]*\brole=\"img\"[^>]*>", text):
            assert "aria-hidden" not in tag, f"{rel}: {tag}"


def test_disabled_pagination_links_have_no_href() -> None:
    for rel in ("notifications.html", "notifications/_alerts_panel.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        assert not re.search(r"page-item \{% if[^%]*%\}disabled", text), rel
