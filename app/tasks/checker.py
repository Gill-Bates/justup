#!/usr/bin/env python3
#
# app/tasks/checker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Uptime check task: runs on a schedule, probes each monitor,
writes metrics to TSDB, and updates SQLite status/incidents."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import socket
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from ..db.sqlite_incidents import create_incident, get_open_incident, resolve_incident
from ..db.sqlite_monitors import get_active_monitors, update_monitor_status
from ..db.sqlite_runtime import connect
from ..db.sqlite_settings import get_setting
from ..db.tsdb import append as tsdb_append
from ..utils.config import Config
from ..utils.scheduler import Scheduler
from ..utils.time import utcnow

_log = logging.getLogger(__name__)

_JOB_NAME = "uptime_checker"
_DEFAULT_CHECK_INTERVAL = 60
_MAX_CONCURRENT_CHECKS = 20

# Hostname validation: RFC 952/1123 compliant
_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,251}[a-zA-Z0-9])?$')


def _validate_hostname(value: str) -> str:
	"""Validate hostname to prevent command injection and ensure valid DNS names."""
	value = value.strip()
	if not value:
		raise ValueError("Hostname is required")
	if len(value) > 253:
		raise ValueError(f"Hostname too long: {len(value)} chars")
	if not _HOSTNAME_RE.match(value):
		raise ValueError(f"Invalid hostname format: {value!r}")
	return value


async def start_checker(scheduler: Scheduler, cfg: Config) -> None:
	conn = connect(cfg.db_path)
	try:
		interval_str = get_setting(conn, "check_interval_default")
		interval = int(interval_str) if interval_str else _DEFAULT_CHECK_INTERVAL
	finally:
		conn.close()

	async def _run():
		await run_checks(cfg)

	scheduler.add(_JOB_NAME, interval, _run)
	_log.info("Uptime checker scheduled every %ds", interval)


async def stop_checker(scheduler: Scheduler) -> None:
	"""Stop the uptime checker (scheduler handles cleanup)."""
	_log.info("Stopping uptime checker")


async def run_checks(cfg: Config) -> None:
	"""Run checks for all active monitors with concurrency limit and shared DB connection."""
	conn = connect(cfg.db_path)
	try:
		monitors = get_active_monitors(conn)
		if not monitors:
			return

		# Concurrency limit to prevent resource exhaustion
		sem = asyncio.Semaphore(_MAX_CONCURRENT_CHECKS)

		async def _bounded_check(monitor: dict) -> tuple[dict, CheckResult | Exception]:
			async with sem:
				try:
					result = await _probe_monitor(dict(monitor))
					return (monitor, result)
				except Exception as e:
					_log.error("Check failed for monitor %d: %s", monitor["id"], e)
					return (monitor, CheckResult(is_up=False, response_time_ms=0, error=str(e)))

		results = await asyncio.gather(
			*[_bounded_check(dict(m)) for m in monitors],
			return_exceptions=True,
		)

		# Persist all results using shared connection
		for item in results:
			if isinstance(item, Exception):
				_log.error("Unhandled gather exception: %s", item)
				continue
			monitor, result = item
			_persist_result(cfg, conn, monitor, result)
	finally:
		conn.close()


async def _probe_monitor(monitor: dict[str, Any]) -> CheckResult:
	"""Probe a single monitor and return the result (no DB I/O)."""
	monitor_type = monitor["monitor_type"]
	if monitor_type == "http" or monitor_type == "keyword":
		return await _check_http(monitor)
	elif monitor_type == "tcp":
		return await _check_tcp(monitor)
	elif monitor_type == "ping":
		return await _check_ping(monitor)
	elif monitor_type == "dns":
		return await _check_dns(monitor)
	else:
		_log.warning("Unknown monitor type %s for monitor %d", monitor_type, monitor["id"])
		return CheckResult(is_up=False, response_time_ms=0, error=f"Unknown type: {monitor_type}")


def _persist_result(cfg: Config, conn, monitor: dict, result: CheckResult) -> None:
	"""Write result to TSDB and update SQLite status/incidents (sync, uses shared connection)."""
	monitor_id = monitor["id"]

	# Write to TSDB - individual metrics
	ts = utcnow()
	tsdb_append(cfg.tsdb_dir, monitor_id, "response_time", result.response_time_ms, ts)
	tsdb_append(cfg.tsdb_dir, monitor_id, "is_up", 1 if result.is_up else 0, ts)
	if result.status_code:
		tsdb_append(cfg.tsdb_dir, monitor_id, "status_code", result.status_code, ts)

	# Update SQLite status
	status_str = "up" if result.is_up else "down"
	update_monitor_status(
		conn, monitor_id,
		status=status_str,
		response_time_ms=result.response_time_ms,
		status_code=result.status_code,
		error=result.error,
		cert_expiry_at=result.cert_expires_at,
		cert_issuer=result.cert_issuer,
	)

	# Incident management
	if not result.is_up:
		open_incident = get_open_incident(conn, monitor_id)
		if not open_incident:
			create_incident(conn, monitor_id, error_message=result.error or "Monitor down")
	else:
		open_incident = get_open_incident(conn, monitor_id)
		if open_incident:
			resolve_incident(conn, open_incident["id"])


@dataclass(slots=True)
class CheckResult:
	"""Result of a monitor check probe."""
	is_up: bool
	response_time_ms: float
	status_code: int = 0
	error: str | None = None
	cert_expires_at: str | None = None
	cert_issuer: str | None = None


@asynccontextmanager
async def _timed_check():
	"""Context manager that times execution and catches common exceptions."""
	start = time.monotonic()
	result = {"value": None}
	try:
		yield result
	except asyncio.TimeoutError:
		elapsed_ms = (time.monotonic() - start) * 1000
		result["value"] = CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Timeout")
	except Exception as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		result["value"] = CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=str(e))


def _fetch_cert_sync(hostname: str, port: int) -> dict | None:
	"""Synchronously fetch SSL certificate info (runs in thread pool)."""
	try:
		ctx = ssl.create_default_context()
		with ctx.wrap_socket(socket.socket(), server_hostname=hostname) as s:
			s.settimeout(5)
			s.connect((hostname, port))
			return s.getpeercert()
	except Exception as e:
		_log.debug("Failed to fetch cert for %s:%d: %s", hostname, port, e)
		return None


async def _check_http(monitor: dict[str, Any]) -> CheckResult:
	url = monitor.get("url", "")
	method = (monitor.get("method") or "GET").upper()
	timeout_seconds = monitor.get("timeout_seconds") or 30
	verify_ssl = monitor.get("verify_ssl", True)
	follow_redirects = monitor.get("follow_redirects", True)
	max_redirects = monitor.get("max_redirects") or 10
	expected_status = monitor.get("expected_status_code") or 200
	keyword = monitor.get("keyword")
	headers_json = monitor.get("headers_json")
	body = monitor.get("body")

	headers = {}
	if headers_json:
		try:
			headers = json.loads(headers_json)
		except (json.JSONDecodeError, TypeError):
			pass

	cert_expires_at = None
	cert_issuer = None

	async with _timed_check() as ctx:
		async with httpx.AsyncClient(
			verify=verify_ssl,
			follow_redirects=follow_redirects,
			max_redirects=max_redirects,
			timeout=timeout_seconds,
		) as client:
			start = time.monotonic()
			resp = await client.request(method, url, headers=headers, content=body)
			elapsed_ms = (time.monotonic() - start) * 1000

		status_code = resp.status_code

		# SSL certificate info (async-safe, offloaded to thread pool)
		if url.startswith("https://"):
			parsed = urlparse(url)
			hostname = parsed.hostname or ""
			port = parsed.port or 443
			loop = asyncio.get_running_loop()
			cert = await loop.run_in_executor(
				None, functools.partial(_fetch_cert_sync, hostname, port)
			)
			if cert:
				not_after = cert.get("notAfter", "")
				if not_after:
					cert_expires_at = not_after
				issuer = cert.get("issuer", ())
				for rdn in issuer:
					for attr_type, attr_value in rdn:
						if attr_type == "organizationName":
							cert_issuer = attr_value
							break

		# Determine success
		is_up = status_code == expected_status
		error = None

		if not is_up:
			error = f"Expected status {expected_status}, got {status_code}"

		# Keyword check
		if keyword and is_up:
			if keyword not in resp.text:
				is_up = False
				error = f'Keyword "{keyword}" not found in response'

		ctx["value"] = CheckResult(
			is_up=is_up, response_time_ms=round(elapsed_ms, 2),
			status_code=status_code, error=error,
			cert_expires_at=cert_expires_at, cert_issuer=cert_issuer,
		)

	return ctx["value"]


async def _check_tcp(monitor: dict[str, Any]) -> CheckResult:
	hostname = _validate_hostname(monitor.get("hostname", ""))
	port = monitor.get("port") or 80
	timeout_seconds = monitor.get("timeout_seconds") or 10

	async with _timed_check() as ctx:
		start = time.monotonic()
		_, writer = await asyncio.wait_for(
			asyncio.open_connection(hostname, port),
			timeout=timeout_seconds,
		)
		elapsed_ms = (time.monotonic() - start) * 1000
		writer.close()
		await writer.wait_closed()
		ctx["value"] = CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))

	return ctx["value"]


async def _check_ping(monitor: dict[str, Any]) -> CheckResult:
	"""ICMP ping check. Note: Uses Linux-specific 'ping -c 1 -W <timeout>' syntax."""
	hostname = _validate_hostname(monitor.get("hostname", ""))
	timeout_seconds = monitor.get("timeout_seconds") or 10

	async with _timed_check() as ctx:
		start = time.monotonic()
		proc = await asyncio.create_subprocess_exec(
			"ping", "-c", "1", "-W", str(timeout_seconds), hostname,
			stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
		)
		stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds + 5)
		elapsed_ms = (time.monotonic() - start) * 1000

		if proc.returncode == 0:
			# Parse ping time from output
			output = stdout.decode("utf-8", errors="replace")
			for line in output.splitlines():
				if "time=" in line:
					try:
						time_part = line.split("time=")[1].split()[0]
						elapsed_ms = float(time_part.replace("ms", ""))
					except (IndexError, ValueError):
						pass
					break
			ctx["value"] = CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))
		else:
			ctx["value"] = CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Ping failed")

	return ctx["value"]


async def _check_dns(monitor: dict[str, Any]) -> CheckResult:
	hostname = _validate_hostname(monitor.get("hostname", ""))
	timeout_seconds = monitor.get("timeout_seconds") or 10

	async with _timed_check() as ctx:
		start = time.monotonic()
		loop = asyncio.get_running_loop()
		await asyncio.wait_for(
			loop.getaddrinfo(hostname, None),
			timeout=timeout_seconds,
		)
		elapsed_ms = (time.monotonic() - start) * 1000
		ctx["value"] = CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))

	# Custom error messages for DNS-specific failures
	if ctx["value"] and ctx["value"].error:
		if "gaierror" in str(ctx["value"].error):
			ctx["value"] = CheckResult(
				is_up=False,
				response_time_ms=ctx["value"].response_time_ms,
				error=f"DNS resolution failed: {ctx['value'].error}"
			)

	return ctx["value"]
