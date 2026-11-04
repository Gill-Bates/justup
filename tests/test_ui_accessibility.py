#!/usr/bin/env python3
#
# tests/test_ui_accessibility.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static accessibility contracts for Bootstrap modals in the Jinja templates."""

from __future__ import annotations

import re
from pathlib import Path

from tests._modal_html import divs_with_class

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _modal_templates() -> list[Path]:
    return [p for p in TEMPLATES.rglob("*.html") if divs_with_class(p.read_text(encoding="utf-8"), "modal")]


def test_every_modal_is_viewport_centered_and_scrollable() -> None:
    paths = _modal_templates()
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for _, dialog, classes in divs_with_class(text, "modal-dialog"):
            assert "modal-dialog-centered" in classes, f"{path.name}: {dialog}"
            assert "modal-dialog-scrollable" in classes, f"{path.name}: {dialog}"


def test_every_modal_has_an_existing_accessible_title() -> None:
    for path in _modal_templates():
        text = path.read_text(encoding="utf-8")
        for _, tag, _ in divs_with_class(text, "modal"):
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
    collapse = re.search(r'<button\b[^>]*id="sidebar-collapse"[^>]*>', base, re.DOTALL)
    assert collapse, "desktop sidebar control must be a button"
    assert all(attr in collapse.group(0) for attr in ("aria-expanded=", "aria-label=", 'aria-controls="app-sidebar"'))
    assert base.index('id="sidebar-collapse"') < base.index('class="ju-profile"'), "control sits above the profile"
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


def _css_rule(css: str, selector: str) -> str:
    """Return the declaration block of the first rule whose selector list is exactly *selector*."""
    match = re.search(rf"(?:^|\n|\}}\s*){re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"missing CSS rule: {selector}"
    return match.group(1)


def test_application_shell_is_viewport_bound_for_every_base_page() -> None:
    """Shared shell contract (DESIGN.md): no page-level opt-in, main is the single scroll region."""
    static = TEMPLATES.parent / "static" / "css"
    layout = (static / "gentelella-layout.css").read_text(encoding="utf-8")
    shell = _css_rule(layout, ".ju-shell")
    assert re.search(r"height:\s*100vh;\s*height:\s*100dvh;", shell), "shell is one dynamic viewport high"
    main = _css_rule(layout, ".ju-content>main")
    assert "overflow-y: auto" in main and "min-height: 0" in main and "flex: 1 1 auto" in main
    assert "flex: 0 0 auto" in _css_rule(layout, ".ju-content .ju-footer"), "footer never shrinks"

    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    order = [base.index(marker) for marker in ('class="ju-shell"', 'class="ju-topbar"', "<main", 'class="ju-footer"')]
    assert order == sorted(order), "shell order: topbar, main, footer"
    assert base.count('class="ju-shell"') == 1 and base.count("<main") == 1
    assert 'id="main-content"' in base


def test_network_diagnostics_content_layout_is_accessible() -> None:
    page = (TEMPLATES / "target_detail.html").read_text(encoding="utf-8")
    fragment = (TEMPLATES / "fragments/traceroute_map.html").read_text(encoding="utf-8")
    assert 'id="target-network-diagnostics-pane"' in page
    # The scroll region must stay keyboard-focusable and carry an accessible name.
    assert re.search(
        r'class="table-responsive tr-table-scroll" tabindex="0" role="region" aria-label(?:ledby)?=',
        fragment,
    )


# --- Modal dialog contract (DESIGN.md, "Modal dialogs") -------------------------------------------
_VARIANTS = ("ju-modal--form", "ju-modal--confirm", "ju-modal--critical")
_CSS = TEMPLATES.parent / "static" / "css" / "ui.css"


def _modal_chunks(text: str) -> list[tuple[str, str]]:
    modals = divs_with_class(text, "modal")
    starts = [offset for offset, _, _ in modals]
    return [(tag, text[s:e]) for (s, tag, _), e in zip(modals, [*starts[1:], len(text)], strict=True)]


def _all_modal_chunks() -> list[tuple[Path, str, str]]:
    return [(p, tag, block) for p in _modal_templates() for tag, block in _modal_chunks(p.read_text(encoding="utf-8"))]


def test_every_modal_declares_exactly_one_variant() -> None:
    chunks = _all_modal_chunks()
    assert len(chunks) >= 16  # 14 settings dialogs, group and dashboard widget dialogs
    for path, tag, _ in chunks:
        assert sum(v in re.search(r'class="([^"]*)"', tag).group(1).split() for v in _VARIANTS) == 1, (
            f"{path.name}: {tag}"
        )


def test_modals_use_the_shared_shell_without_local_styling() -> None:
    for path, _, block in _all_modal_chunks():
        header = re.search(r'<div class="modal-header[^"]*">', block)
        assert header and header.group(0) == '<div class="modal-header">', f"{path.name}: {header}"
        assert not re.search(r"\b(bg-danger|bg-warning|bg-success|text-white|btn-close-white)\b", block), path.name
        assert " style=" not in block, f"{path.name}: inline style"
        assert 'class="card' not in block and "card-body" not in block, f"{path.name}: nested card"
        assert re.search(r'<h2 class="modal-title ju-modal-title"', block), f"{path.name}: title is not an h2"


def test_modal_ids_referenced_by_aria_and_labels_exist() -> None:
    for path in _modal_templates():
        text = path.read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([^"{]+)"', text))
        for attr in ("aria-labelledby", "aria-describedby", "for", "form"):
            for value in re.findall(rf'\b{attr}="([^"{{]+)"', text):
                for ref in value.split():
                    assert ref in ids, f"{path.name}: {attr}={ref} has no target"


def test_modal_form_controls_have_a_programmatic_name() -> None:
    for path, _, block in _all_modal_chunks():
        for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", block):
            if re.search(r'type="(hidden|checkbox)"|aria-hidden="true"', tag):
                continue
            ident = re.search(r'\bid="([^"]+)"', tag)
            assert ident, f"{path.name}: control without id: {tag}"
            assert re.search(rf'<label\b[^>]*\bfor="{re.escape(ident.group(1))}"', block) or "aria-label=" in tag, (
                f"{path.name}: {ident.group(1)} has no label"
            )
        for tag in re.findall(r'<input\b[^>]*type="checkbox"[^>]*>', block):
            assert 'role="switch"' in tag, f"{path.name}: switch without role: {tag}"


def test_modal_groups_use_fieldset_with_legend_and_channels_share_one_pattern() -> None:
    text = (TEMPLATES / "settings" / "modals.html").read_text(encoding="utf-8")
    assert 'role="group"' not in text
    assert len(re.findall(r"<fieldset\b", text)) == len(re.findall(r"<legend\b", text)) >= 3
    for prefix in ("", "edit-"):
        block = text.split(f'id="{prefix}recipient-signal-enabled"')[1].split("</fieldset>")[0]
        assert block.count('class="ju-channel"') == 1  # the Signal row; the Email row follows in the same fieldset
    assert text.count("ju-channel-body") == 4 and text.count('class="ju-channel"') == 4


def test_channel_switches_control_their_field_group() -> None:
    text = (TEMPLATES / "settings" / "modals.html").read_text(encoding="utf-8")
    for prefix in ("", "edit-"):
        for channel, field in (("signal", "phone"), ("email", "email")):
            switch = re.search(rf'<input\b[^>]*id="{prefix}recipient-{channel}-enabled"[^>]*>', text).group(0)
            assert 'role="switch"' in switch and f'aria-controls="{prefix}recipient-{field}-container"' in switch
            group = re.search(rf'<div\b[^>]*id="{prefix}recipient-{field}-container"[^>]*>', text).group(0)
            assert "ju-channel-body" in group
            assert not re.search(r"\bhidden\b|d-none|style=", group)  # visibility follows the switch at runtime
    assert text.count('aria-controls="') == 4
    # The shared hint belongs to the test buttons and is hidden together with the last channel.
    assert re.search(r'<div class="ju-field" data-channel-hint>\s*<p[^>]*id="edit-recipient-test-hint"', text)


def test_recipient_dialog_has_one_labelled_test_pattern() -> None:
    text = (TEMPLATES / "settings" / "modals.html").read_text(encoding="utf-8")
    tests = re.findall(r'<button\b[^>]*data-test-channel="[^"]+"[^>]*>', text, re.DOTALL)
    assert len(tests) == 2 and not any('"all"' in t for t in tests)
    for tag in tests:
        assert re.search(r'aria-label="Send test message to \w+"', tag)
        assert 'aria-describedby="edit-recipient-test-hint"' in tag
    assert re.search(r'id="edit-recipient-test-result"[^>]*role="status"[^>]*aria-live="polite"', text)
    assert "all-targets-help" not in text  # the switch is self-explanatory; no description


def test_modal_css_uses_tokens_only() -> None:
    css = _CSS.read_text(encoding="utf-8")
    section = css.split("11. MODAL DIALOGS", 1)[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|!important", section), "hard-coded color or !important"
    assert not re.search(r"#(add|edit)?[A-Za-z-]*[mM]odal\b", section), "modal-ID selector"


# --- Focus lifecycle and field errors (DESIGN.md, "Modal accessibility", "Form fields and switches") ---
_JS = TEMPLATES.parent / "static" / "js"


def test_focus_is_managed_by_one_module_not_the_autofocus_attribute() -> None:
    for path in TEMPLATES.rglob("*.html"):
        assert not re.search(r"<[^>]+\sautofocus\b", path.read_text(encoding="utf-8")), f"{path.name}: autofocus"
    for path in _JS.rglob("*.js"):
        if ".min." in path.name or "bundle" in path.name or path.name == "modal-focus.js":
            continue
        assert "autofocus" not in path.read_text(encoding="utf-8"), f"{path.name}: autofocus"
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert (
        base.index("bootstrap.bundle.min.js") < base.index("utils/modal-focus.js") < base.index("utils/field-errors.js")
    )
    module = (_JS / "utils" / "modal-focus.js").read_text(encoding="utf-8")
    for event in ("show.bs.modal", "shown.bs.modal", "hidden.bs.modal"):
        assert event in module
    for path in _JS.rglob("*.js"):
        if path.name in {"modal-focus.js", "confirm-dialog.js"} or ".min." in path.name or "bundle" in path.name:
            continue
        assert "'shown.bs.modal'" not in path.read_text(encoding="utf-8"), (
            f"{path.name}: per-modal focus handling outside the focus module"
        )


def test_form_validation_uses_inline_field_errors() -> None:
    for name in ("settings.recipients.js", "settings.users.js"):
        assert "JuFieldErrors.show(" in (_JS / name).read_text(encoding="utf-8"), name
    groups = (TEMPLATES / "groups.html").read_text(encoding="utf-8")
    assert "JuFieldErrors.show(" in groups
    module = (_JS / "utils" / "field-errors.js").read_text(encoding="utf-8")
    assert "aria-invalid" in module and "aria-describedby" in module
    recipients = (_JS / "settings.recipients.js").read_text(encoding="utf-8")
    assert "validationError" not in recipients  # field errors no longer go into the shared error area


def test_modal_icon_buttons_have_names_and_request_errors_are_alerts() -> None:
    for path, _, block in _all_modal_chunks():
        for tag, inner in re.findall(r"(<button\b[^>]*>)(.*?)</button>", block, re.DOTALL):
            visible = re.sub(r"<span[^>]*material-icons[^>]*>.*?</span>|<[^>]+>", "", inner).strip()
            assert visible or "aria-label=" in tag, f"{path.name}: unnamed button {tag}"
        for tag in re.findall(r'<div\b[^>]*class="[^"]*ju-modal-error[^"]*"[^>]*>', block):
            assert 'role="alert"' in tag, f"{path.name}: {tag}"


def test_card_mode_keeps_table_headers_in_the_accessibility_tree() -> None:
    css = (TEMPLATES.parent / "static" / "css" / "ui.css").read_text(encoding="utf-8")
    card = css[css.index("@container ju-table (width <40rem)") :]
    card = card[: card.index(".rtable tbody tr {")]
    match = re.search(r"\.rtable thead\s*\{([^}]*)\}", card)
    assert match, "missing card-mode header rule"
    rule = match.group(1)
    assert "display: none" not in rule and "visibility: hidden" not in rule
    assert "clip-path" in rule and "position: absolute" in rule
    assert "thead" not in card.replace(".rtable thead", "", 1), "one shared header rule, no per-table variant"


def test_rtable_cell_labels_match_their_column_headers() -> None:
    for rel in ("groups.html", "fragments/targets_table.html", "notifications/_alerts_panel.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        head = re.search(r"<thead>(.*?)</thead>", text, re.DOTALL)
        assert head, rel
        cells = re.findall(r"<th\b[^>]*>(.*?)</th>", head.group(1), re.DOTALL)
        headers = {re.sub(r"<[^>]+>", "", cell).strip() for cell in cells}
        labels = set(re.findall(r'<td data-label="([^"]*)"', text))
        assert labels - {""} <= headers, f"{rel}: cell labels without a column header: {labels - headers}"


def test_iframes_are_sandboxed_and_never_allow_scripts() -> None:
    # The email template editor and preview render user-defined HTML; the sandbox is a security boundary (DESIGN.md).
    found = 0
    for path in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<iframe\b[^>]*>", path.read_text(encoding="utf-8")):
            found += 1
            sandbox = re.search(r'sandbox="([^"]*)"', tag)
            assert sandbox, f"{path.name}: iframe without sandbox: {tag}"
            assert "allow-scripts" not in sandbox.group(1).split(), f"{path.name}: sandbox allows scripts: {tag}"
    assert found


CSS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
FOCUS_TOKENS = ("--ju-focus-ring", "--ju-focus-field-border", "--ju-control-edge", "--ju-switch-thumb-off")


def test_focus_tokens_exist_for_light_and_dark() -> None:
    layout = (CSS_DIR / "gentelella-layout.css").read_text(encoding="utf-8")
    dark_at = layout.index('[data-bs-theme="dark"]')
    for token in FOCUS_TOKENS:
        assert f"{token}:" in layout[:dark_at], f"{token} missing in the light theme"
        assert f"{token}:" in layout[dark_at:], f"{token} missing in the dark theme"
    assert "--ju-focus-outline-width:" in layout and "--ju-focus-outline-offset:" in layout


def test_focus_rules_use_the_shared_tokens_not_the_primary_colour() -> None:
    offenders = []
    for path in CSS_DIR.glob("*.css"):
        if path.name.endswith(".min.css") or path.name in {"leaflet.css", "gridstack.min.css", "material-icons.css"}:
            continue
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if ":focus" not in selector:
                continue
            for prop in re.findall(r"(?:^|;)\s*(outline[\w-]*|box-shadow|border-color)\s*:([^;]+)", body):
                if re.search(r"--bs-primary|#[0-9a-fA-F]{3,8}\b|rgba?\(", prop[1]):
                    offenders.append(f"{path.name}: {selector.strip()} {prop[0]}: {prop[1].strip()}")
    assert not offenders, "focus rules must use the --ju-focus-* tokens:\n" + "\n".join(offenders)
