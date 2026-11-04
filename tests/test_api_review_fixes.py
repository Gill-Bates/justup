#!/usr/bin/env python3
#
# tests/test_api_review_fixes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regression tests for API review fixes: contracts of individual routers on a minimal app."""

# ruff: noqa: ARG005, SLF001
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import frontend, metrics, recipients, settings, users
from app.api.auth import get_current_user
from app.db import pop_locations
from app.db import sqlite as sqlite_db
from app.utils import crypto, report_metrics
from app.utils.deps import get_conn
from app.utils.rate_limit import RateLimitExceededError, rate_limit_exceeded_handler, reset_all
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture
def conn(tmp_path: Path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


def _client(conn, *routers, user: dict) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
    app.state.tsdb_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _target(conn, name: str) -> int:
    return sqlite_db.create_target(
        conn, {"name": name, "host": f"{name}.example.com", "interval_seconds": 60, "retention_days": 30}
    )


def _mk_user(conn, name: str, *, admin: bool) -> dict:
    uid = sqlite_db.create_user(conn, username=name, password_hash="stub", is_admin=admin)  # noqa: S106
    return dict(sqlite_db.get_user(conn, uid))


# --- signal QR fragment: backend returns (png, session_id) -----------------------------------


@pytest.mark.parametrize(
    ("backend_result", "expected"),
    [((b"\x89PNG", "sid"), "data:image/png;base64,"), ((None, None), "QR linking not available")],
)
def test_signal_qr_fragment_unpacks_backend_tuple(monkeypatch, backend_result, expected):
    backend = SimpleNamespace(get_link_qr=lambda device_name: backend_result)
    monkeypatch.setattr("app.services.notifications.signal_backend.get_signal_backend", lambda: backend)
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda request, conn: {"is_admin": 1})
    resp = frontend.fragment_signal_qr(SimpleNamespace(), None)
    assert expected in resp.body.decode()


# --- metrics: inverted range ------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard/uptime", "/dashboard/downtime"])
def test_dashboard_routes_reject_inverted_range(conn, path):
    client = _client(conn, metrics.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(path, params={"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"})
    assert resp.status_code == 400


# --- users: strict is_active, password CAS ----------------------------------------------------


def test_set_user_active_rejects_string_and_missing_value(conn):
    admin = _mk_user(conn, "root", admin=True)
    target = _mk_user(conn, "bob", admin=False)
    client = _client(conn, users.router, user=admin)
    url = f"/users/{target['id']}/active"
    assert client.post(url, json={"is_active": "false"}).status_code == 422
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"is_active": False}).status_code == 200
    assert sqlite_db.get_user(conn, target["id"])["is_active"] == 0


def test_change_password_conflicts_when_hash_changed_concurrently(conn, monkeypatch):
    user = _mk_user(conn, "alice", admin=False)
    monkeypatch.setattr(users, "verify_password", lambda plain, hashed: True)
    monkeypatch.setattr(sqlite_db, "update_user_password", lambda *a, **k: False)
    client = _client(conn, users.router, user=user)
    resp = client.post(
        "/me/password", json={"current_password": "old-password-123", "new_password": "N3w-password-456!"}
    )
    assert resp.status_code == 409


# --- recipients -------------------------------------------------------------------------------


def test_verify_signal_requires_admin_and_deduplicates(conn, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(recipients, "_is_phone_probably_registered", lambda p: calls.append(p) or True)
    nonadmin = _client(conn, recipients.router, user=_mk_user(conn, "v", admin=False))
    assert nonadmin.post("/recipients/verify-signal", json=["+4915112345678"]).status_code == 403
    admin = _client(conn, recipients.router, user=_mk_user(conn, "a", admin=True))
    resp = admin.post("/recipients/verify-signal", json=["+4915112345678", "+49 151 12345678"])
    assert resp.status_code == 200
    assert calls == ["+4915112345678"]


def test_targets_by_recipient_groups_in_one_query(conn):
    rid = sqlite_db.create_recipient(conn, name="R", email="r@example.com")
    other = sqlite_db.create_recipient(conn, name="O", email="o@example.com")
    t_b = _target(conn, "B")
    t_a = _target(conn, "A")
    sqlite_db.set_target_recipients(conn, t_b, [rid])
    sqlite_db.set_target_recipients(conn, t_a, [rid])
    grouped = sqlite_db.get_targets_by_recipient(conn)
    assert [t["name"] for t in grouped[rid]] == ["A", "B"]
    assert other not in grouped
    assert [t["id"] for t in sqlite_db.get_recipient_targets(conn, rid)] == [t["id"] for t in grouped[rid]]


# --- pop import runs offset -------------------------------------------------------------------


def test_import_run_stats_offset_in_sql(conn):
    for i in range(5):
        conn.execute(
            "INSERT INTO pop_import_runs (run_id, source, started_at, status) VALUES (?, 's', ?, 'success')",
            (f"r{i}", f"2026-01-0{i + 1}T00:00:00+00:00"),
        )
    conn.commit()
    page = pop_locations.get_import_run_stats(conn, limit=2, offset=2)
    assert [r["started_at"][:10] for r in page] == ["2026-01-03", "2026-01-02"]


# --- reports: failed queueing must not leave a pending job ------------------------------------


def test_pdf_request_marks_job_failed_when_queue_rejects(conn, monkeypatch, tmp_path):
    reports = importlib.import_module("app.api.reports")
    tid = _target(conn, "T")

    async def _boom(_req):
        raise RuntimeError("PDF generation queue is full")

    monkeypatch.setattr(reports, "submit_pdf_job", _boom)
    client = _client(conn, reports.router, user=_mk_user(conn, "u", admin=False))
    client.app.state.db_path = tmp_path / "x.db"
    client.app.state.tsdb_dir = tmp_path
    with pytest.raises(RuntimeError):
        client.post(f"/reports/targets/{tid}/pdf/request")
    assert conn.execute("SELECT status FROM report_jobs").fetchone()["status"] == "failed"


def test_pdf_job_keeps_selected_custom_range(conn, monkeypatch, tmp_path):
    reports = importlib.import_module("app.api.reports")
    tid = _target(conn, "range")
    queued = []

    async def capture(request):
        queued.append(request)

    monkeypatch.setattr(reports, "submit_pdf_job", capture)
    client = _client(conn, reports.router, user=_mk_user(conn, "viewer", admin=False))
    client.app.state.db_path = tmp_path / "db" / "justup.db"
    client.app.state.tsdb_dir = tmp_path
    since = "2025-01-15T09:30:00+02:00"
    until = "2026-01-15T09:45:00+02:00"
    response = client.post(f"/reports/targets/{tid}/pdf/request", params={"since": since, "until": until})
    assert response.status_code == 200, response.text
    assert len(queued) == 1
    assert queued[0].from_date == datetime(2025, 1, 15, 7, 30, tzinfo=UTC)
    assert queued[0].to_date == datetime(2026, 1, 15, 7, 45, tzinfo=UTC)


# --- thread offloading, limits and locks (C01, C02, C06-C08, C10, C16) ------------------------


def _assert_off_event_loop() -> None:
    """Blocking code must run in a worker thread, i.e. without a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError("blocking call executed on the event loop thread")


def _load_reports(_monkeypatch):
    return importlib.import_module("app.api.reports")


def test_report_pdf_tsdb_access_runs_off_event_loop(conn, monkeypatch):
    reports = _load_reports(monkeypatch)
    tid = _target(conn, "T")
    conn.execute("UPDATE targets SET enable_http_check = 1 WHERE id = ?", (tid,))
    conn.commit()
    calls: list[str] = []

    def fake_query(*_a, **_k):
        # The route swallows fetch errors, so record the thread instead of asserting inside.
        try:
            asyncio.get_running_loop()
            calls.append("on-loop")
        except RuntimeError:
            calls.append("worker")
        time.sleep(0.01)
        return []

    monkeypatch.setattr(reports.tsdb_db, "query_auto_resolution", fake_query)
    monkeypatch.setattr(reports, "generate_pdf_report", lambda *_a, **_k: b"%PDF-1.4")
    client = _client(conn, reports.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(
        f"/reports/targets/{tid}/pdf", params={"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}
    )
    assert resp.status_code == 200, resp.text
    assert calls
    assert set(calls) == {"worker"}


def test_availability_series_tsdb_access_runs_off_event_loop(conn, monkeypatch):
    tid = _target(conn, "T")
    seen: list[int] = []

    def fake_series(*_a, **kwargs):
        _assert_off_event_loop()
        seen.append(kwargs["target_id"])
        return {}

    monkeypatch.setattr(frontend.tsdb_db, "query_availability_series", fake_series)
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(f"/ui/fragments/availability-series/{tid}")
    assert resp.status_code == 200, resp.text
    assert seen == [tid]


def test_availability_series_uses_exact_selected_range(conn, monkeypatch):
    tid = _target(conn, "range")
    captured = []

    def fake_series(*_a, **kwargs):
        captured.append(kwargs)
        return {}

    monkeypatch.setattr(frontend.tsdb_db, "query_availability_series", fake_series)
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "viewer", admin=False))
    response = client.get(
        f"/ui/fragments/availability-series/{tid}",
        params={"since": "2026-01-02T03:04:00+02:00", "until": "2026-01-09T10:11:00+02:00"},
    )
    assert response.status_code == 200, response.text
    assert captured[0]["since"] == datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    assert captured[0]["until"] == datetime(2026, 1, 9, 8, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("since", "until"),
    [
        pytest.param("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z", id="aware"),
        pytest.param("2026-02-01T00:00:00", "2026-01-01T00:00:00+00:00", id="naive-and-aware"),
    ],
)
def test_downtime_history_fragment_rejects_inverted_range(conn, monkeypatch, since, until):
    tid = _target(conn, "T")
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(
        f"/ui/fragments/downtime-history/{tid}",
        params={"since": since, "until": until},
    )
    assert resp.status_code == 400


def test_notification_queue_endpoints_call_spooler_off_event_loop(conn, monkeypatch):
    from app.api import settings
    from app.services.notifications import spooler as spooler_module

    class FakeSpooler:
        def get_queue_stats_detailed(self):
            _assert_off_event_loop()
            return {
                "by_status": {},
                "channels": {},
                "cooldowns": [],
                "oldest_pending": None,
                "max_retry_count": 0,
                "next_retry_at": None,
                "stuck_processing": 0,
                "failed_recent": 0,
                "processing_timeout_seconds": 0,
            }

        def get_log(self, **_k):
            _assert_off_event_loop()
            return {"items": [], "total": 0}

        def flush_queue(self):
            _assert_off_event_loop()
            return 0

        def clear_log(self):
            _assert_off_event_loop()
            return 0

    monkeypatch.setattr(spooler_module, "get_spooler", lambda _factory: FakeSpooler())
    client = _client(conn, settings.router, user=_mk_user(conn, "root", admin=True))
    client.app.state.db_path = Path("unused.db")
    assert client.get("/settings/notification-queue/stats").status_code == 200
    assert client.get("/settings/notification-queue/log").status_code == 200
    assert client.delete("/settings/notification-queue/flush").status_code == 200
    assert client.delete("/settings/notification-queue/clear-log").status_code == 200


def _two_recipients(conn, tid: int) -> tuple[int, int]:
    first = sqlite_db.create_recipient(conn, name="A", email="a@example.com")
    second = sqlite_db.create_recipient(conn, name="B", email="b@example.com")
    sqlite_db.set_target_recipients(conn, tid, [first])
    return first, second


def test_alert_users_rejects_with_429_when_task_limit_reached_and_keeps_assignment(conn, monkeypatch):
    from app.api import targets

    tid = _target(conn, "T")
    first, second = _two_recipients(conn, tid)
    monkeypatch.setattr(targets, "_background_tasks", {object() for _ in range(targets.MAX_ACTIVE_NOTIFICATION_TASKS)})
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    resp = client.put(f"/targets/{tid}/alert-users", json=[first, second])
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert sqlite_db.get_target_recipient_ids(conn, tid) == [first]


def test_alert_users_below_task_limit_starts_task(conn, monkeypatch):
    from app.api import targets

    tid = _target(conn, "T")
    first, second = _two_recipients(conn, tid)
    monkeypatch.setattr(
        targets, "_background_tasks", {object() for _ in range(targets.MAX_ACTIVE_NOTIFICATION_TASKS - 1)}
    )

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(targets, "_safe_notify_recipients_background", _noop)
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    client.app.state.db_path = Path("unused.db")
    resp = client.put(f"/targets/{tid}/alert-users", json=[first, second])
    assert resp.status_code == 200
    assert resp.json()["notification_task_id"]
    assert set(sqlite_db.get_target_recipient_ids(conn, tid)) == {first, second}


def test_alert_users_without_new_recipients_is_not_limited(conn, monkeypatch):
    from app.api import targets

    tid = _target(conn, "T")
    first, _ = _two_recipients(conn, tid)
    monkeypatch.setattr(targets, "_background_tasks", {object() for _ in range(targets.MAX_ACTIVE_NOTIFICATION_TASKS)})
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    assert client.put(f"/targets/{tid}/alert-users", json=[first]).status_code == 200


def _run_notify_sync_bounded(targets, task_id: str, *, timeout: float = 5.0) -> dict:
    """Run the welcome-task wait loop in a thread and fail if it does not terminate (no hang)."""
    target_id = 1
    status = {"status": "running", "errors": [], "updated_at": 0.0, "completed_at": None}
    targets._notification_tasks[target_id] = {task_id: status}

    def _run() -> None:
        targets._notify_recipients_background_sync(Path("unused.db"), target_id, task_id, "T", {1})

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout)
    try:
        # A missing terminal state means the bounded/cancellable exit did not fire: fail with evidence.
        assert not worker.is_alive(), f"welcome task did not terminate within {timeout}s (status={status})"
    finally:
        targets.signal_notification_shutdown()  # release the thread if it is still waiting
        worker.join(timeout)
        targets._notification_shutdown.clear()
        targets._notification_tasks.pop(target_id, None)
    return status


def test_welcome_task_wait_is_bounded_when_maintenance_stays_active(monkeypatch):
    from app.api import targets
    from app.utils import maintenance

    monkeypatch.setattr(targets, "_NOTIFICATION_MAINTENANCE_WAIT_SECONDS", 0.2)
    targets._notification_shutdown.clear()
    assert maintenance.begin_maintenance(timeout=1)
    try:
        status = _run_notify_sync_bounded(targets, "bounded")
    finally:
        maintenance.end_maintenance()
    # Loop exited on its own timeout instead of spinning forever.
    assert status["status"] == "deferred"
    assert any("timeout" in e for e in status["errors"])


def test_welcome_task_wait_is_cancelled_on_shutdown(monkeypatch):
    from app.api import targets
    from app.utils import maintenance

    # Long bound: only a shutdown signal can terminate the wait in time.
    monkeypatch.setattr(targets, "_NOTIFICATION_MAINTENANCE_WAIT_SECONDS", 3600.0)
    targets._notification_shutdown.clear()
    assert maintenance.begin_maintenance(timeout=1)
    try:
        target_id, task_id = 1, "shutdown"
        status = {"status": "running", "errors": [], "updated_at": 0.0, "completed_at": None}
        targets._notification_tasks[target_id] = {task_id: status}
        worker = threading.Thread(
            target=lambda: targets._notify_recipients_background_sync(Path("unused.db"), target_id, task_id, "T", {1})
        )
        worker.start()
        time.sleep(0.2)  # let it enter the wait
        targets.signal_notification_shutdown()
        worker.join(5)
        assert not worker.is_alive(), f"welcome task did not stop on shutdown (status={status})"
        assert status["status"] == "deferred"
        assert any("shutdown" in e for e in status["errors"])
    finally:
        targets._notification_shutdown.clear()
        targets._notification_tasks.pop(1, None)
        maintenance.end_maintenance()


def test_welcome_task_deferred_when_data_generation_changed(monkeypatch):
    from app.api import targets
    from app.utils import maintenance

    targets._notification_shutdown.clear()
    # Capture a generation, then advance it so the work section sees a stale snapshot.
    start_gen = maintenance.current_generation()
    monkeypatch.setattr(maintenance, "current_generation", lambda: start_gen)
    monkeypatch.setattr(maintenance, "_GENERATION", start_gen + 1, raising=False)

    status = _run_notify_sync_bounded(targets, "stale")

    assert status["status"] == "deferred"
    assert any("generation" in e for e in status["errors"])


def test_test_alert_runs_in_worker_with_own_connection_and_is_rate_limited(conn, monkeypatch):
    from app.api import targets
    from app.services.notifications import dispatcher as dispatcher_module

    tid = _target(conn, "T")
    _first, _ = _two_recipients(conn, tid)
    seen: list[object] = []

    class FakeDispatcher:
        def __init__(self, conn, db_conn_factory=None):  # noqa: ARG002
            seen.append(conn)

        def dispatch_alert(self, _alert):
            _assert_off_event_loop()
            return SimpleNamespace(channels_attempted=1, channels_succeeded=1, send_results=[], users_notified=1)

    monkeypatch.setattr(dispatcher_module, "NotificationDispatcher", FakeDispatcher)
    reset_all()
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    client.app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    statuses = [client.post(f"/targets/{tid}/test-alert").status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    assert len(seen) == 5
    assert all(c is not conn for c in seen)
    reset_all()


def test_pop_refresh_entry_points_share_one_lock(conn, monkeypatch):
    from app.api import pop
    from app.services import pop_refresh

    entered, release = threading.Event(), threading.Event()

    def blocking_refresh(_factory):
        entered.set()
        release.wait(5)
        return pop_refresh.PopRefreshResult(run_id="r", source=pop_refresh.SOURCE_PEERINGDB)

    monkeypatch.setattr(pop_refresh, "_do_peeringdb_refresh", blocking_refresh)
    client = _client(conn, pop.router, user=_mk_user(conn, "root", admin=True))
    client.app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    reset_all()

    # The startup/scheduler path (run_full_refresh) holds the lock; the API must be turned away.
    holder = threading.Thread(target=pop_refresh.refresh_from_peeringdb, args=(lambda: None,))
    holder.start()
    try:
        assert entered.wait(5)
        resp = client.post("/api/admin/pop/refresh", params={"source": "peeringdb"})
        assert resp.status_code == 409
        assert resp.json()["results"][0]["error"] == "already_running"
        monkeypatch.setattr(
            pop_refresh,
            "refresh_from_unlocode",
            lambda _f: pop_refresh.PopRefreshResult(run_id="", source="unlocode", error="already_running"),
        )
        second = pop_refresh.run_full_refresh(lambda: None)
        assert second[0].error == "already_running"
    finally:
        release.set()
        holder.join(5)
    reset_all()


# --- session lifetime (cookie vs. server-side expiry) -----------------------------------------


@pytest.mark.parametrize("days", [1, 3, 7])
def test_login_cookie_max_age_is_full_configured_session_lifetime(conn, days):
    from app.models.users import hash_password

    sqlite_db.create_user(conn, username="alice", password_hash=hash_password("Correct-horse-1"), is_admin=False)
    sqlite_db.set_setting(conn, "session_max_days", str(days))
    reset_all()
    client = _client(conn, users.router, user={})
    resp = client.post("/login", json={"username": "alice", "password": "Correct-horse-1"})
    assert resp.status_code == 200, resp.text
    assert f"Max-Age={days * 86400}" in resp.headers["set-cookie"]


def test_cookie_alone_does_not_grant_access_after_server_side_expiry(conn):
    from app.models.users import hash_token

    uid = sqlite_db.create_user(conn, username="bob", password_hash="stub", is_admin=False)  # noqa: S106
    now = datetime.now(UTC)
    app = FastAPI()
    app.dependency_overrides[get_conn] = lambda: conn

    @app.get("/ui/probe")
    def _probe(user=Depends(get_current_user)):
        return {"id": user["id"]}

    client = TestClient(app)
    client.cookies.set("token", "tok")
    sqlite_db.issue_token(
        conn,
        user_id=uid,
        token_hash=hash_token("tok"),
        expires_at=now + timedelta(hours=1),
        max_expires_at=now + timedelta(days=3),
    )
    assert client.get("/ui/probe").status_code == 200
    # Idle expiry passed on the server while the browser still holds the cookie.
    conn.execute("UPDATE auth_tokens SET expires_at = ?", (now - timedelta(minutes=1),))
    conn.commit()
    assert client.get("/ui/probe").status_code == 401
    # Absolute lifetime exhausted even though the idle window would still be open.
    conn.execute(
        "UPDATE auth_tokens SET expires_at = ?, max_expires_at = ?",
        (now + timedelta(hours=1), now - timedelta(minutes=1)),
    )
    conn.commit()
    assert client.get("/ui/probe").status_code == 401


# --- reports: attachment download reachable by cookie navigation ------------------------------
def test_pdf_download_navigation_is_cookie_authenticated_attachment(conn, monkeypatch, tmp_path):
    from app.models.users import hash_token

    reports = importlib.import_module("app.api.reports")
    uid = sqlite_db.create_user(conn, username="dl", password_hash="stub", is_admin=False)  # noqa: S106
    tid = _target(conn, "dl")
    sqlite_db.create_report_job(conn, "job1", tid, user_id=uid)
    sqlite_db.update_report_job(conn, "job1", status="completed", filename="Report 1.pdf", completed=True)
    pdf = tmp_path / "job1.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(reports, "get_pdf_path", lambda _job_id: pdf)
    now = datetime.now(UTC)
    sqlite_db.issue_token(
        conn,
        user_id=uid,
        token_hash=hash_token("tok"),
        expires_at=now + timedelta(hours=1),
        max_expires_at=now + timedelta(days=1),
    )
    app = FastAPI()
    app.include_router(reports.ui_router)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    url = "/ui/reports/jobs/job1/download"
    assert client.get(url).status_code == 401  # no session: no file
    client.cookies.set("token", "tok")
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    assert resp.headers["content-type"] == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="Report_1.pdf"' in disposition and "filename*=UTF-8''Report_1.pdf" in disposition
    assert "inline" not in disposition
    assert resp.headers["cache-control"] == "private, no-store"
    assert client.head(url).status_code == 200


# --- storage statistics (shared by the settings API and the frontend) -------------------------


def test_storage_stats_counts_bytes_and_points(tmp_path):
    from app.utils.storage_stats import compute_storage_stats

    db_file = tmp_path / "justup.db"
    db_file.write_bytes(b"x" * 10)
    tsdb = tmp_path / "tsdb"
    (tsdb / "1").mkdir(parents=True)
    (tsdb / "1" / "a.jsonl").write_text('{"v":1}\n{"v":2}\n')
    (tsdb / "b.jsonl").write_text('{"v":3}\n')
    (tsdb / "ignored.txt").write_text("nope\n")
    stats = compute_storage_stats(db_file, tsdb)
    assert stats == {
        "sql_size_bytes": 10,
        "tsdb_size_bytes": (tsdb / "1" / "a.jsonl").stat().st_size + (tsdb / "b.jsonl").stat().st_size,
        "total_points": 3,
        "tsdb_saved_bytes": 0,
    }
    assert compute_storage_stats(tmp_path / "missing.db", tmp_path / "missing") == {
        "sql_size_bytes": 0,
        "tsdb_size_bytes": 0,
        "total_points": 0,
        "tsdb_saved_bytes": 0,
    }


# --- DRY pins: tcp port parsing, template preview, naive=UTC, signal pre-checks ---------------


def test_tcp_port_parsers_keep_strict_write_and_tolerant_read_policies():
    from fastapi import HTTPException

    from app.api import targets

    assert targets._validate_tcp_ports("443, 80 8080,80") == "80,443,8080"
    assert targets._validate_tcp_ports(None) == targets.DEFAULT_TCP_PORTS
    assert targets._validate_tcp_ports("  ") == targets.DEFAULT_TCP_PORTS
    for bad in ("abc", "0", "65536", "80,x", "80abc", "80.5", "\u00b2"):
        with pytest.raises(HTTPException) as exc:
            targets._validate_tcp_ports(bad)
        assert exc.value.status_code == 422

    assert report_metrics.parse_tcp_ports("443,abc,0,65536, 80,,443") == [443, 80, 443]
    assert report_metrics.parse_tcp_ports("") == []


def test_smtp_template_preview_roundtrip_replaces_logo_placeholder(conn):
    from app.services.notifications.email_template import LOGO_URL_PREVIEW

    client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))
    resp = client.post(
        "/settings/smtp/template",
        json={"subject": f"S {LOGO_URL_PREVIEW}", "html": f"<img src='{LOGO_URL_PREVIEW}'>"},
    )
    assert resp.status_code == 200
    assert sqlite_db.get_setting(conn, "smtp_mail_template_subject", None) == "S {{logo_url}}"
    assert sqlite_db.get_setting(conn, "smtp_mail_template_html", None) == "<img src='{{logo_url}}'>"
    body = client.get("/settings/smtp/template").json()
    assert body["subject"] == f"S {LOGO_URL_PREVIEW}"
    assert body["html"] == f"<img src='{LOGO_URL_PREVIEW}'>"
    assert "{{logo_url}}" not in body["html"]


def test_smtp_template_preview_renders_sample_without_placeholders(conn):
    from app.services.notifications.email_template import LOGO_URL_PREVIEW

    client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))

    down = client.post("/settings/smtp/template/preview", json={"variant": "down"})
    assert down.status_code == 200
    data = down.json()
    assert "{{" not in data["html"] and "{{" not in data["subject"]
    assert "Service DOWN" in data["html"]
    # Logo is shown via the loadable preview URL, never the raw cid: reference.
    assert LOGO_URL_PREVIEW in data["html"]
    assert "cid:" not in data["html"]

    resolved = client.post("/settings/smtp/template/preview", json={"variant": "resolved"})
    assert resolved.status_code == 200
    assert "Service RESOLVED" in resolved.json()["html"]


def test_smtp_template_preview_uses_unsaved_editor_html(conn):
    client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))
    resp = client.post(
        "/settings/smtp/template/preview",
        json={"subject": "X", "html": "<p>custom {{target.name}}</p>", "variant": "down"},
    )
    assert resp.status_code == 200
    html = resp.json()["html"]
    assert "custom Test Target" in html
    assert "{{" not in html


def test_smtp_template_reset_returns_the_new_default(conn):
    from app.services.notifications.email_template import DEFAULT_HTML_TEMPLATE

    client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))
    # A custom template is stored first, then reset clears it back to the default.
    sqlite_db.set_setting(conn, "smtp_mail_template_html", "<p>mine</p>")
    resp = client.post("/settings/smtp/template/reset")
    assert resp.status_code == 200
    assert resp.json()["html"] == DEFAULT_HTML_TEMPLATE
    assert sqlite_db.get_setting(conn, "smtp_mail_template_html", None) is None


def test_test_mail_enqueues_a_non_empty_text_body(conn, monkeypatch):
    from app.services.notifications import spooler as spooler_module
    from app.utils.constants import SettingKeys

    for key, value in (
        (SettingKeys.SMTP_ENABLED, True),
        (SettingKeys.SMTP_HOST, "smtp.example.com"),
        (SettingKeys.SMTP_FROM, "alerts@example.com"),
    ):
        sqlite_db.set_setting(conn, key, value)

    queued: list[tuple[tuple, dict]] = []

    class FakeSpooler:
        def enqueue(self, *args, **kwargs):
            queued.append((args, kwargs))
            return 1

    monkeypatch.setattr(spooler_module, "get_spooler", lambda _factory: FakeSpooler())
    client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))
    client.app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    resp = client.post("/settings/smtp/test-mail", json={"recipient": "you@example.com"})
    assert resp.status_code == 200, resp.text

    payload = queued[0][1]["payload"]
    assert payload["text_body"].strip(), "test mail must carry a non-empty plain-text part"
    assert "Service DOWN" in payload["text_body"]
    # Realistic sample data, not the old hard-coded 2026-01-29 outage.
    assert "2026-01-29" not in payload["html_body"]
    assert payload["html_body"].count("{{") == 0


def test_smtp_fragment_shows_preview_url_instead_of_placeholder(conn, monkeypatch):
    from app.services.notifications.email_template import LOGO_URL_PREVIEW

    sqlite_db.set_setting(conn, "smtp_mail_template_html", "<img src='{{logo_url}}'>")
    sqlite_db.set_setting(conn, "smtp_mail_template_subject", "Hi {{logo_url}}")
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda request, conn: {"id": 1, "is_admin": 1})
    captured: dict = {}
    monkeypatch.setattr(
        frontend.templates,
        "TemplateResponse",
        lambda request, name, ctx: captured.update(ctx) or "ok",
    )
    assert frontend.fragment_settings_smtp(SimpleNamespace(), conn) == "ok"
    assert captured["template"] == {"subject": f"Hi {LOGO_URL_PREVIEW}", "html": f"<img src='{LOGO_URL_PREVIEW}'>"}


@pytest.mark.parametrize("path", ["/dashboard/uptime", "/dashboard/downtime"])
def test_dashboard_routes_treat_naive_datetimes_as_utc(conn, path):
    client = _client(conn, metrics.router, user=_mk_user(conn, "u", admin=False))
    inverted = client.get(path, params={"since": "2026-02-01T00:00:00", "until": "2026-01-01T00:00:00Z"})
    assert inverted.status_code == 400
    mixed_ok = client.get(path, params={"since": "2026-01-01T00:00:00", "until": "2026-02-01T00:00:00+00:00"})
    assert mixed_ok.status_code == 200, mixed_ok.text


@pytest.fixture
def signal_env(conn, monkeypatch):
    from app.utils.constants import SettingKeys

    reset_all()
    state = SimpleNamespace(accounts=["+4915112345678"], calls=[], error=None)

    class FakeBackend:
        def list_accounts(self):
            if state.error:
                raise state.error
            return state.accounts

        def unregister(self, sender_number):
            state.calls.append(("unregister", sender_number))

        def unlink_device(self, sender_number, delete_cache):  # noqa: ARG002
            state.calls.append(("unlink", sender_number))

    monkeypatch.setattr("app.services.notifications.signal_backend.get_signal_backend", FakeBackend)
    monkeypatch.setattr(settings, "verify_password", lambda plain, hashed: plain == "pw")
    sqlite_db.set_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, "+4915112345678")
    state.client = _client(conn, settings.router, user=_mk_user(conn, "adm", admin=True))
    state.key = SettingKeys.SIGNAL_API_SENDER_NUMBER
    return state


def _post(env, path, body):
    reset_all()
    return env.client.post(path, json=body)


@pytest.mark.parametrize(
    ("action", "path"),
    [("unregister", "/settings/signal/unregister"), ("unlink", "/settings/signal/unlink")],
)
def test_signal_removal_prechecks_are_shared_by_unlink_and_unregister(conn, signal_env, action, path):
    from app.services.notifications.signal_errors import SignalUnavailable

    env = signal_env
    assert _post(env, path, {"password": "wrong"}).status_code == 401
    assert env.calls == []

    env.accounts = ["+49999"]
    assert _post(env, path, {"password": "pw"}).status_code == 409
    assert env.calls == []
    assert sqlite_db.get_setting(conn, env.key) == "+4915112345678"

    env.error = SignalUnavailable("down")
    assert _post(env, path, {"password": "pw"}).status_code == 503
    env.error = None

    env.accounts = []
    resp = _post(env, path, {"password": "pw"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert env.calls == []
    assert sqlite_db.get_setting(conn, env.key) is None

    sqlite_db.set_setting(conn, env.key, "+4915112345678")
    env.accounts = ["+4915112345678"]
    resp = _post(env, path, {"password": "pw"})
    assert resp.status_code == 200
    assert env.calls == [(action, "+4915112345678")]
    assert sqlite_db.get_setting(conn, env.key) is None


# --- metrics: downtime detection over long windows ---------------------------------------------
def _seed_http_up(tsdb_dir: Path, target_id: int, values: list[int], start: datetime) -> None:
    from app.db import tsdb

    for i, value in enumerate(values):
        tsdb.append_point(
            tsdb_dir,
            target_id=target_id,
            metric="http_up",
            value=value,
            retention_days=365,
            at=start + timedelta(seconds=i),
        )


def _downtime(conn, tid, values, start, *, enabled=True):
    conn.execute("UPDATE targets SET enable_http_check = 1, is_enabled = ? WHERE id = ?", (int(enabled), tid))
    conn.commit()
    _seed_http_up(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, tid, values, start)
    client = _client(conn, metrics.router, user=_mk_user(conn, "u", admin=False))
    since = (start - timedelta(minutes=1)).isoformat()
    resp = client.get("/dashboard/downtime", params={"since": since})
    assert resp.status_code == 200
    return {t["target_id"]: t for t in resp.json()["targets"]}


@pytest.mark.parametrize("extra_targets", [0, 100])
def test_downtime_finds_outage_older_than_the_newest_points(conn, extra_targets):
    tid = _target(conn, "flaky")
    for i in range(extra_targets):
        _target(conn, f"filler{i}")
    start = datetime.now(UTC) - timedelta(hours=2)
    found = _downtime(conn, tid, [0] * 120 + [1] * 2100, start)
    summary = found[tid]["services"]["http_up"]
    assert summary["downtime_seconds"] == 120
    assert summary["incidents"] == 1
    assert summary["ongoing"] is False


def test_downtime_ignores_incidents_below_threshold(conn):
    tid = _target(conn, "blip")
    start = datetime.now(UTC) - timedelta(hours=2)
    assert tid not in _downtime(conn, tid, [0] * 10 + [1] * 600, start)


def test_downtime_skips_disabled_targets(conn):
    tid = _target(conn, "off")
    start = datetime.now(UTC) - timedelta(minutes=10)
    assert tid not in _downtime(conn, tid, [1] * 5 + [0] * 400, start, enabled=False)


def test_downtime_of_stale_series_stops_at_last_point(conn):
    tid = _target(conn, "stalled")
    start = datetime.now(UTC) - timedelta(hours=3)
    summary = _downtime(conn, tid, [1] * 10 + [0] * 300, start)[tid]["services"]["http_up"]
    assert summary["ongoing"] is False
    assert summary["downtime_seconds"] == 299


def test_downtime_of_fresh_series_is_ongoing(conn):
    tid = _target(conn, "down")
    start = datetime.now(UTC) - timedelta(seconds=400)
    summary = _downtime(conn, tid, [1] * 10 + [0] * 390, start)[tid]["services"]["http_up"]
    assert summary["ongoing"] is True
    assert summary["downtime_seconds"] >= 390


# --- CR-02: per-hop geolocation must not block the event loop ------------------------------------
def test_traceroute_hop_geolocation_runs_off_event_loop(monkeypatch):
    from app.services import mtr_run, route_interpolator, traceroute_geo

    hop = {"hop": 1, "ip": "203.0.113.7", "hostname": "h.example", "status": "ok", "latency_ms": {"avg": 1.0}}
    threads: list[int] = []

    def fake_geo(ip, _hostname=None):
        if ip == "203.0.113.7":
            threads.append(threading.get_ident())
            raise RuntimeError("stop")

    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda host: "198.51.100.1")
    monkeypatch.setattr(mtr_run, "run_mtr", lambda *a, **k: [hop])
    monkeypatch.setattr(
        mtr_run, "analyze_route", lambda hops, ip: {"last_responding_hop": 0, "destination_reached": True}
    )
    monkeypatch.setattr(route_interpolator, "interpolate_route", lambda hops, **k: hops)
    monkeypatch.setattr(route_interpolator, "collapse_interpolated_hops", lambda hops, *a, **k: hops)
    monkeypatch.setattr(route_interpolator, "ensure_destination_hop", lambda hops, **k: hops)
    monkeypatch.setattr(traceroute_geo, "geolocate_with_hostname_hint", fake_geo)
    monkeypatch.setattr(traceroute_geo, "mtr_hops_to_geo_points", lambda *a, **k: [])
    loop_thread = threading.get_ident()
    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(frontend._run_traceroute_fragment(SimpleNamespace(), 1, "h.example", "ns"))
    assert threads
    assert threads[0] != loop_thread


# --- CR-04: uptime fragment reads the sqlite3.Row column ----------------------------------------
def test_uptime_fragment_shows_percentage_from_cached_status(conn, monkeypatch):
    tid = _target(conn, "up")
    conn.execute(
        "INSERT INTO target_status(target_id, overall_status, updated_at, uptime_percent) VALUES (?, 'up', ?, 99.5)",
        (tid, datetime.now(UTC)),
    )
    conn.commit()
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda request, conn: {"id": 1})
    html = frontend.fragment_uptime(tid, SimpleNamespace(), conn).body.decode()
    assert "99" in html
    assert "—" not in html


# --- CR-06: alert filter target_id validation matches the API ------------------------------------
@pytest.mark.parametrize("raw", ["0", "-1"])
def test_ui_alert_filter_rejects_non_positive_target_id(conn, raw):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        frontend._alerts_panel_context(conn, target_id=raw, status=None, from_date=None, to_date=None, page=1)
    assert (exc.value.status_code, exc.value.detail) == (400, "target_id must be >= 1")


def test_ui_alert_filter_rejects_text_and_accepts_empty_and_positive(conn):
    from fastapi import HTTPException

    tid = _target(conn, "a")
    with pytest.raises(HTTPException) as exc:
        frontend._alerts_panel_context(conn, target_id="abc", status=None, from_date=None, to_date=None, page=1)
    assert exc.value.detail == "Invalid target_id"
    for ok in ("", None, str(tid)):
        frontend._alerts_panel_context(conn, target_id=ok, status=None, from_date=None, to_date=None, page=1)


# --- CR-05: metric reset keeps SQLite and TSDB consistent ----------------------------------------
def test_reset_metrics_rolls_back_sqlite_when_tsdb_delete_fails(conn, monkeypatch):
    from app.api import targets
    from app.db import tsdb

    tid = _target(conn, "r")
    sqlite_db.create_alert(
        conn,
        alert_id="a1",
        target_id=tid,
        target_name="r",
        failed_service="http",
        started_at=datetime.now(UTC),
        recipients_notified=[],
    )
    conn.execute(
        "INSERT INTO target_status(target_id, overall_status, updated_at, uptime_percent) VALUES (?, 'up', ?, 99.5)",
        (tid, datetime.now(UTC)),
    )
    conn.commit()

    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(tsdb, "delete_target", boom)
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    try:
        resp = client.post(f"/targets/{tid}/reset")
        assert resp.status_code >= 500
    except OSError:
        pass  # the unhandled error surfaces through the test client; state is asserted below
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE target_id = ?", (tid,)).fetchone()[0] == 1
    assert sqlite_db.get_target_status(conn, tid) is not None


@pytest.mark.parametrize("stored", [None, ""])
def test_smtp_template_defaults_apply_on_both_routes_when_missing_or_empty(conn, monkeypatch, stored):
    from app.services.notifications.email_template import (
        DEFAULT_HTML_TEMPLATE,
        DEFAULT_SUBJECT,
        template_to_preview,
    )

    if stored is not None:
        sqlite_db.set_setting(conn, "smtp_mail_template_html", stored)
        sqlite_db.set_setting(conn, "smtp_mail_template_subject", stored)
    expected = {"subject": template_to_preview(DEFAULT_SUBJECT), "html": template_to_preview(DEFAULT_HTML_TEMPLATE)}
    client = _client(conn, settings.router, user=_mk_user(conn, "root", admin=True))
    assert client.get("/settings/smtp/template").json() == expected
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda request, conn: {"id": 1, "is_admin": 1})
    captured: dict = {}
    monkeypatch.setattr(frontend.templates, "TemplateResponse", lambda request, name, ctx: captured.update(ctx) or "ok")
    frontend.fragment_settings_smtp(SimpleNamespace(), conn)
    assert captured["template"] == expected


def test_storage_stats_counts_wal_and_shm_files(tmp_path):
    from app.utils.storage_stats import compute_storage_stats

    db_file = tmp_path / "justup.db"
    db_file.write_bytes(b"x" * 10)
    (tmp_path / "justup.db-wal").write_bytes(b"w" * 5)
    (tmp_path / "justup.db-shm").write_bytes(b"s" * 3)
    assert compute_storage_stats(db_file, tmp_path / "tsdb")["sql_size_bytes"] == 18


# --- downtime history: empty state must distinguish "all up" from "no data" ----------------------
def _downtime_fragment(conn, monkeypatch, tid: int, since: datetime) -> str:
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "dt", admin=False))
    resp = client.get(f"/ui/fragments/downtime-history/{tid}", params={"since": since.isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.text


def test_downtime_history_empty_state_without_measurements(conn, monkeypatch):
    tid = _target(conn, "nodata")
    html = _downtime_fragment(conn, monkeypatch, tid, datetime.now(UTC) - timedelta(days=1))
    assert "No measurement data in selected range" in html
    assert "stayed up" not in html


def test_downtime_history_empty_state_all_up(conn, monkeypatch):
    tid = _target(conn, "allup")
    conn.execute("UPDATE targets SET enable_http_check = 1 WHERE id = ?", (tid,))
    conn.commit()
    start = datetime.now(UTC) - timedelta(hours=2)
    _seed_http_up(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, tid, [1] * 5, start)
    html = _downtime_fragment(conn, monkeypatch, tid, start - timedelta(hours=1))
    assert "No downtimes in selected range" in html
    assert "No measurement data" not in html


def test_downtime_history_lists_periods_when_down(conn, monkeypatch):
    tid = _target(conn, "down")
    conn.execute("UPDATE targets SET enable_http_check = 1 WHERE id = ?", (tid,))  # only enabled checks count
    conn.commit()
    start = datetime.now(UTC) - timedelta(hours=2)
    _seed_http_up(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, tid, [1, 0] + [0] * 200 + [1], start)
    html = _downtime_fragment(conn, monkeypatch, tid, start - timedelta(hours=1))
    assert "No downtimes in selected range" not in html
    assert "No measurement data" not in html


# --- reports: _cap_points keeps first/last and never divides by zero ----------------------------
@pytest.mark.parametrize(
    ("max_points", "expected"),
    [(0, list(range(10))), (1, [0]), (2, [0, 9]), (3, [0, 8, 9])],
)
def test_cap_points_small_limits(max_points, expected):
    assert report_metrics.cap_points(list(range(10)), max_points) == expected


# --- users: admin revokes all sessions of a user ----------------------------------------------
def _issue_token(conn, user_id: int, token_hash: str) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    sqlite_db.issue_token(conn, user_id=user_id, token_hash=token_hash, expires_at=expires, max_expires_at=expires)


def test_admin_revokes_all_sessions_of_a_user(conn, caplog):
    admin = _mk_user(conn, "boss", admin=True)
    victim = _mk_user(conn, "victim", admin=False)
    other = _mk_user(conn, "other", admin=False)
    for h in ("v1", "v2"):
        _issue_token(conn, victim["id"], h)
    _issue_token(conn, other["id"], "o1")
    client = _client(conn, users.router, user=admin)

    with caplog.at_level("INFO"):
        resp = client.post(f"/users/{victim['id']}/revoke-sessions")

    assert resp.status_code == 200
    assert resp.json() == {"revoked": 2}
    remaining = conn.execute("SELECT user_id FROM auth_tokens").fetchall()
    assert [r[0] for r in remaining] == [other["id"]]
    assert any("USER_SESSIONS_REVOKED" in r.getMessage() and "by=boss" in r.getMessage() for r in caplog.records)


def test_revoke_sessions_requires_admin_and_rejects_self_and_unknown(conn):
    admin = _mk_user(conn, "boss", admin=True)
    plain = _mk_user(conn, "plain", admin=False)
    assert _client(conn, users.router, user=plain).post(f"/users/{admin['id']}/revoke-sessions").status_code == 403
    admin_client = _client(conn, users.router, user=admin)
    assert admin_client.post(f"/users/{admin['id']}/revoke-sessions").status_code == 403
    assert admin_client.post("/users/9999/revoke-sessions").status_code == 404


# --- targets: URLs with embedded credentials are rejected -------------------------------------
@pytest.mark.parametrize("url", ["http://user:secret@example.com/", "https://user@example.com/x", "http://:pw@h/"])
def test_http_url_with_userinfo_is_rejected_without_echo(url):
    from fastapi import HTTPException

    targets = importlib.import_module("app.api.targets")
    with pytest.raises(HTTPException) as exc:
        targets._validate_http_url(url)
    assert exc.value.status_code == 422
    assert "secret" not in str(exc.value.detail)


def test_http_url_without_userinfo_is_accepted():
    targets = importlib.import_module("app.api.targets")
    assert targets._validate_http_url("https://example.com:8443/a?b=c@d") == "https://example.com:8443/a?b=c@d"


# --- target detail: stored overall_status is passed through without a fallback -------------------
@pytest.mark.parametrize("stored", ["up", "down", "unknown", "disabled"])
def test_target_detail_view_passes_stored_overall_status_through(conn, stored):
    tid = _target(conn, f"detail-{stored}")
    conn.execute(
        "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, ?, ?)",
        (tid, stored, datetime.now(UTC)),
    )
    conn.commit()
    row = sqlite_db.get_target(conn, tid)
    assert frontend._target_detail_view(conn, row, tid)["overall_status"] == stored


@pytest.mark.parametrize(
    ("status", "label"),
    [("up", "Online"), ("down", "Offline"), ("disabled", "Disabled"), ("unknown", "Unknown")],
)
def test_target_detail_header_renders_current_status_immediately(status, label):
    rendered = frontend.templates.get_template("fragments/target_detail/header.html").render(
        target={"id": 1, "name": "example.com", "group": None, "overall_status": status},
        user=None,
        has_recipients=False,
    )

    assert f"target-status-pill--{status}" in rendered
    assert f"<span>{label}</span>" in rendered


def test_target_status_row_created_by_default_is_unknown_not_empty(conn):
    # The column is NOT NULL DEFAULT 'unknown' and every writer uses a TargetStatus constant.
    tid = _target(conn, "detail-default")
    conn.execute("INSERT INTO target_status(target_id, updated_at) VALUES (?, ?)", (tid, datetime.now(UTC)))
    conn.commit()
    assert sqlite_db.get_target_status(conn, tid)["overall_status"] == "unknown"
    assert frontend._target_detail_view(conn, sqlite_db.get_target(conn, tid), tid)["overall_status"] == "unknown"


# --- F4: the recipient test status GET only reads the queue ---------------------------------------
def test_recipient_test_status_get_does_not_change_the_queue(conn):
    rid = sqlite_db.create_recipient(conn, name="R", email="r@example.com")
    conn.execute(
        """
        INSERT INTO notification_queue(notification_type, channel_type, recipient_id, recipient_name,
            recipient_address, payload, status, retry_count, last_error, created_at, updated_at, next_retry_at)
        VALUES ('test', 'email', ?, 'R', 'r@example.com', '{}', 'pending', 1, 'smtp down', ?, ?, ?)
        """,
        (rid, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    qid = conn.execute("SELECT id FROM notification_queue").fetchone()[0]
    before = tuple(conn.execute("SELECT * FROM notification_queue WHERE id = ?", (qid,)).fetchone())
    client = _client(conn, recipients.router, user=_mk_user(conn, "root", admin=True))
    for _ in range(3):
        resp = client.get(f"/recipients/{rid}/test/{qid}")
        assert resp.status_code == 200
        assert resp.json()["state"] == "failed"
    assert tuple(conn.execute("SELECT * FROM notification_queue WHERE id = ?", (qid,)).fetchone()) == before


# --- F3: downtime overview without ``since`` is limited to the last 24 hours ----------------------
def test_downtime_without_since_ignores_outages_older_than_24h(conn):
    tid = _target(conn, "windowed")
    conn.execute("UPDATE targets SET enable_http_check = 1 WHERE id = ?", (tid,))
    conn.commit()
    tsdb_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    now = datetime.now(UTC)
    _seed_http_up(tsdb_dir, tid, [0] * 150 + [1] * 50, now - timedelta(days=3))
    _seed_http_up(tsdb_dir, tid, [0] * 150 + [1] * 600, now - timedelta(hours=1))
    client = _client(conn, metrics.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get("/dashboard/downtime")
    assert resp.status_code == 200
    summary = {t["target_id"]: t for t in resp.json()["targets"]}[tid]["services"]["http_up"]
    assert summary["incidents"] == 1
    assert summary["downtime_seconds"] == 150


# --- F6: user update and password reset are one transaction ---------------------------------------
def test_update_user_rolls_back_attributes_when_password_reset_fails(conn, monkeypatch):
    admin = _mk_user(conn, "root", admin=True)
    victim = _mk_user(conn, "victim", admin=False)

    def boom(*_a, **_k):
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(sqlite_db, "revoke_tokens_for_user", boom)
    client = _client(conn, users.router, user=admin)
    payload = {
        "username": "renamed",
        "is_admin": False,
        "can_close_alerts": True,
        "new_password": "Correct-Horse-Battery-9!",
    }
    with pytest.raises(RuntimeError):
        client.put(f"/users/{victim['id']}", json=payload)
    after = sqlite_db.get_user(conn, victim["id"])
    assert after["username"] == "victim"
    assert not after["can_close_alerts"]
    assert after["password_hash"] == "stub"  # noqa: S105


# --- P3: alerts range, signal verify code, report job errors -------------------------------------
@pytest.mark.parametrize(
    "params",
    [
        {"from_date": "2026-02-01T00:00:00Z", "to_date": "2026-01-01T00:00:00Z"},
        {"from_date": "2026-02-01T00:00:00", "to_date": "2026-01-01T00:00:00+00:00"},  # naive vs aware
    ],
)
def test_alert_list_rejects_inverted_range(conn, params):
    from app.api import alerts

    client = _client(conn, alerts.router, user=_mk_user(conn, "u", admin=False))
    assert client.get("/alerts", params=params).status_code == 400
    ok = client.get("/alerts", params={"from_date": "2026-01-01T00:00:00", "to_date": "2026-02-01T00:00:00Z"})
    assert ok.status_code == 200


def test_alert_summary_route_returns_numeric_counters_and_is_not_an_alert_id(conn):
    from app.api import alerts

    client = _client(conn, alerts.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get("/alerts/summary")
    assert resp.status_code == 200
    assert resp.json() == {"open": 0, "acknowledged_24h": 0, "started_24h": 0, "resolved_24h": 0}


@pytest.mark.parametrize(("code", "status"), [("12345a", 422), ("12 456", 422), ("12345", 422), ("123456", 204)])
def test_signal_verify_accepts_only_six_digits(conn, monkeypatch, code, status):
    from app.api import signal as signal_api

    seen: list[str] = []
    monkeypatch.setattr(
        signal_api, "get_signal_backend", lambda: SimpleNamespace(verify=lambda number, code: seen.append(code))
    )
    reset_all()
    client = _client(conn, signal_api.router, user=_mk_user(conn, "adm", admin=True))
    resp = client.post("/signal/verify", json={"number": "+4915112345678", "code": code})
    assert resp.status_code == status
    assert seen == (["123456"] if status == 204 else [])


def test_signal_verify_normalizes_number():
    from app.api.signal import RegisterRequest, VerifyRequest

    assert (
        VerifyRequest(number="+49 151 12345678", code="123456").number
        == RegisterRequest(number="+49 151 12345678").number
    )


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("PDF job could not be queued", "PDF job could not be queued"),
        ("OperationalError: no such table: secret_table", "PDF generation failed"),
        (None, "PDF generation failed"),
    ],
)
def test_failed_report_job_exposes_only_known_messages(conn, stored, shown):
    reports = importlib.import_module("app.api.reports")
    user = _mk_user(conn, "u", admin=False)
    job_id = "0" * 32
    sqlite_db.create_report_job(conn, job_id, _target(conn, "T"), user_id=user["id"])
    sqlite_db.update_report_job(conn, job_id, status="failed", error=stored, completed=True)
    resp = _client(conn, reports.router, user=user).get(f"/reports/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["error"] == shown


# --- DRY-1: one SMTP readiness rule for the recipient test and the SMTP test mail -----------------
@pytest.mark.parametrize(
    ("enabled", "host", "sender", "expected"),
    [
        (False, "smtp.example.com", "a@example.com", "disabled"),
        (True, "", "a@example.com", "incomplete"),
        (True, "smtp.example.com", "", "incomplete"),
        (True, "smtp.example.com", "a@example.com", "ready"),
    ],
)
def test_smtp_readiness_requires_enabled_host_and_sender(conn, enabled, host, sender, expected):
    from app.utils.constants import SettingKeys
    from app.utils.smtp_readiness import smtp_readiness

    sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, enabled)
    sqlite_db.set_setting(conn, SettingKeys.SMTP_HOST, host)
    sqlite_db.set_setting(conn, SettingKeys.SMTP_FROM, sender)
    assert smtp_readiness(conn) == expected
    if expected != "ready":
        with pytest.raises(recipients._TestRefusedError) as refused:
            recipients._build_email_test_payload(conn, "R")
        assert refused.value.status_code == 409


# --- DRY-2: notification category filter follows the stored category contract --------------------
def test_notification_category_filter_accepts_exactly_the_stored_categories():
    from app.api import notifications
    from app.db import notifications as inbox

    pattern = importlib.import_module("re").compile(notifications._NOTIFICATION_CATEGORY_PATTERN)
    assert all(pattern.match(category) for category in inbox.CATEGORIES)
    assert not pattern.match("monitoring|x")
    assert not pattern.match("unknown")


# --- F5: a failed metric reset can be retried until it completes ----------------------------------
def test_reset_metrics_is_retryable_after_a_failed_tsdb_delete(conn, monkeypatch):
    from app.api import targets
    from app.db import tsdb

    tid = _target(conn, "retry")
    sqlite_db.create_alert(
        conn,
        alert_id="a1",
        target_id=tid,
        target_name="retry",
        failed_service="http",
        started_at=datetime.now(UTC),
        recipients_notified=[],
    )
    real_delete = tsdb.delete_target
    monkeypatch.setattr(tsdb, "delete_target", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk")))
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    with pytest.raises(OSError, match="disk"):
        client.post(f"/targets/{tid}/reset")
    monkeypatch.setattr(tsdb, "delete_target", real_delete)
    assert client.post(f"/targets/{tid}/reset").status_code == 204
    assert client.post(f"/targets/{tid}/reset").status_code == 204  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE target_id = ?", (tid,)).fetchone()[0] == 0


# --- TSDB deletion result: leftover data is reported and the action stays repeatable --------------
def test_target_delete_rolls_back_when_tsdb_data_remains(conn, monkeypatch):
    from app.api import targets
    from app.db import tsdb

    tid = _target(conn, "left")
    real_delete = tsdb.delete_target
    monkeypatch.setattr(tsdb, "delete_target", lambda *_a, **_k: False)
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    resp = client.delete(f"/targets/{tid}")
    assert resp.status_code == 500
    assert "repeated" in resp.json()["detail"]
    assert sqlite_db.get_target(conn, tid) is not None
    monkeypatch.setattr(tsdb, "delete_target", real_delete)
    assert client.delete(f"/targets/{tid}").status_code == 204
    assert sqlite_db.get_target(conn, tid) is None


def test_reset_metrics_fails_when_tsdb_reports_leftovers(conn, monkeypatch):
    from app.api import targets
    from app.db import tsdb

    tid = _target(conn, "resetleft")
    sqlite_db.create_alert(
        conn,
        alert_id="a1",
        target_id=tid,
        target_name="resetleft",
        failed_service="http",
        started_at=datetime.now(UTC),
        recipients_notified=[],
    )
    monkeypatch.setattr(tsdb, "delete_target", lambda *_a, **_k: False)
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    assert client.post(f"/targets/{tid}/reset").status_code == 500
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE target_id = ?", (tid,)).fetchone()[0] == 1


def test_tsdb_purge_reports_leftover_data(conn, monkeypatch):
    from app.api import settings as settings_api
    from app.db import tsdb

    monkeypatch.setattr(settings_api, "is_purge_enabled", lambda _c: True)
    monkeypatch.setattr(settings_api, "_verify_admin_password", lambda *_a, **_k: None)
    monkeypatch.setattr(tsdb, "purge_tsdb", lambda _d: False)
    client = _client(conn, settings_api.router, user=_mk_user(conn, "root", admin=True))
    resp = client.post("/settings/tsdb/purge", json={"password": "x"})
    assert resp.status_code == 500
    assert "repeated" in resp.json()["detail"]
    monkeypatch.setattr(tsdb, "purge_tsdb", lambda _d: True)
    assert client.post("/settings/tsdb/purge", json={"password": "x"}).json() == {"ok": True}


def test_test_status_reports_legacy_pending_test_message_with_attempts_as_failed(conn):
    rid = sqlite_db.create_recipient(conn, name="R", email="r@example.com")
    conn.execute(
        """
        INSERT INTO notification_queue (notification_type, channel_type, recipient_id, recipient_name,
            recipient_address, payload, status, retry_count, next_retry_at, created_at, updated_at)
        VALUES ('test', 'email', ?, 'R', 'r@example.com', '{}', 'pending', 1, '2020-01-01T00:00:00+00:00',
            '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
        """,
        (rid,),
    )
    conn.commit()
    qid = conn.execute("SELECT MAX(id) FROM notification_queue").fetchone()[0]
    client = _client(conn, recipients.router, user=_mk_user(conn, "root", admin=True))
    reset_all()
    assert client.get(f"/recipients/{rid}/test/{qid}").json()["state"] == "failed"


@pytest.mark.parametrize("stored", [None, ""])
def test_signal_template_defaults_apply_on_both_routes_when_missing_or_empty(conn, monkeypatch, stored):
    from app.services.notifications.signal_template import (
        DEFAULT_SIGNAL_TEMPLATE_DOWN,
        DEFAULT_SIGNAL_TEMPLATE_UP,
    )
    from app.utils.constants import SettingKeys

    if stored is not None:
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, stored)
        sqlite_db.set_setting(conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, stored)
    expected = {"down": DEFAULT_SIGNAL_TEMPLATE_DOWN, "up": DEFAULT_SIGNAL_TEMPLATE_UP}
    client = _client(conn, settings.router, user=_mk_user(conn, "root", admin=True))
    assert client.get("/settings/signal/template").json() == expected
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda request, conn: {"id": 1, "is_admin": 1})
    captured: dict = {}
    monkeypatch.setattr(frontend.templates, "TemplateResponse", lambda request, name, ctx: captured.update(ctx) or "ok")
    frontend.fragment_settings_signal(SimpleNamespace(), conn)
    assert captured["signal_template"] == expected


def test_verify_signal_answers_cached_numbers_without_backend(conn, monkeypatch):
    from app.services.notifications import signal_backend

    phone = "+4915112345678"
    monkeypatch.setitem(recipients._SIGNAL_REGISTRATION_CACHE, phone, (True, time.monotonic()))
    monkeypatch.setattr(signal_backend, "get_signal_backend", lambda: pytest.fail("backend must not be called"))
    reset_all()
    client = _client(conn, recipients.router, user=_mk_user(conn, "root", admin=True))
    resp = client.post("/recipients/verify-signal", json=[phone])
    assert resp.status_code == 200
    assert resp.json()["results"] == {phone: True}


# --- recipients: the unique phone index is the authority --------------------------------------


def test_duplicate_phone_create_race_maps_to_409(conn, monkeypatch):
    client = _client(conn, recipients.router, user=_mk_user(conn, "adm", admin=True))
    monkeypatch.setattr(recipients, "_check_signal_registration", lambda phone: None)
    body = {"name": "A", "phone": "+4915112345678"}
    assert client.post("/recipients", json=body).status_code == 201
    # Simulate the lost race: the pre-check no longer sees the first row.
    monkeypatch.setattr(sqlite_db, "get_recipient_by_phone", lambda c, p: None)
    resp = client.post("/recipients", json={**body, "name": "B"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Phone number already exists"
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 1


def test_duplicate_phone_update_race_maps_to_409(conn, monkeypatch):
    client = _client(conn, recipients.router, user=_mk_user(conn, "adm", admin=True))
    monkeypatch.setattr(recipients, "_check_signal_registration", lambda phone: None)
    sqlite_db.create_recipient(conn, name="A", phone="+4915112345678")
    other = sqlite_db.create_recipient(conn, name="B", phone="+4915187654321")
    monkeypatch.setattr(sqlite_db, "get_recipient_by_phone", lambda c, p: None)
    resp = client.patch(f"/recipients/{other}", json={"phone": "+4915112345678"})
    assert resp.status_code == 409
    assert sqlite_db.get_recipient(conn, other)["phone"] == "+4915187654321"


def test_recipients_without_phone_may_repeat(conn):
    sqlite_db.create_recipient(conn, name="A", email="a@example.com")
    sqlite_db.create_recipient(conn, name="B", email="b@example.com")
    assert conn.execute("SELECT COUNT(*) FROM recipients WHERE phone IS NULL").fetchone()[0] == 2


# --- frontend review fixes: availability range cap, uptime cache, About cache, day-end filter ---


def _availability_status(conn, monkeypatch, tid: int, **params) -> int:
    monkeypatch.setattr(frontend.tsdb_db, "query_availability_series", lambda *_a, **_k: {})
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "avail", admin=False))
    return client.get(f"/ui/fragments/availability-series/{tid}", params=params).status_code


@pytest.mark.parametrize(
    ("since", "until", "expected"),
    [
        ("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 200),  # exactly 365 days
        ("2025-01-01T00:00:00Z", "2026-01-01T00:00:01Z", 400),  # one second over the cap
        ("2024-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 400),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", 400),  # since after until
        ("2026-01-01T00:00:00", "2026-01-01T00:00:00", 400),  # naive values are UTC, empty range
    ],
)
def test_availability_series_bounds_explicit_range(conn, monkeypatch, since, until, expected):
    tid = _target(conn, "cap")
    assert _availability_status(conn, monkeypatch, tid, since=since, until=until) == expected


def test_availability_series_treats_naive_range_as_utc(conn, monkeypatch):
    tid = _target(conn, "naive")
    captured = []
    monkeypatch.setattr(frontend.tsdb_db, "query_availability_series", lambda *_a, **kw: captured.append(kw) or {})
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "naive", admin=False))
    resp = client.get(
        f"/ui/fragments/availability-series/{tid}",
        params={"since": "2026-01-01T00:00:00", "until": "2026-01-02T00:00:00"},
    )
    assert resp.status_code == 200
    assert captured[0]["since"] == datetime(2026, 1, 1, tzinfo=UTC)


def test_uptime_fragment_caches_for_target_without_status_row(conn, monkeypatch):
    tid = _target(conn, "nostatus")
    conn.execute("UPDATE targets SET enable_http_check = 1 WHERE id = ?", (tid,))
    conn.commit()
    tsdb_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    _seed_http_up(tsdb_dir, tid, [1, 1, 1, 0], datetime.now(UTC) - timedelta(hours=1))
    assert sqlite_db.get_target_status(conn, tid) is None
    scans = []
    real = frontend.tsdb_db.count_binary_points
    monkeypatch.setattr(frontend.tsdb_db, "count_binary_points", lambda *a, **k: scans.append(1) or real(*a, **k))
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "up", admin=False))
    first = client.get(f"/ui/fragments/uptime/{tid}")
    second = client.get(f"/ui/fragments/uptime/{tid}")
    assert first.status_code == second.status_code == 200
    assert "75" in first.text
    assert first.text == second.text
    assert len(scans) == 1
    assert sqlite_db.get_target_status(conn, tid)["uptime_percent"] == 75.0


def test_cache_uptime_does_not_overwrite_scheduler_value(conn):
    tid = _target(conn, "sched")
    conn.execute(
        "INSERT INTO target_status (target_id, uptime_percent, updated_at) VALUES (?, 99.0, ?)",
        (tid, datetime.now(UTC)),
    )
    conn.commit()
    frontend._cache_uptime(conn, tid, 10.0)
    assert sqlite_db.get_target_status(conn, tid)["uptime_percent"] == 99.0


def test_about_page_probes_signal_cli_once_and_caches_failure(conn, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise FileNotFoundError("signal-cli")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(frontend, "_signal_cli_cache", None)
    monkeypatch.setattr(frontend, "require_ui_user", lambda *_a, **_k: {"id": 1})
    monkeypatch.setattr(frontend, "templates", SimpleNamespace(TemplateResponse=lambda _r, _n, ctx: ctx))
    for _ in range(3):
        ctx = frontend.about_page(SimpleNamespace(), conn)
    assert len(calls) == 1
    assert ("signal-cli", "not available") in ctx["dependencies"]


def test_about_page_reprobes_signal_cli_after_ttl(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: calls.append(1) or SimpleNamespace(returncode=0, stdout="signal-cli 0.13.23\n"),
    )
    monkeypatch.setattr(frontend, "_signal_cli_cache", None)
    assert frontend._signal_cli_version() == "0.13.23"
    assert frontend._signal_cli_version() == "0.13.23"
    assert len(calls) == 1
    monkeypatch.setattr(frontend, "_signal_cli_cache", (0.0, "old"))
    assert frontend._signal_cli_version() == "0.13.23"
    assert len(calls) == 2


def test_alerts_day_filter_includes_last_microsecond_of_day(conn):
    tid = _target(conn, "dayend")
    sqlite_db.create_alert(
        conn,
        alert_id="a-dayend",
        target_id=tid,
        target_name="dayend",
        failed_service="HTTP",
        started_at=datetime(2026, 3, 5, 23, 59, 59, 500000, tzinfo=UTC),
        recipients_notified=[],
    )
    ctx = frontend._alerts_panel_context(
        conn, target_id=None, status=None, from_date="2026-03-05", to_date="2026-03-05", page=1
    )
    assert [a["id"] for a in ctx["alerts"]] == ["a-dayend"]


def test_downtime_history_ignores_data_of_disabled_checks(conn, monkeypatch):
    tid = _target(conn, "disabled")
    conn.execute(
        "UPDATE targets SET enable_http_check = 0, enable_ping = 1, enable_tcp_connect = 0 WHERE id = ?", (tid,)
    )
    conn.commit()
    start = datetime.now(UTC) - timedelta(hours=2)
    _seed_http_up(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, tid, [1] * 5, start)
    html = _downtime_fragment(conn, monkeypatch, tid, datetime.now(UTC) - timedelta(days=1))
    assert "No measurement data in selected range" in html


def test_downtime_has_data_false_without_enabled_checks(tmp_path):
    assert frontend._has_availability_data(tmp_path, 1, datetime.now(UTC) - timedelta(days=1), None, ()) is False


def test_signal_qr_fragment_is_not_cacheable(conn, monkeypatch):
    from app.services.notifications import signal_backend

    class FakeBackend:
        def get_link_qr(self, device_name="justUp"):  # noqa: ARG002 - fake of the backend signature
            return b"\x89PNG", "sid"

    monkeypatch.setattr(signal_backend, "get_signal_backend", FakeBackend)
    monkeypatch.setattr(frontend, "require_admin_htmx", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "qr", admin=True))
    resp = client.get("/ui/fragments/signal-qr")
    assert resp.status_code == 200
    assert "data:image/png;base64" in resp.text
    assert resp.headers["Cache-Control"] == "private, no-store"
