#!/usr/bin/env python3
#
# tracking/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
JustUp Telemetry Collector

Receives encrypted telemetry data from JustUp instances and provides
an admin dashboard for viewing statistics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ─── LOGGING ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── CONFIGURATION ─────────────────────────────────────────────────────────

def _get_required_env(name: str) -> str:
    """Get required environment variable or raise."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} not set")
    return value


def _get_config():
    """Load configuration, fail fast on missing required vars."""
    # In development mode, allow defaults
    dev_mode = os.getenv("TRACKING_DEV_MODE", "").lower() in ("1", "true", "yes")
    
    if dev_mode:
        logger.warning("Running in DEV MODE with default credentials - DO NOT USE IN PRODUCTION")
        return {
            "api_secret": os.getenv("TRACKING_API_SECRET", "gW6ufXTMejEK?ApO5TtyqIrC=JSxV4er"),
            "admin_user": os.getenv("TRACKING_ADMIN_USER", "admin"),
            "admin_pass": os.getenv("TRACKING_ADMIN_PASS", "devpass123"),
            "db_path": Path(os.getenv("TRACKING_DB_PATH", "/opt/justup-dev/tracking/data/tracking.db")),
        }
    
    # Production: require all secrets
    return {
        "api_secret": _get_required_env("TRACKING_API_SECRET"),
        "admin_user": os.getenv("TRACKING_ADMIN_USER", "admin"),
        "admin_pass": _get_required_env("TRACKING_ADMIN_PASS"),
        "db_path": Path(os.getenv("TRACKING_DB_PATH", "/data/tracking.db")),
    }


# Load config at module level (will fail fast if missing required vars)
try:
    CONFIG = _get_config()
except RuntimeError as e:
    logger.error(str(e))
    raise SystemExit(1)

API_SECRET: str = CONFIG["api_secret"]
ADMIN_USERNAME: str = CONFIG["admin_user"]
ADMIN_PASSWORD: str = CONFIG["admin_pass"]
DB_PATH: Path = CONFIG["db_path"]

# Cookie security: secure=True only when NOT in dev mode (HTTP has no secure cookies)
_SECURE_COOKIES: bool = os.getenv("TRACKING_DEV_MODE", "").lower() not in ("1", "true", "yes")


def _get_tracking_version() -> str:
    """Get tracking version from environment or VERSION file."""
    # First, check environment variable (set during Docker build)
    env_version = os.getenv("APP_VERSION", "").strip()
    if env_version and env_version != "dev":
        return env_version
    
    # Fallback: read from VERSION file
    for path in [Path("/app/VERSION"), Path(__file__).parent / "VERSION", Path("VERSION")]:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    
    return "dev"


# Constants
TRACKING_VERSION = _get_tracking_version()
MAX_PAYLOAD_SIZE = 64 * 1024  # 64KB max payload
RATE_LIMIT_WINDOW_HOURS = 1   # Max 1 report per instance per hour
RETENTION_DAYS = 90           # Keep reports for 90 days

# UUID pattern for validation
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# GeoIP database path (MaxMind GeoLite2-City)
GEOIP_DB_PATH = Path(os.getenv("GEOIP_DB_PATH", "/data/GeoLite2-City.mmdb"))

# Rate limiter (slowapi)
limiter = Limiter(key_func=get_remote_address)


# ─── GEOIP LOOKUP ──────────────────────────────────────────────────────────

_geoip_reader: Optional[Any] = None


def _init_geoip() -> Optional[Any]:
    """Initialize GeoIP reader (lazy load)."""
    global _geoip_reader
    if _geoip_reader is not None:
        return _geoip_reader
    
    try:
        import geoip2.database
        
        if GEOIP_DB_PATH.exists():
            _geoip_reader = geoip2.database.Reader(str(GEOIP_DB_PATH))
            logger.info("GeoIP database loaded: %s", GEOIP_DB_PATH)
        else:
            logger.warning("GeoIP database not found at %s", GEOIP_DB_PATH)
            return None
    except ImportError:
        logger.warning("geoip2 library not installed - country lookup disabled")
        return None
    except Exception as e:
        logger.warning("Failed to load GeoIP database: %s", e)
        return None
    
    return _geoip_reader


def _close_geoip() -> None:
    """Close GeoIP reader on shutdown."""
    global _geoip_reader
    if _geoip_reader is not None:
        try:
            _geoip_reader.close()
            logger.info("GeoIP reader closed")
        except Exception:
            pass
        _geoip_reader = None


# ─── GEOIP BOOTSTRAP ───────────────────────────────────────────────────────

# GeoLite2 database download URL (P3TERX mirror, updated regularly)
GEOIP_DOWNLOAD_URL = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb"

# MMDB format magic bytes (metadata section marker)
MMDB_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"

# Minimum DB size (10MB for City DB which is typically ~60MB)
MIN_GEOIP_DB_SIZE = 10_000_000


def _verify_mmdb(file_path: Path) -> bool:
    """
    Verify that a file is a valid MaxMind MMDB database.
    
    Checks:
    1. File exists and has reasonable size (>10MB for City DB)
    2. Contains the MMDB metadata marker
    3. Can be opened by geoip2 library
    """
    if not file_path.exists():
        return False
    
    file_size = file_path.stat().st_size
    if file_size < MIN_GEOIP_DB_SIZE:
        logger.warning("GeoIP database too small: %d bytes (minimum: %d)", file_size, MIN_GEOIP_DB_SIZE)
        return False
    
    # Check for MMDB metadata marker in last 128KB of file
    try:
        with open(file_path, "rb") as f:
            f.seek(max(0, file_size - 131072))
            tail = f.read()
            if MMDB_METADATA_MARKER not in tail:
                logger.warning("GeoIP database missing MMDB metadata marker")
                return False
    except Exception as e:
        logger.warning("Failed to read GeoIP database: %s", e)
        return False
    
    # Try to open with geoip2 library
    try:
        import geoip2.database
        with geoip2.database.Reader(str(file_path)) as reader:
            if "City" not in reader.metadata().database_type:
                logger.warning("GeoIP database is not City type: %s", reader.metadata().database_type)
                return False
            logger.info("GeoIP database verified: %s, build %d",
                        reader.metadata().database_type, reader.metadata().build_epoch)
    except ImportError:
        logger.debug("geoip2 not installed, skipping deep verification")
    except Exception as e:
        logger.warning("GeoIP database verification failed: %s", e)
        return False
    
    return True


def _bootstrap_geoip_db() -> Optional[Path]:
    """
    Download GeoLite2-City database if not present or invalid.
    
    Returns the path to the database if successful, None otherwise.
    """
    import tempfile
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    
    geoip_path = GEOIP_DB_PATH
    
    # Check if valid database already exists
    if geoip_path.exists():
        if _verify_mmdb(geoip_path):
            logger.debug("GeoIP database already present and valid: %s", geoip_path)
            return geoip_path
        else:
            logger.warning("GeoIP database exists but failed verification, re-downloading...")
    
    logger.info("Downloading GeoIP database from %s...", GEOIP_DOWNLOAD_URL)
    
    # Ensure target directory exists
    geoip_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Download to temp file first, then verify before moving
    temp_path = None
    try:
        # Create temp file in target directory (same filesystem = atomic rename)
        temp_fd, temp_path_str = tempfile.mkstemp(
            suffix=".mmdb.tmp",
            prefix="geoip_",
            dir=str(geoip_path.parent)
        )
        temp_path = Path(temp_path_str)
        os.close(temp_fd)  # Close fd, we'll write by path
        
        # Download with timeout and user agent
        req = Request(
            GEOIP_DOWNLOAD_URL,
            headers={"User-Agent": "justUp-Tracking/1.0"}
        )
        
        with urlopen(req, timeout=120) as response:
            # Check content type
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                logger.error("GeoIP download returned HTML instead of binary data")
                return None
            
            # Download with progress logging
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            hasher = hashlib.sha256()
            
            with open(temp_path, "wb") as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
            
            logger.info("Downloaded %s bytes, SHA256: %s...", f"{downloaded:,}", hasher.hexdigest()[:16])
        
        # Verify the downloaded file
        if not _verify_mmdb(temp_path):
            logger.error("Downloaded GeoIP database failed verification - possibly corrupted")
            return None
        
        # Atomic rename (same filesystem)
        temp_path.rename(geoip_path)
        temp_path = None  # Renamed successfully
        logger.info("GeoIP database installed: %s", geoip_path)
        return geoip_path
        
    except URLError as e:
        logger.warning("Failed to download GeoIP database: %s", e)
        return None
    except Exception as e:
        logger.error("GeoIP database download error: %s", e)
        return None
    finally:
        # Cleanup temp file on failure
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def lookup_country(ip_str: Optional[str]) -> Optional[str]:
    """
    Look up country code for an IP address (IPv4 or IPv6).
    
    Args:
        ip_str: IP address string (e.g., "1.2.3.4" or "2001:db8::1")
    
    Returns:
        Two-letter country code (e.g., "DE", "US") or None if lookup fails.
    """
    if not ip_str:
        return None
    
    # Validate IP address format (prevents injection, handles both IPv4/IPv6)
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        
        # Skip private/reserved addresses
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved:
            return None
    except ValueError:
        return None
    
    reader = _init_geoip()
    if not reader:
        return None
    
    try:
        response = reader.city(ip_str)
        return response.country.iso_code
    except Exception:
        # Address not found in database or other error
        return None


def get_country_distribution(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """
    Get count of instances by country from cached column.
    
    Returns list of (country_code, count) tuples sorted by count descending.
    """
    rows = conn.execute("""
        SELECT country, COUNT(*) as count FROM instances
        WHERE country IS NOT NULL
        GROUP BY country
        ORDER BY count DESC
    """).fetchall()
    
    result = [(row[0], row[1]) for row in rows]
    
    # Count instances without country
    unknown = conn.execute("""
        SELECT COUNT(*) FROM instances WHERE country IS NULL
    """).fetchone()[0]
    
    if unknown > 0:
        result.append(("??", unknown))
    
    return result


# Country code to name mapping (common countries)
COUNTRY_NAMES = {
    "DE": "Germany",
    "US": "United States",
    "GB": "United Kingdom",
    "FR": "France",
    "NL": "Netherlands",
    "AT": "Austria",
    "CH": "Switzerland",
    "BE": "Belgium",
    "PL": "Poland",
    "IT": "Italy",
    "ES": "Spain",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "CZ": "Czech Republic",
    "RU": "Russia",
    "UA": "Ukraine",
    "CA": "Canada",
    "AU": "Australia",
    "JP": "Japan",
    "CN": "China",
    "IN": "India",
    "BR": "Brazil",
    "??": "Unknown",
}


def country_name(code: str) -> str:
    """Get full country name from ISO code."""
    return COUNTRY_NAMES.get(code, code)


def country_flag(code: str) -> str:
    """Return HTML for SVG flag icon based on ISO country code."""
    if not code or len(code) != 2 or code == "??":
        return '<span class="flag-icon flag-unknown">🌐</span>'
    
    # Return img tag pointing to flag SVG (flag-icons use lowercase)
    code_lower = code.lower()
    return f'<img src="/static/flags/{code_lower}.svg" alt="{code}" class="flag-icon" loading="lazy">'

# Session management
SESSION_COOKIE_NAME = "session_token"
SESSION_MAX_AGE = 6 * 60 * 60   # 6 hours inactivity timeout
CSRF_MAX_AGE = 600  # 10 minutes

# HMAC-signed CSRF tokens (no cookie needed, works behind any reverse proxy)
_CSRF_KEY = secrets.token_bytes(32)  # Ephemeral, regenerated on restart


def _make_csrf_token() -> str:
    """Create HMAC-signed CSRF token embedded in HTML form (no cookie needed)."""
    ts = str(int(time.time()))
    sig = hmac.new(_CSRF_KEY, ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _check_csrf_token(token: str) -> bool:
    """Verify CSRF token signature and expiry."""
    if not token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    ts_str, sig = parts
    try:
        ts = int(ts_str)
    except (ValueError, TypeError):
        return False
    if time.time() - ts > CSRF_MAX_AGE:
        return False
    expected = hmac.new(_CSRF_KEY, ts_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# ─── EXPONENTIAL BACKOFF FOR LOGIN ATTEMPTS ─────────────────────────────────

LOGIN_BACKOFF_BASE = 2        # Base for exponential: 2^attempts seconds
LOGIN_BACKOFF_MAX = 3600      # Max lockout: 1 hour
LOGIN_ATTEMPT_RESET = 3600    # Reset attempts after 1 hour of no failures


def _get_client_ip(request: Request) -> str:
    """Get client IP, respecting X-Forwarded-For header for reverse proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # First IP in chain is the original client
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login_lockout(ip: str) -> tuple[bool, int]:
    """
    Check if IP is locked out due to failed login attempts.
    Returns (is_locked, seconds_remaining).
    Also cleans up old entries.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        now = datetime.now(timezone.utc)
        row = conn.execute(
            "SELECT attempts, last_attempt, locked_until FROM login_attempts WHERE ip = ?",
            (ip,)
        ).fetchone()
        
        if not row:
            return False, 0
        
        attempts, last_attempt_str, locked_until_str = row
        last_attempt = datetime.fromisoformat(last_attempt_str)
        
        # Reset if no attempts in the reset window
        if (now - last_attempt).total_seconds() > LOGIN_ATTEMPT_RESET:
            conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
            conn.commit()
            return False, 0
        
        # Check if still locked
        if locked_until_str:
            locked_until = datetime.fromisoformat(locked_until_str)
            if now < locked_until:
                remaining = int((locked_until - now).total_seconds())
                return True, remaining
        
        return False, 0
    finally:
        conn.close()


def record_failed_login(ip: str) -> int:
    """
    Record a failed login attempt and calculate lockout duration.
    Returns the lockout seconds (0 for first attempt).
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        row = conn.execute(
            "SELECT attempts, last_attempt FROM login_attempts WHERE ip = ?",
            (ip,)
        ).fetchone()
        
        if row:
            attempts, last_attempt_str = row
            last_attempt = datetime.fromisoformat(last_attempt_str)
            
            # Reset count if last attempt was long ago
            if (now - last_attempt).total_seconds() > LOGIN_ATTEMPT_RESET:
                attempts = 0
            
            attempts += 1
        else:
            attempts = 1
        
        # Calculate exponential backoff: 2^(attempts-1) seconds, capped
        if attempts >= 2:
            lockout_seconds = min(LOGIN_BACKOFF_BASE ** (attempts - 1), LOGIN_BACKOFF_MAX)
            locked_until = now + timedelta(seconds=lockout_seconds)
            locked_until_str = locked_until.isoformat()
        else:
            lockout_seconds = 0
            locked_until_str = None
        
        # Upsert the record
        conn.execute("""
            INSERT INTO login_attempts (ip, attempts, last_attempt, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                attempts = excluded.attempts,
                last_attempt = excluded.last_attempt,
                locked_until = excluded.locked_until
        """, (ip, attempts, now_str, locked_until_str))
        conn.commit()
        
        logger.warning("Failed login attempt #%d from IP: %s (lockout: %ds)", attempts, ip, lockout_seconds)
        return lockout_seconds
    finally:
        conn.close()


def clear_login_attempts(ip: str) -> None:
    """Clear failed login attempts for an IP after successful login."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
        conn.commit()
    finally:
        conn.close()

# Cleanup tracking (time-based instead of probabilistic)
_last_cleanup: Optional[datetime] = None
CLEANUP_INTERVAL = 24 * 60 * 60  # Once per day

# ─── PYDANTIC MODELS ───────────────────────────────────────────────────────

class SystemInfo(BaseModel):
    """System information from telemetry."""
    os: Optional[str] = None
    osv: Optional[str] = None
    py: Optional[str] = None
    arch: Optional[str] = None
    cpu: Optional[int] = None
    mem: Optional[float] = None
    hh: Optional[int] = None
    uptime: Optional[int] = None      # system uptime in seconds
    docker: Optional[bool] = None     # running in Docker container?
    dist: Optional[str] = None        # OS distribution (e.g. 'Ubuntu 22.04')


class Stats(BaseModel):
    """Usage statistics from telemetry."""
    t: int = Field(default=0, ge=0)    # targets
    u: int = Field(default=0, ge=0)    # users
    nc: list[str] = Field(default_factory=list)  # notification channels
    at: int = Field(default=0, ge=0)   # active (enabled) targets
    oa: int = Field(default=0, ge=0)   # open alerts
    dbs: Optional[float] = None        # DB size in MB


class TelemetryPayload(BaseModel):
    """Validated telemetry payload structure."""
    id: str
    ts: str
    v: str = "0.0.0"
    ip: Optional[str] = None
    sys: SystemInfo = Field(default_factory=SystemInfo)
    st: Stats = Field(default_factory=Stats)
    
    @field_validator('id')
    @classmethod
    def validate_instance_id(cls, v: str) -> str:
        """Validate instance ID is a valid UUID (prevents XSS)."""
        if not UUID_PATTERN.match(v):
            raise ValueError('Instance ID must be a valid UUID')
        return v.lower()


# ─── DATABASE ──────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize database schema with WAL mode."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    try:
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS instances (
                id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ip TEXT,
                country TEXT,
                version TEXT,
                os TEXT,
                os_version TEXT,
                python_version TEXT,
                arch TEXT,
                cpu_count INTEGER,
                memory_gb REAL,
                hostname_hash INTEGER,
                uptime INTEGER,
                docker INTEGER,
                dist TEXT,
                active_targets INTEGER,
                open_alerts INTEGER,
                db_size REAL,
                report_count INTEGER DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                ip TEXT,
                version TEXT,
                targets INTEGER,
                users INTEGER,
                notification_channels TEXT,
                active_targets INTEGER,
                open_alerts INTEGER,
                db_size REAL,
                raw_json TEXT,
                FOREIGN KEY (instance_id) REFERENCES instances(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_reports_instance ON reports(instance_id);
            CREATE INDEX IF NOT EXISTS idx_reports_received ON reports(received_at);
            CREATE INDEX IF NOT EXISTS idx_instances_last_seen ON instances(last_seen);
            
            -- Sessions table (persistent, survives restarts)
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_activity TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            
            -- Login attempts tracking for exponential backoff (by IP)
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt TEXT NOT NULL,
                locked_until TEXT
            );
        """)
        
        # Migration: add country column if missing (for existing DBs)
        try:
            conn.execute("ALTER TABLE instances ADD COLUMN country TEXT")
            logger.info("Added country column to instances table")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Migration: add last_activity column to sessions if missing
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_activity TEXT")
            # Backfill existing sessions with created_at as last_activity
            conn.execute("UPDATE sessions SET last_activity = created_at WHERE last_activity IS NULL")
            logger.info("Added last_activity column to sessions table")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Create index on last_activity (after migration ensures column exists)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity)")
        
        # Migration: add new telemetry columns if missing
        for col, col_type in [
            ("uptime", "INTEGER"),
            ("docker", "INTEGER"),
            ("dist", "TEXT"),
            ("active_targets", "INTEGER"),
            ("open_alerts", "INTEGER"),
            ("db_size", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE instances ADD COLUMN {col} {col_type}")
                logger.info("Added %s column to instances table", col)
            except sqlite3.OperationalError:
                pass
        
        for col, col_type in [
            ("active_targets", "INTEGER"),
            ("open_alerts", "INTEGER"),
            ("db_size", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {col} {col_type}")
                logger.info("Added %s column to reports table", col)
            except sqlite3.OperationalError:
                pass
        
        # Create country index AFTER migration (column must exist)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_country ON instances(country)")
        
        conn.commit()
    finally:
        conn.close()
    
    logger.info("Database initialized at %s", DB_PATH)


@contextmanager
def get_db():
    """Database connection context manager with proper error handling."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_old_reports(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> int:
    """Delete reports older than specified days. Returns count deleted."""
    cursor = conn.execute("""
        DELETE FROM reports 
        WHERE datetime(received_at) < datetime('now', ? || ' days')
    """, (f"-{days}",))
    return cursor.rowcount


def check_rate_limit(conn: sqlite3.Connection, instance_id: str) -> bool:
    """Check if instance is within rate limit. Returns True if allowed."""
    modifier = f"-{RATE_LIMIT_WINDOW_HOURS} hours"
    result = conn.execute("""
        SELECT COUNT(*) FROM reports 
        WHERE instance_id = ? 
        AND datetime(received_at) > datetime('now', ?)
    """, (instance_id, modifier)).fetchone()
    return result[0] == 0


def maybe_cleanup(conn: sqlite3.Connection) -> None:
    """Run cleanup if enough time has passed (time-based, not probabilistic)."""
    global _last_cleanup
    now = datetime.now(timezone.utc)
    
    if _last_cleanup is None or (now - _last_cleanup).total_seconds() > CLEANUP_INTERVAL:
        deleted_reports = cleanup_old_reports(conn)
        if deleted_reports > 0:
            logger.info("Cleaned up %d old reports", deleted_reports)
        
        deleted_sessions = cleanup_expired_sessions(conn)
        if deleted_sessions > 0:
            logger.info("Cleaned up %d expired sessions", deleted_sessions)
        
        deleted_attempts = cleanup_old_login_attempts(conn)
        if deleted_attempts > 0:
            logger.info("Cleaned up %d old login attempt records", deleted_attempts)
        
        _last_cleanup = now


# ─── CRYPTO ────────────────────────────────────────────────────────────────

def decrypt_payload(payload_b64: str, expected_sig: str) -> Optional[dict]:
    """
    Decrypt AES-256-GCM encrypted payload.
    
    Format: Base64(nonce[12] + ciphertext + auth_tag[16])
    
    Returns None on any failure (auth or decrypt).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        logger.error("cryptography library not installed")
        return None
    
    try:
        key_material = API_SECRET.encode('utf-8')
        aes_key = hashlib.sha256(key_material).digest()
        
        # Decode payload
        encrypted_data = base64.b64decode(payload_b64)
        
        # Verify HMAC signature first (authentication)
        sig_check = hmac.new(key_material, encrypted_data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_check, expected_sig):
            logger.warning("HMAC signature verification failed")
            return None
        
        # Extract nonce (12 bytes) and ciphertext
        if len(encrypted_data) < 12 + 16:  # nonce + min auth tag
            logger.warning("Encrypted payload too short")
            return None
            
        nonce = encrypted_data[:12]
        ciphertext = encrypted_data[12:]
        
        # Decrypt with AES-256-GCM
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        
        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        logger.warning("Decryption failed: %s", type(e).__name__)
        return None


# ─── FASTAPI APP ───────────────────────────────────────────────────────────

from fastapi.staticfiles import StaticFiles

# Templates directory
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    
    # Bootstrap GeoIP database (download if missing)
    _bootstrap_geoip_db()
    
    init_db()
    
    # Verify required templates exist
    for tmpl in ("dashboard.html", "instance.html"):
        if not (templates_dir / tmpl).exists():
            raise FileNotFoundError(f"Required template missing: {templates_dir / tmpl}")
    
    logger.info("Telemetry collector started")
    
    yield
    
    # Shutdown
    _close_geoip()
    logger.info("Telemetry collector stopped")


app = FastAPI(
    title="JustUp Telemetry Collector",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# Mount static files (JS for CSP-compliant inline-free templates)
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Attach rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Custom exception for auth redirect (HTTPException with 303 returns JSON, not redirect)
class AuthRequired(Exception):
    """Raised when authentication is required."""
    pass


@app.exception_handler(AuthRequired)
async def auth_redirect_handler(request: Request, exc: AuthRequired):
    """Redirect to login page when authentication is required."""
    return RedirectResponse(url="/login", status_code=303)


templates = Jinja2Templates(directory=str(templates_dir))


def create_session(username: str) -> str:
    """Create a new session token for a user (stored in SQLite)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=SESSION_MAX_AGE)
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at, last_activity) VALUES (?, ?, ?, ?, ?)",
            (token, username, now.isoformat(), expires_at.isoformat(), now.isoformat())
        )
    return token


def verify_session(token: str) -> Optional[str]:
    """Verify session token and return username if valid (from SQLite).
    
    Checks both absolute expiry and inactivity timeout (6 hours).
    Updates last_activity on successful verification to extend session.
    """
    if not token:
        return None
    
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, last_activity FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now_iso)
        ).fetchone()
        
        if row:
            username, last_activity_str = row
            
            # Check inactivity timeout (6 hours)
            last_activity = datetime.fromisoformat(last_activity_str)
            if (now - last_activity).total_seconds() > SESSION_MAX_AGE:
                # Session expired due to inactivity - delete it
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            
            # Valid session - update last_activity and extend expires_at
            new_expires = now + timedelta(seconds=SESSION_MAX_AGE)
            conn.execute(
                "UPDATE sessions SET last_activity = ?, expires_at = ? WHERE token = ?",
                (now_iso, new_expires.isoformat(), token)
            )
            return username
    
    return None


def delete_session(token: str) -> None:
    """Delete a session from the database."""
    if not token:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def cleanup_expired_sessions(conn: sqlite3.Connection) -> int:
    """Delete expired sessions (both by expiry date and inactivity). Returns count deleted."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    inactivity_cutoff = (now - timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    
    cursor = conn.execute(
        "DELETE FROM sessions WHERE expires_at < ? OR last_activity < ?",
        (now_iso, inactivity_cutoff)
    )
    return cursor.rowcount


def cleanup_old_login_attempts(conn: sqlite3.Connection) -> int:
    """Delete old login attempt records (older than reset window)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LOGIN_ATTEMPT_RESET)).isoformat()
    cursor = conn.execute(
        "DELETE FROM login_attempts WHERE last_attempt < ?",
        (cutoff,)
    )
    return cursor.rowcount


def get_current_user(request: Request) -> Optional[str]:
    """Get current user from session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return verify_session(token)
    return None


def require_admin(request: Request) -> str:
    """Dependency that requires admin authentication."""
    username = get_current_user(request)
    if not username:
        raise AuthRequired()
    return username


# ─── PUBLIC ENDPOINT ───────────────────────────────────────────────────────

@app.post("/updater")
@limiter.limit("30/minute")  # Max 30 requests per minute per IP
async def collect_telemetry(
    request: Request,
    response: Response,
    x_sig: str = Header(None, alias="X-Sig"),
    x_ver: str = Header(None, alias="X-Ver"),
):
    """
    Receive encrypted telemetry data from JustUp instances.
    
    Headers:
        X-Sig: HMAC-SHA256 signature (proves shared secret without transmitting it)
        X-Ver: Application version
    
    Body: Base64-encoded AES-256-GCM encrypted JSON
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info(
        "Telemetry request from %s (X-Ver=%s, Content-Length=%s)",
        client_ip,
        x_ver or "missing",
        request.headers.get("content-length", "unknown")
    )
    
    if not x_sig:
        logger.warning("Telemetry request missing X-Sig header from %s", client_ip)
        raise HTTPException(status_code=400, detail="Missing signature")
    
    # Enforce payload size limit (with safe int parsing)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_PAYLOAD_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    
    # Read body with size check (catches chunked encoding bypasses)
    body = await request.body()
    if len(body) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    
    payload_b64 = body.decode('utf-8')
    
    # Decrypt and authenticate (HMAC verification happens inside)
    raw_data = decrypt_payload(payload_b64, x_sig)
    if raw_data is None:
        raise HTTPException(status_code=401, detail="Invalid signature or payload")
    
    # Validate payload structure with Pydantic
    if not isinstance(raw_data, dict):
        raise HTTPException(status_code=400, detail="Payload must be JSON object")
    
    try:
        data = TelemetryPayload.model_validate(raw_data)
    except Exception as e:
        logger.warning("Payload validation failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid payload structure")
    
    now = datetime.now(timezone.utc).isoformat()
    
    # Look up country once (cached in DB for efficient dashboard queries)
    country = lookup_country(data.ip)
    
    with get_db() as conn:
        # Rate limiting: max 1 report per instance per hour
        if not check_rate_limit(conn, data.id):
            return {"status": "throttled", "message": "Rate limit exceeded"}
        
        # UPSERT instance (atomic, no race condition)
        conn.execute("""
            INSERT INTO instances (
                id, first_seen, last_seen, ip, country, version, os, os_version,
                python_version, arch, cpu_count, memory_gb, hostname_hash,
                uptime, docker, dist, active_targets, open_alerts, db_size,
                report_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                last_seen = excluded.last_seen,
                ip = excluded.ip,
                country = excluded.country,
                version = excluded.version,
                os = excluded.os,
                os_version = excluded.os_version,
                python_version = excluded.python_version,
                arch = excluded.arch,
                cpu_count = excluded.cpu_count,
                memory_gb = excluded.memory_gb,
                hostname_hash = excluded.hostname_hash,
                uptime = excluded.uptime,
                docker = excluded.docker,
                dist = excluded.dist,
                active_targets = excluded.active_targets,
                open_alerts = excluded.open_alerts,
                db_size = excluded.db_size,
                report_count = report_count + 1
        """, (
            data.id,
            now,
            now,
            data.ip,
            country,
            data.v,
            data.sys.os,
            data.sys.osv,
            data.sys.py,
            data.sys.arch,
            data.sys.cpu,
            data.sys.mem,
            data.sys.hh,
            data.sys.uptime,
            1 if data.sys.docker else (0 if data.sys.docker is not None else None),
            data.sys.dist,
            data.st.at,
            data.st.oa,
            data.st.dbs,
        ))
        
        # Insert report (store validated Pydantic model, not raw unvalidated data)
        conn.execute("""
            INSERT INTO reports (
                instance_id, received_at, ip, version, targets, users,
                notification_channels, active_targets, open_alerts, db_size,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.id,
            now,
            data.ip,
            data.v,
            data.st.t,
            data.st.u,
            json.dumps(data.st.nc),
            data.st.at,
            data.st.oa,
            data.st.dbs,
            json.dumps(data.model_dump()),
        ))
        
        # Time-based cleanup (runs at most once per CLEANUP_INTERVAL)
        maybe_cleanup(conn)
    
    return {"status": "ok", "instance": data.id}


# ─── ADMIN DASHBOARD ───────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def login_page(request: Request, error: str = None):
    """Show login form with CSRF token."""
    # If already logged in, redirect to admin
    if get_current_user(request):
        return RedirectResponse(url="/admin", status_code=303)
    
    # Generate HMAC-signed CSRF token (no cookie needed)
    csrf_token = _make_csrf_token()
    
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "csrf_token": csrf_token,
    })


@app.post("/login")
@limiter.limit("10/minute")  # Rate limit as additional protection
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    """Process login form submission with CSRF validation and exponential backoff."""
    client_ip = _get_client_ip(request)
    
    # Check if IP is locked out
    is_locked, remaining_seconds = check_login_lockout(client_ip)
    if is_locked:
        logger.warning("Login blocked for IP %s: %d seconds remaining", client_ip, remaining_seconds)
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": f"Too many failed attempts. Try again in {remaining_seconds} seconds.",
            "csrf_token": _make_csrf_token(),
            "lockout_seconds": remaining_seconds,
        }, status_code=429)
    
    # Validate HMAC-signed CSRF token (no cookie involved)
    if not _check_csrf_token(csrf_token):
        logger.warning("CSRF validation failed for login attempt")
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    
    # Encode to bytes for constant-time comparison (secrets.compare_digest requires ASCII or bytes)
    is_valid_user = secrets.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    is_valid_pass = secrets.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    
    if not (is_valid_user and is_valid_pass):
        # Record failed attempt and get lockout duration
        lockout_seconds = record_failed_login(client_ip)
        error_msg = "Invalid username or password"
        if lockout_seconds > 0:
            error_msg = f"Invalid credentials. Locked out for {lockout_seconds} seconds."
        
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": error_msg,
            "csrf_token": _make_csrf_token(),
            "lockout_seconds": lockout_seconds,
        }, status_code=401)
    
    # Successful login - clear any failed attempts
    clear_login_attempts(client_ip)
    
    # Create session
    token = create_session(username)
    
    # Redirect to admin with session cookie
    redirect = RedirectResponse(url="/admin", status_code=303)
    redirect.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_SECURE_COOKIES,
        samesite="lax",
    )
    return redirect


@app.get("/logout")
async def logout(request: Request):
    """Log out and clear session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    delete_session(token)
    
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/admin", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def admin_dashboard(request: Request, username: str = Depends(require_admin)):
    """Admin dashboard showing telemetry statistics."""
    with get_db() as conn:
        # Get summary stats
        total_instances = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        total_reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        
        # Active instances (seen in last 48 hours)
        active_instances = conn.execute("""
            SELECT COUNT(*) FROM instances 
            WHERE datetime(last_seen) > datetime('now', '-48 hours')
        """).fetchone()[0]
        
        # Version distribution
        versions = conn.execute("""
            SELECT version, COUNT(*) as count 
            FROM instances 
            WHERE version IS NOT NULL
            GROUP BY version 
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()
        
        # OS distribution
        os_dist = conn.execute("""
            SELECT os, COUNT(*) as count 
            FROM instances 
            WHERE os IS NOT NULL
            GROUP BY os 
            ORDER BY count DESC
        """).fetchall()
        
        # Recent instances
        recent_instances = conn.execute("""
            SELECT id, ip, version, os, arch, cpu_count, memory_gb, 
                   first_seen, last_seen, report_count,
                   docker, dist, uptime, active_targets, open_alerts, db_size,
                   country
            FROM instances 
            ORDER BY last_seen DESC 
            LIMIT 50
        """).fetchall()
        
        # Aggregate stats
        agg_stats = conn.execute("""
            SELECT 
                SUM(targets) as total_targets,
                SUM(users) as total_users,
                AVG(targets) as avg_targets,
                AVG(users) as avg_users,
                SUM(active_targets) as total_active_targets,
                SUM(open_alerts) as total_open_alerts
            FROM (
                SELECT instance_id, MAX(targets) as targets, MAX(users) as users,
                       MAX(active_targets) as active_targets, MAX(open_alerts) as open_alerts
                FROM reports
                GROUP BY instance_id
            )
        """).fetchone()
        
        # Country distribution (GeoIP lookup)
        country_dist = get_country_distribution(conn)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "username": username,
        "tracking_version": TRACKING_VERSION,
        "total_instances": total_instances,
        "active_instances": active_instances,
        "total_reports": total_reports,
        "versions": versions,
        "os_dist": os_dist,
        "country_dist": country_dist,
        "country_name": country_name,
        "country_flag": country_flag,
        "recent_instances": recent_instances,
        "total_targets": agg_stats[0] or 0,
        "total_users": agg_stats[1] or 0,
        "avg_targets": round(agg_stats[2] or 0, 1),
        "avg_users": round(agg_stats[3] or 0, 1),
        "total_active_targets": agg_stats[4] or 0,
        "total_open_alerts": agg_stats[5] or 0,
    })


@app.get("/admin/instance/{instance_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def admin_instance_detail(
    request: Request,
    instance_id: str,
    username: str = Depends(require_admin),
):
    """Detailed view of a single instance."""
    # Validate instance_id format (security)
    if not UUID_PATTERN.match(instance_id):
        raise HTTPException(status_code=400, detail="Invalid instance ID format")
    
    with get_db() as conn:
        instance = conn.execute(
            "SELECT * FROM instances WHERE id = ?",
            (instance_id.lower(),)
        ).fetchone()
        
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        
        reports = conn.execute("""
            SELECT received_at, ip, version, targets, users, notification_channels,
                   active_targets, open_alerts, db_size
            FROM reports 
            WHERE instance_id = ?
            ORDER BY received_at DESC
            LIMIT 100
        """, (instance_id.lower(),)).fetchall()
    
    return templates.TemplateResponse("instance.html", {
        "request": request,
        "username": username,
        "tracking_version": TRACKING_VERSION,
        "instance": instance,
        "reports": reports,
    })


@app.post("/admin/purge-db")
@limiter.limit("5/minute")
async def purge_database(
    request: Request,
    username: str = Depends(require_admin),
):
    """Purge all telemetry data (instances, reports). Sessions are preserved."""
    with get_db() as conn:
        deleted_reports = conn.execute("DELETE FROM reports").rowcount
        deleted_instances = conn.execute("DELETE FROM instances").rowcount
        logger.warning(
            "Database purged by %s: %d instances, %d reports deleted",
            username, deleted_instances, deleted_reports,
        )
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/")
async def root():
    """Return 404 on root access."""
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


# ─── MAIN ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("HOST", "127.0.0.1")  # Loopback by default (safe)
    port = int(os.getenv("PORT", "8080"))
    
    if host == "0.0.0.0":
        logger.warning("Binding to 0.0.0.0 - ensure firewall is configured!")
    
    uvicorn.run(app, host=host, port=port)
