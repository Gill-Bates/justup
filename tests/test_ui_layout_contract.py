#!/usr/bin/env python3
#
# tests/test_ui_layout_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static CSS contracts shared with the browser layout suite."""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def test_application_css_loads_no_external_assets() -> None:
    """DESIGN.md: CSS, fonts and icons are local; no stylesheet fetches an asset at runtime."""
    css_dir = _REPO / "app" / "static" / "css"
    external = re.compile(r"(?:url\(\s*|@import\s+)[\"']?(?:https?:)?//", re.IGNORECASE)
    sheets = [p for p in css_dir.glob("*.css") if ".min." not in p.name]
    assert sheets, "no application stylesheet found"
    offenders = [p.name for p in sheets if external.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"external runtime assets in: {offenders}"


def test_dashboard_downtime_link_preset_exists_on_target_detail() -> None:
    """The widget's range preset must be one the target page's time-range select (and parser) offers."""
    widget = (_REPO / "app/static/js/dashboard.js").read_text(encoding="utf-8")
    selector = (_REPO / "app/templates/fragments/target_detail/time_range_selector.html").read_text(encoding="utf-8")
    match = re.search(r"LOOKBACK_RANGE_PRESET = '([a-z0-9]+)'", widget)
    assert match is not None, "dashboard.js must name the preset its downtime links carry"
    assert f'<option value="{match.group(1)}"' in selector


def _style_sources() -> list[tuple[str, str]]:
    css_files = (_REPO / "app/static/css").glob("*.css")
    css = [(p.name, p.read_text(encoding="utf-8")) for p in css_files if ".min." not in p.name]
    pages = []
    for page in (_REPO / "app/templates").rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE)
        if blocks:
            pages.append((str(page.relative_to(_REPO)), "\n".join(blocks)))
    return css + pages


def test_safe_area_insets_are_additive_terms_only() -> None:
    """DC-VP-5: env(safe-area-inset-*) appears only inside max() or calc(); viewport-fit stays default."""
    offenders = []
    for name, text in _style_sources():
        for match in re.finditer(r"env\(\s*safe-area-inset-", text):
            enclosing = []
            for char_match in re.finditer(r"([A-Za-z-]*)\(|\)", text[: match.start()]):
                if char_match.group(0) == ")":
                    if enclosing:
                        enclosing.pop()
                else:
                    enclosing.append(char_match.group(1).lower())
            if not any(fn in {"max", "calc"} for fn in enclosing):
                offenders.append(f"{name}: {text[max(0, match.start() - 30) : match.end() + 30]!r}")
    assert not offenders, offenders
    for page in (_REPO / "app/templates").rglob("*.html"):
        assert "viewport-fit=cover" not in page.read_text(encoding="utf-8"), page.name


def test_template_style_blocks_set_no_private_focus_ring() -> None:
    """DC-FOCUS-1: inline <style> blocks do not set their own focus box-shadow ring or outline colour/width."""
    focus_rule = re.compile(r"([^{}]*:focus(?:-visible|-within)?[^{}]*)\{([^{}]*)\}")
    offenders = []
    for name, text in _style_sources():
        if name.endswith(".css"):
            continue
        for selector, body in focus_rule.findall(text):
            for decl in re.findall(r"(outline[\w-]*|box-shadow)\s*:\s*([^;]+)", body):
                prop, value = decl
                shadow_off = value.strip() in {"none", "unset", "initial"}
                outline_off = value.strip() in {"none", "0", "unset", "initial"}
                shadow_bad = prop == "box-shadow" and not shadow_off
                outline_bad = prop.startswith("outline") and "var(--ju-focus" not in value and not outline_off
                if shadow_bad or outline_bad:
                    offenders.append(f"{name}: {selector.strip()} {prop}: {value.strip()}")
    assert not offenders, offenders


def test_mail_content_link_colour_stays_inside_the_frame_document() -> None:
    """DC-PD-1: #0d6efd exists only as a style injected into the mail frame document, never in app UI."""
    needle = "#0d6efd"
    hits = [
        p.relative_to(_REPO).as_posix()
        for root in ("app/static/css", "app/templates")
        for p in (_REPO / root).rglob("*")
        if p.is_file()
        and p.suffix in {".css", ".html"}
        and ".min." not in p.name
        and needle in p.read_text(encoding="utf-8").lower()
    ]
    assert not hits, f"app stylesheets and templates must not use the mail link colour: {hits}"
    source = (_REPO / "app/static/js/settings.smtp-template.js").read_text(encoding="utf-8")
    block = source[source.index("doc.createElement('style')") : source.index("doc.head.appendChild(style)")]
    assert needle in block.lower(), "the colour must sit in the style appended to the frame document head"
    assert source.lower().count(needle) == 1, "the colour must appear once, in that style only"


_VENDOR_CSS = {"bootstrap.min.css", "gridstack.min.css", "leaflet.css", "material-icons.css"}


def _own_css_rules(text: str) -> list[tuple[str, str]]:
    """Leaf rules (selector, body) of an application stylesheet; at-rule wrappers are transparent."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return [(" ".join(sel.split()), body) for sel, body in re.findall(r"([^{}@;]+)\{([^{}]*)\}", text)]


def test_media_query_sizes_are_listed_in_the_design_breakpoint_rule() -> None:
    """DC-BP-1: every viewport size in an @media query of application CSS or inline style is named in DESIGN.md."""
    design = (_REPO / "DESIGN.md").read_text(encoding="utf-8")
    rule = next(line for line in design.splitlines() if line.startswith("- **DC-BP-1**"))
    listed = {float(n) for n in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", rule)}
    used: dict[float, set[str]] = {}
    for name, text in _style_sources():
        if name in _VENDOR_CSS or ".min." in name:
            continue
        for query in re.findall(r"@media([^{]*)\{", text):
            for value in re.findall(r"(?:(?:min|max)-)?(?:width|height)\s*(?::|<=|>=|<|>)\s*([0-9.]+)px", query):
                # Bootstrap-style 575.98 and the 359 px companion of 360 round up to the listed size.
                size = float(value)
                size = float(round(size + 0.02)) if size % 1 else size + (1 if size == 359 else 0)
                used.setdefault(size, set()).add(name)
    unlisted = {size: sorted(files) for size, files in used.items() if size not in listed}
    assert used, "no @media size found; the extraction is broken"
    assert not unlisted, f"viewport sizes missing from DC-BP-1: {unlisted}"
    assert {992.0, 576.0, 480.0, 360.0} <= set(used), "documented application breakpoints are no longer used"


def test_no_visual_viewport_api_in_application_scripts() -> None:
    """DC-VP-3: window.visualViewport is not used (no device failure is recorded that would allow it)."""
    offenders = [
        p.relative_to(_REPO).as_posix()
        for p in (_REPO / "app/static/js").rglob("*.js")
        if ".min." not in p.name and "bundle" not in p.name and "visualViewport" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_dashboard_grid_fallbacks_equal_the_css_token_defaults() -> None:
    """DC-GRID-5: dashboard-grid.js reads the dashboard.css tokens and its fallbacks equal their defaults."""
    css = (_REPO / "app/static/css/dashboard.css").read_text(encoding="utf-8")
    js = (_REPO / "app/static/js/dashboard-grid.js").read_text(encoding="utf-8")
    ui = (_REPO / "app/static/css/ui.css").read_text(encoding="utf-8")

    def token(name: str) -> str:
        return re.search(rf"{name}:\s*([^;]+);", css).group(1).strip()

    def px(value: str) -> float:
        number = float(re.match(r"[0-9.]+", value).group(0))
        return number * 16 if value.endswith("rem") else number

    tile = px(re.search(r"--ui-space-tile:\s*([^;]+);", ui).group(1).strip())
    assert (
        token("--ju-dash-gutter") == "calc(var(--ju-dash-gap) / 2)" and token("--ju-dash-gap") == "var(--ui-space-tile)"
    )
    defaults = {n: float(v) for n, v in re.findall(r"const (DEFAULT_[A-Z_]+) = ([0-9.]+);", js)}
    assert defaults["DEFAULT_GRID_MARGIN"] == tile / 2
    assert defaults["DEFAULT_CELL_HEIGHT"] == px(token("--ju-dash-cell-height"))
    assert defaults["DEFAULT_HANDLE_HEIGHT"] == px(token("--ju-dash-handle-height"))
    for name in ("--ju-dash-gutter", "--ju-dash-cell-height", "--ju-dash-handle-height"):
        assert f"cssPixels('{name}'" in js, f"dashboard-grid.js does not read {name}"


def test_pages_do_not_restyle_the_shared_card_frame() -> None:
    """DC-CARD-7: border, radius, surface and shadow of .card and its header/body/footer live in the shared sheets."""
    owners = {"gentelella-layout.css", "ui.css"}
    frame = re.compile(r"^(border(-[a-z-]+)?|background(-[a-z]+)?|box-shadow)$")
    offenders = []
    for name, text in _style_sources():
        if name in owners or name in _VENDOR_CSS or ".min." in name:
            continue
        for selector, body in _own_css_rules(text):
            for part in selector.split(","):
                last = part.strip().split()[-1] if part.strip() else ""
                if "::" in last or not re.search(r"\.card(?:-header|-body|-footer)?$", last):
                    continue
                offenders += [
                    f"{name}: {part.strip()} {p}" for p in re.findall(r"([\w-]+)\s*:", body) if frame.match(p)
                ]
    assert not offenders, offenders


def test_scroll_regions_contain_overscroll() -> None:
    """DC-SCROLL-4: every nested region with overflow auto or scroll sets overscroll-behavior (main is level 1)."""
    scrolls = re.compile(r"overflow(?:-[xy])?\s*:[^;]*\b(?:auto|scroll)\b")
    offenders = []
    for name, text in _style_sources():
        if name in _VENDOR_CSS or ".min." in name:
            continue
        for selector, body in _own_css_rules(text):
            if (
                scrolls.search(body)
                and "overscroll-behavior" not in body
                and not selector.startswith(".ju-content>main:has(")
            ):
                offenders.append(f"{name}: {selector}")
    assert not offenders, offenders


def test_layout_spec_titles_use_the_effective_zoom_label_only_for_effective_zoom() -> None:
    """DC-RESP-3: a spec title says "200 % zoom" only together with "effective"."""
    titles = re.compile(r"^\s*(?:\w*[tT]est|describe)(?:\.\w+)?\(\s*([`\"'])(.*?)\1", re.MULTILINE)
    offenders = []
    for spec in (_REPO / "tests/layout").glob("*.spec.mjs"):
        for _quote, title in titles.findall(spec.read_text(encoding="utf-8")):
            if re.search(r"200\s*%\s*zoom", title, re.IGNORECASE) and "effective" not in title.lower():
                offenders.append(f"{spec.name}: {title}")
    assert not offenders, offenders


_OWN_CSS = (
    "ui.css",
    "gentelella-layout.css",
    "dashboard.css",
    "notifications.css",
    "target-detail.css",
    "target-wizard.css",
    "traceroute-map.css",
)
_LITERAL_COLOUR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\((?!var\()|(?<![-\w])(?:white|black)(?![-\w])"
)
# Literal colours outside token definitions that stay, with the reason (DC-TOK-2).
_LITERAL_COLOUR_ALLOWED = {
    # alpha mask of the tab strip fade: the colour channel is never painted
    (
        "ui.css",
        (
            "mask-image: linear-gradient(to right, transparent, black var(--ui-tab-fade-start), "
            "black calc(100% - var(--ui-tab-fade-end)), transparent);"
        ),
    ),
    # map water fallback, pinned to route_map_rules.PALETTE by test_route_map_rules.py
    ("traceroute-map.css", "background-color: #e4ecf4;"),
    ("traceroute-map.css", "background-color: #0c1726;"),
}


def test_colour_literals_stay_in_token_definitions() -> None:
    """DC-TOK-2: a raw colour appears only in a custom-property definition or in a listed, reasoned exception."""
    offenders = []
    for name in _OWN_CSS:
        text = re.sub(r"/\*.*?\*/", "", (_REPO / "app/static/css" / name).read_text(encoding="utf-8"), flags=re.DOTALL)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--") or "white-space" in stripped:
                continue
            if _LITERAL_COLOUR.search(stripped) and (name, stripped) not in _LITERAL_COLOUR_ALLOWED:
                offenders.append(f"{name}: {stripped}")
    assert not offenders, offenders


def test_focus_like_outline_width_reads_the_focus_token() -> None:
    """DC-FOCUS-1: an outline in application CSS takes its width from --ju-focus-outline-width, never a literal."""
    offenders = []
    for name in _OWN_CSS:
        text = (_REPO / "app/static/css" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"outline(-width)?:\s*[^;]*\d+px", stripped) and name != "gentelella-layout.css":
                offenders.append(f"{name}: {stripped}")
    assert not offenders, offenders


def test_important_is_not_used_for_tabs_modals_or_focus() -> None:
    """DC-TOK-4, DC-TAB-5: no !important declaration sits in a rule whose selector targets tabs, modals or focus."""
    selector_pattern = re.compile(
        r"nav-link|nav-pills|nav-tabs|ju-tabs|\[role=\"tab\"\]|\.modal|:focus|focus-visible|\.ju-focus",
        re.IGNORECASE,
    )
    offenders = []
    for name in _OWN_CSS:
        text = re.sub(r"/\*.*?\*/", "", (_REPO / "app/static/css" / name).read_text(encoding="utf-8"), flags=re.DOTALL)
        for selector, body in _own_css_rules(text):
            if "!important" in body and selector_pattern.search(selector):
                offenders.append(f"{name}: {selector}")
    assert not offenders, offenders


def test_shell_rules_and_about_page_have_their_owner_stylesheet() -> None:
    """DC-OWN-1, DC-EXT-6: footer and logo rules live in gentelella-layout.css; About has no inline <style>."""
    ui = (_REPO / "app/static/css/ui.css").read_text(encoding="utf-8")
    shell = (_REPO / "app/static/css/gentelella-layout.css").read_text(encoding="utf-8")
    assert not re.search(r"^\.ju-(?:footer|logo)\b", ui, re.MULTILINE), "shell rules belong to gentelella-layout.css"
    assert re.search(r"^\.ju-footer-link\b", shell, re.MULTILINE)
    assert "<style" not in (_REPO / "app/templates/about.html").read_text(encoding="utf-8")
