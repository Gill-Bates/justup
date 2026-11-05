#!/usr/bin/env python3
#
# tests/test_network_ports_persistence.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Multi-protocol target persistence: schema 6 -> 8 migration, port identity and server-derived service metadata."""

# ruff: noqa: SLF001  # white-box access to the migration table
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import targets
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.db.sqlite import migrations
from app.services import target_identity
from app.services.nmap_scanner import (
    NmapHostResult,
    NmapPortResult,
    NmapScanResult,
    NmapServiceInfo,
)
from app.utils.deps import get_conn
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

NEW_PORT_COLUMNS = ("raw_state", "service_identity", "identity_changed_at", "latency_ms")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


@pytest.fixture
def conn(db_path: Path):
    connection = sqlite_db.connect(db_path)
    yield connection
    sqlite_db.close_connection(connection)


def _values(name: str = "t", **extra) -> dict:
    return {
        "name": name,
        "host": f"{name}.example.com",
        "interval_seconds": 60,
        "retention_days": 30,
        "enable_ping": True,
        "enable_tcp_connect": True,
        "tcp_host": f"{name}.example.com",
        "tcp_ports": "22,53",
        **extra,
    }


def _downgrade_to_v6(conn: sqlite3.Connection) -> None:
    """Shape a current database like the released v1.0.2 schema (user_version 6)."""
    conn.execute("DROP TABLE target_network_ports")
    conn.execute("ALTER TABLE targets DROP COLUMN enable_udp_scan")
    conn.execute("ALTER TABLE targets DROP COLUMN udp_ports")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()


def _scan(*ports: NmapPortResult, address: str = "198.51.100.7") -> NmapScanResult:
    now = utcnow()
    return NmapScanResult(
        hosts=[NmapHostResult(address=address, ports=list(ports))], started_at=now, completed_at=now, complete=True
    )


def _port(protocol: str, port: int, state: str = "open", **service) -> NmapPortResult:
    return NmapPortResult(
        protocol=protocol, port=port, state=state, service=NmapServiceInfo(**service) if service else None
    )


def _app_client(conn, tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.tsdb_dir = tmp_path
    app.include_router(targets.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "root", "is_admin": 1}
    return TestClient(app)


# ─── migration from the previous schema version ───────────────────────────────


def test_migration_from_v6_keeps_tcp_and_ping_and_defaults_udp_off(db_path: Path):
    conn = sqlite_db.connect(db_path)
    tid = sqlite_db.create_target(conn, _values("legacy"))
    quiet = sqlite_db.create_target(conn, _values("quiet", enable_tcp_connect=False, tcp_ports="80"))
    _downgrade_to_v6(conn)
    sqlite_db.close_connection(conn)

    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.init_schema(conn)
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        row = sqlite_db.get_target(conn, tid)
        assert (row["enable_ping"], row["enable_tcp_connect"], row["tcp_ports"]) == (1, 1, "22,53")
        assert (row["enable_udp_scan"], row["udp_ports"]) == (0, "")
        assert sqlite_db.get_target(conn, quiet)["enable_udp_scan"] == 0
        ports = sqlite_db.list_network_ports(conn, tid)
        assert [(p["protocol"], p["port"], p["source"]) for p in ports] == [
            ("tcp", 22, "manual"),
            ("tcp", 53, "manual"),
        ]
        assert {p["service_identity"] for p in ports} == {"unknown"}
        assert sqlite_db.list_network_ports(conn, quiet) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        sqlite_db.close_connection(conn)


def test_migration_is_idempotent_and_rerun_safe(db_path: Path):
    conn = sqlite_db.connect(db_path)
    tid = sqlite_db.create_target(conn, _values("legacy"))
    _downgrade_to_v6(conn)
    sqlite_db.close_connection(conn)
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.init_schema(conn)
        before = sqlite_db.list_network_ports(conn, tid)
        # Re-running both steps directly (e.g. after an interrupted deploy) must change nothing.
        migrations._migrate_7_network_ports(conn)
        migrations._migrate_8_network_port_state(conn)
        conn.commit()
        sqlite_db.init_schema(conn)
        assert sqlite_db.list_network_ports(conn, tid) == before
        columns = [r[1] for r in conn.execute("PRAGMA table_info(target_network_ports)")]
        assert len(columns) == len(set(columns))
    finally:
        sqlite_db.close_connection(conn)


def test_database_already_at_v7_gains_the_state_columns(db_path: Path):
    conn = sqlite_db.connect(db_path)
    tid = sqlite_db.create_target(conn, _values("dev"))
    sqlite_db.sync_network_ports(
        conn, tid, tcp_ports=[22], udp_ports=[], scan_result=_scan(_port("tcp", 22, name="ssh", method="probed"))
    )
    conn.commit()
    for column in NEW_PORT_COLUMNS:
        conn.execute(f"ALTER TABLE target_network_ports DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    sqlite_db.close_connection(conn)

    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.init_schema(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(target_network_ports)")}
        assert set(NEW_PORT_COLUMNS) <= columns
        (port,) = sqlite_db.list_network_ports(conn, tid)
        assert (port["service_name"], port["source"], port["service_identity"]) == ("ssh", "discovered", "identified")
    finally:
        sqlite_db.close_connection(conn)


# ─── multi-protocol round trip and port identity ──────────────────────────────


def test_api_round_trip_with_tcp_udp_and_icmp(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    body = {
        "name": "multi",
        "host": "multi.example.com",
        "enable_ping": True,
        "enable_tcp_connect": True,
        "tcp_host": "multi.example.com",
        "tcp_ports": "53,22",
        "enable_udp_scan": True,
        "udp_ports": "123,53",
    }
    created = client.post("/targets", json=body)
    assert created.status_code == 201, created.text
    shown = client.get(f"/targets/{created.json()['id']}").json()
    assert (shown["enable_ping"], shown["enable_tcp_connect"], shown["enable_udp_scan"]) == (True, True, True)
    assert (shown["tcp_ports"], shown["udp_ports"]) == ("22,53", "53,123")
    assert [(p["protocol"], p["port"], p["source"]) for p in shown["network_ports"]] == [
        ("tcp", 22, "manual"),
        ("tcp", 53, "manual"),
        ("udp", 53, "manual"),
        ("udp", 123, "manual"),
    ]
    patched = client.patch(f"/targets/{shown['id']}", json={"enable_udp_scan": False})
    assert patched.status_code == 200, patched.text
    assert patched.json()["udp_ports"] == "53,123"  # ports are kept, only the check is off
    assert {p["protocol"] for p in patched.json()["network_ports"]} == {"tcp"}


def test_clients_that_only_know_tcp_fields_keep_working(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    created = client.post(
        "/targets",
        json={
            "name": "old-client",
            "host": "old.example.com",
            "enable_tcp_connect": True,
            "tcp_host": "old.example.com",
            "tcp_ports": "25,587",
        },
    )
    assert created.status_code == 201, created.text
    shown = created.json()
    assert (shown["enable_udp_scan"], shown["udp_ports"]) == (False, "")
    assert (shown["tcp_ports"], shown["enable_tcp_connect"]) == ("25,587", True)
    again = client.patch(f"/targets/{shown['id']}", json={"tcp_ports": "25"})
    assert again.status_code == 200 and again.json()["tcp_ports"] == "25"
    assert [p["port"] for p in again.json()["network_ports"]] == [25]


def test_udp_without_ports_is_rejected(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    resp = client.post("/targets", json={"name": "u", "host": "u.example.com", "enable_udp_scan": True})
    assert resp.status_code == 422


def test_tcp_53_and_udp_53_are_distinct_identities(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    tcp = {
        "name": "dns-tcp",
        "host": "dns.example.com",
        "enable_tcp_connect": True,
        "tcp_host": "dns.example.com",
        "tcp_ports": "53",
    }
    udp = {"name": "dns-udp", "host": "dns.example.com", "enable_udp_scan": True, "udp_ports": "53"}
    assert client.post("/targets", json=tcp).status_code == 201
    assert client.post("/targets", json=udp).status_code == 201
    identities = target_identity.check_identities(
        {
            "host": "dns.example.com",
            "enable_tcp_connect": True,
            "tcp_ports": "53",
            "enable_udp_scan": True,
            "udp_ports": "53",
        }
    )
    assert {kind for kind, _ in identities} == {"tcp", "udp"}
    duplicate = client.post("/targets", json=dict(udp, name="dns-udp-2"))
    assert duplicate.status_code == 409


def test_tcp_and_udp_rows_with_the_same_number_coexist(conn):
    tid = sqlite_db.create_target(conn, _values("same", enable_udp_scan=True, udp_ports="53"))
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[53], udp_ports=[53])
    sqlite_db.update_network_port_status(conn, tid, "udp", 53, "unknown", raw_state="open|filtered")
    sqlite_db.update_network_port_status(conn, tid, "tcp", 53, "up", latency_ms=1.5)
    by_key = {(p["protocol"], p["port"]): p for p in sqlite_db.list_network_ports(conn, tid)}
    assert by_key[("udp", 53)]["status"] == "unknown" and by_key[("udp", 53)]["raw_state"] == "open|filtered"
    assert by_key[("tcp", 53)]["status"] == "up" and by_key[("tcp", 53)]["latency_ms"] == 1.5
    assert by_key[("tcp", 53)]["raw_state"] is None


def test_invalid_state_values_are_rejected(conn):
    tid = sqlite_db.create_target(conn, _values("bad"))
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22], udp_ports=[])
    with pytest.raises(ValueError):
        sqlite_db.update_network_port_status(conn, tid, "tcp", 22, "up", raw_state="weird")


# ─── service metadata is derived on the server only ───────────────────────────


def test_manual_ports_have_no_service_identity_but_are_storable(conn):
    tid = sqlite_db.create_target(conn, _values("manual", enable_udp_scan=True, udp_ports="161"))
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22], udp_ports=[161])
    for port in sqlite_db.list_network_ports(conn, tid):
        assert port["source"] == "manual"
        assert port["service_name"] is None and port["method"] is None and port["discovered_at"] is None
        assert port["service_identity"] == "unknown"


def test_scan_metadata_is_stored_per_protocol_and_table_lookups_are_not_identification(conn):
    tid = sqlite_db.create_target(conn, _values("scan", enable_udp_scan=True, udp_ports="53"))
    scan = _scan(
        _port("tcp", 22, name="ssh", product="OpenSSH", version="9.6", method="probed", confidence=10),
        _port("udp", 53, "open|filtered", name="domain", method="table", confidence=3),
    )
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22, 53], udp_ports=[53], scan_result=scan)
    by_key = {(p["protocol"], p["port"]): p for p in sqlite_db.list_network_ports(conn, tid)}
    ssh = by_key[("tcp", 22)]
    assert (ssh["source"], ssh["service_name"], ssh["method"], ssh["confidence"]) == ("discovered", "ssh", "probed", 10)
    assert ssh["service_identity"] == "identified" and ssh["discovered_at"] is not None
    dns_udp = by_key[("udp", 53)]
    assert dns_udp["method"] == "table" and dns_udp["service_identity"] == "unknown"
    # TCP:53 was not observed, so UDP:53 metadata must not leak onto it.
    assert by_key[("tcp", 53)]["service_name"] is None and by_key[("tcp", 53)]["source"] == "manual"


def test_unchanged_ports_keep_their_metadata_when_saved_again(conn):
    tid = sqlite_db.create_target(conn, _values("keep"))
    scan = _scan(_port("tcp", 22, name="ssh", method="probed", confidence=10))
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22, 53], udp_ports=[], scan_result=scan)
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22], udp_ports=[])
    (port,) = sqlite_db.list_network_ports(conn, tid)
    assert (port["port"], port["service_name"], port["source"]) == (22, "ssh", "discovered")


def test_scan_host_change_discards_observations_but_keeps_ports_and_history(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    created = client.post(
        "/targets",
        json={
            "name": "host-reset",
            "host": "old.example.com",
            "enable_tcp_connect": True,
            "tcp_host": "old.example.com",
            "tcp_ports": "22",
            "enable_udp_scan": True,
            "udp_ports": "53",
        },
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    scan = _scan(
        _port("tcp", 22, name="ssh", product="OpenSSH", version="9.6", method="probed", confidence=10),
        _port("udp", 53, name="domain", method="probed", confidence=8),
    )
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22], udp_ports=[53], scan_result=scan)
    sqlite_db.update_network_port_status(conn, tid, "tcp", 22, "up", raw_state="open", latency_ms=2.5)
    sqlite_db.update_network_port_status(conn, tid, "udp", 53, "down", raw_state="closed")
    sqlite_db.record_service_observation(conn, tid, "tcp", 22, identity="changed", detail="old host service")
    conn.execute(
        "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, 'down', ?)",
        (tid, utcnow()),
    )
    conn.commit()
    tsdb.append_point(tmp_path, target_id=tid, metric="tcp_up_22", value=1, retention_days=30, at=utcnow())
    history = tsdb.query_latest(tmp_path, target_id=tid, metric="tcp_up_22")

    renamed = client.patch(f"/targets/{tid}", json={"name": "host-reset-renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["network_ports"][0]["service_name"] == "ssh"
    assert sqlite_db.get_target_status(conn, tid)["overall_status"] == "down"

    changed = client.patch(f"/targets/{tid}", json={"host": "new.example.com", "tcp_host": "new.example.com"})
    assert changed.status_code == 200, changed.text
    result = changed.json()
    assert (result["tcp_ports"], result["udp_ports"], result["enable_tcp_connect"], result["enable_udp_scan"]) == (
        "22",
        "53",
        True,
        True,
    )
    assert {(port["protocol"], port["port"]) for port in result["network_ports"]} == {("tcp", 22), ("udp", 53)}
    for port in result["network_ports"]:
        assert port["source"] == "manual"
        assert port["status"] == "unknown" and port["service_identity"] == "unknown"
        assert all(
            port[key] is None
            for key in (
                "service_name",
                "service_product",
                "service_version",
                "method",
                "confidence",
                "discovered_at",
                "checked_at",
                "last_up_at",
                "detail",
                "raw_state",
                "identity_changed_at",
                "latency_ms",
            )
        )
    assert sqlite_db.get_target_status(conn, tid) is None
    assert tsdb.query_latest(tmp_path, target_id=tid, metric="tcp_up_22") == history


@pytest.mark.parametrize(
    ("disabled", "kept", "flag"),
    [("tcp", "udp", "enable_tcp_connect"), ("udp", "tcp", "enable_udp_scan")],
)
def test_disabling_a_protocol_drops_its_port_rows_but_keeps_history(conn, tmp_path: Path, disabled, kept, flag):
    client = _app_client(conn, tmp_path)
    created = client.post(
        "/targets",
        json={
            "name": f"off-{disabled}",
            "host": "off.example.com",
            "enable_tcp_connect": True,
            "tcp_host": "off.example.com",
            "tcp_ports": "22",
            "enable_udp_scan": True,
            "udp_ports": "53",
        },
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    sqlite_db.sync_network_ports(
        conn,
        tid,
        tcp_ports=[22],
        udp_ports=[53],
        scan_result=_scan(
            _port("tcp", 22, name="ssh", method="probed", confidence=10),
            _port("udp", 53, name="domain", method="probed", confidence=8),
        ),
    )
    sqlite_db.update_network_port_status(conn, tid, "tcp", 22, "up", raw_state="open")
    sqlite_db.update_network_port_status(conn, tid, "udp", 53, "up", raw_state="open")
    conn.commit()
    metrics = {"tcp": "tcp_up_22", "udp": "udp_up_53"}
    for metric in metrics.values():
        tsdb.append_point(tmp_path, target_id=tid, metric=metric, value=1, retention_days=30, at=utcnow())
    history = {proto: tsdb.query_latest(tmp_path, target_id=tid, metric=m) for proto, m in metrics.items()}
    assert all(history.values())
    before_kept = {
        (p["protocol"], p["port"]): p for p in sqlite_db.list_network_ports(conn, tid) if p["protocol"] == kept
    }

    off = client.patch(f"/targets/{tid}", json={flag: False})
    assert off.status_code == 200, off.text
    rows = sqlite_db.list_network_ports(conn, tid)
    assert {p["protocol"] for p in rows} == {kept}
    assert {(p["protocol"], p["port"]): p for p in rows} == before_kept
    assert {p["protocol"] for p in off.json()["network_ports"]} == {kept}
    for proto, metric in metrics.items():
        assert tsdb.query_latest(tmp_path, target_id=tid, metric=metric) == history[proto]

    # Re-enabling starts clean: configured ports return as manual rows without the old observations.
    on = client.patch(f"/targets/{tid}", json={flag: True})
    assert on.status_code == 200, on.text
    back = [p for p in on.json()["network_ports"] if p["protocol"] == disabled]
    assert [(p["port"], p["source"], p["service_name"]) for p in back] == [
        (22 if disabled == "tcp" else 53, "manual", None)
    ]
    assert back[0]["status"] in (None, "unknown") and back[0]["checked_at"] is None
    for proto, metric in metrics.items():
        assert tsdb.query_latest(tmp_path, target_id=tid, metric=metric) == history[proto]


def _observed_target(conn, tmp_path: Path, name: str):
    """A target with a separate tcp_host and one identified TCP and one identified UDP port."""
    client = _app_client(conn, tmp_path)
    created = client.post(
        "/targets",
        json={
            "name": name,
            "host": "old.example.com",
            "enable_tcp_connect": True,
            "tcp_host": "tcp.example.com",
            "tcp_ports": "22",
            "enable_udp_scan": True,
            "udp_ports": "53",
        },
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    scan = _scan(
        _port("tcp", 22, name="ssh", method="probed", confidence=10),
        _port("udp", 53, name="domain", method="probed", confidence=8),
    )
    sqlite_db.sync_network_ports(conn, tid, tcp_ports=[22], udp_ports=[53], scan_result=scan)
    conn.execute(
        "INSERT INTO target_status(target_id, overall_status, updated_at) VALUES (?, 'down', ?)",
        (tid, utcnow()),
    )
    conn.commit()
    return client, tid


def _names(response) -> dict[str, str | None]:
    return {port["protocol"]: port["service_name"] for port in response.json()["network_ports"]}


def test_host_change_resets_only_udp_observations_when_tcp_has_its_own_host(conn, tmp_path: Path):
    client, tid = _observed_target(conn, tmp_path, "host-only")
    changed = client.patch(f"/targets/{tid}", json={"host": "new.example.com"})
    assert changed.status_code == 200, changed.text
    assert _names(changed) == {"tcp": "ssh", "udp": None}
    assert sqlite_db.get_target_status(conn, tid) is None


def test_tcp_host_change_resets_only_tcp_observations(conn, tmp_path: Path):
    client, tid = _observed_target(conn, tmp_path, "tcp-host-only")
    changed = client.patch(f"/targets/{tid}", json={"tcp_host": "other.example.com"})
    assert changed.status_code == 200, changed.text
    assert _names(changed) == {"tcp": None, "udp": "domain"}
    assert sqlite_db.get_target_status(conn, tid)["overall_status"] == "down"


def _stub_job(monkeypatch, *, host: str, result: NmapScanResult | None, owner_ok: bool = True):
    job = SimpleNamespace(host=host, result=result)
    monkeypatch.setattr(
        targets, "completed_job", lambda scan_id, _owner: job if owner_ok and scan_id == "scan-1" else None
    )


def test_forged_network_ports_in_the_request_are_ignored(conn, tmp_path: Path):
    client = _app_client(conn, tmp_path)
    body = {
        "name": "forge",
        "host": "forge.example.com",
        "enable_tcp_connect": True,
        "tcp_host": "forge.example.com",
        "tcp_ports": "22",
        "network_ports": [
            {
                "protocol": "tcp",
                "port": 22,
                "source": "discovered",
                "service_name": "evil",
                "method": "probed",
                "confidence": 10,
            }
        ],
        "service_name": "evil",
    }
    resp = client.post("/targets", json=body)
    assert resp.status_code == 201, resp.text
    (port,) = resp.json()["network_ports"]
    assert (port["source"], port["service_name"], port["method"]) == ("manual", None, None)
    patched = client.patch(
        f"/targets/{resp.json()['id']}",
        json={"network_ports": [{"protocol": "tcp", "port": 22, "service_name": "evil"}]},
    )
    assert patched.status_code == 200
    assert patched.json()["network_ports"][0]["service_name"] is None


def test_unknown_foreign_or_mismatching_scan_ids_are_rejected(conn, tmp_path: Path, monkeypatch):
    client = _app_client(conn, tmp_path)
    base = {
        "name": "s",
        "host": "scan.example.com",
        "enable_tcp_connect": True,
        "tcp_host": "scan.example.com",
        "tcp_ports": "22",
    }
    result = _scan(_port("tcp", 22, name="ssh", method="probed", confidence=10))
    _stub_job(monkeypatch, host="scan.example.com", result=result, owner_ok=False)
    assert client.post("/targets", json=dict(base, network_scan_id="scan-1")).status_code == 422  # not the caller's job
    _stub_job(monkeypatch, host="other.example.com", result=result)
    assert client.post("/targets", json=dict(base, network_scan_id="scan-1")).status_code == 422  # other host
    assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 0


def test_authorized_scan_metadata_is_persisted_from_the_job(conn, tmp_path: Path, monkeypatch):
    client = _app_client(conn, tmp_path)
    _stub_job(
        monkeypatch,
        host="scan.example.com",
        result=_scan(
            _port("tcp", 22, name="ssh", product="OpenSSH", method="probed", confidence=10),
            _port("udp", 53, name="domain", method="probed", confidence=8),
        ),
    )
    body = {
        "name": "s",
        "host": "scan.example.com",
        "enable_tcp_connect": True,
        "tcp_host": "scan.example.com",
        "tcp_ports": "22",
        "enable_udp_scan": True,
        "udp_ports": "53",
        "network_scan_id": "scan-1",
    }
    resp = client.post("/targets", json=body)
    assert resp.status_code == 201, resp.text
    ports = {(p["protocol"], p["port"]): p for p in resp.json()["network_ports"]}
    assert ports[("tcp", 22)]["service_name"] == "ssh" and ports[("tcp", 22)]["source"] == "discovered"
    assert ports[("udp", 53)]["service_name"] == "domain" and ports[("udp", 53)]["confidence"] == 8


def _split_host_body() -> dict:
    return {
        "name": "split",
        "host": "udp.example.com",
        "enable_tcp_connect": True,
        "tcp_host": "tcp.example.com",
        "tcp_ports": "22",
        "enable_udp_scan": True,
        "udp_ports": "53",
        "network_scan_id": "scan-1",
    }


def test_scan_of_host_does_not_become_tcp_baseline_when_tcp_host_differs(conn, tmp_path: Path, monkeypatch):
    client = _app_client(conn, tmp_path)
    scan = _scan(
        _port("tcp", 22, name="ssh", method="probed", confidence=10),
        _port("udp", 53, name="domain", method="probed", confidence=8),
    )
    _stub_job(monkeypatch, host="udp.example.com", result=scan)
    resp = client.post("/targets", json=_split_host_body())
    assert resp.status_code == 201, resp.text
    ports = {(p["protocol"], p["port"]): p for p in resp.json()["network_ports"]}
    assert ports[("tcp", 22)]["service_name"] is None and ports[("tcp", 22)]["source"] == "manual"
    assert ports[("udp", 53)]["service_name"] == "domain"


def test_scan_of_tcp_host_is_accepted_and_sets_only_tcp(conn, tmp_path: Path, monkeypatch):
    client = _app_client(conn, tmp_path)
    scan = _scan(
        _port("tcp", 22, name="ssh", method="probed", confidence=10),
        _port("udp", 53, name="domain", method="probed", confidence=8),
    )
    _stub_job(monkeypatch, host="TCP.example.com.", result=scan)
    resp = client.post("/targets", json=_split_host_body())
    assert resp.status_code == 201, resp.text
    ports = {(p["protocol"], p["port"]): p for p in resp.json()["network_ports"]}
    assert ports[("tcp", 22)]["service_name"] == "ssh"
    assert ports[("udp", 53)]["service_name"] is None


def test_scan_of_unrelated_host_is_rejected_for_split_hosts(conn, tmp_path: Path, monkeypatch):
    client = _app_client(conn, tmp_path)
    _stub_job(monkeypatch, host="other.example.com", result=_scan())
    assert client.post("/targets", json=_split_host_body()).status_code == 422


def test_host_spelling_change_keeps_observations_and_status(conn, tmp_path: Path):
    client, tid = _observed_target(conn, tmp_path, "host-spelling")
    changed = client.patch(f"/targets/{tid}", json={"host": " OLD.example.com. "})
    assert changed.status_code == 200, changed.text
    assert _names(changed) == {"tcp": "ssh", "udp": "domain"}
    assert sqlite_db.get_target_status(conn, tid)["overall_status"] == "down"
