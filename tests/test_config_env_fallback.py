#!/usr/bin/env python3
#
# tests/test_config_env_fallback.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Invalid LOG_LEVEL / JUSTUP_PORT values fall back to the defaults and say so in the log."""

from __future__ import annotations

import logging

import pytest

from app.utils import config as config_module
from app.utils.config import Config, ConfigValidationError, load_config
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def test_invalid_log_level_and_port_fall_back_with_warning(clean_env, caplog):
    clean_env.setenv("LOG_LEVEL", "LOUD")
    clean_env.setenv("JUSTUP_PORT", "eighty")
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        cfg = load_config()
    assert (cfg.log_level, cfg.port) == ("INFO", 8000)
    assert "LOG_LEVEL" in caplog.text and "JUSTUP_PORT" in caplog.text


def test_valid_values_are_used_without_warning(clean_env, caplog):
    clean_env.setenv("LOG_LEVEL", "debug")
    clean_env.setenv("JUSTUP_PORT", "9001")
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        cfg = load_config()
    assert (cfg.log_level, cfg.port) == ("DEBUG", 9001)
    assert not caplog.records


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No .env file and none of the server variables set, so only the defaults can apply."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *_a, **_k: None)
    for name in ("JUSTUP_HOST", "JUSTUP_PORT", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    return monkeypatch


@pytest.mark.usefixtures("clean_env")
def test_missing_variables_give_the_documented_defaults():
    cfg = load_config()
    assert (cfg.host, cfg.port, cfg.log_level) == ("0.0.0.0", 8000, "INFO")  # noqa: S104 - documented default
    defaults = Config(*(cfg.base_dir, cfg.db_path, cfg.tsdb_dir, cfg.data_dir))
    assert (defaults.host, defaults.port, defaults.log_level) == (cfg.host, cfg.port, cfg.log_level)


def test_explicit_values_are_used(clean_env):
    clean_env.setenv("JUSTUP_HOST", "127.0.0.1")
    clean_env.setenv("JUSTUP_PORT", "9090")
    clean_env.setenv("LOG_LEVEL", "warning")
    cfg = load_config()
    assert (cfg.host, cfg.port, cfg.log_level) == ("127.0.0.1", 9090, "WARNING")


def test_invalid_log_level_falls_back_to_the_default(clean_env, caplog):
    clean_env.setenv("LOG_LEVEL", "LOUD")
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        assert load_config().log_level == "INFO"
    assert "LOG_LEVEL" in caplog.text


@pytest.mark.parametrize("raw", ["1", "80", "65535"])
def test_ports_inside_the_range_are_accepted(clean_env, raw):
    clean_env.setenv("JUSTUP_PORT", raw)
    assert load_config().port == int(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999"])
def test_ports_outside_the_range_are_rejected(clean_env, raw):
    clean_env.setenv("JUSTUP_PORT", raw)
    with pytest.raises(ConfigValidationError, match="JUSTUP_PORT must be between 1 and 65535"):
        load_config()


@pytest.mark.parametrize("raw", ["eighty", "", "80.5", "8 0"])
def test_non_numeric_port_keeps_the_fallback_to_the_default(clean_env, caplog, raw):
    clean_env.setenv("JUSTUP_PORT", raw)
    with caplog.at_level(logging.WARNING, logger="app.utils.config"):
        assert load_config().port == 8000
    assert "JUSTUP_PORT" in caplog.text
