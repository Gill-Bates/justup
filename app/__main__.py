#!/usr/bin/env python3
#
# app/__main__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Entry point for running the application (python -m app)."""

from __future__ import annotations

import contextlib
import logging
import sys

# Unified log format with timestamp (gray timestamp if TTY)
_is_tty = sys.stderr.isatty()
if _is_tty:
    _LOG_FORMAT = "\033[90m%(asctime)s\033[0m | %(levelname)-7s | \033[36m%(name)s\033[0m | %(message)s"
else:
    _LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _AccessToDebugFilter(logging.Filter):
    """Emit uvicorn access records (logged at INFO) only for LOG_LEVEL=DEBUG, labelled DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.run(log_level=...) resets the access logger level, so the handler decides.
        # The root level is the configured LOG_LEVEL (set by app.main._setup_logging).
        if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
            return False
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


# Uvicorn log config dict (overrides internal defaults)
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "access_debug": {"()": _AccessToDebugFilter},
    },
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
            "filters": ["access_debug"],
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "WARNING", "propagate": False},
    },
}


def make_server_class():
    """Build a uvicorn Server that ends SSE streams as soon as a stop signal arrives."""
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            # Uvicorn waits for open responses before the lifespan shutdown runs, so close the
            # streams now; otherwise they get cancelled after the grace timeout and log an ERROR.
            bus = getattr(getattr(self.config.app, "state", None), "event_bus", None)
            if bus is not None:
                bus.close_threadsafe()
            super().handle_exit(sig, frame)

    return _Server


def main() -> None:
    """Launch uvicorn with the FastAPI app."""
    import uvicorn

    from .main import create_app
    from .utils.config import load_config

    cfg = load_config()
    app = create_app()

    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        # Proxy headers are resolved by utils.client_ip (UPMON_TRUSTED_PROXIES); uvicorn
        # would otherwise rewrite client/scheme from any peer's headers.
        proxy_headers=False,
        log_level=cfg.log_level.lower(),
        log_config=UVICORN_LOG_CONFIG,
        # Safety net only: streams are closed on the stop signal (see make_server_class).
        timeout_graceful_shutdown=5,
    )
    # Uvicorn re-raises the captured SIGINT after a clean shutdown; uvicorn.run() swallows it too.
    with contextlib.suppress(KeyboardInterrupt):
        make_server_class()(config).run()


if __name__ == "__main__":
    sys.exit(main() or 0)
