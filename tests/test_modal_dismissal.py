#!/usr/bin/env python3
#
# tests/test_modal_dismissal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Modal dismissal policy: no close on backdrop click/tap; Escape and explicit controls close.

Static contracts over templates and scripts, plus the browser run of ``tests/modal/run.mjs``
(``PLAYWRIGHT_DIR=/opt/ux-test-tools``, engines via ``JUSTUP_MODAL_BROWSERS``; skipped when
Node, Playwright or an engine is missing, failing instead with ``JUSTUP_MODAL_REQUIRE_BROWSER=1``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_APP = _REPO / "app"
_RUNNER = _REPO / "tests" / "modal" / "run.mjs"
_SKIP_EXIT = 77  # keep in sync with SKIP in run.mjs
_MODAL_RE = re.compile(r'<div\b[^>]*\bclass="modal(?:\s[^"]*)?"[^>]*>', re.DOTALL)


def _modal_templates() -> list[Path]:
    return [p for p in (_APP / "templates").rglob("*.html") if 'class="modal ' in p.read_text(encoding="utf-8")]


def _modal_blocks(text: str) -> list[str]:
    """Split a template into one chunk per modal root element."""
    starts = [m.start() for m in _MODAL_RE.finditer(text)]
    return [text[s:e] for s, e in zip(starts, [*starts[1:], len(text)], strict=True)]


def test_modals_exist() -> None:
    assert _modal_templates()


def test_backdrop_dismissal_is_disabled_centrally() -> None:
    app_js = (_APP / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"Modal\.Default\.backdrop\s*=\s*'static'", app_js)
    assert re.search(r"Modal\.Default\.keyboard\s*=\s*true", app_js)


def test_no_modal_overrides_static_backdrop_or_escape() -> None:
    # A per-modal attribute would silently re-enable backdrop dismissal or disable Escape.
    for path in _modal_templates():
        for tag in _MODAL_RE.findall(path.read_text(encoding="utf-8")):
            assert 'data-bs-backdrop="true"' not in tag and "data-bs-backdrop='true'" not in tag, f"{path.name}: {tag}"
            assert 'data-bs-keyboard="false"' not in tag, f"{path.name}: {tag}"


def test_modal_scripts_do_not_close_on_outside_click_or_override_backdrop() -> None:
    forbidden = (
        re.compile(r"Modal[^\n]*backdrop\s*:\s*(true|'true'|\"true\")"),
        re.compile(r"Modal[^\n]*keyboard\s*:\s*false"),
        re.compile(r"\.modal-backdrop[^\n]*addEventListener"),
        re.compile(
            r"(?:click|mousedown|mouseup|pointerdown|pointerup|touchstart|touchend)[^\n]*modal\.hide\(",
            re.IGNORECASE,
        ),
    )
    scripts = [p for p in (_APP / "static" / "js").rglob("*.js") if ".min." not in p.name and "vendor" not in p.parts]
    scripts += list((_APP / "templates").rglob("*.html"))
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not pattern.search(text), f"{path.relative_to(_REPO)}: {pattern.pattern}"


def test_native_dialogs_are_not_used_without_a_cancel_policy() -> None:
    # A native <dialog> would need its own backdrop policy; fail so the new pattern is reviewed.
    for path in (_APP / "templates").rglob("*.html"):
        assert "<dialog" not in path.read_text(encoding="utf-8"), path.name
    for path in (_APP / "static" / "js").rglob("*.js"):
        if ".min." not in path.name:
            assert ".showModal(" not in path.read_text(encoding="utf-8").replace("export function showModal", ""), (
                path.name
            )


def test_every_modal_has_aria_name_and_visible_close_controls() -> None:
    for path in _modal_templates():
        for block in _modal_blocks(path.read_text(encoding="utf-8")):
            ident = _MODAL_RE.search(block).group(0)  # type: ignore[union-attr]
            assert "aria-labelledby=" in ident, f"{path.name}: {ident}"
            header_close = re.search(
                r'<button\b[^>]*\bbtn-close\b[^>]*data-bs-dismiss="modal"[^>]*>', block, re.DOTALL
            ) or re.search(r'<button\b[^>]*data-bs-dismiss="modal"[^>]*\bbtn-close\b[^>]*>', block, re.DOTALL)
            assert header_close and "aria-label=" in header_close.group(0), (
                f"{path.name}: no labelled X button in {ident}"
            )
            footer = re.search(r'<div class="modal-footer[^"]*">(.*?)</div>\s*</div>', block, re.DOTALL)
            assert footer and re.search(r'data-bs-dismiss="modal"[^>]*>\s*(Cancel|Close)\b', footer.group(1)), (
                f"{path.name}: no visible Cancel/Close text button in {ident}"
            )


def _unavailable(reason: str) -> None:
    if os.environ.get("JUSTUP_MODAL_REQUIRE_BROWSER") == "1":
        pytest.fail(f"Modal browser check required but unavailable: {reason}")
    pytest.skip(reason)


def test_modal_dismissal_policy_in_browser() -> None:
    node = shutil.which("node")
    if node is None:
        _unavailable("Node.js is not installed")
    result = subprocess.run(
        [node, str(_RUNNER), "--browsers", os.environ.get("JUSTUP_MODAL_BROWSERS", "chromium")],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode == _SKIP_EXIT:
        _unavailable(result.stdout.strip() or "Playwright or browser unavailable")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-1000:]


def _shipped_sources() -> list[Path]:
    js = [p for p in (_APP / "static" / "js").rglob("*.js") if ".min." not in p.name and "umd" not in p.name]
    return [*js, *(_APP / "templates").rglob("*.html")]


def test_destructive_actions_use_the_shared_confirm_dialog_not_native_confirm() -> None:
    native = re.compile(r"(?<![\w.])(?:window\.)?confirm\(")
    offenders = [
        p.relative_to(_REPO).as_posix() for p in _shipped_sources() if native.search(p.read_text(encoding="utf-8"))
    ]
    # confirm-dialog.js mentions "confirm()" only in its header comment; real calls are confirmAction(.
    assert [o for o in offenders if not o.endswith("utils/confirm-dialog.js")] == []


def test_shared_confirm_dialog_is_static_cancel_focused_and_loaded_before_use() -> None:
    script = (_APP / "static" / "js" / "utils" / "confirm-dialog.js").read_text(encoding="utf-8")
    assert 'data-bs-backdrop="static"' in script and 'data-bs-keyboard="true"' in script
    assert "ju-confirm-cancel').focus()" in script  # safe default focus
    assert "current.opener.focus()" in script  # focus returns to the invoking element
    base = (_APP / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("bootstrap.bundle.min.js") < base.index("utils/confirm-dialog.js") < base.index("js/app.js")


def test_mobile_sidebar_backdrop_click_still_closes_the_drawer() -> None:
    # Deliberate difference to modals: the sidebar drawer is a disclosure, so the backdrop dismisses it.
    sidebar = (_APP / "static" / "js" / "sidebar.js").read_text(encoding="utf-8")
    assert "backdrop.addEventListener('click', function () { closeDrawer(true); })" in sidebar
    assert "evt.key === 'Escape'" in sidebar and "closeBtn.addEventListener('click'" in sidebar
