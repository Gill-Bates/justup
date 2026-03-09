#!/usr/bin/env python3
#
# app/services/mtr_run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""MTR execution and parsing utilities.

Uses mtr-tiny (or mtr as fallback) for stable hop detection with:
- Consistent latency values (avg/min/max)
- Packet loss percentage
- JSON output for reliable parsing

This replaces the legacy traceroute-based approach.
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_log = logging.getLogger(__name__)

# Maximum hostname length for display (keep full hostname for POP inference)
# 50 chars is reasonable for PDF/UI - CSS can handle further truncation if needed
HOSTNAME_DISPLAY_MAX_LENGTH = 50
HOSTNAME_TRUNCATION_SUFFIX = "\u2026"

# --- Status constants (shared with traceroute_geo, route_interpolator) ---
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_TIMEOUT = "timeout"


def _find_mtr_binary() -> Optional[str]:
	"""Find mtr or mtr-tiny binary."""
	# Prefer mtr-tiny (smaller, no X11 deps)
	for binary in ["mtr-tiny", "mtr"]:
		path = shutil.which(binary)
		if path:
			return path
	return None


def _mtr_supports_json(mtr_bin: str) -> bool:
	"""Check if mtr binary supports --json output.
	
	Some older mtr-tiny versions don't have --json.
	Feature-detect to avoid silent failures.
	"""
	try:
		result = subprocess.run(
			[mtr_bin, "--help"],
			capture_output=True,
			text=True,
			timeout=5,
			stdin=subprocess.DEVNULL,
		)
		# Check both stdout and stderr (--help may go to either)
		help_text = result.stdout + result.stderr
		# Use word boundaries to avoid false positives (e.g., -jitter)
		return bool(re.search(r'--json\b', help_text) or re.search(r'\b-j\b', help_text))
	except Exception:
		return False


def run_mtr(
	host: str,
	count: int = 3,
	max_hops: int = 20,
	timeout_s: int = 30,
	force_ipv6: bool | None = None,
) -> list[dict[str, Any]]:
	"""Run MTR and return parsed results in normalized format.

	Args:
		host: Target hostname or IP.
		count: Number of pings per hop (default: 3 for stability).
		max_hops: Maximum hop count.
		timeout_s: Overall command timeout in seconds.
		force_ipv6: If True, force IPv6 (-6). If False, force IPv4 (-4).
		            If None (default), let the system decide.

	Returns:
		List of hop dicts with keys:
		- hop: int (1-based hop number)
		- ip: str (IP address or "*" for timeout)
		- hostname: str (resolved hostname or None)
		- hostname_display: str (shortened hostname for UI/PDF or None)
		- latency_ms: dict with avg, min, max (or None for timeout)
		- loss_pct: float (0.0 - 100.0)
		- status: "ok" | "timeout" | "partial"
	"""
	hops: list[dict[str, Any]] = []

	mtr_bin = _find_mtr_binary()
	if not mtr_bin:
		_log.warning("mtr/mtr-tiny not found in PATH")
		return hops

	# Feature detection: ensure --json is supported
	if not _mtr_supports_json(mtr_bin):
		_log.warning(
			"mtr binary '%s' does not support --json output. "
			"Upgrade to mtr >= 0.92 or install mtr-tiny with JSON support.",
			mtr_bin,
		)
		return hops

	try:
		# MTR options:
		# --json: JSON output (mtr >= 0.92)
		# -n: no DNS (we do our own for timeout control, more universal than --no-dns)
		# -c: packet count per hop
		# -m: max hops
		cmd = [
			mtr_bin,
			"--json",
			"-n",
			"-c", str(count),
			"-m", str(max_hops),
		]
		
		# Force address family if requested (improves IPv6 reliability / consistency)
		if force_ipv6 is True:
			cmd.append("-6")
		elif force_ipv6 is False:
			cmd.append("-4")
		
		cmd.append(host)

		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			timeout=timeout_s,
			stdin=subprocess.DEVNULL,
		)

		if result.returncode != 0:
			_log.debug("MTR returned non-zero: %s, stderr: %s", result.returncode, result.stderr)
			# MTR may still have partial output

		if not result.stdout:
			_log.debug("MTR produced no output for %s", host)
			return hops

		# Parse JSON output
		try:
			data = json.loads(result.stdout)
		except json.JSONDecodeError as e:
			_log.warning("Failed to parse MTR JSON output: %s", e)
			return hops

		# MTR JSON structure:
		# { "report": { "mtr": {...}, "hubs": [...] } }
		report = data.get("report", {})
		hubs = report.get("hubs", [])

		def _hub_float(hub: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
			for k in keys:
				if k in hub and hub.get(k) is not None:
					try:
						return float(hub.get(k))
					except (TypeError, ValueError):
						pass
			return default

		for hop_num, hub in enumerate(hubs, start=1):
			# hop_num is the hop index (MTR's "count" is probe count, not hop number)
			ip_raw = hub.get("host") if isinstance(hub, dict) else None
			if not ip_raw and isinstance(hub, dict):
				ip_raw = hub.get("Host")
			ip = str(ip_raw).strip() if ip_raw else "*"

			# Cast to float - MTR may return "0.0" as string in some versions
			loss = _hub_float(hub, ("Loss%", "loss", "Loss"), 100.0) if isinstance(hub, dict) else 100.0

			avg = _hub_float(hub, ("Avg", "avg"), 0.0) if isinstance(hub, dict) else 0.0
			best = _hub_float(hub, ("Best", "best"), 0.0) if isinstance(hub, dict) else 0.0
			wrst = _hub_float(hub, ("Wrst", "wrst"), 0.0) if isinstance(hub, dict) else 0.0

			# Determine status based on loss
			# Note: MTR sometimes returns 99.9% for effective timeouts
			if loss >= 99.9 or ip == "???":
				status = STATUS_TIMEOUT
				ip = "*"
				loss = 100.0
				latency = None
			elif loss > 0:
				status = STATUS_PARTIAL
				latency = {"avg": avg, "min": best, "max": wrst}
			else:
				status = STATUS_OK
				latency = {"avg": avg, "min": best, "max": wrst}

			hop_data: dict[str, Any] = {
				"hop": hop_num,
				"ip": ip,
				"hostname": None,  # Resolved after parsing (parallel)
				"hostname_display": None,  # Shortened display variant
				"latency_ms": latency,
				"loss_pct": loss,
				"status": status,
			}

			hops.append(hop_data)

		# Parallel DNS resolution for all responding hops
		ips_to_resolve = {
			h["ip"] for h in hops
			if h["status"] in (STATUS_OK, STATUS_PARTIAL) and h["ip"] != "*"
		}

		if ips_to_resolve:
			executor = _get_dns_executor()
			futures = {ip: executor.submit(_resolve_sync, ip) for ip in ips_to_resolve}

			resolved: dict[str, str | None] = {}
			for ip, future in futures.items():
				timeout = 1.5 if ":" in ip else 0.5
				try:
					resolved[ip] = future.result(timeout=timeout)
				except Exception:
					resolved[ip] = None

			# Apply resolved hostnames to hops
			for h in hops:
				hostname = resolved.get(h["ip"])
				if hostname:
					h["hostname"] = hostname
					h["hostname_display"] = _shorten_hostname_for_display(hostname)

	except subprocess.TimeoutExpired:
		_log.warning("MTR timed out for %s after %ds", host, timeout_s)
	except FileNotFoundError:
		_log.warning("MTR binary not found")
	except Exception as e:
		_log.debug("MTR failed for %s: %s", host, e, exc_info=True)

	return hops


# Shared executor for DNS resolution (avoids creating per-hop executors)
_dns_executor: ThreadPoolExecutor | None = None
_dns_executor_lock = threading.Lock()  # Initialize on module load, not lazily

def _get_dns_executor() -> ThreadPoolExecutor:
	"""Get or create the shared DNS resolver executor."""
	global _dns_executor
	
	if _dns_executor is None:
		with _dns_executor_lock:
			if _dns_executor is None:
				_dns_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolver")
				# Register cleanup on process exit (graceful shutdown for tests/reload)
				atexit.register(_dns_executor.shutdown, wait=False)
	return _dns_executor


def _resolve_sync(ip: str) -> str | None:
	"""Synchronous DNS resolution (runs in executor thread)."""
	try:
		hostname, _, _ = socket.gethostbyaddr(ip)
		# Resolver may return a trailing dot for FQDNs
		return hostname.strip().strip(".")
	except Exception:
		return None


def _shorten_hostname_for_display(hostname: str, max_length: int = HOSTNAME_DISPLAY_MAX_LENGTH) -> str:
	"""Shorten hostnames for UI/PDF display (keeps full hostname for POP inference)."""
	if not hostname:
		return hostname
	if len(hostname) <= max_length:
		return hostname
	return hostname[: max_length - len(HOSTNAME_TRUNCATION_SUFFIX)] + HOSTNAME_TRUNCATION_SUFFIX


def resolve_target_ip(host: str) -> str | None:
	"""Resolve target hostname to IP for destination matching (IPv4 or IPv6)."""
	if not host:
		return None
	
	# IP literal (IPv4 or IPv6) -> return as-is (normalized)
	try:
		return str(ipaddress.ip_address(host))
	except ValueError:
		pass
	
	# DNS resolve: follow system address selection order
	try:
		infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
		for info in infos:
			sockaddr = info[4]
			if sockaddr and sockaddr[0]:
				return sockaddr[0]
		return None
	except Exception:
		return None


def analyze_route(
	hops: list[dict[str, Any]],
	target_ip: str | None = None,
) -> dict[str, Any]:
	"""Analyze route for ICMP blocking patterns and destination reachability.

	Returns:
		Dict with:
		- destination_reached: bool
		- destination_ip: str or None
		- blocked_range: tuple(start_hop, end_hop) or None
		- last_responding_hop: int (last hop that responded before first timeout block)
		- needs_interpolation: bool
		- timeout_blocks: list of (start_hop, end_hop) tuples
	"""
	if not hops:
		return {
			"destination_reached": False,
			"destination_ip": target_ip,
			"blocked_range": None,
			"last_responding_hop": 0,
			"needs_interpolation": False,
		}

	# Check if destination was reached (at any position in hops)
	destination_reached = False
	destination_idx = -1
	if target_ip:
		for i, h in enumerate(hops):
			if h.get("ip") == target_ip and h.get("status") in (STATUS_OK, STATUS_PARTIAL):
				destination_reached = True
				destination_idx = i
				# First successful hit is sufficient – later hops irrelevant
				break

	# Find ALL timeout blocks between responding hops
	# A timeout block is a sequence of consecutive timeout hops
	timeout_blocks: list[tuple[int, int]] = []  # (start_hop, end_hop)
	last_responding_hop = 0
	last_responding_before_first_block = 0
	
	# Collect all timeout block ranges
	current_block_start = None
	for i, h in enumerate(hops):
		status = h.get("status", STATUS_TIMEOUT)
		hop_num = h.get("hop", i + 1)
		
		if status == STATUS_TIMEOUT:
			if current_block_start is None:
				current_block_start = hop_num
		else:
			# Responding hop (ok/partial)
			if current_block_start is not None:
				# Close the timeout block
				prev_hop_num = hops[i - 1]["hop"] if i > 0 else current_block_start
				timeout_blocks.append((current_block_start, prev_hop_num))
				current_block_start = None
			
			# Track last responding hop BEFORE first timeout block
			# (only update if we haven't seen any blocks yet)
			if not timeout_blocks:
				last_responding_before_first_block = hop_num
			# Always update the global last responding hop
			last_responding_hop = hop_num
	
	# Handle trailing timeout block (ends without a responding hop)
	if current_block_start is not None:
		last_hop_num = hops[-1].get("hop", len(hops)) if hops else current_block_start
		timeout_blocks.append((current_block_start, last_hop_num))

	# Determine if interpolation is needed:
	# - Destination must be reached
	# - At least one timeout block must exist between a responding hop and destination
	needs_interpolation = False
	blocked_start = None
	blocked_end = None
	
	if destination_reached and timeout_blocks:
		# Find the first timeout block that precedes the destination
		for block_start, block_end in timeout_blocks:
			if block_start < hops[destination_idx].get("hop", destination_idx + 1):
				needs_interpolation = True
				if blocked_start is None or block_start < blocked_start:
					blocked_start = block_start
				if blocked_end is None or block_end > blocked_end:
					blocked_end = block_end
	
	if needs_interpolation:
		_log.info(
			"ICMP blocking detected: %d timeout block(s), range %s-%s, interpolation required",
			len(timeout_blocks),
			blocked_start,
			blocked_end or "destination",
		)

	return {
		"destination_reached": destination_reached,
		"destination_ip": target_ip,
		"blocked_range": (blocked_start, blocked_end) if blocked_start else None,
		"last_responding_hop": last_responding_before_first_block if blocked_start else last_responding_hop,
		"needs_interpolation": needs_interpolation,
		"timeout_blocks": timeout_blocks,  # All detected timeout blocks
	}
