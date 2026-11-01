#!/usr/bin/env python3
#
# tools/tsdb-bench.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Offline TSDB storage benchmarks for justUp (synthetic data, never touches real data).

Subcommands:
  codecs      Compare gzip / bz2 / lzma (and zstd when the optional ``zstandard`` module is
              importable) on synthetic series files: ratio, compress and decompress speed, peak RSS.
  segments    Compare daily and weekly segmentation: file count, logical size, filesystem blocks.
  scenarios   Build the steady-state legacy dataset for each scenario and measure size (logical and
              st_blocks), write throughput, query time (24 h / 30 d / 365 d), housekeeping, backup
              size/time and restore time. Runs against the checked-out ``app.db.tsdb``, so the same
              command produces the baseline (JSONL only) and the post-change numbers.

Usage:
  .venv/bin/python tools/tsdb-bench.py codecs [--tmpdir DIR]
  .venv/bin/python tools/tsdb-bench.py segments [--tmpdir DIR]
  .venv/bin/python tools/tsdb-bench.py scenarios [--only NAME ...] [--tmpdir DIR] [--json FILE]

Use ``--tmpdir`` on a real disk (not tmpfs): fsync and block allocation on tmpfs hide real costs.
"""

# ruff: noqa: E501, S311, S101, SLF001
# Benchmark script: deterministic PRNG, console output and private TSDB helpers by design.
from __future__ import annotations

import argparse
import bz2
import gzip
import json
import lzma
import multiprocessing as mp
import random
import resource
import shutil
import sys
import tarfile
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import tsdb

try:  # optional, benchmark-only: zstd is deliberately not an application dependency
    import zstandard
except ImportError:
    zstandard = None

DAY = 86400
UP = ("ping_up", "http_up", "tcp_up")
HTTP_SET = ("ping_ms", "ping_up", "http_ms", "http_up", "http_status")


@dataclass(frozen=True)
class Scenario:
    name: str
    interval: int
    retention_days: int
    metrics: tuple[str, ...]


def _tcp_metrics(ports: tuple[int, ...]) -> tuple[str, ...]:
    return ("ping_ms", "ping_up", "tcp_up", "tcp_ms", *[f"tcp_up_{p}" for p in ports], *[f"tcp_ms_{p}" for p in ports])


SCENARIOS = {
    s.name: s
    for s in (
        Scenario("ping-60s-365d", 60, 365, ("ping_ms", "ping_up")),
        Scenario("http-ping-60s-365d", 60, 365, HTTP_SET),
        Scenario("http-ping-cert-30s-365d", 30, 365, (*HTTP_SET, "cert_days_left")),
        Scenario("tcp-ports-60s-365d", 60, 365, _tcp_metrics((22, 80, 443, 3306))),
        Scenario("intensive-5s-365d", 5, 365, HTTP_SET),
        Scenario("longterm-60s-3650d", 60, 3650, ("ping_ms", "ping_up")),
    )
}


# ---------------------------------------------------------------------------
# Synthetic data (mirrors what the scheduler writes: full-precision floats, 0/1 ints, status codes)
# ---------------------------------------------------------------------------


def _value_fn(metric: str, rng: random.Random, *, rollup: bool):
    base = tsdb._base_metric(metric)
    if tsdb.is_binary_metric(base):
        if rollup:
            return lambda: "0.0" if rng.random() < 0.002 else "1.0"
        return lambda: "0" if rng.random() < 0.0005 else "1"
    if base == "http_status":
        if rollup:
            return lambda: "200.0"
        return lambda: "503" if rng.random() < 0.0005 else "200"
    if base == "cert_days_left":
        return lambda: "73.0"
    mu = 3.0 + (zlib.crc32(base.encode()) % 7) * 0.15
    sigma = 0.08 if rollup else 0.25
    return lambda: repr(rng.lognormvariate(mu, sigma))


def _write_series(path: Path, start: float, end: float, step: int, *, jitter: bool, valfn) -> int:
    """Write ``[start, end)`` at ``step`` seconds; raw points carry microsecond jitter like utcnow()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    rng = random.Random(7)
    with path.open("w", encoding="utf-8", buffering=1 << 20) as f:
        t = start
        while t < end:
            ts = datetime.fromtimestamp(t + (rng.random() * 0.9 if jitter else 0.0), tz=UTC).isoformat()
            f.write('{"ts": "' + ts + '", "value": ' + valfn() + "}\n")
            t += step
            n += 1
    return n


def generate_legacy(tsdb_dir: Path, sc: Scenario, now: datetime, target_id: int = 1) -> int:
    """Steady-state layout of the previous release: raw 30 d, 5m rollups to 180 d, 1h rollups to retention."""
    t_now = now.timestamp()
    total = 0
    cap = min(sc.retention_days, tsdb.MAX_RETENTION_DAYS)
    for metric in sc.metrics:
        rng = random.Random(zlib.crc32(f"{sc.name}/{metric}".encode()))
        raw = tsdb._series_path(tsdb_dir, target_id, metric)
        total += _write_series(
            raw, t_now - 30 * DAY, t_now, sc.interval, jitter=True, valfn=_value_fn(metric, rng, rollup=False)
        )
        if cap > 30:
            r5 = tsdb._series_path(tsdb_dir, target_id, f"{metric}@5m_median")
            lo = t_now - min(cap, 180) * DAY
            lo -= lo % 300
            total += _write_series(
                r5, lo, t_now - 30 * DAY, 300, jitter=False, valfn=_value_fn(metric, rng, rollup=True)
            )
        if cap > 180:
            r1 = tsdb._series_path(tsdb_dir, target_id, f"{metric}@1h_median")
            lo = t_now - cap * DAY
            lo -= lo % 3600
            total += _write_series(
                r1, lo, t_now - 180 * DAY, 3600, jitter=False, valfn=_value_fn(metric, rng, rollup=True)
            )
    return total


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def dir_stats(root: Path) -> dict:
    logical = blocks = files = 0
    for p in root.rglob("*"):
        if p.is_file() and p.suffix != ".lock":
            st = p.stat()
            logical += st.st_size
            blocks += st.st_blocks * 512
            files += 1
    return {"files": files, "logical_bytes": logical, "disk_bytes": blocks}


def _child(q, fn, args):
    t0, c0 = time.perf_counter(), time.process_time()
    out = fn(*args)
    q.put((out, time.perf_counter() - t0, time.process_time() - c0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))


def run_phase(fn, *args) -> dict:
    """Run ``fn`` in a forked child so wall time, CPU time and peak RSS belong to that phase only."""
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q, fn, args))
    p.start()
    out, wall, cpu, rss = q.get()
    p.join()
    return {"result": out, "wall_s": round(wall, 3), "cpu_s": round(cpu, 3), "peak_rss_mb": round(rss / 1024, 1)}


def _mb(n: float) -> float:
    return round(n / 1e6, 2)


# ---------------------------------------------------------------------------
# codecs / segments
# ---------------------------------------------------------------------------


def _codecs():
    codecs = {
        "gzip-1": (lambda b: gzip.compress(b, 1), gzip.decompress),
        "gzip-6": (lambda b: gzip.compress(b, 6), gzip.decompress),
        "gzip-9": (lambda b: gzip.compress(b, 9), gzip.decompress),
        "bz2-9": (lambda b: bz2.compress(b, 9), bz2.decompress),
        "xz-6": (lambda b: lzma.compress(b, preset=6), lzma.decompress),
    }
    if zstandard is not None:
        for level in (3, 10, 19):
            c, d = zstandard.ZstdCompressor(level=level), zstandard.ZstdDecompressor()
            codecs[f"zstd-{level}"] = (c.compress, d.decompress)
    return codecs


def _codec_row(name: str, comp, decomp, blob: bytes) -> dict:
    t0 = time.perf_counter()
    packed = comp(blob)
    ct = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert decomp(packed) == blob
    dt = time.perf_counter() - t0
    return {
        "codec": name,
        "ratio": round(len(blob) / len(packed), 2),
        "saving_pct": round(100 * (1 - len(packed) / len(blob)), 1),
        "compress_MBps": round(len(blob) / ct / 1e6, 1),
        "decompress_MBps": round(len(blob) / dt / 1e6, 1),
    }


def cmd_codecs(args) -> None:
    work = Path(tempfile.mkdtemp(prefix="tsdb-bench-codecs-", dir=args.tmpdir))
    try:
        now = datetime.now(UTC)
        sc = SCENARIOS["http-ping-60s-365d"]
        generate_legacy(work, sc, now)
        print(
            f"zstd available: {zstandard is not None}" + (f" (zstandard {zstandard.__version__})" if zstandard else "")
        )
        for metric in ("ping_ms", "ping_up", "http_status", "ping_ms@5m_median", "ping_ms@1h_median"):
            blob = tsdb._series_path(work, 1, metric).read_bytes()
            print(f"\n== {metric}: {len(blob)} bytes, {blob.count(10)} lines")
            for name, (comp, decomp) in _codecs().items():
                row = run_phase(_codec_row, name, comp, decomp, blob)
                print(
                    "  {codec:8} ratio {ratio:>6}  saving {saving_pct:>5}%  comp {compress_MBps:>6} MB/s  decomp {decompress_MBps:>7} MB/s".format(
                        **row["result"]
                    )
                    + f"  peakRSS {row['peak_rss_mb']} MB"
                )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_segments(args) -> None:
    work = Path(tempfile.mkdtemp(prefix="tsdb-bench-seg-", dir=args.tmpdir))
    try:
        now = datetime.now(UTC)
        sc = SCENARIOS["http-ping-60s-365d"]
        generate_legacy(work, sc, now)
        raw_lines = tsdb._series_path(work, 1, "ping_ms").read_text(encoding="utf-8").splitlines(keepends=True)
        print("ping_ms raw, 30 days of 60 s checks, gzip level 6, per series:")
        for label, keyfn in (
            ("daily", lambda ts: ts[:10]),
            ("weekly", lambda ts: datetime.fromisoformat(ts).strftime("%G-W%V")),
            ("monthly", lambda ts: ts[:7]),
        ):
            groups: dict[str, list[str]] = {}
            for line in raw_lines:
                groups.setdefault(keyfn(json.loads(line)["ts"]), []).append(line)
            logical = sum(len(gzip.compress("".join(v).encode(), 6)) for v in groups.values())
            blocks = sum(-(-len(gzip.compress("".join(v).encode(), 6)) // 4096) * 4096 for v in groups.values())
            print(f"  {label:8} files {len(groups):>3}  gz bytes {logical:>9}  4K-block bytes {blocks:>9}")
        print(f"  plain jsonl bytes {sum(len(x) for x in raw_lines)}")
        # Multi-member append (daily members concatenated into a weekly file)
        members = {}
        for line in raw_lines:
            members.setdefault(json.loads(line)["ts"][:10], []).append(line)
        joined = b"".join(gzip.compress("".join(v).encode(), 6) for v in members.values())
        single = gzip.compress("".join(raw_lines).encode(), 6)
        print(f"  daily members concatenated: {len(joined)} bytes vs one stream {len(single)} bytes")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def _write_throughput(tsdb_dir: Path, n: int) -> float:
    t0 = time.perf_counter()
    start = datetime.now(UTC)
    for i in range(n):
        tsdb.append_point(
            tsdb_dir,
            target_id=99,
            metric="ping_ms",
            value=12.3456789 + i,
            retention_days=365,
            at=start + timedelta(milliseconds=i),
        )
    return round(n / (time.perf_counter() - t0), 0)


def _query_times(tsdb_dir: Path, metric: str) -> dict:
    out = {}
    for label, days in (("24h", 1), ("30d", 30), ("365d", 365)):
        samples = []
        for _ in range(3):
            now = datetime.now(UTC)
            t0 = time.perf_counter()
            pts = tsdb.query_auto_resolution(
                tsdb_dir, target_id=1, metric=metric, since=now - timedelta(days=days), limit=1000
            )
            samples.append(time.perf_counter() - t0)
        out[label] = {"ms": round(sorted(samples)[1] * 1000, 1), "points": len(pts)}
    return out


def _housekeeping(tsdb_dir: Path, retention_days: int) -> dict:
    """Full housekeeping pass. On the segmented store the first pass is also the migration of the
    legacy files; the second pass shows the steady-state (skip) cost."""
    out = {}
    for label in ("first_s", "second_s"):
        t0 = time.perf_counter()
        if hasattr(tsdb, "housekeep_target"):
            # Unthrottled and unbudgeted: measures the work, not the politeness delay.
            while not tsdb.housekeep_target(tsdb_dir, 1, retention_days, budget_seconds=3600, throttle=False).complete:
                pass
        else:
            tsdb.run_rollups(tsdb_dir, 1, retention_days)
            tsdb.prune_all_series(tsdb_dir, 1, retention_days)
        out[label] = round(time.perf_counter() - t0, 3)
    return out


def _backup(tsdb_dir: Path, archive: Path, snap: Path) -> int:
    """Mirror create_backup(): lock-consistent snapshot, then bzip2 tarball."""
    tsdb.snapshot_tsdb(tsdb_dir, snap)
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(snap, arcname="data/tsdb")
    return archive.stat().st_size


def _restore(archive: Path, dest: Path) -> int:
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(dest, filter="data")
    return sum(1 for _ in dest.rglob("*") if _.is_file())


def run_scenario(sc: Scenario, base: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"{sc.name}-", dir=base))
    try:
        tsdb_dir = work / "tsdb"
        now = datetime.now(UTC)
        t0 = time.perf_counter()
        points = generate_legacy(tsdb_dir, sc, now)
        gen_s = round(time.perf_counter() - t0, 1)
        res: dict = {
            "scenario": sc.name,
            "interval_s": sc.interval,
            "retention_days": sc.retention_days,
            "points": points,
            "generate_s": gen_s,
        }
        res["before"] = dir_stats(tsdb_dir)
        res["housekeeping"] = run_phase(_housekeeping, tsdb_dir, sc.retention_days)
        res["after"] = dir_stats(tsdb_dir)
        res["write_pts_per_s"] = run_phase(_write_throughput, work / "w", 3000)["result"]
        res["query"] = run_phase(_query_times, tsdb_dir, "ping_ms")
        archive = work / "backup.tar.bz2"
        res["backup"] = run_phase(_backup, tsdb_dir, archive, work / "snap")
        res["backup_bytes"] = archive.stat().st_size
        res["restore"] = run_phase(_restore, archive, work / "restore")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_scenarios(args) -> None:
    results = []
    for name, sc in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        print(f"-- {name} ...", flush=True)
        r = run_scenario(sc, Path(args.tmpdir) if args.tmpdir else Path(tempfile.gettempdir()))
        results.append(r)
        b, a = r["before"], r["after"]
        print(
            f"   points {r['points']:>9}  files {b['files']}->{a['files']}  logical {_mb(b['logical_bytes'])}->{_mb(a['logical_bytes'])} MB  "
            f"disk(st_blocks) {_mb(b['disk_bytes'])}->{_mb(a['disk_bytes'])} MB  backup {_mb(r['backup_bytes'])} MB"
        )
        print(
            f"   housekeeping first {r['housekeeping']['result']['first_s']}s second {r['housekeeping']['result']['second_s']}s "
            f"(peak RSS {r['housekeeping']['peak_rss_mb']} MB, cpu {r['housekeeping']['cpu_s']}s)  write {r['write_pts_per_s']} pts/s"
        )
        q = r["query"]["result"]
        print(
            f"   query 24h {q['24h']['ms']} ms / 30d {q['30d']['ms']} ms / 365d {q['365d']['ms']} ms  "
            f"backup {r['backup']['wall_s']}s  restore {r['restore']['wall_s']}s (peak RSS {r['restore']['peak_rss_mb']} MB)",
            flush=True,
        )
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("codecs", cmd_codecs), ("segments", cmd_segments), ("scenarios", cmd_scenarios)):
        p = sub.add_parser(name)
        p.add_argument("--tmpdir", default=None, help="directory on a real disk for scratch data")
        if name == "scenarios":
            p.add_argument("--only", nargs="*", choices=list(SCENARIOS))
            p.add_argument("--json", default=None)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    if args.tmpdir:
        Path(args.tmpdir).mkdir(parents=True, exist_ok=True)
    args.fn(args)


if __name__ == "__main__":
    main()
