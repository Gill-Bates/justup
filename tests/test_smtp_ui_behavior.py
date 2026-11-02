#!/usr/bin/env python3
#
# tests/test_smtp_ui_behavior.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Browser run of ``tests/smtp-ui/run.mjs``: STARTTLS switch states and ordered settings saves.

Needs Node.js, Playwright (``PLAYWRIGHT_DIR=/opt/ux-test-tools``) and a browser engine (engines via
``JUSTUP_SMTP_UI_BROWSERS``). Skipped when one is missing, failing instead with
``JUSTUP_SMTP_UI_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_RUNNER = _REPO / "tests" / "smtp-ui" / "run.mjs"
_SKIP_EXIT = 77  # keep in sync with SKIP in run.mjs


def _unavailable(reason: str) -> None:
    if os.environ.get("JUSTUP_SMTP_UI_REQUIRE_BROWSER") == "1":
        pytest.fail(f"SMTP UI browser check required but unavailable: {reason}")
    pytest.skip(reason)


def test_smtp_switch_and_ordered_saves_in_browser() -> None:
    node = shutil.which("node")
    if node is None:
        _unavailable("Node.js is not installed")
    result = subprocess.run(
        [node, str(_RUNNER), "--browsers", os.environ.get("JUSTUP_SMTP_UI_BROWSERS", "chromium")],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={**os.environ, "JUSTUP_PYTHON": sys.executable},
    )
    if result.returncode == _SKIP_EXIT:
        _unavailable(result.stdout.strip() or "Playwright or browser unavailable")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-1000:]
