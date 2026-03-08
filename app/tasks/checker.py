#!/usr/bin/env python3
#
# app/tasks/checker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Uptime check task: runs on a schedule, probes each monitor,
writes metrics to TSDB, and updates SQLite status/incidents."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any

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


async def start_checker(scheduler: Scheduler, cfg: Config) -> None:
	conn = connect(cfg.db_path)
	try:
		interval_str = get_setting(conn, "check_interval_default")
		interval = int(interval_str) if interval_str else _DEFAULT_CHECK_INTERVAL
	finally:
		conn.close()

	async def _run():
		await run_checks(cfg)

	scheduler.add_job(_JOB_NAME, _run, interval_seconds=interval)
	_log.info("Uptime checker scheduled every %ds", interval)


async def stop_checker(scheduler: Scheduler) -> None:
	scheduler.remove_job(_JOB_NAME)


async def run_checks(cfg: Config) -> None:
	conn = connect(cfg.db_path)
	try:
		monitors = get_active_monitors(conn)
	finally:
		conn.close()

	if not monitors:
		return

	tasks = [_check_monitor(cfg, dict(m)) for m in monitors]
	await asyncio.gather(*tasks, return_exceptions=True)


async def _check_monitor(cfg: Config, monitor: dict[str, Any]) -> None:
	monitor_id = monitor["id"]
	monitor_type = monitor["monitor_type"]
	try:
		if monitor_type == "http" or monitor_type == "keyword":
			result = await _check_http(monitor)
		elif monitor_type == "tcp":
			result = await _check_tcp(monitor)
		elif monitor_type == "ping":
			result = await _check_ping(monitor)
		elif monitor_type == "dns":
			result = await _check_dns(monitor)
		else:
			_log.warning("Unknown monitor type %s for monitor %d", monitor_type, monitor_id)
			return
	except Exception as e:
		_log.error("Check failed for monitor %d: %s", monitor_id, e)
		result = CheckResult(is_up=False, response_time_ms=0, status_code=0, error=str(e))

	# Write to TSDB
	ts = utcnow()
	tsdb_append(cfg.tsdb_dir, str(monitor_id), {
		"ts": ts.isoformat(),
		"is_up": result.is_up,
		"response_time_ms": result.response_time_ms,
		"status_code": result.status_code,
		"error": result.error,
	})

	# Update SQLite status
	conn = connect(cfg.db_path)
	try:
		update_monitor_status(
			conn, monitor_id,
			is_up=result.is_up,
			response_time_ms=result.response_time_ms,
			status_code=result.status_code,
			error_message=result.error,
			cert_expires_at=result.cert_expires_at,
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
	finally:
		conn.close()


class CheckResult:
	__slots__ = ("is_up", "response_time_ms", "status_code", "error", "cert_expires_at", "cert_issuer")

	def __init__(
		self,
		is_up: bool,
		response_time_ms: float,
		status_code: int = 0,
		error: str | None = None,
		cert_expires_at: str | None = None,
		cert_issuer: str | None = None,
	):
		self.is_up = is_up
		self.response_time_ms = response_time_ms
		self.status_code = status_code
		self.error = error
		self.cert_expires_at = cert_expires_at
		self.cert_issuer = cert_issuer


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

	start = time.monotonic()
	try:
		async with httpx.AsyncClient(
			verify=verify_ssl,
			follow_redirects=follow_redirects,
			max_redirects=max_redirects,
			timeout=timeout_seconds,
		) as client:
			resp = await client.request(method, url, headers=headers, content=body)
			elapsed_ms = (time.monotonic() - start) * 1000

		status_code = resp.status_code

		# SSL certificate info
		if url.startswith("https://"):
			try:
				from urllib.parse import urlparse
				parsed = urlparse(url)
				hostname = parsed.hostname or ""
				port = parsed.port or 443
				ctx = ssl.create_default_context()
				with ctx.wrap_socket(socket.socket(), server_hostname=hostname) as s:
					s.settimeout(5)
					s.connect((hostname, port))
					cert = s.getpeercert()
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
			except Exception:
				pass

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

		return CheckResult(
			is_up=is_up, response_time_ms=round(elapsed_ms, 2),
			status_code=status_code, error=error,
			cert_expires_at=cert_expires_at, cert_issuer=cert_issuer,
		)

	except httpx.TimeoutException:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Timeout")
	except Exception as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=str(e))


async def _check_tcp(monitor: dict[str, Any]) -> CheckResult:
	hostname = monitor.get("hostname", "")
	port = monitor.get("port") or 80
	timeout_seconds = monitor.get("timeout_seconds") or 10

	start = time.monotonic()
	try:
		_, writer = await asyncio.wait_for(
			asyncio.open_connection(hostname, port),
			timeout=timeout_seconds,
		)
		elapsed_ms = (time.monotonic() - start) * 1000
		writer.close()
		await writer.wait_closed()
		return CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))
	except asyncio.TimeoutError:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Timeout")
	except Exception as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=str(e))


async def _check_ping(monitor: dict[str, Any]) -> CheckResult:
	hostname = monitor.get("hostname", "")
	timeout_seconds = monitor.get("timeout_seconds") or 10

	start = time.monotonic()
	try:
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
			return CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))
		else:
			return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Ping failed")
	except asyncio.TimeoutError:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="Timeout")
	except Exception as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=str(e))


async def _check_dns(monitor: dict[str, Any]) -> CheckResult:
	hostname = monitor.get("hostname", "")
	timeout_seconds = monitor.get("timeout_seconds") or 10

	start = time.monotonic()
	try:
		loop = asyncio.get_event_loop()
		await asyncio.wait_for(
			loop.getaddrinfo(hostname, None),
			timeout=timeout_seconds,
		)
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=True, response_time_ms=round(elapsed_ms, 2))
	except asyncio.TimeoutError:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error="DNS timeout")
	except socket.gaierror as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=f"DNS resolution failed: {e}")
	except Exception as e:
		elapsed_ms = (time.monotonic() - start) * 1000
		return CheckResult(is_up=False, response_time_ms=round(elapsed_ms, 2), error=str(e))
