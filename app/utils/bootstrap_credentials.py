#!/usr/bin/env python3
#
# app/utils/bootstrap_credentials.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Retrievable copy of the initial admin password, kept only until it was changed.

The generated bootstrap password is shown on every start until the admin changed it, so the
plaintext must survive restarts. It lives in one file next to (not inside) the backed-up
data directories, created with mode 0600, and is removed as soon as the password changes.
It is never written to the database, the logging handlers or an API response; the file is
only trusted while it is a private regular file of the service user.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import stat
from pathlib import Path

_log = logging.getLogger(__name__)

FILE_NAME = ".bootstrap-admin-credentials"
_MAX_BYTES = 1024

_path: Path | None = None


def configure(data_dir: Path | None) -> Path | None:
    """Set the credential file location (``<data_dir>/FILE_NAME``) for this process."""
    global _path
    _path = None if data_dir is None else Path(data_dir) / FILE_NAME
    return _path


def configured_path() -> Path | None:
    return _path


def store(username: str, password: str, path: Path | None = None) -> bool:
    """Atomically write the credentials with mode 0600; return False (and log) on failure."""
    target = path or _path
    if target is None:
        return False
    tmp = target.with_name(f"{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        # O_EXCL + 0600: never follows a planted link and is never readable by others.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"username": username, "password": password}, fh)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(target)
    except OSError as exc:
        # replace() can fail after the plaintext was written; never leave the temp file behind.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        _log.warning(
            "Could not persist the bootstrap credentials (%s); they are only shown now, use justup-cli if lost",
            type(exc).__name__,
        )
        return False
    return True


def load(path: Path | None = None) -> tuple[str, str] | None:
    """Return (username, password) from a trusted credential file, else None."""
    target = path or _path
    if target is None:
        return None
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        if hasattr(os, "geteuid") and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            _log.warning("Ignoring the bootstrap credential file: it must be private and owned by the service user")
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            data = json.load(fh)
        username, password = data["username"], data["password"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    return username, password


def discard(path: Path | None = None) -> None:
    """Remove the credential file (idempotent)."""
    target = path or _path
    if target is not None:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
            # Leftovers of a store() killed before its rename also hold the plaintext.
            for leftover in target.parent.glob(f"{target.name}.*.tmp"):
                leftover.unlink(missing_ok=True)


def stored_username(path: Path | None = None) -> str | None:
    """Username of the pending credentials, or None when nothing is pending."""
    target = path or _path
    if target is None or not target.exists():
        return None
    creds = load(target)
    return creds[0] if creds else None
