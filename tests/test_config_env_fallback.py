#!/usr/bin/env python3
#
# tests/test_config_env_fallback.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Invalid LOG_LEVEL / JUSTUP_PORT values fall back to the defaults and say so in the log."""

from __future__ import annotations

import logging

from app.utils.config import load_config
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def test_invalid_log_level_and_port_fall_back_with_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    monkeypatch.setenv("JUSTUP_PORT", "eighty")
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        cfg = load_config()
    assert (cfg.log_level, cfg.port) == ("INFO", 8000)
    assert "LOG_LEVEL" in caplog.text and "JUSTUP_PORT" in caplog.text


def test_valid_values_are_used_without_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("JUSTUP_PORT", "9001")
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        cfg = load_config()
    assert (cfg.log_level, cfg.port) == ("DEBUG", 9001)
    assert not caplog.records
