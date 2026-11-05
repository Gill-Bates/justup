#!/usr/bin/env python3
#
# app/utils/backup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Backup integrity (HMAC-SHA256), safe archive validation/extraction and directory swap.

All functions here are blocking; async callers run them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ..db import sqlite as sqlite_db
from .crypto import derive_domain_keys

_log = logging.getLogger(__name__)

BACKUP_DIRECTORIES = ("sqlite", "tsdb", "pdf_reports")
# The application database below the archive's "data" root and below a staging directory.
APP_DB_RELPATH = PurePosixPath("sqlite/justup.db")
_APP_DB_ARCHIVE_PATH = (PurePosixPath("data") / APP_DB_RELPATH).as_posix()
# Schema object categories of manifest format_version 1; a new set needs a new format version.
SCHEMA_CATEGORIES_V1 = ("tables", "indexes", "views", "triggers")
BACKUP_HMAC_SECRET_ENV = "JUSTUP_BACKUP_HMAC_SECRET"  # noqa: S105 - env var name, not a secret value
_HMAC_INFO = b"justup/backup-hmac/v1"

MAX_BACKUP_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # compressed upload
MAX_UNPACKED_BYTES = 40 * 1024 * 1024 * 1024  # sum of member sizes
MAX_MEMBERS = 500_000
MAX_MANIFEST_BYTES = 64 * 1024
MANIFEST_PATH = "data/backup-manifest.json"

BACKUP_NAME_RE = re.compile(r"^justup_backup_\d{8}_\d{6}_([a-f0-9]{64})\.tar\.bz2$")
LEGACY_NAME_RE = re.compile(r"^justup_backup_\d{8}_\d{6}_[a-f0-9]{16}\.tar\.bz2$")
SQLITE_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")  # a snapshot is self-contained; side files never belong in a backup
# Numeric release parts plus an optional known pre-release tag or "+build" metadata. Anything else
# (".post1", "-1", free text) cannot be ordered safely.
_RELEASE_RE = re.compile(
    r"(?P<numbers>\d+(?:\.\d+)*)"
    r"(?:(?P<pre>[-._]?(?:alpha|beta|preview|pre|rc|dev|a|b|c)[-._]?\d*)|\+[0-9A-Za-z._-]+)?\Z"
)
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")


def _connect_snapshot_readonly(db_file: Path) -> sqlite3.Connection:
    """Open a snapshot read-only and immutable, so inspecting it can neither write nor create side files."""
    return sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _schema_metadata(conn: sqlite3.Connection) -> tuple[int, dict[str, list[dict[str, str]]]]:
    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    objects: dict[str, list[dict[str, str]]] = {name: [] for name in SCHEMA_CATEGORIES_V1}
    for kind, name, table_name, definition in conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE type IN ('table', 'index', 'view', 'trigger')
          AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'
        ORDER BY type, name
        """
    ):
        if definition is None:
            continue
        objects[f"{kind}s" if kind != "index" else "indexes"].append(
            {
                "name": name,
                "table": table_name,
                "definition_sha256": hashlib.sha256(definition.encode("utf-8")).hexdigest(),
            }
        )
    return schema_version, objects


def create_backup_manifest(db_file: Path) -> dict[str, object]:
    """Describe the actual application schema without exporting table data."""
    from .version import get_build_info, get_version

    try:
        conn = _connect_snapshot_readonly(db_file)
        try:
            schema_version, schema = _schema_metadata(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError("Backup failed: could not read the SQLite snapshot schema") from exc
    schema_digest = hashlib.sha256(_canonical_json({"schema_version": schema_version, "schema": schema})).hexdigest()
    return {
        "format_version": 1,
        "application": {"name": "justup", "version": get_version(), "build_id": get_build_info()},
        "database": {
            "engine": "sqlite",
            "schema_version": schema_version,
            "schema_sha256": schema_digest,
            "schema": schema,
        },
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def serialize_backup_manifest(db_file: Path) -> bytes:
    """Return deterministic UTF-8 manifest bytes for a SQLite snapshot."""
    return _canonical_json(create_backup_manifest(db_file)) + b"\n"


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate_backup_manifest(raw: bytes) -> dict[str, object]:
    """Parse and validate the bounded, versioned backup format contract."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest exceeds the size limit)")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest is invalid JSON)") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"format_version", "application", "database", "created_at"}:
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest fields are invalid)")
    if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
        raise BackupError("Backup incompatible! Manifest format version is not supported.")
    application = manifest["application"]
    if (
        not isinstance(application, dict)
        or set(application) != {"name", "version", "build_id"}
        or application.get("name") != "justup"
        or not all(isinstance(application.get(key), str) and application[key] for key in ("version", "build_id"))
        or len(application["version"]) > 128
        or len(application["build_id"]) > 128
    ):
        raise BackupError("Backup corrupt! Cannot be imported. (Application metadata is invalid)")
    database = manifest["database"]
    if (
        not isinstance(database, dict)
        or set(database) != {"engine", "schema_version", "schema_sha256", "schema"}
        or database.get("engine") != "sqlite"
        or type(database.get("schema_version")) is not int
        or database["schema_version"] < 1
        or not isinstance(database.get("schema_sha256"), str)
        or not _SHA256_RE.fullmatch(database["schema_sha256"])
    ):
        raise BackupError("Backup corrupt! Cannot be imported. (Database metadata is invalid)")
    schema = database["schema"]
    categories = SCHEMA_CATEGORIES_V1
    if not isinstance(schema, dict) or set(schema) != set(categories):
        raise BackupError("Backup corrupt! Cannot be imported. (Schema metadata is invalid)")
    for category in categories:
        entries = schema[category]
        if not isinstance(entries, list):
            raise BackupError("Backup corrupt! Cannot be imported. (Schema metadata is invalid)")
        names: list[str] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "table", "definition_sha256"}
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
                or not isinstance(entry.get("table"), str)
                or not entry["table"]
                or not isinstance(entry.get("definition_sha256"), str)
                or not _SHA256_RE.fullmatch(entry["definition_sha256"])
            ):
                raise BackupError("Backup corrupt! Cannot be imported. (Schema metadata is invalid)")
            names.append(entry["name"])
        if names != sorted(set(names)):
            raise BackupError(
                "Backup corrupt! Cannot be imported. (Schema metadata contains duplicates or is unordered)"
            )
    schema_digest = hashlib.sha256(
        _canonical_json({"schema_version": database["schema_version"], "schema": schema})
    ).hexdigest()
    if not hmac.compare_digest(schema_digest, database["schema_sha256"]):
        raise BackupError("Backup corrupt! Cannot be imported. (Schema fingerprint does not match the manifest)")
    created_at = manifest["created_at"]
    try:
        created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError as exc:
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest timestamp is invalid)") from exc
    if created is None or created.tzinfo is None or created.utcoffset() != UTC.utcoffset(created):
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest timestamp is invalid)")
    return manifest


def _compare_releases(backup_version: str, installed_version: str) -> int | None:
    """Order two release strings: -1, 0 or 1, or None when they cannot be ordered safely.

    Numeric parts compare as numbers (``1.0.10`` > ``1.0.9``, trailing zeros are ignored). A pre-release
    is older than its final release. Identical strings are equal; two different pre-releases of one
    version are not comparable.
    """
    if backup_version == installed_version:
        return 0
    backup, installed = _RELEASE_RE.match(backup_version), _RELEASE_RE.match(installed_version)
    if backup is None or installed is None:
        return None
    backup_parts = tuple(map(int, backup["numbers"].split(".")))
    installed_parts = tuple(map(int, installed["numbers"].split(".")))
    width = max(len(backup_parts), len(installed_parts))
    backup_parts += (0,) * (width - len(backup_parts))
    installed_parts += (0,) * (width - len(installed_parts))
    if backup_parts != installed_parts:
        return -1 if backup_parts < installed_parts else 1
    backup_pre, installed_pre = backup["pre"] is not None, installed["pre"] is not None
    if backup_pre and installed_pre:
        return None
    if backup_pre != installed_pre:
        return -1 if backup_pre else 1
    return 0


def validate_backup_compatibility(manifest: dict[str, object]) -> None:
    """Reject newer or unclassifiable app/schema versions before extracting an archive."""
    from .version import get_build_info, get_version

    application = manifest["application"]
    database = manifest["database"]
    backup_version = application["version"]
    installed_version = get_version()
    if backup_version == "dev" or installed_version == "dev":
        if not (
            backup_version == installed_version == "dev"
            and application["build_id"] != "dev"
            and application["build_id"] == get_build_info()
        ):
            raise BackupError("Backup incompatible! Development builds require an identical, verifiable build ID.")
    else:
        ordering = _compare_releases(backup_version, installed_version)
        if ordering is None:
            raise BackupError("Backup incompatible! Application version cannot be compared safely.")
        if ordering > 0:
            raise BackupError("Backup incompatible! It was created by a newer application version.")
    schema_version = database["schema_version"]
    if schema_version > sqlite_db.SCHEMA_VERSION and schema_version != sqlite_db.LEGACY_SCHEMA_VERSION:
        if schema_version < sqlite_db.LEGACY_SCHEMA_VERSION:
            raise BackupError(
                f"Backup incompatible! No migration path exists from schema version {schema_version}. "
                "Existing data was not changed."
            )
        raise BackupError(
            f"Backup incompatible! Its database schema version {schema_version} is newer than the supported "
            f"version {sqlite_db.SCHEMA_VERSION}. Existing data was not changed."
        )
    if schema_version != sqlite_db.SCHEMA_VERSION and sqlite_db._missing_migrations(schema_version):  # noqa: SLF001
        raise BackupError(
            f"Backup incompatible! No migration path exists from schema version {schema_version}. "
            "Existing data was not changed."
        )


class BackupError(Exception):
    """A backup is invalid, unsafe or of an unsupported format (message is user-presentable)."""


class RestoreFailedError(Exception):
    """Applying the restore failed; ``rolled_back`` tells whether the original data was restored."""

    def __init__(self, message: str, *, rolled_back: bool, rollback_dir: Path | None = None) -> None:
        super().__init__(message)
        self.rolled_back = rolled_back
        self.rollback_dir = rollback_dir


def parse_backup_filename(filename: str) -> str:
    """Return the expected HMAC hex digest from a backup filename or raise BackupError."""
    match = BACKUP_NAME_RE.match(filename)
    if match:
        return match.group(1)
    if LEGACY_NAME_RE.match(filename):
        raise BackupError(
            "Unsupported legacy backup format (truncated signature, key stored in the database). "
            "Backups from before v1.0.0 cannot be restored; create a new backup with this version."
        )
    raise BackupError("Backup corrupt! Cannot be imported. (Invalid filename format)")


class BackupConfigError(BackupError):
    """The backup signing configuration is unusable (message never contains the secret)."""


MIN_BACKUP_SECRET_CHARS = 32
MIN_BACKUP_SECRET_DISTINCT = 8


def validate_backup_secret(secret: str) -> str:
    """Reject an external backup secret that is too short or too repetitive to be a key.

    A random 32-character hex or base64 string passes; ``aaaa...`` or a short passphrase does not.
    The value is never echoed in the error.
    """
    if len(secret) < MIN_BACKUP_SECRET_CHARS or len(set(secret)) < MIN_BACKUP_SECRET_DISTINCT:
        raise BackupConfigError(
            f"{BACKUP_HMAC_SECRET_ENV} is too weak: use at least {MIN_BACKUP_SECRET_CHARS} random characters "
            f"with at least {MIN_BACKUP_SECRET_DISTINCT} distinct ones (e.g. `openssl rand -hex 32`)."
        )
    return secret


def parse_backup_secrets(raw: str) -> list[str]:
    """Split a comma-separated rotation list (``NEW,OLD``) and validate every key on its own.

    Empty entries and duplicates are rejected so a typo cannot silently drop or repeat a key.
    Messages name the position, never the value.
    """
    entries = [entry.strip() for entry in raw.split(",")]
    seen: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if not entry:
            raise BackupConfigError(f"{BACKUP_HMAC_SECRET_ENV} has an empty entry at position {position}.")
        try:
            validate_backup_secret(entry)
        except BackupConfigError as exc:
            raise BackupConfigError(f"{exc} (entry {position} of {len(entries)})") from None
        if entry in seen:
            raise BackupConfigError(f"{BACKUP_HMAC_SECRET_ENV} contains a duplicate key at position {position}.")
        seen.add(entry)
    return entries


def backup_hmac_keys() -> list[bytes]:
    """HMAC keys for backups, primary (signing) key first.

    The external override is a comma-separated rotation list ``NEW,OLD``: the first key signs, all
    verify, each is validated individually. Without it, one domain-separated key is derived per
    SECRET_KEY(S) entry, so backups signed with a rotated-out SECRET_KEYS entry still verify.
    """
    external = os.environ.get(BACKUP_HMAC_SECRET_ENV, "").strip()
    if external:
        return [secret.encode("utf-8") for secret in parse_backup_secrets(external)]
    return derive_domain_keys(_HMAC_INFO)


def compute_backup_hmacs(filepath: Path, keys: Iterable[bytes]) -> list[str]:
    """HMAC-SHA256 hex digests of a file for several keys, reading the file only once."""
    digests = [hmac.new(key, digestmod=hashlib.sha256) for key in keys]
    with Path(filepath).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            for digest in digests:
                digest.update(chunk)
    return [digest.hexdigest() for digest in digests]


def compute_backup_hmac(filepath: Path, key: bytes | None = None) -> str:
    """Full HMAC-SHA256 hex digest of a file (streaming); the first configured key unless ``key`` is given."""
    return compute_backup_hmacs(filepath, [key if key is not None else backup_hmac_keys()[0]])[0]


def verify_backup_hmac(filepath: Path, expected_hex: str) -> bool:
    """Check the digest against every configured key in one pass, without short-circuiting.

    All keys are always evaluated and compared in constant time, so neither the matching key's
    position nor the file is read more than once.
    """
    expected = expected_hex.strip().lower().encode("utf-8", errors="replace")
    matched = False
    for digest in compute_backup_hmacs(filepath, backup_hmac_keys()):
        matched |= hmac.compare_digest(digest.encode("ascii"), expected)
    return matched


def snapshot_sqlite_dir(source: Path, dest: Path) -> None:
    """Copy ``source`` into ``dest`` with every SQLite database as a transaction-consistent snapshot.

    Uses the SQLite online backup API, so concurrent writers cannot tear the copy (a plain file
    copy of a live WAL database can). ``-wal``/``-shm`` side files are not copied: the snapshot
    contains all committed data in the main file. Other files are copied as they are.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name.endswith(SQLITE_SIDE_SUFFIXES) or not entry.is_file():
            continue
        target = dest / entry.name
        if entry.suffix != ".db":
            shutil.copy2(entry, target)
            continue
        src_conn = sqlite3.connect(str(entry), timeout=30)
        try:
            dst_conn = sqlite3.connect(str(target))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        except sqlite3.Error as exc:
            _log.error("SQLite snapshot of %s failed: %s", entry.name, exc)
            raise BackupError(f"Backup failed: could not snapshot database {entry.name}") from exc
        finally:
            src_conn.close()


def snapshot_pdf_reports(source: Path, dest: Path) -> int:
    """Copy finished PDF reports into ``dest``; skip in-flight ``*.tmp`` writes.

    A file deleted by the cleanup loop between listing and copy is skipped, not fatal, so a
    concurrent sweep never fails the backup. ``shutil.copy2`` preserves mtimes, which
    ``sweep_orphan_pdfs`` uses as its age cutoff. Returns the number of files copied.
    """
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in sorted(source.iterdir()):
        if not entry.is_file() or entry.name.endswith(".tmp"):
            continue
        try:
            shutil.copy2(entry, dest / entry.name)
        except FileNotFoundError:
            continue  # cleanup loop removed it mid-copy; not fatal
        copied += 1
    return copied


def check_sqlite_integrity(db_file: Path, *, full: bool = False) -> None:
    """Raise BackupError unless the file is a SQLite database that passes quick_check (or integrity_check)."""
    pragma = "integrity_check" if full else "quick_check"
    try:
        conn = _connect_snapshot_readonly(db_file)
        try:
            rows = [row[0] for row in conn.execute(f"PRAGMA {pragma}").fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.error("Database %s is unreadable: %s", db_file.name, exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Database is not a valid SQLite file)") from exc
    if rows != ["ok"]:
        _log.error("Database %s failed %s: %s", db_file.name, pragma, rows[:3])
        raise BackupError("Backup corrupt! Cannot be imported. (Database integrity check failed)")


def check_app_schema_compatible(db_file: Path) -> None:
    """Raise BackupError unless this release can open the application database of a backup."""
    conn = None
    try:
        conn = _connect_snapshot_readonly(db_file)
        sqlite_db.check_schema_compatibility(conn)
    except sqlite_db.IncompatibleSchemaError as exc:
        _log.error("Backup database %s has an incompatible schema (%s): %s", db_file.name, exc.kind, exc)
        if exc.kind == sqlite_db.SCHEMA_CORRUPT:
            raise BackupError("Backup corrupt! Cannot be imported. (Database is not a valid SQLite file)") from exc
        stored = "unversioned" if exc.version in (None, 0) else str(exc.version)
        reason = {
            sqlite_db.SCHEMA_FUTURE: "it was written by a newer release",
            sqlite_db.SCHEMA_UNVERSIONED: "it is an unversioned pre-release database",
            sqlite_db.SCHEMA_NO_MIGRATION_PATH: "no migration path exists from it",
        }.get(exc.kind, "its table layout differs from the supported schema")
        raise BackupError(
            f"Backup incompatible! Its database schema (version {stored}) is not supported by this release "
            f"(supports version {sqlite_db.SCHEMA_VERSION}): {reason}. Existing data was not changed."
        ) from exc
    except sqlite3.Error as exc:
        _log.error("Database %s is unreadable: %s", db_file.name, exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Database is not a valid SQLite file)") from exc
    finally:
        if conn is not None:
            conn.close()


def verify_manifest_database(db_file: Path, manifest: dict[str, object]) -> None:
    """Verify that the extracted SQLite snapshot matches the signed schema metadata."""
    database = manifest["database"]
    try:
        conn = _connect_snapshot_readonly(db_file)
        try:
            schema_version, schema = _schema_metadata(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError("Backup corrupt! Cannot be imported. (Application database is unreadable)") from exc
    if schema_version != database["schema_version"] or schema != database["schema"]:
        raise BackupError("Backup corrupt! Cannot be imported. (Manifest does not match the application database)")
    schema_digest = hashlib.sha256(_canonical_json({"schema_version": schema_version, "schema": schema})).hexdigest()
    if not hmac.compare_digest(schema_digest, database["schema_sha256"]):
        raise BackupError("Backup corrupt! Cannot be imported. (Application database schema fingerprint mismatch)")


def _assert_no_foreign_objects(db_file: Path) -> None:
    """Reject triggers and views: this schema defines none, and a restored one would run SQL of the backup."""
    try:
        conn = _connect_snapshot_readonly(db_file)
        try:
            found = conn.execute("SELECT type, name FROM sqlite_schema WHERE type IN ('trigger', 'view')").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError("Backup corrupt! Cannot be imported. (Application database is unreadable)") from exc
    if found:
        _log.error(
            "Backup database holds unexpected objects: %s", ", ".join(f"{kind} {name}" for kind, name in found[:5])
        )
        raise BackupError(
            "Backup incompatible! Its application database contains triggers or views this release does not "
            "define. Existing data was not changed."
        )


def verify_extracted_databases(extracted_data: Path, manifest: dict[str, object] | None = None) -> None:
    """Check every SQLite database of an extracted backup; a valid HMAC does not imply a usable database.

    All databases must pass integrity checks; the application database must exist and match both the
    signed schema metadata and the migration path supported by this release.
    """
    app_db = extracted_data / APP_DB_RELPATH
    sqlite_dir = app_db.parent
    if not sqlite_dir.is_dir() or not app_db.is_file():
        raise BackupError("Backup corrupt! Cannot be imported. (Missing data/sqlite/justup.db)")
    if manifest is not None:
        verify_manifest_database(app_db, manifest)
    _assert_no_foreign_objects(app_db)
    for db_file in sorted(sqlite_dir.glob("*.db")):
        check_sqlite_integrity(db_file)
        if db_file == app_db:
            check_app_schema_compatible(db_file)


class _ArchiveValidator:
    """Backup archive rules applied member by member, so a stream never has to be held in memory.

    Feed every member in archive order through ``check`` and call ``finish`` after the last one.
    """

    def __init__(self, *, max_members: int, max_bytes: int) -> None:
        self._max_members = max_members
        self._max_bytes = max_bytes
        self._count = 0
        self._total = 0
        self._seen: set[str] = set()
        self._regular_files: set[str] = set()
        self._parents: set[str] = set()
        self._manifest_found = False
        self._database_found = False

    def check(self, member: tarfile.TarInfo) -> str:
        """Validate one member and return its normalized archive path."""
        self._count += 1
        if self._count > self._max_members:
            raise BackupError("Backup corrupt! Cannot be imported. (Too many archive entries)")
        if not (member.isreg() or member.isdir()):
            raise BackupError("Backup corrupt! Cannot be imported. (Links or special files are not allowed)")
        path = PurePosixPath(member.name)
        parts = path.parts
        if path.is_absolute() or ".." in parts or not parts or parts[0] != "data":
            raise BackupError("Backup corrupt! Cannot be imported. (Unsafe path in archive)")
        normalized = path.as_posix()
        if normalized in self._seen:
            raise BackupError("Backup corrupt! Cannot be imported. (Duplicate archive path)")
        self._seen.add(normalized)
        if normalized == MANIFEST_PATH:
            if not member.isreg():
                raise BackupError("Backup corrupt! Cannot be imported. (Invalid manifest entry)")
            if member.size > MAX_MANIFEST_BYTES:
                raise BackupError("Backup corrupt! Cannot be imported. (Manifest exceeds the size limit)")
            self._manifest_found = True
        elif len(parts) > 1 and parts[1] not in BACKUP_DIRECTORIES:
            raise BackupError("Backup corrupt! Cannot be imported. (Unexpected content in archive)")
        elif len(parts) <= 2 and member.isreg():
            # "data" and "data/<backup dir>" must be directories, never files
            raise BackupError("Backup corrupt! Cannot be imported. (Invalid archive structure)")
        if len(parts) > 2 and parts[1] == "sqlite" and parts[-1].endswith(SQLITE_SIDE_SUFFIXES):
            raise BackupError("Backup corrupt! Cannot be imported. (SQLite side files are not allowed)")
        if normalized == _APP_DB_ARCHIVE_PATH:
            if not member.isreg():
                raise BackupError("Backup corrupt! Cannot be imported. (Invalid application database entry)")
            self._database_found = True
        parents = {parent.as_posix() for parent in path.parents}
        if not parents.isdisjoint(self._regular_files) or (member.isreg() and normalized in self._parents):
            raise BackupError("Backup corrupt! Cannot be imported. (A file is also used as a directory)")
        self._parents |= parents
        if member.isreg():
            self._regular_files.add(normalized)
        self._total += member.size
        if self._total > self._max_bytes:
            raise BackupError("Backup corrupt! Cannot be imported. (Unpacked size exceeds the limit)")
        return normalized

    def finish(self) -> None:
        if not self._manifest_found:
            raise BackupError("Backup corrupt! Cannot be imported. (Missing backup manifest)")
        if not self._database_found:
            raise BackupError("Backup corrupt! Cannot be imported. (Missing data/sqlite/justup.db)")


def validate_members(
    members: Iterable[tarfile.TarInfo], *, max_members: int = MAX_MEMBERS, max_bytes: int = MAX_UNPACKED_BYTES
) -> None:
    """Reject archives with unsafe, duplicate, oversized or incomplete backup paths."""
    validator = _ArchiveValidator(max_members=max_members, max_bytes=max_bytes)
    for member in members:
        validator.check(member)
    validator.finish()


def _iter_members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    """Yield archive members one at a time; TarFile would otherwise keep every parsed header alive."""
    while (member := tar.next()) is not None:
        tar.members.clear()
        yield member


def _inspect_stream(raw: BinaryIO, *, max_members: int, max_bytes: int) -> dict[str, object]:
    try:
        validator = _ArchiveValidator(max_members=max_members, max_bytes=max_bytes)
        raw_manifest = b""
        with tarfile.open(fileobj=raw, mode="r:bz2") as tar:
            for member in _iter_members(tar):
                if validator.check(member) == MANIFEST_PATH:
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise BackupError("Backup corrupt! Cannot be imported. (Manifest cannot be read)")
                    with handle:
                        raw_manifest = handle.read(MAX_MANIFEST_BYTES + 1)
            validator.finish()
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        _log.error("Failed to inspect backup archive: %s", exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Archive validation failed)") from exc
    manifest = validate_backup_manifest(raw_manifest)
    validate_backup_compatibility(manifest)
    return manifest


def inspect_backup_archive(
    archive: Path, *, max_members: int = MAX_MEMBERS, max_bytes: int = MAX_UNPACKED_BYTES
) -> dict[str, object]:
    """Validate archive paths and signed metadata before the potentially large extraction."""
    try:
        with Path(archive).open("rb") as raw:
            return _inspect_stream(raw, max_members=max_members, max_bytes=max_bytes)
    except OSError as exc:
        _log.error("Failed to open backup archive: %s", exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Archive validation failed)") from exc


def _extract_stream(
    raw: BinaryIO, dest: Path, manifest: dict[str, object], *, max_members: int, max_bytes: int
) -> None:
    """Validate every member right before extracting it and require the embedded manifest to match."""
    try:
        validator = _ArchiveValidator(max_members=max_members, max_bytes=max_bytes)
        with tarfile.open(fileobj=raw, mode="r:bz2") as tar:
            for member in _iter_members(tar):
                path = validator.check(member)
                tar.extract(member, dest, filter="data")
                if path == MANIFEST_PATH and validate_backup_manifest((dest / path).read_bytes()) != manifest:
                    raise BackupError("Backup corrupt! Cannot be imported. (Manifest changed during extraction)")
            validator.finish()
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        _log.error("Failed to read backup archive: %s", exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Extraction failed)") from exc


def extract_backup(
    archive: Path,
    dest: Path,
    *,
    max_members: int = MAX_MEMBERS,
    max_bytes: int = MAX_UNPACKED_BYTES,
    manifest: dict[str, object] | None = None,
) -> Path:
    """Validate and extract a backup archive into ``dest``; return the extracted ``data`` directory.

    The archive is opened once. Without a ``manifest`` it is inspected first on that handle; either way
    every member is validated again while it is extracted, so an archive that changed after an earlier
    inspection is rejected instead of trusted.
    """
    try:
        with Path(archive).open("rb") as raw:
            if manifest is None:
                manifest = _inspect_stream(raw, max_members=max_members, max_bytes=max_bytes)
                raw.seek(0)
            _extract_stream(raw, dest, manifest, max_members=max_members, max_bytes=max_bytes)
    except OSError as exc:
        _log.error("Failed to read backup archive: %s", exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Extraction failed)") from exc
    extracted = dest / "data"
    if not extracted.is_dir():
        raise BackupError("Backup corrupt! Cannot be imported. (No data directory found)")
    verify_extracted_databases(extracted, manifest)
    return extracted


def migrate_staged_application_database(extracted_data: Path) -> tuple[int, int]:
    """Migrate and fully validate the staged application database before it can be swapped in."""
    db_file = extracted_data / APP_DB_RELPATH
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite_db.connect(db_file)
        original_version = sqlite_db.get_schema_version(conn)
        if original_version > sqlite_db.SCHEMA_VERSION and original_version != sqlite_db.LEGACY_SCHEMA_VERSION:
            raise BackupError("Backup incompatible! Its database schema is newer than this release supports.")
        if original_version != sqlite_db.SCHEMA_VERSION:
            sqlite_db.init_schema(conn)
        else:
            sqlite_db.check_schema_compatibility(conn)
        migrated_version = sqlite_db.get_schema_version(conn)
        if migrated_version != sqlite_db.SCHEMA_VERSION:
            raise BackupError("Backup migration failed on staging; existing data was not changed.")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError(
                "Backup migration failed on staging: foreign_key_check failed; existing data was not changed."
            )
        if [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()] != ["ok"]:
            raise BackupError("Staging integrity_check failed; existing data was not changed.")
        sqlite_db.check_schema_compatibility(conn)
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0 or (checkpoint[1] >= 0 and checkpoint[1] != checkpoint[2]):
            raise BackupError("Staging WAL checkpoint failed; existing data was not changed.")
    except (BackupError, sqlite_db.DatabaseQuiescedError):
        raise  # the latter means a restore already completed and the instance awaits its restart
    except Exception as exc:
        _log.error("Staged application database migration or validation failed: %s", exc)
        raise BackupError("Backup migration failed on staging; existing data was not changed.") from exc
    finally:
        closed = conn is None or sqlite_db.close_connection(conn)
    if not closed:
        raise BackupError(
            "Backup migration failed on staging: database connection did not close; existing data was not changed."
        )
    # A closed WAL database leaves no side files; a leftover one would hold data the swap does not carry.
    if any(Path(f"{db_file}{suffix}").exists() for suffix in SQLITE_SIDE_SUFFIXES):
        raise BackupError(
            "Backup migration failed on staging: SQLite side files remain; existing data was not changed."
        )
    return original_version, migrated_version


def _rename(src: Path, dst: Path) -> None:
    """Atomic rename on one filesystem; never falls back to copy (fails with EXDEV instead)."""
    src.replace(dst)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def apply_restore(extracted_data: Path, data_dir: Path) -> list[str]:
    """Swap the backup directories into ``data_dir`` with rollback; return the restored directory names.

    ``extracted_data`` must live on the same filesystem as ``data_dir`` (stage it inside
    ``data_dir``), so every step is an atomic rename. A cross-device rename fails and is rolled back.
    """
    plan: list[str] = []
    for subdir in BACKUP_DIRECTORIES:
        source = extracted_data / subdir
        if not (source.exists() or source.is_symlink()):
            continue
        if source.is_symlink() or not source.is_dir():
            raise BackupError("Backup corrupt! Cannot be imported. (Invalid archive structure)")
        plan.append(subdir)

    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    rollback_dir = data_dir / f".rollback_{stamp}_{os.getpid()}_{os.urandom(3).hex()}"
    rollback_dir.mkdir()
    moved_away: set[str] = set()  # originals now living in rollback_dir
    attempted: set[str] = set()  # targets that may hold (partially) restored data
    restored: list[str] = []
    try:
        for subdir in plan:
            target = data_dir / subdir
            if target.exists() or target.is_symlink():
                _rename(target, rollback_dir / subdir)
                moved_away.add(subdir)
            attempted.add(subdir)
            _rename(extracted_data / subdir, target)
            restored.append(subdir)
    except Exception as exc:
        _log.error("Restore failed after %s: %s. Attempting rollback...", restored or "no directory", exc)
        failures: list[Exception] = []
        for subdir in reversed(plan):
            target = data_dir / subdir
            try:
                if subdir in attempted:
                    _remove(target)  # safe: the original was moved away before this target was touched
                if subdir in moved_away:
                    _rename(rollback_dir / subdir, target)
            except Exception as rollback_error:  # noqa: BLE001 - keep rolling back the remaining directories
                failures.append(rollback_error)
        if failures:
            _log.critical("ROLLBACK FAILED: %s. Manual intervention required!", failures[0])
            raise RestoreFailedError("rollback failed", rolled_back=False, rollback_dir=rollback_dir) from failures[0]
        shutil.rmtree(rollback_dir, ignore_errors=True)
        raise RestoreFailedError("restore failed", rolled_back=True) from exc
    shutil.rmtree(rollback_dir, ignore_errors=True)
    _log.info("Restore applied: %s", ", ".join(restored) or "nothing to restore")
    return restored
