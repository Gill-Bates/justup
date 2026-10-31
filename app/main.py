#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""FastAPI application factory and startup lifecycle wiring."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .api import alerts as alerts_api
from .api import dashboard_layout as dashboard_layout_api
from .api import events as events_api
from .api import frontend as frontend_ui
from .api import groups as groups_api
from .api import health as health_api
from .api import metrics as metrics_api
from .api import notifications as notifications_api
from .api import pop as pop_api
from .api import quality as quality_api
from .api import recipients as recipients_api
from .api import reports as reports_api
from .api import settings as settings_api
from .api import signal as signal_api
from .api import targets as targets_api
from .api import users as users_api
from .db import sqlite as sqlite_db
from .middleware.csrf import CSRFMiddleware
from .middleware.maintenance import MaintenanceWriteMiddleware
from .monitor.scheduler import MonitorScheduler
from .services import event_bus, pdf_worker
from .services.notifications.signal_backend import (
    get_signal_backend,
    reset_signal_backend,
)
from .utils import bootstrap, config
from .utils.banner import print_banner
from .utils.config import ConfigValidationError, load_config, validate_runtime_config
from .utils.crypto import MissingEncryptionKeyError, validate_encryption_key
from .utils.rate_limit import limiter
from .utils.request_id import RequestIDMiddleware
from .utils.supervisor import ServiceSpec, ServiceSupervisor
from .utils.tls_scanner import shutdown_tls_scanner
from .utils.version import get_version

_log = logging.getLogger(__name__)

# Paths that stay reachable while the bootstrap admin password is unchanged.
_BOOTSTRAP_GATE_ALLOWED_EXACT_PATHS = frozenset(
    {
        "/",
        "/health",
        "/healthz",
        "/favicon.ico",
        "/robots.txt",
        "/ui/login",
        "/ui/change-password",
        "/login",
        "/logout",
        "/me/complete-required-change",
    }
)
_BOOTSTRAP_GATE_ALLOWED_PREFIXES = ("/static/",)


@contextmanager
def _db_connection(factory):
    """Context manager for database connections with guaranteed cleanup."""
    conn = factory()
    try:
        yield conn
    finally:
        sqlite_db.close_connection(conn)


# A scheduler iteration can legitimately run long (daily VACUUM, slow checks), so only a long silence is a hang.
SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS = 1800.0

# Pause after an unexpected POP refresh error so a failure that persists no run cannot busy-loop.
POP_ERROR_RETRY_SECONDS = 30.0

# Upper bound per shutdown step; the sum stays below docker-compose `stop_grace_period` (60 s).
SHUTDOWN_STEP_TIMEOUT_SECONDS = 20.0


async def _bounded_stop(awaitable, *, label: str, timeout: float = SHUTDOWN_STEP_TIMEOUT_SECONDS) -> None:
    """Await a shutdown step for at most ``timeout`` seconds; log (never raise) a failure or timeout."""
    try:
        await asyncio.wait_for(awaitable, timeout=timeout)
        _log.debug("%s stopped", label)
    except TimeoutError:
        _log.warning("%s did not stop within %.0fs, continuing shutdown", label, timeout)
    except Exception as e:  # noqa: BLE001 - lifespan boundary: startup/shutdown steps log and continue, never crash the app
        _log.warning("Error stopping %s: %s", label, e)


def _setup_logging(log_level: str) -> None:
    """Configure logging level from config."""
    level = getattr(logging, log_level, logging.INFO)
    logging.getLogger().setLevel(level)
    for logger_name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(logger_name).setLevel(level)
    # uvicorn emits access lines at INFO; keep them to LOG_LEVEL=DEBUG (health checks and HTMX polling are noisy)
    logging.getLogger("uvicorn.access").setLevel(level if level <= logging.DEBUG else logging.WARNING)


def _release_bootstrap_gate(app: FastAPI):
    """Return a callback that lifts the first-boot gate after the admin password changed."""

    def _release() -> None:
        if not app.state.bootstrap_gate_active:
            return
        app.state.bootstrap_gate_active = False
        app.state.bootstrap_gate_completed = True
        _log.info("BOOTSTRAP_GATE released: admin password changed.")

    return _release


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Handles startup (bootstrap, scheduler) and shutdown (cleanup).
    Uses context manager pattern to guarantee cleanup even on errors.
    """
    cfg = app.state.cfg
    app.state.shutting_down = False
    # Initialize all task variables for safe cleanup even if startup fails
    scheduler = None
    signal_backend = None
    pdf_stop_event = None
    pop_refresh_stop_event = None

    # ─── EVENT BUS ───────────────────────────────────────────
    # Started first so no publisher can race a missing loop; closed first on shutdown to end SSE streams.
    live_bus = event_bus.EventBus()
    live_bus.start(asyncio.get_running_loop())
    app.state.event_bus = live_bus
    event_bus.set_current_bus(live_bus)

    # ─── BOOTSTRAP ───────────────────────────────────────────
    admin_created = bootstrap.bootstrap(cfg.db_path, cfg.tsdb_dir, cfg.data_dir)
    app.state.bootstrap_gate_active = admin_created
    app.state.bootstrap_gate_completed = not admin_created
    app.state.bootstrap_gate_release = _release_bootstrap_gate(app)
    if admin_created:
        _log.info(
            "First-boot mode: only login and password change are available until the bootstrap password is changed."
        )

    # ─── SIGNAL BACKEND ──────────────────────────────────────
    # Initialize eagerly at startup (not lazy on first use)
    # This ensures any configuration issues are detected immediately
    try:
        signal_backend = get_signal_backend()
    except Exception as e:  # noqa: BLE001 - lifespan boundary: startup/shutdown steps log and continue, never crash the app
        _log.warning("Signal backend initialization failed: %s", e)

    # ─── POP LOCATIONS ───────────────────────────────────────
    # Initialize SQLite-based POP location lookups
    from .db import pop_locations
    from .services.pop_codes import set_pop_db_connection_factory

    # Central connection factory for entire app
    # Accepts optional db_path for API compatibility with cleanup_loop
    def _db_conn_factory(_db_path: Path | None = None):
        return sqlite_db.connect(cfg.db_path)

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
            _log.warning("POP locations: %d entries (seed-only, no external imports yet)", pop_count)
        else:
            _log.info(
                "POP locations: %d total (%d active, %d from external sources)", pop_count, active_count, external_count
            )
        conn.commit()

    # Set factory for pop_codes lookups (thread-safe, no persistent connection)
    set_pop_db_connection_factory(_db_conn_factory)

    _log.info("Starting scheduler...")
    scheduler = MonitorScheduler(
        get_conn=_db_conn_factory, tsdb_dir=cfg.tsdb_dir, refresh_seconds=config.MONITOR_REFRESH_SECONDS
    )
    # One watchdog restarts every background service with backoff; scheduler hangs are detected by heartbeat.
    supervisor = ServiceSupervisor()
    app.state.supervisor = supervisor
    scheduler.on_heartbeat = lambda: supervisor.heartbeat("monitor_scheduler")
    supervisor.add(
        ServiceSpec(
            name="monitor_scheduler",
            run=scheduler.run,
            request_stop=scheduler.request_stop,
            heartbeat_timeout=SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS,
        )
    )

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
    supervisor.add(
        ServiceSpec(
            name="pdf_worker",
            run=lambda: pdf_worker.run_pdf_worker_loop(_db_conn_factory, pdf_stop_event),
            request_stop=pdf_stop_event.set,
        )
    )
    supervisor.add(
        ServiceSpec(
            name="pdf_cleanup",
            run=lambda: pdf_worker.cleanup_expired_jobs(_db_conn_factory, cfg.db_path, pdf_stop_event),
            request_stop=pdf_stop_event.set,
        )
    )

    # ─── POP REFRESH TASK ────────────────────────────────────
    # Self-managing refresh of the external POP locations (PeeringDB). justUp decides itself when a
    # refresh is due from the persisted pop_import_runs history (last SUCCESSFUL import, not the
    # container start), so a restart never re-downloads fresh data and overdue refreshes are caught
    # up promptly. No user setting controls the interval or the startup delay any more.
    from .services import pop_refresh

    pop_refresh_stop_event = asyncio.Event()

    def _plan_delay() -> float:
        """Compute the next wait from persistent state (connection-scoped, off the event loop)."""
        with _db_connection(_db_conn_factory) as conn:
            return pop_refresh.plan_next_refresh(conn).delay_seconds

    async def _pop_refresh_loop():
        """Background task that refreshes external POP data on demand, state-based.

        The wait before every attempt is computed from the import history, so the initial wait on a
        fresh install is a short jitter, a restart resumes from the persisted schedule instead of
        waiting out a fixed interval again, and a failure streak backs off exponentially.
        """
        while not pop_refresh_stop_event.is_set():
            delay = await asyncio.to_thread(_plan_delay)
            if delay > 0:
                # Interruptible wait so a shutdown during the delay never starts a refresh after.
                with suppress(TimeoutError):
                    await asyncio.wait_for(pop_refresh_stop_event.wait(), timeout=delay)
                continue
            if pop_refresh_stop_event.is_set():
                break

            try:
                _log.info("POP locations refresh is due, starting")
                # Fetching PeeringDB data is blocking external I/O. The refresh service holds a
                # maintenance section only for its local import phase, never during the fetch. Only
                # sources with a real external import run (PeeringDB); the UN/LOCODE placeholder is
                # never scheduled.
                results = await asyncio.to_thread(pop_refresh.refresh_external_sources, _db_conn_factory)
                for r in results:
                    if r.success:
                        _log.info(
                            "POP refresh %s: %d processed, %d inserted, %d updated, %d retired",
                            r.source,
                            r.processed,
                            r.inserted,
                            r.updated,
                            r.retired,
                        )
                    else:
                        _log.warning("POP refresh %s failed: %s", r.source, r.error)

                with _db_connection(_db_conn_factory) as conn:
                    active_count = pop_locations.count_active_pop_locations(conn)
                    _log.debug("POP refresh complete: %d active locations in DB", active_count)

                # A refresh refused by a restore persists no run (it never started one), so the
                # state is unchanged and the plan would stay "due". Wait a short, bounded interval
                # before retrying so an active restore cannot cause a tight re-attempt loop.
                if any(r.error == "maintenance_in_progress" for r in results):
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            pop_refresh_stop_event.wait(), timeout=pop_refresh.POP_MAINTENANCE_RETRY_SECONDS
                        )
            except Exception as e:  # noqa: BLE001 - lifespan boundary: startup/shutdown steps log and continue, never crash the app
                _log.exception("POP refresh task error: %s", e)
                # An error before any run was persisted leaves the plan "due"; pause so it cannot spin.
                with suppress(TimeoutError):
                    await asyncio.wait_for(pop_refresh_stop_event.wait(), timeout=POP_ERROR_RETRY_SECONDS)

            # Loop back: the next delay (freshness window or backoff) is recomputed from the freshly
            # persisted run on the next iteration.

    supervisor.add(ServiceSpec(name="pop_refresh", run=_pop_refresh_loop, request_stop=pop_refresh_stop_event.set))

    # ─── RELEASE CHECK ───────────────────────────────────────
    # Daily check for a newer published GitHub release; reports through the notification inbox.
    from .services.release_check import ReleaseChecker

    release_checker = ReleaseChecker(_db_conn_factory)
    supervisor.add(
        ServiceSpec(name="release_check", run=release_checker.run, request_stop=release_checker.request_stop)
    )

    _log.info("Application started")

    try:
        yield  # App runs here
    finally:
        # ─── SHUTDOWN ────────────────────────────────────────────
        _log.info("Shutting down application...")

        # 0. Signal every background owner to stop before waiting on any of them, so none of
        #    them starts new work while another one is still being awaited.
        app.state.shutting_down = True
        # Close SSE streams first: open streams would otherwise outlive every later shutdown step.
        event_bus.set_current_bus(None)
        await _bounded_stop(live_bus.close(), label="event_bus", timeout=5.0)
        # No service may be restarted from here on, whatever the stop order does to it.
        supervisor.begin_shutdown()
        if pop_refresh_stop_event is not None:
            pop_refresh_stop_event.set()
        if pdf_stop_event is not None:
            pdf_stop_event.set()
        # Release any welcome task waiting out an in-progress restore so it cannot spin.
        targets_api.signal_notification_shutdown()

        # 1. Stop scheduler FIRST (most active DB user); bounded so a stuck check cannot hang shutdown
        if scheduler is not None:
            await _bounded_stop(scheduler.stop(), label="scheduler")

        # 2. Stop background tasks (stop events are already set)
        await _bounded_stop(supervisor.stop(), label="service_supervisor")
        # to_thread jobs cannot be cancelled; shutdown() joins the PDF thread pool, so keep it off the loop
        await _bounded_stop(asyncio.to_thread(pdf_worker.shutdown_pdf_worker), label="pdf_worker_pool")
        # TLS scan pool: cancels queued scans; running ones end by their own connection timeouts
        shutdown_tls_scanner()

        # 3. Cleanup Signal backend (terminate any lingering link processes)
        # Must happen BEFORE closing DB connections (backend may query DB)
        if signal_backend is not None:
            try:
                # Reset singleton (close + clear global state)
                # This prevents subsequent get_signal_backend() from returning closed instance
                reset_signal_backend()
                _log.debug("Signal backend closed and reset")
            except Exception as e:  # noqa: BLE001 - lifespan boundary: startup/shutdown steps log and continue, never crash the app
                _log.debug("Signal backend cleanup (non-critical): %s", e)

        # 4. Close ALL tracked DB connections (graceful shutdown, last step)
        try:
            closed = sqlite_db.close_all_connections()
            _log.debug("Closed %d DB connections", closed)
        except Exception as e:  # noqa: BLE001 - lifespan boundary: startup/shutdown steps log and continue, never crash the app
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
        print(f"\n❌ FATAL: {e}\n", file=sys.stderr)  # noqa: T201 - fatal message after logging.shutdown()
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

    app = FastAPI(title="justUp", version=get_version(), lifespan=_lifespan)
    app.state.cfg = cfg
    app.state.db_path = cfg.db_path
    app.state.tsdb_dir = cfg.tsdb_dir
    app.state.data_dir = cfg.data_dir
    app.state.bootstrap_gate_active = False
    app.state.bootstrap_gate_completed = True
    app.state.bootstrap_gate_release = None

    # Middleware stack (LIFO order: last added = first executed)
    # Refuse new state-changing requests while restore has exclusive file/database access.
    app.add_middleware(MaintenanceWriteMiddleware)
    # CSRF protection (UI only)
    app.add_middleware(CSRFMiddleware)
    # Request ID middleware (outermost to trace ALL requests including CSRF errors)
    app.add_middleware(RequestIDMiddleware)

    # Rate limiting with slowapi
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.exception_handler(sqlite_db.DatabaseQuiescedError)
    async def _database_quiesced(_request, _exc):
        return JSONResponse(
            status_code=503,
            content={"detail": "The system is temporarily unavailable (maintenance in progress)."},
            headers={"Retry-After": "30"},
        )

    @app.middleware("http")
    async def bootstrap_gate(request, call_next):
        """Block everything but sign-in and the password change until the bootstrap password was replaced."""
        if not request.app.state.bootstrap_gate_active:
            return await call_next(request)
        path = request.url.path
        if path in _BOOTSTRAP_GATE_ALLOWED_EXACT_PATHS or path.startswith(_BOOTSTRAP_GATE_ALLOWED_PREFIXES):
            return await call_next(request)
        if path.startswith("/ui/"):
            return RedirectResponse(url="/ui/login", status_code=302)
        return JSONResponse(
            status_code=423,
            content={"detail": "Bootstrap is waiting for the admin password change.", "bootstrap_gate": True},
            headers={"Retry-After": "30"},
        )

    # Static files (CSS, JS, fonts)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots_txt():
        return "User-agent: *\nDisallow: /"

    app.include_router(users_api.router)
    app.include_router(dashboard_layout_api.router)
    app.include_router(health_api.router)
    app.include_router(events_api.router)
    app.include_router(groups_api.router)
    app.include_router(targets_api.router)
    app.include_router(alerts_api.router)
    app.include_router(notifications_api.router)
    app.include_router(metrics_api.router)
    app.include_router(settings_api.router)
    app.include_router(signal_api.router)
    app.include_router(recipients_api.router)
    app.include_router(reports_api.router)
    app.include_router(reports_api.ui_router)
    app.include_router(quality_api.router)
    app.include_router(pop_api.router)
    app.include_router(frontend_ui.router)

    return app


__all__ = ["create_app"]
