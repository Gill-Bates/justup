#!/usr/bin/env python3
#
# app/db/pop_locations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
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
from typing import Any

from app.db.pop_lookup import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, lkp_h, lkp_p, lkp_t, normalize_pop_code
from app.utils.time import utcnow

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


# Shared normalizer (defined in pop_lookup to avoid a circular import).
_normalize_pop_code = normalize_pop_code


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
# Sources and confidence levels
# ---------------------------------------------------------------------------

# Source identifiers (where did this entry come from?)
SOURCE_PEERINGDB = "peeringdb"  # Imported from PeeringDB API
SOURCE_UNLOCODE = "unlocode"  # Imported from UN/LOCODE database
SOURCE_SEED = "seed"  # Built-in seed data (stable, curated)
SOURCE_MANUAL = "manual"  # Manually added via admin API

# Confidence levels (how certain are we about this mapping?)
# - HIGH (100): Authoritative source (manual, PeeringDB verified, seed)
# - MEDIUM (70): Inferred from patterns, unverified but plausible
# - LOW (40): Heuristic match, may need review
CONFIDENCE_LOW = 40  # HIGH/MEDIUM are imported from app.db.pop_lookup

# Priority levels (lookup order - lower wins!)
# - CANONICAL (0): Primary code for this location (e.g., "fra" = Frankfurt)
# - ALIAS (10): Known alternative (e.g., "ffm" = Frankfurt)
#
# Priority is for ORDERING within same alias_code matches.
# Lower priority = preferred match.
PRIORITY_CANONICAL = 0
PRIORITY_ALIAS = 10

# ---------------------------------------------------------------------------
# Safety thresholds for retire logic
# ---------------------------------------------------------------------------

# Minimum records to process before allowing retire (prevents empty-run disasters)
MIN_PROCESSED_FOR_RETIRE = 10

# Maximum percentage of active entries that can be retired in one run
# Protects against shrinking dataset attacks (e.g., API returns only 5% of data)
MAX_RETIRE_PERCENT = 30.0


def _with_row_factory(fn):
    """Decorator to ensure row_factory is set for dict-like row access."""

    def wrapper(conn, *args, **kwargs):
        _ensure_row_factory(conn)
        return fn(conn, *args, **kwargs)

    return wrapper


_SEED_DATA: list[tuple[str, str, str, str, float, float]] = [
    # Columns: canonical_code, alias_code, city, country, lat, lon
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
    for canonical, alias, _city, _country, _lat, _lon in _SEED_DATA:
        if alias in seen_aliases:
            raise ValueError(f"Duplicate alias '{alias}': used by both '{seen_aliases[alias]}' and '{canonical}'")
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
                    (canonical, alias, city, country, lat, lon, SOURCE_SEED, CONFIDENCE_HIGH, priority, now, now),
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

    Note: Schema is defined in sqlite.py (single source of truth).
    """
    # Bootstrap seed data if table is empty
    if is_pop_table_empty(conn):
        _log.info("POP locations table empty, bootstrapping...")
        bootstrap_pop_locations(conn)


# ---------------------------------------------------------------------------
# Lookup functions
# ---------------------------------------------------------------------------

lookup_pop_by_tokens = lkp_t
lookup_pop_by_prefix = lkp_p
lookup_pop_by_hostname = lkp_h


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
    import_run_id: str | None = None,
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
            (
                canonical_code,
                alias_code,
                city,
                country,
                lat,
                lon,
                source,
                confidence,
                priority,
                now,
                now,
                now,
                import_run_id,
            ),
        )
        return True
    except sqlite3.Error as e:
        _log.debug("Failed to upsert POP location %s/%s: %s", canonical_code, alias_code, e)
        return False


def get_pop_location_count(conn: sqlite3.Connection) -> int:
    """Get total number of POP locations."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations").fetchone()
    return row[0]


# Whitelist: sort_by is interpolated into SQL, so only these literals may pass.
POP_LOCATION_SORT_COLUMNS = ("canonical_code", "city", "country")


@_with_row_factory
def list_pop_locations(
    conn: sqlite3.Connection,
    source: str | None = None,
    include_retired: bool = False,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "canonical_code",
) -> list[dict]:
    """List POP locations with optional filtering and whitelisted sorting."""
    if sort_by not in POP_LOCATION_SORT_COLUMNS:
        raise ValueError(f"Unsupported sort column: {sort_by!r}")
    # retired_clause is internally controlled (safe for f-string)
    retired_clause = "" if include_retired else "AND retired = 0"

    if source:
        rows = conn.execute(
            f"""
            SELECT * FROM pop_locations
            WHERE source = ? {retired_clause}
            ORDER BY {sort_by}, canonical_code, priority
            LIMIT ? OFFSET ?
            """,  # noqa: S608 - clauses code-controlled/whitelisted, values bound as params
            (source, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT * FROM pop_locations
            WHERE 1=1 {retired_clause}
            ORDER BY {sort_by}, canonical_code, priority
            LIMIT ? OFFSET ?
            """,  # noqa: S608 - clauses code-controlled/whitelisted, values bound as params
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
    now_str = now.isoformat().replace(":", "-")
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
    error_message: str | None = None,
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
        (now, status, records_processed, records_inserted, records_updated, records_retired, error_message, run_id),
    )
    conn.commit()

    _log.info(
        "Finished import run %s: processed=%d, inserted=%d, updated=%d, retired=%d, status=%s",
        run_id,
        records_processed,
        records_inserted,
        records_updated,
        records_retired,
        status,
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
            "Skipping retire for %s: processed=%d < min=%d", source, records_processed, MIN_PROCESSED_FOR_RETIRE
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
            source,
            retire_percent,
            would_retire,
            active_count,
            MAX_RETIRE_PERCENT,
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
            retired_count,
            source,
            retire_percent,
            active_count,
        )

    return retired_count


@_with_row_factory
def get_last_import_run(
    conn: sqlite3.Connection,
    source: str | None = None,
) -> dict | None:
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
def get_last_import_attempt(conn: sqlite3.Connection, source: str) -> dict | None:
    """Return the most recent terminal import run for a source (completed or failed).

    Unlike ``get_last_import_run`` this does not filter on status, so a run that ended in failure
    is visible to the scheduler. Still-running rows are excluded: a crashed run is recovered to
    ``failed`` at startup, so a leftover ``running`` row only ever means a genuinely live import.
    """
    row = conn.execute(
        """
        SELECT * FROM pop_import_runs
        WHERE source = ? AND status IN ('completed', 'failed')
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    return dict(row) if row else None


def count_consecutive_failures(conn: sqlite3.Connection, source: str) -> int:
    """Count failed terminal runs since the last successful one (0 right after a success).

    Drives the backoff: the most recent terminal runs are inspected newest-first and counted until
    the first ``completed`` run, so a single success resets the streak.
    """
    rows = conn.execute(
        """
        SELECT status FROM pop_import_runs
        WHERE source = ? AND status IN ('completed', 'failed')
        ORDER BY started_at DESC
        """,
        (source,),
    ).fetchall()
    failures = 0
    for row in rows:
        if row[0] == "completed":
            break
        failures += 1
    return failures


@_with_row_factory
def get_import_run_stats(conn: sqlite3.Connection, limit: int = 10, offset: int = 0) -> list[dict]:
    """Get recent import run statistics."""
    rows = conn.execute(
        """
        SELECT * FROM pop_import_runs
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    return [dict(row) for row in rows]


def count_active_pop_locations(conn: sqlite3.Connection) -> int:
    """Get count of non-retired POP locations."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 0").fetchone()
    return row[0]


def count_retired_pop_locations(conn: sqlite3.Connection) -> int:
    """Get count of retired POP locations."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 1").fetchone()
    return row[0]


def count_external_pop_locations(conn: sqlite3.Connection) -> int:
    """Count POP locations from external sources (not seed/manual)."""
    row = conn.execute("SELECT COUNT(*) FROM pop_locations WHERE source NOT IN ('seed', 'manual')").fetchone()
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


__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "MAX_RETIRE_PERCENT",
    "MIN_PROCESSED_FOR_RETIRE",
    "PRIORITY_ALIAS",
    "PRIORITY_CANONICAL",
    "SOURCE_MANUAL",
    "SOURCE_PEERINGDB",
    "SOURCE_SEED",
    "SOURCE_UNLOCODE",
    "Location",
    "bootstrap_pop_locations",
    "count_active_pop_locations",
    "count_consecutive_failures",
    "count_external_pop_locations",
    "count_retired_pop_locations",
    "ensure_pop_locations",
    "finish_import_run",
    "get_import_run_stats",
    "get_last_import_attempt",
    "get_last_import_run",
    "get_pop_location_count",
    "is_pop_table_empty",
    "list_pop_locations",
    "lookup_pop_by_hostname",
    "lookup_pop_by_prefix",
    "lookup_pop_by_tokens",
    "recover_stale_import_runs",
    "retire_stale_entries",
    "start_import_run",
    "upsert_pop_location",
]
