#!/usr/bin/env python3
#
# app/db/pop_locations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""POP Locations database layer.

This module re-exports the compiled Cython implementation.
Implementation is in app/_cython/pop_locations.pyx
"""

from app._cython.pop_locations import (
    # Types
    Location,
    # Lookup API (Cython implementation via _pl)
    lookup_pop_by_tokens,
    lookup_pop_by_prefix,
    lookup_pop_by_hostname,
    # Constants
    SOURCE_PEERINGDB,
    SOURCE_UNLOCODE,
    SOURCE_SEED,
    SOURCE_LEARNED,
    SOURCE_MANUAL,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    PRIORITY_CANONICAL,
    PRIORITY_ALIAS,
    PRIORITY_LEARNED,
    MIN_PROCESSED_FOR_RETIRE,
    MAX_RETIRE_PERCENT,
    # Lifecycle / DB API
    is_pop_table_empty,
    bootstrap_pop_locations,
    ensure_pop_locations,
    touch_pop_location,
    record_candidate_alias,
    promote_candidate_to_learned,
    upsert_pop_location,
    get_pop_location_count,
    list_pop_locations,
    start_import_run,
    finish_import_run,
    retire_stale_entries,
    get_last_import_run,
    get_import_run_stats,
    count_active_pop_locations,
    count_retired_pop_locations,
    count_external_pop_locations,
    recover_stale_import_runs,
    get_source_stats,
)

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
