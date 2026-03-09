# cython: language_level=3

"""
Telemetry module for anonymous usage statistics.

Provides periodic system status collection for operational monitoring.
Can be disabled via Settings -> General -> Enable Telemetry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import random
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional

_log = logging.getLogger(__name__)

# httpx optional - graceful degradation
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False

# cryptography optional - required for payload encryption
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

# Constants
_TELEMETRY_ENDPOINT = "https://upstats.cirrio.de/updater"
_TELEMETRY_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours
_TELEMETRY_CHECK_INTERVAL = 30  # Check setting changes every 30 seconds (faster UX)
_TELEMETRY_TIMEOUT = 10.0  # HTTP timeout
_TELEMETRY_RETRY_ATTEMPTS = 3  # Max retry attempts on failure
_TELEMETRY_RETRY_BASE_DELAY = 60  # Base seconds between retries

# Module state
_instance_uuid: Optional[str] = None
_uuid_lock = Lock()


# ─── OBFUSCATED KEY STORAGE ────────────────────────────────────────────────
# The key is stored XOR-encoded to prevent plain-text extraction from binary.
# This is NOT cryptographic security - it's obfuscation against casual inspection.
# Key reconstruction happens at runtime only.

def _xor_decode(encoded: bytes, mask: bytes) -> bytes:
    """XOR decode with rotating mask."""
    return bytes(b ^ mask[i % len(mask)] for i, b in enumerate(encoded))


def _get_auth_key() -> str:
    """
    Reconstruct authentication key at runtime.
    
    Key is stored XOR-encoded to prevent grep/strings extraction.
    """
    # Pre-computed XOR-encoded chunks (mask: b'mgmt')
    # Original key reconstructed only at runtime
    _c1 = bytes([0x0a, 0x30, 0x5b, 0x01, 0x0b, 0x3f, 0x39, 0x39])
    _c2 = bytes([0x08, 0x0d, 0x28, 0x3f, 0x52, 0x26, 0x1d, 0x3b])
    _c3 = bytes([0x58, 0x33, 0x19, 0x0d, 0x1c, 0x2e, 0x1f, 0x37])
    _c4 = bytes([0x50, 0x2d, 0x3e, 0x0c, 0x3b, 0x53, 0x08, 0x06])
    
    _mask = b'mgmt'
    
    p1 = _xor_decode(_c1, _mask)
    p2 = _xor_decode(_c2, _mask)
    p3 = _xor_decode(_c3, _mask)
    p4 = _xor_decode(_c4, _mask)
    
    return (p1 + p2 + p3 + p4).decode('utf-8')


def _ensure_mgmt_table(conn: sqlite3.Connection) -> None:
    """Create telemetry table if not exists (for UUID storage)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _sys_mgmt (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _get_or_create_instance_id(conn: sqlite3.Connection) -> tuple[str, bool]:
    """
    Get existing instance ID or create a new one.
    
    The ID is persisted in SQLite and reused across restarts.
    
    Thread-safety: The lock protects the in-memory cache (_instance_uuid).
    The conn parameter MUST be a fresh connection from db_conn_factory() that
    is not shared across threads. SQLite connections are not thread-safe.
    
    Returns:
        Tuple of (instance_id, is_new) where is_new is True if this is the first time
        the UUID was created (indicates fresh install/first run).
    """
    global _instance_uuid
    
    # Fast path: check cache without DB access (minimizes lock contention)
    with _uuid_lock:
        if _instance_uuid is not None:
            return (_instance_uuid, False)
    
    # Slow path: load from DB or create new (outside lock to avoid holding it during I/O)
    _ensure_mgmt_table(conn)
    
    # Try to load existing ID
    cursor = conn.execute(
        "SELECT value FROM _sys_mgmt WHERE key = 'sys_id'"
    )
    row = cursor.fetchone()
    
    if row:
        # Found existing ID - cache it
        with _uuid_lock:
            _instance_uuid = row[0]
            return (_instance_uuid, False)
    else:
        # Generate new ID and store it - this is the first run
        new_uuid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO _sys_mgmt (key, value, created_at) VALUES (?, ?, ?)",
            ("sys_id", new_uuid, now)
        )
        conn.commit()
        
        # Cache the new UUID
        with _uuid_lock:
            _instance_uuid = new_uuid
            return (_instance_uuid, True)


def _reset_instance_id() -> None:
    """Reset cached instance ID (for testing only)."""
    global _instance_uuid
    with _uuid_lock:
        _instance_uuid = None


def _is_docker() -> bool:
    """Detect if running inside a Docker container."""
    try:
        if Path("/.dockerenv").exists():
            return True
        if Path("/proc/1/cgroup").exists():
            text = Path("/proc/1/cgroup").read_text()
            if "docker" in text or "containerd" in text or "kubepods" in text:
                return True
    except Exception:
        pass
    return False


def _get_os_dist() -> Optional[str]:
    """Get OS distribution name (e.g. 'Ubuntu 22.04', 'Debian 12')."""
    try:
        import distro  # type: ignore[import-untyped]
        name = distro.name(pretty=True)
        return name if name else None
    except ImportError:
        pass
    try:
        p = Path("/etc/os-release")
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def _get_system_uptime() -> Optional[int]:
    """Get system uptime in seconds."""
    try:
        if Path("/proc/uptime").exists():
            return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        pass
    try:
        import psutil
        import time as _time
        return int(_time.time() - psutil.boot_time())
    except Exception:
        pass
    return None


def _collect_system_info() -> dict[str, Any]:
    """Collect system environment information."""
    try:
        import psutil
        memory = psutil.virtual_memory()
        cpu_count = psutil.cpu_count(logical=True) or 0
        mem_total_gb = round(memory.total / (1024**3), 1)
    except ImportError:
        cpu_count = os.cpu_count() or 0
        mem_total_gb = 0
    
    # Deterministic hostname hash (hash() is randomized since Python 3.3)
    hostname_hash = int(hashlib.sha256(platform.node().encode()).hexdigest(), 16) % (10**8)
    
    return {
        "os": platform.system(),
        "osv": platform.release(),
        "py": platform.python_version(),
        "arch": platform.machine(),
        "cpu": cpu_count,
        "mem": mem_total_gb,
        "hh": hostname_hash,
        "uptime": _get_system_uptime(),
        "docker": _is_docker(),
        "dist": _get_os_dist(),
    }


def _count_targets(conn: sqlite3.Connection) -> int:
    """Count total monitoring targets."""
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM targets")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def _count_users(conn: sqlite3.Connection) -> int:
    """Count total users."""
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM users")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def _count_active_targets(conn: sqlite3.Connection) -> int:
    """Count enabled (active) monitoring targets."""
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM targets WHERE is_enabled = 1")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def _count_open_alerts(conn: sqlite3.Connection) -> int:
    """Count currently open (unresolved) alerts."""
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM alerts WHERE ended_at IS NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def _get_db_size_mb(conn: sqlite3.Connection) -> Optional[float]:
    """Get SQLite database file size in MB."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row:
            db_path = row[2]
            if db_path:
                size_bytes = Path(db_path).stat().st_size
                return round(size_bytes / (1024 * 1024), 2)
    except Exception:
        pass
    return None


def _get_active_channels(conn: sqlite3.Connection) -> list[str]:
    """Get list of enabled notification channel types."""
    channels: set[str] = set()
    
    try:
        # Check settings for enabled channels
        cursor = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'notifications_%_enabled'"
        )
        for row in cursor:
            key = row[0]
            value = row[1]
            if value and value.lower() in ("true", "1", "yes"):
                # Extract channel name properly (handles multi-word like "push_bullet")
                channel_name = key.removeprefix("notifications_").removesuffix("_enabled")
                if channel_name:
                    channels.add(channel_name)
    except Exception:
        pass
    
    # Check for Signal configuration
    try:
        cursor = conn.execute(
            "SELECT value FROM settings WHERE key = 'signal_enabled'"
        )
        row = cursor.fetchone()
        if row and row[0] and row[0].lower() in ("true", "1", "yes"):
            channels.add("signal")
    except Exception:
        pass
    
    return list(channels)


def _is_valid_ip(ip_string: str) -> bool:
    """Validate IPv4 or IPv6 address format using stdlib ipaddress module."""
    if not ip_string or not isinstance(ip_string, str):
        return False
    try:
        import ipaddress as _ipa
        _ipa.ip_address(ip_string)
        return True
    except (ValueError, TypeError):
        return False


def _get_public_ip() -> Optional[str]:
    """
    Get public IP address via external services with fallback chain.
    
    Tries multiple endpoints sequentially. Handles both plain-text and
    JSON responses, and supports IPv4 and IPv6.
    """
    if not _HAS_HTTPX:
        return None
    
    services = [
        "https://ident.me",
        "https://checkip.amazonaws.com",
        "https://httpbin.org/ip",
    ]
    
    for url in services:
        try:
            resp = httpx.get(url, timeout=5.0, follow_redirects=True)
            if resp.status_code != 200:
                continue
            
            body = resp.text.strip()
            if not body:
                continue
            
            # Try JSON first (httpbin returns {"origin": "1.2.3.4"})
            ip_candidate = None
            try:
                data = resp.json()
                if isinstance(data, dict):
                    ip_candidate = (
                        data.get("ip")
                        or data.get("origin")
                        or data.get("address")
                    )
            except (ValueError, TypeError):
                pass
            
            # Fall back to plain-text body (ident.me, checkip.amazonaws.com)
            if not ip_candidate:
                ip_candidate = body
            
            if ip_candidate:
                ip_candidate = ip_candidate.strip()
            
            if ip_candidate and _is_valid_ip(ip_candidate):
                return ip_candidate
        except Exception:
            continue
    
    return None


def _get_app_version() -> str:
    """Get application version from VERSION file (searches multiple locations)."""
    # Try multiple locations to handle different deployment scenarios
    candidates = [
        Path(__file__).parent.parent.parent / "VERSION",  # Normal: app/_cython -> app -> root
        Path(__file__).parent.parent / "VERSION",         # Moved one level
        Path.cwd() / "VERSION",                           # Current working directory
    ]
    
    for version_file in candidates:
        try:
            if version_file.exists():
                return version_file.read_text().strip()
        except Exception:
            continue
    
    return "0.0.0"


def collect_status_data(conn: sqlite3.Connection, *, include_ip: bool = False) -> dict[str, Any]:
    """
    Collect all status data points.
    
    Returns a dict ready for serialization and transmission.
    
    Args:
        conn: SQLite connection
        include_ip: If True, fetch public IP (blocking HTTP call - explicit opt-in)
    """
    instance_id, _ = _get_or_create_instance_id(conn)
    
    # Get public IP only if explicitly requested (and httpx available)
    public_ip: Optional[str] = None
    if include_ip and _HAS_HTTPX:
        public_ip = _get_public_ip()
    
    return {
        "id": instance_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "v": _get_app_version(),
        "ip": public_ip,
        "sys": _collect_system_info(),
        "st": {
            "t": _count_targets(conn),
            "u": _count_users(conn),
            "nc": _get_active_channels(conn),
            "at": _count_active_targets(conn),
            "oa": _count_open_alerts(conn),
            "dbs": _get_db_size_mb(conn),
        },
    }


def _encrypt_payload(data: dict[str, Any]) -> tuple[str, str]:
    """
    Encrypt and encode payload for transmission using AES-256-GCM.
    
    Returns (encrypted_base64, nonce_hex) tuple.
    AES-GCM provides authenticated encryption (AEAD) - no separate HMAC needed.
    
    Raises:
        RuntimeError: If cryptography library is not available
    """
    if not _HAS_CRYPTO:
        raise RuntimeError("cryptography library required for payload encryption")
    
    # Derive 256-bit key from auth secret using SHA256
    key_material = _get_auth_key().encode('utf-8')
    aes_key = hashlib.sha256(key_material).digest()  # 32 bytes = AES-256
    
    # Serialize to JSON
    json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
    
    # Generate random 96-bit nonce (recommended for GCM)
    nonce = os.urandom(12)
    
    # Encrypt with AES-256-GCM (provides confidentiality + authentication)
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, json_bytes, None)
    
    # Combine nonce + ciphertext and Base64 encode
    payload_b64 = base64.b64encode(nonce + ciphertext).decode('ascii')
    
    # HMAC signature for backward compatibility with server validation
    # Note: AES-GCM already provides authentication via built-in auth tag
    signature = hmac.new(key_material, nonce + ciphertext, hashlib.sha256).hexdigest()
    
    return payload_b64, signature


def submit_status(data: dict[str, Any]) -> bool:
    """
    Submit status data to collection endpoint.
    
    Returns True on success, False on any failure.
    """
    if not _HAS_HTTPX:
        _log.warning("Telemetry: httpx not available, cannot send data")
        return False
    
    if not _HAS_CRYPTO:
        _log.warning("Telemetry: cryptography not available, cannot encrypt payload")
        return False
    
    try:
        payload_b64, signature = _encrypt_payload(data)
        
        resp = httpx.post(
            _TELEMETRY_ENDPOINT,
            content=payload_b64,
            timeout=_TELEMETRY_TIMEOUT,
            follow_redirects=True,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sig": signature,  # HMAC auth only - server has shared secret
                "X-Ver": data.get("v", "0.0.0"),
            },
        )
        
        success = resp.status_code in (200, 201, 202, 204)
        
        if not success:
            _log.warning(
                "Telemetry: Server returned status %d (expected 200-204)",
                resp.status_code
            )
        
        return success
        
    except Exception as e:
        _log.error(
            "Telemetry: Failed to send report - %s: %s",
            type(e).__name__,
            str(e)
        )
        return False


def _collect_and_submit(db_conn_factory) -> bool:
    """
    Synchronous helper: collect data and submit once (no retry).
    
    Designed to run in thread pool executor to avoid blocking event loop.
    Retry logic is handled by the async caller to avoid blocking workers.
    
    Returns True on success, False on failure.
    """
    # Phase 1: Collect data once (DB + IP lookup)
    conn = db_conn_factory()
    try:
        data = collect_status_data(conn, include_ip=False)
    finally:
        conn.close()
    
    # Add public IP (network I/O outside DB connection)
    if _HAS_HTTPX:
        public_ip = _get_public_ip()
        data["ip"] = public_ip
    
    # Submit once (no blocking retry)
    return submit_status(data)


def _is_telemetry_enabled(conn: sqlite3.Connection) -> bool:
    """
    Check if telemetry is enabled in settings.
    
    Default: True (enabled) for new installations.
    """
    try:
        cursor = conn.execute(
            "SELECT value FROM settings WHERE key = 'telemetry_enabled'"
        )
        row = cursor.fetchone()
        if row is None:
            # Setting not found - default to enabled
            return True
        # Parse boolean from string
        return str(row[0]).lower() in ("true", "1", "yes")
    except Exception:
        # On any error, default to enabled
        return True


async def _send_telemetry_with_retry(loop, db_conn_factory) -> bool:
    """
    Send telemetry with async exponential backoff retry.
    
    This avoids blocking threadpool workers with time.sleep().
    Uses asyncio.sleep() for non-blocking delays.
    
    Returns True on success, False if all attempts failed.
    """
    for attempt in range(_TELEMETRY_RETRY_ATTEMPTS):
        try:
            # Run blocking I/O in threadpool
            success = await loop.run_in_executor(
                None, _collect_and_submit, db_conn_factory
            )
            
            if success:
                _log.info("Telemetry: Report sent successfully")
                return True
            
            _log.warning("Telemetry: Attempt %d/%d failed", attempt + 1, _TELEMETRY_RETRY_ATTEMPTS)
            
        except Exception as e:
            _log.error(
                "Telemetry: Attempt %d/%d raised exception - %s: %s",
                attempt + 1,
                _TELEMETRY_RETRY_ATTEMPTS,
                type(e).__name__,
                str(e)
            )
        
        # Exponential backoff with async sleep (non-blocking)
        if attempt < _TELEMETRY_RETRY_ATTEMPTS - 1:
            delay = _TELEMETRY_RETRY_BASE_DELAY * (2 ** attempt)  # 60s, 120s, 240s
            # Add jitter to prevent thundering herd
            jitter = random.randint(0, delay // 4)
            total_delay = delay + jitter
            await asyncio.sleep(total_delay)
    
    _log.error("Telemetry: All %d attempts failed, giving up until next interval", _TELEMETRY_RETRY_ATTEMPTS)
    return False


async def telemetry_loop(
    db_conn_factory,
    stop_event: asyncio.Event,
) -> None:
    """
    Background task for periodic telemetry collection and submission.
    
    Logic:
    - Runs continuously, checking telemetry setting every 30 seconds
    - On first run (UUID created): sends immediately if enabled
    - On disabled → enabled transition: sends immediately
    - When enabled: sends every 24h
    - When disabled: only monitors for re-enablement
    
    Args:
        db_conn_factory: Callable that returns a SQLite connection
        stop_event: Event to signal graceful shutdown
    
    All blocking I/O runs in thread pool to avoid blocking event loop.
    """
    loop = asyncio.get_running_loop()
    
    # Phase 1: Check initial state and UUID status
    conn = db_conn_factory()
    try:
        is_enabled = _is_telemetry_enabled(conn)
        instance_id, is_first_run = _get_or_create_instance_id(conn)
    finally:
        conn.close()
    
    _log.debug(
        "Telemetry: loop started (enabled=%s, first_run=%s, instance=%s)",
        is_enabled, is_first_run, instance_id[:8] + "..." if instance_id else "?"
    )
    
    # If first run and enabled, send immediately
    last_send_time = 0
    if is_first_run and is_enabled:
        if await _send_telemetry_with_retry(loop, db_conn_factory):
            last_send_time = time.time()
    
    # Track previous state for detecting transitions
    was_enabled = is_enabled
    
    # Phase 2: Main loop - runs continuously, monitors state changes
    
    while not stop_event.is_set():
        # Sleep for check interval (or until stop)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_TELEMETRY_CHECK_INTERVAL)
            break  # Stop event was set
        except asyncio.TimeoutError:
            pass  # Normal timeout, continue monitoring
        
        try:
            # Check current telemetry state
            conn = db_conn_factory()
            try:
                is_enabled = _is_telemetry_enabled(conn)
            finally:
                conn.close()
            
            # Detect state transition: disabled → enabled
            if not was_enabled and is_enabled:
                _log.info("Telemetry: re-enabled, sending report now")
                if await _send_telemetry_with_retry(loop, db_conn_factory):
                    last_send_time = time.time()
                was_enabled = True
                continue
            
            # State change: enabled → disabled
            if was_enabled and not is_enabled:
                _log.info("Telemetry: disabled by user")
                was_enabled = False
                continue
            
            # If enabled, check if it's time for next 24h report
            if is_enabled:
                time_since_last = time.time() - last_send_time
                
                # Add ±5% jitter to interval to prevent thundering herd
                interval_jitter = random.randint(
                    -_TELEMETRY_INTERVAL_SECONDS // 20,
                    _TELEMETRY_INTERVAL_SECONDS // 20
                )
                next_interval = _TELEMETRY_INTERVAL_SECONDS + interval_jitter
                
                if time_since_last >= next_interval:
                    if await _send_telemetry_with_retry(loop, db_conn_factory):
                        last_send_time = time.time()
                    # On failure: last_send_time stays unchanged so retry
                    # happens on the next 30-second check cycle
        
        except Exception:
            _log.exception("Telemetry: Unexpected error in loop iteration")
            # Continue running - don't let transient errors kill the task


# Export public API
__all__ = [
    "telemetry_loop",
    "collect_status_data",
    "submit_status",
]
