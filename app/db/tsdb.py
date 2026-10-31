#!/usr/bin/env python3
#
# app/db/tsdb.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tiny JSONL-based time-series storage for monitoring metrics.

Designed for single-node deployments with proper file locking.

Layout per target and series: the newest data lives in the uncompressed, append-only
``<series>.jsonl``; closed days move into gzip period segments ``<series>.<period>.jsonl.gz``
(see ``tsdb_segments``). Every reader sees both transparently under one shared series lock, and
only the segments overlapping the requested time range are opened.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError as e:  # pragma: no cover - platform-specific guard
    raise ImportError("tsdb requires fcntl (Unix-only). Windows is not supported for file-based TSDB.") from e

import contextlib
import gzip
import itertools
import json
import logging
import math
import os
import random
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from ..utils.time import ensure_utc, parse_utc, utcnow
from . import tsdb_segments as seg

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
DEFAULT_RETENTION_DAYS = 365
PRUNE_INTERVAL_SECONDS = 300  # Only prune a series every 5 minutes max

# Rollup configuration: resolution -> (bucket_seconds, retention_days)
ROLLUP_CONFIG = {
    "raw": (None, 30),  # Raw data: 30 days
    "5m_median": (300, 180),  # 5-minute median: 180 days
    "1h_median": (3600, MAX_RETENTION_DAYS),  # Hourly rollups follow target retention, up to 10 years
}

# Metrics that are 0/1 (up/down) - use min() instead of median
BINARY_METRICS = {"ping_up", "http_up", "tcp_up"}
_TCP_UP_PORT_RE = re.compile(r"^tcp_up_([0-9]{1,5})$")


def is_binary_metric(base: str) -> bool:
    """True for 0/1 availability metrics, including per-port ``tcp_up_<1..65535>``."""
    if base in BINARY_METRICS:
        return True
    m = _TCP_UP_PORT_RE.match(base)
    return m is not None and 1 <= int(m.group(1)) <= 65535


# Metrics where time-bucket resampling would change semantic meaning.
# Example: http_status is categorical and usually consumed as a distribution, not a time-series line.
NON_RESAMPLE_METRICS = {"http_status"}

# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricPoint:
    """A single time-series data point."""

    ts: datetime
    value: Any


@dataclass
class _PruneState:
    """Tracks when each series was last pruned to avoid excessive I/O."""

    last_prune: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_prune_state = _PruneState()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _series_path(tsdb_dir: Path, target_id: int, metric: str) -> Path:
    """Build the path to a series JSONL file."""
    safe_metric = "".join(c for c in metric if c.isalnum() or c in ("_", "-"))
    return tsdb_dir / f"target_{target_id}" / f"{safe_metric}.jsonl"


def _lock_path(series_path: Path) -> Path:
    """Return the lock file path for a series."""
    return series_path.with_suffix(".lock")


class _FileLock:
    """
    Context manager for exclusive file locking using fcntl.

    Creates a lock file alongside the target file to coordinate access.
    Works across multiple processes (uvicorn workers).
    """

    def __init__(self, series_path: Path, *, exclusive: bool = True):
        self._lock_path = _lock_path(series_path)
        self._exclusive = exclusive
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        fcntl.flock(self._fd, mode)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        return False


def _try_prune(series_key: str) -> bool:
    """Check prune eligibility and update last-prune timestamp only on actual prune."""
    now = time.monotonic()
    with _prune_state.lock:
        last = _prune_state.last_prune.get(series_key, 0.0)
        if now - last < PRUNE_INTERVAL_SECONDS:
            return False
        if random.random() >= 0.1:  # noqa: S311 - prune sampling, not security-sensitive
            return False
        _prune_state.last_prune[series_key] = now
        return True


def _validate_retention(retention_days: int) -> int:
    """Clamp target retention to the range accepted by the target model."""
    return max(MIN_RETENTION_DAYS, min(int(retention_days), MAX_RETENTION_DAYS))


def _effective_retention_days(resolution: str, target_retention_days: int) -> int:
    """Apply the target's history limit to a resolution's configured retention cap."""
    if resolution not in ROLLUP_CONFIG:
        raise ValueError(f"Unknown TSDB resolution: {resolution}")
    return min(ROLLUP_CONFIG[resolution][1], _validate_retention(target_retention_days))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_tsdb(tsdb_dir: Path) -> None:
    """Initialize the TSDB directory structure."""
    tsdb_dir.mkdir(parents=True, exist_ok=True)


def purge_tsdb(tsdb_dir: Path) -> None:
    """
    Completely remove all TSDB data.

    Uses shutil.rmtree for robustness against parallel file creation.
    """
    if not tsdb_dir.exists():
        return
    try:
        shutil.rmtree(tsdb_dir)
    except OSError:
        # Fallback: manual cleanup if rmtree fails
        for root, dirs, files in os.walk(tsdb_dir, topdown=False):
            for f in files:
                with contextlib.suppress(OSError):
                    Path(root, f).unlink(missing_ok=True)
            for d in dirs:
                with contextlib.suppress(OSError):
                    Path(root, d).rmdir()
    # Recreate the base directory
    tsdb_dir.mkdir(parents=True, exist_ok=True)


def delete_target(tsdb_dir: Path, target_id: int) -> None:
    """
    Delete all time-series data for a specific target.

    Uses shutil.rmtree for robustness.
    """
    tdir = tsdb_dir / f"target_{target_id}"
    if not tdir.exists():
        return
    try:
        shutil.rmtree(tdir)
    except OSError:
        # Fallback: manual cleanup
        for f in tdir.glob("*"):
            with contextlib.suppress(OSError):
                f.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tdir.rmdir()


def _copy_exact(src: Any, dst: Any, size: int) -> None:
    """Copy the first ``size`` bytes: the state the snapshot noted, without lines appended since."""
    while size > 0 and (chunk := src.read(min(size, 1 << 20))):
        dst.write(chunk)
        size -= len(chunk)


def snapshot_tsdb(tsdb_dir: Path, dest: Path) -> int:
    """Copy every series (active file and all its segments) into ``dest``, one series at a time.

    The series lock is held only while the files are opened and the active file's size is noted,
    so the active file and its segments belong to the same state (a compaction in between could
    otherwise duplicate or drop a day) and appends are not delayed by the copy. Segments are
    immutable, and the active file is copied up to the noted size, so no copied file is ever torn.
    Different series can differ by the copy duration. Lock and temp files are skipped. Returns the
    number of files copied.
    """
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for tdir in sorted(tsdb_dir.glob("target_*")):
        if not tdir.is_dir():
            continue
        out_dir = dest / tdir.name
        for stem in sorted(_series_stems(tdir)):
            active = tdir / f"{stem}.jsonl"
            snap = _open_snapshot(active, binary=True)
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                for segment, handle in snap.segments:
                    with (out_dir / segment.path.name).open("wb") as out:
                        shutil.copyfileobj(handle, out, 1 << 20)
                    copied += 1
                if snap.active is not None:
                    with (out_dir / active.name).open("wb") as out:
                        _copy_exact(snap.active, out, snap.active_size)
                    copied += 1
            finally:
                snap.close()
    return copied


def append_point(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    value: Any,
    retention_days: int,
    at: datetime | None = None,
) -> None:
    """
    Append a data point to a time series.

    Thread-safe and process-safe via file locking.
    Pruning is rate-limited to avoid excessive I/O.

    Args:
            tsdb_dir: Base directory for TSDB storage.
            target_id: Numeric ID of the monitoring target.
            metric: Name of the metric (e.g., "ping_ms", "http_up").
            value: The metric value to store.
            retention_days: How many days to retain data. Minimum is 1.
            at: Optional timestamp; defaults to now (UTC).
    """
    retention_days = _validate_retention(retention_days)

    if at is None:
        at = utcnow()
    else:
        if at.tzinfo is None:
            raise ValueError("Naive timestamp is not allowed in TSDB")
        at = ensure_utc(at)

    p = _series_path(tsdb_dir, target_id, metric)
    series_key = f"{target_id}:{metric}"

    with _FileLock(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            line = json.dumps({"ts": at.isoformat(), "value": value}, ensure_ascii=False)
            f.write(line + "\n")

        # Rate-limited + probabilistic pruning to keep append O(1) in hot path
        if _try_prune(series_key):
            _prune_series_locked(p, retention_days)


def _series_stems(tdir: Path) -> set[str]:
    """Series that exist in a target directory, as active file, as segments, or both."""
    return {p.stem for p in tdir.glob("*.jsonl")} | seg.segment_stems(tdir)


def _series_exists(series_path: Path) -> bool:
    """True when the series has an active file or at least one segment."""
    return series_path.exists() or bool(seg.list_segments(series_path.parent, series_path.stem))


def _prune_series_locked(series_path: Path, retention_days: int) -> None:
    """
    Prune old data points from the active file of a series (streaming, bounded memory).

    MUST be called while holding the file lock for series_path. Segments are pruned by
    ``prune_series`` (housekeeping), never on the append path.
    """
    _prune_active_locked(series_path, utcnow() - timedelta(days=_validate_retention(retention_days)))


def _has_expired_head(series_path: Path, cutoff: datetime) -> bool:
    """O(1) pre-check for pruning: the file is chronological, so only its first valid line can be expired.

    A file whose first lines are all invalid is still rewritten so the garbage is dropped.
    """
    with series_path.open("r", encoding="utf-8") as f:
        for line in itertools.islice(f, _PROBE_LINES):
            ts = seg.line_ts(line)
            if ts is not None:
                return ts < cutoff
        return series_path.stat().st_size > 0


def _prune_active_locked(series_path: Path, cutoff: datetime) -> None:
    """Rewrite the active file without expired or invalid lines; no rewrite when nothing is dropped."""
    if not series_path.exists() or not _has_expired_head(series_path, cutoff):
        return
    tmp = series_path.with_suffix(series_path.suffix + ".tmp")
    kept = dropped = 0
    try:
        with series_path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
            for raw_line in src:
                line = raw_line.strip()
                ts = seg.line_ts(line)
                if ts is None or ts < cutoff:
                    dropped += 1
                    continue
                dst.write(line + "\n")
                kept += 1
            if dropped and kept:
                dst.flush()
                os.fsync(dst.fileno())
        if not dropped:
            tmp.unlink(missing_ok=True)
        elif kept:
            tmp.replace(series_path)
        else:
            tmp.unlink(missing_ok=True)
            series_path.unlink(missing_ok=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _prune_segments_locked(series_path: Path, cutoff: datetime) -> None:
    """Delete expired segments and trim the one segment that straddles the cutoff (streaming, bounded RAM)."""
    directory = series_path.parent
    for segment in seg.list_segments(directory, series_path.stem):
        if segment.end <= cutoff:
            segment.path.unlink(missing_ok=True)
            continue
        if segment.start >= cutoff:
            continue
        first = seg.first_ts(segment.path)
        if first is None or first >= cutoff:
            continue
        stats: dict[str, Any] = {"count": 0, "newest": None}

        def kept(path: Path = segment.path, stats: dict[str, Any] = stats) -> Iterator[str]:
            for line in seg.iter_lines(path):
                ts = seg.line_ts(line)
                if ts is not None and ts >= cutoff:
                    stats["count"] += 1
                    stats["newest"] = ts if stats["newest"] is None or ts > stats["newest"] else stats["newest"]
                    yield line

        try:
            tmp, _ = seg.build_segment(directory, f"{segment.stem}.{segment.key}", keep_from=None, lines=kept())
        except seg.SegmentCorruptError as e:
            _log.warning("TSDB segment %s is damaged and left untrimmed: %s", segment.path.name, e)
            continue  # never rewrite a damaged archive; it is read as far as it is intact
        try:
            if stats["count"] == 0:
                segment.path.unlink(missing_ok=True)
            elif seg.verify_segment(tmp, stats["count"], stats["newest"]):
                seg.publish(tmp, segment.path)
            else:
                raise OSError(f"verification of trimmed segment {segment.path.name} failed; original kept")
        finally:
            tmp.unlink(missing_ok=True)


def prune_series(tsdb_dir: Path, *, target_id: int, metric: str, retention_days: int) -> None:
    """
    Manually trigger pruning for a specific series (active file and segments).

    This function acquires the file lock and prunes regardless of the
    rate-limiting interval. Useful for housekeeping tasks.
    """
    retention_days = _validate_retention(retention_days)
    p = _series_path(tsdb_dir, target_id, metric)
    if not _series_exists(p):
        return

    cutoff = utcnow() - timedelta(days=retention_days)
    with _FileLock(p):
        _prune_segments_locked(p, cutoff)
        _prune_active_locked(p, cutoff)


@dataclass
class _Snapshot:
    """Open handles of a series taken under the shared lock; reading them needs no lock.

    A handle keeps the file it was opened on, so a compaction that replaces a segment or the active
    file afterwards cannot change what this reader sees (it sees one consistent state).
    """

    segments: list[tuple[seg.Segment, Any]] = field(default_factory=list)
    active: Any = None
    active_size: int = 0

    def close(self) -> None:
        for _, handle in self.segments:
            handle.close()
        if self.active is not None:
            self.active.close()


def _open_snapshot(
    series_path: Path,
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    binary: bool = False,
    with_active: bool = True,
) -> _Snapshot:
    """Open the segments overlapping ``[since, until]`` and the active file under the shared lock.

    The lock is held for the opens only, never while data is decompressed, so a long read cannot
    delay the appends of the monitoring loop.
    """
    snap = _Snapshot()
    if not _series_exists(series_path):
        return snap
    try:
        with _FileLock(series_path, exclusive=False):
            # Compression only moves days before the current UTC day, so a segment never holds newer data.
            closed_until = _hot_start(utcnow())
            for segment in seg.list_segments(series_path.parent, series_path.stem):
                if since is not None and min(segment.end, closed_until) <= since:
                    continue
                if until is not None and segment.start > until:
                    continue
                try:
                    snap.segments.append((segment, seg.open_segment(segment.path)))
                except seg.SegmentCorruptError as e:
                    _log.warning("Skipping unreadable TSDB segment %s: %s", segment.path.name, e)
            if with_active and series_path.exists():
                if binary:
                    snap.active = series_path.open("rb")
                else:
                    snap.active = series_path.open("r", encoding="utf-8", errors="replace")
                snap.active_size = os.fstat(snap.active.fileno()).st_size
    except BaseException:
        snap.close()
        raise
    return snap


def _iter_series_lines(
    series_path: Path, since: datetime | None = None, until: datetime | None = None
) -> Iterator[str]:
    """Yield raw lines of a series, oldest segment first, then the active file.

    Only segments overlapping ``[since, until]`` are opened, a damaged segment is logged and skipped
    from the point of damage on. The caller should close the generator (``contextlib.closing``).
    A torn last line of the active file (append in flight) is invalid JSON and skipped by the parser.
    """
    snap = _open_snapshot(series_path, since, until)
    try:
        for segment, handle in snap.segments:
            try:
                yield from seg.iter_lines_from(handle, segment.path.name)
            except seg.SegmentCorruptError as e:
                _log.warning("Skipping damaged TSDB segment %s: %s", segment.path.name, e)
        if snap.active is not None:
            yield from snap.active
    finally:
        snap.close()


def _parse_point(raw_line: str, since: datetime | None, until: datetime | None) -> MetricPoint | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        ts = parse_utc(obj["ts"])
        if ts is None:
            return None
        val = obj.get("value")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        _log.debug("Skipping invalid TSDB line: %s", e)
        return None
    if since and ts < since:
        return None
    if until and ts > until:
        return None
    return MetricPoint(ts=ts, value=val)


def _tail_lines(f: Any, limit: int) -> list[MetricPoint]:
    """Last ``limit`` raw lines of an open binary file, read backwards in blocks."""
    f.seek(0, os.SEEK_END)
    pos = f.tell()
    buffer = b""
    chunks: list[bytes] = []
    while pos > 0 and len(chunks) <= limit:
        step = min(8192, pos)
        pos -= step
        f.seek(pos)
        buffer = f.read(step) + buffer
        chunks = buffer.splitlines()
    lines = [c.decode("utf-8", errors="replace") for c in chunks[-limit:]]
    return [pt for pt in (_parse_point(line, None, None) for line in lines) if pt is not None]


def _tail_active(p: Path, limit: int) -> list[MetricPoint]:
    """``_tail_lines`` for a path; the caller holds the series lock if the file may be replaced."""
    if not p.exists():
        return []
    with p.open("rb") as f:
        return _tail_lines(f, limit)


_TAIL_CACHE_LINES = 2000  # newest lines remembered per segment
_TAIL_CACHE_SEGMENTS = 32
# Value: (newest lines, holds every readable line, segment is damaged).
_tail_cache: dict[tuple[str, int, int], tuple[tuple[str, ...], bool, bool]] = {}
_tail_cache_lock = threading.Lock()


def _segment_tail(segment: seg.Segment, handle: Any, need: int) -> list[str]:
    """Newest ``need`` lines of a segment. Segments never change once published, so the newest lines
    are cached per (path, size, mtime) and a dashboard polling right after midnight does not
    decompress a whole week of data per request."""
    st = os.fstat(handle.fileno())
    key = (str(segment.path), st.st_size, st.st_mtime_ns)
    with _tail_cache_lock:
        cached = _tail_cache.get(key)
    if cached is not None and (cached[1] or len(cached[0]) >= need):
        if cached[2]:
            _log.debug("TSDB segment %s is damaged, serving its cached readable tail", segment.path.name)
        return list(cached[0][-need:])
    keep = max(_TAIL_CACHE_LINES, need)
    window: deque[str] = deque(maxlen=keep)
    total = 0
    damaged = False
    try:
        for line in seg.iter_lines_from(handle, segment.path.name):
            window.append(line)
            total += 1
    except seg.SegmentCorruptError as e:
        # Cache the readable part too; a repair changes size or mtime and so misses the key.
        damaged = True
        _log.warning("TSDB segment %s is damaged, serving its readable tail: %s", segment.path.name, e)
    entry = (tuple(window)[-_TAIL_CACHE_LINES:], total <= _TAIL_CACHE_LINES, damaged)
    with _tail_cache_lock:
        if len(_tail_cache) >= _TAIL_CACHE_SEGMENTS:
            _tail_cache.pop(next(iter(_tail_cache)))
        _tail_cache[key] = entry
    return list(window)[-need:]


def _tail_segments(snap_segments: list[tuple[seg.Segment, Any]], need: int) -> list[MetricPoint]:
    """Newest ``need`` points held by segments, scanning from the newest segment backwards."""
    collected: list[MetricPoint] = []
    for segment, handle in reversed(snap_segments):
        try:
            lines = _segment_tail(segment, handle, need - len(collected))
        except seg.SegmentCorruptError as e:
            _log.warning("Skipping damaged TSDB segment %s: %s", segment.path.name, e)
            continue
        pts = [pt for pt in (_parse_point(line, None, None) for line in lines) if pt is not None]
        collected = pts + collected
        if len(collected) >= need:
            break
    return collected[-need:]


def query(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
    latest: bool = False,
) -> list[MetricPoint]:
    """
    Query data points from a time series (active file and segments, transparently).

    Args:
            tsdb_dir: Base directory for TSDB storage.
            target_id: Numeric ID of the monitoring target.
            metric: Name of the metric to query.
            since: Optional lower bound (inclusive) for timestamps.
            until: Optional upper bound (inclusive) for timestamps.
            limit: Maximum number of points to return.
            latest: If True, return the *latest* points (tail of file).
                    If False, return the *oldest* points (head of file).

    Returns:
            List of MetricPoint objects, sorted chronologically (oldest first).
    """
    p = _series_path(tsdb_dir, target_id, metric)
    if not _series_exists(p):
        return []

    since_u = ensure_utc(since) if since else None
    until_u = ensure_utc(until) if until else None

    if limit <= 0:
        return []

    # Fast path: latest tail without time filters (common dashboard use-case)
    if latest and since_u is None and until_u is None:
        snap = _open_snapshot(p, binary=True)
        try:
            points = _tail_lines(snap.active, limit) if snap.active is not None else []
            if len(points) < limit:
                points = _tail_segments(snap.segments, limit - len(points)) + points
        finally:
            snap.close()
        return points

    # General path: stream under the shared lock so memory stays bounded by `limit`.
    tail: deque[MetricPoint] = deque(maxlen=limit)
    head: list[MetricPoint] = []
    with contextlib.closing(_iter_series_lines(p, since_u, until_u)) as lines:
        for raw_line in lines:
            pt = _parse_point(raw_line, since_u, until_u)
            if pt is None:
                continue
            if latest:
                tail.append(pt)
            else:
                head.append(pt)
                if len(head) >= limit:
                    break
    return list(tail) if latest else head


def query_zero_points(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[MetricPoint]:
    """Newest ``limit`` points of a binary series whose value is 0 within the window (oldest first).

    Streams the whole window so an outage is found regardless of how many points follow it.
    """
    p = _series_path(tsdb_dir, target_id, metric)
    if limit <= 0 or not _series_exists(p):
        return []
    since_u = ensure_utc(since) if since else None
    until_u = ensure_utc(until) if until else None
    tail: deque[MetricPoint] = deque(maxlen=limit)
    with contextlib.closing(_iter_series_lines(p, since_u, until_u)) as lines:
        for raw_line in lines:
            pt = _parse_point(raw_line, since_u, until_u)
            if pt is not None and pt.value == 0:
                tail.append(pt)
    return list(tail)


def query_latest(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    count: int = 100,
) -> list[MetricPoint]:
    """
    Convenience function to query the latest N data points.

    Args:
            tsdb_dir: Base directory for TSDB storage.
            target_id: Numeric ID of the monitoring target.
            metric: Name of the metric to query.
            count: Number of latest points to retrieve.

    Returns:
            List of MetricPoint objects, sorted chronologically (oldest first).
    """
    return query(tsdb_dir, target_id=target_id, metric=metric, limit=count, latest=True)


def list_metrics(tsdb_dir: Path, target_id: int) -> list[str]:
    """
    List all available metrics for a target.

    Returns:
            List of metric names (without file extension).
    """
    tdir = tsdb_dir / f"target_{target_id}"
    if not tdir.exists():
        return []

    metrics = []
    for stem in _series_stems(tdir):
        metric = stem
        for resolution in ("5m_median", "1h_median"):
            if stem.endswith(resolution):
                base = stem[: -len(resolution)]
                if base:
                    metric = f"{base}@{resolution}"
                break
        metrics.append(metric)
    return sorted(metrics)


# ---------------------------------------------------------------------------
# Rollup / Downsampling Functions
# ---------------------------------------------------------------------------


def _compute_median(values: list[float]) -> float:
    """Compute median of a list of numeric values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    return sorted_vals[mid]


def _resample_points(
    points: list[MetricPoint],
    *,
    metric: str,
    since: datetime,
    until: datetime,
    max_points: int,
) -> list[MetricPoint]:
    """Deterministically resample points into <= max_points time buckets.

    This is *visual* resampling meant for charts (PDF/UI): it guarantees a stable
    point density across large ranges. It does not attempt to preserve every peak.

    - Binary metrics use min() so any downtime in a bucket is preserved.
    - Other numeric metrics use median() (robust against spikes).
    """
    if max_points <= 0:
        return points
    if len(points) <= max_points:
        return points

    base = _base_metric(metric)
    if base in NON_RESAMPLE_METRICS:
        # Deterministic even sampling across the full range (keeps distributions unbiased)
        step = len(points) / max_points
        out = [points[int(i * step)] for i in range(max_points)]
        # Enforce endpoints to preserve time range boundaries
        out[0] = points[0]
        out[-1] = points[-1]
        out.sort(key=lambda p: p.ts)
        return out

    range_seconds = max(1.0, (ensure_utc(until) - ensure_utc(since)).total_seconds())
    bucket_seconds = max(1, math.ceil(range_seconds / max_points))

    # Bucket numeric values
    buckets: dict[datetime, list[float]] = {}
    for p in points:
        try:
            val = float(p.value)
        except (TypeError, ValueError):
            continue
        b = _bucket_key(ensure_utc(p.ts), bucket_seconds)
        buckets.setdefault(b, []).append(val)

    if not buckets:
        # Fall back: keep endpoints and evenly subsample
        if len(points) == 1:
            return points
        out = [points[0]]
        step = max(1, math.ceil(len(points) / max_points))
        if len(points) > 2:
            out.extend(points[step:-1:step])
        out.append(points[-1])
        out.sort(key=lambda p: p.ts)
        return out[:max_points]

    is_binary = is_binary_metric(base)
    resampled: list[MetricPoint] = []
    for bucket_start in sorted(buckets.keys()):
        vals = buckets[bucket_start]
        if not vals:
            continue
        agg_value = min(vals) if is_binary else _compute_median(vals)
        resampled.append(MetricPoint(ts=bucket_start, value=agg_value))

    # Ensure we keep the first/last timestamps for stable charts
    if resampled:
        first_ts = ensure_utc(points[0].ts)
        last_ts = ensure_utc(points[-1].ts)
        if resampled[0].ts != _bucket_key(first_ts, bucket_seconds):
            with contextlib.suppress(TypeError, ValueError):
                resampled.insert(0, MetricPoint(ts=first_ts, value=float(points[0].value)))
        if resampled[-1].ts != _bucket_key(last_ts, bucket_seconds):
            with contextlib.suppress(TypeError, ValueError):
                resampled.append(MetricPoint(ts=last_ts, value=float(points[-1].value)))

    resampled.sort(key=lambda p: p.ts)
    if len(resampled) > max_points:
        # Deterministic hard-cap: evenly pick across the resampled set
        step = len(resampled) / max_points
        resampled = [resampled[int(i * step)] for i in range(max_points)]

    return resampled


def _bucket_key(ts: datetime, bucket_seconds: int) -> datetime:
    """Return the start of the bucket for a given timestamp."""
    epoch = int(ensure_utc(ts).timestamp())
    bucket_start = (epoch // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(bucket_start, tz=UTC)


def _rollup_suffix(resolution: str) -> str:
    """Return the file suffix for a rollup resolution."""
    return f"@{resolution}"


def _is_rollup_metric(metric: str) -> bool:
    """Check if a metric name is a rollup (contains @)."""
    return "@" in metric


def _base_metric(metric: str) -> str:
    """Extract the base metric name from a rollup metric."""
    if "@" in metric:
        return metric.split("@", maxsplit=1)[0]
    return metric


def _last_ts(series_path: Path) -> datetime | None:
    """Newest timestamp of a series, read under a shared lock (the watermark of what was rolled up)."""
    with _FileLock(series_path, exclusive=False):
        return _last_ts_locked(series_path)


def downsample_series(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    source_resolution: str,
    target_resolution: str,
    since: datetime,
    until: datetime,
) -> int:
    """
    Downsample a metric from one resolution to another.

    Only *complete* target buckets inside ``[since, until)`` are written, so a bucket is never built
    from a partial set of samples. Buckets at or before the newest existing rollup point are skipped,
    which makes a repeated or overlapping run idempotent without loading all existing timestamps.

    For binary metrics (ping_up, http_up, tcp_up), uses min() to detect
    any downtime within the bucket. For other metrics, uses median().

    Args:
            tsdb_dir: Base directory for TSDB storage.
            target_id: Numeric ID of the monitoring target.
            metric: Base metric name (e.g., "ping_ms").
            source_resolution: Source resolution ("raw", "5m_median").
            target_resolution: Target resolution ("5m_median", "1h_median").
            since: Start of the time range to process.
            until: End of the time range to process (exclusive bucket end).

    Returns:
            Number of rollup points created.
    """
    if target_resolution not in ROLLUP_CONFIG:
        return 0

    bucket_seconds, _ = ROLLUP_CONFIG[target_resolution]
    if bucket_seconds is None:
        return 0  # Can't downsample to raw

    source_metric = metric if source_resolution == "raw" else f"{metric}{_rollup_suffix(source_resolution)}"
    target_path = _series_path(tsdb_dir, target_id, f"{metric}{_rollup_suffix(target_resolution)}")
    source_path = _series_path(tsdb_dir, target_id, source_metric)

    lo = _ceil_to_bucket(ensure_utc(since), bucket_seconds)
    hi = _bucket_key(ensure_utc(until), bucket_seconds)  # exclusive; only buckets ending by here
    watermark = _last_ts(target_path) if _series_exists(target_path) else None
    if watermark is not None:
        lo = max(lo, _bucket_key(watermark, bucket_seconds) + timedelta(seconds=bucket_seconds))
    if lo >= hi:
        return 0

    buckets: dict[datetime, list[float]] = {}
    with contextlib.closing(_iter_series_lines(source_path, lo, hi)) as lines:
        for raw_line in lines:
            p = _parse_point(raw_line, lo, hi - timedelta(microseconds=1))
            if p is None or p.value is None:
                continue
            try:
                val = float(p.value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(val):
                buckets.setdefault(_bucket_key(p.ts, bucket_seconds), []).append(val)
    if not buckets:
        return 0

    is_binary = is_binary_metric(_base_metric(metric))
    with _FileLock(target_path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Re-check under the exclusive lock: another run may have written meanwhile.
        newest = _last_ts_locked(target_path)
        created = 0
        with target_path.open("a", encoding="utf-8") as f:
            for bucket_start, values in sorted(buckets.items()):
                if newest is not None and bucket_start <= newest:
                    continue
                # For up/down metrics: 0 if any check failed, 1 if all passed
                agg_value = min(values) if is_binary else _compute_median(values)
                f.write(json.dumps({"ts": bucket_start.isoformat(), "value": agg_value}, ensure_ascii=False) + "\n")
                created += 1
            f.flush()
            os.fsync(f.fileno())
    return created


def _last_ts_locked(series_path: Path) -> datetime | None:
    """Newest timestamp: tail of the active file, else the newest segment. Rollups are written in time order."""
    tail = _tail_active(series_path, 8)
    if tail:
        return max(pt.ts for pt in tail)  # max, not last: tolerates a few out-of-order lines
    for segment in reversed(seg.list_segments(series_path.parent, series_path.stem)):
        _, newest, _ = seg.segment_info(segment.path)
        if newest is not None:
            return newest
    return None


def _ceil_to_bucket(ts: datetime, bucket_seconds: int) -> datetime:
    start = _bucket_key(ts, bucket_seconds)
    return start if start == ts else start + timedelta(seconds=bucket_seconds)


def run_rollups(
    tsdb_dir: Path,
    target_id: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, int]:
    """
    Run all rollup operations for a target (always on; there is no switch).

    Creates 5-minute and hourly rollups at the target's retention boundaries.
    Raw data is capped at 30 days, 5-minute data at 180 days, and hourly data
    at the target retention (up to 3650 days). Every complete bucket older than
    the handoff point and newer than the last rollup is converted, so a missed
    housekeeping day is caught up on the next run instead of leaving a gap.
    Categorical metrics (``http_status``) are not rolled up: a median of status
    codes is not a status.

    Returns:
            Dict mapping rollup series names to number of points created.
    """
    now = utcnow()
    target_retention = _validate_retention(retention_days)
    raw_retention = _effective_retention_days("raw", target_retention)
    five_minute_retention = _effective_retention_days("5m_median", target_retention)
    results: dict[str, int] = {}
    epoch = datetime.fromtimestamp(0, tz=UTC)

    # Get all raw metrics (no @ in name)
    raw_metrics = [m for m in list_metrics(tsdb_dir, target_id) if not _is_rollup_metric(m)]

    for metric in raw_metrics:
        if _base_metric(metric) in NON_RESAMPLE_METRICS:
            continue
        # Convert raw samples just before their effective retention cutoff.
        count_5m = downsample_series(
            tsdb_dir,
            target_id=target_id,
            metric=metric,
            source_resolution="raw",
            target_resolution="5m_median",
            since=epoch,
            until=now - timedelta(days=max(0, raw_retention - 1)),
        )
        if count_5m > 0:
            results[f"{metric}@5m_median"] = count_5m

        # Convert 5-minute buckets just before their effective retention cutoff.
        count_1h = downsample_series(
            tsdb_dir,
            target_id=target_id,
            metric=metric,
            source_resolution="5m_median",
            target_resolution="1h_median",
            since=epoch,
            until=now - timedelta(days=max(0, five_minute_retention - 1)),
        )
        if count_1h > 0:
            results[f"{metric}@1h_median"] = count_1h

    return results


def prune_all_series(
    tsdb_dir: Path,
    target_id: int,
    target_retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """
    Prune all series for a target according to their retention policies.

    Raw metrics: min(30 days, target retention)
    5m rollups: min(180 days, target retention)
    1h rollups: target retention (up to 3650 days)
    """
    target_retention = _validate_retention(target_retention_days)
    for metric in list_metrics(tsdb_dir, target_id):
        if "@1h_median" in metric:
            resolution = "1h_median"
        elif "@5m_median" in metric:
            resolution = "5m_median"
        else:
            resolution = "raw"

        retention = _effective_retention_days(resolution, target_retention)
        try:
            prune_series(tsdb_dir, target_id=target_id, metric=metric, retention_days=retention)
        except (OSError, ValueError) as e:  # one bad series must not stop the pruning of the others
            _log.warning("TSDB pruning of %s (target %s) failed, will retry: %s", metric, target_id, e)


# ---------------------------------------------------------------------------
# Compaction: closed days move from the active JSONL into gzip period segments
# ---------------------------------------------------------------------------

COMPACT_BUDGET_SECONDS = 3.0  # per target and housekeeping run; the rest continues in the next run
_STALE_TEMP_SECONDS = 3600
_CORRUPT_KEEP_SECONDS = 30 * 86400
_PROBE_LINES = 64
_MAX_THROTTLE_SECONDS = 0.5


@dataclass
class CompactionResult:
    """Outcome of one compaction pass over a target."""

    complete: bool = True  # False: budget, stop request or a lost race left work for the next run
    series: int = 0  # series that moved data into segments
    lines: int = 0  # records moved
    errors: int = 0  # series that failed with an I/O error (the retry should back off)


@dataclass
class HousekeepingResult:
    """Outcome of one housekeeping run for a target."""

    rollups: dict[str, int] = field(default_factory=dict)
    compaction: CompactionResult = field(default_factory=CompactionResult)
    skipped: bool = False  # another run holds this target's housekeeping lock

    @property
    def complete(self) -> bool:
        return self.compaction.complete


@dataclass
class _Chunk:
    """Closed lines of one period, compressed into a temp file while the active file is scanned."""

    key: str
    tmp: Path
    out: Any
    gz: Any
    count: int = 0
    first: datetime | None = None
    last: datetime | None = None
    ordered: bool = True

    def add(self, line: str, ts: datetime) -> None:
        self.gz.write((line.rstrip("\n") + "\n").encode("utf-8"))
        self.count += 1
        if self.last is not None and ts <= self.last:
            self.ordered = False
        self.first = ts if self.first is None or ts < self.first else self.first
        self.last = ts if self.last is None or ts > self.last else self.last

    def close(self) -> None:
        if not self.gz.closed:
            self.gz.close()
        if not self.out.closed:
            self.out.flush()
            os.fsync(self.out.fileno())
            self.out.close()


@dataclass
class _Plan:
    """A fully built and verified replacement for one period segment."""

    dest: Path
    final: Path
    expected_sig: tuple[int, int] | None  # (size, mtime_ns) of the segment this plan was built against
    quarantine: bool = False
    aside_tmp: Path | None = None  # durable copy of the damaged segment, published under the lock


def _hot_start(now: datetime) -> datetime:
    """Start of the current UTC day: everything older is closed and may be compressed."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def _needs_compaction(active: Path, hot_start: datetime) -> bool:
    """O(1) skip check: the active file is chronological, so its first valid line decides."""
    try:
        with active.open("r", encoding="utf-8") as f:
            for line in itertools.islice(f, _PROBE_LINES):
                ts = seg.line_ts(line)
                if ts is not None:
                    return ts < hot_start
            return active.stat().st_size > 0  # only invalid lines in the probe window: scan once
    except FileNotFoundError:
        return False


def _open_chunk(directory: Path, stem: str, key: str) -> _Chunk:
    tmp, fd = seg.new_temp(directory, f"{stem}.{key}.part")
    out = os.fdopen(fd, "wb")
    gz = gzip.GzipFile(fileobj=out, mode="wb", compresslevel=seg.GZIP_LEVEL, mtime=0)
    return _Chunk(key=key, tmp=tmp, out=out, gz=gz)


def _scan_closed_prefix(
    active: Path,
    stem: str,
    resolution: str,
    hot_start: datetime,
    expected: os.stat_result,
    chunks: dict[str, _Chunk],
) -> int:
    """Compress the contiguous closed prefix of the active file into ``chunks``; returns consumed bytes.

    No lock is held: closed lines are immutable and appends only extend the file. The scan stops at
    the first line of the current day, at an incomplete trailing line, or at the size seen at the
    start. ``chunks`` is filled in place so the caller can delete the temp files on any failure.
    """
    consumed = 0
    try:
        with active.open("rb") as f:
            if os.fstat(f.fileno()).st_ino != expected.st_ino:
                raise _RacedError
            for raw in f:
                if consumed + len(raw) > expected.st_size or not raw.endswith(b"\n"):
                    break
                line = raw.decode("utf-8", errors="replace")
                ts = seg.line_ts(line)
                if ts is not None:
                    if ts >= hot_start:
                        break
                    key = seg.period_key(resolution, ts)
                    if key not in chunks:
                        chunks[key] = _open_chunk(active.parent, stem, key)
                    chunks[key].add(line, ts)
                consumed += len(raw)  # invalid lines are consumed (dropped), as pruning always did
    finally:
        for chunk in chunks.values():
            chunk.close()
    return consumed


def _plan_segment(directory: Path, stem: str, chunk: _Chunk) -> _Plan | None:
    """Build and verify the replacement segment for one period, outside any lock.

    The merge branch holds one period in memory (at most about a week of raw data) and treats the
    timestamp as the record key; it only runs after a crash, for late lines or for a damaged archive.
    """
    dest = seg.segment_path(directory, stem, chunk.key)
    sig = _signature(dest)
    aside_tmp: Path | None = None
    if sig is None:
        final, expected, newest = chunk.tmp, chunk.count, chunk.last
        quarantine = False
    else:
        count, newest_old, damaged = seg.segment_info(dest)
        quarantine = damaged
        aside_tmp = _prepare_aside(dest, directory, sig) if damaged else None
        newer = newest_old is None or (chunk.first is not None and chunk.first > newest_old)
        if not damaged and chunk.ordered and newer:
            # Common case: a new closed day. Copy the old bytes and append one member, no recompression.
            final, fd = seg.new_temp(directory, f"{stem}.{chunk.key}.cat")
            try:
                with os.fdopen(fd, "wb") as out:
                    for part in (dest, chunk.tmp):
                        with part.open("rb") as src:
                            shutil.copyfileobj(src, out, 1 << 20)
                    out.flush()
                    os.fsync(out.fileno())
            except BaseException:
                final.unlink(missing_ok=True)
                raise
            expected, newest = count + chunk.count, chunk.last
            chunk.tmp.unlink(missing_ok=True)
        else:
            # Rare: crash leftovers (duplicates), stragglers or a damaged archive. Merge by timestamp.
            old_lines, _ = seg.read_lines_safely(dest)
            merged: dict[datetime, str] = {}
            for line in itertools.chain(old_lines, seg.iter_lines(chunk.tmp)):
                ts = seg.line_ts(line)
                if ts is not None:
                    merged.setdefault(ts, line.rstrip("\n"))
            ordered = [merged[ts] for ts in sorted(merged)]
            final, _ = seg.build_segment(directory, f"{stem}.{chunk.key}.merge", keep_from=None, lines=ordered)
            expected, newest = len(ordered), max(merged) if merged else None
            chunk.tmp.unlink(missing_ok=True)
    if not seg.verify_segment(final, expected, newest):
        final.unlink(missing_ok=True)
        raise OSError(f"verification of segment {dest.name} failed; original data kept")
    return _Plan(dest=dest, final=final, expected_sig=sig, quarantine=quarantine, aside_tmp=aside_tmp)


class _RacedError(Exception):
    """The active file or a segment changed under a compaction; the attempt is dropped untouched."""


def _stage_remainder(active: Path, directory: Path, stem: str, consumed: int, scanned_size: int, ino: int) -> Path:
    """Copy the not yet closed bytes ``[consumed, scanned_size)`` into a durable temp file, outside the lock."""
    tmp, fd = seg.new_temp(directory, f"{stem}.active")
    try:
        with os.fdopen(fd, "wb") as out, active.open("rb") as src:
            if os.fstat(src.fileno()).st_ino != ino:
                raise _RacedError
            src.seek(consumed)
            remaining = scanned_size - consumed
            while remaining > 0 and (chunk := src.read(min(remaining, 1 << 20))):
                out.write(chunk)
                remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _prepare_aside(path: Path, directory: Path, sig: tuple[int, int] | None) -> Path:
    """Copy a damaged segment into a durable temp file, outside the series lock.
    The copy is valid only for the version in ``sig``; a segment that changed meanwhile drops the attempt.
    """
    tmp, fd = seg.new_temp(directory, f"{path.name}.aside")
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        if sig is None or _signature(path) != sig or tmp.stat().st_size != sig[0]:
            raise _RacedError
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _set_aside(plan: _Plan) -> None:
    """Publish the prepared copy of a damaged segment next to its replacement; readers ignore it.
    A copy, not a hard link: it gets its own modification time, which starts its 30 day keep period.
    The rename is made durable before the caller replaces the damaged segment.
    """
    if plan.aside_tmp is None:
        raise OSError(f"no prepared copy of damaged segment {plan.dest.name}")
    aside = plan.dest.with_name(f"{plan.dest.name}.corrupt-{int(time.time())}-{os.urandom(2).hex()}")
    plan.aside_tmp.replace(aside)
    seg.fsync_dir(plan.dest.parent)
    plan.aside_tmp = None
    _log.warning("TSDB segment %s was damaged; the readable part was kept and the original set aside", plan.dest.name)


def _compact_series(active: Path, hot_start: datetime) -> int:
    """Move the closed prefix of one active file into segments; returns the records moved.

    Returns 0 when there was nothing to do and -1 when the attempt lost a race and was dropped.
    The exclusive lock covers only checks, a copy of the bytes appended meanwhile and renames; the
    scan, compression, verification and the copy of the open day happen before it.
    """
    stem, directory = active.stem, active.parent
    resolution = seg.resolution_of_stem(stem)
    try:
        st = active.stat()
    except FileNotFoundError:
        return 0
    chunks: dict[str, _Chunk] = {}
    plans: list[_Plan] = []
    remainder: Path | None = None
    try:
        consumed = _scan_closed_prefix(active, stem, resolution, hot_start, st, chunks)
        if not consumed:
            return 0
        moved = sum(c.count for c in chunks.values())
        plans.extend(_plan_segment(directory, stem, chunks[key]) for key in sorted(chunks))
        remainder = _stage_remainder(active, directory, stem, consumed, st.st_size, st.st_ino)
        with _FileLock(active):
            now_st = active.stat() if active.exists() else None
            if (
                now_st is None
                or now_st.st_ino != st.st_ino
                or now_st.st_size < st.st_size
                or any(_signature(p.dest) != p.expected_sig for p in plans)
            ):
                _log.debug("TSDB compaction of %s raced with a writer; retrying later", active.name)
                raise _RacedError
            with active.open("rb") as src, remainder.open("ab") as out:  # lines appended since the scan
                src.seek(st.st_size)
                shutil.copyfileobj(src, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            for plan in plans:
                if plan.quarantine:
                    _set_aside(plan)
                plan.final.replace(plan.dest)
            seg.fsync_dir(directory)  # segments are durable before the active file drops their days
            remainder.replace(active)
            remainder = None
            seg.fsync_dir(directory)
        return moved
    except _RacedError:
        return -1
    finally:
        leftovers = (
            *(c.tmp for c in chunks.values()),
            *(p.final for p in plans),
            *(p.aside_tmp for p in plans),
            remainder,
        )
        for path in leftovers:
            if path is not None:
                path.unlink(missing_ok=True)  # published files are gone by now; leftovers are failed attempts


def compact_target(
    tsdb_dir: Path,
    target_id: int,
    *,
    budget_seconds: float = COMPACT_BUDGET_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    throttle: bool = True,
    now: datetime | None = None,
) -> CompactionResult:
    """Compress the closed days of every series of a target, one series at a time.

    Idempotent and resumable: the layout itself is the progress marker. A series whose active file
    starts at the current day costs one small read, so a repeated run is nearly free. The run stops when
    ``budget_seconds`` is spent or ``should_stop()`` is true and reports ``complete=False``;
    ``throttle`` adds a short pause after each series so I/O and CPU stay shared with monitoring.
    """
    result = CompactionResult()
    tdir = tsdb_dir / f"target_{target_id}"
    if not tdir.is_dir():
        return result
    hot_start = _hot_start(ensure_utc(now) if now else utcnow())
    deadline = time.monotonic() + budget_seconds
    for active in sorted(tdir.glob("*.jsonl")):
        if should_stop is not None and should_stop():
            result.complete = False
            break
        if not _needs_compaction(active, hot_start):
            continue
        if time.monotonic() >= deadline:
            result.complete = False
            break
        started = time.monotonic()
        try:
            moved = _compact_series(active, hot_start)
        except (OSError, ValueError) as e:
            _log.warning("TSDB compaction of %s failed (retry later): %s", active.name, e)
            result.complete = False
            result.errors += 1
            continue
        if moved < 0:
            result.complete = False
        elif moved:
            result.series += 1
            result.lines += moved
        if throttle:
            time.sleep(min(time.monotonic() - started, _MAX_THROTTLE_SECONDS))
    return result


@contextlib.contextmanager
def _housekeeping_lock(tdir: Path) -> Iterator[bool]:
    """Non-blocking per-target lock so two housekeeping runs (or workers) never overlap."""
    fd = os.open(tdir / ".housekeeping.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _remove_stale_temps(tdir: Path) -> None:
    """Delete temp files left by a crashed run and damaged segments set aside more than 30 days ago."""
    now = time.time()
    for pattern, max_age in (("*.tmp", _STALE_TEMP_SECONDS), ("*.corrupt-*", _CORRUPT_KEEP_SECONDS)):
        for path in tdir.glob(pattern):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < now - max_age:
                    path.unlink()


def housekeep_target(
    tsdb_dir: Path,
    target_id: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    *,
    budget_seconds: float = COMPACT_BUDGET_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    throttle: bool = True,
) -> HousekeepingResult:
    """Daily TSDB housekeeping for one target: rollups, retention pruning, then compaction.

    Rollups read before pruning removes their source; compaction runs last so it never compresses
    data that pruning is about to delete. Runs of the same target never overlap.
    """
    result = HousekeepingResult()
    tdir = tsdb_dir / f"target_{target_id}"
    if not tdir.is_dir():
        result.rollups = run_rollups(tsdb_dir, target_id, retention_days)
        prune_all_series(tsdb_dir, target_id, retention_days)
        return result
    try:
        with _housekeeping_lock(tdir) as acquired:
            if not acquired:
                result.skipped = True
                result.compaction.complete = False
                return result
            _remove_stale_temps(tdir)
            result.rollups = run_rollups(tsdb_dir, target_id, retention_days)
            prune_all_series(tsdb_dir, target_id, retention_days)
            result.compaction = compact_target(
                tsdb_dir, target_id, budget_seconds=budget_seconds, should_stop=should_stop, throttle=throttle
            )
    except FileNotFoundError:
        _log.debug("TSDB directory of target %s vanished during housekeeping (target deleted?)", target_id)
    return result


def query_auto_resolution(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    since: datetime,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[MetricPoint]:
    """
    Query metric data with automatic resolution selection.

    Selects the appropriate resolution based on the time range:
    - 0-30 days: raw data
    - 30-180 days: 5m_median rollups
    - >180 days: 1h_median rollups

    For queries spanning multiple resolutions, combines data from all
    relevant series.

    Args:
            tsdb_dir: Base directory for TSDB storage.
            target_id: Numeric ID of the monitoring target.
            metric: Base metric name (e.g., "ping_ms").
            since: Start of the query range.
            until: End of the query range (defaults to now).
            limit: Maximum number of points to return.

    Returns:
            List of MetricPoint objects, sorted chronologically.
    """
    now = utcnow()
    until = ensure_utc(until) if until else now
    since = ensure_utc(since)

    # Calculate boundaries
    raw_cutoff = now - timedelta(days=30)
    rollup_5m_cutoff = now - timedelta(days=180)

    all_points: list[MetricPoint] = []

    # Fetch enough points to cover the requested range before visual resampling.
    # query() applies the limit *during* retrieval, so using the caller's limit
    # directly can accidentally drop most of the range (e.g. 7 days of raw data).
    internal_limit = max(limit * 20, 50_000)
    internal_limit = min(internal_limit, 200_000)

    # Query 1h rollups if needed (>180 days ago)
    if since < rollup_5m_cutoff:
        query_until = min(until, rollup_5m_cutoff - timedelta(microseconds=1))
        rollup_metric = f"{metric}@1h_median"
        points = query(
            tsdb_dir,
            target_id=target_id,
            metric=rollup_metric,
            since=since,
            until=query_until,
            limit=internal_limit,
        )
        all_points.extend(points)

    # Query 5m rollups if needed (30-180 days ago)
    if since < raw_cutoff and until > rollup_5m_cutoff:
        query_since = max(since, rollup_5m_cutoff)
        query_until = min(until, raw_cutoff - timedelta(microseconds=1))
        rollup_metric = f"{metric}@5m_median"
        points = query(
            tsdb_dir,
            target_id=target_id,
            metric=rollup_metric,
            since=query_since,
            until=query_until,
            limit=internal_limit,
        )
        all_points.extend(points)

    # Query raw data if needed (<30 days ago)
    if until > raw_cutoff:
        query_since = max(since, raw_cutoff)
        points = query(
            tsdb_dir,
            target_id=target_id,
            metric=metric,
            since=query_since,
            until=until,
            limit=internal_limit,
        )
        all_points.extend(points)

    # De-duplicate boundary timestamps across mixed resolutions (prefer newer-resolution points)
    by_ts: dict[datetime, MetricPoint] = {}
    for point in all_points:
        by_ts[point.ts] = point

    # Sort by timestamp and limit
    all_points = sorted(by_ts.values(), key=lambda p: p.ts)
    if len(all_points) > limit:
        all_points = _resample_points(
            all_points,
            metric=metric,
            since=since,
            until=until,
            max_points=limit,
        )

    return all_points


def query_display_series(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    since: datetime,
    until: datetime,
    bucket_seconds: int,
) -> dict[str, Any]:
    """Stream stored points into bounded display buckets without changing stored series.

    Historical status medians cannot identify individual HTTP codes, so status
    distributions include raw checks only and expose their coverage explicitly.
    """
    since = ensure_utc(since)
    until = ensure_utc(until)
    now = utcnow()
    raw_cutoff = now - timedelta(days=30)
    rollup_cutoff = now - timedelta(days=180)
    sources = []
    if metric != "http_status":
        sources.extend(
            [
                (f"{metric}@1h_median", since, min(until, rollup_cutoff - timedelta(microseconds=1))),
                (f"{metric}@5m_median", max(since, rollup_cutoff), min(until, raw_cutoff - timedelta(microseconds=1))),
            ]
        )
    sources.append((metric, max(since, raw_cutoff), until))

    buckets: dict[int, list[float]] = {}
    bucket_rollup: set[int] = set()
    status_counts: dict[str, int] = {}
    for source, start, end in sources:
        if start > end:
            continue
        path = _series_path(tsdb_dir, target_id, source)
        if not _series_exists(path):
            continue
        with contextlib.closing(_iter_series_lines(path, start, end)) as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    ts = parse_utc(item["ts"])
                    value = float(item["value"])
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
                if ts is None or ts < start or ts > end or not math.isfinite(value):
                    continue
                if metric == "http_status":
                    code = int(value)
                    if value != code or (code != 0 and not 100 <= code <= 599):
                        continue
                    key = str(code) if code else "Error"
                    status_counts[key] = status_counts.get(key, 0) + 1
                index = int((ts - since).total_seconds()) // bucket_seconds
                buckets.setdefault(index, []).append(value)
                if source != metric:
                    bucket_rollup.add(index)

    points: list[dict[str, Any]] = []
    for index, values in sorted(buckets.items()):
        start = since + timedelta(seconds=index * bucket_seconds)
        end = min(until, start + timedelta(seconds=bucket_seconds))
        if metric == "http_status":
            value = max(values, key=lambda code: (6 if code == 0 else int(code) // 100, code))
        elif is_binary_metric(metric):
            value = min(values)
        else:
            value = _compute_median(values)
        code_counts = None
        if metric == "http_status":
            code_counts = {str(int(code)) if code else "Error": values.count(code) for code in set(values)}
        points.append(
            {
                "ts": start.isoformat(),
                "value": value,
                "min": None if index in bucket_rollup else min(values),
                "max": None if index in bucket_rollup else max(values),
                "count": len(values),
                "bucket_end": end.isoformat(),
                "rollup": index in bucket_rollup,
                "code_counts": code_counts,
            }
        )
    return {
        "points": points,
        "bucket_seconds": bucket_seconds,
        "status_counts": status_counts if metric == "http_status" else None,
        "raw_status_only": metric == "http_status" and since < raw_cutoff,
    }


@dataclass
class DowntimePeriod:
    """Represents a downtime period with start, end, and duration."""

    start: datetime
    end: datetime | None  # None if still ongoing
    duration_seconds: int | None  # None if still ongoing
    failed_services: list[str] = field(default_factory=list)  # e.g. ["HTTP", "Ping", "TCP"]


def query_downtime_periods(
    tsdb_dir: Path,
    *,
    target_id: int,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
    min_duration_seconds: int = 60,
) -> list[DowntimePeriod]:
    """
    Find downtime periods by analyzing metric transitions across all check types.

    A downtime period starts when any metric goes from 1 to 0,
    and ends when all metrics go back to 1 (or is ongoing).

    Only closed periods >= min_duration_seconds are counted
    to avoid false positives from transient packet loss or single check failures.

    Returns a list of DowntimePeriod (most recent first), each with a list
    of which services (HTTP, Ping, TCP) were down during that period.
    """
    # Metric name → display label mapping
    metric_labels = {
        "http_up": "HTTP",
        "ping_up": "Ping",
        "tcp_up": "TCP",
    }

    # Query all metrics and collect points with their metric type
    # Build a dict: timestamp → {metric: is_up}
    timeline: dict[datetime, dict[str, bool]] = {}

    for metric in ("http_up", "ping_up", "tcp_up"):
        points = query(
            tsdb_dir,
            target_id=target_id,
            metric=metric,
            since=since,
            until=until,
            limit=50000,  # Read all points in range
            latest=False,
        )
        for point in points:
            if point.ts not in timeline:
                timeline[point.ts] = {}
            is_up = point.value == 1 or point.value is True
            timeline[point.ts][metric] = is_up

    if not timeline:
        return []

    # Sort timestamps and process transitions
    sorted_times = sorted(timeline.keys())

    periods: list[DowntimePeriod] = []
    downtime_start: datetime | None = None
    current_failed: set[str] = set()

    # Track last known state for each metric
    # Pre-query: get state before 'since' to avoid assuming initial state is "up"
    last_state: dict[str, bool] = {}
    if since:
        for metric in ("http_up", "ping_up", "tcp_up"):
            pre_points = query(
                tsdb_dir,
                target_id=target_id,
                metric=metric,
                until=since,
                limit=1,
                latest=True,
            )
            if pre_points:
                is_up = pre_points[0].value == 1 or pre_points[0].value is True
                last_state[metric] = is_up

    for ts in sorted_times:
        states = timeline[ts]

        # Update last known states with new data
        last_state.update(states)

        # Determine which services are currently down
        failed_now = {m for m, is_up in last_state.items() if not is_up}

        if failed_now and downtime_start is None:
            # Start of downtime
            downtime_start = ts
            current_failed = failed_now.copy()
        elif failed_now and downtime_start is not None:
            # Still in downtime - track any additional failures
            current_failed.update(failed_now)
        elif not failed_now and downtime_start is not None:
            # End of downtime - check minimum duration
            duration = int((ts - downtime_start).total_seconds())
            if duration >= min_duration_seconds:
                # Convert metric names to display labels
                failed_labels = sorted([metric_labels[m] for m in current_failed])
                periods.append(
                    DowntimePeriod(
                        start=downtime_start,
                        end=ts,
                        duration_seconds=duration,
                        failed_services=failed_labels,
                    )
                )
            downtime_start = None
            current_failed = set()

    # If still in downtime at end of data
    # Always include ongoing downtime regardless of duration
    if downtime_start is not None:
        failed_labels = sorted([metric_labels[m] for m in current_failed])
        periods.append(
            DowntimePeriod(
                start=downtime_start,
                end=None,
                duration_seconds=None,
                failed_services=failed_labels,
            )
        )

    # Return most recent first, limited
    periods.reverse()
    return periods[:limit]


def _availability_transitions(
    tsdb_dir: Path,
    *,
    target_id: int,
    metric: str,
    since: datetime,
    until: datetime,
    check_interval_seconds: int = 60,
) -> list[dict[str, Any]]:
    """Keep every observed state transition and mark gaps without filling them."""
    now = utcnow()
    raw_cutoff = now - timedelta(days=30)
    rollup_cutoff = now - timedelta(days=180)
    sources = (
        (f"{metric}@1h_median", since, min(until, rollup_cutoff - timedelta(microseconds=1)), 3600),
        (f"{metric}@5m_median", max(since, rollup_cutoff), min(until, raw_cutoff - timedelta(microseconds=1)), 300),
        (metric, max(since, raw_cutoff), until, None),
    )
    result: list[dict[str, Any]] = []
    previous_ts: datetime | None = None
    previous_up: int | None = None
    interval: float | None = None
    for name, start, end, rollup_interval in sources:
        if start > end:
            continue
        path = _series_path(tsdb_dir, target_id, name)
        if not _series_exists(path):
            continue
        with contextlib.closing(_iter_series_lines(path, start, end)) as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    ts = parse_utc(item["ts"])
                    up = int(float(item["value"]))
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
                if ts is None or ts < start or ts > end or up not in (0, 1):
                    continue
                if previous_ts is not None:
                    delta = (ts - previous_ts).total_seconds()
                    expected = rollup_interval or interval or check_interval_seconds
                    if expected is not None and delta > expected * 3:
                        if previous_up is not None and result[-1]["ts"] != previous_ts.isoformat():
                            result.append({"ts": previous_ts.isoformat(), "up": previous_up})
                        if previous_up is not None:
                            result.append({"ts": (previous_ts + timedelta(microseconds=1)).isoformat(), "up": None})
                        previous_up = None
                    elif rollup_interval is None and delta > 0:
                        interval = min(interval, delta) if interval is not None else delta
                if rollup_interval and up == 0:
                    result.append({"ts": ts.isoformat(), "up": 0, "observed": True})
                    result.append({"ts": (ts + timedelta(microseconds=1)).isoformat(), "up": None})
                    previous_up = None
                else:
                    if previous_up != up:
                        result.append({"ts": ts.isoformat(), "up": up})
                    previous_up = up
                previous_ts = ts
    if previous_ts is not None and previous_up is not None and result[-1]["ts"] != previous_ts.isoformat():
        result.append({"ts": previous_ts.isoformat(), "up": previous_up})
    return result


def _limit_availability_points(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Cap a transition list at ``limit`` points without dropping any retained outage.

    Down and gap points are grouped with their neighbouring up points into episodes. The newest
    episodes are kept first, so older state flapping is dropped as a whole and the step line stays
    consistent. Only when the outage episodes alone exceed the cap are the oldest ones omitted.
    """
    budget = max(int(limit), 1)
    if len(points) <= budget:
        return points
    episodes: list[tuple[int, int]] = []
    for i, point in enumerate(points):
        if point.get("up") == 1:
            continue
        lo, hi = max(i - 1, 0), min(i + 1, len(points) - 1)
        if episodes and lo <= episodes[-1][1] + 1:
            episodes[-1] = (episodes[-1][0], hi)
        else:
            episodes.append((lo, hi))
    last = len(points) - 1
    keep: set[int] = set()
    if not episodes or episodes[-1][1] != last:
        keep.add(last)  # keep the most recent observation time
    for lo, hi in reversed(episodes):
        size = hi - lo + 1
        free = budget - len(keep)
        if free <= 0:
            break
        # An oversized episode keeps its newest points.
        keep.update(range(max(lo, hi - free + 1), hi + 1) if size > free else range(lo, hi + 1))
    return [points[i] for i in sorted(keep)]


def query_availability_series(
    tsdb_dir: Path,
    *,
    target_id: int,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
    enabled_services: set[str] | None = None,
    tcp_ports: list[int] | None = None,
    check_interval_seconds: int = 60,
) -> dict[str, list[dict]]:
    """
    Query availability time series per service (not aggregated).

    Returns a dict where keys are service labels and values are lists of points.
    Example:
    {
      "PING": [{"ts": "2026-02-08T10:00:00Z", "up": 1}, ...],
      "HTTP": [{"ts": "2026-02-08T10:00:00Z", "up": 1}, ...],
      "TCP:443": [{"ts": "2026-02-08T10:00:00Z", "up": 1}, ...]
    }

    This allows frontend to display one line per service instead of aggregated status.

    Args:
            limit: Maximum points per service; outage episodes are kept in preference to up-only history.
            enabled_services: Set of enabled service names (e.g., {"PING", "HTTP", "TCP"})
                              If None, all metrics are queried.
            tcp_ports: List of TCP ports to query individually (e.g., [80, 443, 8080]).
                       Only used when "TCP" is in enabled_services.
    """
    series: dict[str, list[dict]] = {}

    def add_series(label: str, metric: str) -> None:
        try:
            points = _availability_transitions(
                tsdb_dir,
                target_id=target_id,
                metric=metric,
                since=ensure_utc(since) if since else utcnow() - timedelta(days=30),
                until=ensure_utc(until) if until else utcnow(),
                check_interval_seconds=check_interval_seconds,
            )
            points = _limit_availability_points(points, limit)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            _log.debug("Skipping availability metric %s for target %s: %s", metric, target_id, e)
            return
        if points:
            series[label] = points

    for metric, label in [("ping_up", "PING"), ("http_up", "HTTP")]:
        if enabled_services is None or label in enabled_services:
            add_series(label, metric)

    if enabled_services is None or "TCP" in enabled_services:
        if tcp_ports:
            for port in tcp_ports:
                add_series(f"TCP:{port}", f"tcp_up_{port}")
        else:
            # No per-port list: fall back to the aggregated tcp_up series.
            add_series("TCP", "tcp_up")

    return series
