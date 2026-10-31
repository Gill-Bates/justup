#!/usr/bin/env python3
#
# app/utils/storage_stats.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Storage statistics for the SQLite file and the time-series directory (JSONL and gzip segments)."""

from __future__ import annotations

import gzip
import os
import threading
import zlib
from contextlib import suppress
from pathlib import Path

_CHUNK = 1 << 20
_ACTIVE_SUFFIX = ".jsonl"
_SEGMENT_SUFFIX = ".jsonl.gz"

# Segments are immutable once published, so (path, size, mtime) identifies their content and the
# decompression needed to count points is paid once per segment, not on every refresh.
_segment_cache: dict[tuple[str, int, int], tuple[int, int]] = {}
_cache_lock = threading.Lock()


def _count_stream(opener, path: Path) -> tuple[int, int] | None:
    """Return ``(lines, uncompressed_bytes)`` read in fixed-size chunks, ``None`` for an unreadable file."""
    lines = nbytes = 0
    try:
        with opener(path, "rb") as f:
            while chunk := f.read(_CHUNK):
                nbytes += len(chunk)
                lines += chunk.count(b"\n")
    except (OSError, EOFError, zlib.error):
        return None
    return lines, nbytes


def compute_storage_stats(db_path: Path, tsdb_dir: Path) -> dict[str, int]:
    """Return DB size, TSDB size, point count and the size saved by compression.

    ``tsdb_size_bytes`` is the size of all series files (active files and segments).
    ``tsdb_saved_bytes`` is the decompressed size of all segments minus their stored size,
    measured, not estimated. Blocking I/O, run in a threadpool.
    """
    sql_size_bytes = 0
    # In WAL mode recent writes live in the -wal file until checkpointed; -shm is the shared index.
    for suffix in ("", "-wal", "-shm"):
        with suppress(OSError):
            sql_size_bytes += Path(f"{db_path}{suffix}").stat().st_size
    tsdb_size_bytes = 0
    total_points = 0
    segment_raw_bytes = 0
    segment_stored_bytes = 0
    seen: dict[tuple[str, int, int], tuple[int, int]] = {}

    if tsdb_dir.exists():
        for root, _dirs, files in os.walk(tsdb_dir):
            for name in files:
                path = Path(root, name)
                if not name.endswith((_ACTIVE_SUFFIX, _SEGMENT_SUFFIX)):
                    continue  # locks, in-flight temp files and set-aside damaged segments are not series data
                try:
                    if path.is_symlink():
                        continue
                    st = path.stat()
                except OSError:
                    continue
                tsdb_size_bytes += st.st_size
                if name.endswith(_SEGMENT_SUFFIX):
                    key = (str(path), st.st_size, st.st_mtime_ns)
                    with _cache_lock:
                        counted = _segment_cache.get(key)
                    if counted is None:
                        counted = _count_stream(gzip.open, path)
                    if counted is not None:
                        seen[key] = counted
                        total_points += counted[0]
                        segment_raw_bytes += counted[1]
                        segment_stored_bytes += st.st_size
                elif name.endswith(_ACTIVE_SUFFIX):
                    counted = _count_stream(open, path)
                    if counted is not None:
                        total_points += counted[0]

    with _cache_lock:
        _segment_cache.clear()
        _segment_cache.update(seen)

    return {
        "sql_size_bytes": sql_size_bytes,
        "tsdb_size_bytes": tsdb_size_bytes,
        "total_points": total_points,
        "tsdb_saved_bytes": max(0, segment_raw_bytes - segment_stored_bytes),
    }
