#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""FastAPI application factory and startup lifecycle wiring."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .utils.banner import print_banner
from .utils.config import load_config
from .utils import config
from .utils.rate_limit import limiter
from .utils.request_id import RequestIDMiddleware
from .middleware.csrf import CSRFMiddleware
from .utils.crypto import validate_encryption_key, MissingEncryptionKeyError
from .utils.config import validate_runtime_config, ConfigValidationError

from .utils import bootstrap
from .db import pop_locations
from .services.pop_codes import set_pop_db_connection_factory
from .services import pop_refresh
from .api import alerts as alerts_api
from .api import frontend as frontend_ui
from .api import groups as groups_api
from .api import health as health_api
from .api import metrics as metrics_api
from .api import pop as pop_api
from .api import quality as quality_api
from .api import recipients as recipients_api
from .api import reports as reports_api
from .api import settings as settings_api
from .api import signal as signal_api
from .api import targets as targets_api
from .api import users as users_api
from .db import sqlite as sqlite_db
from .monitor.scheduler import MonitorScheduler
from .services import pdf_worker
from .services.notifications.signal_backend import get_signal_backend, reset_signal_backend
from .services import telemetry as telemetry_service

_log = logging.getLogger(__name__)


@contextmanager
def _db_connection(factory):
	"""Context manager for database connections with guaranteed cleanup."""
	conn = factory()
	try:
		yield conn
	finally:
		conn.close()


async def _graceful_cancel(task: asyncio.Task | None, *, timeout: float = 5.0, label: str = "task") -> None:
	"""Stop a background task gracefully with timeout, then force cancel."""
	if task is None or task.done():
		return
	
	# Wait passively for task to complete (stop events were set beforehand)
	done, _ = await asyncio.wait({task}, timeout=timeout)
	
	if task not in done:
		_log.debug("Task %s did not stop in %.1fs, cancelling", label, timeout)
		task.cancel()
		try:
			await task
		except asyncio.CancelledError:
			pass


def _setup_logging(log_level: str) -> None:
	"""Configure logging level from config."""
	level = getattr(logging, log_level, logging.INFO)
	logging.getLogger().setLevel(level)
	# Set all uvicorn loggers including access to the configured level
	# Note: uvicorn.access may not exist if not using uvicorn server
	for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
		logging.getLogger(logger_name).setLevel(level)


@asynccontextmanager
async def _lifespan(app: FastAPI):
	"""
	Application lifespan manager.
	
	Handles startup (bootstrap, scheduler) and shutdown (cleanup).
	Uses context manager pattern to guarantee cleanup even on errors.
	"""
	cfg = app.state.cfg
	# Initialize all task variables for safe cleanup even if startup fails
	scheduler = None
	signal_backend = None
	pdf_worker_task = None
	pdf_cleanup_task = None
	pop_refresh_task = None
	pdf_stop_event = None
	pop_refresh_stop_event = None
	telemetry_task = None
	telemetry_stop_event = None
	
	# ─── BOOTSTRAP ───────────────────────────────────────────
	bootstrap.bootstrap(cfg.db_path, cfg.tsdb_dir, cfg.data_dir)
	
	# ─── SIGNAL BACKEND ──────────────────────────────────────
	# Initialize eagerly at startup (not lazy on first use)
	# This ensures any configuration issues are detected immediately
	try:
		signal_backend = get_signal_backend()
	except Exception as e:
		_log.warning("Signal backend initialization failed: %s", e)
	
	# ─── POP LOCATIONS ───────────────────────────────────────
	# Initialize SQLite-based POP location lookups
	
	# Central connection factory for entire app
	def _db_conn_factory(path=None):
		"""Create a database connection. Path argument is optional (uses cfg.db_path if not provided)."""
		return sqlite_db.connect(path or cfg.db_path)
	
	with _db_connection(_db_conn_factory) as conn:
		pop_locations.ensure_pop_locations(conn)
		
		# Recover any stale import runs from previous crash
		stale_runs = pop_locations.recover_stale_import_runs(conn)
		if stale_runs > 0:
			_log.info("Recovered %d stale POP import runs from previous crash", stale_runs)
		
		pop_count = pop_locations.get_pop_location_count(conn)
		active_count = pop_locations.count_active_pop_locations(conn)
		external_count = pop_locations.count_external_pop_locations(conn)
		
		if external_count == 0:
			_log.warning(
				"POP locations: %d entries (seed-only, no external imports yet)",
				pop_count
			)
		else:
			_log.info(
				"POP locations: %d total (%d active, %d from external sources)",
				pop_count, active_count, external_count
			)
		conn.commit()
	
	# Set factory for pop_codes lookups (thread-safe, no persistent connection)
	set_pop_db_connection_factory(_db_conn_factory)
	
	_log.info("Starting scheduler...")
	scheduler = MonitorScheduler(
		get_conn=_db_conn_factory,
		tsdb_dir=cfg.tsdb_dir,
		refresh_seconds=config.MONITOR_REFRESH_SECONDS
	)
	scheduler.start()
	
	# Set scheduler on app.state early (before any potential failures)
	# This ensures request handlers don't get AttributeError during startup
	app.state.scheduler = scheduler
	
	# ─── PDF WORKER ──────────────────────────────────────────
	# Mark any orphaned jobs from previous runs as failed (queue was lost)
	with _db_connection(_db_conn_factory) as conn:
		stale_count = sqlite_db.mark_stale_report_jobs_failed(conn)
		if stale_count > 0:
			_log.info("Marked %d stale PDF jobs as failed (server restart)", stale_count)
		conn.commit()
	
	pdf_worker.init_pdf_worker(cfg.data_dir)
	pdf_stop_event = asyncio.Event()
	pdf_worker_task = asyncio.create_task(
		pdf_worker.run_pdf_worker_loop(_db_conn_factory, pdf_stop_event)
	)
	pdf_cleanup_task = asyncio.create_task(
		pdf_worker.cleanup_expired_jobs(_db_conn_factory, cfg.db_path, pdf_stop_event)
	)
	
	# ─── POP REFRESH TASK ────────────────────────────────────
	# Monthly refresh of POP locations from external sources
	
	# Configurable delays (testable without waiting hours)
	# Validate and clamp to non-negative values
	try:
		POP_INITIAL_DELAY_SECONDS = max(0, int(os.getenv("POP_REFRESH_INITIAL_DELAY", "3600")))
	except ValueError:
		_log.warning("Invalid POP_REFRESH_INITIAL_DELAY, using default 3600s")
		POP_INITIAL_DELAY_SECONDS = 3600
	
	try:
		POP_REFRESH_INTERVAL_SECONDS = max(0, int(os.getenv("POP_REFRESH_INTERVAL", str(30 * 24 * 3600))))
	except ValueError:
		_log.warning("Invalid POP_REFRESH_INTERVAL, using default 30 days")
		POP_REFRESH_INTERVAL_SECONDS = 30 * 24 * 3600
	
	pop_refresh_stop_event = asyncio.Event()
	
	async def _pop_refresh_loop():
		"""Background task for monthly POP location refresh."""
		# Wait for initial delay (or early stop signal)
		try:
			await asyncio.wait_for(
				pop_refresh_stop_event.wait(),
				timeout=POP_INITIAL_DELAY_SECONDS,
			)
			return  # Stop requested during initial delay
		except asyncio.TimeoutError:
			pass  # Normal timeout, proceed to first refresh
		
		while not pop_refresh_stop_event.is_set():
			try:
				_log.info("Starting monthly POP locations refresh")
				# Run synchronous HTTP and DB operations in thread pool to avoid blocking event loop
				results = await asyncio.to_thread(
					pop_refresh.run_full_refresh, _db_conn_factory
				)
				
				# Log summary per source
				for r in results:
					if r.success:
						_log.info("POP refresh %s: %d processed, %d inserted, %d updated, %d retired",
							r.source, r.processed, r.inserted, r.updated, r.retired)
					else:
						_log.warning("POP refresh %s failed: %s", r.source, r.error)
				
				# Log total active POPs after refresh (helps debug "why nothing matches?")
				with _db_connection(_db_conn_factory) as conn:
					active_count = pop_locations.count_active_pop_locations(conn)
					_log.debug("POP refresh complete: %d active locations in DB", active_count)
					
			except Exception as e:
				_log.exception("POP refresh task error: %s", e)
			
			# Wait for next refresh (or stop signal)
			try:
				await asyncio.wait_for(
					pop_refresh_stop_event.wait(),
					timeout=POP_REFRESH_INTERVAL_SECONDS
				)
				break  # Stop event was set
			except asyncio.TimeoutError:
				pass  # Normal timeout, continue loop
	
	pop_refresh_task = asyncio.create_task(_pop_refresh_loop())
	
	# ─── TELEMETRY ───────────────────────────────────────────
	# Background telemetry reporting (24h interval)
	telemetry_stop_event = asyncio.Event()
	telemetry_task = asyncio.create_task(
		telemetry_service.telemetry_loop(_db_conn_factory, telemetry_stop_event)
	)
	
	_log.info("Application started")
	
	try:
		yield  # App runs here
	finally:
		# ─── SHUTDOWN ────────────────────────────────────────────
		_log.info("Shutting down application...")
		
		# 1. Stop scheduler FIRST (most active DB user)
		if scheduler is not None:
			try:
				await scheduler.stop()
				_log.debug("Scheduler stopped")
			except Exception as e:
				_log.warning("Error stopping scheduler: %s", e)
		
		# 2. Stop background tasks
		if pop_refresh_stop_event is not None:
			pop_refresh_stop_event.set()
		await _graceful_cancel(pop_refresh_task, label="pop_refresh")
		
		# Stop telemetry
		if telemetry_stop_event is not None:
			telemetry_stop_event.set()
		await _graceful_cancel(telemetry_task, label="telemetry")
		
		if pdf_stop_event is not None:
			pdf_stop_event.set()
		await _graceful_cancel(pdf_worker_task, label="pdf_worker")
		await _graceful_cancel(pdf_cleanup_task, label="pdf_cleanup")
		pdf_worker.shutdown_pdf_worker()
		
		# 3. Cleanup Signal backend (terminate any lingering link processes)
		# Must happen BEFORE closing DB connections (backend may query DB)
		if signal_backend is not None:
			try:
				# Reset singleton (close + clear global state)
				# This prevents subsequent get_signal_backend() from returning closed instance
				reset_signal_backend()
				_log.debug("Signal backend closed and reset")
			except Exception as e:
				_log.debug("Signal backend cleanup (non-critical): %s", e)
		
		# 3b. Clear global POP DB connection factory (prevent stale references across restarts)
		try:
			from .services.pop_codes import clear_pop_db_connection_factory
			clear_pop_db_connection_factory()
		except Exception as e:
			_log.debug("POP factory cleanup (non-critical): %s", e)
		
		# 4. Close ALL tracked DB connections (graceful shutdown, last step)
		try:
			closed = sqlite_db.close_all_connections()
			_log.debug("Closed %d DB connections", closed)
		except Exception as e:
			_log.warning("Error closing DB connections: %s", e)
		
		_log.info("Shutdown complete")


def create_app() -> FastAPI:
	"""Create and configure the FastAPI application instance."""
	print_banner()
	
	# Configure logging basics before validation (so critical messages are properly formatted)
	from .utils import logging_config  # noqa: F401 — side-effect import
	
	# Validate encryption key FIRST - exit immediately if missing
	try:
		validate_encryption_key()
	except MissingEncryptionKeyError as e:
		_log.critical(str(e))
		logging.shutdown()  # Flush all log handlers
		print(f"\n❌ FATAL: {e}\n", file=sys.stderr)
		# Raise SystemExit (catchable by tests) instead of sys.exit()
		raise SystemExit(1) from e
	
	# Load configuration AFTER validation
	cfg = load_config()
	_setup_logging(cfg.log_level)
	
	# Validate runtime configuration (non-blocking warnings)
	try:
		validate_runtime_config()
	except ConfigValidationError as e:
		# ConfigValidationError indicates issues that don't prevent startup
		# but should be addressed (e.g., deprecated settings, suboptimal config)
		_log.warning("Configuration validation warnings: %s", e)

	# Disable Swagger UI and ReDoc in production (security: avoid exposing API schema)
	app = FastAPI(title="justUp", version="0.1.0", lifespan=_lifespan, docs_url=None, redoc_url=None)
	app.state.cfg = cfg
	app.state.db_path = cfg.db_path
	app.state.tsdb_dir = cfg.tsdb_dir
	app.state.data_dir = cfg.data_dir

	# Middleware stack (LIFO order: last added = first executed)
	# CSRF protection (UI only)
	app.add_middleware(CSRFMiddleware)
	# Request ID middleware (outermost to trace ALL requests including CSRF errors)
	app.add_middleware(RequestIDMiddleware)

	# Rate limiting with slowapi
	app.state.limiter = limiter
	app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

	# Static files (CSS, JS, fonts)
	static_dir = Path(__file__).parent / "static"
	app.mount("/static", StaticFiles(directory=static_dir), name="static")

	# No rate limiting on robots.txt - static, lightweight, legitimate crawlers check frequently
	@app.get("/robots.txt", response_class=PlainTextResponse)
	def robots_txt():
		return "User-agent: *\nDisallow: /"

	app.include_router(users_api.router)
	app.include_router(health_api.router)
	app.include_router(groups_api.router)
	app.include_router(targets_api.router)
	app.include_router(alerts_api.router)
	app.include_router(metrics_api.router)
	app.include_router(settings_api.router)
	app.include_router(signal_api.router)
	app.include_router(recipients_api.router)
	app.include_router(reports_api.router)
	app.include_router(quality_api.router)
	app.include_router(pop_api.router)
	app.include_router(frontend_ui.router)

	return app


__all__ = ["create_app"]
