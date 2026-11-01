#!/usr/bin/env python3
#
# app/utils/logging_config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Early logging configuration.

This module is imported before uvicorn starts to ensure all logs
(including uvicorn's startup messages) have consistent formatting.
"""

from __future__ import annotations

import logging
import sys
import time


class HealthEndpointFilter(logging.Filter):
    """Filter that limits /health endpoint logging to once per minute.

    Under ``python -m app`` uvicorn's log_config replaces these handlers, so the filter
    only applies when uvicorn is started without that config.
    """

    def __init__(self, interval: int = 60):
        super().__init__()
        self.interval = interval
        self._last_health_log: float = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/health" in msg and "GET" in msg:
            now = time.time()
            if now - self._last_health_log < self.interval:
                return False
            self._last_health_log = now
        return True


# Unified format: timestamp | level | logger | message
_is_tty = sys.stderr.isatty()
if _is_tty:
    _fmt = "\033[90m%(asctime)s\033[0m | %(levelname)-7s | \033[36m%(name)s\033[0m | %(message)s"
else:
    _fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

_datefmt = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=_fmt,
    datefmt=_datefmt,
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)

# Apply same format to uvicorn loggers (including access logs)
_formatter = logging.Formatter(_fmt, datefmt=_datefmt)
_health_filter = HealthEndpointFilter(interval=60)

for _logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_log = logging.getLogger(_logger_name)
    _uv_log.handlers.clear()
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(_formatter)
    if _logger_name == "uvicorn.access":
        _handler.addFilter(_health_filter)
    _uv_log.addHandler(_handler)
    _uv_log.propagate = False

# Reduce noise from HTTP clients
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
