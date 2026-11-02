#!/usr/bin/env python3
#
# tests/test_spooler_process_restart.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Real process restart of the spooler against a local dummy SMTP server (nothing leaves localhost).

Process A queues three mails while the server is down and is killed with SIGKILL; process B starts
fresh on the same database file after the server came up and must deliver each mail exactly once.
Not covered: the full application (uvicorn) and a Docker container restart.
"""

from __future__ import annotations

import re
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from email.message import EmailMessage
    sys.path.insert(0, sys.argv[1])
    from app.db import sqlite as sdb
    from app.services.notifications import delivery_policy as policy
    from app.services.notifications import spooler as sm
    from app.services.notifications.smtp_transport import SMTPConnection, send_message

    db, port, enqueue, seconds = Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1", float(sys.argv[5])
    policy.BASE_DELAY_SECONDS = 1  # shortened so the test does not wait for real backoff
    policy.MAX_DELAY_SECONDS = 1
    policy.CHANNEL_POLICIES["email"] = policy.ChannelPolicy(min_interval_seconds=0.3, batch_limit=4)
    conn = sdb.connect(db)
    sdb.init_schema(conn)
    sdb.close_connection(conn)
    spooler = sm.NotificationSpooler(lambda: sdb.connect(db))
    if enqueue:
        for i in range(3):
            body = {"subject": f"m{i}", "text_body": "t"}
            spooler.enqueue("alert", "email", 1, "R", "r@example.com", body, immediate=True)
    cfg = SMTPConnection(host="127.0.0.1", port=port, user=None, password=None, use_tls=False)

    def send_email(to, subject, text, html, message_id_key=None):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "a@example.com", to, subject
        msg["Message-ID"] = f"<{message_id_key}@example.com>"
        msg.set_content(text or "t")
        send_message(msg, cfg, timeout=3)
        return True

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        spooler.process_queue_sync(None, send_email)
        time.sleep(0.2)
    """
)


class DummySmtp:
    def __init__(self, port: int) -> None:
        self.messages: list[str] = []
        self.times: list[float] = []
        self._sock = socket.create_server(("127.0.0.1", port))
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket) -> None:
        with client, client.makefile("rwb") as io:
            io.write(b"220 dummy\r\n")
            io.flush()
            data: list[bytes] = []
            in_data = False
            for raw in io:
                if in_data:
                    if raw == b".\r\n":
                        self.messages.append(b"".join(data).decode())
                        self.times.append(time.monotonic())
                        in_data = False
                        io.write(b"250 queued\r\n")
                    else:
                        data.append(raw)
                    io.flush()
                    continue
                cmd = raw[:4].upper()
                if cmd == b"DATA":
                    in_data, data = True, []
                    io.write(b"354 go\r\n")
                elif cmd == b"QUIT":
                    io.write(b"221 bye\r\n")
                    io.flush()
                    return
                else:
                    io.write(b"250 ok\r\n")
                io.flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(script: Path, db: Path, port: int, *, enqueue: bool, seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(script), str(ROOT), str(db), str(port), "1" if enqueue else "0", str(seconds)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _all_sent(db: Path) -> bool:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        rows = conn.execute("SELECT status FROM notification_queue").fetchall()
    return len(rows) == 3 and all(r[0] == "sent" for r in rows)


def test_killed_process_is_replaced_and_every_mail_arrives_exactly_once(tmp_path: Path):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    db = tmp_path / "queue.db"
    port = _free_port()

    first = _spawn(script, db, port, enqueue=True, seconds=30)  # server down: connection refused
    time.sleep(2.5)
    assert first.poll() is None, "first process ended early"
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=5)

    server = DummySmtp(port)
    try:
        second = _spawn(script, db, port, enqueue=False, seconds=120)
        try:
            # Poll with a generous deadline: start-up and retry back-off stretch under CPU load.
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and not _all_sent(db):
                assert second.poll() is None, "second process ended early"
                time.sleep(0.25)
            time.sleep(1.5)  # settle window: a duplicate delivery would show up here
        finally:
            second.terminate()
            _, err = second.communicate(timeout=30)
    finally:
        server.close()
    assert b"Traceback" not in err, err.decode()[-500:]

    ids = [re.search(r"^Message-ID: <(.+?)>", m, re.MULTILINE | re.IGNORECASE).group(1) for m in server.messages]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # no duplicate delivery after the restart
    gaps = [b - a for a, b in zip(server.times, server.times[1:], strict=False)]
    assert min(gaps) >= 0.25  # the shortened pacing gap holds between sends

    rows = sqlite3.connect(db).execute("SELECT status, retry_count FROM notification_queue").fetchall()
    assert [r[0] for r in rows] == ["sent"] * 3
    assert all(r[1] >= 1 for r in rows)  # they failed while the server was down, then recovered
