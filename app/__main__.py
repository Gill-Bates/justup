#!/usr/bin/env python3
#
# app/__main__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Entry point for running the application (python -m app)."""

from __future__ import annotations

import sys


# Unified log format with timestamp (gray timestamp if TTY)
_is_tty = sys.stderr.isatty()
if _is_tty:
    _LOG_FORMAT = "\033[90m%(asctime)s\033[0m | %(levelname)-7s | \033[36m%(name)s\033[0m | %(message)s"
else:
    _LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Uvicorn log config dict (overrides internal defaults)
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": _LOG_FORMAT,
            "datefmt": _DATE_FORMAT,
        },
        "access": {
            "format": _LOG_FORMAT,
            "datefmt": _DATE_FORMAT,
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def main() -> None:
    """Launch uvicorn with the FastAPI app."""
    import uvicorn

    from .utils.config import load_config
    from .main import create_app

    cfg = load_config()
    app = create_app()

    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=cfg.log_level.lower(),
        log_config=UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
