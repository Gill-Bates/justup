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

import contextlib
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

from ...utils.phone import mask_phone as _mask_phone
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

# Timeouts in seconds.
DEFAULT_TIMEOUT: Final[int] = 10
QR_TIMEOUT: Final[int] = 30  # QR generation can be slow
REGISTER_TIMEOUT: Final[int] = 30  # Registration requests external servers
VERIFY_TIMEOUT: Final[int] = 20  # Verification can be slow
PROFILE_TIMEOUT: Final[int] = 20  # Profile update with avatar upload

# Rate limiting: max 1 message per MIN_SEND_INTERVAL seconds
# Prevents overwhelming signal-cli with rapid-fire alerts
MIN_SEND_INTERVAL: Final[float] = 1.0  # seconds between sends

# Phone number validation (strict E.164)
PHONE_REGEX: Final[re.Pattern] = re.compile(r"^\+[1-9]\d{7,14}$")

# Global process lock: signal-cli uses file-level locking on its data directory,
# so only one signal-cli subprocess can run at a time.  Without this, concurrent
# calls (e.g. send_message + check_user_registered) race for the file lock and
# the loser times out.  A threading.Lock serialises all subprocess invocations.
_CLI_PROCESS_LOCK: Final[threading.Lock] = threading.Lock()


def _sanitize_cli_arg(value: str, default: str = "") -> str:
    """Sanitize user input for signal-cli to prevent argument injection.

    Args:
        value: User input to sanitize
        default: Default value if input is invalid

    Returns:
        Sanitized value, or default if input starts with '-'

    Note:
        While subprocess list mode (no shell=True) prevents shell injection,
        signal-cli could misinterpret values starting with '-' as flags.
    """
    if not value or value.strip().startswith("-"):
        return default
    return value


def _get_signal_cli_path() -> str:
    """Get signal-cli binary path from environment or default."""
    return os.environ.get("SIGNAL_CLI_PATH", DEFAULT_SIGNAL_CLI_PATH)


def _get_signal_data_dir() -> str:
    """Get signal-cli data directory from environment or default."""
    return os.environ.get("SIGNAL_DATA_DIR", DEFAULT_SIGNAL_DATA_DIR)


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


def _drain_stream(stream) -> None:
    """Read a pipe until EOF and discard the data."""
    try:
        for _ in iter(stream.readline, ""):
            pass
    except (OSError, ValueError):
        pass


def _start_pipe_drain(proc: subprocess.Popen) -> list[threading.Thread]:
    """Start daemon threads that discard stdout/stderr of a long-running child."""
    threads = []
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            t = threading.Thread(target=_drain_stream, args=(stream,), daemon=True, name="signal-link-drain")
            t.start()
            threads.append(t)
    return threads


def _finish_pipe_drain(proc: subprocess.Popen, threads: list[threading.Thread]) -> None:
    """Join the drain threads of an exited child, then close its pipes."""
    for t in threads:
        t.join(timeout=2)
    # A reader still blocked in readline() would make close() block; leave it to end on EOF
    if any(t.is_alive() for t in threads):
        return
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


class SignalCliBackend(SignalBackend):
    """Signal backend using local signal-cli subprocess.

    Status is always fetched fresh from signal-cli - no caching.
    This ensures the UI always shows the real current state.
    """

    def __init__(
        self,
        cli_path: str | None = None,
        data_dir: str | None = None,
        sender_number: str | None = None,
    ):
        self._cli_path = cli_path or _get_signal_cli_path()
        self._data_dir = data_dir or _get_signal_data_dir()
        self._sender = sender_number

        # Active link process for this instance (thread-safe per worker)
        self._link_process: subprocess.Popen | None = None
        self._link_drain_threads: list[threading.Thread] = []
        self._link_session_id: str | None = None  # Track link session for race prevention

        # Lock for operations that modify signal-cli state (link, unregister)
        # Prevents race conditions when multiple requests hit the same backend
        self._state_lock = threading.Lock()

        # Rate limiting: track last send time per sender to prevent overwhelming signal-cli
        # Using dict allows multiple sender accounts without blocking each other
        self._last_send_times: dict[str, float] = {}
        self._send_lock = threading.Lock()

        # Ensure the data directory exists.
        Path(self._data_dir).mkdir(parents=True, exist_ok=True)

    def __del__(self) -> None:
        """Cleanup: Terminate any lingering link process.

        Note: __del__ is not guaranteed to be called (GC, shutdown, forks).
        For reliable cleanup, use close() explicitly or integrate with
        app lifecycle (e.g., FastAPI shutdown event).
        """
        # Guard against __init__ failures (e.g., if _link_process wasn't set)
        if hasattr(self, "_link_process"):
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

            except Exception as e:  # noqa: BLE001 - signal-cli boundary: log and degrade so a subprocess/IPC failure never crashes the caller
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
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run signal-cli with given arguments.

        Args:
            args: Command arguments (without signal-cli binary)
            timeout: Timeout in seconds
            check: If True, raise on non-zero exit code

        Returns:
            CompletedProcess with stdout/stderr

        Raises:
            SignalUnavailable: if binary not found
            SignalTimeout: if command times out
        """
        resolved = self._resolve_cli()

        cmd = [
            resolved,
            "--config",
            self._data_dir,
            *args,
        ]

        _log.debug("Running signal-cli: %s", " ".join(cmd[:4]) + " ...")

        # Ensure UTF-8 environment for proper emoji/unicode handling
        env = os.environ.copy()
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            with _CLI_PROCESS_LOCK:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    input=input_text,
                    timeout=timeout,
                    text=True,
                    encoding="utf-8",  # Explicit UTF-8 for emoji/unicode support
                    env=env,
                    check=False,  # We handle errors ourselves
                )

            if result.returncode != 0 and check:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                _log.warning("signal-cli failed (exit %d): %s", result.returncode, stderr)

            return result

        except subprocess.TimeoutExpired as e:
            raise SignalTimeout(f"signal-cli timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise SignalUnavailable(f"signal-cli not found: {e}") from e
        except OSError as e:
            raise SignalUnavailable(f"Failed to run signal-cli: {e}") from e

    def _run_signal_rpc(self, method: str, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """Run one documented JSON-RPC request with stdin closed after the request."""
        request_id = str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        result = self._run_signal_cli(
            ["jsonRpc", "--receive-mode=manual"],
            timeout=timeout,
            check=False,
            input_text=json.dumps(payload) + "\n",
        )
        for line in result.stdout.splitlines():
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and response.get("id") == request_id:
                if response.get("jsonrpc") != "2.0" or not any(k in response for k in ("result", "error")):
                    break
                return response
        if result.returncode != 0:
            _log.warning("signal-cli JSON-RPC %s exited with code %d", method, result.returncode)
        raise SignalUnavailable(f"Signal {method} returned no valid response")

    def _require_rpc_success(self, response: dict, operation: str, account: str) -> None:
        """Map structured JSON-RPC errors to the existing Signal domain errors."""
        error = response.get("error")
        if error is None:
            if "result" not in response:
                raise SignalUnavailable(f"Signal {operation} returned no result")
            return
        if not isinstance(error, dict):
            raise SignalSendFailed(f"Signal {operation} failed")
        code = error.get("code")
        data = error.get("data")
        retry_after = data.get("retryAfterSeconds") if isinstance(data, dict) else None
        retry_seconds = retry_after if isinstance(retry_after, int) and retry_after > 0 else 3600
        _log.warning("Signal %s failed for %s (rpc_code=%s)", operation, _mask_phone(account), code)
        if code == -5 and operation in {"registration", "verification"}:
            raise SignalRateLimited(f"Signal {operation} rate limited", retry_after_seconds=retry_seconds)
        message = error.get("message")
        if operation == "registration" and (code == -6 or (isinstance(message, str) and "captcha" in message.lower())):
            raise SignalSendFailed("Captcha required")
        raise SignalSendFailed(f"Signal {operation} failed")

    def _run_signal_cli_binary(
        self,
        args: list[str],
        timeout: int = DEFAULT_TIMEOUT,
        check: bool = False,
    ) -> bytes:
        """Run signal-cli and return raw binary output (for images).

        Args:
            args: Command arguments
            timeout: Timeout in seconds
            check: If True, raise SignalSendFailed on non-zero exit code

        Returns:
            Raw stdout bytes

        Raises:
            SignalUnavailable: if binary not found
            SignalTimeout: if command times out
            SignalSendFailed: if check=True and exit code != 0
        """
        resolved = self._resolve_cli()

        cmd = [
            resolved,
            "--config",
            self._data_dir,
            *args,
        ]

        # Ensure UTF-8 environment for proper emoji/unicode handling
        env = os.environ.copy()
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"

        try:
            with _CLI_PROCESS_LOCK:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                    check=False,
                )

            if check and result.returncode != 0:
                stderr = result.stderr.decode(errors="replace").strip() if result.stderr else "Unknown error"
                _log.warning("signal-cli binary command failed (exit %d): %s", result.returncode, stderr[:100])
                raise SignalSendFailed(f"signal-cli failed: {stderr[:100]}")

            return result.stdout

        except subprocess.TimeoutExpired as e:
            raise SignalTimeout(f"signal-cli timed out after {timeout}s") from e
        except (FileNotFoundError, OSError) as e:
            raise SignalUnavailable(f"Failed to run signal-cli: {e}") from e

    def invalidate_cache(self) -> None:
        """No-op for API compatibility. Status is always fetched fresh."""

    def status(self, force_refresh: bool = False) -> SignalStatus:  # noqa: ARG002 - SignalBackend interface
        """Get current Signal status - always fresh from signal-cli.

        Args:
            force_refresh: Ignored (kept for API compatibility). Status is always fresh.

        Always queries signal-cli directly to ensure accurate state.
        No caching - the UI must always show the real current status.
        """
        try:
            return self._fetch_status_uncached()
        except (SignalTimeout, SignalUnavailable) as e:
            _log.warning("Signal status check failed: %s", e)
            return SignalStatus(
                reachable=False,
                registered=False,
                linked=False,
                accounts=[],
                error=str(e),
            )

    def _fetch_status_uncached(self) -> SignalStatus:
        """Fetch fresh status from signal-cli (no caching).

        Performs a lightweight server-side validation to detect deregistered accounts.
        """
        # Check if signal-cli is available (quick version check).
        # SignalUnavailable/SignalTimeout propagate to the caller for fallback handling.
        version_result = self._run_signal_cli(["--version"], timeout=5, check=False)
        if version_result.returncode != 0:
            return SignalStatus(
                reachable=False,
                registered=False,
                linked=False,
                accounts=[],
                error="signal-cli not working",
            )

        # Get accounts from local config
        accounts = self.list_accounts()
        if not accounts:
            return SignalStatus(
                reachable=True,
                registered=False,
                linked=False,
                accounts=[],
                error=None,
            )

        self._validate_account_server_side(accounts[0])

        return SignalStatus(
            reachable=True,
            registered=True,
            linked=True,
            accounts=[_mask_phone(a) for a in accounts],
            error=None,
        )

    def _validate_account_server_side(self, account: str) -> None:
        """Validate a local account through a structured server-backed command."""
        response = self._run_signal_rpc("listDevices", {"account": account}, timeout=10)
        if "error" in response or not isinstance(response.get("result"), list):
            raise SignalUnavailable("Unable to validate Signal account; check the device link")

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
            _log.debug("Rate limiting %s: sleeping %.2fs before send", _mask_phone(effective_sender), sleep_time)
            time.sleep(sleep_time)

        response = self._run_signal_rpc(
            "send",
            {"account": effective_sender, "recipient": [recipient], "message": message},
        )
        error = response.get("error")
        result = response.get("result")
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict):
                result = data.get("response")
        if isinstance(result, dict):
            send_results = result.get("results", [])
            if not isinstance(send_results, list):
                raise SignalSendFailed("Signal returned an invalid send result")
            for item in send_results:
                if not isinstance(item, dict):
                    raise SignalSendFailed("Signal returned an invalid recipient result")
                outcome = item.get("type")
                if outcome == "RATE_LIMIT_FAILURE":
                    retry_after = item.get("retryAfterSeconds")
                    seconds = retry_after if isinstance(retry_after, int) and retry_after > 0 else 3600
                    raise SignalRateLimited("Signal rate limited this sender", retry_after_seconds=seconds)
                if outcome == "UNREGISTERED_FAILURE":
                    raise SignalRecipientInvalid(f"Recipient {_mask_phone(recipient)} is not registered with Signal")
                if outcome == "IDENTITY_FAILURE":
                    raise SignalSendFailed("Recipient identity changed; verify it in Signal")
                if outcome in {"NETWORK_FAILURE", "INVALID_PRE_KEY_FAILURE"}:
                    raise SignalSendFailed("Signal delivery failed; retry later")
                if outcome not in {None, "SUCCESS"}:
                    raise SignalSendFailed("Signal delivery failed")
        if error is not None:
            if isinstance(error, dict) and error.get("code") == -5:
                raise SignalRateLimited("Signal rate limited this sender")
            _log.warning("Signal send returned JSON-RPC error for %s", _mask_phone(recipient))
            raise SignalSendFailed("Signal delivery failed")
        if not isinstance(result, dict) or not isinstance(result.get("timestamp"), int):
            raise SignalSendFailed("Signal returned no delivery timestamp")

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

        response = self._run_signal_rpc(
            "getUserStatus",
            {"account": effective_sender, "recipient": [recipient]},
            timeout=15,
        )
        if "error" in response:
            raise SignalUnavailable("Unable to check Signal recipient")
        result = response.get("result")
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise SignalUnavailable("Signal recipient status has an invalid format")
        registered = result[0].get("isRegistered")
        if not isinstance(registered, bool):
            raise SignalUnavailable("Signal recipient status is missing")
        return registered

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
                except Exception:  # noqa: BLE001 - signal-cli boundary: log and degrade so a subprocess/IPC failure never crashes the caller
                    with contextlib.suppress(Exception):
                        self._link_process.kill()
                self._link_process = None

            # Generate new session ID for this link attempt
            self._link_session_id = str(uuid.uuid4())
            session_id = self._link_session_id

            cmd = [
                resolved,
                "--config",
                self._data_dir,
                "link",
                "-n",
                safe_device_name,
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
            # Keep draining output so a chatty signal-cli never blocks on a full pipe
            self._link_drain_threads = _start_pipe_drain(proc)

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
            except Exception as e:  # noqa: BLE001 - signal-cli boundary: log and degrade so a subprocess/IPC failure never crashes the caller
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
                return False

            proc = self._link_process
            drain_threads = list(self._link_drain_threads)

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
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=1)
                _finish_pipe_drain(proc, drain_threads)
                # Re-acquire lock to update state
                with self._state_lock:
                    if self._link_process is proc:
                        self._link_process = None
                        self._link_session_id = None
                return False

            # The drain threads hit EOF now that the process exited; then release the pipes
            _finish_pipe_drain(proc, drain_threads)

            # Re-acquire lock to update state
            with self._state_lock:
                if self._link_process is proc:
                    self._link_process = None
                    self._link_session_id = None

            if returncode == 0:
                _log.info("Link process completed successfully")
                return True
            _log.warning("Link process exited with code %d", returncode)
            return False

        except Exception as e:  # noqa: BLE001 - signal-cli boundary: log and degrade so a subprocess/IPC failure never crashes the caller
            _log.error("Error waiting for link process: %s", e)
            # Re-acquire lock to update state
            with self._state_lock:
                if self._link_process is proc:
                    self._link_process = None
                    self._link_session_id = None
            return False

    def get_link_session_id(self) -> str | None:
        """Get the current link session ID.

        Returns:
            Current session ID if a link process is active, None otherwise
        """
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
            self._link_session_id = None

    def unregister(self, sender_number: str | None = None, delete_cache: bool = True) -> None:
        """Delete one selected primary account from Signal's servers."""
        with self._state_lock:
            if not sender_number:
                raise SignalSendFailed("An explicit Signal account is required for unregister")
            if sender_number not in self.list_accounts():
                raise SignalNotRegistered("Selected Signal account is not registered locally")

            response = self._run_signal_rpc("unregister", {"account": sender_number, "deleteAccount": True})
            self._require_rpc_success(response, "account removal", sender_number)

            if delete_cache:
                if sender_number in self.list_accounts():
                    local_response = self._run_signal_rpc(
                        "deleteLocalAccountData",
                        {"account": sender_number, "ignoreRegistered": True},
                    )
                    self._require_rpc_success(local_response, "local data removal", sender_number)
                if sender_number in self.list_accounts():
                    raise SignalSendFailed("Signal account remains after unregister")
            _log.info("Signal account unregistered: %s", _mask_phone(sender_number))

    def unlink_device(self, sender_number: str | None = None, delete_cache: bool = True) -> None:
        """Remove one local device's data; revoke it on the primary phone separately."""
        with self._state_lock:
            if not sender_number or not delete_cache:
                raise SignalSendFailed("An account and local data deletion are required for unlink")
            if sender_number not in self.list_accounts():
                raise SignalNotRegistered("Selected Signal account is not registered locally")

            response = self._run_signal_rpc(
                "deleteLocalAccountData",
                {"account": sender_number, "ignoreRegistered": True},
            )
            self._require_rpc_success(response, "local data removal", sender_number)
            if sender_number in self.list_accounts():
                raise SignalSendFailed("Signal account remains after local data removal")
            _log.info("Local Signal data removed for %s", _mask_phone(sender_number))

    def list_accounts(self) -> list[str]:
        """List local numbered accounts from signal-cli's structured response."""
        response = self._run_signal_rpc("listAccounts")
        if "error" in response:
            _log.warning("signal-cli listAccounts returned JSON-RPC error")
            raise SignalUnavailable("Unable to list Signal accounts")
        result = response.get("result")
        if not isinstance(result, list):
            raise SignalUnavailable("Signal account list has an invalid format")
        accounts: list[str] = []
        for item in result:
            if not isinstance(item, dict):
                raise SignalUnavailable("Signal account entry has an invalid format")
            number = item.get("number")
            if number is None:
                continue
            if not isinstance(number, str) or not PHONE_REGEX.fullmatch(number):
                raise SignalUnavailable("Signal account entry has an invalid number")
            accounts.append(number)
        return accounts

    def get_valid_sender(self, configured_sender: str | None = None) -> str:
        """Get a valid Signal sender number, validated against signal-cli.

        This is THE authoritative method to get a sender number. It ensures:
        1. The number actually exists in signal-cli (not just in DB)
        2. Uses the first account only when no sender is configured; a configured
           sender that is not registered raises instead of silently switching accounts

        Args:
            configured_sender: Optional sender from DB settings (may be stale/wrong)

        Returns:
            A valid phone number from signal-cli

        Raises:
            SignalNotRegistered: If no accounts exist or the configured sender is not among them
        """
        accounts = self.list_accounts()

        if not accounts:
            raise SignalNotRegistered("No Signal accounts registered with signal-cli")

        # If configured sender exists in signal-cli, use it
        if configured_sender and configured_sender in accounts:
            return configured_sender

        if configured_sender:
            _log.error("Configured Signal sender %s is not registered in signal-cli", _mask_phone(configured_sender))
            raise SignalNotRegistered(
                f"Configured Signal sender {_mask_phone(configured_sender)} is not registered with signal-cli"
            )

        return accounts[0]

    def register(self, number: str, use_voice: bool = False, captcha: str | None = None) -> None:
        """Request an SMS or voice verification code through JSON-RPC."""
        with self._state_lock:
            if not PHONE_REGEX.match(number):
                raise SignalRecipientInvalid(f"Invalid phone number format: {_mask_phone(number)}")
            _log.info("Starting Signal registration for %s (voice=%s)", _mask_phone(number), use_voice)
            response = self._run_signal_rpc(
                "register",
                {"account": number, "voice": use_voice, "captcha": captcha, "reregister": False},
                timeout=REGISTER_TIMEOUT,
            )
            self._require_rpc_success(response, "registration", number)
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

            if not clean_code.isdigit() or len(clean_code) != 6:
                raise SignalSendFailed("Verification code must be 6 digits")
            _log.info("Verifying Signal registration for %s", _mask_phone(number))
            response = self._run_signal_rpc(
                "verify",
                {"account": number, "verificationCode": clean_code},
                timeout=VERIFY_TIMEOUT,
            )
            self._require_rpc_success(response, "verification", number)
            _log.info("Signal registration verified for %s", _mask_phone(number))

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
            profile = {"account": account, "givenName": name}

            # Resolve avatar path if not explicitly provided
            if avatar_path is None:
                avatar_path = _find_avatar()

            if avatar_path:
                # Verify avatar file exists
                if Path(avatar_path).is_file():
                    profile["avatar"] = avatar_path
                    _log.debug("Using avatar: %s", avatar_path)
                else:
                    _log.warning("Avatar file not found: %s", avatar_path)

            _log.info("Updating Signal profile for %s", _mask_phone(account))
            response = self._run_signal_rpc(
                "updateProfile",
                profile,
                timeout=PROFILE_TIMEOUT,
            )
            self._require_rpc_success(response, "profile update", account)
            _log.info("Signal profile updated for %s", _mask_phone(account))
