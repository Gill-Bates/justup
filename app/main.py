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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, frontend_pages, frontend_shared, monitors, passkeys, settings, users
from .api.frontend_shared import RedirectTo, redirect_to_handler
from .db.sqlite_runtime import close_all_connections
from .db.sqlite_schema import ensure_default_admin, ensure_schema
from .db.sqlite_settings import validate_secret_key
from .middleware.csrf import CSRFMiddleware
from .middleware.security_headers import SecurityHeadersMiddleware
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
	await scheduler.stop_graceful()
	close_all_connections()
	_log.info("Bye.")


def create_app() -> FastAPI:
	app = FastAPI(title=APP_NAME, version=VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)

	# ── Middleware ──
	app.state.limiter = limiter
	app.add_middleware(RequestIDMiddleware)
	app.add_middleware(CSRFMiddleware)
	app.add_middleware(SecurityHeadersMiddleware)

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
	app.include_router(settings.router, prefix="/api/settings")

	# ── Frontend routes ──
	app.include_router(frontend_pages.router)
	app.include_router(frontend_shared.router)

	# ── Swagger (admin-only, toggleable) ──
	_register_swagger_routes(app)

	# ── Root redirect ──
	@app.get("/")
	def root_redirect():
		return RedirectResponse(url="/ui/dashboard", status_code=303)

	return app


def _register_swagger_routes(app: FastAPI) -> None:
	"""Register admin-protected Swagger UI at /swagger."""
	import sqlite3
	from fastapi import Depends, HTTPException
	from .api.auth import require_admin
	from .utils.deps import get_conn
	from .db.sqlite_settings import get_setting

	_SWAGGER_ENABLE_KEY = "enable_swagger"
	_SWAGGER_TRUTHY = {"1", "true", "yes", "on"}

	def _is_swagger_enabled(conn: sqlite3.Connection) -> bool:
		value = get_setting(conn, _SWAGGER_ENABLE_KEY, "0")
		return str(value or "").strip().lower() in _SWAGGER_TRUTHY

	@app.get("/swagger/openapi.json", include_in_schema=False)
	async def swagger_openapi_json(_=Depends(require_admin), conn: sqlite3.Connection = Depends(get_conn)):
		if not _is_swagger_enabled(conn):
			raise HTTPException(status_code=404, detail="Swagger API disabled")
		return JSONResponse(content=app.openapi())

	@app.get("/swagger", include_in_schema=False)
	async def swagger_ui(_=Depends(require_admin), conn: sqlite3.Connection = Depends(get_conn)):
		if not _is_swagger_enabled(conn):
			raise HTTPException(status_code=404, detail="Swagger API disabled")
		return HTMLResponse(_SWAGGER_HTML)


_SWAGGER_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{APP_NAME} – API Docs</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css"
        integrity="sha384-OiJUz2Or7cLjcY1Eaw2xhMeUY3z5Csh2+HG9WXElrCqx45ddJCnYXN0a/HQQsJtz"
        crossorigin="anonymous">
  <style>
    html {{ box-sizing: border-box; overflow-y: scroll; }}
    body {{ margin: 0; background: #fafafa; }}
    .swagger-ui .topbar {{ display: none; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js"
          integrity="sha384-BxL6Z8PoHDrYi8O8M1NBMsFQH7sRaSmCF6y7iWMN6ijIc0+QfMwJ3ZqY5rkGNwmq"
          crossorigin="anonymous"></script>
  <script>
    SwaggerUIBundle({{
      url: "/swagger/openapi.json",
      dom_id: "#swagger-ui",
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset,
      ],
      layout: "BaseLayout",
      deepLinking: true,
    }});
  </script>
</body>
</html>
"""
