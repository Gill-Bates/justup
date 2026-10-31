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
import logging
import os
import re
import shutil
import sqlite3
import tarfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .crypto import derive_domain_keys

_log = logging.getLogger(__name__)

BACKUP_DIRECTORIES = ("sqlite", "tsdb", "pdf_reports")
BACKUP_HMAC_SECRET_ENV = "JUSTUP_BACKUP_HMAC_SECRET"  # noqa: S105 - env var name, not a secret value
_HMAC_INFO = b"justup/backup-hmac/v1"

MAX_BACKUP_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # compressed upload
MAX_UNPACKED_BYTES = 40 * 1024 * 1024 * 1024  # sum of member sizes
MAX_MEMBERS = 500_000

BACKUP_NAME_RE = re.compile(r"^justup_backup_\d{8}_\d{6}_([a-f0-9]{64})\.tar\.bz2$")
LEGACY_NAME_RE = re.compile(r"^justup_backup_\d{8}_\d{6}_[a-f0-9]{16}\.tar\.bz2$")


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
        if entry.name.endswith(("-wal", "-shm", "-journal")) or not entry.is_file():
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
        conn = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
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


def verify_extracted_databases(extracted_data: Path) -> None:
    """Integrity-check every SQLite database of an extracted backup; a valid HMAC does not imply a valid database."""
    sqlite_dir = extracted_data / "sqlite"
    if not sqlite_dir.is_dir():
        return
    for db_file in sorted(sqlite_dir.glob("*.db")):
        check_sqlite_integrity(db_file)


def validate_members(
    members: Iterable[tarfile.TarInfo], *, max_members: int = MAX_MEMBERS, max_bytes: int = MAX_UNPACKED_BYTES
) -> None:
    """Reject archives with too many/large members, links, special files or unexpected paths."""
    total = 0
    for count, member in enumerate(members, start=1):
        if count > max_members:
            raise BackupError("Backup corrupt! Cannot be imported. (Too many archive entries)")
        if not (member.isreg() or member.isdir()):
            raise BackupError("Backup corrupt! Cannot be imported. (Links or special files are not allowed)")
        path = PurePosixPath(member.name)
        parts = path.parts
        if path.is_absolute() or ".." in parts or not parts or parts[0] != "data":
            raise BackupError("Backup corrupt! Cannot be imported. (Unsafe path in archive)")
        if len(parts) > 1 and parts[1] not in BACKUP_DIRECTORIES:
            raise BackupError("Backup corrupt! Cannot be imported. (Unexpected content in archive)")
        if len(parts) <= 2 and member.isreg():
            # "data" and "data/<backup dir>" must be directories, never files
            raise BackupError("Backup corrupt! Cannot be imported. (Invalid archive structure)")
        total += member.size
        if total > max_bytes:
            raise BackupError("Backup corrupt! Cannot be imported. (Unpacked size exceeds the limit)")


def extract_backup(
    archive: Path, dest: Path, *, max_members: int = MAX_MEMBERS, max_bytes: int = MAX_UNPACKED_BYTES
) -> Path:
    """Validate and extract a backup archive into ``dest``; return the extracted ``data`` directory."""
    try:
        with tarfile.open(archive, mode="r:bz2") as tar:
            validate_members(tar, max_members=max_members, max_bytes=max_bytes)
        with tarfile.open(archive, mode="r:bz2") as tar:
            tar.extractall(dest, filter="data")
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        _log.error("Failed to read backup archive: %s", exc)
        raise BackupError("Backup corrupt! Cannot be imported. (Extraction failed)") from exc
    extracted = dest / "data"
    if not extracted.is_dir():
        raise BackupError("Backup corrupt! Cannot be imported. (No data directory found)")
    verify_extracted_databases(extracted)
    return extracted


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
