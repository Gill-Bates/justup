#!/usr/bin/env python3
#
# app/services/notifications/signal_cli_backend.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal backend implementation using signal-cli subprocess.

Design principles:
- subprocess.run with capture_output=True, timeout
- No shell mode
- Explicit exit code evaluation
- Defensive timeout defaults (10s)

Example commands:
    signal-cli --config /app/data/signal -u <sender> send <recipient> -m <text>
    signal-cli --config /app/data/signal link -n justUp
    signal-cli --config /app/data/signal listAccounts
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Final

from .signal_backend import SignalBackend
from .signal_errors import (
    SignalNotRegistered,
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalSendFailed,
    SignalTimeout,
    SignalUnavailable,
)
from .signal_types import SignalStatus

_log = logging.getLogger(__name__)

# ─── Configuration ──────────────────────────────────────────────────────────────

# Default paths (can be overridden via environment)
DEFAULT_SIGNAL_CLI_PATH: Final[str] = "/usr/local/bin/signal-cli"
DEFAULT_SIGNAL_DATA_DIR: Final[str] = "/app/data/signal"

# Legacy signal-cli config locations (older images/configs).
# Used to preserve linking across upgrades when the config dir changed.
LEGACY_SIGNAL_DATA_DIRS: Final[tuple[str, ...]] = (
    "/app/data/signal-cli",
    "/app/data/.local/share/signal-cli",
)

# Timeouts (seconds)
DEFAULT_TIMEOUT: Final[int] = 10
QR_TIMEOUT: Final[int] = 30  # QR generation can be slow
REGISTER_TIMEOUT: Final[int] = 30  # Registration requests external servers
VERIFY_TIMEOUT: Final[int] = 20  # Verification can be slow
PROFILE_TIMEOUT: Final[int] = 20  # Profile update with avatar upload

# Rate limiting: max 1 message per MIN_SEND_INTERVAL seconds
# Prevents overwhelming signal-cli with rapid-fire alerts
MIN_SEND_INTERVAL: Final[float] = 1.0  # seconds between sends

# Status cache TTL: how long to cache status() results
# This prevents blocking the event loop with repeated signal-cli subprocess calls
# when multiple UI fragments request status simultaneously
STATUS_CACHE_TTL: Final[float] = 10.0  # seconds

# Phone number validation (strict E.164: max 15 digits total including country code)
# Pattern: + followed by 1-9, then 6-14 more digits (total 7-15 digits after +)
PHONE_REGEX: Final[re.Pattern] = re.compile(r'^\+[1-9]\d{6,14}$')

# Regex for extracting phone numbers from any text (for robust parsing)
PHONE_EXTRACT_REGEX: Final[re.Pattern] = re.compile(r'\+[1-9]\d{7,14}')

# Global process lock: signal-cli uses file-level locking on its data directory,
# so only one signal-cli subprocess can run at a time.  Without this, concurrent
# calls (e.g. send_message + check_user_registered) race for the file lock and
# the loser times out.  A threading.Lock serialises all subprocess invocations.
_CLI_PROCESS_LOCK: Final[threading.Lock] = threading.Lock()


def _mask_phone(number: str) -> str:
    """Mask phone number for GDPR-compliant logging."""
    if not number or len(number) < 6:
        return "***"
    return number[:3] + "****" + number[-2:]


def _sanitize_cli_arg(value: str, default: str = "") -> str:
    """Sanitize user input for signal-cli to prevent argument injection.
    
    Args:
        value: User input to sanitize
        default: Default value if input is invalid
        
    Returns:
        Sanitized value, or default if input starts with '-' or contains null bytes
        
    Note:
        While subprocess list mode (no shell=True) prevents shell injection,
        signal-cli could misinterpret values starting with '-' as flags.
        Null bytes could truncate strings in C-based argument parsing.
    """
    if not value or "\x00" in value or value.strip().startswith("-"):
        return default
    return value


def _get_signal_cli_path() -> str:
    """Get signal-cli binary path from environment or default."""
    return os.environ.get("SIGNAL_CLI_PATH", DEFAULT_SIGNAL_CLI_PATH)


def _get_signal_data_dir() -> str:
    """Get signal-cli data directory from environment or default."""
    return os.environ.get("SIGNAL_DATA_DIR", DEFAULT_SIGNAL_DATA_DIR)


def _get_sender_number() -> str | None:
    """Get configured sender number from environment."""
    return os.environ.get("SIGNAL_SENDER_NUMBER")


def _signal_accounts_count(data_dir: Path) -> int:
    """Return number of configured accounts in a signal-cli config dir.
    
    Validates basic schema: accounts must be a list of dicts with 'number' key.
    """
    try:
        accounts_file = data_dir / "data" / "accounts.json"
        if not accounts_file.is_file():
            return 0
        payload = json.loads(accounts_file.read_text(encoding="utf-8"))
        accounts = payload.get("accounts", [])
        if not isinstance(accounts, list):
            return 0
        # Basic schema validation: each account should be a dict with 'number'
        valid_count = sum(
            1 for acc in accounts
            if isinstance(acc, dict) and "number" in acc
        )
        return valid_count
    except Exception:
        return 0


def _maybe_migrate_legacy_signal_data_dir(target_dir: Path) -> None:
    """Migrate legacy signal-cli config into the current data dir (best-effort)."""
    if _signal_accounts_count(target_dir) > 0:
        return

    for legacy in LEGACY_SIGNAL_DATA_DIRS:
        legacy_dir = Path(legacy)
        if legacy_dir == target_dir:
            continue
        if _signal_accounts_count(legacy_dir) <= 0:
            continue

        try:
            if target_dir.exists() and any(target_dir.iterdir()):
                backup_dir = target_dir.with_name(f"{target_dir.name}.backup-{int(time.time())}")
                # Use shutil.move instead of rename() to handle cross-filesystem moves
                shutil.move(str(target_dir), str(backup_dir))
                _log.info("Backed up empty Signal data dir to: %s", backup_dir)

            shutil.copytree(legacy_dir, target_dir, dirs_exist_ok=True)
            _log.info("Migrated legacy Signal data dir from %s to %s", legacy_dir, target_dir)
        except Exception as e:
            _log.warning("Failed to migrate legacy Signal data dir (%s -> %s): %s", legacy_dir, target_dir, e)
        break


def _find_avatar() -> str | None:
    """Find avatar.png in default locations.
    
    Returns:
        Path to avatar.png if found, None otherwise
    """
    possible_paths = [
        Path(__file__).parent.parent.parent / "static" / "avatar.png",  # Relative to app/
        Path("/app/static/avatar.png"),  # Docker container
        Path("/app/app/static/avatar.png"),  # Docker container (workdir layout)
    ]
    
    for p in possible_paths:
        if p.is_file():
            _log.debug("Found avatar at: %s", p)
            return str(p)
    
    _log.debug("No avatar found in default locations")
    return None


class SignalCliBackend(SignalBackend):
    """Signal backend using local signal-cli subprocess.
    
    Status is cached for STATUS_CACHE_TTL seconds to prevent blocking the event loop
    with repeated signal-cli subprocess calls. State-changing operations (link, unlink,
    register, verify) automatically invalidate the cache.
    """
    
    def __init__(
        self,
        cli_path: str | None = None,
        data_dir: str | None = None,
        sender_number: str | None = None,
    ):
        self._cli_path = cli_path or _get_signal_cli_path()
        self._data_dir = data_dir or _get_signal_data_dir()
        self._sender = sender_number or _get_sender_number()
        
        # Active link process for this instance (thread-safe per worker)
        self._link_process: subprocess.Popen | None = None
        self._link_session_id: str | None = None  # Track link session for race prevention
        
        # Lock for operations that modify signal-cli state (link, unregister)
        # Prevents race conditions when multiple requests hit the same backend
        self._state_lock = threading.Lock()
        
        # Rate limiting: track last send time per sender to prevent overwhelming signal-cli
        # Using dict allows multiple sender accounts without blocking each other
        self._last_send_times: dict[str, float] = {}
        self._send_lock = threading.Lock()
        
        # Status cache: prevents blocking event loop with repeated signal-cli calls
        # when multiple UI fragments request status simultaneously
        self._status_cache: SignalStatus | None = None
        self._status_cache_time: float = 0.0
        self._status_cache_lock = threading.Lock()
        self._status_fetching: bool = False  # Track fetch in progress for thundering herd prevention
        
        # Track link completion state separately from process state
        self._link_completed: bool = False
        
        # Holds CLI lock during linking to prevent concurrent signal-cli processes
        self._holds_cli_lock: bool = False
        
        # Subprocess environment (build once, reuse for all calls)
        self._subprocess_env = os.environ.copy()
        self._subprocess_env.update({
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        })
        
        # Ensure data directory exists; keep linking persistent across upgrades.
        _maybe_migrate_legacy_signal_data_dir(Path(self._data_dir))
        Path(self._data_dir).mkdir(parents=True, exist_ok=True)
    
    def __del__(self) -> None:
        """Cleanup: Terminate any lingering link process.
        
        Note: __del__ is not guaranteed to be called (GC, shutdown, forks).
        For reliable cleanup, use close() explicitly or integrate with
        app lifecycle (e.g., FastAPI shutdown event).
        """
        # Guard against __init__ failures (e.g., if _link_process wasn't set)
        if hasattr(self, '_link_process'):
            self._cleanup_link_process()
    
    def close(self) -> None:
        """Explicitly cleanup resources. Call on app shutdown."""
        self._cleanup_link_process()
    
    def _cleanup_link_process(self) -> None:
        """Terminate any running link process and close its pipes to prevent resource leaks."""
        if self._link_process is not None:
            try:
                # Check if process is still running
                if self._link_process.poll() is None:
                    _log.debug("Cleaning up lingering link process (PID %s)", self._link_process.pid)
                    self._link_process.terminate()
                    try:
                        self._link_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._link_process.kill()
                        self._link_process.wait(timeout=1)
                
                # Explicitly close pipes to prevent resource leaks
                if self._link_process.stdout:
                    self._link_process.stdout.close()
                if self._link_process.stderr:
                    self._link_process.stderr.close()
                    
            except Exception as e:
                _log.warning("Error cleaning up link process: %s", e)
            finally:
                self._link_process = None
    
    def _resolve_cli(self) -> str:
        """Resolve signal-cli binary path.
        
        Returns:
            Absolute path to signal-cli binary
            
        Raises:
            SignalUnavailable: if binary not found
        """
        cli = Path(self._cli_path)
        if cli.is_absolute() and cli.is_file():
            return str(cli)
        resolved = shutil.which(self._cli_path)
        if resolved:
            return resolved
        raise SignalUnavailable(f"signal-cli not found: {self._cli_path}")
    
    def _run_signal_cli(
        self,
        args: list[str],
        timeout: int = DEFAULT_TIMEOUT,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run signal-cli with given arguments.
        
        Args:
            args: Command arguments (without signal-cli binary)
            timeout: Timeout in seconds
            check: If True, raise on non-zero exit code
            text: If True, decode output as UTF-8 text; if False, return raw bytes
            
        Returns:
            CompletedProcess with stdout/stderr
            
        Raises:
            SignalUnavailable: if binary not found
            SignalTimeout: if command times out
        """
        resolved = self._resolve_cli()
        
        cmd = [
            resolved,
            "--config", self._data_dir,
            *args,
        ]
        
        _log.debug("Running signal-cli: %s", " ".join(cmd[:4]) + " ...")
        
        try:
            with _CLI_PROCESS_LOCK:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    text=text,
                    encoding="utf-8" if text else None,  # Explicit UTF-8 for emoji/unicode support
                    env=self._subprocess_env,
                    check=False,  # We handle errors ourselves
                )
            
            if result.returncode != 0 and check:
                if text:
                    stderr = result.stderr.strip() if result.stderr else "Unknown error"
                else:
                    stderr = result.stderr.decode(errors="replace").strip() if result.stderr else "Unknown error"
                _log.warning("signal-cli failed (exit %d): %s", result.returncode, stderr[:100])
            
            return result
            
        except subprocess.TimeoutExpired as e:
            raise SignalTimeout(f"signal-cli timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise SignalUnavailable(f"signal-cli not found: {e}") from e
        except OSError as e:
            raise SignalUnavailable(f"Failed to run signal-cli: {e}") from e
    
    def _run_signal_cli_json(
        self,
        args: list[str],
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict | list | None:
        """Run signal-cli with JSON output and parse the result.
        
        Args:
            args: Command arguments (account-specific args like -u should be included)
            timeout: Timeout in seconds
            
        Returns:
            Parsed JSON (dict or list), or None if empty/unparseable
            
        Note: --output=json is a global flag, must come before -u and subcommand.
        """
        # --output=json is a global flag, must come before -u and subcommand
        json_args = ["--output=json"] + args
        
        result = self._run_signal_cli(json_args, timeout=timeout, check=False)
        
        if result.returncode != 0:
            return None
        
        stdout = result.stdout.strip() if result.stdout else ""
        if not stdout:
            return None
        
        # signal-cli with --output=json outputs JSON on stdout, logs on stderr
        # However, some versions may mix output. Try robust parsing:
        # 1. Try parsing entire output first (fast path)
        # 2. If that fails, scan line by line for lines starting with { or [
        
        # Fast path: try parsing entire output
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            pass
        
        # Slow path: scan line by line for JSON-like content
        # Look for lines starting with { or [ (after stripping whitespace)
        for line in stdout.split('\n'):
            stripped = line.strip()
            if stripped.startswith(('{', '[')):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    continue
        
        # Fallback: try finding first { or [ in entire string (original logic)
        # This can match characters inside log messages, but better than failing
        json_start_bracket = stdout.find('[')
        json_start_brace = stdout.find('{')
        
        if json_start_bracket == -1 and json_start_brace == -1:
            _log.debug("No JSON found in signal-cli output: %r", stdout[:100])
            return None
        
        if json_start_bracket == -1:
            json_start = json_start_brace
        elif json_start_brace == -1:
            json_start = json_start_bracket
        else:
            json_start = min(json_start_bracket, json_start_brace)
        
        json_str = stdout[json_start:]
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            _log.debug("Failed to parse signal-cli JSON output: %s", e)
            return None
    
    def _delete_account_cache(self, number: str) -> None:
        """Delete local cache directory for a specific account.
        
        Args:
            number: Phone number in E.164 format
            
        This manually removes the account's data directory from disk.
        Used when signal-cli's deleteLocalAccountData doesn't work or after unregister.
        """
        # Signal-cli stores data in: {data_dir}/data/{number}/
        account_dir = Path(self._data_dir) / "data" / number
        
        if account_dir.exists():
            try:
                shutil.rmtree(account_dir)
                _log.info("Deleted local cache directory for %s", _mask_phone(number))
            except OSError as e:
                _log.warning("Failed to delete cache directory for %s: %s", _mask_phone(number), e)
        else:
            _log.debug("No cache directory found for %s", _mask_phone(number))
    
    
    def invalidate_cache(self) -> None:
        """Invalidate the status cache.
        
        This method provides a public API for invalidating the internal status cache.
        Useful when external code knows that Signal state has changed.
        """
        self._invalidate_status_cache()
    
    def status(self, force_refresh: bool = False) -> SignalStatus:
        """Get current Signal status, with caching to prevent event loop blocking.
        
        Args:
            force_refresh: If True, bypass cache and fetch fresh status.
        
        Caches status for STATUS_CACHE_TTL seconds to prevent blocking the event loop
        with repeated signal-cli subprocess calls when multiple UI fragments request
        status simultaneously. State-changing operations (link, unlink, register)
        automatically invalidate the cache.
        """
        with self._status_cache_lock:
            now = time.monotonic()
            cache_age = now - self._status_cache_time
            
            # Return cached status if valid and not forcing refresh
            if (
                not force_refresh
                and self._status_cache is not None
                and cache_age < STATUS_CACHE_TTL
            ):
                _log.debug("Signal status: returning cached (age=%.1fs)", cache_age)
                return self._status_cache
        
        # Fetch fresh status (outside lock to allow concurrent requests to wait)
        try:
            fresh_status = self._fetch_status_uncached()
        except (SignalTimeout, SignalUnavailable) as e:
            _log.warning("Signal status check failed: %s", e)
            fresh_status = SignalStatus(
                reachable=False,
                registered=False,
                linked=False,
                accounts=[],
                error=str(e),
            )
        
        # Update cache
        with self._status_cache_lock:
            self._status_cache = fresh_status
            self._status_cache_time = time.monotonic()
        
        return fresh_status
    
    def _invalidate_status_cache(self) -> None:
        """Invalidate status cache after state-changing operations."""
        with self._status_cache_lock:
            self._status_cache = None
            self._status_cache_time = 0.0
    
    def _fetch_status_uncached(self) -> SignalStatus:
        """Fetch fresh status from signal-cli (no caching).
        
        Performs a lightweight server-side validation to detect deregistered accounts.
        """
        # Check if signal-cli is available (quick version check)
        try:
            version_result = self._run_signal_cli(["--version"], timeout=5, check=False)
            if version_result.returncode != 0:
                return SignalStatus(
                    reachable=False,
                    registered=False,
                    linked=False,
                    accounts=[],
                    error="signal-cli not working",
                )
        except (SignalUnavailable, SignalTimeout) as e:
            raise  # Propagate for fallback handling
        
        # Get accounts from local config
        try:
            accounts = self.list_accounts()
            if not accounts:
                return SignalStatus(
                    reachable=True,
                    registered=False,
                    linked=False,
                    accounts=[],
                    error=None,
                )
            
            # Validate first account against Signal's servers
            # Uses quick receive with 1s timeout to check if account is still valid
            # This catches the "User X is not registered" error from Signal
            first_account = accounts[0]
            is_valid = self._validate_account_server_side(first_account)
            
            if not is_valid:
                _log.warning(
                    "Account %s exists locally but is not registered with Signal servers",
                    _mask_phone(first_account)
                )
                return SignalStatus(
                    reachable=True,
                    registered=False,
                    linked=False,
                    accounts=[_mask_phone(a) for a in accounts],
                    error="Account not registered with Signal (re-link required)",
                )
            
            return SignalStatus(
                reachable=True,
                registered=True,
                linked=True,
                accounts=[_mask_phone(a) for a in accounts],
                error=None,
            )
        except (SignalUnavailable, SignalTimeout) as e:
            raise  # Propagate for fallback handling
    
    def _validate_account_server_side(self, account: str) -> bool:
        """Validate account against Signal's servers.
        
        Uses 'listDevices' to check if the account is still registered.
        Signal returns "User X is not registered" if the account has been
        deregistered server-side.
        
        Args:
            account: Phone number to validate
            
        Returns:
            True if account is valid, False if deregistered
        """
        try:
            # Use listDevices - faster and more reliable than receive
            result = self._run_signal_cli(
                ["-u", account, "listDevices"],
                timeout=10,
                check=False,
            )
            
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""
            combined = f"{stderr} {stdout}".lower()
            
            if "not registered" in combined:
                return False
            
            # Any other result (success, device list) means account is valid
            return True
            
        except (SignalTimeout, SignalUnavailable):
            # On timeout/unavailable, assume account is valid (don't block on slow signal-cli)
            _log.debug("Account validation timed out for %s, assuming valid", _mask_phone(account))
            return True
    
    def send_message(self, recipient: str, message: str, sender: str | None = None) -> None:
        """Send a message via signal-cli.
        
        Args:
            recipient: Phone number to send to (E.164 format)
            message: Message text
            sender: Sender phone number (optional, uses first available account if not valid)
            
        Rate-limited: Enforces minimum interval between sends to prevent
        overwhelming signal-cli with rapid-fire alerts.
        
        Note: Sender is ALWAYS validated against signal-cli accounts.
        If the provided sender doesn't exist, uses first available account.
        """
        # CRITICAL: Validate sender against signal-cli (THE authority)
        # Never trust a sender value from DB/payload without validation
        effective_sender = self.get_valid_sender(sender)
        
        # Validate recipient
        recipient = recipient.strip()
        if not PHONE_REGEX.match(recipient):
            raise SignalRecipientInvalid(f"Invalid phone number: {_mask_phone(recipient)}")
        
        # Rate limiting: calculate sleep time and optimistically update timestamp
        # to prevent race condition where multiple threads read the same stale value
        sleep_time = 0.0
        with self._send_lock:
            now = time.monotonic()
            last_send = self._last_send_times.get(effective_sender, 0.0)
            elapsed = now - last_send
            if elapsed < MIN_SEND_INTERVAL:
                sleep_time = MIN_SEND_INTERVAL - elapsed
            else:
                sleep_time = 0.0
            # Optimistically update timestamp NOW to block other threads
            # This prevents TOCTOU race where multiple threads sleep and send simultaneously
            self._last_send_times[effective_sender] = now + sleep_time
        
        if sleep_time > 0:
            _log.debug("Rate limiting %s: sleeping %.2fs before send",
                      _mask_phone(effective_sender), sleep_time)
            time.sleep(sleep_time)
        
        result = self._run_signal_cli(
            ["-u", effective_sender, "send", "-m", message, recipient],
            timeout=DEFAULT_TIMEOUT,
            check=False,
        )
        
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else "Unknown error"
            lowered = stderr.lower()
            
            if "rate limiting" in lowered or "rate limit" in lowered:
                raise SignalRateLimited(stderr or "signal rate limited")
            
            # Distinguish sender vs recipient "not registered" errors
            # signal-cli patterns:
            # - "User +123 is not registered" → typically the sender
            # - "Unregistered user" or recipient-specific errors
            if "not registered" in lowered:
                # Check if it's clearly about the recipient
                recipient_masked = _mask_phone(recipient)
                if recipient in stderr or recipient_masked in stderr:
                    raise SignalRecipientInvalid(f"Recipient {recipient_masked} is not registered with Signal")
                # Otherwise assume sender issue (more common)
                raise SignalNotRegistered("Sender account not registered with Signal")
            
            # Check for other recipient-specific errors
            if "unregistered user" in lowered or "unknown recipient" in lowered:
                raise SignalRecipientInvalid(f"Recipient {_mask_phone(recipient)} is not registered with Signal")
            
            _log.error("Failed to send Signal message: %s", stderr)
            raise SignalSendFailed(f"Failed to send message: {stderr[:100]}")
        
        _log.info("Signal message sent to %s", _mask_phone(recipient))
    
    def check_user_registered(self, recipient: str, sender: str | None = None) -> bool:
        """Check if a phone number is registered with Signal.
        
        Uses signal-cli getUserStatus command to verify registration.
        
        Args:
            recipient: Phone number to check (E.164 format)
            sender: Sender phone number (optional, uses configured sender if not provided)
            
        Returns:
            True if recipient is registered with Signal, False otherwise
            
        Raises:
            SignalNotRegistered: if no sender account is configured
            SignalRecipientInvalid: if phone number format is invalid
            SignalUnavailable: if signal-cli is not available
        """
        # Validate sender against signal-cli accounts (consistent with send_message)
        effective_sender = self.get_valid_sender(sender or self._sender)
        
        # Validate recipient format
        recipient = recipient.strip()
        if not PHONE_REGEX.match(recipient):
            raise SignalRecipientInvalid(f"Invalid phone number format: {_mask_phone(recipient)}")
        
        try:
            # Use JSON output for reliable parsing
            result = self._run_signal_cli_json(
                ["-u", effective_sender, "getUserStatus", recipient],
                timeout=15,
            )
            
            _log.debug("getUserStatus JSON for %s: %r", _mask_phone(recipient), result)
            
            if result is None:
                # JSON parsing failed, fall back to text parsing
                text_result = self._run_signal_cli(
                    ["-u", effective_sender, "getUserStatus", recipient],
                    timeout=15,
                    check=False,
                )
                stdout = text_result.stdout.strip() if text_result.stdout else ""
                stdout_lower = stdout.lower()
                return "isregistered: true" in stdout_lower or ": true" in stdout_lower
            
            # JSON format: [{"number": "+49...", "isRegistered": true}]
            if isinstance(result, list) and len(result) > 0:
                return result[0].get("isRegistered", False) is True
            
            # Single object format: {"number": "+49...", "isRegistered": true}
            if isinstance(result, dict):
                return result.get("isRegistered", False) is True
            
            return False
            
        except SignalUnavailable:
            raise
        except Exception as e:
            _log.warning("Error checking Signal registration for %s: %s", _mask_phone(recipient), e)
            return False
    
    def get_link_qr(self, device_name: str = "justUp") -> tuple[bytes | None, str | None]:
        """Generate QR code for device linking.
        
        Note: signal-cli link command outputs a URI immediately, then waits
        for the phone to scan the QR code. We use Popen to read the URI
        without waiting for the process to complete.
        
        IMPORTANT: After calling this method, the link process runs in the
        background waiting for the user to scan the QR code. Call wait_for_link()
        with the returned session_id after the user has scanned, or cancel_link() to abort.
        
        Platform note: Uses select.select() which is POSIX-only. Windows is not
        supported for device linking (Docker/Linux target environment).
        
        Thread-safe: Uses lock to prevent concurrent link attempts.
        
        Returns:
            Tuple of (QR code PNG bytes, session_id) or (None, None) on failure
        """
        # Sanitize device name to prevent argument injection
        safe_device_name = _sanitize_cli_arg(device_name, "justUp")
        
        # Acquire lock to prevent race conditions (e.g., parallel QR requests)
        if not self._state_lock.acquire(blocking=True, timeout=5):
            _log.warning("Could not acquire lock for get_link_qr")
            return None, None
        
        try:
            resolved = self._resolve_cli()
            
            # Kill any existing link process for this instance
            if self._link_process is not None:
                try:
                    self._link_process.terminate()
                    self._link_process.wait(timeout=2)
                except Exception:
                    try:
                        self._link_process.kill()
                    except Exception:
                        pass
                self._link_process = None
            
            # Generate new session ID for this link attempt
            self._link_session_id = str(uuid.uuid4())
            session_id = self._link_session_id
            
            cmd = [
                resolved,
                "--config", self._data_dir,
                "link", "-n", safe_device_name,
            ]
            
            _log.debug("Starting signal-cli link: %s", " ".join(cmd[:4]) + " ...")
            
            # Start the process without waiting for completion
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            
            # Store reference for later cleanup
            self._link_process = proc
            
            # Read the first line (the sgnl:// URI) with timeout
            ready, _, _ = select.select([proc.stdout], [], [], QR_TIMEOUT)
            
            if not ready:
                _log.warning("signal-cli link timed out waiting for URI")
                self._cleanup_link_process()
                self._link_session_id = None
                return None, None
            
            link_uri = proc.stdout.readline().strip()
            
            if not link_uri or not link_uri.startswith("sgnl://"):
                _log.warning("Invalid link URI from signal-cli: %s", link_uri[:50] if link_uri else "(empty)")
                self._cleanup_link_process()
                self._link_session_id = None
                return None, None
            
            _log.info("Got link URI, generating QR code (link process running in background)")
            
            # Generate QR code
            try:
                import qrcode
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=4,
                )
                qr.add_data(link_uri)
                qr.make(fit=True)
                
                img = qr.make_image(fill_color="black", back_color="white")
                
                # Convert to PNG bytes
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                return buffer.getvalue(), session_id
                
            except ImportError:
                _log.error("qrcode library not installed")
                self._cleanup_link_process()
                self._link_session_id = None
                return None, None
            except Exception as e:
                _log.error("QR code generation failed: %s", e)
                self._cleanup_link_process()
                self._link_session_id = None
                return None, None
                
        except (SignalUnavailable, SignalTimeout) as e:
            _log.warning("Failed to generate QR code: %s", e)
            return None, None
        finally:
            # Release lock - process is now running in background
            self._state_lock.release()
    
    def wait_for_link(self, session_id: str, timeout: int = 60) -> bool:
        """Wait for the link process to complete after QR scan.
        
        This must be called after get_link_qr() and after the user has
        scanned the QR code on their phone. The link process will complete
        when Signal confirms the link.
        
        Args:
            session_id: Session ID returned by get_link_qr()
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if link completed successfully, False otherwise
            
        Thread-safe: Grab process reference under lock, wait outside lock to avoid
        blocking other operations (e.g., cancel_link).
        """
        # Grab process reference under lock, then release to avoid blocking
        with self._state_lock:
            # Validate session ID to prevent race conditions
            if self._link_session_id != session_id:
                _log.warning("Link session mismatch: expected %s, got %s", self._link_session_id, session_id)
                return False
            
            if self._link_process is None:
                _log.debug("No link process running")
                return True
            
            proc = self._link_process
        
        # Wait OUTSIDE the lock to allow cancel_link() and other operations
        try:
            # Wait for the process to complete
            _log.info("Waiting for signal-cli link process to complete (timeout=%ds)...", timeout)
            
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _log.warning("Link process timed out after %ds, terminating", timeout)
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # Re-acquire lock to update state
                with self._state_lock:
                    self._link_process = None
                return False
            
            # Read any remaining output
            stdout, stderr = "", ""
            try:
                stdout = proc.stdout.read() if proc.stdout else ""
                stderr = proc.stderr.read() if proc.stderr else ""
            except Exception:
                pass
            
            # Re-acquire lock to update state
            with self._state_lock:
                self._link_process = None
            
            if returncode == 0:
                _log.info("Link process completed successfully")
                self._invalidate_status_cache()
                return True
            else:
                _log.warning("Link process exited with code %d: %s", returncode, stderr[:200] if stderr else "")
                return False
                
        except Exception as e:
            _log.error("Error waiting for link process: %s", e)
            # Re-acquire lock to update state
            with self._state_lock:
                self._link_process = None
            return False
    def get_link_session_id(self) -> str | None:
        """Get the current link session ID.
        
        Returns:
            Current session ID if a link process is active, None otherwise
            
        Thread-safe: Uses lock to prevent race conditions when reading session ID.
        """
        with self._state_lock:
            return self._link_session_id
    
    def cancel_link(self) -> None:
        """Cancel any running link process.
        
        Call this if the user cancels the linking flow.
        
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            if self._link_process is not None:
                _log.info("Cancelling link process")
                self._cleanup_link_process()
                self._invalidate_status_cache()
    
    def unregister(self, sender_number: str | None = None, delete_cache: bool = True) -> None:
        """Unregister the current account and optionally delete local data.
        
        Args:
            sender_number: Phone number to unregister. If None, unregisters ALL accounts.
            delete_cache: If True, deletes local cache data (keys, session). Default: True
                          
        For primary accounts: Uses --delete-account to remove from Signal servers.
        For linked devices: Falls back to deleteLocalAccountData with --ignore-registered.
                          
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            # Get all accounts to unregister
            accounts_to_unregister = []
            
            if sender_number:
                accounts_to_unregister = [sender_number]
            else:
                # Unregister ALL accounts
                accounts_to_unregister = self.list_accounts()
                if not accounts_to_unregister:
                    raise SignalNotRegistered("No account to unregister")
            
            for number in accounts_to_unregister:
                # Try --delete-account first (works for primary registered accounts)
                result = self._run_signal_cli(
                    ["-u", number, "unregister", "--delete-account"],
                    timeout=DEFAULT_TIMEOUT,
                    check=False,
                )
                
                if result.returncode != 0:
                    stderr = result.stderr.strip() if result.stderr else ""
                    
                    # If "not registered", this is a linked device - use deleteLocalAccountData
                    if "not registered" in stderr.lower():
                        _log.info("Account %s is linked device, using deleteLocalAccountData", _mask_phone(number))
                        if delete_cache:
                            result2 = self._run_signal_cli(
                                ["-u", number, "deleteLocalAccountData", "--ignore-registered"],
                                timeout=DEFAULT_TIMEOUT,
                                check=False,
                            )
                            if result2.returncode != 0:
                                stderr2 = result2.stderr.strip() if result2.stderr else "Unknown error"
                                _log.warning("Failed to delete local data for %s: %s", _mask_phone(number), stderr2)
                            else:
                                _log.info("Linked device data deleted: %s", _mask_phone(number))
                        else:
                            _log.info("Skipped deleting local cache data for %s", _mask_phone(number))
                    else:
                        _log.warning("Failed to unregister %s: %s", _mask_phone(number), stderr)
                else:
                    _log.info("Signal account unregistered: %s", _mask_phone(number))
                    if delete_cache:
                        # Also delete local cache after successful unregister
                        self._delete_account_cache(number)
            
            # Invalidate status cache after any unregister operation
            self._invalidate_status_cache()
    
    def unlink_device(self, sender_number: str | None = None, delete_cache: bool = False) -> None:
        """Unlink device (optionally delete local data, keep Signal account).
        
        Args:
            sender_number: Phone number to unlink. If None, affects ALL local accounts.
            delete_cache: If True, deletes local cache data (keys, session). Default: False
                          
        This only removes the local signal-cli data if delete_cache=True. 
        The Signal account remains active and can be linked again from the Signal app.
        Uses --ignore-registered to force deletion even for registered accounts.
                          
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            # Get all accounts to process
            accounts_to_process = []
            
            if sender_number:
                accounts_to_process = [sender_number]
            else:
                # Process ALL local accounts
                accounts_to_process = self.list_accounts()
                if not accounts_to_process:
                    raise SignalNotRegistered("No account to unlink")
            
            if delete_cache:
                for number in accounts_to_process:
                    # Use deleteLocalAccountData with --ignore-registered
                    result = self._run_signal_cli(
                        ["-u", number, "deleteLocalAccountData", "--ignore-registered"],
                        timeout=DEFAULT_TIMEOUT,
                        check=False,
                    )
                    
                    if result.returncode != 0:
                        stderr = result.stderr.strip() if result.stderr else "Unknown error"
                        _log.warning("Failed to delete local data for %s: %s (trying manual cleanup)", _mask_phone(number), stderr)
                        # Try manual deletion as fallback
                        self._delete_account_cache(number)
                    else:
                        _log.info("Unlinked device and deleted local data: %s", _mask_phone(number))
            else:
                _log.info("Unlink requested without cache deletion for: %s", 
                         ", ".join(_mask_phone(n) for n in accounts_to_process))
            
            # Invalidate status cache after unlink operation
            self._invalidate_status_cache()

    def list_accounts(self) -> list[str]:
        """List all registered accounts.
        
        Parses signal-cli listAccounts output which can have various formats:
        - "Number: +491234567890"
        - Raw "+491234567890" on its own line
        - Mixed with log prefixes or timestamps
        
        Uses regex extraction for robustness against format variations.
        """
        # Try JSON output first for reliable parsing
        result = self._run_signal_cli_json(
            ["listAccounts"],
            timeout=DEFAULT_TIMEOUT,
        )
        
        if result is not None and isinstance(result, list):
            # JSON format: [{"number": "+49..."}, ...] or just ["+49...", ...]
            accounts = []
            for item in result:
                if isinstance(item, str) and PHONE_REGEX.match(item):
                    accounts.append(item)
                elif isinstance(item, dict) and "number" in item:
                    number = item["number"]
                    if PHONE_REGEX.match(number):
                        accounts.append(number)
            if accounts:
                return accounts
        
        # Fallback to text parsing
        text_result = self._run_signal_cli(
            ["listAccounts"],
            timeout=DEFAULT_TIMEOUT,
            check=False,
        )
        
        if text_result.returncode != 0:
            # No accounts is not an error
            return []
        
        # Use regex extraction for robustness against:
        # - Localized log messages
        # - Timestamped prefixes
        # - Various output formats
        accounts = []
        seen = set()  # Avoid duplicates
        
        for line in text_result.stdout.strip().split("\n"):
            # Extract all E.164 phone numbers from the line
            matches = PHONE_EXTRACT_REGEX.findall(line)
            for number in matches:
                if number not in seen and PHONE_REGEX.match(number):
                    accounts.append(number)
                    seen.add(number)
        
        return accounts

    def get_valid_sender(self, configured_sender: str | None = None) -> str:
        """Get a valid Signal sender number, validated against signal-cli.
        
        This is THE authoritative method to get a sender number. It ensures:
        1. The number actually exists in signal-cli (not just in DB)
        2. Falls back to first available account if configured doesn't exist
        
        Args:
            configured_sender: Optional sender from DB settings (may be stale/wrong)
            
        Returns:
            A valid phone number from signal-cli
            
        Raises:
            SignalNotRegistered: If no accounts are registered with signal-cli
        """
        accounts = self.list_accounts()
        
        if not accounts:
            raise SignalNotRegistered("No Signal accounts registered with signal-cli")
        
        # If configured sender exists in signal-cli, use it
        if configured_sender and configured_sender in accounts:
            return configured_sender
        
        # Configured sender doesn't exist or wasn't provided - use first available
        first_account = accounts[0]
        
        if configured_sender and configured_sender not in accounts:
            _log.warning(
                "Configured Signal sender %s not found in signal-cli, using %s instead",
                _mask_phone(configured_sender),
                _mask_phone(first_account)
            )
        
        return first_account

    def register(self, number: str, use_voice: bool = False, captcha: str | None = None) -> None:
        """Start registration process for a phone number.
        
        Uses signal-cli register command to request a verification code.
        
        Args:
            number: Phone number in E.164 format
            use_voice: If True, use voice call instead of SMS
            captcha: Captcha token if required
            
        Security Note:
            The captcha token is passed as a command-line argument and may be
            visible in process listings. This is acceptable because captcha tokens
            are single-use and short-lived. Do not pass sensitive credentials here.
            
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            if not PHONE_REGEX.match(number):
                raise SignalRecipientInvalid(f"Invalid phone number format: {_mask_phone(number)}")
            
            cmd = ["-u", number, "register"]
            
            if use_voice:
                cmd.append("--voice")
            
            if captcha:
                # Sanitize captcha to prevent argument injection
                safe_captcha = _sanitize_cli_arg(captcha, "")
                if safe_captcha:
                    cmd.extend(["--captcha", safe_captcha])
            
            _log.info("Starting Signal registration for %s (voice=%s)", _mask_phone(number), use_voice)
            
            result = self._run_signal_cli(
                cmd,
                timeout=REGISTER_TIMEOUT,
                check=False,
            )
            
            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                _log.error("Registration failed for %s: %s", _mask_phone(number), stderr[:200])
                
                lowered = stderr.lower()
                
                # Check for rate limiting (Signal is very aggressive with new registrations)
                if "rate limit" in lowered or "rate-limit" in lowered or "too many attempts" in lowered:
                    # Extract retry time if available (e.g., "retry after 3600 seconds")
                    retry_match = re.search(r'(\d+)\s*(?:seconds?|s)', lowered)
                    retry_seconds = int(retry_match.group(1)) if retry_match else 3600
                    raise SignalRateLimited(f"Registration rate limited: {stderr[:100]}", retry_after_seconds=retry_seconds)
                
                # Check for captcha requirement
                if "captcha" in lowered or "challenge" in lowered:
                    raise SignalSendFailed(f"Captcha required: {stderr[:200]}")
                
                raise SignalSendFailed(f"Registration failed: {stderr[:200]}")
            
            _log.info("Verification code requested for %s", _mask_phone(number))

    def verify(self, number: str, code: str) -> None:
        """Verify registration with the received code.
        
        Args:
            number: Phone number being verified
            code: 6-digit verification code (may include dashes)
            
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            if not PHONE_REGEX.match(number):
                raise SignalRecipientInvalid(f"Invalid phone number format: {_mask_phone(number)}")
            
            # Clean up code (remove dashes, spaces)
            clean_code = code.replace("-", "").replace(" ", "").strip()
            
            # Sanitize code to prevent argument injection (though unlikely with 6 digits)
            if not clean_code.isdigit() or len(clean_code) != 6:
                raise SignalSendFailed("Verification code must be 6 digits")
            
            # Extra safety check (already validated above, but explicit)
            safe_code = _sanitize_cli_arg(clean_code, "")
            if not safe_code:
                raise SignalSendFailed("Invalid verification code format")
            
            _log.info("Verifying Signal registration for %s", _mask_phone(number))
            
            result = self._run_signal_cli(
                ["-u", number, "verify", safe_code],
                timeout=VERIFY_TIMEOUT,
                check=False,
            )
            
            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                _log.error("Verification failed for %s: %s", _mask_phone(number), stderr[:200])
                raise SignalSendFailed(f"Verification failed: {stderr[:200]}")
            
            _log.info("Signal registration verified for %s", _mask_phone(number))
            self._invalidate_status_cache()

    def update_profile(self, name: str, avatar_path: str | None = None) -> None:
        """Update Signal profile name and avatar.
        
        Args:
            name: Display name shown in Signal chats
            avatar_path: Optional explicit path to avatar image. If None, tries default locations.
            
        Thread-safe: Uses lock to prevent concurrent state modifications.
        """
        with self._state_lock:
            # Get the first registered account
            accounts = self.list_accounts()
            if not accounts:
                raise SignalNotRegistered("No account registered to update profile")
            
            account = accounts[0]
            
            # Sanitize name to prevent argument injection
            safe_name = _sanitize_cli_arg(name, "justUp")
            
            cmd = ["-u", account, "updateProfile", "--given-name", safe_name]
            
            # Resolve avatar path if not explicitly provided
            if avatar_path is None:
                avatar_path = _find_avatar()
            
            if avatar_path:
                # Validate path to prevent path traversal attacks
                avatar_pathobj = Path(avatar_path).resolve()
                
                # Ensure path is within allowed directories
                allowed_dirs = [
                    Path("/opt/justup-dev/app/static").resolve(),
                    Path("/opt/justup-dev/data").resolve(),
                    Path.home() / ".config" / "justup",
                ]
                
                if not any(
                    avatar_pathobj.is_relative_to(allowed_dir)
                    for allowed_dir in allowed_dirs
                ):
                    _log.warning(
                        "Avatar path outside allowed directories: %s",
                        avatar_path,
                    )
                    raise ValueError(f"Avatar path not allowed: {avatar_path}")
                
                # Verify avatar file exists
                if avatar_pathobj.is_file():
                    cmd.extend(["--avatar", str(avatar_pathobj)])
                    _log.debug("Using avatar: %s", avatar_pathobj)
                else:
                    _log.warning("Avatar file not found: %s", avatar_pathobj)
            
            _log.info("Updating Signal profile for %s: name=%s", _mask_phone(account), name)
            
            result = self._run_signal_cli(
                cmd,
                timeout=PROFILE_TIMEOUT,
                check=False,
            )
            
            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                _log.error("Profile update failed: %s", stderr[:200])
                raise SignalSendFailed(f"Profile update failed: {stderr[:200]}")
            
            _log.info("Signal profile updated for %s", _mask_phone(account))
