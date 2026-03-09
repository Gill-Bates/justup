#!/usr/bin/env python3
#
# app/_cython/pop_locations.pyx
# Copyright (C) 2025-2026 Gill-Bates http://github.com/Gill-Bates
#

"""
POP Locations database layer.

Single Source of Truth for hostname-based geolocation.
Replaces static Python dicts with SQLite-backed lookups.

Lifecycle:
1. First start: Table is empty → bootstrap from seed data
2. Runtime: SQL-based lookup with priority/confidence ranking
3. Monthly refresh: Update from PeeringDB, preserve manual entries
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Optional

from app.utils.time import utcnow

from app.db._pl import lkp_h, lkp_p, lkp_t

_log = logging.getLogger(__name__)

# Type alias
Location = dict[str, Any]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _ensure_row_factory(conn: sqlite3.Connection) -> None:
    """
    Ensure row mapping access works for this connection.

    Many functions in this module use `row["col"]` and `dict(row)`, which require
    `conn.row_factory = sqlite3.Row` (or a compatible row_factory that returns a
    dict-like object).
    """
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row


def _normalize_pop_code(code: str) -> str:
    """Normalize a POP code for comparison.
    
    Args:
        code: Raw POP code (e.g. "FRA", "fra1", " Fra2 ")
    
    Returns:
        Lowercase, stripped version (e.g. "fra", "fra1", "fra2")
    """
    return code.lower().strip()


def _normalize_city(city: str) -> str:
    """Normalize city name (strip whitespace, title case).
    
    Args:
        city: Raw city name (e.g. "  frankfurt ", "PARIS")
    
    Returns:
        Title-cased, stripped version (e.g. "Frankfurt", "Paris")
    """
    return city.strip().title() if city else ""


def _normalize_country(country: str) -> str:
    """Normalize country code (strip whitespace, uppercase ISO-3166).
    
    Args:
        country: Raw country code (e.g. " de ", "De")
    
    Returns:
        Uppercase, stripped version (e.g. "DE")
    """
    return country.strip().upper() if country else ""


# ---------------------------------------------------------------------------
# Schema migrations (no framework, inline checks)
# ---------------------------------------------------------------------------

def _get_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Get column names for a table."""
    # Whitelist: only allow valid identifiers (prevents SQL injection)
    if not table.isidentifier():
        raise ValueError(f"Invalid table name: {table}")
    cursor = conn.execute(f"PRAGMA table_info([{table}])")
    return {row[1] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Sources and confidence levels
# ---------------------------------------------------------------------------

# Source identifiers (where did this entry come from?)
SOURCE_PEERINGDB = "peeringdb"   # Imported from PeeringDB API
SOURCE_UNLOCODE = "unlocode"     # Imported from UN/LOCODE database
SOURCE_SEED = "seed"             # Built-in seed data (stable, curated)
SOURCE_LEARNED = "learned"       # Auto-learned from PTR patterns
SOURCE_MANUAL = "manual"         # Manually added via admin API

# Confidence levels (how certain are we about this mapping?)
# - HIGH (100): Authoritative source (manual, PeeringDB verified, seed)
# - MEDIUM (70): Inferred from patterns, unverified but plausible
# - LOW (40): Heuristic match, may need review
CONFIDENCE_HIGH = 100
CONFIDENCE_MEDIUM = 70
CONFIDENCE_LOW = 40

# Priority levels (lookup order - lower wins!)
# - CANONICAL (0): Primary code for this location (e.g., "fra" = Frankfurt)
# - ALIAS (10): Known alternative (e.g., "ffm" = Frankfurt)
# - LEARNED (20): Auto-discovered patterns, lower trust
#
# Priority is for ORDERING within same alias_code matches.
# Lower priority = preferred match.
# Example: If "fra" exists as both CANONICAL and LEARNED,
#          the CANONICAL entry wins (priority 0 < 20).
PRIORITY_CANONICAL = 0
PRIORITY_ALIAS = 10
PRIORITY_LEARNED = 20

# ---------------------------------------------------------------------------
# Safety thresholds for retire logic
# ---------------------------------------------------------------------------

# Minimum records to process before allowing retire (prevents empty-run disasters)
MIN_PROCESSED_FOR_RETIRE = 10

# Maximum percentage of active entries that can be retired in one run
# Protects against shrinking dataset attacks (e.g., API returns only 5% of data)
MAX_RETIRE_PERCENT = 30.0


# ---------------------------------------------------------------------------
# Normalization (enforce lowercase codes, trim whitespace)
# ---------------------------------------------------------------------------

def _with_row_factory(fn):
    """Decorator to ensure row_factory is set for dict-like row access."""
    # Note: @functools.wraps removed - incompatible with Cython binding=False
    # (builtin_function_or_method objects have read-only __name__ attribute)
    def wrapper(conn, *args, **kwargs):
        _ensure_row_factory(conn)
        return fn(conn, *args, **kwargs)
    return wrapper


_SEED_DATA: list[tuple[str, str, str, str, float, float]] = [
    # (canonical_code, alias_code, city, country, lat, lon)
    # Core EU
    ("fra", "fra", "Frankfurt", "DE", 50.1109, 8.6821),
    ("fra", "ffm", "Frankfurt", "DE", 50.1109, 8.6821),
    ("ams", "ams", "Amsterdam", "NL", 52.3676, 4.9041),
    ("lon", "lon", "London", "GB", 51.5074, -0.1278),
    ("par", "par", "Paris", "FR", 48.8566, 2.3522),
    ("zrh", "zrh", "Zurich", "CH", 47.3769, 8.5417),
    ("mil", "mil", "Milan", "IT", 45.4642, 9.1900),
    ("mad", "mad", "Madrid", "ES", 40.4168, -3.7038),
    ("vie", "vie", "Vienna", "AT", 48.2082, 16.3738),
    ("waw", "waw", "Warsaw", "PL", 52.2297, 21.0122),
    ("prg", "prg", "Prague", "CZ", 50.0755, 14.4378),
    
    # Germany
    ("ber", "ber", "Berlin", "DE", 52.5200, 13.4050),
    ("ham", "ham", "Hamburg", "DE", 53.5511, 9.9937),
    ("muc", "muc", "Munich", "DE", 48.1351, 11.5820),
    ("muc", "mcn", "Munich", "DE", 48.1351, 11.5820),
    ("dus", "dus", "Düsseldorf", "DE", 51.2277, 6.7735),
    ("cgn", "cgn", "Cologne", "DE", 50.9375, 6.9603),
    ("stu", "stu", "Stuttgart", "DE", 48.7758, 9.1829),
    ("nue", "nue", "Nuremberg", "DE", 49.4521, 11.0767),
    ("nue", "nbg", "Nuremberg", "DE", 49.4521, 11.0767),
    ("han", "han", "Hannover", "DE", 52.3759, 9.7320),
    ("dre", "dre", "Dresden", "DE", 51.0504, 13.7373),
    ("lei", "lei", "Leipzig", "DE", 51.3397, 12.3731),
    ("bre", "bre", "Bremen", "DE", 53.0793, 8.8017),
    ("dor", "dor", "Dortmund", "DE", 51.5136, 7.4653),
    ("ess", "ess", "Essen", "DE", 51.4556, 7.0116),
    ("fsn", "fsn", "Falkenstein", "DE", 50.4756, 12.3650),
    ("wup", "wup", "Wuppertal", "DE", 51.2562, 7.1508),
    
    # Provider-specific aliases (Colt, Lumen, Aorta, etc.)
    ("fra", "frf", "Frankfurt", "DE", 50.1109, 8.6821),
    ("fra", "frf1", "Frankfurt", "DE", 50.1109, 8.6821),
    ("fra", "fra04", "Frankfurt", "DE", 50.1109, 8.6821),
    ("ams", "ams01", "Amsterdam", "NL", 52.3676, 4.9041),
    ("wup", "wup01", "Wuppertal", "DE", 51.2562, 7.1508),
    
    # US major metros
    ("nyc", "nyc", "New York", "US", 40.7128, -74.0060),
    ("lax", "lax", "Los Angeles", "US", 34.0522, -118.2437),
    ("ord", "ord", "Chicago", "US", 41.8781, -87.6298),
    ("ord", "chi", "Chicago", "US", 41.8781, -87.6298),
    ("dfw", "dfw", "Dallas", "US", 32.7767, -96.7970),
    ("iad", "iad", "Ashburn", "US", 39.0438, -77.4874),
    ("iad", "ash", "Ashburn", "US", 39.0438, -77.4874),
    ("sjc", "sjc", "San Jose", "US", 37.3382, -121.8863),
    ("sfo", "sfo", "San Francisco", "US", 37.7749, -122.4194),
    ("sea", "sea", "Seattle", "US", 47.6062, -122.3321),
    ("atl", "atl", "Atlanta", "US", 33.7490, -84.3880),
    ("mia", "mia", "Miami", "US", 25.7617, -80.1918),
    ("bos", "bos", "Boston", "US", 42.3601, -71.0589),
    ("phl", "phl", "Philadelphia", "US", 39.9526, -75.1652),
    ("phx", "phx", "Phoenix", "US", 33.4484, -112.0740),
    ("den", "den", "Denver", "US", 39.7392, -104.9903),
    ("hou", "hou", "Houston", "US", 29.7604, -95.3698),
    
    # APAC
    ("sin", "sin", "Singapore", "SG", 1.3521, 103.8198),
    ("hkg", "hkg", "Hong Kong", "HK", 22.3193, 114.1694),
    ("nrt", "nrt", "Tokyo", "JP", 35.6895, 139.6917),
    ("nrt", "tyo", "Tokyo", "JP", 35.6895, 139.6917),
    ("osa", "osa", "Osaka", "JP", 34.6937, 135.5023),
    ("syd", "syd", "Sydney", "AU", -33.8688, 151.2093),
    ("mel", "mel", "Melbourne", "AU", -37.8136, 144.9631),
    ("icn", "icn", "Seoul", "KR", 37.5665, 126.9780),
    ("bom", "bom", "Mumbai", "IN", 19.0760, 72.8777),
    ("del", "del", "Delhi", "IN", 28.7041, 77.1025),
    
    # Nordics
    ("hel", "hel", "Helsinki", "FI", 60.1699, 24.9384),
    ("sto", "sto", "Stockholm", "SE", 59.3293, 18.0686),
    ("osl", "osl", "Oslo", "NO", 59.9139, 10.7522),
    ("cph", "cph", "Copenhagen", "DK", 55.6761, 12.5683),
    
    # Other EU
    ("dub", "dub", "Dublin", "IE", 53.3498, -6.2603),
    ("lis", "lis", "Lisbon", "PT", 38.7223, -9.1393),
    ("bru", "bru", "Brussels", "BE", 50.8503, 4.3517),
    ("gva", "gva", "Geneva", "CH", 46.2044, 6.1432),
    ("mrs", "mrs", "Marseille", "FR", 43.2965, 5.3698),
    ("laut", "laut", "Lauterbourg", "FR", 48.9700, 8.1800),
    ("bcn", "bcn", "Barcelona", "ES", 41.3851, 2.1734),
    ("rom", "rom", "Rome", "IT", 41.9028, 12.4964),
    ("rom", "fco", "Rome", "IT", 41.9028, 12.4964),
    ("bud", "bud", "Budapest", "HU", 47.4979, 19.0402),
    ("ath", "ath", "Athens", "GR", 37.9838, 23.7275),
    ("ist", "ist", "Istanbul", "TR", 41.0082, 28.9784),
]


def _validate_seed_data() -> None:
    """Validate seed data for duplicate aliases (fails at import time)."""
    seen_aliases: dict[str, str] = {}
    for canonical, alias, city, country, lat, lon in _SEED_DATA:
        if alias in seen_aliases:
            raise ValueError(
                f"Duplicate alias '{alias}': "
                f"used by both '{seen_aliases[alias]}' and '{canonical}'"
            )
        seen_aliases[alias] = canonical


# Validate at module import time
_validate_seed_data()


# ---------------------------------------------------------------------------
# Table management
# ---------------------------------------------------------------------------

def is_pop_table_empty(conn: sqlite3.Connection) -> bool:
    """Check if pop_locations table is empty."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations").fetchone()
    return row[0] == 0


def bootstrap_pop_locations(conn: sqlite3.Connection) -> int:
    """
    Populate pop_locations table with seed data.
    
    Returns:
        Number of rows inserted.
    """
    now = utcnow()
    inserted = 0
    
    try:
        conn.execute("BEGIN")
        for canonical, alias, city, country, lat, lon in _SEED_DATA:
            # Determine priority based on whether this is canonical or alias
            priority = PRIORITY_CANONICAL if canonical == alias else PRIORITY_ALIAS
            
            try:
                conn.execute(
                    """
                    INSERT INTO pop_locations 
                        (canonical_code, alias_code, city, country, lat, lon, 
                         source, confidence, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (canonical, alias, city, country, lat, lon,
                     SOURCE_SEED, CONFIDENCE_HIGH, priority, now, now),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Already exists (UNIQUE constraint)
                pass
        
        conn.commit()
        _log.info("Bootstrapped %d POP locations from seed data", inserted)
        return inserted
    except Exception:
        conn.rollback()
        raise


def ensure_pop_locations(conn: sqlite3.Connection) -> None:
    """
    Ensure pop_locations table is populated with seed data.
    
    Call this on app startup AFTER init_db() has created the schema.
    Schema migrations are handled by app.utils.migration.
    
    Note: Schema is defined in sqlite.py (single source of truth).
    """
    # Bootstrap seed data if table is empty
    if is_pop_table_empty(conn):
        _log.info("POP locations table empty, bootstrapping...")
        bootstrap_pop_locations(conn)


# ---------------------------------------------------------------------------
# Lookup functions (Cython implementation)
# ---------------------------------------------------------------------------
#
lookup_pop_by_tokens = lkp_t
lookup_pop_by_prefix = lkp_p
lookup_pop_by_hostname = lkp_h


def touch_pop_location(conn: sqlite3.Connection, alias_code: str) -> bool:
    """
    Update last_seen_at timestamp for a POP location.
    
    Call this when a POP alias is successfully used in a lookup
    to track usage patterns. Useful for:
    - Identifying unused entries for retirement
    - Prioritizing frequently-used aliases
    - Analytics on network path patterns
    
    Note: Don't call on every lookup (expensive). Use for:
    - Periodic sampling (every Nth match)
    - New alias learning confirmation
    - Manual touch via admin API
    
    Returns:
        True if updated, False if alias not found.
    """
    now = utcnow()
    cur = conn.execute(
        "UPDATE pop_locations SET last_seen_at = ? WHERE alias_code = ? AND retired = 0",
        (now, _normalize_pop_code(alias_code)),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Learning / candidate management
# ---------------------------------------------------------------------------

def record_candidate_alias(
    conn: sqlite3.Connection,
    alias_code: str,
    canonical_guess: Optional[str] = None,
) -> None:
    """
    Record a potential new POP alias for later review.
    
    Increments seen_count if already exists.
    """
    now = utcnow()
    alias_code = _normalize_pop_code(alias_code)
    
    try:
        conn.execute(
            """
            INSERT INTO pop_alias_candidates 
                (alias_code, canonical_guess, seen_count, last_seen, reviewed, created_at)
            VALUES (?, ?, 1, ?, 0, ?)
            ON CONFLICT(alias_code) DO UPDATE SET 
                seen_count = seen_count + 1,
                last_seen = excluded.last_seen,
                canonical_guess = COALESCE(excluded.canonical_guess, canonical_guess)
            """,
            (alias_code, canonical_guess, now, now),
        )
        conn.commit()
    except sqlite3.Error as e:
        _log.debug("Failed to record candidate alias %s: %s", alias_code, e)


def promote_candidate_to_learned(
    conn: sqlite3.Connection,
    alias_code: str,
    canonical_code: str,
    min_seen_count: int = 5,
) -> bool:
    """
    Promote a candidate alias to a learned POP location.
    
    Only promotes if seen_count >= min_seen_count.
    
    Returns:
        True if promoted, False otherwise.
    """
    _ensure_row_factory(conn)
    alias_code = _normalize_pop_code(alias_code)
    canonical_code = _normalize_pop_code(canonical_code)
    
    try:
        conn.execute("BEGIN IMMEDIATE")
        
        # Get candidate
        row = conn.execute(
            """
            SELECT seen_count FROM pop_alias_candidates 
            WHERE alias_code = ? AND reviewed = 0
            """,
            (alias_code,),
        ).fetchone()
        
        if not row or row["seen_count"] < min_seen_count:
            conn.rollback()
            return False
        
        # Get canonical location
        canonical = conn.execute(
            """
            SELECT city, country, lat, lon FROM pop_locations
            WHERE canonical_code = ? AND alias_code = canonical_code
            LIMIT 1
            """,
            (canonical_code,),
        ).fetchone()
        
        if not canonical:
            conn.rollback()
            return False
        
        now = utcnow()
        
        # Insert as learned alias
        conn.execute(
            """
            INSERT INTO pop_locations 
                (canonical_code, alias_code, city, country, lat, lon,
                 source, confidence, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canonical_code, alias_code, canonical["city"], canonical["country"],
             canonical["lat"], canonical["lon"], SOURCE_LEARNED,
             CONFIDENCE_MEDIUM, PRIORITY_LEARNED, now, now),
        )
        
        # Mark candidate as reviewed
        conn.execute(
            "UPDATE pop_alias_candidates SET reviewed = 1 WHERE alias_code = ?",
            (alias_code,),
        )
        
        conn.commit()
        _log.info("Promoted candidate %s → %s as learned alias", alias_code, canonical_code)
        return True
        
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Refresh / import functions
# ---------------------------------------------------------------------------

def upsert_pop_location(
    conn: sqlite3.Connection,
    canonical_code: str,
    alias_code: str,
    city: str,
    country: str,
    lat: float,
    lon: float,
    source: str,
    confidence: int,
    priority: int,
    import_run_id: Optional[str] = None,
) -> bool:
    """
    Insert or update a POP location.
    
    Manual entries are never overwritten.
    
    Args:
        import_run_id: Optional run ID for delta tracking
    
    Returns:
        True if inserted/updated, False if skipped.
    """
    now = utcnow()
    
    # Normalize inputs (enforce lowercase codes, length limits)
    canonical_code = _normalize_pop_code(canonical_code)
    alias_code = _normalize_pop_code(alias_code)
    city = _normalize_city(city)
    country = _normalize_country(country)
    
    # Clamp coordinates to valid range
    lat = max(-90.0, min(90.0, float(lat)))
    lon = max(-180.0, min(180.0, float(lon)))
    
    # Clamp confidence/priority
    confidence = max(0, min(100, int(confidence)))
    priority = max(0, int(priority))
    
    # Check if manual entry exists
    existing = conn.execute(
        """
        SELECT source FROM pop_locations 
        WHERE alias_code = ? AND canonical_code = ?
        """,
        (alias_code, canonical_code),
    ).fetchone()
    
    if existing and existing[0] == SOURCE_MANUAL:
        # Never overwrite manual entries, but update last_seen
        if import_run_id:
            conn.execute(
                """
                UPDATE pop_locations 
                SET last_seen_at = ?, import_run_id = ?
                WHERE alias_code = ? AND canonical_code = ?
                """,
                (now, import_run_id, alias_code, canonical_code),
            )
        return False
    
    try:
        conn.execute(
            """
            INSERT INTO pop_locations 
                (canonical_code, alias_code, city, country, lat, lon,
                 source, confidence, priority, created_at, updated_at,
                 last_seen_at, import_run_id, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(alias_code, canonical_code) DO UPDATE SET
                city = excluded.city,
                country = excluded.country,
                lat = excluded.lat,
                lon = excluded.lon,
                source = excluded.source,
                confidence = excluded.confidence,
                priority = excluded.priority,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at,
                import_run_id = excluded.import_run_id,
                retired = 0
            WHERE pop_locations.source != 'manual'
            """,
            (canonical_code, alias_code, city, country, lat, lon,
             source, confidence, priority, now, now, now, import_run_id),
        )
        return True
    except sqlite3.Error as e:
        _log.debug("Failed to upsert POP location %s/%s: %s", canonical_code, alias_code, e)
        return False


def get_pop_location_count(conn: sqlite3.Connection) -> int:
    """Get total number of POP locations."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations").fetchone()
    return row[0]


@_with_row_factory
def list_pop_locations(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    include_retired: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List POP locations with optional filtering."""
    # retired_clause is internally controlled (safe for f-string)
    retired_clause = "" if include_retired else "AND retired = 0"
    
    if source:
        rows = conn.execute(
            f"""
            SELECT * FROM pop_locations 
            WHERE source = ? {retired_clause}
            ORDER BY canonical_code, priority
            LIMIT ? OFFSET ?
            """,
            (source, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT * FROM pop_locations 
            WHERE 1=1 {retired_clause}
            ORDER BY canonical_code, priority
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Import run management
# ---------------------------------------------------------------------------

def start_import_run(conn: sqlite3.Connection, source: str) -> str:
    """
    Start a new import run and return the run_id.
    
    Args:
        source: Import source (e.g., 'peeringdb', 'unlocode')
    
    Returns:
        Unique run_id for this import
    """
    now = utcnow()
    now_str = now.isoformat().replace(':', '-')
    run_id = f"{source}_{now_str}_{uuid.uuid4().hex[:8]}"
    
    conn.execute(
        """
        INSERT INTO pop_import_runs 
            (run_id, source, started_at, status)
        VALUES (?, ?, ?, 'running')
        """,
        (run_id, source, now),
    )
    conn.commit()
    
    _log.info("Started import run %s for source %s", run_id, source)
    return run_id


def finish_import_run(
    conn: sqlite3.Connection,
    run_id: str,
    records_processed: int,
    records_inserted: int,
    records_updated: int,
    records_retired: int = 0,
    error_message: Optional[str] = None,
) -> None:
    """
    Finish an import run and record statistics.
    """
    now = utcnow()
    status = "failed" if error_message else "completed"
    
    conn.execute(
        """
        UPDATE pop_import_runs SET
            finished_at = ?,
            status = ?,
            records_processed = ?,
            records_inserted = ?,
            records_updated = ?,
            records_retired = ?,
            error_message = ?
        WHERE run_id = ?
        """,
        (now, status, records_processed, records_inserted, 
         records_updated, records_retired, error_message, run_id),
    )
    conn.commit()
    
    _log.info(
        "Finished import run %s: processed=%d, inserted=%d, updated=%d, retired=%d, status=%s",
        run_id, records_processed, records_inserted, records_updated, records_retired, status
    )


def retire_stale_entries(
    conn: sqlite3.Connection,
    source: str,
    run_id: str,
    records_processed: int = 0,
) -> int:
    """
    Mark entries as retired if they weren't seen in the current import run.
    
    Only affects entries from the specified source.
    Manual and seed entries are never retired.
    
    Safety guards:
    - Requires minimum processed count (prevents empty-run disasters)
    - Limits max retire percentage (prevents shrinking dataset attacks)
    
    Args:
        source: The import source to check
        run_id: The current import run ID
        records_processed: Number of records processed in this run
    
    Returns:
        Number of entries retired (0 if safety guards triggered)
    """
    # Explicit guard: never retire manual or seed entries (prevents accidental data loss)
    if source in (SOURCE_MANUAL, SOURCE_SEED):
        _log.debug("Refusing to retire %s entries (protected source)", source)
        return 0
    
    # Safety: Minimum processed threshold
    if records_processed < MIN_PROCESSED_FOR_RETIRE:
        _log.warning(
            "Skipping retire for %s: processed=%d < min=%d",
            source, records_processed, MIN_PROCESSED_FOR_RETIRE
        )
        return 0
    
    # Count active entries from this source
    row = conn.execute(
        """
        SELECT COUNT(*) FROM pop_locations 
        WHERE source = ? AND retired = 0
        """,
        (source,),
    ).fetchone()
    active_count = row[0]
    
    if active_count == 0:
        return 0
    
    # Count how many would be retired
    row = conn.execute(
        """
        SELECT COUNT(*) FROM pop_locations 
        WHERE source = ?
          AND (import_run_id IS NULL OR import_run_id != ?)
          AND retired = 0
        """,
        (source, run_id),
    ).fetchone()
    would_retire = row[0]
    
    # Safety: Max retire percentage
    retire_percent = (would_retire / active_count) * 100
    if retire_percent > MAX_RETIRE_PERCENT:
        _log.warning(
            "Skipping retire for %s: would retire %.1f%% (%d/%d) > max %.1f%%. "
            "Possible shrinking dataset attack or API issue.",
            source, retire_percent, would_retire, active_count, MAX_RETIRE_PERCENT
        )
        return 0
    
    # Actually retire entries from this source that weren't updated
    result = conn.execute(
        """
        UPDATE pop_locations SET
            retired = 1,
            updated_at = ?
        WHERE source = ?
          AND (import_run_id IS NULL OR import_run_id != ?)
          AND retired = 0
        """,
        (utcnow(), source, run_id),
    )
    retired_count = result.rowcount
    conn.commit()
    
    if retired_count > 0:
        _log.info(
            "Retired %d stale entries from source %s (%.1f%% of %d active)",
            retired_count, source, retire_percent, active_count
        )
    
    return retired_count


@_with_row_factory
def get_last_import_run(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
) -> Optional[dict]:
    """Get the last completed import run, optionally filtered by source."""
    if source:
        row = conn.execute(
            """
            SELECT * FROM pop_import_runs 
            WHERE source = ? AND status = 'completed'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (source,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM pop_import_runs 
            WHERE status = 'completed'
            ORDER BY started_at DESC
            LIMIT 1
            """,
        ).fetchone()
    
    return dict(row) if row else None


@_with_row_factory
def get_import_run_stats(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Get recent import run statistics."""
    rows = conn.execute(
        """
        SELECT * FROM pop_import_runs 
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    
    return [dict(row) for row in rows]


def count_active_pop_locations(conn: sqlite3.Connection) -> int:
    """Get count of non-retired POP locations."""
    row = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE retired = 0"
    ).fetchone()
    return row[0]


def count_retired_pop_locations(conn: sqlite3.Connection) -> int:
    """Get count of retired POP locations."""
    row = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE retired = 1"
    ).fetchone()
    return row[0]


def count_external_pop_locations(conn: sqlite3.Connection) -> int:
    """Count POP locations from external sources (not seed/manual)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source NOT IN ('seed', 'manual')"
    ).fetchone()
    return row[0] if row else 0


def recover_stale_import_runs(conn: sqlite3.Connection) -> int:
    """
    Mark stale import runs as failed (started but never finished).
    
    Should be called at startup to clean up orphaned runs from crashes.
    
    Returns:
        Number of runs marked as failed
    """
    now = utcnow()
    result = conn.execute(
        """
        UPDATE pop_import_runs SET
            finished_at = ?,
            status = 'failed',
            error_message = 'Recovered at startup - process crashed or was killed'
        WHERE status = 'running'
        """,
        (now,),
    )
    recovered = result.rowcount
    conn.commit()
    
    if recovered > 0:
        _log.warning("Recovered %d stale import runs (marked as failed)", recovered)
    
    return recovered


def get_source_stats(conn: sqlite3.Connection, source: str) -> dict:
    """
    Get statistics for a specific source.
    
    Returns:
        Dict with active_count, retired_count, last_run info
    """
    active = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = ? AND retired = 0",
        (source,),
    ).fetchone()[0]
    
    retired = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = ? AND retired = 1",
        (source,),
    ).fetchone()[0]
    
    last_run = get_last_import_run(conn, source)
    
    return {
        "source": source,
        "active_count": active,
        "retired_count": retired,
        "total_count": active + retired,
        "last_run": last_run,
    }


__all__ = [
    # Types
    "Location",
    # Lookup API (Cython implementation)
    "lookup_pop_by_tokens",
    "lookup_pop_by_prefix",
    "lookup_pop_by_hostname",
    # Constants
    "SOURCE_PEERINGDB",
    "SOURCE_UNLOCODE",
    "SOURCE_SEED",
    "SOURCE_LEARNED",
    "SOURCE_MANUAL",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_LOW",
    "PRIORITY_CANONICAL",
    "PRIORITY_ALIAS",
    "PRIORITY_LEARNED",
    "MIN_PROCESSED_FOR_RETIRE",
    "MAX_RETIRE_PERCENT",
    # Lifecycle / DB API (Python)
    "is_pop_table_empty",
    "bootstrap_pop_locations",
    "ensure_pop_locations",
    "touch_pop_location",
    "record_candidate_alias",
    "promote_candidate_to_learned",
    "upsert_pop_location",
    "get_pop_location_count",
    "list_pop_locations",
    "start_import_run",
    "finish_import_run",
    "retire_stale_entries",
    "get_last_import_run",
    "get_import_run_stats",
    "count_active_pop_locations",
    "count_retired_pop_locations",
    "count_external_pop_locations",
    "recover_stale_import_runs",
    "get_source_stats",
]
