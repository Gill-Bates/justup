#!/usr/bin/env python3
#
# app/db/tsdb.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tiny JSONL-based time-series storage for monitoring metrics.

Designed for single-node deployments with proper file locking.
"""

from __future__ import annotations

try:
	import fcntl
except ImportError as e:  # pragma: no cover - platform-specific guard
	raise ImportError(
		"tsdb requires fcntl (Unix-only). Windows is not supported for file-based TSDB."
	) from e

import json
import logging
import math
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..utils.time import ensure_utc, parse_utc, utcnow

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RETENTION_DAYS = 1
DEFAULT_RETENTION_DAYS = 365
PRUNE_INTERVAL_SECONDS = 300  # Only prune a series every 5 minutes max

# Rollup configuration: resolution -> (bucket_seconds, retention_days)
ROLLUP_CONFIG = {
	"raw": (None, 30),          # Raw data: 30 days
	"5m_median": (300, 180),    # 5-minute median: 180 days
	"1h_median": (3600, 365),   # 1-hour median: 365 days
}

# Metrics that are 0/1 (up/down) - use min() instead of median
BINARY_METRICS = {"ping_up", "http_up", "tcp_up"}

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
		self._fd: Optional[int] = None

	def __enter__(self) -> "_FileLock":
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
		if random.random() >= 0.1:
			return False
		_prune_state.last_prune[series_key] = now
		return True


def _validate_retention(retention_days: int) -> int:
	"""Ensure retention_days is within acceptable bounds."""
	if retention_days < MIN_RETENTION_DAYS:
		return MIN_RETENTION_DAYS
	return retention_days


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
				try:
					Path(root, f).unlink(missing_ok=True)
				except OSError:
					pass
			for d in dirs:
				try:
					Path(root, d).rmdir()
				except OSError:
					pass
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
			try:
				f.unlink(missing_ok=True)
			except OSError:
				pass
		try:
			tdir.rmdir()
		except OSError:
			pass


def append_point(
	tsdb_dir: Path,
	*,
	target_id: int,
	metric: str,
	value: Any,
	retention_days: int,
	at: Optional[datetime] = None,
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
		# Enforce timezone awareness (Task 6.2)
		if at.tzinfo is None:
			raise ValueError("Naive timestamp is not allowed in TSDB")
		at = ensure_utc(at)

	if at.tzinfo is None or at.utcoffset() is None or at.utcoffset().total_seconds() != 0:
		raise ValueError(f"Timestamp must be timezone-aware UTC, got {at!r}")

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


def _prune_series_locked(series_path: Path, retention_days: int) -> None:
	"""
	Prune old data points from a series file.

	MUST be called while holding the file lock for series_path.
	"""
	if not series_path.exists():
		return

	retention_days = _validate_retention(retention_days)
	cutoff = utcnow() - timedelta(days=retention_days)
	kept: list[str] = []

	with series_path.open("r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
				# Defensive parsing (Task 6.4)
				ts = parse_utc(obj["ts"])
				if ts is None:
					continue
			except (json.JSONDecodeError, KeyError, TypeError, ValueError):
				continue
			if ts >= cutoff:
				kept.append(line)

	# Rewrite atomically via temp file + replace
	if len(kept) == 0:
		series_path.unlink(missing_ok=True)
		return

	tmp = series_path.with_suffix(series_path.suffix + ".tmp")
	with tmp.open("w", encoding="utf-8") as f:
		for line in kept:
			f.write(line + "\n")
	tmp.replace(series_path)


def prune_series(
	tsdb_dir: Path, *, target_id: int, metric: str, retention_days: int
) -> None:
	"""
	Manually trigger pruning for a specific series.

	This function acquires the file lock and prunes regardless of the
	rate-limiting interval. Useful for housekeeping tasks.
	"""
	retention_days = _validate_retention(retention_days)
	p = _series_path(tsdb_dir, target_id, metric)
	if not p.exists():
		return

	with _FileLock(p):
		_prune_series_locked(p, retention_days)


def query(
	tsdb_dir: Path,
	*,
	target_id: int,
	metric: str,
	since: Optional[datetime] = None,
	until: Optional[datetime] = None,
	limit: int = 1000,
	latest: bool = False,
) -> list[MetricPoint]:
	"""
	Query data points from a time series.

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
	if not p.exists():
		return []

	since_u = ensure_utc(since) if since else None
	until_u = ensure_utc(until) if until else None

	def _parse_lines(lines: list[str], stop_early: bool) -> list[MetricPoint]:
		matched: list[MetricPoint] = []
		for line in lines:
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
				ts = parse_utc(obj["ts"])
				if ts is None:
					continue
				val = obj.get("value")
			except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
				_log.debug("Skipping invalid TSDB line in %s: %s", p, e)
				continue

			if since_u and ts < since_u:
				continue
			if until_u and ts > until_u:
				continue

			matched.append(MetricPoint(ts=ts, value=val))
			if stop_early and len(matched) >= limit:
				break
		return matched

	# Fast path: latest tail without time filters (common dashboard use-case)
	if latest and since_u is None and until_u is None:
		with _FileLock(p, exclusive=False):
			with p.open("rb") as f:
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
		return _parse_lines(lines, stop_early=False)

	# General path: copy raw lines under shared lock, parse outside lock
	with _FileLock(p, exclusive=False):
		with p.open("r", encoding="utf-8") as f:
			raw_lines = f.readlines()

	all_matching = _parse_lines(raw_lines, stop_early=not latest)

	if latest:
		# Return the last N points, but keep chronological order
		if len(all_matching) > limit:
			all_matching = all_matching[-limit:]
	else:
		# Return the first N points
		if len(all_matching) > limit:
			all_matching = all_matching[:limit]

	return all_matching


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

	metrics: list[str] = []
	for f in tdir.glob("*.jsonl"):
		metrics.append(f.stem)
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
	bucket_seconds = max(1, int(math.ceil(range_seconds / max_points)))

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
		step = max(1, int(math.ceil(len(points) / max_points)))
		if len(points) > 2:
			out.extend(points[step:-1:step])
		out.append(points[-1])
		out.sort(key=lambda p: p.ts)
		return out[:max_points]

	is_binary = base in BINARY_METRICS
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
			try:
				resampled.insert(0, MetricPoint(ts=first_ts, value=float(points[0].value)))
			except (TypeError, ValueError):
				pass
		if resampled[-1].ts != _bucket_key(last_ts, bucket_seconds):
			try:
				resampled.append(MetricPoint(ts=last_ts, value=float(points[-1].value)))
			except (TypeError, ValueError):
				pass

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
	return datetime.fromtimestamp(bucket_start, tz=timezone.utc)


def _rollup_suffix(resolution: str) -> str:
	"""Return the file suffix for a rollup resolution."""
	return f"@{resolution}"


def _is_rollup_metric(metric: str) -> bool:
	"""Check if a metric name is a rollup (contains @)."""
	return "@" in metric


def _base_metric(metric: str) -> str:
	"""Extract the base metric name from a rollup metric."""
	if "@" in metric:
		return metric.split("@")[0]
	return metric


def _read_existing_timestamps(series_path: Path) -> tuple[set[str], datetime | None]:
	"""Read existing timestamps and find the latest one in a single file scan.

	Returns:
		Tuple of (set of ISO timestamp strings, latest datetime or None)
	"""
	if not series_path.exists():
		return set(), None

	existing_ts: set[str] = set()
	last_ts: datetime | None = None

	with series_path.open("r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
				ts_str = obj["ts"]
				existing_ts.add(ts_str)
				last_ts = parse_utc(ts_str)
			except (json.JSONDecodeError, KeyError, TypeError, ValueError):
				continue

	return existing_ts, last_ts


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

	For binary metrics (ping_up, http_up, tcp_up), uses min() to detect
	any downtime within the bucket. For other metrics, uses median().

	Args:
		tsdb_dir: Base directory for TSDB storage.
		target_id: Numeric ID of the monitoring target.
		metric: Base metric name (e.g., "ping_ms").
		source_resolution: Source resolution ("raw", "5m_median").
		target_resolution: Target resolution ("5m_median", "1h_median").
		since: Start of the time range to process.
		until: End of the time range to process.

	Returns:
		Number of rollup points created.
	"""
	if target_resolution not in ROLLUP_CONFIG:
		return 0

	bucket_seconds, _ = ROLLUP_CONFIG[target_resolution]
	if bucket_seconds is None:
		return 0  # Can't downsample to raw

	# Determine source metric name
	if source_resolution == "raw":
		source_metric = metric
	else:
		source_metric = f"{metric}{_rollup_suffix(source_resolution)}"

	# Determine target metric name
	target_metric = f"{metric}{_rollup_suffix(target_resolution)}"
	target_path = _series_path(tsdb_dir, target_id, target_metric)

	# Query source data
	points = query(
		tsdb_dir,
		target_id=target_id,
		metric=source_metric,
		since=since,
		until=until,
		limit=100000,  # High limit for rollup processing
	)

	if not points:
		return 0

	# Bucket the points
	buckets: dict[datetime, list[float]] = {}
	for p in points:
		if p.value is None:
			continue
		try:
			val = float(p.value)
		except (TypeError, ValueError):
			continue
		bucket_start = _bucket_key(p.ts, bucket_seconds)
		if bucket_start not in buckets:
			buckets[bucket_start] = []
		buckets[bucket_start].append(val)

	if not buckets:
		return 0

	# Determine aggregation function
	base = _base_metric(metric)
	is_binary = base in BINARY_METRICS

	# Write rollup points
	_, retention_days = ROLLUP_CONFIG[target_resolution]

	with _FileLock(target_path):
		target_path.parent.mkdir(parents=True, exist_ok=True)
		existing_ts, _ = _read_existing_timestamps(target_path)

		# Append new rollup points (dedupe against current on-disk state)
		created = 0
		with target_path.open("a", encoding="utf-8") as f:
			for bucket_start, values in sorted(buckets.items()):
				ts_iso = bucket_start.isoformat()
				if ts_iso in existing_ts:
					continue

				if is_binary:
					# For up/down metrics: 0 if any check failed, 1 if all passed
					agg_value = min(values)
				else:
					agg_value = _compute_median(values)

				line = json.dumps({"ts": ts_iso, "value": agg_value}, ensure_ascii=False)
				f.write(line + "\n")
				created += 1

		# Prune old rollup data
		_prune_series_locked(target_path, retention_days)

	return created


def run_rollups(tsdb_dir: Path, target_id: int) -> dict[str, int]:
	"""
	Run all rollup operations for a target.

	Creates 5-minute and 1-hour rollups for all raw metrics.

	Returns:
		Dict mapping rollup series names to number of points created.
	"""
	now = utcnow()
	results: dict[str, int] = {}

	# Get all raw metrics (no @ in name)
	raw_metrics = [m for m in list_metrics(tsdb_dir, target_id) if not _is_rollup_metric(m)]

	for metric in raw_metrics:
		# Raw → 5m_median: process data from 30-180 days ago (with 1-day overlap)
		since_5m = now - timedelta(days=181)
		until_5m = now - timedelta(days=29)
		count_5m = downsample_series(
			tsdb_dir,
			target_id=target_id,
			metric=metric,
			source_resolution="raw",
			target_resolution="5m_median",
			since=since_5m,
			until=until_5m,
		)
		if count_5m > 0:
			results[f"{metric}@5m_median"] = count_5m

		# 5m_median → 1h_median: process data from 180-365 days ago (with 1-day overlap)
		since_1h = now - timedelta(days=366)
		until_1h = now - timedelta(days=179)
		count_1h = downsample_series(
			tsdb_dir,
			target_id=target_id,
			metric=metric,
			source_resolution="5m_median",
			target_resolution="1h_median",
			since=since_1h,
			until=until_1h,
		)
		if count_1h > 0:
			results[f"{metric}@1h_median"] = count_1h

	return results


def prune_all_series(tsdb_dir: Path, target_id: int, raw_retention_days: int | None = None) -> None:
	"""
	Prune all series for a target according to their retention policies.

	Raw metrics: target-specific retention (if provided), otherwise 30 days
	5m rollups: 180 days
	1h rollups: 365 days
	"""
	raw_retention = _validate_retention(raw_retention_days) if raw_retention_days is not None else ROLLUP_CONFIG["raw"][1]
	for metric in list_metrics(tsdb_dir, target_id):
		if "@1h_median" in metric:
			retention = ROLLUP_CONFIG["1h_median"][1]
		elif "@5m_median" in metric:
			retention = ROLLUP_CONFIG["5m_median"][1]
		else:
			retention = raw_retention

		prune_series(tsdb_dir, target_id=target_id, metric=metric, retention_days=retention)


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
	min_duration_seconds: int = 60,  # Task 2: Minimum downtime to count (avoid spikes)
) -> list[DowntimePeriod]:
	"""
	Find downtime periods by analyzing metric transitions across all check types.

	A downtime period starts when any metric goes from 1 to 0,
	and ends when all metrics go back to 1 (or is ongoing).
	
	Task 2 (Debouncing): Only periods >= min_duration_seconds are counted
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
				periods.append(DowntimePeriod(
					start=downtime_start,
					end=ts,
					duration_seconds=duration,
					failed_services=failed_labels,
				))
			downtime_start = None
			current_failed = set()

	# If still in downtime at end of data
	# Always include ongoing downtime regardless of duration
	if downtime_start is not None:
		failed_labels = sorted([metric_labels[m] for m in current_failed])
		periods.append(DowntimePeriod(
			start=downtime_start,
			end=None,
			duration_seconds=None,
			failed_services=failed_labels,
		))

	# Return most recent first, limited
	periods.reverse()
	return periods[:limit]


def query_availability_series(
	tsdb_dir: Path,
	*,
	target_id: int,
	since: Optional[datetime] = None,
	until: Optional[datetime] = None,
	limit: int = 1000,
	enabled_services: Optional[set[str]] = None,
	tcp_ports: Optional[list[int]] = None,
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
		enabled_services: Set of enabled service names (e.g., {"PING", "HTTP", "TCP"})
		                  If None, all metrics are queried (legacy behavior).
		tcp_ports: List of TCP ports to query individually (e.g., [80, 443, 8080]).
		           Only used when "TCP" is in enabled_services.
	"""
	series: dict[str, list[dict]] = {}
	
	# Query PING and HTTP metrics
	for metric, label in [("ping_up", "PING"), ("http_up", "HTTP")]:
		if enabled_services is not None and label not in enabled_services:
			continue
		
		try:
			points = query(
				tsdb_dir,
				target_id=target_id,
				metric=metric,
				since=since,
				until=until,
				limit=limit,
				latest=True,
			)
			
			if points:
				series[label] = [
					{"ts": p.ts.isoformat(), "up": int(p.value) if p.value is not None else 0}
					for p in points
				]
		except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
			_log.debug("Skipping availability metric %s for target %s: %s", metric, target_id, e)
			continue
	
	# Query individual TCP port metrics
	if enabled_services is None or "TCP" in enabled_services:
		if tcp_ports:
			# Query specific ports
			for port in tcp_ports:
				try:
					points = query(
						tsdb_dir,
						target_id=target_id,
						metric=f"tcp_up_{port}",
						since=since,
						until=until,
						limit=limit,
						latest=True,
					)
					
					if points:
						series[f"TCP:{port}"] = [
							{"ts": p.ts.isoformat(), "up": int(p.value) if p.value is not None else 0}
							for p in points
						]
				except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
					_log.debug("Skipping availability metric tcp_up_%s for target %s: %s", port, target_id, e)
					continue
		else:
			# Fallback: query aggregated tcp_up if no ports specified
			try:
				points = query(
					tsdb_dir,
					target_id=target_id,
					metric="tcp_up",
					since=since,
					until=until,
					limit=limit,
					latest=True,
				)
				
				if points:
					series["TCP"] = [
						{"ts": p.ts.isoformat(), "up": int(p.value) if p.value is not None else 0}
						for p in points
					]
			except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
				_log.debug("Skipping availability metric tcp_up for target %s: %s", target_id, e)
	
	return series
