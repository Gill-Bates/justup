#!/usr/bin/env python3
#
# tests/test_ui_accessibility.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static accessibility contracts for Bootstrap modals in the Jinja templates."""

from __future__ import annotations

import re
from pathlib import Path

from app.api.frontend import templates
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


def test_sidebar_links_to_about_page_for_every_user() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    about = re.search(r'<a\b[^>]*href="/ui/about"[^>]*>.*?</a>', base, re.DOTALL)
    assert about, "About page is missing from the sidebar"
    assert "active_page == 'about'" in about.group(0)
    assert 'aria-current="page"' in about.group(0)
    assert 'ju-nav-text">About</span>' in about.group(0)
    assert base.index('href="/ui/about"') > base.index("{% endif %}", base.index('href="/ui/settings"'))


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


def test_all_page_templates_compile() -> None:
    for path in TEMPLATES.rglob("*.html"):
        templates.env.get_template(path.relative_to(TEMPLATES).as_posix())


def test_status_icons_with_accessible_name_are_not_aria_hidden() -> None:
    for rel in ("fragments/target_status.html", "notifications/_alerts_panel.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        for tag in re.findall(r"<span\b[^>]*\brole=\"img\"[^>]*>", text):
            assert "aria-hidden" not in tag, f"{rel}: {tag}"


def test_disabled_pagination_links_have_no_href() -> None:
    for rel in ("notifications.html", "notifications/_alerts_panel.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        assert not re.search(r"page-item \{% if[^%]*%\}disabled", text), rel
        disabled = re.findall(r'<li class="page-item[^"]*\bdisabled\b[^"]*">(.*?)</li>', text, re.DOTALL)
        assert disabled, f"{rel}: no disabled pagination item found"
        for item in disabled:
            assert "<a" not in item and "href=" not in item, f"{rel}: disabled item is a link: {item}"


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


_SIZES = ("ju-modal-size--compact", "ju-modal-size--standard", "ju-modal-size--wide")


def test_modal_size_is_independent_of_intent_and_comes_from_width_tokens() -> None:
    """DC-MODAL-5: at most one size class per dialog; widths are set only by the three width tokens."""
    for path, tag, _ in _all_modal_chunks():
        classes = re.search(r'class="([^"]*)"', tag).group(1).split()
        assert sum(c in _SIZES for c in classes) <= 1, f"{path.name}: {tag}"
        assert not any(c.startswith("ju-modal--") and c not in _VARIANTS for c in classes), f"{path.name}: {tag}"
    css = _CSS.read_text(encoding="utf-8")
    for size in _SIZES:
        assert re.search(
            rf"\.{size}\s*\{{\s*--bs-modal-width: var\(--ui-modal-width-{size.rsplit('--', 1)[1]}\);", css
        ), size
    assert ".ju-modal--wide" not in css, "wide is a size, never an intent"
    assert len(re.findall(r"--bs-modal-width:", css)) == 6, "only intent defaults and size classes set the dialog width"
    # The size classes come after the intent defaults, so an explicit size wins at equal specificity.
    assert css.index(".ju-modal--confirm,\n.ju-modal--critical {") < css.index(".ju-modal-size--compact {")


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
        signal = text.index(f'id="{prefix}recipient-signal-enabled"')
        channel_start = text.rfind("<fieldset", 0, signal)
        channel_end = text.index("</fieldset>", signal) + len("</fieldset>")
        channel_group = text[channel_start:channel_end]
        settings_start = text.index('<fieldset class="ju-fieldset ju-recipient-settings"', channel_end)
        settings_end = text.index("</fieldset>", settings_start) + len("</fieldset>")
        settings_group = text[settings_start:settings_end]

        assert "Notification Channels" in channel_group
        assert channel_group.count('class="ju-channel"') == 2
        assert f'id="{prefix}recipient-enabled"' not in channel_group
        assert f'id="{prefix}recipient-all-targets"' not in channel_group
        assert "Recipient Settings" in settings_group
        assert f'id="{prefix}recipient-enabled"' in settings_group
        assert f'id="{prefix}recipient-all-targets"' in settings_group
    # Add/Edit Recipient plus Create/Edit User: two channel rows each.
    assert text.count("ju-channel-body") == 8 and text.count('class="ju-channel"') == 8


def test_channel_switches_control_their_field_group() -> None:
    text = (TEMPLATES / "settings" / "modals.html").read_text(encoding="utf-8")
    for prefix in ("", "edit-"):
        for channel, field in (("signal", "phone"), ("email", "email")):
            switch = re.search(rf'<input\b[^>]*id="{prefix}recipient-{channel}-enabled"[^>]*>', text).group(0)
            assert 'role="switch"' in switch and f'aria-controls="{prefix}recipient-{field}-container"' in switch
            group = re.search(rf'<div\b[^>]*id="{prefix}recipient-{field}-container"[^>]*>', text).group(0)
            assert "ju-channel-body" in group
            assert not re.search(r"\bhidden\b|d-none|style=", group)  # visibility follows the switch at runtime
    assert text.count('aria-controls="') == 8  # the two recipient and the two user dialogs
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


# --- Focus lifecycle and field errors (DESIGN.md DC-MODAL-13, DC-FORM-6..11) ---
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
    groups = (_JS / "groups.js").read_text(encoding="utf-8")
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


# DC-FOCUS-1, DC-FOCUS-6, DC-EX-FOCUS-2: a focus rule reads only these tokens (colour, width, offset).
_FOCUS_TOKEN_NAMES = {
    "--ju-focus-ring",
    "--ju-focus-field-border",
    "--ju-focus-outline-width",
    "--ju-focus-outline-offset",
    "--ju-control-edge",
    "--ju-sidebar-ring",
}
_FOCUS_PROPERTY = re.compile(r"(?:^|;)\s*(outline[\w-]*|box-shadow|border-color)\s*:([^;]+)")


def _focus_rule_violations(value: str) -> list[str]:
    problems = [f"var({name})" for name in re.findall(r"var\((--[\w-]+)", value) if name not in _FOCUS_TOKEN_NAMES]
    if re.search(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|color-mix)\(", value):
        problems.append("literal colour")
    if re.search(r"(?<![\w-])-?\d*\.?\d+(?:px|rem|em)\b", value):
        problems.append("literal length (use the width and offset tokens)")
    return problems


def test_focus_rules_use_the_shared_tokens_not_the_primary_colour() -> None:
    offenders = []
    for path in CSS_DIR.glob("*.css"):
        if path.name.endswith(".min.css") or path.name in {"leaflet.css", "gridstack.min.css", "material-icons.css"}:
            continue
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if ":focus" not in selector:
                continue
            for name, value in _FOCUS_PROPERTY.findall(body):
                offenders.extend(
                    f"{path.name}: {selector.strip()} {name}: {value.strip()} ({problem})"
                    for problem in _focus_rule_violations(value)
                )
    assert not offenders, "focus rules must use only the --ju-focus-* tokens:\n" + "\n".join(offenders)


def test_plex_font_faces_use_font_display_fallback() -> None:
    layout = (CSS_DIR / "gentelella-layout.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^{}]*)\}", layout)
    plex = [face for face in faces if "IBM Plex Sans" in face]
    assert plex, "no IBM Plex Sans @font-face declaration found"
    for face in plex:
        assert re.search(r"font-display\s*:\s*fallback\s*;", face), (
            f"IBM Plex Sans must use font-display: fallback (DC-TOK-8):\n{face}"
        )


def test_floating_panels_use_the_shared_elevation_token_in_both_themes() -> None:
    css = TEMPLATES.parent / "static" / "css"
    layout = (css / "gentelella-layout.css").read_text(encoding="utf-8")
    assert len(re.findall(r"--ju-shadow-raised:", layout)) == 2, "light and dark each define the elevation token"
    notifications = (css / "notifications.css").read_text(encoding="utf-8")
    shadows = re.findall(r"box-shadow:\s*([^;]+);", notifications)
    assert "var(--ju-shadow-raised)" in shadows
    assert not [s for s in shadows if "rgb(" in s and "var(" not in s], shadows


def test_port_picker_disclosures_chips_and_result_lists_have_accessible_names() -> None:
    picker = (TEMPLATES / "fragments" / "network_scan_picker.html").read_text(encoding="utf-8")
    for hook in ("data-scan-discover", "data-scan-other"):
        tag = re.search(rf"<details\b[^>]*{hook}[^>]*>.*?<summary\b[^>]*>", picker, re.DOTALL)
        assert tag, hook
    assert "Discover ports with Nmap" in picker
    for hook in ("data-port-chips", "data-scan-confirmed ", "data-scan-other-list"):
        tag = re.search(rf"<ul\b[^>]*{hook}[^>]*>", picker)
        assert tag and ("aria-labelledby=" in tag.group(0) or "aria-label=" in tag.group(0)), hook
    # Decorative icons stay out of the accessibility tree; the manual input is described by its help and error text.
    assert re.search(r'<span class="material-icons" aria-hidden="true">radar</span>', picker)
    manual = re.search(r"<input\b[^>]*data-manual-input[^>]*>", picker).group(0)
    assert (
        "manual-help" in manual and "manual-ports-error" not in manual
    )  # the field error id is added by JuFieldErrors


def _card_headers(text: str):
    """Yield the complete markup of every `.card-header` element, including nested divs."""
    for start in re.finditer(r"<div\b[^>]*\bclass=\"[^\"]*\bcard-header\b[^>]*>", text):
        depth, pos = 1, start.end()
        for tag in re.finditer(r"<(/?)div\b", text[pos:]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                yield text[start.start() : pos + tag.end()]
                break


def test_card_headers_have_no_collapse_toggle() -> None:
    """DC-CARD-1: a card header is plain; only the enable switch of an optional field group may carry aria-controls."""
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<details\b[^>]*\bclass=\"[^\"]*\bcard\b", text), f"{path.name}: card as <details>"
        summary_header = re.search(r"<summary\b[^>]*\bclass=\"[^\"]*\bcard-header\b", text)
        assert not summary_header, f"{path.name}: <summary> card header"
        for header in _card_headers(text):
            where = f"{path.name}: {header[:80]!r}"
            assert 'data-bs-toggle="collapse"' not in header, f"{where}: collapse toggle in a card header"
            assert "<details" not in header and "<summary" not in header, f"{where}: disclosure in a card header"
            assert "ju-collapse-chevron" not in header, f"{where}: chevron in a card header"
            for control in re.finditer(r"<(?:button|input|a)\b[^>]*>", header):
                tag = control.group(0)
                if "aria-controls=" in tag:
                    assert 'role="switch"' in tag, f"{where}: aria-controls outside an optional-group switch"
                if "aria-expanded=" in tag:
                    assert 'data-bs-toggle="dropdown"' in tag, f"{where}: aria-expanded in a card header"
    assert "settings-details" not in (TEMPLATES.parent / "static" / "css" / "ui.css").read_text(encoding="utf-8")


# --- One tab component (DESIGN.md, "Tabs") ---
_CSS_DIR = TEMPLATES.parent / "static" / "css"
_TAB_SELECTOR = re.compile(r"nav-link\b|nav-pills|nav-tabs|ju-tabs|tablist|\[role=[\"']?tab")
_TAB_OWNERS = {"ui.css", "gentelella-layout.css"}
_VENDOR_CSS = {"bootstrap.min.css", "leaflet.css"}


def _selectors(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [m.group(1).strip() for m in re.finditer(r"([^{}]+)\{", css) if not m.group(1).strip().startswith("@")]


def test_tab_strips_use_the_shared_component_and_no_page_restyles_them() -> None:
    # Every strip is a ju-tabs; the class of defect is a tab look defined anywhere but the shared owner.
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<[a-z]+\b[^>]*class=\"[^\"]*\bnav-(?:tabs|pills)\b[^\"]*\"[^>]*>", text):
            assert re.search(r'class="[^"]*\bju-tabs\b', tag) and "nav-tabs" not in tag, f"{path.name}: {tag}"
        for style in re.findall(r"<style\b[^>]*>(.*?)</style>", text, flags=re.DOTALL):
            assert not any(_TAB_SELECTOR.search(s) for s in _selectors(style)), f"{path.name}: inline tab CSS"
    for path in _CSS_DIR.glob("*.css"):
        if path.name in _TAB_OWNERS | _VENDOR_CSS:
            continue
        hits = [s for s in _selectors(path.read_text(encoding="utf-8")) if _TAB_SELECTOR.search(s)]
        assert not hits, f"{path.name} styles tabs outside the shared component: {hits}"
    shared = _selectors(_CSS.read_text(encoding="utf-8"))
    page_scoped = [s for s in shared if "#" in s.replace("#fff", "") and re.search(r"nav-link|tablist", s)]
    assert not page_scoped, f"ui.css restyles a tab strip by id: {page_scoped}"


def test_real_tabs_and_link_strips_keep_their_own_semantics() -> None:
    """DC-TAB-6 to DC-TAB-9: tablist strips and link strips share the look, not the ARIA model."""
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<ul\b[^>]*\bju-tabs\b[^>]*>(.*?)</ul>", text, re.DOTALL):
            tag, items = match.group(0).split(">", 1)[0], match.group(1)
            before = text[: match.start()].rstrip()
            if 'role="tablist"' in tag:
                assert "aria-label=" in tag and not before.endswith("<nav>"), f"{path.name}: {tag}"
                for control in re.findall(r"<(?:button|a)\b[^>]*\bnav-link\b[^>]*>", items):
                    assert 'role="tab"' in control and "aria-controls=" in control, f"{path.name}: {control}"
                    assert "aria-disabled" not in control, f"{path.name}: a disabled real tab uses native `disabled`"
                    assert not re.search(r'class="[^"]*\bdisabled\b', control), f"{path.name}: {control}"
            else:
                assert re.search(r'<nav\b[^>]*aria-label="[^"]+"[^>]*>$', before), (
                    f"{path.name}: link strip needs <nav aria-label>"
                )
                assert "aria-label=" not in tag, f"{path.name}: the label belongs on the nav landmark"
                for link in re.findall(r"<a\b[^>]*\bnav-link\b[^>]*>", items):
                    assert 'role="tab"' not in link, f"{path.name}: {link}"
                    if re.search(r'class="[^"]*\bdisabled\b', link):
                        assert "href=" not in link and 'aria-disabled="true"' in link and 'tabindex="-1"' in link, link


def test_disabled_tab_state_is_not_pointer_events_only() -> None:
    css = _CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.ju-tabs \.nav-link:is\(\.disabled, :disabled, \[aria-disabled=\"true\"\]\)\s*\{([^}]*)\}", css)
    assert rule and "cursor: not-allowed" in rule.group(1)
    hover = re.search(r"(\.ju-tabs \.nav-link:not\([^{]*\):hover)\s*\{", css)
    assert hover and ".disabled" in hover.group(1) and "[aria-disabled" in hover.group(1), (
        "hover must skip disabled tabs"
    )


def test_tab_component_reads_tokens_only() -> None:
    css = _CSS.read_text(encoding="utf-8")
    section = css.split("TABS (DESIGN.md", 1)[1].split("/* Table negative margins", 1)[0]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|!important", section), "hard-coded color or !important"
    assert "var(--bs-primary)" in section and "var(--ju-primary-contrast)" in section
    assert re.search(r"\.ju-scrollbar-accent,\s*\.ju-tabs\s*\{[^}]*scrollbar-color:\s*var\(--bs-primary\)", css)


def test_check_card_and_signal_switches_own_their_optional_group() -> None:
    """DC-CARD-3 / DC-FORM-15: the switch names its group via aria-controls; the group is no Bootstrap collapse."""
    form = (TEMPLATES / "target_form.html").read_text(encoding="utf-8")
    for check in ("http", "ping", "tcp", "udp"):
        assert f'id="{check}-check-body"' in form and f'aria-controls="{check}-check-body"' in form, check
    signal = (TEMPLATES / "settings" / "signal.html").read_text(encoding="utf-8")
    switch = re.search(r'<input\b[^>]*id="signal_enabled"[^>]*>', signal, re.DOTALL).group(0)
    assert 'role="switch"' in switch and 'aria-controls="signal-panel"' in switch
    assert re.search(r'id="signal-panel"[^>]*>', signal) and not re.search(
        r'class="[^"]*\bcollapse\b[^"]*"\s+id="signal-panel"', signal
    )


def test_field_errors_carry_no_live_region_role() -> None:
    # DC-FORM-11: a field error is tied to its control (aria-invalid, aria-describedby) and is no live region.
    producer = (CSS_DIR.parent / "js" / "utils" / "field-errors.js").read_text(encoding="utf-8")
    assert not re.search(r"setAttribute\(\s*['\"](?:role|aria-live)['\"]", producer), (
        "JuFieldErrors must not add a live region"
    )
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        tags = re.findall(r"<[a-z][^>]*(?:-field-error|invalid-feedback)[^>]*>", text)
        offenders.extend(
            f"{path.relative_to(TEMPLATES)}: {tag[:100]}" for tag in tags if re.search(r"\b(?:role|aria-live)=", tag)
        )
    assert not offenders, "field-error elements must not set role or aria-live:\n" + "\n".join(offenders)


# Selectors that may remove the native outline: DC-EX-FOCUS-1 (main, offline overlay) and the variants whose
# ring is drawn by another element (V3 stretched link row via ::after, V4 tag input entry via its field).
_OUTLINE_NONE_ALLOWED = (
    "main:focus",
    "#server-offline-overlay",
    "a.stretched-link:focus-visible",
    ".tag-input__entry:focus",
)


def test_outline_none_is_limited_to_the_documented_focus_cases() -> None:
    offenders = []
    for path in CSS_DIR.glob("*.css"):
        if path.name.endswith(".min.css") or path.name in {"leaflet.css", "gridstack.min.css", "material-icons.css"}:
            continue
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if re.search(r"(?:^|;)\s*outline\s*:\s*(?:none|0)\s*(?:;|$)", body.strip()) and not any(
                a in selector for a in _OUTLINE_NONE_ALLOWED
            ):
                offenders.append(f"{path.name}: {' '.join(selector.split())}")
    assert not offenders, "outline removal needs an exception row (DC-EXC-1):\n" + "\n".join(offenders)


def test_template_style_blocks_set_no_own_focus_indication() -> None:
    # DC-FOCUS-1/-7: inline <style> may not add focus box-shadow rings or its own focus colour.
    rule = re.compile(r"([^{}]*:(?:focus|focus-visible|focus-within)[^{}]*)\{([^}]*)\}")
    for path in TEMPLATES.rglob("*.html"):
        for style in re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(encoding="utf-8"), re.DOTALL):
            for selector, body in rule.findall(style):
                assert "box-shadow" not in body, f"{path.name}: {selector.strip()}"
                assert not re.search(r"outline\s*:\s*[^;]*\S", body.replace("outline: none", "")), (
                    f"{path.name}: {selector.strip()}"
                )


def test_tcp_ports_container_uses_the_shared_focus_group() -> None:
    css = (TEMPLATES.parent / "static" / "css" / "ui.css").read_text(encoding="utf-8")
    assert re.search(r"\.tcp-ports-container:focus-within\s*\{[^}]*--ju-focus-ring", css)


# ── S1 forms/a11y (N08, N20, N22, N23, N25, N52) ──────────────────────────────
def _tag_after(text: str, needle: str) -> str:
    """Return the full opening tag that contains `needle`."""
    index = text.index(needle)
    start = text.rindex("<", 0, index)
    return text[start : text.index(">", index) + 1]


def test_wizard_switches_are_role_switch_inside_ju_switch() -> None:
    """DC-FORM-5 / N08: every wizard switch is a real checkbox with role=switch inside `.ju-switch`."""
    text = (TEMPLATES / "target_wizard.html").read_text(encoding="utf-8")
    wrappers = re.findall(r'<div class="([^"]*\bform-switch\b[^"]*)">\s*(<input\b[^>]*>)', text)
    assert len(wrappers) == 7
    for classes, field in wrappers:
        assert "ju-switch" in classes.split(), field
        assert 'type="checkbox"' in field and 'role="switch"' in field, field


def test_target_form_marks_required_fields_with_ju_required() -> None:
    """DC-FORM-2 / N23: no literal trailing asterisk in a label; the form carries the required note."""
    text = (TEMPLATES / "target_form.html").read_text(encoding="utf-8")
    assert not re.search(r"\*\s*</label>", text), "literal asterisk in a label"
    assert 'Monitor Name <span class="ju-required" aria-hidden="true">*</span>' in text
    assert "Fields marked * are required." in text


def test_target_form_switches_name_their_hidden_groups_with_aria_controls() -> None:
    """DC-FORM-15 / N20: SLA and Basic-Auth switches own their hidden groups."""
    text = (TEMPLATES / "target_form.html").read_text(encoding="utf-8")
    sla = _tag_after(text, 'id="sla_enabled"')
    assert 'aria-controls="sla-fields"' in sla and 'id="sla-fields"' in text
    auth = _tag_after(text, 'id="http_basic_auth_enabled"')
    controls = re.search(r'aria-controls="([^"]+)"', auth)
    assert controls, auth
    ids = controls.group(1).split()
    assert len(ids) == 2
    for group_id in ids:
        group = re.search(rf'<div\b[^>]*\bid="{group_id}"[^>]*>', text)
        assert group and "auth-field" in group.group(0), group_id


def test_single_switches_are_not_wrapped_in_a_role_group() -> None:
    """DC-FORM-4 / N25: role=group is for related controls; one switch with its own label is no group."""
    text = (TEMPLATES / "target_form.html").read_text(encoding="utf-8")
    for switch in ("is_enabled", "http_prefer_head"):
        assert f'id="{switch}-group-label"' not in text, switch
        assert f'aria-labelledby="{switch}-group-label"' not in text, switch


def test_smtp_button_groups_expose_pressed_state() -> None:
    """DC-A11Y-3 / N52: each toggle button of the two button groups has `aria-pressed` that matches `.active`."""
    text = (TEMPLATES / "settings" / "smtp.html").read_text(encoding="utf-8")
    for button_id in ("btn-mode-edit", "btn-mode-html", "btn-mode-preview", "btn-preview-down", "btn-preview-resolved"):
        tag = _tag_after(text, f'id="{button_id}"')
        pressed = "true" if "active" in re.search(r'class="([^"]*)"', tag).group(1).split() else "false"
        assert f'aria-pressed="{pressed}"' in tag, tag


def _help_text_without_tie(path: Path) -> list[str]:
    """`.form-text` blocks that sit beside a control but no aria-describedby of the file names them (DC-FORM-3)."""
    from html.parser import HTMLParser

    void = {"input", "br", "hr", "img", "meta", "link", "source", "area", "base", "col", "embed", "param", "wbr"}

    class Node:
        def __init__(self, tag, attrs, parent):
            self.tag, self.attrs, self.parent, self.kids = tag, dict(attrs), parent, []

    class Tree(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = self.cur = Node("root", [], None)

        def handle_starttag(self, tag, attrs):
            node = Node(tag, attrs, self.cur)
            self.cur.kids.append(node)
            if tag not in void:
                self.cur = node

        def handle_startendtag(self, tag, attrs):
            self.cur.kids.append(Node(tag, attrs, self.cur))

        def handle_endtag(self, tag):
            node = self.cur
            while node is not None and node.tag != tag:
                node = node.parent
            if node is not None and node.parent is not None:
                self.cur = node.parent

    def walk(node):
        for kid in node.kids:
            yield kid
            yield from walk(kid)

    text = path.read_text(encoding="utf-8")
    tree = Tree()
    tree.feed(text)
    token = re.compile(r"(?:\{\{.*?\}\}|\S)+")  # a Jinja expression with spaces stays one id
    described = {t.group(0) for value in re.findall(r'aria-describedby="([^"]*)"', text) for t in token.finditer(value)}
    found = []
    for node in walk(tree.root):
        if "form-text" not in (node.attrs.get("class") or "").split():
            continue
        controls = [
            c for c in walk(node.parent) if c.tag in {"input", "select", "textarea"} and c.attrs.get("type") != "hidden"
        ]
        if controls and node.attrs.get("id") not in described:
            field = controls[0].attrs.get("id")
            found.append(f"{path.name}: .form-text beside #{field} has no id named by aria-describedby")
    return found


def test_form_text_beside_a_control_is_tied_with_aria_describedby() -> None:
    """DC-FORM-3 / N22: scripted scan over every template."""
    problems = [line for path in sorted(TEMPLATES.rglob("*.html")) for line in _help_text_without_tie(path)]
    assert not problems, "\n".join(problems)


APP_STATIC_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


def test_recipients_script_has_no_inline_font_size() -> None:
    """N07, DC-EXT-6: icon sizes come from `.ju-icon-*` classes, not from style attributes."""
    source = (APP_STATIC_JS / "settings.recipients.js").read_text(encoding="utf-8")
    assert "font-size" not in source


def test_targets_table_status_placeholder_is_a_spinner_with_a_word() -> None:
    """N13, DC-STATE-1 (S6): a loading status is spinner and word, not the neutral circle glyph."""
    text = (TEMPLATES / "fragments" / "targets_table.html").read_text(encoding="utf-8")
    placeholder = re.search(r'hx-get="/ui/fragments/target-status/[^>]*>\s*(.*?Loading status</span>)', text, re.DOTALL)
    assert placeholder
    assert "spinner-border" in placeholder.group(1)
    assert ">circle<" not in placeholder.group(1)


def test_spooler_static_labels_mirror_the_script_labels() -> None:
    """N47, DC-SPOOL-1: summary and filter labels equal the status labels of `_statusBadge`."""
    html = (TEMPLATES / "settings" / "spooler.html").read_text(encoding="utf-8")
    script = (APP_STATIC_JS / "settings.spooler.js").read_text(encoding="utf-8")
    for label in ("Waiting for handoff", "Handing over"):
        assert f"'{label}'" in script
        assert f">{label}</div>" in html or f">{label}</option>" in html
    assert re.search(r'<option value="pending">Waiting for handoff</option>', html)
    assert re.search(r'<option value="processing">Handing over</option>', html)
    assert ">Waiting</" not in html
    assert ">Processing</" not in html


def test_wizard_port_summary_errors_carry_no_role() -> None:
    """DC-FORM-11: the summary error slots are described by aria-describedby; they are not live regions or alerts."""
    text = (TEMPLATES / "target_wizard.html").read_text(encoding="utf-8")
    for protocol in ("tcp", "udp"):
        slot = re.search(rf'<div\b[^>]*id="wizard-{protocol}-summary-error"[^>]*>', text)
        assert slot, protocol
        assert "role=" not in slot.group(0), slot.group(0)
        button = re.search(rf'<button\b[^>]*data-edit-ports="{protocol}"[^>]*>', text)
        assert button
        assert f"wizard-{protocol}-summary-error" in button.group(0)


# T-27 / OR-17 / DC-TOK-5: elevation has three levels. Aliases (`--ui-modal-shadow`, `--td-shadow`) carry no value of
# their own, and an elevation shadow (a non-inset layer with a blur) in application CSS or inline styles reads a token.
_ELEVATION_TOKENS = {
    "--ui-shadow-subtle",
    "--ju-shadow",
    "--ju-shadow-raised",
    "--ui-modal-shadow",
    "--td-shadow",
    "--bs-modal-box-shadow",
}
_ELEVATION_ALIASES = {"--ui-modal-shadow": "var(--ju-shadow-raised)", "--td-shadow": "var(--ju-shadow)"}
_SHADOW_COLOUR_FUNCTION = re.compile(r"(?:var|rgb|rgba|hsl|hsla|color-mix)\((?:[^()]|\([^()]*\))*\)")


def _own_css_and_inline_styles() -> list[tuple[str, str]]:
    sources = [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(CSS_DIR.glob("*.css"))
        if ".min." not in path.name
    ]
    sources += [
        (str(path.relative_to(TEMPLATES)), path.read_text(encoding="utf-8"))
        for path in sorted(TEMPLATES.rglob("*.html"))
    ]
    # Vendor sheets without the `.min.` marker (leaflet, material icons) carry no elevation of the application.
    return [(name, text) for name, text in sources if not name.startswith(("leaflet", "material-icons", "gridstack"))]


def _top_level_layers(value: str) -> list[str]:
    layers, depth, current = [], 0, ""
    for char in value:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            layers.append(current.strip())
            current = ""
        else:
            current += char
    return [*layers, current.strip()]


def _has_blur(layer: str) -> bool:
    lengths = [
        part
        for part in _SHADOW_COLOUR_FUNCTION.sub("C", layer).split()
        if re.fullmatch(r"-?\d*\.?\d+(?:px|rem|em)?", part)
    ]
    return len(lengths) >= 3 and float(re.sub(r"[a-z]+", "", lengths[2]) or 0) != 0


def test_elevation_has_three_levels_and_no_literal_blur_shadow() -> None:
    sheets = dict(_own_css_and_inline_styles())
    css = "\n".join(text for name, text in sheets.items() if name.endswith(".css"))
    # Each base level is defined for light and dark; the aliases are defined once as an alias.
    for token in ("--ju-shadow", "--ju-shadow-raised", "--ui-shadow-subtle"):
        assert len(re.findall(rf"(?m)^\s*{re.escape(token)}\s*:", css)) == 2, (
            f"{token} is defined for light and for dark"
        )
    for token, target in _ELEVATION_ALIASES.items():
        values = re.findall(rf"(?m)^\s*{re.escape(token)}\s*:\s*([^;]+);", css)
        assert values and set(values) == {target}, (
            f"{token} aliases {target} instead of carrying its own value: {values}"
        )
    # No fourth level: every custom property named *shadow* (Bootstrap's `--bs-*` aside) is a base token or an alias.
    names = set(re.findall(r"(?m)^\s*(--(?!bs-)[\w-]*shadow[\w-]*)\s*:", css))
    assert names <= _ELEVATION_TOKENS, f"an elevation level outside DC-TOK-5: {sorted(names - _ELEVATION_TOKENS)}"
    offenders = []
    for name, text in sheets.items():
        for match in re.finditer(r"(?<![\w-])box-shadow\s*:\s*([^;{}\"']+)", text):
            for layer in _top_level_layers(match.group(1)):
                if layer in {"none", "inherit", "initial", "unset"} or "inset" in layer.split() or not _has_blur(layer):
                    continue
                if layer.startswith("var(--bs-"):
                    continue  # a Bootstrap component default that the application sheets did not restyle
                if (
                    re.fullmatch(r"var\((--[\w-]+)\)", layer)
                    and re.fullmatch(r"var\((--[\w-]+)\)", layer).group(1) in _ELEVATION_TOKENS
                ):
                    continue
                offenders.append(f"{name}: {layer}")
    assert not offenders, f"an elevation shadow that does not read an elevation token (DC-TOK-2, DC-TOK-5): {offenders}"
