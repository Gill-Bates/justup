#!/usr/bin/env python3
#
# tests/test_backup_restore.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Backup integrity (full HMAC-SHA256), archive validation limits and the restore endpoint."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import threading
from collections.abc import Iterable
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
from app.utils.rate_limit import reset_all
from app.utils.version import get_version
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
    reset_all()  # the heavy-endpoint rate limit is process-wide; many restore calls would hit it
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


def _db_bytes(schema_version: int = sqlite_db.SCHEMA_VERSION, *, legacy_target: bool = False) -> bytes:
    """A current-layout database with a chosen version stamp for compatibility checks."""
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "justup.db"
        conn = sqlite_db.connect(db)
        sqlite_db.init_schema(conn)
        if legacy_target:
            sqlite_db.create_target(
                conn,
                {
                    "name": "legacy",
                    "host": "legacy.example.com",
                    "interval_seconds": 60,
                    "retention_days": 30,
                    "enable_ping": True,
                    "enable_tcp_connect": True,
                    "tcp_host": "legacy.example.com",
                    "tcp_ports": "22,53",
                },
            )
        conn.execute("INSERT INTO settings VALUES (?, ?, ?)", ("migration-marker", "preserved", "2026-01-01T00:00:00Z"))
        conn.execute(f"PRAGMA user_version = {schema_version}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sqlite_db.close_connection(conn)
        return db.read_bytes()


DB_BYTES = _db_bytes()


def _manifest_bytes(db: bytes, *, app_version: str | None = None, build_id: str | None = None) -> bytes:
    with TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "justup.db"
        db_file.write_bytes(db)
        manifest = backup_mod.create_backup_manifest(db_file)
    if app_version is not None:
        manifest["application"]["version"] = app_version
    if build_id is not None:
        manifest["application"]["build_id"] = build_id
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _good(
    path: Path,
    db: bytes = DB_BYTES,
    *,
    manifest_db: bytes | None = None,
    app_version: str | None = None,
    build_id: str | None = None,
) -> Path:
    manifest_bytes = _manifest_bytes(
        db if manifest_db is None else manifest_db, app_version=app_version, build_id=build_id
    )
    return _tar(path, _entries(db, manifest_bytes))


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

    def test_manifest_is_covered_by_archive_hmac(self, tmp_path: Path):
        archive = _good(tmp_path / "source.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        entries = []
        with tarfile.open(archive, mode="r:bz2") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    entries.append((member.name, None))
                    continue
                handle = tar.extractfile(member)
                assert handle is not None
                content = handle.read()
                if member.name == "data/backup-manifest.json":
                    content += b" "
                entries.append((member.name, content))
        changed = _tar(tmp_path / "changed.tar.bz2", entries)
        assert not verify_backup_hmac(changed, digest)

    def test_other_instance_key_does_not_verify(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        assert not verify_backup_hmac(archive, digest)

    def test_rotated_key_still_verifies_old_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        digest = backup_mod.compute_backup_hmac(archive)
        old = os.environ["SECRET_KEY"]
        monkeypatch.delenv("SECRET_KEY")
        monkeypatch.setenv("SECRET_KEYS", f"{Fernet.generate_key().decode('ascii')},{old}")
        assert verify_backup_hmac(archive, digest)

    def test_signing_key_is_not_stored_in_the_database(self, env):
        client, token, _ = env
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        assert client.post("/settings/backup", headers=headers).status_code == 200
        conn = sqlite_db.connect(client.app.state.db_path)
        try:
            stored = "\n".join(str(v) for row in conn.execute("SELECT value FROM settings") for v in row)
        finally:
            sqlite_db.close_connection(conn)
        for key in backup_mod.backup_hmac_keys():
            assert key.decode("utf-8", "replace") not in stored

    def test_legacy_format_is_rejected_with_clear_message(self):
        with pytest.raises(BackupError, match="legacy backup format"):
            parse_backup_filename("justup_backup_20260101_000000_0123456789abcdef.tar.bz2")
        with pytest.raises(BackupError, match="Invalid filename"):
            parse_backup_filename("evil.tar.bz2")


class TestArchiveValidation:
    def test_valid_archive_extracts(self, tmp_path: Path):
        extracted = backup_mod.extract_backup(_good(tmp_path / "a.tar.bz2"), tmp_path / "out")
        assert (extracted / "sqlite" / "justup.db").read_bytes() == DB_BYTES

    def test_missing_manifest_and_duplicate_paths_are_rejected(self, tmp_path: Path):
        archive = _tar(
            tmp_path / "missing.tar.bz2",
            [("data", None), ("data/sqlite", None), ("data/sqlite/justup.db", DB_BYTES)],
        )
        with pytest.raises(BackupError, match="Missing backup manifest"):
            backup_mod.inspect_backup_archive(archive)

        duplicate = _tar(
            tmp_path / "duplicate.tar.bz2",
            [
                ("data", None),
                ("data/sqlite", None),
                ("data/sqlite/justup.db", DB_BYTES),
                ("data/sqlite/justup.db", DB_BYTES),
                ("data/backup-manifest.json", _manifest_bytes(DB_BYTES)),
            ],
        )
        with pytest.raises(BackupError, match="Duplicate archive path"):
            backup_mod.inspect_backup_archive(duplicate)

    def test_missing_application_database_is_rejected(self, tmp_path: Path):
        archive = _tar(
            tmp_path / "missing-db.tar.bz2",
            [
                ("data", None),
                ("data/sqlite", None),
                ("data/sqlite/other.db", DB_BYTES),
                ("data/backup-manifest.json", _manifest_bytes(DB_BYTES)),
            ],
        )
        with pytest.raises(BackupError, match=r"Missing data/sqlite/justup\.db"):
            backup_mod.inspect_backup_archive(archive)

    def test_unknown_metadata_and_fingerprint_mismatch_are_rejected(self):
        unknown = json.loads(_manifest_bytes(DB_BYTES))
        unknown["extra"] = True
        with pytest.raises(BackupError, match="Manifest fields"):
            backup_mod.validate_backup_manifest(json.dumps(unknown).encode())
        mismatched = json.loads(_manifest_bytes(DB_BYTES))
        mismatched["database"]["schema_sha256"] = "0" * 64
        with pytest.raises(BackupError, match="fingerprint"):
            backup_mod.validate_backup_manifest(json.dumps(mismatched).encode())

    def test_app_version_comparison_is_numeric_and_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        import app.utils.version as version_module

        manifest = backup_mod.validate_backup_manifest(_manifest_bytes(DB_BYTES, app_version="1.0.2"))
        monkeypatch.setattr(version_module, "get_version", lambda: "1.0.10")
        backup_mod.validate_backup_compatibility(manifest)
        with pytest.raises(BackupError, match="newer application version"):
            backup_mod.validate_backup_compatibility(
                backup_mod.validate_backup_manifest(_manifest_bytes(DB_BYTES, app_version="1.0.11"))
            )
        with pytest.raises(BackupError, match="cannot be compared safely"):
            backup_mod.validate_backup_compatibility(
                backup_mod.validate_backup_manifest(_manifest_bytes(DB_BYTES, app_version="unknown"))
            )

    def test_development_builds_require_an_identical_build_id(self, monkeypatch: pytest.MonkeyPatch):
        import app.utils.version as version_module

        monkeypatch.setattr(version_module, "get_version", lambda: "dev")
        monkeypatch.setattr(version_module, "get_build_info", lambda: "build-123")
        manifest = backup_mod.validate_backup_manifest(
            _manifest_bytes(DB_BYTES, app_version="dev", build_id="build-123")
        )
        backup_mod.validate_backup_compatibility(manifest)
        manifest["application"]["build_id"] = "build-other"
        with pytest.raises(BackupError, match="identical, verifiable build ID"):
            backup_mod.validate_backup_compatibility(manifest)

    def test_missing_migration_path_is_rejected(self):
        manifest = backup_mod.validate_backup_manifest(_manifest_bytes(_db_bytes(9)))
        with pytest.raises(BackupError, match="No migration path"):
            backup_mod.validate_backup_compatibility(manifest)

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

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("../evil", "Unsafe path"),
            ("data/../../evil", "Unsafe path"),
            ("/etc/passwd", "Unsafe path"),
            ("other/file", "Unsafe path"),
            ("data/unknown/x", "Unexpected content"),
        ],
    )
    def test_traversal_and_unexpected_paths_are_rejected(self, tmp_path: Path, name, reason):
        # A valid manifest and database are present, so only the path can cause the rejection.
        archive = _tar(tmp_path / "a.tar.bz2", _entries(extra=[(name, b"x")]))
        with pytest.raises(BackupError, match=reason):
            backup_mod.extract_backup(archive, tmp_path / "out")
        assert not (tmp_path / "evil").exists()
        assert not (tmp_path / "out" / "evil").exists()

    def test_garbage_is_reported_as_corrupt(self, tmp_path: Path):
        bad = tmp_path / "bad.tar.bz2"
        bad.write_bytes(b"BZh9notreallybzip")
        with pytest.raises(BackupError, match="Archive validation failed"):
            backup_mod.extract_backup(bad, tmp_path / "out")


def _entries(
    db: bytes = DB_BYTES, manifest_bytes: bytes | None = None, extra: Iterable[tuple[str, bytes | None]] = ()
) -> list[tuple[str, bytes | None]]:
    """The one definition of the required backup layout."""
    return [
        ("data", None),
        ("data/sqlite", None),
        ("data/sqlite/justup.db", db),
        ("data/tsdb", None),
        ("data/backup-manifest.json", manifest_bytes or _manifest_bytes(db)),
        *extra,
    ]


class TestExtractionRevalidatesTheArchive:
    """Extraction validates the stream it extracts from, also when a manifest was already inspected."""

    @staticmethod
    def _signed() -> tuple[bytes, dict[str, object]]:
        """Manifest bytes as embedded in the test archives, plus the parsed form a prior inspection returns."""
        raw = _manifest_bytes(DB_BYTES)
        return raw, backup_mod.validate_backup_manifest(raw)

    def test_unexpected_content_is_rejected_although_a_manifest_was_passed(self, tmp_path: Path):
        raw, manifest = self._signed()
        archive = _tar(tmp_path / "a.tar.bz2", [*_entries(manifest_bytes=raw), ("data/unknown/x", b"x")])
        with pytest.raises(BackupError, match="Unexpected content"):
            backup_mod.extract_backup(archive, tmp_path / "out", manifest=manifest)
        assert not (tmp_path / "out" / "data" / "unknown").exists()

    @pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
    def test_links_inside_the_archive_are_rejected_although_a_manifest_was_passed(self, tmp_path: Path, kind):
        raw, manifest = self._signed()
        info = tarfile.TarInfo("data/sqlite/link.db")
        info.type = kind
        info.linkname = "data/sqlite/justup.db" if kind == tarfile.LNKTYPE else "justup.db"
        archive = _tar(tmp_path / "a.tar.bz2", _entries(manifest_bytes=raw), extra=info)
        with pytest.raises(BackupError, match="Links or special"):
            backup_mod.extract_backup(archive, tmp_path / "out", manifest=manifest)
        assert not (tmp_path / "out" / "data" / "sqlite" / "link.db").exists()

    def test_duplicate_paths_are_rejected_although_a_manifest_was_passed(self, tmp_path: Path):
        raw, manifest = self._signed()
        archive = _tar(tmp_path / "a.tar.bz2", [*_entries(manifest_bytes=raw), ("data/sqlite/justup.db", b"other")])
        with pytest.raises(BackupError, match="Duplicate archive path"):
            backup_mod.extract_backup(archive, tmp_path / "out", manifest=manifest)

    def test_a_file_used_as_directory_is_rejected(self, tmp_path: Path):
        archive = _tar(tmp_path / "a.tar.bz2", [*_entries(), ("data/tsdb/x", b"1"), ("data/tsdb/x/y", b"2")])
        with pytest.raises(BackupError, match="also used as a directory"):
            backup_mod.inspect_backup_archive(archive)
        late_parent = _tar(tmp_path / "b.tar.bz2", [*_entries(), ("data/tsdb/x/y", b"2"), ("data/tsdb/x", b"1")])
        with pytest.raises(BackupError, match="also used as a directory"):
            backup_mod.inspect_backup_archive(late_parent)

    def test_manifest_that_differs_from_the_inspected_one_is_rejected(self, tmp_path: Path):
        _raw, manifest = self._signed()
        other_db = _db_bytes(2)
        archive = _good(tmp_path / "other.tar.bz2", other_db)
        with pytest.raises(BackupError, match="Manifest changed"):
            backup_mod.extract_backup(archive, tmp_path / "out", manifest=manifest)

    def test_archive_changed_after_inspection_is_rejected(self, tmp_path: Path):
        raw, _manifest = self._signed()
        archive = _tar(tmp_path / "a.tar.bz2", _entries(manifest_bytes=raw))
        manifest = backup_mod.inspect_backup_archive(archive)
        _tar(archive, [*_entries(manifest_bytes=raw), ("data/unknown/x", b"x")])  # same path, new content
        with pytest.raises(BackupError, match="Unexpected content"):
            backup_mod.extract_backup(archive, tmp_path / "out", manifest=manifest)
        assert not (tmp_path / "out" / "data" / "unknown").exists()

    def test_archive_replaced_between_inspection_and_extraction_is_not_reopened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        archive = _good(tmp_path / "a.tar.bz2")
        opened: list[Path] = []
        real_open = Path.open

        def spy(self, *args, **kwargs):
            if self == archive:
                opened.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", spy)
        backup_mod.extract_backup(archive, tmp_path / "out")  # no manifest: inspection and extraction in one run
        assert len(opened) == 1

    def test_valid_archive_extracts_with_and_without_a_passed_manifest(self, tmp_path: Path):
        archive = _good(tmp_path / "a.tar.bz2")
        manifest = backup_mod.inspect_backup_archive(archive)
        extracted = backup_mod.extract_backup(archive, tmp_path / "out1", manifest=manifest)
        assert (extracted / "sqlite" / "justup.db").read_bytes() == DB_BYTES
        assert (extracted / "backup-manifest.json").is_file() and (extracted / "tsdb").is_dir()
        assert backup_mod.extract_backup(archive, tmp_path / "out2") == tmp_path / "out2" / "data"


class TestBoundedMemberHandling:
    def test_member_limit_boundary(self, tmp_path: Path):
        archive = _good(tmp_path / "a.tar.bz2")  # five members
        backup_mod.inspect_backup_archive(archive, max_members=5)
        with pytest.raises(BackupError, match="Too many"):
            backup_mod.inspect_backup_archive(archive, max_members=4)
        assert backup_mod.extract_backup(archive, tmp_path / "ok", max_members=5).is_dir()
        with pytest.raises(BackupError, match="Too many"):
            backup_mod.extract_backup(archive, tmp_path / "no", max_members=4)

    def test_validate_members_boundary_for_generic_iterables(self):
        def members(count: int):
            for index in range(count):
                info = tarfile.TarInfo(f"data/tsdb/{index}")
                yield info

        with pytest.raises(BackupError, match="Too many"):
            backup_mod.validate_members(members(4), max_members=3)
        with pytest.raises(BackupError, match="Missing backup manifest"):  # three members pass the limit check
            backup_mod.validate_members(members(3), max_members=3)

    def test_inspection_does_not_keep_every_member_in_the_tar_parser(self, tmp_path: Path, monkeypatch):
        entries = [*_entries(), *((f"data/tsdb/f{i}", b"") for i in range(3000))]
        archive = _tar(tmp_path / "a.tar.bz2", entries)
        opened: list[tarfile.TarFile] = []
        real_open = tarfile.open

        def spy(*args, **kwargs):
            tar = real_open(*args, **kwargs)
            opened.append(tar)
            return tar

        monkeypatch.setattr(backup_mod.tarfile, "open", spy)
        backup_mod.inspect_backup_archive(archive)
        assert opened and all(len(tar.members) <= 1 for tar in opened)

    def test_an_oversized_archive_stops_reading_at_the_limit(self, tmp_path: Path, monkeypatch):
        archive = _tar(tmp_path / "a.tar.bz2", [("data", None)] + [(f"data/tsdb/f{i}", b"") for i in range(5000)])
        parsed = 0
        real_next = tarfile.TarFile.next

        def counting_next(self):
            nonlocal parsed
            parsed += 1
            return real_next(self)

        monkeypatch.setattr(tarfile.TarFile, "next", counting_next)
        with pytest.raises(BackupError, match="Too many"):
            backup_mod.inspect_backup_archive(archive, max_members=50)
        assert 50 <= parsed <= 60  # aborted right after the limit instead of reading all 5001 headers

    def test_many_small_entries_stay_within_a_modest_memory_budget(self, tmp_path: Path):
        import tracemalloc

        entries = [*_entries(), *((f"data/tsdb/f{i}", b"") for i in range(60_000))]
        archive = _tar(tmp_path / "a.tar.bz2", entries)
        tracemalloc.start()
        try:
            backup_mod.inspect_backup_archive(archive)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < MEMBER_MEMORY_BUDGET_BYTES


MEMBER_MEMORY_BUDGET_BYTES = 12 * 1024 * 1024


class TestBackupContractConstants:
    def test_missing_application_database_in_a_staged_directory(self, tmp_path: Path):
        (tmp_path / "sqlite").mkdir()
        (tmp_path / "sqlite" / "other.db").write_bytes(DB_BYTES)
        with pytest.raises(BackupError, match=r"Missing data/sqlite/justup\.db"):
            backup_mod.verify_extracted_databases(tmp_path)

    def test_application_database_is_required_at_one_archive_and_staging_path(self, tmp_path: Path):
        extracted = backup_mod.extract_backup(_good(tmp_path / "a.tar.bz2"), tmp_path / "out")
        assert (extracted / "sqlite" / "justup.db").is_file()
        original, migrated = backup_mod.migrate_staged_application_database(extracted)
        assert original == migrated == sqlite_db.SCHEMA_VERSION

    def test_application_database_must_be_a_regular_archive_member(self, tmp_path: Path):
        archive = _tar(
            tmp_path / "a.tar.bz2",
            [
                ("data", None),
                ("data/sqlite", None),
                ("data/sqlite/justup.db", None),
                ("data/backup-manifest.json", _manifest_bytes(DB_BYTES)),
            ],
        )
        with pytest.raises(BackupError, match="Invalid application database entry"):
            backup_mod.inspect_backup_archive(archive)

    def test_created_manifest_has_exactly_the_v1_schema_categories(self, tmp_path: Path):
        db_file = tmp_path / "justup.db"
        db_file.write_bytes(DB_BYTES)
        manifest = backup_mod.create_backup_manifest(db_file)
        assert list(manifest["database"]["schema"]) == ["tables", "indexes", "views", "triggers"]
        backup_mod.validate_backup_manifest(_manifest_bytes(DB_BYTES))  # the generated V1 shape is accepted

    def test_v1_fingerprint_is_stable(self):
        empty = {"tables": [], "indexes": [], "views": [], "triggers": []}
        manifest = json.loads(_manifest_bytes(DB_BYTES))
        manifest["database"].update(
            schema_version=1,
            schema=empty,
            schema_sha256="201c4137a74b5c48e9edc4014204c419cd3b29669bd3ff08a57d6314914f9017",
        )
        assert backup_mod.validate_backup_manifest(json.dumps(manifest).encode())["database"]["schema"] == empty

    @pytest.mark.parametrize("change", ["extra", "missing"])
    def test_unexpected_or_missing_schema_categories_are_rejected(self, change):
        manifest = json.loads(_manifest_bytes(DB_BYTES))
        if change == "extra":
            manifest["database"]["schema"]["sequences"] = []
        else:
            del manifest["database"]["schema"]["views"]
        with pytest.raises(BackupError, match="Schema metadata is invalid"):
            backup_mod.validate_backup_manifest(json.dumps(manifest).encode())


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

    def test_signed_backup_with_unsupported_schema_is_rejected_before_the_swap(self, env, tmp_path: Path):
        old_db = tmp_path / "old.db"
        raw = sqlite3.connect(old_db)
        raw.execute("CREATE TABLE t(a INTEGER)")
        raw.execute(f"PRAGMA user_version={sqlite_db.SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()
        archive = _tar(
            tmp_path / "old.tar.bz2",
            [
                ("data", None),
                ("data/sqlite", None),
                ("data/sqlite/justup.db", old_db.read_bytes()),
                ("data/backup-manifest.json", _manifest_bytes(old_db.read_bytes())),
            ],
        )
        name, payload = _named(archive)
        live = env[2] / "sqlite" / "justup.db"
        before = live.read_bytes()
        resp = _post(env, name, payload)
        assert resp.status_code == 400
        assert "schema" in resp.json()["detail"]
        assert "not changed" in resp.json()["detail"]
        assert live.read_bytes() == before

    def test_backup_from_newer_application_is_rejected(self, env, tmp_path: Path):
        major, *_rest = get_version().split(".")
        newer = f"{int(major) + 1}.0.0"  # always newer than the installed application
        name, payload = _named(_good(tmp_path / "newer-app.tar.bz2", app_version=newer))
        resp = _post(env, name, payload)
        assert resp.status_code == 400
        assert "newer application version" in resp.json()["detail"]

    def test_former_current_schema_is_restamped_before_restore(self, env, tmp_path: Path):
        schema_version = sqlite_db.LEGACY_SCHEMA_VERSION
        name, payload = _named(_good(tmp_path / "schema-10.tar.bz2", db=_db_bytes(schema_version)))
        resp = _post(env, name, payload)
        assert resp.status_code == 200, resp.text
        assert "Backup compatible" in resp.json()["message"]
        assert f"reset from {schema_version} to {sqlite_db.SCHEMA_VERSION}" in resp.json()["message"]
        live = env[2] / "sqlite" / "justup.db"
        conn = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == sqlite_db.SCHEMA_VERSION
            assert conn.execute("SELECT value FROM settings WHERE key='migration-marker'").fetchone()[0] == "preserved"
            assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_new_backup_carries_udp_configuration_and_port_metadata(self, env):
        client, token, data_dir = env
        live = data_dir / "sqlite" / "justup.db"
        conn = sqlite_db.connect(live)
        try:
            tid = sqlite_db.create_target(
                conn,
                {
                    "name": "multi",
                    "host": "multi.example.com",
                    "interval_seconds": 60,
                    "retention_days": 30,
                    "enable_ping": True,
                    "enable_tcp_connect": True,
                    "tcp_host": "multi.example.com",
                    "tcp_ports": "53",
                    "enable_udp_scan": True,
                    "udp_ports": "53",
                },
            )
            sqlite_db.sync_network_ports(conn, tid, tcp_ports=[53], udp_ports=[53])
            sqlite_db.update_network_port_status(conn, tid, "udp", 53, "unknown", raw_state="open|filtered")
            conn.commit()
        finally:
            sqlite_db.close_connection(conn)
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        resp = client.post("/settings/backup", headers=headers)
        assert resp.status_code == 200, resp.text
        name = resp.headers["content-disposition"].split("filename=")[1]
        conn = sqlite_db.connect(live)
        try:
            conn.execute("DELETE FROM targets")
            conn.commit()
        finally:
            sqlite_db.close_connection(conn)
        assert _post(env, name, resp.content).status_code == 200
        conn = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            assert conn.execute("SELECT enable_udp_scan, udp_ports FROM targets").fetchone() == (1, "53")
            rows = conn.execute(
                "SELECT protocol, port, status, raw_state, service_identity FROM target_network_ports ORDER BY protocol"
            ).fetchall()
            assert rows == [("tcp", 53, None, None, "unknown"), ("udp", 53, "unknown", "open|filtered", "unknown")]
        finally:
            conn.close()

    def test_failed_staging_migration_leaves_live_database_unchanged(
        self, env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        live = env[2] / "sqlite" / "justup.db"
        conn = sqlite_db.connect(live)
        sqlite_db.set_setting(conn, "live-marker", "unchanged")
        sqlite_db.close_connection(conn)
        before = live.read_bytes()

        def fail_migration(_conn):
            raise RuntimeError("injected staging migration failure")

        backup_db = _db_bytes(sqlite_db.LEGACY_SCHEMA_VERSION)
        monkeypatch.setattr(sqlite_db, "init_schema", fail_migration)
        name, payload = _named(_good(tmp_path / "migration-failure.tar.bz2", db=backup_db))
        resp = _post(env, name, payload)
        assert resp.status_code == 400
        assert "existing data was not changed" in resp.json()["detail"]
        assert live.read_bytes() == before

    def test_valid_signed_archive_restores(self, env, tmp_path: Path):
        name, payload = _named(_good(tmp_path / "a.tar.bz2"))
        resp = _post(env, name, payload)
        assert resp.status_code == 200, resp.text
        assert "Backup compatible" in resp.json()["message"]
        assert (env[2] / "sqlite" / "justup.db").read_bytes() == DB_BYTES


class TestHmacKeys:
    def test_all_keys_are_evaluated_in_one_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        archive = _good(tmp_path / "a.tar.bz2")
        old = os.environ["SECRET_KEY"]
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
        old = os.environ["SECRET_KEY"]
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
        src = tmp_path / "sqlite"
        src.mkdir()
        db = src / "justup.db"
        conn = sqlite_db.connect(db)
        conn.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b INTEGER NOT NULL)")
        conn.commit()
        conn.close()
        stop, wrote = threading.Event(), threading.Event()
        errors: list[Exception] = []

        def writer():
            wconn = sqlite_db.connect(db)
            try:
                while not stop.is_set():
                    with sqlite_db.transaction(wconn):  # a and b always change together
                        wconn.execute("INSERT INTO ledger(a, b) VALUES (1, 1)")
                        wconn.execute("UPDATE ledger SET a = a + 1, b = b + 1")
                    wrote.set()
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
            finally:
                wconn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert wrote.wait(5)  # snapshots must overlap real writes
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
            assert snap.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] > 0
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
        name, payload = _named(_good(tmp_path / "bad.tar.bz2", db=corrupt, manifest_db=DB_BYTES))
        resp = _post(env, name, payload)  # the HMAC of this archive is valid
        assert resp.status_code == 400
        assert "database" in resp.json()["detail"].lower()
        assert marker.read_text() == "original"

    def test_garbage_in_database_pages_fails_quick_check(self, tmp_path: Path):
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
                manifest_file = tar.extractfile(backup_mod.MANIFEST_PATH)
                database_file = tar.extractfile("data/sqlite/justup.db")
                assert manifest_file is not None and database_file is not None
                manifest = backup_mod.validate_backup_manifest(manifest_file.read())
                snapshot_bytes = database_file.read()
            assert "data/sqlite/justup.db" in names
            assert backup_mod.MANIFEST_PATH in names
            filename = resp.headers["content-disposition"].split("filename=")[1]
            assert backup_mod.verify_backup_hmac(archive, backup_mod.parse_backup_filename(filename))
            snapshot_db = Path(out) / "snapshot.db"
            snapshot_db.write_bytes(snapshot_bytes)
            actual_snapshot_manifest = backup_mod.create_backup_manifest(snapshot_db)
            assert manifest["format_version"] == 1
            assert manifest["application"]["version"] == get_version()
            assert manifest["database"] == actual_snapshot_manifest["database"]
            assert manifest["created_at"].endswith("Z")
            assert not any(n.endswith(("-wal", "-shm")) for n in names)
            extracted = backup_mod.extract_backup(archive, Path(out) / "x")  # runs the integrity check
            backup_mod.check_sqlite_integrity(extracted / "sqlite" / "justup.db", full=True)

    def test_backup_temp_archive_is_private_and_removed(self, env, monkeypatch):
        client, token, _data_dir = env
        seen: list[tuple[Path, int]] = []
        real_mkstemp = settings_api.tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            seen.append((Path(name), Path(name).stat().st_mode & 0o777))
            return fd, name

        monkeypatch.setattr(settings_api.tempfile, "mkstemp", spy)
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        assert client.post("/settings/backup", headers=headers).status_code == 200
        assert [mode for _p, mode in seen] == [0o600]
        assert not seen[0][0].exists()

    def test_backup_temp_archive_is_removed_when_archiving_fails(self, env, monkeypatch):
        client, token, _data_dir = env
        paths: list[Path] = []
        real_mkstemp = settings_api.tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            paths.append(Path(name))
            return fd, name

        monkeypatch.setattr(settings_api.tempfile, "mkstemp", spy)

        def fail(*_a, **_k):
            raise BackupError("boom")

        monkeypatch.setattr(settings_api, "snapshot_sqlite_dir", fail)
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": client.cookies.get("csrf_token")}
        assert client.post("/settings/backup", headers=headers).status_code == 500
        assert paths and not paths[0].exists()


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
        assert (env[2] / "sqlite" / "justup.db").read_bytes() == DB_BYTES

    def test_restore_503_changes_nothing_and_resets_the_state(self, env, tmp_path: Path, monkeypatch):
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
        assert entered.wait(5)
        try:
            resp = _post(env, name, payload)
        finally:
            release.set()
            t.join(5)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "30"
        assert db_file.read_bytes() == before
        assert not list(env[2].glob(".restore_staging_*"))  # staging directory cleaned up
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


# Hardening of the manifest-based restore: versions, hostile databases, real v1 history


def _app_db_with(*statements: str) -> bytes:
    """The current application database with extra statements applied (foreign keys off)."""
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "justup.db"
        db.write_bytes(DB_BYTES)
        raw = sqlite3.connect(db)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            for statement in statements:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        return db.read_bytes()


def _archive(path: Path, db: bytes, *, app_version: str | None = None, extra=()) -> Path:
    """A signed-style archive whose manifest describes ``db`` exactly (what the export would write)."""
    return _tar(path, _entries(db, _manifest_bytes(db, app_version=app_version), extra))


def _assert_rejected(env, archive: Path, fragment: str, status: int = 400) -> None:
    live = env[2] / "sqlite" / "justup.db"
    before = live.read_bytes()
    name, payload = _named(archive)
    resp = _post(env, name, payload)
    assert resp.status_code == status, resp.text
    assert fragment in resp.json()["detail"], resp.json()["detail"]
    assert live.read_bytes() == before
    assert not maintenance.is_active()
    assert not list(env[2].glob(".restore_staging_*"))
    sqlite_db.close_connection(sqlite_db.connect(live))  # still usable: nothing was quiesced


class TestReleaseComparison:
    """Application releases are ordered numerically; anything that cannot be ordered fails closed."""

    @pytest.fixture
    def installed(self, monkeypatch: pytest.MonkeyPatch):
        import app.utils.version as version_module

        def use(version: str) -> None:
            monkeypatch.setattr(version_module, "get_version", lambda: version)

        return use

    @staticmethod
    def _check(backup_version: str) -> None:
        manifest = backup_mod.validate_backup_manifest(_manifest_bytes(DB_BYTES, app_version=backup_version))
        backup_mod.validate_backup_compatibility(manifest)

    @pytest.mark.parametrize(
        ("installed_version", "backup_version"),
        [
            ("1.0.2", "1.0.2"),
            ("1.0.2", "1.0.2.0"),  # trailing zero parts do not matter
            ("1.0.10", "1.0.9"),  # numeric, not lexicographic
            ("1.0.2.1", "1.0.2"),
            ("1.1.0", "1.1.0rc1"),  # a pre-release is older than its final release
            ("1.1.0rc1", "1.1.0rc1"),  # an installed pre-release restores its own backups
            ("1.1.0", "1.1.0+build7"),
        ],
    )
    def test_accepted(self, installed, installed_version: str, backup_version: str):
        installed(installed_version)
        self._check(backup_version)

    @pytest.mark.parametrize(
        ("installed_version", "backup_version"),
        [
            ("1.0.9", "1.0.10"),
            ("1.0.2", "1.0.2.1"),  # a numeric suffix is newer
            ("1.1.0rc1", "1.1.0"),
            ("1.0.2", "1.0.2.post1"),  # post-releases are newer and not orderable here
            ("1.0.2", "1.0.2-1"),
            ("1.1.0rc1", "1.1.0rc2"),  # two different pre-releases cannot be ordered
            ("1.0.2", "v-unknown"),
        ],
    )
    def test_rejected(self, installed, installed_version: str, backup_version: str):
        installed(installed_version)
        with pytest.raises(BackupError, match=r"newer application version|cannot be compared safely"):
            self._check(backup_version)


class TestHostileBackupContent:
    def test_sqlite_side_files_are_not_accepted_in_an_archive(self, tmp_path: Path):
        for suffix in ("-wal", "-shm", "-journal"):
            archive = _archive(
                tmp_path / f"a{suffix}.tar.bz2", DB_BYTES, extra=((f"data/sqlite/justup.db{suffix}", b"x"),)
            )
            with pytest.raises(BackupError, match="side files"):
                backup_mod.inspect_backup_archive(archive)

    def test_triggers_and_views_in_the_application_database_are_rejected(self, env, tmp_path: Path):
        trigger = _app_db_with("CREATE TRIGGER evil AFTER INSERT ON settings BEGIN DELETE FROM users; END")
        _assert_rejected(env, _archive(tmp_path / "t.tar.bz2", trigger), "triggers or views")
        view = _app_db_with("CREATE VIEW sneaky AS SELECT * FROM users")
        _assert_rejected(env, _archive(tmp_path / "v.tar.bz2", view), "triggers or views")

    def test_foreign_key_violations_are_rejected_before_the_swap(self, env, tmp_path: Path):
        dangling = _app_db_with(
            "INSERT INTO alerts(id, target_id, target_name, failed_service, started_at, created_at, updated_at)"
            " VALUES ('x', 999, 't', 'ping', '2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        _assert_rejected(env, _archive(tmp_path / "fk.tar.bz2", dangling), "foreign_key_check")

    def test_leftover_wal_after_the_staging_migration_rejects_the_backup(
        self, env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        real_close = sqlite_db.close_connection

        def close_and_leave_wal(conn):
            path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            closed = real_close(conn)
            Path(f"{path}-wal").write_bytes(b"left behind")
            return closed

        monkeypatch.setattr(sqlite_db, "close_connection", close_and_leave_wal)
        _assert_rejected(
            env,
            _archive(tmp_path / "a.tar.bz2", _db_bytes(sqlite_db.LEGACY_SCHEMA_VERSION)),
            "existing data was not changed",
        )

    def test_unexpected_exception_while_opening_the_staged_database_rejects_the_backup(
        self, env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        real_connect = sqlite_db.connect

        def broken_connect(path, *args, **kwargs):
            if ".restore_staging_" in str(path):
                raise OSError("disk exploded")
            return real_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite_db, "connect", broken_connect)
        _assert_rejected(env, _archive(tmp_path / "a.tar.bz2", DB_BYTES), "existing data was not changed")


class TestRestoreAfterRestore:
    def test_second_restore_before_the_restart_is_postponed_not_failed(self, env, tmp_path: Path):
        name, payload = _named(_archive(tmp_path / "a.tar.bz2", DB_BYTES))
        assert _post(env, name, payload).status_code == 200  # the instance now waits for its restart
        again = _post(env, name, payload)
        assert again.status_code == 503, again.text
        assert again.headers["retry-after"] == "30"


class TestFormerCurrentSchemaBackup:
    """A version-10 backup keeps its data when restamped to the new version-1 baseline."""

    def _restore(self, env, tmp_path: Path):
        archive = _archive(
            tmp_path / "v10.tar.bz2",
            _db_bytes(sqlite_db.LEGACY_SCHEMA_VERSION, legacy_target=True),
            extra=(("data/tsdb/1/ping.jsonl", b'{"t": 1, "v": 12.5}\n'),),
        )
        name, payload = _named(archive)
        response = _post(env, name, payload)
        assert response.status_code == 200, response.text
        sqlite_db.end_quiesce()
        return response

    def test_data_survives_restamp_before_swap(self, env, tmp_path: Path):
        response = self._restore(env, tmp_path)
        assert f"reset from {sqlite_db.LEGACY_SCHEMA_VERSION} to 1" in response.json()["message"]
        assert response.json()["migrated"] is True
        assert response.json()["backup_schema_version"] == sqlite_db.LEGACY_SCHEMA_VERSION
        assert (env[2] / "tsdb" / "1" / "ping.jsonl").read_bytes() == b'{"t": 1, "v": 12.5}\n'
        conn = sqlite3.connect(env[2] / "sqlite" / "justup.db")
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT id, name FROM targets").fetchall() == [(1, "legacy")]
            assert conn.execute("SELECT value FROM settings WHERE key='migration-marker'").fetchone()[0] == "preserved"
        finally:
            conn.close()

    def test_second_start_does_not_change_restored_data(self, env, tmp_path: Path):
        self._restore(env, tmp_path)
        conn = sqlite_db.connect(env[2] / "sqlite" / "justup.db")
        try:
            sqlite_db.init_schema(conn)
            assert sqlite_db.get_schema_version(conn) == 1
            assert conn.execute("SELECT name FROM targets").fetchone()[0] == "legacy"
        finally:
            sqlite_db.close_connection(conn)

    def test_failed_swap_leaves_live_database_untouched(self, env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        live = env[2] / "sqlite" / "justup.db"
        before = live.read_bytes()

        def failing_swap(*_a, **_k):
            raise backup_mod.RestoreFailedError("restore failed", rolled_back=True)

        monkeypatch.setattr(settings_api, "apply_restore", failing_swap)
        archive = _archive(tmp_path / "v10.tar.bz2", _db_bytes(sqlite_db.LEGACY_SCHEMA_VERSION))
        name, payload = _named(archive)
        response = _post(env, name, payload)
        assert response.status_code == 500
        assert "rollback successful" in response.json()["detail"]
        assert live.read_bytes() == before
        assert not maintenance.is_active()
