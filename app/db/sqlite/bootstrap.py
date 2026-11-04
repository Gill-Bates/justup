#!/usr/bin/env python3
#
# app/db/sqlite/bootstrap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bootstrap admin creation on first start."""

from __future__ import annotations

import logging
import secrets
import sqlite3
import sys
from pathlib import Path

from ...models.users import hash_password, verify_password
from ...utils import bootstrap_credentials
from ...utils.time import utcnow
from .connection import transaction
from .users import get_user_by_username

# Keep the pre-split logger name: log filters and caplog assertions address app.db.sqlite.
_log = logging.getLogger("app.db.sqlite")


# ---------------------------------------------------------------------------
# Bootstrap admin (first start only)
# ---------------------------------------------------------------------------

BOOTSTRAP_ADMIN_USERNAME = "admin"


_PRONOUNCEABLE_VOWELS = "aeiou"


_PRONOUNCEABLE_CONSONANTS = "bcdfghjklmnpqrstvwxyz"


_BOOTSTRAP_SPECIALS = "!?#$%&*+-=@"


def _print_bootstrap_credentials(username: str, password: str) -> None:
    """Write the bootstrap credentials to stdout, bypassing the logging handlers."""
    banner = (
        "\n"
        "======================================================================\n"
        " justUp! bootstrap admin (shown on every start until the password is changed)\n"
        f"   Username: {username}\n"
        f"   Password: {password}\n"
        " Sign in and change this password to continue.\n"
        "======================================================================\n"
    )
    sys.stdout.write(banner)
    sys.stdout.flush()


def reprint_pending_bootstrap_credentials(conn: sqlite3.Connection) -> bool:
    """Show the initial admin password again while it is still the valid, unchanged one.

    The stored plaintext is only trusted when it still matches the account's hash and the
    account still must change its password; otherwise (changed via API, CLI or a restore)
    the file is stale and removed without printing. Returns True if the banner was printed.
    """
    creds = bootstrap_credentials.load()
    if creds is None:
        bootstrap_credentials.discard()  # missing is a no-op; unreadable or malformed is never trusted
        return False
    username, password = creds
    user = get_user_by_username(conn, username)
    if user is None or not user["must_change_password"] or not verify_password(password, user["password_hash"]):
        bootstrap_credentials.discard()
        return False
    _print_bootstrap_credentials(username, password)
    return True


def _generate_pronounceable_password() -> str:
    """Generate a short one-time password in 'Tujoza47!' style.

    Pattern: 3 consonant-vowel syllables (6 letters, first one capitalised),
    2 digits and 1 special character from a small, easily typed set (9 chars).
    ``must_change_password=1`` forces an immediate change. Randomness comes
    only from the ``secrets`` module.
    """
    letters = "".join(
        secrets.choice(_PRONOUNCEABLE_CONSONANTS) + secrets.choice(_PRONOUNCEABLE_VOWELS) for _ in range(3)
    )
    digits = f"{secrets.randbelow(100):02d}"
    return letters.capitalize() + digits + secrets.choice(_BOOTSTRAP_SPECIALS)


def ensure_default_admin(conn: sqlite3.Connection, credentials_path: Path | None = None) -> bool:
    """Create the bootstrap admin on first start when no users exist.

    Generates a random pronounceable password, stores only its hash with
    ``must_change_password = 1`` and prints the plaintext to stdout after the
    insert is committed. The plaintext also goes to a private 0600 file (see
    ``utils.bootstrap_credentials``) so every later start can show it again until
    the password was changed; it never reaches the database or the logging
    handlers. Returns True if the admin was created.
    """
    password: str | None = None
    try:
        # IMMEDIATE takes the write lock first so concurrent workers serialise on the user count.
        with transaction(conn, immediate=True):
            (count,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            if count == 0:
                password = _generate_pronounceable_password()
                conn.execute(
                    "INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, "
                    "must_change_password, created_at) VALUES (?, ?, 1, 1, 1, 1, ?)",
                    (BOOTSTRAP_ADMIN_USERNAME, hash_password(password), utcnow()),
                )
                # Written before the commit: a file without a committed account is stale and ignored.
                bootstrap_credentials.store(BOOTSTRAP_ADMIN_USERNAME, password, credentials_path)
    except sqlite3.IntegrityError:
        _log.debug("Default admin insert skipped because a user already exists")
        return False
    if password is None:
        return False
    _print_bootstrap_credentials(BOOTSTRAP_ADMIN_USERNAME, password)
    _log.info("Bootstrap admin '%s' created; the initial password was printed to the console", BOOTSTRAP_ADMIN_USERNAME)
    return True
