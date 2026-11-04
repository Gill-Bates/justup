#!/usr/bin/env python3
#
# tests/test_dashboard_layout.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-user dashboard layout: schema, storage, API contract (auth, CSRF, isolation, validation)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.dashboard_layout import DASHBOARD_WIDGETS
from app.db import dashboard_layouts as layouts
from app.db import sqlite as sqlite_db
from app.models.users import hash_token, new_token
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect

ROOT = Path(__file__).resolve().parent.parent
URL = "/me/dashboard-layout"
_STATUS = {"id": "status-online", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True, "height_mode": "auto"}
_STATUS_WIDGETS = [
    _STATUS,
    {"id": "status-offline", "x": 3, "y": 0, "w": 3, "h": 2, "visible": True, "height_mode": "auto"},
    {"id": "status-disabled", "x": 6, "y": 0, "w": 3, "h": 2, "visible": True, "height_mode": "auto"},
    {"id": "status-total", "x": 9, "y": 0, "w": 3, "h": 2, "visible": True, "height_mode": "auto"},
]
_QUALITY = {"id": "quality", "x": 0, "y": 2, "w": 3, "h": 4, "visible": False, "height_mode": "auto"}
_DOWNTIME = {"id": "downtime", "x": 3, "y": 2, "w": 6, "h": 4, "visible": True, "height_mode": "manual"}
_VALID = {"version": 2, "widgets": [*_STATUS_WIDGETS, _QUALITY, _DOWNTIME]}


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


def _user(conn: sqlite3.Connection, name: str) -> tuple[int, str]:
    user_id = sqlite_db.create_user(conn, username=name, password_hash="stub-hash", is_admin=False)  # noqa: S106
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    return user_id, token


class _Api:
    """A TestClient session for one bearer token that echoes the rotating CSRF cookie."""

    def __init__(self, app, token: str | None) -> None:
        self.client = TestClient(app)
        self.client.get("/ui/login")  # sets the csrf_token cookie
        self.token = token

    def call(self, method: str, body=None, *, csrf: bool = True):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if csrf:
            headers["X-CSRF-Token"] = self.client.cookies.get("csrf_token")
        return self.client.request(method, URL, json=body, headers=headers)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-dashboard-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        app = create_app()
        connection = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(connection)
            alice_id, alice_token = _user(connection, "alice")
            bob_id, bob_token = _user(connection, "bob")
        finally:
            sqlite_db.close_connection(connection)
        yield {
            "app": app,
            "alice": _Api(app, alice_token),
            "bob": _Api(app, bob_token),
            "anon": _Api(app, None),
            "ids": (alice_id, bob_id),
        }


def test_schema_constraints_reject_out_of_range_rows(conn):
    user_id, _ = _user(conn, "u")
    insert = (
        "INSERT INTO dashboard_layouts (user_id, widget_id, position, x, y, w, h) VALUES (?, 'quality', 0, ?, ?, ?, ?)"
    )
    for bad in [(-1, 0, 1, 1), (10, 0, 3, 1), (0, 1000, 1, 1), (0, 0, 0, 1), (0, 0, 1, 101)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (user_id, *bad))
    conn.rollback()


def test_storage_roundtrip_replace_delete_and_cascade(conn):
    user_id, _ = _user(conn, "u")
    assert layouts.get_dashboard_layout(conn, user_id) is None
    layouts.set_dashboard_layout(conn, user_id, _VALID)
    assert layouts.get_dashboard_layout(conn, user_id) == _VALID
    layouts.set_dashboard_layout(conn, user_id, {"version": 2, "widgets": [_STATUS]})
    assert layouts.get_dashboard_layout(conn, user_id) == {"version": 2, "widgets": [_STATUS]}
    with pytest.raises(ValueError):
        layouts.set_dashboard_layout(conn, user_id, {"version": 1, "widgets": [_STATUS]})
    assert layouts.get_dashboard_layout(conn, user_id) == {"version": 2, "widgets": [_STATUS]}
    with pytest.raises(ValueError):
        layouts.set_dashboard_layout(conn, user_id, {"version": 2, "widgets": [_STATUS, _STATUS]})
    assert layouts.get_dashboard_layout(conn, user_id)["widgets"] == [_STATUS]
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    assert layouts.get_dashboard_layout(conn, user_id) is None


def test_api_requires_authentication(env):
    anon = env["anon"]
    assert anon.call("GET").status_code == 401
    assert anon.call("PUT", _VALID).status_code == 401
    assert anon.call("DELETE").status_code == 401


def test_mutations_require_csrf_token(env):
    alice = env["alice"]
    assert alice.call("PUT", _VALID, csrf=False).status_code == 403
    assert alice.call("DELETE", csrf=False).status_code == 403
    assert alice.call("GET").json() == {"layout": None}


def test_save_load_reset_cycle(env):
    alice = env["alice"]
    assert alice.call("GET").json() == {"layout": None}
    put = alice.call("PUT", _VALID)
    assert put.status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}
    assert alice.call("DELETE").json() == {"layout": None}
    assert alice.call("GET").json() == {"layout": None}
    assert alice.call("DELETE").status_code == 200  # idempotent


def test_v1_layout_is_rejected_without_changing_the_saved_v2_layout(env):
    alice = env["alice"]
    assert alice.call("PUT", _VALID).status_code == 200
    response = alice.call("PUT", {**_VALID, "version": 1})
    assert response.status_code == 422
    assert alice.call("GET").json() == {"layout": _VALID}


def test_layouts_are_isolated_per_user(env):
    alice, bob = env["alice"], env["bob"]
    assert alice.call("PUT", _VALID).status_code == 200
    assert bob.call("GET").json() == {"layout": None}
    other = {"version": 2, "widgets": [{**_STATUS, "visible": False}]}
    assert bob.call("PUT", other).status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}
    assert bob.call("DELETE").status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}


def test_admin_widget_is_rejected_and_filtered_after_role_change(env):
    alice = env["alice"]
    queue = {**_STATUS, "id": "delivery-queue", "h": 3}
    assert alice.call("PUT", {"version": 2, "widgets": [queue]}).status_code == 403
    connection = sqlite_db.connect(env["app"].state.db_path)
    try:
        layouts.set_dashboard_layout(connection, env["ids"][0], {"version": 2, "widgets": [_STATUS, queue]})
    finally:
        sqlite_db.close_connection(connection)
    assert alice.call("GET").json() == {"layout": {"version": 2, "widgets": [_STATUS]}}


@pytest.mark.parametrize(
    "layout",
    [
        {"version": 2, "widgets": [{**_STATUS, "id": "nope"}]},
        {"version": 2, "widgets": [_STATUS, _STATUS]},
        {"version": 2, "widgets": [{**_STATUS, "x": 10}]},  # x + w > 12
        {"version": 2, "widgets": [{**_STATUS, "y": 999, "h": 2}]},  # y + h > 1000
        {"version": 2, "widgets": [{**_STATUS, "x": -1}]},
        {"version": 2, "widgets": [{**_STATUS, "w": 0}]},
        {"version": 2, "widgets": [{**_STATUS, "w": 1}]},
        {"version": 2, "widgets": [{**_STATUS, "h": 1}]},
        {"version": 2, "widgets": [{**_STATUS, "height_mode": "invalid"}]},
        {"version": 2, "widgets": [{key: value for key, value in _STATUS.items() if key != "height_mode"}]},
        {"version": 2, "widgets": [{**_STATUS, "h": 101}]},
        {"version": 2, "widgets": [{**_STATUS, "w": "12"}]},
        {"version": 2, "widgets": [{**_STATUS, "html": "<b>x</b>"}]},
        {"version": 2, "widgets": [_STATUS] * 14},
        {"version": 2, "widgets": [_STATUS], "extra": 1},
        {"version": 3, "widgets": [_STATUS]},
        {"widgets": []},
        [],
    ],
)
def test_invalid_layouts_are_rejected_and_nothing_is_stored(env, layout):
    alice = env["alice"]
    assert alice.call("PUT", _VALID).status_code == 200
    assert alice.call("PUT", layout).status_code == 422
    assert alice.call("GET").json() == {"layout": _VALID}


def test_widget_allowlist_matches_client_registry_and_template():
    registry = (ROOT / "app/static/js/dashboard-widgets.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    status_fragment = (ROOT / "app/templates/fragments/status_summary.html").read_text(encoding="utf-8")
    registry_widget_ids = re.findall(r"def\(['\"]([a-z-]+)['\"]", registry)
    assert len(registry_widget_ids) == len(set(registry_widget_ids)), "duplicate widget IDs in client registry"
    assert set(registry_widget_ids) == DASHBOARD_WIDGETS
    template_widget_ids = set(re.findall(r'data-widget-id="([a-z-]+)"', template))
    assert template_widget_ids == DASHBOARD_WIDGETS
    assert sum(widget_id.startswith("status-") for widget_id in template_widget_ids) == 4
    hero_ids = {f"dashboard-{widget_id}" for widget_id in template_widget_ids if widget_id.startswith("status-")}
    assert set(re.findall(r'id="(dashboard-status-[a-z]+)"', template)) == hero_ids
    assert set(re.findall(r'id="(dashboard-status-[a-z]+)"', status_fragment)) == hero_ids
    assert status_fragment.count('hx-swap-oob="outerHTML"') == 4
    assert len(re.findall(r'data-widget-id="', template)) == len(DASHBOARD_WIDGETS)


def test_widget_sizes_agree_between_template_registry_and_server():
    """Registry and server table must carry identical minimums; the template carries defaults only."""
    registry = (ROOT / "app/static/js/dashboard-widgets.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    definitions = {
        m.group(1): tuple(int(v) for v in m.groups()[1:])
        for m in re.finditer(r"def\('([a-z-]+)', '[^']+', (\d+), (\d+), (\d+), (\d+), (\d+), (\d+)", registry)
    }
    assert set(definitions) == set(layouts.DASHBOARD_WIDGET_MIN_SIZES) == DASHBOARD_WIDGETS
    for wid, (x, y, w, h, min_w, min_h) in definitions.items():
        assert layouts.DASHBOARD_WIDGET_MIN_SIZES[wid] == (min_w, min_h), f"{wid}: registry and server minimum differ"
        tile = re.search(rf'<div class="grid-stack-item[^"]*"[^>]*gs-id="{wid}"[^>]*>', template)
        assert tile is not None, wid
        assert "gs-min-" not in tile.group(), f"{wid}: the template must not carry its own minimum"
        for key, value in {"x": x, "y": y, "w": w, "h": h}.items():
            assert f'gs-{key}="{value}"' in tile.group(), f"{wid}: template default {key} differs from the registry"


def test_uptime_summary_excludes_unknown_time_and_disabled_targets(env, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.api import dashboard_layout as api

    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    monkeypatch.setattr(api, "utcnow", lambda: now)
    monkeypatch.setattr(
        api.sqlite_db,
        "list_targets",
        lambda _conn: [
            {"id": 1, "is_enabled": True, "enable_http_check": True, "interval_seconds": 60},
            {"id": 2, "is_enabled": False, "enable_http_check": True, "interval_seconds": 60},
        ],
    )
    calls = []

    def availability(_directory, **kwargs):
        calls.append(kwargs)
        return {
            "HTTP": [
                {"ts": (now - timedelta(hours=24)).isoformat(), "up": 1},
                {"ts": (now - timedelta(hours=12)).isoformat(), "up": None},
            ]
        }

    monkeypatch.setattr(api.tsdb_db, "query_availability_series", availability)
    alice = env["alice"]
    response = alice.client.get(URL + "/uptime", headers={"Authorization": f"Bearer {alice.token}"})
    assert response.status_code == 200
    # A sample vouches only for stale_after (3 intervals = 180 s), not for the 12 h until the next one.
    body = response.json()
    assert body["uptime_percent"] == 100 and body["coverage_percent"] == pytest.approx(180 / 86400 * 100)
    assert len(calls) == 1
    assert calls[0]["target_id"] == 1
    assert calls[0]["enabled_services"] == {"HTTP"}


def test_uptime_summary_without_measurements_is_unknown(env, monkeypatch):
    from app.api import dashboard_layout as api

    monkeypatch.setattr(api.sqlite_db, "list_targets", lambda _conn: [])
    alice = env["alice"]
    response = alice.client.get(URL + "/uptime", headers={"Authorization": f"Bearer {alice.token}"})
    assert response.status_code == 200
    assert response.json() == {"uptime_percent": None, "coverage_percent": 0}


def _avail(points, *, stale_after=100.0, minutes=10):
    from datetime import UTC, datetime, timedelta

    from app.api.dashboard_layout import _availability_seconds

    until = datetime(2026, 1, 1, 12, minutes, tzinfo=UTC)
    since = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return _availability_seconds(
        [{"ts": (since + timedelta(seconds=s)).isoformat(), "up": up} for s, up in points], since, until, stale_after
    )


def test_availability_gap_longer_than_stale_after_is_unknown_not_carried_forward():
    # up at 0s, next sample only at 400s: only stale_after (100s) of the gap is known
    assert _avail([(0, 1), (400, 0)]) == (100.0, 100.0 + 100.0)


def test_availability_transition_within_stale_after_uses_the_next_timestamp():
    assert _avail([(0, 1), (60, 0), (120, 1)], minutes=3) == (60.0 + 60.0, 60.0 + 60.0 + 60.0)


def test_availability_invalid_state_and_window_bounds():
    assert _avail([(0, 2), (30, 1)], stale_after=1000.0, minutes=1) == (30.0, 30.0)
    assert _avail([(0, 1)], stale_after=1000.0, minutes=1) == (60.0, 60.0)
    assert _avail([], minutes=1) == (0.0, 0.0)
