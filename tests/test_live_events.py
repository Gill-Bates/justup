#!/usr/bin/env python3
#
# tests/test_live_events.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Event bus and the authenticated SSE channel: fan-out, audiences, session revocation, overflow, shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import events as events_api
from app.db import notifications as inbox
from app.db import sqlite as sqlite_db
from app.models.users import hash_password, hash_token, new_token
from app.services import event_bus
from app.services.event_bus import EventBus, LiveEvent
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _run(coro):
    return asyncio.run(coro)


async def _drain(sub, count: int, timeout: float = 1.0) -> list[LiveEvent]:
    out = []
    for _ in range(count):
        item = await sub.next(timeout)
        if item is not None:
            out.append(item)
    return out


def test_event_reaches_every_subscriber() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        subs = [bus.subscribe(i, False) for i in range(3)]
        bus.publish(LiveEvent("target.status", {"target_id": 7}))
        return [await _drain(s, 1) for s in subs]

    for got in _run(scenario()):
        assert [e.data for e in got] == [{"target_id": 7}]


def test_user_and_admin_events_reach_only_authorised_clients() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        alice, bob, boss = bus.subscribe(1, False), bus.subscribe(2, False), bus.subscribe(3, True)
        bus.publish(LiveEvent("notification.changed", {}, user_ids=frozenset({1})))
        bus.publish(LiveEvent("notification.changed", {}, admin_only=True))
        bus.publish(LiveEvent("notification.changed", {}, user_ids=frozenset({2}), admins_too=True))
        await asyncio.sleep(0.05)
        return [await _drain(s, 3, 0.05) for s in (alice, bob, boss)]

    alice, bob, boss = _run(scenario())
    assert len(alice) == 1
    assert len(bob) == 1
    assert len(boss) == 2  # admin-only plus the admins_too event, never the user-1 event


def test_full_buffer_collapses_into_one_resync_and_never_blocks_the_publisher() -> None:
    async def scenario():
        bus = EventBus(queue_size=4)
        bus.start()
        slow = bus.subscribe(1, False)
        for i in range(50):  # a client that never reads
            bus.publish(LiveEvent("target.measurement", {"target_id": i}))
        await asyncio.sleep(0.05)
        first = await slow.next(0.1)
        second = await slow.next(0.05)
        return first, second, len(slow._items)  # noqa: SLF001

    first, second, buffered = _run(scenario())
    assert first.type == "system.resync"
    assert second is None  # events after the overflow are not replayed
    assert buffered == 0


def test_backlog_overflow_resyncs_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_bus, "MAX_PENDING_PUBLISHES", 3)

    async def scenario():
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, False)
        for i in range(10):  # all queued before the loop runs a single delivery callback
            bus.publish(LiveEvent("target.status", {"target_id": i}))
        await asyncio.sleep(0.05)
        return await _drain(sub, 5, 0.05)

    types = [e.type for e in _run(scenario())]
    assert "system.resync" in types


def test_publish_from_another_thread_is_delivered() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, False)
        worker = threading.Thread(target=lambda: bus.publish(LiveEvent("alert.changed", {"alert_id": "a"})))
        worker.start()
        worker.join()
        return await _drain(sub, 1)

    assert _run(scenario())[0].type == "alert.changed"


def test_publish_without_running_bus_is_a_noop() -> None:
    event_bus.set_current_bus(None)
    event_bus.publish("target.status", {"target_id": 1})  # must not raise
    EventBus().publish(LiveEvent("target.status", {}))  # not started: dropped


def test_reconnect_gets_a_fresh_subscription_and_closed_bus_refuses_new_ones() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        old = bus.subscribe(1, False)
        old.close()
        new = bus.subscribe(1, False)
        bus.publish(LiveEvent("target.changed", {"target_id": 2}))
        got = await _drain(new, 1)
        await bus.close()
        return got, bus.subscribe(1, False), old.closed

    got, after_close, old_closed = _run(scenario())
    assert got[0].type == "target.changed"
    assert after_close is None
    assert old_closed


def test_bus_close_ends_open_streams() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, False)

        async def always_valid():
            return True

        async def consume():
            return [frame async for frame in events_api.stream_frames(sub, always_valid)]

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await bus.close()
        frames = await asyncio.wait_for(task, 1.0)
        return frames, bus.subscriber_count

    frames, remaining = _run(scenario())
    assert frames[0].startswith("retry: ")
    assert "system.heartbeat" in frames[0]
    assert remaining == 0


def test_stream_ends_with_auth_lost_when_the_session_stops_being_valid() -> None:
    async def scenario():
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, False)
        valid = {"v": True}

        async def check():
            return valid["v"]

        frames = []
        async for frame in events_api.stream_frames(sub, check, heartbeat_seconds=0.02, recheck_seconds=0.02):
            frames.append(frame)
            if len(frames) == 2:
                valid["v"] = False
        return frames

    frames = _run(scenario())
    assert frames[-1].startswith("event: system.auth-lost")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-live-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.delenv("SECRET_KEYS", raising=False)
        monkeypatch.setattr(crypto, "_primary_key", None)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        sqlite_db.init_schema(conn)
        tokens: dict[str, str] = {}
        for name, admin in (("boss", True), ("alice", False)):
            uid = sqlite_db.create_user(conn, username=name, password_hash=hash_password("Passw0rd!x1"), is_admin=admin)
            token, expires, max_expires = new_token()
            sqlite_db.issue_token(
                conn, user_id=uid, token_hash=hash_token(token), expires_at=expires, max_expires_at=max_expires
            )
            tokens[name] = token
        sqlite_db.close_connection(conn)
        with TestClient(app) as c:
            c.tokens = tokens  # type: ignore[attr-defined]
            c.db_path = app.state.db_path  # type: ignore[attr-defined]
            yield c
    event_bus.set_current_bus(None)


def test_stream_requires_authentication(client: TestClient) -> None:
    for url in ("/ui/events", "/ui/events/session"):
        resp = client.get(url)
        assert resp.status_code == 401
        assert "no-store" in resp.headers["cache-control"]
    assert client.get("/ui/events", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_stream_sends_streaming_headers_and_ends_when_the_session_is_revoked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events_api, "SESSION_RECHECK_SECONDS", 0.05)
    client.cookies.set("token", client.tokens["alice"])  # type: ignore[attr-defined]
    conn = sqlite_db.connect(client.db_path)  # type: ignore[attr-defined]
    sqlite_db.revoke_tokens_for_user(conn, sqlite_db.get_user_by_username(conn, "alice")["id"])
    sqlite_db.close_connection(conn)
    # Revoked before the request: refused. The open-stream case is covered through stream_frames above.
    assert client.get("/ui/events").status_code == 401

    client.cookies.set("token", client.tokens["boss"])  # type: ignore[attr-defined]
    monkeypatch.setattr(events_api, "revalidate", lambda *_a: False)
    resp = client.get("/ui/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    assert "no-store" in resp.headers["cache-control"]
    assert "event: system.heartbeat" in resp.text
    assert "event: system.auth-lost" in resp.text


def test_revocation_is_detected_on_an_open_stream_without_renewing_the_session(client: TestClient) -> None:
    db_path = client.db_path  # type: ignore[attr-defined]
    session = events_api.check_token(db_path, client.tokens["alice"])  # type: ignore[attr-defined]
    assert session is not None
    conn = sqlite_db.connect(db_path)
    before = conn.execute("SELECT expires_at FROM auth_tokens WHERE token_hash = ?", (session.token_hash,)).fetchone()[
        0
    ]
    assert events_api.revalidate(db_path, session)
    after = conn.execute("SELECT expires_at FROM auth_tokens WHERE token_hash = ?", (session.token_hash,)).fetchone()[0]
    assert before == after  # re-checking must not extend the inactivity window
    sqlite_db.revoke_tokens_for_user(conn, session.user_id)
    sqlite_db.close_connection(conn)
    assert not events_api.revalidate(db_path, session)


def test_disabled_user_and_role_change_end_the_stream(client: TestClient) -> None:
    db_path = client.db_path  # type: ignore[attr-defined]
    session = events_api.check_token(db_path, client.tokens["alice"])  # type: ignore[attr-defined]
    conn = sqlite_db.connect(db_path)
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (session.user_id,))
    conn.commit()
    assert not events_api.revalidate(db_path, session)  # role changed: new identity, new stream
    conn.execute("UPDATE users SET is_admin = 0, is_active = 0 WHERE id = ?", (session.user_id,))
    conn.commit()
    sqlite_db.close_connection(conn)
    assert events_api.check_token(db_path, client.tokens["alice"]) is None  # type: ignore[attr-defined]


def test_lifespan_exit_clears_the_bus(client: TestClient) -> None:
    assert client.app.state.event_bus.subscriber_count == 0  # type: ignore[attr-defined]
    assert event_bus._current is client.app.state.event_bus  # type: ignore[attr-defined]  # noqa: SLF001


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite_db.connect(tmp_path / "e.db")
    sqlite_db.init_schema(c)
    yield c
    sqlite_db.close_connection(c)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(event_bus, "publish", lambda *a, **k: calls.append((a, k)))
    return calls


def _emit(conn, key: str = "k1") -> int | None:
    return inbox.emit(
        conn,
        event_type="alert.opened",
        category="monitoring",
        severity="error",
        title="x",
        idempotency_key=key,
        audience="admins",
    )


def test_notification_event_is_published_only_after_commit(conn, published) -> None:
    assert _emit(conn) is not None
    assert len(published) == 1
    args, kwargs = published[0]
    assert args[0] == "notification.changed"
    assert kwargs["admin_only"] is True
    assert "title" not in json.dumps(args[1])  # ids only, never content


def test_rolled_back_transaction_publishes_nothing(conn, published) -> None:
    with pytest.raises(RuntimeError), sqlite_db.transaction(conn):
        _emit(conn, "rolled-back")
        assert published == []  # not before the commit
        raise RuntimeError("abort")
    assert published == []
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_user_targeted_notification_event_is_limited_to_the_recipient(conn, published) -> None:
    uid = sqlite_db.create_user(conn, username="u", password_hash="x", is_admin=False)  # noqa: S106
    inbox.emit(
        conn,
        event_type="report.completed",
        category="reports",
        severity="success",
        title="t",
        idempotency_key="user-ev",
        audience="user",
        recipient_user_id=uid,
    )
    assert published[0][1]["user_ids"] == [uid]


def test_grouped_delivery_notification_publishes_after_regrouping(conn, published) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    inbox.emit_delivery_sent(conn, "email", now=now)
    assert len(published) == 1  # first delivery inserts the row
    inbox.emit_delivery_sent(conn, "email", now=now)
    assert len(published) == 2  # regrouped row is visible activity again
    args, kwargs = published[1]
    assert args[0] == "notification.changed"
    assert kwargs["admin_only"] is True
    assert conn.execute("SELECT event_count FROM notifications").fetchone()[0] == 2


def test_pending_password_change_and_expired_token_deny_the_stream_like_the_api(client: TestClient) -> None:
    db_path = client.db_path  # type: ignore[attr-defined]
    token = client.tokens["alice"]  # type: ignore[attr-defined]
    session = events_api.check_token(db_path, token)
    assert session is not None
    conn = sqlite_db.connect(db_path)
    uid = session.user_id
    conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (uid,))
    conn.commit()
    assert events_api.check_token(db_path, token) is None  # a forced password change blocks new streams
    assert not events_api.revalidate(db_path, session)  # ... and ends open ones
    conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (uid,))
    conn.execute("UPDATE auth_tokens SET expires_at = '2000-01-01 00:00:00' WHERE user_id = ?", (uid,))
    conn.commit()
    sqlite_db.close_connection(conn)
    assert events_api.check_token(db_path, token) is None
    assert not events_api.revalidate(db_path, session)


def test_stream_ends_regularly_when_bus_closes_from_another_thread() -> None:
    async def scenario() -> list[str]:
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, True)
        assert sub is not None

        async def always_valid() -> bool:
            return True

        threading.Timer(0.05, bus.close_threadsafe).start()
        frames = [f async for f in events_api.stream_frames(sub, always_valid, heartbeat_seconds=5, recheck_seconds=5)]
        bus.close_threadsafe()  # idempotent
        await bus.close()  # the lifespan close tolerates the earlier one
        await bus.close()
        assert bus.subscriber_count == 0 and bus.subscribe(1, True) is None
        return frames

    frames = _run(scenario())
    assert frames and frames[0].startswith("retry:")  # ended by StopAsyncIteration, no exception


def test_server_handle_exit_closes_the_bus_before_uvicorn_waits() -> None:
    import signal
    from types import SimpleNamespace

    from app.__main__ import make_server_class

    async def scenario() -> tuple[bool, bool]:
        bus = EventBus()
        bus.start()
        sub = bus.subscribe(1, False)
        assert sub is not None
        import uvicorn

        app = SimpleNamespace(state=SimpleNamespace(event_bus=bus))
        server = make_server_class()(uvicorn.Config(app))
        server.handle_exit(signal.SIGTERM, None)
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.sleep(0.05)
        return server.should_exit, sub.closed

    assert _run(scenario()) == (True, True)


def test_real_uvicorn_stop_with_open_sse_stream_logs_no_error(caplog: pytest.LogCaptureFixture) -> None:
    import signal
    import socket
    import time

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from app.__main__ import make_server_class

    bus_holder: dict[str, EventBus] = {}

    async def stream(_request):
        async def valid() -> bool:
            return True

        sub = bus_holder["bus"].subscribe(1, True)
        return StreamingResponse(events_api.stream_frames(sub, valid), media_type="text/event-stream")

    async def lifespan(app):
        bus = EventBus()
        bus.start()
        bus_holder["bus"] = bus
        app.state.event_bus = bus
        yield
        await bus.close()

    app = Starlette(routes=[Route("/s", stream)], lifespan=lifespan)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", timeout_graceful_shutdown=5)
    server = make_server_class()(config)
    caplog.set_level(logging.ERROR, logger="uvicorn.error")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
        conn.sendall(b"GET /s HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200" in conn.recv(4096)
        started = time.monotonic()
        server.handle_exit(signal.SIGTERM, None)
        thread.join(timeout=4)
        elapsed = time.monotonic() - started
    assert not thread.is_alive() and elapsed < 3
    assert "Exception in ASGI application" not in caplog.text
