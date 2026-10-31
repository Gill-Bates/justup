#!/usr/bin/env python3
#
# app/db/tsdb_segments.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Segment naming and gzip I/O primitives for the time-series store.

A series keeps its newest data in the active, uncompressed ``<stem>.jsonl`` file. Closed days move
into period segments ``<stem>.<key>.jsonl.gz`` (ISO week for raw data, month for 5-minute rollups,
year for hourly rollups). Each closed day is one gzip member; concatenated members form a valid
gzip stream, so appending a day never recompresses the older ones.

The suffix ``.jsonl.gz`` keeps segments out of the ``*.jsonl`` glob that finds active files.

Nothing here takes locks; the caller holds the series lock (``tsdb._FileLock``).
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import logging
import os
import re
import shutil
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from ..utils.time import parse_utc

_log = logging.getLogger(__name__)

SEGMENT_SUFFIX = ".jsonl.gz"
GZIP_LEVEL = 6  # measured: level 9 gains ~3 % at 3-4x the CPU (docs/development/database-maintenance.md)
FILE_MODE = 0o644
_SEGMENT_RE = re.compile(r"^(?P<stem>[A-Za-z0-9_-]+)\.(?P<key>\d{4}W\d{2}|\d{6}|\d{4})\.jsonl\.gz$")
_ROLLUP_SUFFIXES = ("5m_median", "1h_median")


class SegmentCorruptError(Exception):
    """A segment could not be fully decoded."""


@dataclass(frozen=True)
class Segment:
    """One period file of a series."""

    stem: str
    key: str
    path: Path
    start: datetime  # inclusive
    end: datetime  # exclusive


def resolution_of_stem(stem: str) -> str:
    """Resolution of a series stem: rollup series carry their resolution as name suffix."""
    for suffix in _ROLLUP_SUFFIXES:
        if stem.endswith(suffix):
            return suffix
    return "raw"


def period_key(resolution: str, ts: datetime) -> str:
    """Segment key of the period containing ``ts`` (UTC)."""
    ts = ts.astimezone(UTC)
    if resolution == "raw":
        year, week, _ = ts.isocalendar()
        return f"{year:04d}W{week:02d}"
    if resolution == "5m_median":
        return f"{ts.year:04d}{ts.month:02d}"
    return f"{ts.year:04d}"


def period_bounds(key: str) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` of a segment key; the key shape implies the resolution."""
    if "W" in key:
        start = datetime.fromisocalendar(int(key[:4]), int(key[5:]), 1).replace(tzinfo=UTC)
        return start, start + timedelta(days=7)
    if len(key) == 6:
        year, month = int(key[:4]), int(key[4:])
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
        return datetime(year, month, 1, tzinfo=UTC), end
    year = int(key)
    return datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)


def segment_path(directory: Path, stem: str, key: str) -> Path:
    return directory / f"{stem}.{key}{SEGMENT_SUFFIX}"


def _parse_name(name: str) -> tuple[str, str] | None:
    m = _SEGMENT_RE.match(name)
    return (m.group("stem"), m.group("key")) if m else None


def list_segments(directory: Path, stem: str) -> list[Segment]:
    """Segments of one series, oldest period first. Symlinks, temp and quarantined files are ignored."""
    found: list[Segment] = []
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return found
    for entry in entries:
        parsed = _parse_name(entry.name)
        if parsed is None or parsed[0] != stem or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        try:
            start, end = period_bounds(parsed[1])
        except ValueError:  # e.g. week 99: not one of ours
            continue
        found.append(Segment(stem, parsed[1], Path(entry.path), start, end))
    found.sort(key=lambda s: s.start)
    return found


def segment_stems(directory: Path) -> set[str]:
    """Stems that own at least one segment file in ``directory``."""
    stems: set[str] = set()
    try:
        for entry in os.scandir(directory):
            parsed = _parse_name(entry.name)
            if parsed is not None:
                stems.add(parsed[0])
    except FileNotFoundError:
        pass
    return stems


def line_ts(line: str) -> datetime | None:
    """Timestamp of one JSONL record, ``None`` for blank or invalid lines."""
    line = line.strip()
    if not line:
        return None
    try:
        return parse_utc(json.loads(line)["ts"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def open_segment(path: Path) -> BinaryIO:
    """Open a segment for reading without following symlinks. The handle keeps the content it was opened
    on even if the segment is replaced afterwards, which lets a reader work without holding the lock."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise SegmentCorruptError(f"cannot open {path.name}: {e}") from e
    return os.fdopen(fd, "rb")


def iter_lines_from(raw: BinaryIO, name: str) -> Iterator[str]:
    """Stream the lines of an open segment with bounded memory.

    Raises ``SegmentCorruptError`` after yielding every line decoded before the damage, so a caller
    can either stop (readers) or salvage the readable prefix (compaction). Does not close ``raw``.
    """
    gz = gzip.GzipFile(fileobj=raw, mode="rb")
    text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
    try:
        # Not "yield from": closing the generator early must not close the wrapper's file.
        for line in text:  # noqa: UP028 - see above
            yield line
    except (OSError, EOFError, zlib.error) as e:  # BadGzipFile is an OSError
        raise SegmentCorruptError(f"{name}: {e}") from e
    finally:
        text.detach()


def iter_lines(path: Path) -> Iterator[str]:
    """``iter_lines_from`` on a segment path (opened and closed by the generator)."""
    with open_segment(path) as raw:
        yield from iter_lines_from(raw, path.name)


def read_lines_safely(path: Path) -> tuple[list[str], bool]:
    """Read a whole (small) segment, tolerating damage: returns ``(lines, damaged)``."""
    lines: list[str] = []
    try:
        lines.extend(iter_lines(path))
    except SegmentCorruptError as e:
        _log.warning("TSDB segment %s is damaged, %d line(s) readable: %s", path.name, len(lines), e)
        return lines, True
    return lines, False


def segment_info(path: Path) -> tuple[int, datetime | None, bool]:
    """``(line_count, newest_ts, damaged)`` from one streaming pass over a segment."""
    count = 0
    newest: datetime | None = None
    try:
        for line in iter_lines(path):
            ts = line_ts(line)
            if ts is not None:
                count += 1
                newest = ts if newest is None or ts > newest else newest
    except SegmentCorruptError:
        return count, newest, True
    return count, newest, False


def first_ts(path: Path) -> datetime | None:
    """Timestamp of the first valid record of a segment (reads only the first block(s))."""
    try:
        for line in iter_lines(path):
            ts = line_ts(line)
            if ts is not None:
                return ts
    except SegmentCorruptError:
        return None
    return None


def new_temp(directory: Path, label: str) -> tuple[Path, int]:
    """Create an exclusive, symlink-safe temp file next to its destination."""
    path = directory / f".{label}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
    return path, fd


def write_member(fd_file, lines: Iterable[str]) -> int:
    """Write ``lines`` as one gzip member (deterministic header) to an open binary file; returns the count."""
    count = 0
    with gzip.GzipFile(fileobj=fd_file, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as gz:
        for line in lines:
            gz.write((line.rstrip("\n") + "\n").encode("utf-8"))
            count += 1
    return count


def build_segment(directory: Path, label: str, *, keep_from: Path | None, lines: Iterable[str]) -> tuple[Path, int]:
    """Write a temp segment: the raw bytes of ``keep_from`` (if any) followed by one new member.

    Returns ``(temp_path, new_line_count)``; the temp file is fsynced but not yet published.
    """
    tmp, fd = new_temp(directory, label)
    try:
        with os.fdopen(fd, "wb") as out:
            if keep_from is not None:
                with keep_from.open("rb") as old:
                    shutil.copyfileobj(old, out, 1 << 20)
            count = write_member(out, lines)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, count


def verify_segment(path: Path, expected_lines: int, newest: datetime | None) -> bool:
    """Decode the whole file once: line count and newest timestamp must match what was written."""
    count, found_newest, damaged = segment_info(path)
    return not damaged and count == expected_lines and found_newest == newest


def publish(tmp: Path, dest: Path) -> None:
    """Atomically replace ``dest`` with ``tmp`` and make the rename durable."""
    tmp.replace(dest)
    fsync_dir(dest.parent)


def fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
