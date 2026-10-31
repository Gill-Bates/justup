#!/usr/bin/env python3
#
# tests/test_review_cr_misc.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Review findings: wizard TCP host, welcome Signal sender, settings cache, DAL target identity, TLS admission."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from app.api import targets as targets_api
from app.db import sqlite as sqlite_db
from app.utils import runtime_settings, tls_scanner
from app.utils.constants import SettingKeys
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_api_hardening import _encryption_key, env  # noqa: F401 - reused API fixtures

# --- Wizard: TCP check on HTTP profiles ----------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "address"),
    [("website", "https://wizard-tcp.example.com/"), ("api", "https://wizard-tcp.example.com/api/health")],
)
def test_http_profile_with_tcp_check_previews_and_creates(env, profile, address):  # noqa: F811
    body = {
        "profile": profile,
        "address": address,
        "tcp_ports": "443",
        "overrides": {"enable_tcp_connect": True},
    }
    preview = env["admin"].call("POST", "/targets/preview", body)
    assert preview.status_code == 200, preview.text
    values = preview.json()["values"]
    assert values["enable_http_check"] is True
    assert values["enable_tcp_connect"] is True
    assert values["tcp_host"] == "wizard-tcp.example.com"
    assert values["tcp_ports"] == "443"
    created = env["admin"].call("POST", "/targets", values)
    assert created.status_code == 201, created.text
    assert created.json()["tcp_host"] == "wizard-tcp.example.com"


def test_tcp_override_without_ports_is_rejected_for_http_profiles(env):  # noqa: F811
    response = env["admin"].call(
        "POST",
        "/targets/preview",
        {"profile": "website", "address": "https://example.org/", "overrides": {"enable_tcp_connect": True}},
    )
    assert response.status_code == 422


def test_explicit_tcp_host_override_is_kept(env):  # noqa: F811
    response = env["admin"].call(
        "POST",
        "/targets/preview",
        {
            "profile": "website",
            "address": "https://example.org/",
            "tcp_ports": "22",
            "overrides": {"enable_tcp_connect": True, "tcp_host": "ssh.example.org"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"]["tcp_host"] == "ssh.example.org"


# --- Welcome notification: configured Signal sender ----------------------------------------------


class _Backend:
    """Mimics the signal-cli backend contract: configured sender wins, no silent account switch."""

    def __init__(self, accounts: list[str], *, fail: bool = False) -> None:
        self.accounts = accounts
        self.fail = fail

    def get_valid_sender(self, configured: str | None = None) -> str:
        if self.fail:
            raise RuntimeError("signal-cli unavailable")
        if configured in self.accounts:
            return configured
        raise LookupError("not registered")

    def list_accounts(self) -> list[str]:  # pragma: no cover - must never be used to pick a sender
        raise AssertionError("welcome sender must not be taken from the account list")


@pytest.fixture
def mem_conn(tmp_path: Path):
    conn = sqlite_db.connect(tmp_path / "welcome.db")
    sqlite_db.init_schema(conn)
    yield conn
    sqlite_db.close_connection(conn)


def test_welcome_sender_is_the_configured_account_not_the_first(mem_conn):
    sqlite_db.set_setting(mem_conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, "+4915100000002")
    backend = _Backend(["+4915100000001", "+4915100000002"])
    assert targets_api._resolve_welcome_signal_sender(mem_conn, backend) == "+4915100000002"


@pytest.mark.parametrize(
    "configured",
    [None, "", "+4915100000009"],
    ids=["unset", "empty", "not-registered"],
)
def test_welcome_signal_is_skipped_without_usable_sender(mem_conn, configured):
    if configured is not None:
        sqlite_db.set_setting(mem_conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, configured)
    backend = _Backend(["+4915100000001", "+4915100000002"])
    assert targets_api._resolve_welcome_signal_sender(mem_conn, backend) is None


def test_welcome_signal_is_skipped_when_backend_fails(mem_conn):
    sqlite_db.set_setting(mem_conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, "+4915100000001")
    assert targets_api._resolve_welcome_signal_sender(mem_conn, _Backend([], fail=True)) is None


# --- Runtime settings cache: no stale refill after invalidation ----------------------------------


class _Cfg:
    db_path = Path("/nonexistent/x.db")


def test_invalidation_during_read_is_not_overwritten_by_stale_fill(monkeypatch):
    runtime_settings.clear_runtime_cache()
    monkeypatch.setattr("app.utils.config.load_config", _Cfg)
    value = {"v": "old"}
    read_started = threading.Event()
    invalidated = threading.Event()

    def slow_read(_key, _db_path):
        stale = value["v"]
        read_started.set()
        assert invalidated.wait(5)
        return stale

    monkeypatch.setattr(runtime_settings, "_read", slow_read)
    results: list[object] = []
    worker = threading.Thread(target=lambda: results.append(runtime_settings.get_runtime_setting("k", "d")))
    worker.start()
    assert read_started.wait(5)
    value["v"] = "new"
    runtime_settings.clear_runtime_cache()  # a settings write lands while the read is in flight
    invalidated.set()
    worker.join(5)
    assert results == ["old"]  # the in-flight caller may see the old value ...
    monkeypatch.setattr(runtime_settings, "_read", lambda _key, _db_path: value["v"])
    assert runtime_settings.get_runtime_setting("k", "d") == "new"  # ... but it was never published
    runtime_settings.clear_runtime_cache()


def test_db_path_is_resolved_once_per_data_dir(monkeypatch, tmp_path):
    from app.utils import config

    monkeypatch.setattr(runtime_settings, "_path_memo", None)
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path / "a"))
    calls = []
    real = config.load_config
    monkeypatch.setattr(config, "load_config", lambda: calls.append(1) or real())
    first = runtime_settings._resolve_db_path()
    assert runtime_settings._resolve_db_path() == first and len(calls) == 1
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path / "b"))
    assert runtime_settings._resolve_db_path() != first and len(calls) == 2


# --- DAL: exact duplicate targets cannot be created or reached by direct calls -------------------


def _values(**over):
    base = {
        "name": "A",
        "host": "dal.example.com",
        "interval_seconds": 60,
        "retention_days": 30,
        "enable_http_check": True,
        "http_url": "https://dal.example.com/",
    }
    return {**base, **over}


def test_dal_rejects_exact_duplicate_but_allows_other_service_on_same_host(mem_conn):
    sqlite_db.create_target(mem_conn, _values())
    with pytest.raises(sqlite_db.DuplicateTargetError):
        sqlite_db.create_target(mem_conn, _values(name="B", http_url="https://DAL.example.com:443/"))
    other = sqlite_db.create_target(
        mem_conn,
        _values(
            name="C",
            enable_http_check=False,
            http_url=None,
            enable_tcp_connect=True,
            tcp_host="dal.example.com",
            tcp_ports="22",
        ),
    )
    assert other > 0
    assert len(sqlite_db.list_targets(mem_conn)) == 2


def test_dal_update_cannot_create_duplicate(mem_conn):
    sqlite_db.create_target(mem_conn, _values())
    second = sqlite_db.create_target(mem_conn, _values(name="B", http_url="https://dal.example.com/other"))
    with pytest.raises(sqlite_db.DuplicateTargetError):
        sqlite_db.update_target(mem_conn, target_id=second, patch={"http_url": "https://dal.example.com/"})
    assert sqlite_db.get_target(mem_conn, second)["http_url"] == "https://dal.example.com/other"
    sqlite_db.update_target(mem_conn, target_id=second, patch={"name": "B2"})  # self is never a conflict


def test_concurrent_direct_creates_yield_exactly_one_target(tmp_path: Path):
    path = tmp_path / "race.db"
    with closing(sqlite3.connect(path)):
        pass
    setup = sqlite_db.connect(path)
    sqlite_db.init_schema(setup)
    sqlite_db.close_connection(setup)
    barrier = threading.Barrier(6)
    outcomes: list[str] = []

    def create():
        conn = sqlite_db.connect(path)
        try:
            barrier.wait()
            try:
                sqlite_db.create_target(conn, _values())
                outcomes.append("ok")
            except sqlite_db.DuplicateTargetError:
                outcomes.append("dup")
        finally:
            sqlite_db.close_connection(conn)

    threads = [threading.Thread(target=create) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(outcomes) == ["dup"] * 5 + ["ok"]


# --- TLS scanner: bounded admission --------------------------------------------------------------


def test_tls_scan_rejects_when_pending_limit_is_reached_and_recovers(monkeypatch):
    release = threading.Event()
    started = threading.Semaphore(0)

    def blocking_scan(host, port, ip, _sni, _timeout):
        started.release()
        release.wait(10)
        return tls_scanner._failed_result(host, port, "done", ip)

    class _Resolved:
        hostname, port, ip_address = "tls.example.com", 443, "192.0.2.10"

    monkeypatch.setattr(tls_scanner, "resolve_target", lambda _t: _Resolved())
    monkeypatch.setattr(tls_scanner, "_run_sslyze", blocking_scan)
    monkeypatch.setattr(tls_scanner, "_admission", threading.BoundedSemaphore(2))
    monkeypatch.setattr(tls_scanner, "_pool", None)
    try:
        results: list = []
        threads = [
            threading.Thread(target=lambda: results.append(tls_scanner.scan_tls("tls.example.com", deadline=10)))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        assert started.acquire(timeout=5)
        assert started.acquire(timeout=5)  # both pool workers run, so both admission slots are taken
        busy = tls_scanner.scan_tls("tls.example.com", deadline=1)
        assert busy.status.value == "failed"
        assert "busy" in (busy.error or "")
        release.set()
        for t in threads:
            t.join(10)
        assert tls_scanner._admission._value == 2  # every slot returned
        assert tls_scanner.scan_tls("tls.example.com", deadline=5).error == "done"
    finally:
        release.set()
        tls_scanner.shutdown_tls_scanner()
