#!/usr/bin/env python3
#
# app/_cython/quality_probe.pyx
# Copyright (C) 2025-2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Monitoring Quality Probe – measures the monitor's own connectivity.

This module implements a median-based quality gate that prevents false
alerts when the monitoring host itself has connectivity issues.

Supports two target types:
- HTTP URLs (https://..., http://...): HTTP HEAD request
- Plain IPs/hostnames: ICMP ping

The quality score (0-100) is computed from:
- Success ratio of probe targets (60% weight)
- Median latency compared to threshold (40% weight)

States:
- "ok": Score >= MIN_SCORE, all checks proceed normally
- "degraded": Score < MIN_SCORE but >=66% success, checks may be skipped
- "down": Less than minimum successful probes, all checks suspended (score=None)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

import httpx

_log = logging.getLogger(__name__)

# Module-level state for score smoothing (EMA) with thread-safe access
_score_lock = threading.Lock()
_last_score: Optional[float] = None

# Defaults (overridable via environment)
DEFAULT_QUALITY_TARGETS = [
    "https://1.1.1.1",      # Cloudflare (HTTP)
    "8.8.8.8",              # Google DNS (Ping)
    "9.9.9.9",              # Quad9 (Ping)
    "https://www.de-cix.net",  # DE-CIX (HTTP)
    "https://cloudflare.com",  # Cloudflare (HTTP)
]
DEFAULT_MIN_SCORE = 70
DEFAULT_RTT_THRESHOLD_MS = 500  # Realistic for mobile/VPN/IPv6
PROBE_TIMEOUT_SECONDS = 4.0    # Generous timeout for real networks
SCORE_SMOOTHING_FACTOR = 0.3   # EMA: 30% new, 70% previous
MAX_CONCURRENT_PROBES = 5      # Limit parallel probes to avoid self-DoS

# Regex to detect if target is a URL (has scheme)
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class QualityResult:
    """Result of a quality probe run."""
    score: Optional[float]        # 0-100, None if no connectivity
    median_latency_ms: Optional[float]
    success_ratio: float          # 0.0-1.0
    state: str                    # "ok" | "degraded" | "down"


@lru_cache(maxsize=1)
def _get_probe_config() -> tuple[list[str], int, int]:
    """Cached probe config to avoid repeated env var parsing."""
    return get_quality_targets(), get_min_score(), get_rtt_threshold()


def _is_ip_url(url: str) -> bool:
    """Check if URL target is an IP address (IPv4 or IPv6).
    
    Returns True for:
    - https://1.1.1.1
    - https://[2606:4700:4700::1111]
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _validate_ping_target(host: str) -> bool:
    """Validate ping target to prevent command injection.
    
    Returns True if host is a valid IP address or hostname.
    """
    # Try IP address first
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    
    # Hostname: alphanumeric + dots + hyphens, must start with alphanumeric
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-]*$', host)) and len(host) <= 253


def get_quality_targets() -> list[str]:
    """
    Get the list of quality probe targets from environment.
    
    Supports mixed formats:
    - URLs: https://1.1.1.1, https://www.example.com
    - IPs: 8.8.8.8, 9.9.9.9 (will use ping)
    - Hostnames: dns.google (will use ping)
    
    Falls back to sensible defaults if not configured.
    """
    raw = os.getenv("UPMON_QUALITY_TARGETS", "")
    if raw.strip():
        targets = [t.strip() for t in raw.split(",") if t.strip()]
        if len(targets) >= 3:
            return targets
        _log.warning(
            "UPMON_QUALITY_TARGETS has %d targets (minimum 3), using defaults",
            len(targets)
        )
    return DEFAULT_QUALITY_TARGETS


def get_min_score() -> int:
    """Get minimum acceptable quality score."""
    try:
        return int(os.getenv("UPMON_QUALITY_MIN_SCORE", DEFAULT_MIN_SCORE))
    except ValueError:
        return DEFAULT_MIN_SCORE


def get_rtt_threshold() -> int:
    """Get RTT threshold in milliseconds."""
    try:
        val = int(os.getenv("UPMON_QUALITY_RTT_THRESHOLD_MS", DEFAULT_RTT_THRESHOLD_MS))
        return max(1, val)  # Minimum 1ms to prevent division by zero
    except ValueError:
        return DEFAULT_RTT_THRESHOLD_MS


async def _measure_http_rtt(client: httpx.AsyncClient, url: str) -> Optional[float]:
    """
    Measure round-trip time via HTTP HEAD request.
    
    Uses the passed client (with appropriate verify setting).
    Does not follow redirects to get accurate RTT for the target itself.
    Redirects (3xx) are treated as success (server responded).
    Returns RTT in milliseconds, or None on failure.
    """
    try:
        start = time.monotonic()
        resp = await client.head(url, follow_redirects=False)
        elapsed_ms = (time.monotonic() - start) * 1000
        
        # Treat server errors (5xx) as failures, but redirects (3xx) are OK
        # The probe measures "is the server responding", not "is content available"
        if resp.status_code >= 500:
            _log.debug("Quality probe HTTP %s returned %d", url, resp.status_code)
            return None
        
        return elapsed_ms
    except httpx.TimeoutException:
        _log.debug("Quality probe HTTP %s timed out", url)
        return None
    except httpx.RequestError as e:
        _log.debug("Quality probe HTTP %s failed: %s", url, e)
        return None
    except Exception as e:
        _log.debug("Quality probe HTTP %s unexpected error: %s", url, e)
        return None


async def _measure_ping_rtt(host: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Optional[float]:
    """
    Measure round-trip time via ICMP ping.
    
    Uses the system ping command with both OS-level and asyncio timeout protection.
    Returns RTT in milliseconds, or None on failure.
    
    Note: ping -W timeout semantics vary by platform:
    - Linux: -W is in seconds
    - macOS/BSD: -W is in milliseconds, use -t for seconds
    This implementation targets Linux (Docker container environment).
    """
    try:
        # -n: No reverse DNS lookup to avoid hanging on DNS failures
        cmd = ["ping", "-c", "1", "-W", str(int(timeout)), "-n", host]
        
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Asyncio timeout wrapper to catch cases where ping process hangs
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout + 2.0,  # OS timeout + buffer
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _log.debug("Quality probe ping %s timed out (asyncio)", host)
            return None
        elapsed_ms = (time.monotonic() - start) * 1000
        
        if proc.returncode == 0:
            # Try to extract RTT from ping output (more accurate)
            output = stdout.decode("utf-8", errors="ignore")
            # Look for "time=X.XX ms" in output
            match = re.search(r"time[=<]\s*([\d.]+)\s*ms", output, re.IGNORECASE)
            if match:
                return float(match.group(1))
            # Fallback to measured time
            return elapsed_ms
        else:
            _log.debug("Quality probe ping %s failed (exit %d)", host, proc.returncode)
            return None
            
    except asyncio.TimeoutError:
        _log.debug("Quality probe ping %s timed out", host)
        return None
    except Exception as e:
        _log.debug("Quality probe ping %s error: %s", host, e)
        return None


async def _measure_target_rtt(
    client: httpx.AsyncClient, 
    target: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, Optional[float]]:
    """
    Measure RTT to a target, auto-detecting HTTP vs Ping.
    
    Uses semaphore to limit concurrent probes.
    Client is already configured with appropriate verify setting.
    Returns (target, rtt_ms) tuple.
    """
    async with semaphore:
        if URL_PATTERN.match(target):
            rtt = await _measure_http_rtt(client, target)
        else:
            rtt = await _measure_ping_rtt(target)
        
        return (target, rtt)


async def run_quality_probe() -> QualityResult:
    """
    Run the quality probe against all configured targets.
    
    Uses async HTTP HEAD requests for URLs and ICMP ping for IPs/hostnames.
    Median-based aggregation determines connectivity quality.
    
    Returns:
        QualityResult with score, latency, success ratio, and state.
    """
    targets = get_quality_targets()
    rtt_threshold = get_rtt_threshold()
    min_score = get_min_score()
    
    rtts: list[float] = []
    
    # Semaphore limits concurrent probes to avoid network/resource exhaustion
    probe_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    
    # Separate clients for verified and unverified HTTPS (IP-based URLs need verify=False)
    # Most targets use the verified client, IP URLs get special treatment
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
        verify=True,
    ) as verified_client, httpx.AsyncClient(
        timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
        verify=False,
    ) as unverified_client:
        # Run probes with limited concurrency, selecting appropriate client per target
        tasks = []
        for target in targets:
            # Use unverified client for IP-based HTTPS URLs (IPv4 + IPv6)
            if _is_ip_url(target):
                tasks.append(_measure_target_rtt(unverified_client, target, probe_semaphore))
            else:
                tasks.append(_measure_target_rtt(verified_client, target, probe_semaphore))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                _log.debug("Quality probe exception: %s", result)
                continue
            target, rtt = result
            if rtt is not None:
                rtts.append(rtt)
    
    total = len(targets)
    succeeded = len(rtts)
    success_ratio = succeeded / total if total > 0 else 0.0

    # Minimum required successes: at least 2, or 1/3 of targets, whichever is higher
    # This ensures statistical validity for median calculation and prevents false positives
    # when most targets are unreachable.
    min_required = max(2, len(targets) // 3)
    if len(rtts) < min_required:
        _log.warning(
            "Quality probe: only %d/%d targets reachable (min %d required) -> state=down",
            len(rtts), total, min_required
        )
        return QualityResult(
            score=None,
            median_latency_ms=None,
            success_ratio=success_ratio,
            state="down",
        )
    
    # Compute median RTT (robust against outliers)
    median_rtt = statistics.median(rtts)
    
    # Score calculation with weighted components:
    # - 60% weight: success ratio (actual successful probes)
    #   Weighted higher because reliability is more important than speed for monitoring.
    # - 40% weight: latency score (1 - median/threshold, clamped 0-1)
    #   Penalizes high latency but doesn't dominate the score.
    latency_score = max(0.0, min(1.0, 1 - (median_rtt / rtt_threshold)))
    raw_score = (success_ratio * 0.6 + latency_score * 0.4) * 100
    
    # Apply asymmetric EMA smoothing: fast drop on degradation, slow recovery
    # This prevents delayed failure detection while avoiding alert flapping.
    global _last_score
    with _score_lock:
        if _last_score is not None:
            if raw_score < _last_score:
                # Fast but not instant drop: 80% new, 20% previous
                # Prevents single-probe flapping while still reacting quickly
                score = round(_last_score * 0.2 + raw_score * 0.8, 1)
            else:
                # Slow recovery: smooth upward to avoid flapping
                score = round(
                    _last_score * (1 - SCORE_SMOOTHING_FACTOR)
                    + raw_score * SCORE_SMOOTHING_FACTOR, 1
                )
        else:
            score = round(raw_score, 1)
        _last_score = score
    
    # State determination (in order of severity):
    # - "ok": Score meets threshold, normal operation
    # - "degraded": Score below threshold but 2/3+ targets reachable (soft down)
    #   Checks may be skipped to avoid false alerts during connectivity issues.
    # - "down": Less than 2/3 targets reachable (hard down via success_ratio)
    #   Note: The earlier "hard down" check (< 2 RTTs) already returned.
    if score >= min_score:
        state = "ok"
    elif succeeded * 3 >= total * 2:  # Exact 2/3 check (integer math, no float precision issues)
        state = "degraded"
    else:
        state = "down"
    
    _log.debug(
        "Quality probe: score=%.1f, median_rtt=%.1fms, success=%d/%d, state=%s",
        score, median_rtt, len(rtts), total, state
    )
    
    return QualityResult(
        score=score,
        median_latency_ms=round(median_rtt, 1),
        success_ratio=round(success_ratio, 2),
        state=state,
    )


def _reset_score_state() -> None:
    """Reset EMA state. For testing only."""
    global _last_score
    with _score_lock:
        _last_score = None
