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

from gillbates_logging import LineLog


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


# One line per record (UTC time | level | source | message), control characters escaped.
LOG = LineLog("justup")
LOG.setup("INFO")

# Apply same format to uvicorn loggers (including access logs)
_formatter = LOG.formatter()
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
