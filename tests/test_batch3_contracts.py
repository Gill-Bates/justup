#!/usr/bin/env python3
#
# tests/test_batch3_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: ARG001, DTZ001
"""Small contracts: geo dedup, POP tokens, e-mail timestamps, IATA CSV, POP status, stale PDF jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import sqlite as sqlite_db
from app.services import pop_codes_embedded
from app.services.notifications.email_template import _format_timestamp
from app.services.seed.iata_loader import _parse_iata_rows
from app.services.traceroute_geo import _deduplicate_geo_points
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _pt(hop: int, lat: float, **extra) -> dict:
    return {"hop": hop, "lat": lat, "lon": 1.0, **extra}


def _hops(points: list[dict]) -> list[int]:
    return [p["hop"] for p in points]


def test_dedup_merges_only_consecutive_locations() -> None:
    out = _deduplicate_geo_points([_pt(1, 10), _pt(2, 10), _pt(3, 20)])
    assert _hops(out) == [1, 3]
    assert out[0]["_hop_range"] == "1-2"


def test_dedup_keeps_revisited_location() -> None:
    out = _deduplicate_geo_points([_pt(1, 10), _pt(2, 20), _pt(3, 10), _pt(4, 30)])
    assert _hops(out) == [1, 2, 3, 4]


def test_dedup_repeated_destination_location_is_one_marker() -> None:
    out = _deduplicate_geo_points([_pt(1, 10), _pt(2, 20), _pt(3, 20, destination=True)])
    assert _hops(out) == [1, 3]
    assert out[-1]["destination"] is True
    assert out[-1]["_hop_range"] == "2-3"


def test_dedup_keeps_dashed_segment_and_does_not_mutate() -> None:
    src = [_pt(1, 10), _pt(2, 10, segment_to_next="dashed"), _pt(3, 20)]
    out = _deduplicate_geo_points(src)
    assert out[0]["segment_to_next"] == "dashed"
    assert "segment_to_next" not in src[0]


@pytest.mark.parametrize(
    "host", ["münchen.example", "xn--mnchen-3ya.example", "router-ünïcode1.example", "fra1.example"]
)
def test_embedded_lookup_survives_non_ascii_tokens(host) -> None:
    pop_codes_embedded.lookup_embedded(host)  # must not raise


def test_embedded_lookup_still_resolves_ascii_code() -> None:
    codes = pop_codes_embedded.get_all_codes()
    code = next(c for c in codes if isinstance(c, str) and len(c) == 3)
    res = pop_codes_embedded.lookup_embedded(f"{code}1.core.example.net")
    assert res is not None


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("2026-01-01T12:00:00+02:00", "2026-01-01 10:00:00 UTC"),
        ("2026-01-01T12:00:00Z", "2026-01-01 12:00:00 UTC"),
        ("2026-01-01T12:00:00", "2026-01-01 12:00:00 UTC"),
        (datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5))), "2026-01-01 17:00:00 UTC"),
        (datetime(2026, 1, 1, 12), "2026-01-01 12:00:00 UTC"),
        ("not-a-date", "not-a-date"),
        (None, "N/A"),
    ],
)
def test_format_timestamp_converts_to_utc(ts, expected) -> None:
    assert _format_timestamp(ts) == expected


def test_iata_rows_tolerate_missing_cells_and_bom(tmp_path: Path) -> None:
    csv_path = tmp_path / "iata.csv"
    csv_path.write_bytes(
        b"\xef\xbb\xbfiata,city,country_code,latitude,longitude\n"
        b"fra,Frankfurt,DE,50.03,8.55\n"
        b"xyz\n"  # short row: all other cells None
        b"ber,Berlin\n"
        b"ham,Hamburg,DE,53.6,10.0\n"
    )
    rows = _parse_iata_rows(csv_path)
    assert [r[0] for r in rows] == ["fra", "ham"]


@pytest.fixture
def conn(tmp_path: Path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


def test_pop_status_sources_sum_to_active(conn, tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import pop as pop_api
    from app.utils.deps import get_conn

    for i, src in enumerate(["seed", "peeringdb_fac", "peeringdb_ix", "iata", "iata"]):
        conn.execute(
            "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence,"
            " priority, created_at, updated_at) VALUES (?, ?, 'c', 'DE', 1, 1, ?, 50, 1, 'x', 'x')",
            (f"C{i}", f"A{i}", src),
        )
    conn.commit()
    app = FastAPI()
    app.state.db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    app.include_router(pop_api.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[pop_api.require_admin] = lambda: {"is_admin": True}
    body = TestClient(app).get("/api/admin/pop/status").json()
    assert body["sources"]["manual"] == 0  # known source stays listed
    assert body["sources"]["iata"] == 2
    assert sum(body["sources"].values()) == body["active_locations"]


def test_restart_marks_orphaned_pdf_jobs_failed(conn) -> None:
    tid = sqlite_db.create_target(
        conn, {"name": "T", "host": "t.example.com", "interval_seconds": 60, "retention_days": 30}
    )
    for jid, status in (("a", "pending"), ("b", "processing"), ("c", "completed")):
        sqlite_db.create_report_job(conn, jid, tid)
        sqlite_db.update_report_job(conn, jid, status=status)
    assert sqlite_db.mark_stale_report_jobs_failed(conn) == 2
    states = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM report_jobs")}
    assert states == {"a": "failed", "b": "failed", "c": "completed"}


def test_collect_resolved_uses_one_shared_budget() -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.services.mtr_run import _collect_resolved

    def slow(ip: str) -> str:
        time.sleep(2)
        return ip

    with ThreadPoolExecutor(max_workers=9) as ex:
        futures = {f"10.0.0.{i}": ex.submit(slow, f"10.0.0.{i}") for i in range(8)}
        futures["10.0.1.1"] = ex.submit(lambda: "fast.example")
        start = time.monotonic()
        out = _collect_resolved(futures, 0.3)
        elapsed = time.monotonic() - start
    assert elapsed < 1.0  # sequential waits would have taken 8 x budget
    assert out["10.0.1.1"] == "fast.example"
    assert out["10.0.0.0"] is None


def test_collect_resolved_empty_and_failing() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.services.mtr_run import _collect_resolved

    assert _collect_resolved({}, 0.1) == {}
    with ThreadPoolExecutor(max_workers=1) as ex:

        def boom() -> str:
            raise OSError("x")

        assert _collect_resolved({"1.1.1.1": ex.submit(boom)}, 0.5) == {"1.1.1.1": None}


def test_synthetic_destination_hop_does_not_claim_reached_target() -> None:
    from app.services.route_interpolator import ensure_destination_hop

    hops = [{"hop": 1, "ip": "10.0.0.1", "status": "ok", "latency_ms": {"avg": 5.0}, "loss_pct": 0.0}]
    out = ensure_destination_hop(hops, destination_ip="203.0.113.9", final_latency_ms=5.0)
    dest = out[-1]
    assert dest["_synthetic_destination"] is True
    assert dest["status"] == "partial"
    assert dest["latency_ms"] is None
    assert dest["loss_pct"] is None
    assert dest["_estimated_latency_ms"] == 5.0


def test_dedup_single_location_keeps_origin_and_destination() -> None:
    out = _deduplicate_geo_points([_pt(1, 10), _pt(2, 10), _pt(3, 10)])
    assert _hops(out) == [1, 3]
