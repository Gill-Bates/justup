#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, frontend_pages, frontend_shared, monitors, passkeys, users
from .api.frontend_shared import RedirectTo, redirect_to_handler
from .db.sqlite_runtime import close_all_connections
from .db.sqlite_schema import ensure_default_admin, ensure_schema
from .db.sqlite_settings import validate_secret_key
from .middleware.csrf import CSRFMiddleware
from .tasks.checker import start_checker, stop_checker
from .utils.banner import print_banner
from .utils.config import load_config
from .utils.rate_limit import limiter
from .utils.request_id import RequestIDMiddleware
from .utils.scheduler import Scheduler
from .utils.version import APP_NAME, VERSION

_log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
	cfg = load_config()
	log_level = getattr(logging, cfg.log_level.upper(), logging.INFO)
	logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
	_log.info("Starting %s v%s", APP_NAME, VERSION)
	print_banner()

	# Ensure DB schema
	ensure_schema(cfg.db_path)
	ensure_default_admin(cfg.db_path)
	validate_secret_key(cfg.db_path)

	# Store config in app state
	app.state.cfg = cfg
	app.state.db_path = cfg.db_path
	app.state.tsdb_dir = cfg.tsdb_dir

	# Start scheduler
	scheduler = Scheduler()
	app.state.scheduler = scheduler
	await scheduler.start()

	# Start uptime checker
	await start_checker(scheduler, cfg)

	yield

	_log.info("Shutting down %s …", APP_NAME)
	await stop_checker(scheduler)
	await scheduler.stop()
	close_all_connections()
	_log.info("Bye.")


def create_app() -> FastAPI:
	app = FastAPI(title=APP_NAME, version=VERSION, lifespan=_lifespan, docs_url="/swagger", redoc_url=None)

	# ── Middleware ──
	app.state.limiter = limiter
	app.add_middleware(RequestIDMiddleware)
	app.add_middleware(CSRFMiddleware)

	# ── Exception handlers ──
	app.add_exception_handler(RedirectTo, redirect_to_handler)

	# ── Static files ──
	static_dir = Path(__file__).parent / "static"
	if static_dir.is_dir():
		app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

	# ── API routes ──
	app.include_router(auth.router, prefix="/api/auth")
	app.include_router(users.router, prefix="/api/users")
	app.include_router(passkeys.router, prefix="/api/passkeys")
	app.include_router(monitors.router, prefix="/api/monitors")

	# ── Frontend routes ──
	app.include_router(frontend_pages.router)
	app.include_router(frontend_shared.router)

	# ── Root redirect ──
	@app.get("/")
	def root_redirect():
		return RedirectResponse(url="/ui/dashboard", status_code=303)

	return app
