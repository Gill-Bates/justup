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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import frontend, metrics, recipients, settings, users
from app.api.auth import get_current_user
from app.db import pop_locations
from app.db import sqlite as sqlite_db
from app.utils import crypto
from app.utils.deps import get_conn
from app.utils.rate_limit import limiter
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
    app.state.limiter = limiter
    app.state.tsdb_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
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


def test_downtime_history_fragment_rejects_inverted_range(conn, monkeypatch):
    tid = _target(conn, "T")
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(
        f"/ui/fragments/downtime-history/{tid}",
        params={"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
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
                "oldest_pending": None,
                "max_retry_count": 0,
                "next_retry_at": None,
                "stuck_processing": 0,
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
    limiter.reset()
    client = _client(conn, targets.router, user=_mk_user(conn, "root", admin=True))
    client.app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    statuses = [client.post(f"/targets/{tid}/test-alert").status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    assert len(seen) == 5
    assert all(c is not conn for c in seen)
    limiter.reset()


def test_signal_rate_key_is_per_user_not_per_token():
    from app.api import signal

    def req(user_id, token):
        return SimpleNamespace(
            state=SimpleNamespace(auth_user_id=user_id),
            headers={"authorization": f"Bearer {token}"},
        )

    key = signal._signal_limit_key
    assert key(req(1, "tok-a")) == key(req(1, "tok-b"))
    assert key(req(1, "tok-a")) != key(req(2, "tok-a"))


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
    limiter.reset()

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
    limiter.reset()


# --- session lifetime (cookie vs. server-side expiry) -----------------------------------------


@pytest.mark.parametrize("days", [1, 3, 7])
def test_login_cookie_max_age_is_full_configured_session_lifetime(conn, days):
    from app.models.users import hash_password

    sqlite_db.create_user(conn, username="alice", password_hash=hash_password("Correct-horse-1"), is_admin=False)
    sqlite_db.set_setting(conn, "session_max_days", str(days))
    limiter.reset()
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

    from app.api import reports, targets

    assert targets._validate_tcp_ports("443, 80 8080,80") == "80,443,8080"
    assert targets._validate_tcp_ports(None) == targets.DEFAULT_TCP_PORTS
    assert targets._validate_tcp_ports("  ") == targets.DEFAULT_TCP_PORTS
    for bad in ("abc", "0", "65536", "80,x", "80abc", "80.5", "\u00b2"):
        with pytest.raises(HTTPException) as exc:
            targets._validate_tcp_ports(bad)
        assert exc.value.status_code == 422

    assert reports._parse_tcp_ports("443,abc,0,65536, 80,,443") == [443, 80, 443]
    assert reports._parse_tcp_ports("") == []


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


def test_downtime_history_fragment_treats_naive_datetimes_as_utc(conn, monkeypatch):
    tid = _target(conn, "T")
    monkeypatch.setattr(frontend, "resolve_user_or_htmx_redirect", lambda *_a, **_k: {"id": 1})
    client = _client(conn, frontend.router, user=_mk_user(conn, "u", admin=False))
    resp = client.get(
        f"/ui/fragments/downtime-history/{tid}",
        params={"since": "2026-02-01T00:00:00", "until": "2026-01-01T00:00:00+00:00"},
    )
    assert resp.status_code == 400


@pytest.fixture
def signal_env(conn, monkeypatch):
    from app.utils.constants import SettingKeys

    limiter.reset()
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
    limiter.reset()
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
    start = datetime.now(UTC) - timedelta(hours=2)
    _seed_http_up(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, tid, [1] * 5, start)
    html = _downtime_fragment(conn, monkeypatch, tid, start - timedelta(hours=1))
    assert "No downtimes in selected range" in html
    assert "No measurement data" not in html


def test_downtime_history_lists_periods_when_down(conn, monkeypatch):
    tid = _target(conn, "down")
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
    reports = importlib.import_module("app.api.reports")
    assert reports._cap_points(list(range(10)), max_points) == expected


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
