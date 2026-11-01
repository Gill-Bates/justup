#!/usr/bin/env python3
#
# tests/test_ui_layout_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Browser layout regression check (tests/layout/run.mjs).

Run locally: ``PLAYWRIGHT_DIR=/path/to/playwright pytest -q tests/test_ui_layout_contract.py``.
Select engines with ``JUSTUP_LAYOUT_BROWSERS`` (default ``chromium``) and the matrix with
``JUSTUP_LAYOUT_PROFILE`` (``quick`` or ``full``). Without Node, Playwright or a browser the test
is skipped; with ``JUSTUP_LAYOUT_REQUIRE_BROWSER=1`` missing coverage fails instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_RUNNER = _REPO / "tests" / "layout" / "run.mjs"
_SKIP_EXIT = 77  # keep in sync with SKIP in run.mjs


def _unavailable(reason: str) -> None:
    """Skip, or fail when the browser run was explicitly required."""
    if os.environ.get("JUSTUP_LAYOUT_REQUIRE_BROWSER") == "1":
        pytest.fail(
            f"Layout check required (JUSTUP_LAYOUT_REQUIRE_BROWSER=1) but unavailable "
            f"for browsers '{os.environ.get('JUSTUP_LAYOUT_BROWSERS', 'chromium')}': {reason}"
        )
    pytest.skip(reason)


def test_layout_contract_holds_in_browser() -> None:
    node = shutil.which("node")
    if node is None:
        _unavailable("Node.js is not installed")
    result = subprocess.run(
        [
            node,
            str(_RUNNER),
            "--profile",
            os.environ.get("JUSTUP_LAYOUT_PROFILE", "quick"),
            "--browsers",
            os.environ.get("JUSTUP_LAYOUT_BROWSERS", "chromium"),
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    if result.returncode == _SKIP_EXIT or result.stdout.startswith("0 audits"):
        _unavailable(result.stdout.strip() or "Playwright or browser unavailable")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-1000:]


@pytest.mark.parametrize("strict", [False, True])
def test_unavailable_browser_skips_or_fails(monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    """Runner exit 77 skips by default and fails in strict mode."""
    monkeypatch.setattr(shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], _SKIP_EXIT, "SKIP firefox: missing", ""),
    )
    if strict:
        monkeypatch.setenv("JUSTUP_LAYOUT_REQUIRE_BROWSER", "1")
    else:
        monkeypatch.delenv("JUSTUP_LAYOUT_REQUIRE_BROWSER", raising=False)
    with pytest.raises(pytest.fail.Exception if strict else pytest.skip.Exception, match="missing"):
        test_layout_contract_holds_in_browser()
