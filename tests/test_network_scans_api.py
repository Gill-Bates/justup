#!/usr/bin/env python3
#
# tests/test_network_scans_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Nmap scan job API: authorization, validation, job lifecycle, cancellation and limits.

The worker client is replaced by a local stand-in; no Nmap process and no network is involved.
"""

# ruff: noqa: ARG001, ARG002, ARG005, SLF001  # pytest fixtures used for side effects; white-box registry checks
from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import network_scans
from app.api.auth import get_current_user
from app.services import nmap_jobs
from app.services.nmap_scanner import (
    NmapHostResult,
    NmapPortResult,
    NmapScannerError,
    NmapScanResult,
    NmapServiceInfo,
)
from app.utils.rate_limit import RateLimitExceededError, rate_limit_exceeded_handler
from app.utils.time import utcnow


def _result() -> NmapScanResult:
    now = utcnow()
    port = NmapPortResult(
        protocol="tcp",
        port=22,
        state="open",
        service=NmapServiceInfo(name="ssh", method="probed", confidence=10),
    )
    return NmapScanResult(
        hosts=[NmapHostResult(address="10.0.0.5", ports=[port])], started_at=now, completed_at=now, complete=True
    )


def _user(request: Request) -> dict:
    spec = request.headers.get("X-Test-User")
    if not spec:
        raise HTTPException(status_code=401, detail="Not authenticated")
    role, ident = spec.split(":")
    return {"id": int(ident), "is_admin": 1 if role == "admin" else 0, "is_active": 1}


@pytest.fixture
def stub(monkeypatch):
    """Controllable worker client: records calls, can block until released or fail."""
    state = type("State", (), {})()
    state.calls = []
    state.error = None
    state.release = None
    state.cancelled = 0

    async def fake_request_scan(host, **kwargs):
        state.calls.append((host, kwargs))
        try:
            if state.release is not None:
                await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled += 1
            raise
        if state.error is not None:
            raise state.error
        return _result()

    async def worker_ready():
        return state.availability

    state.availability = {"available": True, "state": "ready", "message": "", "detail": ""}
    monkeypatch.setattr(nmap_jobs, "request_scan", fake_request_scan)
    monkeypatch.setattr(network_scans, "availability", worker_ready)
    return state


@pytest.fixture
def client(stub):
    app = FastAPI()
    app.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
    app.include_router(network_scans.router, prefix="/api")
    app.dependency_overrides[get_current_user] = _user
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(nmap_jobs.shutdown_jobs())


ADMIN = {"X-Test-User": "admin:1"}


def test_service_detection_requires_admin_and_explicit_confirmation(client):
    class DetectionScheduler:
        def __init__(self):
            self.previews = 0
            self.confirmations = 0

        async def preview_service_detection(self, target_id, owner_id):
            self.previews += 1
            assert (target_id, owner_id) == (7, 1)
            return {"token": "a" * 32, "changes": []}

        def confirm_service_detection(self, target_id, owner_id, token):
            self.confirmations += 1
            assert (target_id, owner_id, token) == (7, 1, "a" * 32)
            return []

    scheduler = DetectionScheduler()
    client.app.state.scheduler = scheduler
    path = "/api/network-scans/services/7"
    assert client.post(f"{path}/preview").status_code == 401
    assert client.post(f"{path}/preview", headers={"X-Test-User": "user:2"}).status_code == 403
    assert scheduler.previews == 0
    response = client.post(f"{path}/preview", headers=ADMIN)
    assert response.status_code == 200 and response.json()["token"] == "a" * 32
    assert scheduler.previews == 1 and scheduler.confirmations == 0
    assert (
        client.post(f"{path}/confirm", headers={"X-Test-User": "user:2"}, json={"token": "a" * 32}).status_code == 403
    )
    assert scheduler.confirmations == 0
    response = client.post(f"{path}/confirm", headers=ADMIN, json={"token": "a" * 32})
    assert response.status_code == 200 and scheduler.confirmations == 1


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (LookupError("Target not found"), 404),
        (ValueError("Target changed"), 409),
        (NmapScannerError("busy", "Nmap is busy"), 429),
        (NmapScannerError("unavailable", "Nmap worker is down"), 503),
        (NmapScannerError("timeout", "Nmap scan worker timed out"), 503),
    ],
)
def test_service_detection_maps_scheduler_errors_to_http_status(client, error, status_code):
    class FailingScheduler:
        async def preview_service_detection(self, target_id, owner_id):
            raise error

        def confirm_service_detection(self, target_id, owner_id, token):
            raise error

    client.app.state.scheduler = FailingScheduler()
    path = "/api/network-scans/services/7"
    detail = error.error.message if isinstance(error, NmapScannerError) else str(error)
    response = client.post(f"{path}/preview", headers=ADMIN)
    assert (response.status_code, response.json()["detail"]) == (status_code, detail)
    if not isinstance(error, NmapScannerError):  # confirm never talks to the worker
        response = client.post(f"{path}/confirm", headers=ADMIN, json={"token": "a" * 32})
        assert (response.status_code, response.json()["detail"]) == (status_code, detail)


def test_service_detection_without_a_scheduler_is_503(client):
    path = "/api/network-scans/services/7"
    assert client.post(f"{path}/preview", headers=ADMIN).status_code == 503
    assert client.post(f"{path}/confirm", headers=ADMIN, json={"token": "a" * 32}).status_code == 503


def test_service_detection_rejects_a_malformed_token(client):
    client.app.state.scheduler = object()
    response = client.post("/api/network-scans/services/7/confirm", headers=ADMIN, json={"token": "XYZ"})
    assert response.status_code == 422


OTHER_ADMIN = {"X-Test-User": "admin:2"}
PLAIN_USER = {"X-Test-User": "user:3"}


@pytest.mark.parametrize("action", ["preview", "confirm"])
@pytest.mark.parametrize("target_id", ["0", "-1", "abc"])
def test_service_detection_rejects_an_invalid_target_id(client, action, target_id):
    client.app.state.scheduler = object()
    response = client.post(f"/api/network-scans/services/{target_id}/{action}", headers=ADMIN, json={"token": "a" * 32})
    assert response.status_code == 422


def test_service_detection_preview_is_rate_limited_per_admin(client):
    class DetectionScheduler:
        previews = 0

        async def preview_service_detection(self, target_id, owner_id):
            self.previews += 1
            return {"token": None, "changes": []}

    scheduler = DetectionScheduler()
    client.app.state.scheduler = scheduler
    path = "/api/network-scans/services/7/preview"
    assert [client.post(path, headers=ADMIN).status_code for _ in range(3)] == [200, 200, 429]
    assert scheduler.previews == 2
    assert client.post(path, headers=OTHER_ADMIN).status_code == 200  # the budget is per admin


def test_service_detection_confirm_is_rate_limited_per_admin(client):
    class DetectionScheduler:
        confirmations = 0

        def confirm_service_detection(self, target_id, owner_id, token):
            self.confirmations += 1
            raise LookupError("Service detection preview not found")

    scheduler = DetectionScheduler()
    client.app.state.scheduler = scheduler
    path = "/api/network-scans/services/7/confirm"
    codes = [client.post(path, headers=ADMIN, json={"token": "a" * 32}).status_code for _ in range(6)]
    assert codes == [404] * 5 + [429]
    assert scheduler.confirmations == 5


class TestServiceDetectionWithRealScheduler:
    """The route and the real scheduler together: nothing is stored before a confirmation."""

    @pytest.fixture
    def detection(self, client, tmp_path, monkeypatch):
        from app.db import sqlite as sqlite_db
        from app.monitor.scheduler import MonitorScheduler
        from app.services import nmap_client

        db_path = tmp_path / "db" / "justup.db"
        conn = sqlite_db.connect(db_path)
        sqlite_db.init_schema(conn)
        state = type("State", (), {})()
        state.tid = sqlite_db.create_target(
            conn,
            {
                "name": "t",
                "interval_seconds": 60,
                "retention_days": 30,
                "host": "t.example",
                "enable_http_check": False,
                "enable_ping": False,
                "enable_tcp_connect": True,
                "enable_cert_expiration": False,
                "http_url": "https://t.example/",
                "ping_host": "t.example",
                "tcp_host": "t.example",
                "tcp_ports": "22",
            },
        )
        stored = NmapServiceInfo(name="ssh", product="OpenSSH", version="8.0", method="probed", confidence=10)
        found = NmapPortResult(protocol="tcp", port=22, state="open", service=stored)
        now = utcnow()
        sqlite_db.sync_network_ports(
            conn,
            state.tid,
            tcp_ports=[22],
            udp_ports=[],
            scan_result=NmapScanResult(
                hosts=[NmapHostResult(address="192.0.2.1", ports=[found])],
                started_at=now,
                completed_at=now,
                complete=True,
            ),
        )
        conn.commit()
        sqlite_db.close_connection(conn)
        state.db_path = db_path
        state.service = stored
        state.next_service = NmapServiceInfo(name="http", product="nginx", version="1", method="probed", confidence=10)

        async def request_scan(host, **kwargs):
            port = NmapPortResult(protocol="tcp", port=22, state="open", service=state.next_service)
            hosts = [] if state.next_service is None else [NmapHostResult(address="192.0.2.1", ports=[port])]
            stamp = utcnow()
            return NmapScanResult(hosts=hosts, started_at=stamp, completed_at=stamp, complete=True)

        monkeypatch.setattr(nmap_client, "request_scan", request_scan)
        scheduler = MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")
        scheduler._refresh_targets(utcnow())
        client.app.state.scheduler = scheduler
        state.stored_name = lambda: next(
            row["service_name"] for row in _rows(db_path, state.tid) if (row["protocol"], row["port"]) == ("tcp", 22)
        )
        return state

    def test_unknown_target_is_404(self, client, detection):
        assert client.post("/api/network-scans/services/9999/preview", headers=ADMIN).status_code == 404

    def test_no_scan_findings_means_no_token(self, client, detection):
        detection.next_service = None
        body = client.post(f"/api/network-scans/services/{detection.tid}/preview", headers=ADMIN).json()
        assert body == {"token": None, "changes": []}

    def test_nothing_is_stored_until_the_confirmation_and_it_works_once(self, client, detection):
        path = f"/api/network-scans/services/{detection.tid}"
        preview = client.post(f"{path}/preview", headers=ADMIN).json()
        assert [c["kind"] for c in preview["changes"]] == ["service_changed"]
        assert detection.stored_name() == "ssh"
        foreign = client.post(f"{path}/confirm", headers=OTHER_ADMIN, json={"token": preview["token"]})
        assert foreign.status_code == 404 and detection.stored_name() == "ssh"
        first = client.post(f"{path}/confirm", headers=ADMIN, json={"token": preview["token"]})
        assert first.status_code == 200 and detection.stored_name() == "http"
        again = client.post(f"{path}/confirm", headers=ADMIN, json={"token": preview["token"]})
        assert again.status_code == 404

    def test_a_double_submitted_confirmation_applies_once(self, client, detection):
        path = f"/api/network-scans/services/{detection.tid}"
        token = client.post(f"{path}/preview", headers=ADMIN).json()["token"]
        codes: list[int] = []
        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait()
            codes.append(client.post(f"{path}/confirm", headers=ADMIN, json={"token": token}).status_code)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(codes) == [200, 404]


def _rows(db_path, tid):
    from app.db import sqlite as sqlite_db

    conn = sqlite_db.connect(db_path)
    try:
        return [dict(row) for row in sqlite_db.list_network_ports(conn, tid)]
    finally:
        sqlite_db.close_connection(conn)


def _start(client, headers=ADMIN, **body):
    payload = {"host": "10.0.0.5", "protocols": ["tcp"], **body}
    return client.post("/api/network-scans", json=payload, headers=headers)


def _wait(client, job_id, wanted, headers=ADMIN):
    for _ in range(100):
        data = client.get(f"/api/network-scans/{job_id}", headers=headers).json()
        if data["status"] in wanted:
            return data
        time.sleep(0.05)
    raise AssertionError(f"job stayed {data['status']}")


class TestAuthorization:
    def test_unauthenticated_requests_are_rejected(self, client):
        assert _start(client, headers={}).status_code == 401
        assert client.get("/api/network-scans/x").status_code == 401
        assert client.delete("/api/network-scans/x").status_code == 401

    def test_non_admin_cannot_scan_or_read_or_cancel(self, client):
        assert _start(client, headers=PLAIN_USER).status_code == 403
        assert client.get("/api/network-scans/x", headers=PLAIN_USER).status_code == 403
        assert client.delete("/api/network-scans/x", headers=PLAIN_USER).status_code == 403

    def test_foreign_jobs_are_invisible_and_cannot_be_cancelled(self, client, stub):
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"completed"})
        assert client.get(f"/api/network-scans/{job_id}", headers=OTHER_ADMIN).status_code == 404
        assert client.delete(f"/api/network-scans/{job_id}", headers=OTHER_ADMIN).status_code == 404
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()["status"] == "completed"

    def test_foreign_running_job_survives_a_foreign_cancel(self, client, stub):
        stub.release = asyncio.Event()
        job_id = _start(client).json()["id"]
        assert client.delete(f"/api/network-scans/{job_id}", headers=OTHER_ADMIN).status_code == 404
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()["status"] in {"queued", "running"}
        assert stub.cancelled == 0


class TestValidation:
    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.0/24",
            "10.0.0.1-10",
            "10.0.0.1,10.0.0.2",
            "10.0.0.1 10.0.0.2",
            "-sS",
            "--script=vuln",
            "",
            " ",
            "a" * 300,
            "http://10.0.0.5",
            "127.0.0.1",
            "::1",
            "169.254.169.254",
            "localhost",
        ],
    )
    def test_invalid_or_forbidden_hosts(self, client, stub, host):
        response = _start(client, host=host)
        assert response.status_code == 422
        assert stub.calls == []

    @pytest.mark.parametrize(
        "body",
        [
            {"protocols": []},
            {"protocols": ["icmp"]},
            {"protocols": ["tcp", "sctp"]},
            {"profile": "full"},
            {"protocols": "tcp"},
        ],
    )
    def test_invalid_protocols_and_profiles(self, client, stub, body):
        assert _start(client, **body).status_code == 422
        assert stub.calls == []

    @pytest.mark.parametrize(
        "extra",
        [
            {"ports": [22]},
            {"tcp_ports": "1-65535"},
            {"args": "-A --script vuln"},
            {"options": ["-O"]},
            {"timeout": 9999},
        ],
    )
    def test_ports_and_free_arguments_are_rejected_not_ignored(self, client, stub, extra):
        assert _start(client, **extra).status_code == 422
        assert stub.calls == []

    def test_missing_body_fields(self, client):
        assert client.post("/api/network-scans", json={}, headers=ADMIN).status_code == 422

    def test_unresolvable_host_is_a_client_error(self, client, stub, monkeypatch):
        monkeypatch.setattr(nmap_jobs, "resolve_validated", lambda host, allow_private: None)
        assert _start(client, host="gone.example.test").status_code == 422
        assert stub.calls == []


class TestLifecycle:
    def test_start_returns_immediately_with_a_job_id_while_the_scan_runs(self, client, stub):
        stub.release = asyncio.Event()
        started = time.monotonic()
        response = _start(client, protocols=["udp", "tcp"], profile="extended")
        assert response.status_code == 202
        body = response.json()
        assert time.monotonic() - started < 2
        assert body["status"] in {"queued", "running"}
        assert body["host"] == "10.0.0.5" and body["protocols"] == ["tcp", "udp"] and body["profile"] == "extended"
        assert body["result"] is None and body["error"] is None
        assert "progress" not in body
        assert client.get(f"/api/network-scans/{body['id']}", headers=ADMIN).json()["status"] in {"queued", "running"}

    def test_completed_job_carries_the_typed_result(self, client, stub):
        job = _wait(client, _start(client).json()["id"], {"completed"})
        port = job["result"]["hosts"][0]["ports"][0]
        assert port["protocol"] == "tcp" and port["state"] == "open"
        assert port["service"] == {
            "name": "ssh",
            "product": None,
            "version": None,
            "method": "probed",
            "confidence": 10,
        }
        assert job["error"] is None

    def test_scan_request_uses_the_validated_address_not_the_hostname(self, client, stub, monkeypatch):
        import ipaddress

        monkeypatch.setattr(
            nmap_jobs, "resolve_validated", lambda host, allow_private: [ipaddress.ip_address("10.1.2.3")]
        )
        body = _start(client, host="printer.example.test").json()
        _wait(client, body["id"], {"completed"})
        assert body["host"] == "printer.example.test"
        assert stub.calls[0][0] == "10.1.2.3"

    @pytest.mark.parametrize(
        "code",
        ["unavailable", "permission_denied", "timeout", "output_limit", "process_failed", "invalid_xml"],
    )
    def test_engine_errors_become_failed_jobs_with_the_code(self, client, stub, code):
        stub.error = NmapScannerError(code, "boom")
        job = _wait(client, _start(client).json()["id"], {"failed"})
        assert job["error"] == {"code": code, "message": "boom"}
        assert job["result"] is None

    def test_unexpected_exception_is_contained_and_not_leaked(self, client, stub):
        stub.error = RuntimeError("SECRET /etc/shadow detail")
        job = _wait(client, _start(client).json()["id"], {"failed"})
        assert job["error"]["code"] == "process_failed"
        assert "SECRET" not in job["error"]["message"]
        assert _start(client).status_code == 202  # the application keeps serving

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/network-scans/nope", headers=ADMIN).status_code == 404
        assert client.delete("/api/network-scans/nope", headers=ADMIN).status_code == 404


class TestCancellation:
    def test_cancel_stops_the_scan_and_late_results_are_never_adopted(self, client, stub):
        stub.release = asyncio.Event()
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"running"})
        assert client.delete(f"/api/network-scans/{job_id}", headers=ADMIN).status_code == 204
        assert stub.cancelled == 1
        job = client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()
        assert job["status"] == "cancelled" and job["result"] is None
        time.sleep(0.2)
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()["status"] == "cancelled"

    def test_cancel_is_idempotent(self, client, stub):
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"completed"})
        assert client.delete(f"/api/network-scans/{job_id}", headers=ADMIN).status_code == 204
        assert client.delete(f"/api/network-scans/{job_id}", headers=ADMIN).status_code == 204
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()["status"] == "completed"

    def test_cancel_immediately_after_start_releases_the_target(self, client, stub):
        stub.release = asyncio.Event()
        first = _start(client).json()["id"]
        assert client.delete(f"/api/network-scans/{first}", headers=ADMIN).status_code == 204
        assert client.get(f"/api/network-scans/{first}", headers=ADMIN).json()["status"] == "cancelled"
        assert _start(client).status_code == 202

    def test_cancelled_job_never_turns_completed_even_if_the_client_returns_late(self, client, stub):
        stub.release = asyncio.Event()
        job_id = _start(client).json()["id"]
        client.delete(f"/api/network-scans/{job_id}", headers=ADMIN)
        job = nmap_jobs._jobs[job_id].job
        job.status = "cancelled"
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()["result"] is None


class TestLimitsAndExpiry:
    def test_second_scan_for_the_same_address_is_429(self, client, stub):
        stub.release = asyncio.Event()
        assert _start(client).status_code == 202
        assert _start(client, headers=OTHER_ADMIN).status_code == 429
        assert _start(client, host="10.0.0.6").status_code == 202

    def test_address_is_free_again_after_completion(self, client, stub):
        _wait(client, _start(client).json()["id"], {"completed"})
        assert _start(client).status_code == 202

    def test_queue_is_bounded(self, client, stub, monkeypatch):
        stub.release = asyncio.Event()
        monkeypatch.setattr(nmap_jobs, "MAX_ACTIVE_JOBS", 3)
        for n in range(3):
            assert _start(client, host=f"10.0.1.{n}", headers={"X-Test-User": f"admin:{10 + n}"}).status_code == 202
        assert _start(client, host="10.0.1.9", headers={"X-Test-User": "admin:99"}).status_code == 429

    def test_one_admin_cannot_fill_the_whole_queue(self, client, stub, monkeypatch):
        stub.release = asyncio.Event()
        monkeypatch.setattr(nmap_jobs, "MAX_ACTIVE_JOBS_PER_OWNER", 2)
        assert _start(client, host="10.0.2.1").status_code == 202
        assert _start(client, host="10.0.2.2").status_code == 202
        assert _start(client, host="10.0.2.3").status_code == 429
        assert _start(client, host="10.0.2.3", headers=OTHER_ADMIN).status_code == 202

    def test_parallel_requests_for_distinct_hosts_all_complete(self, client, stub):
        results = {}

        def run(n):
            results[n] = _start(client, host=f"10.0.3.{n}", headers={"X-Test-User": f"admin:{100 + n}"})

        threads = [threading.Thread(target=run, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert {r.status_code for r in results.values()} == {202}
        assert len({r.json()["id"] for r in results.values()}) == 5

    def test_parallel_requests_for_one_host_admit_exactly_one(self, client, stub):
        stub.release = asyncio.Event()
        codes = []

        def run(n):
            codes.append(_start(client, headers={"X-Test-User": f"admin:{200 + n}"}).status_code)

        threads = [threading.Thread(target=run, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(codes) == [202, 429, 429, 429, 429]

    def test_rate_limit_per_user(self, client, stub):
        codes = [_start(client, host=f"10.0.4.{n}").status_code for n in range(5)]
        assert codes[:3] == [202, 202, 202] and 429 in codes[3:]

    def test_running_job_expires_is_cancelled_and_released(self, client, stub):
        stub.release = asyncio.Event()
        job_id = _start(client).json()["id"]
        nmap_jobs._jobs[job_id].job.expires_at = utcnow() - timedelta(seconds=1)
        job = client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()
        assert job["status"] == "expired" and job["result"] is None
        time.sleep(0.2)
        assert stub.cancelled == 1
        assert _start(client).status_code == 202

    def test_expired_job_disappears_after_retention(self, client, stub):
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"completed"})
        nmap_jobs._jobs[job_id].job.expires_at = utcnow() - nmap_jobs.EXPIRED_RETENTION - timedelta(seconds=1)
        assert client.get(f"/api/network-scans/{job_id}", headers=ADMIN).status_code == 404

    def test_finished_job_is_reported_expired_after_its_ttl(self, client, stub):
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"completed"})
        nmap_jobs._jobs[job_id].job.expires_at = utcnow() - timedelta(seconds=1)
        job = client.get(f"/api/network-scans/{job_id}", headers=ADMIN).json()
        assert job["status"] == "expired" and job["result"] is None

    def test_completed_job_for_persistence_is_owner_bound(self, client, stub):
        job_id = _start(client).json()["id"]
        _wait(client, job_id, {"completed"})
        assert nmap_jobs.completed_job(job_id, 1) is not None
        assert nmap_jobs.completed_job(job_id, 2) is None

    def test_web_app_survives_when_the_worker_is_missing(self, client, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr("app.services.nmap_client.SOCKET_PATH", "/nonexistent/nmap.sock")
        response = _start(client)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "unavailable"
        assert client.get("/api/network-scans/nope", headers=ADMIN).status_code == 404


class TestAvailability:
    def test_status_reports_a_ready_worker(self, client, stub):
        body = client.get("/api/network-scans/status", headers=ADMIN).json()
        assert body["available"] is True and body["state"] == "ready"

    def test_status_names_the_reason_when_the_worker_is_down(self, client, stub):
        stub.availability = {
            "available": False,
            "state": "failed",
            "message": "Nmap is not installed on this host. Install the nmap package and restart justUp!",
            "detail": "nmap binary not found",
        }
        body = client.get("/api/network-scans/status", headers=ADMIN).json()
        assert body["available"] is False and body["state"] == "failed"
        assert "Install the nmap package" in body["message"]

    def test_status_requires_an_admin(self, client):
        assert client.get("/api/network-scans/status").status_code == 401
        assert client.get("/api/network-scans/status", headers=PLAIN_USER).status_code == 403

    def test_status_is_not_mistaken_for_a_scan_id(self, client, stub):
        assert client.get("/api/network-scans/status", headers=ADMIN).json().get("id") is None

    def test_start_is_refused_with_a_machine_readable_code_while_the_worker_is_down(self, client, stub):
        stub.availability = {
            "available": False,
            "state": "restarting",
            "message": "Nmap scan worker is restarting",
            "detail": "",
        }
        response = _start(client)
        assert response.status_code == 503
        assert response.json()["detail"] == {"code": "unavailable", "message": "Nmap scan worker is restarting"}
        assert stub.calls == []

    def test_start_works_again_once_the_worker_is_back(self, client, stub):
        stub.availability = {"available": False, "state": "restarting", "message": "x", "detail": ""}
        assert _start(client).status_code == 503
        stub.availability = {"available": True, "state": "ready", "message": "", "detail": ""}
        assert _start(client).status_code == 202


def test_late_cleanup_of_an_expired_job_keeps_the_reservation_of_its_successor(monkeypatch):
    """A cancelled-but-still-unwinding task must not release the address a newer job now owns."""
    import ipaddress

    monkeypatch.setattr(nmap_jobs, "resolve_validated", lambda host, allow_private: [ipaddress.ip_address("10.9.9.9")])

    async def scenario():
        slow_unwind = asyncio.Event()
        first_call = True

        async def fake_request_scan(address, **kwargs):
            nonlocal first_call
            hold = first_call
            first_call = False
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if hold:
                    await slow_unwind.wait()  # the old worker call needs time to unwind
                raise

        monkeypatch.setattr(nmap_jobs, "request_scan", fake_request_scan)
        request = nmap_jobs.NmapScanRequest(host="10.9.9.9", protocols={"tcp"})
        first = await nmap_jobs.create_job(request, owner_id=1)
        await asyncio.sleep(0)
        nmap_jobs._jobs[first.id].job.expires_at = nmap_jobs.utcnow() - timedelta(seconds=1)
        second = await nmap_jobs.create_job(request, owner_id=2)  # prunes the old job, takes the address
        slow_unwind.set()
        await asyncio.sleep(0.05)  # the old task finishes its cleanup now
        with pytest.raises(nmap_jobs.NmapScannerError) as busy:
            await nmap_jobs.create_job(request, owner_id=3)
        assert busy.value.error.code == "busy"
        await nmap_jobs.cancel_job(second.id, 2)
        await nmap_jobs.shutdown_jobs()

    asyncio.run(scenario())
