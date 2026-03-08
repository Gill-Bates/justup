#!/usr/bin/env python3
#
# app/db/tsdb.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""JSONL-based time-series storage for uptime metrics.

Stores response times, status codes, and availability data per monitor.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from weakref import WeakValueDictionary

try:
	import fcntl
except ImportError:
	raise ImportError("fcntl module required - Unix-like systems only.") from None

from ..utils.time import ensure_utc, parse_utc, utcnow

_log = logging.getLogger(__name__)

MIN_RETENTION_DAYS = 1
DEFAULT_RETENTION_DAYS = 90
PRUNE_INTERVAL_SECONDS = 300
MAX_SERIES_FILE_BYTES = 8 * 1024 * 1024
MAX_ROTATED_ARCHIVES = 6
FSYNC_BATCH_SIZE = 10
FSYNC_BATCH_INTERVAL = 5.0


@dataclass(frozen=True)
class MetricPoint:
	ts: datetime
	value: Any


def _series_path(tsdb_dir: Path, monitor_id: int, metric: str) -> Path:
	if not metric or not metric.strip():
		raise ValueError("Metric name cannot be empty")
	safe_metric = "".join(c for c in metric if c.isalnum() or c in ("_", "-"))
	if not safe_metric:
		raise ValueError(f"Metric name '{metric}' contains no valid characters")
	return tsdb_dir / f"monitor_{monitor_id}" / f"{safe_metric}.jsonl"


def _lock_path(series_path: Path) -> Path:
	return series_path.with_suffix(".lock")


class _FileLock:
	_thread_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
	_meta_lock: threading.Lock = threading.Lock()

	def __init__(self, series_path: Path, *, read_only: bool = False):
		self._series_path = series_path
		self._lock_path = _lock_path(series_path)
		self._fd: Optional[int] = None
		self._thread_lock: Optional[threading.Lock] = None
		self._read_only = read_only

	def __enter__(self) -> "_FileLock":
		key = str(self._series_path)
		with self._meta_lock:
			thread_lock = self._thread_locks.get(key)
			if thread_lock is None:
				thread_lock = threading.Lock()
				self._thread_locks[key] = thread_lock
			self._thread_lock = thread_lock

		self._thread_lock.acquire()
		self._lock_path.parent.mkdir(parents=True, exist_ok=True)
		self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
		fcntl.flock(self._fd, fcntl.LOCK_SH if self._read_only else fcntl.LOCK_EX)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb) -> None:
		if self._fd is not None:
			fcntl.flock(self._fd, fcntl.LOCK_UN)
			os.close(self._fd)
			self._fd = None
		if self._thread_lock is not None:
			self._thread_lock.release()
			self._thread_lock = None


def append(tsdb_dir: Path, monitor_id: int, metric: str, value: Any, ts: datetime | None = None) -> None:
	if ts is None:
		ts = utcnow()
	ts = ensure_utc(ts)
	path = _series_path(tsdb_dir, monitor_id, metric)

	record = json.dumps({"ts": ts.isoformat(), "v": value}, separators=(",", ":"))

	with _FileLock(path):
		path.parent.mkdir(parents=True, exist_ok=True)
		with open(path, "a", encoding="utf-8") as f:
			f.write(record + "\n")


def query(
	tsdb_dir: Path,
	monitor_id: int,
	metric: str,
	start: datetime | None = None,
	end: datetime | None = None,
	limit: int | None = None,
) -> list[MetricPoint]:
	path = _series_path(tsdb_dir, monitor_id, metric)
	if not path.exists():
		return []

	start_utc = ensure_utc(start) if start else None
	end_utc = ensure_utc(end) if end else None

	points: list[MetricPoint] = []
	with _FileLock(path, read_only=True):
		with open(path, "r", encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					record = json.loads(line)
					ts = parse_utc(record["ts"])
					if ts is None:
						continue
					if start_utc and ts < start_utc:
						continue
					if end_utc and ts > end_utc:
						continue
					points.append(MetricPoint(ts=ts, value=record.get("v")))
				except (json.JSONDecodeError, KeyError):
					continue

	if limit and len(points) > limit:
		points = points[-limit:]

	return points


def prune(tsdb_dir: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
	retention_days = max(retention_days, MIN_RETENTION_DAYS)
	cutoff = utcnow() - timedelta(days=retention_days)
	total_pruned = 0

	if not tsdb_dir.exists():
		return 0

	for monitor_dir in tsdb_dir.iterdir():
		if not monitor_dir.is_dir() or not monitor_dir.name.startswith("monitor_"):
			continue
		for jsonl_file in monitor_dir.glob("*.jsonl"):
			pruned = _prune_file(jsonl_file, cutoff)
			total_pruned += pruned

	return total_pruned


def _prune_file(path: Path, cutoff: datetime) -> int:
	kept_lines = []
	pruned = 0

	with _FileLock(path):
		with open(path, "r", encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					record = json.loads(line)
					ts = parse_utc(record["ts"])
					if ts and ts < cutoff:
						pruned += 1
						continue
				except (json.JSONDecodeError, KeyError):
					continue
				kept_lines.append(line)

		if pruned > 0:
			tmp = path.with_suffix(path.suffix + ".tmp")
			with open(tmp, "w", encoding="utf-8") as f:
				for line in kept_lines:
					f.write(line + "\n")
			os.replace(str(tmp), str(path))

	return pruned


def delete_monitor_metrics(tsdb_dir: Path, monitor_id: int) -> bool:
	"""Delete all metrics for a specific monitor."""
	monitor_dir = tsdb_dir / f"monitor_{monitor_id}"
	if not monitor_dir.exists():
		return False

	try:
		shutil.rmtree(monitor_dir)
		_log.info("Deleted metrics for monitor %d", monitor_id)
		return True
	except Exception as e:
		_log.error("Failed to delete metrics for monitor %d: %s", monitor_id, e)
		return False


def list_monitor_metrics(tsdb_dir: Path, monitor_id: int) -> list[str]:
	"""List available metrics for a monitor."""
	monitor_dir = tsdb_dir / f"monitor_{monitor_id}"
	if not monitor_dir.exists():
		return []

	metrics = []
	for jsonl_file in monitor_dir.glob("*.jsonl"):
		metrics.append(jsonl_file.stem)
	return sorted(metrics)
