#!/usr/bin/env python3
#
# tests/test_backup_restore.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Backup integrity (full HMAC-SHA256), archive validation limits and the restore endpoint."""

from __future__ import annotations

import io
import sqlite3
import tarfile
import threading
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.db import sqlite as sqlite_db
from app.models.users import hash_password, hash_token, new_token
from app.utils import backup as backup_mod
from app.utils import crypto, maintenance
from app.utils.backup import BackupError, parse_backup_filename, verify_backup_hmac
from app.utils.rate_limit import limiter
from tests import _support  # noqa: F401 - imported for its sys.path side effect

PASSWORD = "correct horse battery"  # noqa: S105 - throwaway test value


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("SECRET_KEYS", raising=False)
    monkeypatch.delenv("JUSTUP_BACKUP_HMAC_SECRET", raising=False)
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture(autouse=True)
def _reset_quiesce():
    """A successful restore leaves the database quiesced for the restart; never leak that into other tests."""
    sqlite_db.end_quiesce()
    maintenance.end_maintenance()
    limiter.reset()  # the heavy-endpoint rate limit is process-wide; many restore calls would hit it
    yield
    sqlite_db.end_quiesce()
    maintenance.end_maintenance()


def _tar(path: Path, entries: list[tuple[str, bytes | None]], *, extra=None) -> Path:
    """Write a bz2 tar. ``None`` content means a directory entry."""
    with tarfile.open(path, "w:bz2") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        if extra:
            tar.addfile(extra)
    return path


def _db_bytes() -> bytes:
    """A small but real SQLite database image."""
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.db"
        conn = __import__("sqlite3").connect(db)
        conn.execute("CREATE TABLE t(a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        return db.read_bytes()


DB_BYTES = _db_bytes()


def _good(path: Path, db: bytes = DB_BYTES) -> Path:
    return _tar(path, [("data", None), ("data/sqlite", None), ("data/sqlite/x.db", db), ("data/tsdb", None)])


class TestFilenameAndHmac:
    def test_full_digest_is_signed_and_verified(self, tmp_path: Path):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        assert len(digest) == 64
        assert verify_backup_hmac(archive, digest)
        assert not verify_backup_hmac(archive, "0" * 64)
        assert parse_backup_filename(f"justup_backup_20260101_000000_{digest}.tar.bz2") == digest

    def test_modified_archive_fails_verification(self, tmp_path: Path):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        archive.write_bytes(archive.read_bytes() + b"x")
        assert not verify_backup_hmac(archive, digest)

    def test_other_instance_key_does_not_verify(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        assert not verify_backup_hmac(archive, digest)

    def test_rotated_key_still_verifies_old_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        old = __import__("os").environ["SECRET_KEY"]
        monkeypatch.delenv("SECRET_KEY")
        monkeypatch.setenv("SECRET_KEYS", f"{Fernet.generate_key().decode('ascii')},{old}")
        assert verify_backup_hmac(archive, digest)

    def test_signing_key_is_not_stored_in_the_database(self):
        assert "backup_hmac_secret" not in Path(backup_mod.__file__).read_text().replace("backup_hmac_secret(", "")

    def test_legacy_format_is_rejected_with_clear_message(self):
        with pytest.raises(BackupError, match="legacy backup format"):
            parse_backup_filename("justup_backup_20260101_000000_0123456789abcdef.tar.bz2")
        with pytest.raises(BackupError, match="Invalid filename"):
            parse_backup_filename("evil.tar.bz2")


class TestArchiveValidation:
    def test_valid_archive_extracts(self, tmp_path: Path):
        extracted = backup_mod.extract_backup(_good(tmp_path / "a.tar.bz2"), tmp_path / "out")
        assert (extracted / "sqlite" / "x.db").read_bytes() == DB_BYTES

    def test_too_many_members(self, tmp_path: Path):
        archive = _tar(tmp_path / "a.tar.bz2", [("data", None)] + [(f"data/sqlite/{i}", b"") for i in range(5)])
        with pytest.raises(BackupError, match="Too many"):
            backup_mod.extract_backup(archive, tmp_path / "out", max_members=3)

    def test_unpacked_size_limit(self, tmp_path: Path):
        archive = _tar(tmp_path / "a.tar.bz2", [("data", None), ("data/sqlite/big", b"x" * 100)])
        with pytest.raises(BackupError, match="Unpacked size"):
            backup_mod.extract_backup(archive, tmp_path / "out", max_bytes=50)

    @pytest.mark.parametrize(
        ("kind", "name"),
        [
            (tarfile.SYMTYPE, "data/sqlite/link"),
            (tarfile.LNKTYPE, "data/sqlite/hard"),
            (tarfile.FIFOTYPE, "data/sqlite/f"),
        ],
    )
    def test_links_and_special_files_are_rejected(self, tmp_path: Path, kind, name):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd"
        archive = _tar(tmp_path / "a.tar.bz2", [("data", None)], extra=info)
        with pytest.raises(BackupError, match="Links or special"):
            backup_mod.extract_backup(archive, tmp_path / "out")

    @pytest.mark.parametrize("name", ["../evil", "data/../../evil", "/etc/passwd", "other/file", "data/unknown/x"])
    def test_traversal_and_unexpected_paths_are_rejected(self, tmp_path: Path, name):
        archive = _tar(tmp_path / "a.tar.bz2", [("data", None), (name, b"x")])
        with pytest.raises(BackupError):
            backup_mod.extract_backup(archive, tmp_path / "out")
        assert not (tmp_path / "evil").exists()

    def test_garbage_is_reported_as_corrupt(self, tmp_path: Path):
        bad = tmp_path / "bad.tar.bz2"
        bad.write_bytes(b"BZh9notreallybzip")
        with pytest.raises(BackupError, match="Extraction failed"):
            backup_mod.extract_backup(bad, tmp_path / "out")


class TestApplyRestore:
    def test_swap_replaces_directories_and_keeps_other_files(self, tmp_path: Path):
        data = tmp_path / "data"
        (data / "sqlite").mkdir(parents=True)
        (data / "sqlite" / "old.db").write_text("old")
        (data / "geoip").mkdir()
        (data / "geoip" / "GeoLite2-City.mmdb").write_text("geo")
        new = tmp_path / "new"
        (new / "sqlite").mkdir(parents=True)
        (new / "sqlite" / "new.db").write_text("new")
        assert backup_mod.apply_restore(new, data) == ["sqlite"]
        assert (data / "sqlite" / "new.db").read_text() == "new"
        assert not (data / "sqlite" / "old.db").exists()
        assert (data / "geoip" / "GeoLite2-City.mmdb").read_text() == "geo"
        assert not list(data.glob(".rollback_*"))

    def test_failure_rolls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        data = tmp_path / "data"
        (data / "sqlite").mkdir(parents=True)
        (data / "sqlite" / "old.db").write_text("old")
        (data / "tsdb").mkdir()
        new = tmp_path / "new"
        (new / "sqlite").mkdir(parents=True)
        (new / "tsdb").mkdir()
        real_move = backup_mod._rename  # noqa: SLF001
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 4:  # second directory, moving the restored tree into place
                raise OSError("disk full")
            return real_move(src, dst)

        monkeypatch.setattr(backup_mod, "_rename", flaky)
        with pytest.raises(backup_mod.RestoreFailedError) as exc:
            backup_mod.apply_restore(new, data)
        assert exc.value.rolled_back is True
        assert (data / "sqlite" / "old.db").read_text() == "old"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-restore-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        monkeypatch.setattr(settings_api.os, "kill", lambda *_: None)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(conn)
            uid = sqlite_db.create_user(conn, username="root", password_hash=hash_password(PASSWORD), is_admin=True)
            token, expires, max_expires = new_token()
            sqlite_db.issue_token(
                conn, user_id=uid, token_hash=hash_token(token), expires_at=expires, max_expires_at=max_expires
            )
        finally:
            sqlite_db.close_connection(conn)
        client = TestClient(app)
        client.get("/ui/login")
        yield client, token, Path(temp_dir)


def _post(env, name: str, payload: bytes, password: str = PASSWORD):
    client, token, _ = env
    return client.post(
        "/settings/restore",
        data={"password": password},
        files={"file": (name, payload, "application/x-bzip2")},
        headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")},
    )


def _named(archive: Path) -> tuple[str, bytes]:
    digest = backup_mod.compute_backup_hmac(archive)
    return f"justup_backup_20260101_000000_{digest}.tar.bz2", archive.read_bytes()


class TestRestoreEndpoint:
    def test_backup_then_restore_roundtrip(self, env):
        client, token, data_dir = env
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        resp = client.post("/settings/backup", headers=headers)
        assert resp.status_code == 200, resp.text
        name = resp.headers["content-disposition"].split("filename=")[1]
        assert backup_mod.BACKUP_NAME_RE.match(name)
        marker = data_dir / "tsdb" / "marker.txt"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("created after backup")
        restored = _post(env, name, resp.content)
        assert restored.status_code == 200, restored.text
        assert not marker.exists()

    def test_roundtrip_keeps_plain_and_compressed_series_identical(self, env):
        """A backup holds the legacy JSONL and the gzip segments; restore brings both back unchanged."""
        import io
        from datetime import timedelta

        from app.db import tsdb

        client, token, data_dir = env
        tsdb_dir = data_dir / "tsdb"
        now = tsdb.utcnow().replace(second=0, microsecond=0)
        for metric, days in (("ping_ms", 20), ("http_ms", 2)):
            for i in range(days * 24):
                at = now - timedelta(hours=days * 24 - i)
                tsdb.append_point(tsdb_dir, target_id=1, metric=metric, value=float(i), retention_days=365, at=at)
        compacted = []

        def stop_after_first_series() -> bool:
            return len(compacted) >= 1

        real = tsdb._compact_series  # noqa: SLF001
        tsdb._compact_series = lambda active, hot: compacted.append(active.name) or real(active, hot)  # noqa: SLF001
        try:
            tsdb.compact_target(tsdb_dir, 1, throttle=False, should_stop=stop_after_first_series)
        finally:
            tsdb._compact_series = real  # noqa: SLF001

        def snapshot():
            return {
                m: [(p.ts, p.value) for p in tsdb.query(tsdb_dir, target_id=1, metric=m, limit=10**6)]
                for m in ("ping_ms", "http_ms")
            }

        before = snapshot()
        files_before = sorted(p.name for p in (tsdb_dir / "target_1").iterdir() if not p.name.endswith(".lock"))
        assert any(n.endswith(".jsonl.gz") for n in files_before)  # compressed series ...
        assert "ping_ms.jsonl" in files_before and "http_ms.jsonl" in files_before  # ... next to plain ones
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        resp = client.post("/settings/backup", headers=headers)
        assert resp.status_code == 200, resp.text
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:bz2") as tar:
            archived = sorted(Path(n).name for n in tar.getnames() if Path(n).parent.name == "target_1")
        assert archived == files_before
        tsdb.delete_target(tsdb_dir, 1)
        assert snapshot() == {"ping_ms": [], "http_ms": []}
        name = resp.headers["content-disposition"].split("filename=")[1]
        assert _post(env, name, resp.content).status_code == 200
        assert snapshot() == before

    def test_wrong_password_is_rejected(self, env, tmp_path: Path):
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        assert _post(env, name, payload, password=PASSWORD + "-wrong").status_code == 401

    def test_tampered_archive_is_rejected(self, env, tmp_path: Path):
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        assert _post(env, name, payload + b"tamper").status_code == 400

    def test_legacy_filename_is_rejected_with_message(self, env, tmp_path: Path):
        archive = _good(tmp_path / "a.tar.bz2")
        resp = _post(env, "justup_backup_20260101_000000_0123456789abcdef.tar.bz2", archive.read_bytes())
        assert resp.status_code == 400
        assert "legacy backup format" in resp.json()["detail"]

    def test_signed_but_unsafe_archive_is_rejected(self, env, tmp_path: Path):
        info = tarfile.TarInfo("data/sqlite/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive = _tar(tmp_path / "evil.tar.bz2", [("data", None)], extra=info)
        name, payload = _named(archive)
        resp = _post(env, name, payload)
        assert resp.status_code == 400
        assert "Links or special" in resp.json()["detail"]

    def test_valid_signed_archive_restores(self, env, tmp_path: Path):
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        resp = _post(env, name, payload)
        assert resp.status_code == 200, resp.text
        assert (env[2] / "sqlite" / "x.db").read_bytes() == DB_BYTES


class TestHmacKeys:
    def test_all_keys_are_evaluated_in_one_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        old = __import__("os").environ["SECRET_KEY"]
        monkeypatch.delenv("SECRET_KEY")
        monkeypatch.setenv("SECRET_KEYS", f"{Fernet.generate_key().decode('ascii')},{old}")
        monkeypatch.setattr(crypto, "_primary_key", None)
        opens = []
        real_open = Path.open

        def counting_open(self, *args, **kwargs):
            if self == archive:
                opens.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)
        digests = backup_mod.compute_backup_hmacs(archive, backup_mod.backup_hmac_keys())
        assert len(set(digests)) == 2
        assert len(opens) == 1
        opens.clear()
        # the digest of the LAST key (old, rotated-out) still verifies, with a single read
        assert verify_backup_hmac(archive, digests[1])
        assert verify_backup_hmac(archive, digests[0])
        assert len(opens) == 2
        assert not verify_backup_hmac(archive, "f" * 64)
        assert not verify_backup_hmac(archive, "\u00e4" * 64)  # non-ASCII input is rejected, not an exception

    def test_weak_external_secret_is_rejected_without_echo(self, monkeypatch: pytest.MonkeyPatch):
        for weak in ("short", "a" * 40, "ab" * 20):
            monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", weak)
            with pytest.raises(backup_mod.BackupConfigError) as exc:
                backup_mod.backup_hmac_keys()
            assert weak not in str(exc.value)

    def test_strong_external_secret_signs_and_verifies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", "0123456789abcdef0123456789abcdef-x7Q")
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        assert verify_backup_hmac(archive, digest)
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", "fedcba9876543210fedcba9876543210-y8R")
        assert not verify_backup_hmac(archive, digest)  # changing the external secret does not rotate

    def test_primary_key_change_invalidates_issued_tokens(self, monkeypatch: pytest.MonkeyPatch):
        old = __import__("os").environ["SECRET_KEY"]
        token = "session-token"  # noqa: S105 - throwaway test value
        before = hash_token(token)
        monkeypatch.delenv("SECRET_KEY")
        monkeypatch.setenv("SECRET_KEYS", f"{Fernet.generate_key().decode('ascii')},{old}")
        monkeypatch.setattr(crypto, "_primary_key", None)
        # Tokens are hashed with the PRIMARY key only: moving the old key to the end of SECRET_KEYS
        # keeps encrypted data and backups readable but signs every existing session out.
        assert hash_token(token) != before


class TestSnapshotAndIntegrity:
    def test_snapshot_is_consistent_during_active_writes(self, tmp_path: Path):
        import sqlite3
        import threading

        src = tmp_path / "sqlite"
        src.mkdir()
        db = src / "justup.db"
        conn = sqlite_db.connect(db)
        conn.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b INTEGER NOT NULL)")
        conn.commit()
        conn.close()
        stop = threading.Event()
        errors: list[Exception] = []

        def writer():
            wconn = sqlite_db.connect(db)
            try:
                while not stop.is_set():
                    with sqlite_db.transaction(wconn):  # a and b always change together
                        wconn.execute("INSERT INTO ledger(a, b) VALUES (1, 1)")
                        wconn.execute("UPDATE ledger SET a = a + 1, b = b + 1")
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
            finally:
                wconn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for index in range(5):
                backup_mod.snapshot_sqlite_dir(src, tmp_path / f"snap{index}")
        finally:
            stop.set()
            thread.join()
        assert not errors
        for index in range(5):
            backup_mod.check_sqlite_integrity(tmp_path / f"snap{index}" / "justup.db", full=True)
            snap = sqlite3.connect(tmp_path / f"snap{index}" / "justup.db")
            assert snap.execute("SELECT COUNT(*) FROM ledger WHERE a != b").fetchone()[0] == 0
            snap.close()
        assert not list((tmp_path / "snap0").glob("*-wal"))

    def test_snapshot_pdf_reports_skips_tmp_and_tolerates_concurrent_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        src = tmp_path / "pdf_reports"
        src.mkdir()
        (src / "report.pdf").write_bytes(b"%PDF-1.4 real")
        (src / "inflight.pdf.tmp").write_bytes(b"partial")
        (src / "racer.pdf").write_bytes(b"gone soon")

        real_copy2 = backup_mod.shutil.copy2

        def flaky_copy2(src_path, dst_path, *args, **kwargs):
            # Simulate the cleanup loop deleting one file between listing and copy.
            if Path(src_path).name == "racer.pdf":
                Path(src_path).unlink()
                raise FileNotFoundError(src_path)
            return real_copy2(src_path, dst_path, *args, **kwargs)

        monkeypatch.setattr(backup_mod.shutil, "copy2", flaky_copy2)

        dest = tmp_path / "snap"
        copied = backup_mod.snapshot_pdf_reports(src, dest)

        assert copied == 1
        assert (dest / "report.pdf").read_bytes() == b"%PDF-1.4 real"
        assert not (dest / "inflight.pdf.tmp").exists()  # *.tmp skipped
        assert not (dest / "racer.pdf").exists()  # mid-copy delete omitted, not fatal

    def test_valid_hmac_but_corrupt_database_is_rejected_before_any_swap(self, env, tmp_path: Path):
        data_dir = env[2]
        marker = data_dir / "sqlite" / "keep.txt"
        marker.write_text("original")
        corrupt = DB_BYTES[: len(DB_BYTES) // 2] + b"\x00" * (len(DB_BYTES) // 2)
        name, payload = _named(_good(tmp_path / "bad.tar.bz2", db=corrupt))
        resp = _post(env, name, payload)  # the HMAC of this archive is valid
        assert resp.status_code == 400
        assert "Database" in resp.json()["detail"]
        assert marker.read_text() == "original"

    def test_garbage_in_database_pages_fails_quick_check(self, tmp_path: Path):
        import sqlite3

        db = tmp_path / "broken.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t(a TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * 200)
        conn.commit()
        conn.close()
        raw = bytearray(db.read_bytes())
        raw[4096 * 2 : 4096 * 2 + 200] = b"\xff" * 200
        db.write_bytes(bytes(raw))
        with pytest.raises(BackupError):
            backup_mod.check_sqlite_integrity(db, full=True)

    def test_backup_endpoint_archives_a_consistent_snapshot(self, env):
        client, token, _data_dir = env
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        resp = client.post("/settings/backup", headers=headers)
        assert resp.status_code == 200, resp.text
        with TemporaryDirectory() as out:
            archive = Path(out) / "b.tar.bz2"
            archive.write_bytes(resp.content)
            with tarfile.open(archive) as tar:
                names = tar.getnames()
            assert "data/sqlite/justup.db" in names
            assert not any(n.endswith(("-wal", "-shm")) for n in names)
            extracted = backup_mod.extract_backup(archive, Path(out) / "x")  # runs the integrity check
            backup_mod.check_sqlite_integrity(extracted / "sqlite" / "justup.db", full=True)


class TestRestoreQuiescing:
    def test_restore_does_not_close_foreign_connections_and_waits(self, env, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 0.3)
        app_db = env[2] / "sqlite" / "justup.db"
        other = sqlite_db.connect(app_db)  # a running user that never finishes
        try:
            name, payload = _named(_good(tmp_path / "a.tar.bz2"))
            resp = _post(env, name, payload)
            assert resp.status_code == 503
            assert other.execute("SELECT 1").fetchone()[0] == 1  # handle was NOT closed under its user
            _c = sqlite_db.connect(app_db)  # block lifted again: new connections work
            sqlite_db.close_connection(_c)
        finally:
            sqlite_db.close_connection(other)
            sqlite_db.end_quiesce()

    def test_restore_waits_for_a_live_sqlite_transaction_then_applies(self, env, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 5.0)
        db_path = env[2] / "sqlite" / "justup.db"
        other = sqlite_db.connect(db_path)
        other.execute("BEGIN IMMEDIATE")
        quiesce_waiting = threading.Event()
        real_sleep = sqlite_db.time.sleep

        def observe_quiesce_wait(seconds):
            quiesce_waiting.set()
            real_sleep(seconds)

        monkeypatch.setattr(sqlite_db.time, "sleep", observe_quiesce_wait)
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        responses = []
        restore = threading.Thread(target=lambda: responses.append(_post(env, name, payload)))
        try:
            restore.start()
            assert quiesce_waiting.wait(5)  # endpoint is waiting for the open connection
            assert restore.is_alive()
            assert other.execute("SELECT 1").fetchone()[0] == 1
            sqlite_db.close_connection(other)  # closes the transaction and releases the restore
            restore.join(10)
            assert not restore.is_alive()
            assert len(responses) == 1 and responses[0].status_code == 200, (
                responses[0].text if responses else "restore request produced no response"
            )
        finally:
            sqlite_db.close_connection(other)
            if restore.ident is not None:
                restore.join(10)
            sqlite_db.end_quiesce()

    def test_successful_restore_keeps_database_quiesced_until_restart(self, env, tmp_path: Path):
        try:
            name, payload = _named(_good(tmp_path / "a.tar.bz2"))
            assert _post(env, name, payload).status_code == 200
            with pytest.raises(sqlite_db.DatabaseQuiescedError):
                sqlite_db.connect(env[2] / "sqlite" / "justup.db")
        finally:
            sqlite_db.end_quiesce()


class TestQuiesceRace:
    """begin_quiesce() and connect() are atomic against each other: no connection escapes uncounted."""

    def test_quiesce_waits_for_a_connect_already_past_admission(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "race.db"
        sqlite_db.close_connection(sqlite_db.connect(db))
        connect_entered, release_connect, quiesce_waiting = threading.Event(), threading.Event(), threading.Event()
        connection_closed = threading.Event()
        real_connect = sqlite_db.sqlite3.connect
        real_sleep = sqlite_db.time.sleep
        results: list[bool] = []

        def gated_connect(database, **kwargs):
            if str(database) == str(db):
                connect_entered.set()
                if not release_connect.wait(5):
                    raise TimeoutError("test did not release the admitted SQLite connect")
            return real_connect(database, **kwargs)

        def observe_quiesce_wait(seconds):
            quiesce_waiting.set()
            real_sleep(seconds)

        monkeypatch.setattr(sqlite_db.sqlite3, "connect", gated_connect)
        monkeypatch.setattr(sqlite_db.time, "sleep", observe_quiesce_wait)

        def connect_and_close():
            conn = sqlite_db.connect(db)
            sqlite_db.close_connection(conn)
            connection_closed.set()

        connector = threading.Thread(target=connect_and_close)
        quiescer = threading.Thread(target=lambda: results.append(sqlite_db.begin_quiesce(timeout=5)))
        try:
            connector.start()
            assert connect_entered.wait(5)
            quiescer.start()
            assert quiesce_waiting.wait(5)  # begin_quiesce observed the pending connect and is draining it
            assert results == []
            release_connect.set()
            connector.join(5)
            quiescer.join(5)
            assert not connector.is_alive() and not quiescer.is_alive()
            assert connection_closed.is_set()
            assert results == [True]
            assert sqlite_db.get_connection_stats()["pending_connects"] == 0
            assert sqlite_db.get_connection_stats()["currently_open"] == 0
        finally:
            release_connect.set()
            connector.join(5)
            if quiescer.ident is not None:
                quiescer.join(5)
            sqlite_db.end_quiesce()

    def test_failed_connect_leaves_no_pending_count_and_does_not_block_quiesce(self, tmp_path: Path, monkeypatch):
        def boom(*_a, **_k):
            raise sqlite3.OperationalError("unable to open")

        monkeypatch.setattr(sqlite_db.sqlite3, "connect", boom)
        with pytest.raises(sqlite3.OperationalError):
            sqlite_db.connect(tmp_path / "x.db")
        monkeypatch.undo()
        assert sqlite_db.get_connection_stats()["pending_connects"] == 0
        assert sqlite_db.begin_quiesce(timeout=1)

    def test_pragma_failure_closes_handle_and_releases_pending(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sqlite_db, "SYNCHRONOUS_MODE", "FULL; SELECT 1")
        with pytest.raises(sqlite3.Error):
            sqlite_db.connect(tmp_path / "p.db")
        assert sqlite_db.get_connection_stats()["pending_connects"] == 0
        assert sqlite_db.get_connection_stats()["currently_open"] == 0

    def test_close_connection_is_idempotent(self, tmp_path: Path):
        before = sqlite_db.get_connection_stats()
        conn = sqlite_db.connect(tmp_path / "c.db")
        sqlite_db.close_connection(conn)
        sqlite_db.close_connection(conn)
        after = sqlite_db.get_connection_stats()
        assert after["total_closed"] - before["total_closed"] == 1
        assert after["currently_open"] == before["currently_open"]

    def test_second_quiesce_does_not_lift_the_first(self, tmp_path: Path):
        assert sqlite_db.begin_quiesce(timeout=1)
        assert not sqlite_db.begin_quiesce(timeout=0.1)
        with pytest.raises(sqlite_db.DatabaseQuiescedError):
            sqlite_db.connect(tmp_path / "q.db")


class TestBackupSecretRotation:
    NEW = "0123456789abcdef0123456789abcdef-n3W"
    OLD = "fedcba9876543210fedcba9876543210-o1D"

    def test_first_key_signs_and_every_key_verifies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", self.OLD)
        old_digest = backup_mod.compute_backup_hmac(archive)  # backup made before the rotation
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", f"{self.NEW}, {self.OLD}")
        new_digest = backup_mod.compute_backup_hmac(archive)  # signs with the first key only
        assert new_digest != old_digest
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", self.NEW)
        assert new_digest == backup_mod.compute_backup_hmac(archive)
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", f"{self.NEW},{self.OLD}")
        assert verify_backup_hmac(archive, new_digest)
        assert verify_backup_hmac(archive, old_digest)  # later key still verifies
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", self.NEW)
        assert not verify_backup_hmac(archive, old_digest)  # dropped from the list: no longer accepted

    def test_tampered_file_is_rejected_for_every_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", f"{self.NEW},{self.OLD}")
        archive = _good(tmp_path / "a.tar.bz2")
        digests = backup_mod.compute_backup_hmacs(archive, backup_mod.backup_hmac_keys())
        archive.write_bytes(archive.read_bytes() + b"x")
        assert not any(verify_backup_hmac(archive, d) for d in digests)

    def test_rotation_list_is_read_once_without_early_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", f"{self.NEW},{self.OLD}")
        archive = _good(tmp_path / "a.tar.bz2")
        first = backup_mod.compute_backup_hmacs(archive, backup_mod.backup_hmac_keys())[0]
        compared: list[bool] = []
        real = backup_mod.hmac.compare_digest
        monkeypatch.setattr(backup_mod.hmac, "compare_digest", lambda a, b: compared.append(True) or real(a, b))
        assert verify_backup_hmac(archive, first)  # matches the FIRST key, the second is still compared
        assert len(compared) == 2

    @pytest.mark.parametrize(
        "value",
        [
            "{NEW},{NEW}",  # duplicate
            "{NEW},",  # trailing empty entry
            ",{NEW}",  # leading empty entry
            "{NEW},,{OLD}",  # empty entry in the middle
            "{NEW}, ,{OLD}",  # whitespace-only entry
            "{NEW},short",  # weak second key
            "{NEW},{WEAK}",  # repetitive second key
        ],
    )
    def test_invalid_rotation_lists_are_rejected_without_echo(self, monkeypatch: pytest.MonkeyPatch, value: str):
        weak = "ab" * 20
        monkeypatch.setenv("JUSTUP_BACKUP_HMAC_SECRET", value.format(NEW=self.NEW, OLD=self.OLD, WEAK=weak))
        with pytest.raises(backup_mod.BackupConfigError) as exc:
            backup_mod.backup_hmac_keys()
        message = str(exc.value)
        assert self.NEW not in message
        assert self.OLD not in message
        assert weak not in message
        assert "short" not in message.replace("too short", "")


class TestCoordinatedMaintenance:
    """Backup and restore coordinate with TSDB/SQLite writers through the maintenance state."""

    def _headers(self, env):
        client, token, _ = env
        return {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}

    def test_backup_archives_complete_tsdb_series_while_writers_append(self, env):
        import json
        import threading

        from app.db import sqlite as _s  # noqa: F401
        from app.db import tsdb

        client, _token, data_dir = env
        tsdb_dir = data_dir / "tsdb"
        for i in range(5):
            tsdb.append_point(tsdb_dir, target_id=1, metric="ping_ms", value=i, retention_days=30)
        stop = threading.Event()

        def writer():
            n = 0
            while not stop.is_set():
                tsdb.append_point(tsdb_dir, target_id=1, metric="ping_ms", value="y" * 3000 + str(n), retention_days=30)
                n += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            resp = client.post("/settings/backup", headers=self._headers(env))
        finally:
            stop.set()
            t.join(5)
        assert resp.status_code == 200, resp.text
        with TemporaryDirectory() as out:
            archive = Path(out) / "b.tar.bz2"
            archive.write_bytes(resp.content)
            with tarfile.open(archive) as tar:
                names = tar.getnames()
                member = tar.extractfile("data/tsdb/target_1/ping_ms.jsonl")
                lines = member.read().decode().splitlines()
            assert not any(n.endswith(".lock") for n in names)
            assert len(lines) >= 5
            for line in lines:
                json.loads(line)

    def test_backup_is_refused_with_retry_after_during_maintenance(self, env):
        assert maintenance.begin_maintenance(timeout=1)
        resp = client_resp = env[0].post("/settings/backup", headers=self._headers(env))
        assert client_resp.status_code == 503
        assert resp.headers["retry-after"] == "30"
        maintenance.end_maintenance()
        assert env[0].post("/settings/backup", headers=self._headers(env)).status_code == 200

    def test_restore_waits_until_a_running_backup_snapshot_finishes(self, env, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 5.0)
        backup_entered, release_backup, restore_waiting = threading.Event(), threading.Event(), threading.Event()
        real_snapshot = settings_api.snapshot_sqlite_dir
        real_condition_wait = maintenance._COND.wait  # noqa: SLF001 - event hook for the restore drain

        def blocked_snapshot(source: Path, destination: Path):
            backup_entered.set()
            if not release_backup.wait(5):
                raise TimeoutError("test did not release the backup snapshot")
            return real_snapshot(source, destination)

        def observe_restore_wait(timeout=None):
            restore_waiting.set()
            return real_condition_wait(timeout)

        monkeypatch.setattr(settings_api, "snapshot_sqlite_dir", blocked_snapshot)
        monkeypatch.setattr(maintenance._COND, "wait", observe_restore_wait)  # noqa: SLF001
        backup_responses, restore_responses = [], []
        backup = threading.Thread(
            target=lambda: backup_responses.append(env[0].post("/settings/backup", headers=self._headers(env)))
        )
        name, payload = _named(_good(tmp_path / "overlap.tar.bz2"))
        restore = threading.Thread(target=lambda: restore_responses.append(_post(env, name, payload)))
        try:
            backup.start()
            assert backup_entered.wait(5)  # snapshot is inside the backup work section
            restore.start()
            assert restore_waiting.wait(5)  # restore has begun draining active work
            assert maintenance.is_active()
            assert not backup_responses and not restore_responses
            release_backup.set()
            backup.join(15)
            restore.join(15)
            assert not backup.is_alive() and not restore.is_alive()
            assert len(backup_responses) == 1 and backup_responses[0].status_code == 200
            assert len(restore_responses) == 1 and restore_responses[0].status_code == 200
        finally:
            release_backup.set()
            backup.join(10)
            if restore.ident is not None:
                restore.join(10)
            sqlite_db.end_quiesce()
            maintenance.end_maintenance()

    def test_restore_waits_for_a_running_tsdb_writer_then_applies(self, env, tmp_path: Path, monkeypatch):
        import threading

        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 5.0)
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        entered, release = threading.Event(), threading.Event()
        restore_waiting = threading.Event()
        real_condition_wait = maintenance._COND.wait  # noqa: SLF001 - event hook for the restore drain

        def observe_restore_wait(timeout=None):
            restore_waiting.set()
            return real_condition_wait(timeout)

        monkeypatch.setattr(maintenance._COND, "wait", observe_restore_wait)  # noqa: SLF001

        def writer():
            with maintenance.work_section():  # e.g. a scheduler iteration writing TSDB points
                entered.set()
                release.wait(5)

        t = threading.Thread(target=writer)
        t.start()
        assert entered.wait(5)
        responses = []
        restore = threading.Thread(target=lambda: responses.append(_post(env, name, payload)))
        try:
            restore.start()
            assert restore_waiting.wait(5)
            assert restore.is_alive()
            release.set()
            restore.join(10)
            assert not restore.is_alive()
        finally:
            release.set()
            t.join(5)
            if restore.ident is not None:
                restore.join(10)
        assert len(responses) == 1 and responses[0].status_code == 200, (
            responses[0].text if responses else "restore request produced no response"
        )
        assert (env[2] / "sqlite" / "x.db").read_bytes() == DB_BYTES

    def test_restore_503_changes_nothing_and_resets_the_state(self, env, tmp_path: Path, monkeypatch):
        import threading

        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 0.3)
        db_file = env[2] / "sqlite" / "justup.db"
        before = db_file.read_bytes()
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        entered, release = threading.Event(), threading.Event()

        def stuck_writer():
            with maintenance.work_section():
                entered.set()
                release.wait(5)

        t = threading.Thread(target=stuck_writer)
        t.start()
        entered.wait(5)
        try:
            resp = _post(env, name, payload)
        finally:
            release.set()
            t.join(5)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "30"
        assert db_file.read_bytes() == before
        assert not (env[2] / "sqlite" / "x.db").exists()
        assert not maintenance.is_active()  # refusal lifted
        with maintenance.work_section():
            pass
        _c = sqlite_db.connect(db_file)  # database not left quiesced
        sqlite_db.close_connection(_c)

    def test_sqlite_user_timeout_also_resets_maintenance(self, env, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(settings_api, "RESTORE_QUIESCE_TIMEOUT_SECONDS", 0.3)
        other = sqlite_db.connect(env[2] / "sqlite" / "justup.db")
        try:
            name, payload = _named(_good(tmp_path / "a.tar.bz2"))
            assert _post(env, name, payload).status_code == 503
        finally:
            sqlite_db.close_connection(other)
        assert not maintenance.is_active()

    def test_failed_restore_with_rollback_resets_everything(self, env, tmp_path: Path, monkeypatch):
        from app.db import tsdb
        from app.utils.time import utcnow

        def fail(*_a, **_k):
            raise backup_mod.RestoreFailedError("restore failed", rolled_back=True)

        monkeypatch.setattr(settings_api, "apply_restore", fail)
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        generation_before = maintenance.current_generation()
        assert _post(env, name, payload).status_code == 500
        assert not maintenance.is_active()
        # No data was replaced (rolled back), so pre-restore in-flight work must not be made stale.
        assert maintenance.current_generation() == generation_before
        _c = sqlite_db.connect(env[2] / "sqlite" / "justup.db")
        sqlite_db.close_connection(_c)
        now = utcnow()
        tsdb_dir = env[2] / "tsdb"
        tsdb.append_point(tsdb_dir, target_id=1, metric="ping_ms", value=17, retention_days=30, at=now)
        points = tsdb.query_auto_resolution(
            tsdb_dir,
            target_id=1,
            metric="ping_ms",
            since=now - timedelta(seconds=1),
            until=now + timedelta(seconds=1),
            limit=10,
        )
        assert len(points) == 1 and points[0].value == 17

    def test_unexpected_error_during_swap_resets_everything(self, env, tmp_path: Path, monkeypatch):
        from app.db import tsdb
        from app.utils.time import utcnow

        def explode(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(settings_api, "apply_restore", explode)
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        generation_before = maintenance.current_generation()
        with pytest.raises(OSError, match="disk gone"):
            _post(env, name, payload)
        assert not maintenance.is_active()
        assert maintenance.current_generation() == generation_before  # swap never confirmed
        _c = sqlite_db.connect(env[2] / "sqlite" / "justup.db")
        sqlite_db.close_connection(_c)
        now = utcnow()
        tsdb_dir = env[2] / "tsdb"
        tsdb.append_point(tsdb_dir, target_id=1, metric="ping_ms", value=23, retention_days=30, at=now)
        points = tsdb.query_auto_resolution(
            tsdb_dir,
            target_id=1,
            metric="ping_ms",
            since=now - timedelta(seconds=1),
            until=now + timedelta(seconds=1),
            limit=10,
        )
        assert len(points) == 1 and points[0].value == 23

    def test_failed_rollback_keeps_the_system_blocked(self, env, tmp_path: Path, monkeypatch):
        def fail(*_a, **_k):
            raise backup_mod.RestoreFailedError("rollback failed", rolled_back=False, rollback_dir=tmp_path)

        monkeypatch.setattr(settings_api, "apply_restore", fail)
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        generation_before = maintenance.current_generation()
        assert _post(env, name, payload).status_code == 500
        assert maintenance.is_active()  # data state unknown: no worker may write until restart
        # apply_restore never returned, so the swap was not confirmed and the generation is unchanged.
        assert maintenance.current_generation() == generation_before
        with pytest.raises(sqlite_db.DatabaseQuiescedError):
            sqlite_db.connect(env[2] / "sqlite" / "justup.db")

    def test_successful_restore_keeps_workers_blocked_until_restart(self, env, tmp_path: Path):
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        generation_before = maintenance.current_generation()
        assert _post(env, name, payload).status_code == 200
        assert maintenance.is_active()
        # A confirmed swap advances the data generation so pre-restore work is rejected as stale.
        assert maintenance.current_generation() == generation_before + 1

    def test_two_simultaneous_restores_reject_second_before_extraction(self, env, tmp_path: Path, monkeypatch):
        import threading

        name, payload = _named(_good(tmp_path / "a.tar.bz2"))

        real_extract = settings_api.extract_backup
        first_in = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        calls_lock = threading.Lock()

        def blocking_extract(upload_path, dest, *args, **kwargs):
            with calls_lock:
                calls["n"] += 1
                mine = calls["n"]
            if mine == 1:
                first_in.set()
                release.wait(5)  # first restore holds the guard here
            return real_extract(upload_path, dest, *args, **kwargs)

        monkeypatch.setattr(settings_api, "extract_backup", blocking_extract)

        first_result: list[int] = []

        def first_restore():
            first_result.append(_post(env, name, payload).status_code)

        t = threading.Thread(target=first_restore)
        t.start()
        try:
            assert first_in.wait(5)  # first restore is now blocked inside extraction, holding the guard
            open_before = sqlite_db.get_connection_stats()["currently_open"]

            # Second restore must be rejected fast, before any extraction of its own.
            second = _post(env, name, payload)
            assert second.status_code == 503
            assert second.headers["retry-after"] == "30"
            with calls_lock:
                assert calls["n"] == 1  # the second restore never reached extract_backup
            # No second handle was left open by the rejected restore.
            assert sqlite_db.get_connection_stats()["currently_open"] <= open_before
        finally:
            release.set()
            t.join(5)
        assert first_result == [200]
