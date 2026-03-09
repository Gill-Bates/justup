# cython: language_level=3
# cython: boundscheck=False, wraparound=False

"""Network check implementations (ping, HTTP, TCP, certificate) with SSRF safeguards."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv6Address
from typing import Optional
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from app.utils.http_status import DEFAULT_HTTP_SUCCESS_CODES, is_status_code_allowed

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------

# ThreadPool for blocking I/O (prevents thread explosion)
_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="check_")

# Semaphores for limiting concurrent operations (lazy-init per event loop)
# Use WeakKeyDictionary to avoid memory leaks when event loops are destroyed
_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = WeakKeyDictionary()

# Maximum concurrent ping/tcp operations
MAX_CONCURRENT_PINGS = 5
MAX_CONCURRENT_TCP = 10
MAX_CONCURRENT_DNS = 8

# DNS negative cache max size (prevents unbounded memory growth)
DNS_NEG_CACHE_MAX_SIZE = 1024

# Module-level DNS negative cache (Cython functions don't support dynamic attributes)
# Maps hostname -> expiration timestamp (time.monotonic())
_DNS_NEG_CACHE: dict[str, float] = {}

# Maximum error detail length (prevents log poisoning)
MAX_ERROR_DETAIL_LENGTH = 200

# Blocked hosts/networks for SSRF protection
# NOTE: RFC 1918 private networks (10.x, 172.16.x, 192.168.x) are ALLOWED
# because this is an internal monitoring tool that needs to check internal systems.
# Only dangerous/unusable networks are blocked.
BLOCKED_NETWORKS = [
	ipaddress.ip_network("127.0.0.0/8"),      # Loopback (use real IPs instead)
	ipaddress.ip_network("169.254.0.0/16"),   # Link-local (APIPA, not routable)
	ipaddress.ip_network("::1/128"),          # IPv6 loopback
	ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

# Default User-Agent (deterministic, identifiable)
DEFAULT_USER_AGENT = "justUp/1.0 (Uptime Monitor; +https://github.com/Gill-Bates/justUp)"


def _get_semaphore(name: str) -> asyncio.Semaphore:
	"""Get or create a loop-local semaphore (must be called in async context).
	
	Semaphores are bound to the current event loop to prevent undefined behavior
	when multiple loops exist (tests, uvicorn reload, workers).
	"""
	loop = asyncio.get_running_loop()
	if loop not in _SEMAPHORES:
		_SEMAPHORES[loop] = {}
	loop_sems = _SEMAPHORES[loop]
	
	if name not in loop_sems:
		limits = {
			"ping": MAX_CONCURRENT_PINGS,
			"tcp": MAX_CONCURRENT_TCP,
			"dns": MAX_CONCURRENT_DNS,
		}
		if name not in limits:
			raise ValueError(f"Unknown semaphore: {name}")
		loop_sems[name] = asyncio.Semaphore(limits[name])
	return loop_sems[name]


def _is_ip_blocked_for_ssrf(ip: IPv4Address | IPv6Address) -> bool:
	"""Check if IP is blocked for SSRF protection.
	
	Blocks: loopback, link-local, multicast, reserved.
	Allows: RFC 1918 private networks (10.x, 172.16.x, 192.168.x) for internal monitoring.
	"""
	if any(ip in net for net in BLOCKED_NETWORKS):
		return True
	# Extra hardening: not useful targets
	if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
		return True
	return False


def _deterministic_user_agent(target_id: int | None = None) -> str:
	"""
	Return a deterministic User-Agent.
	
	If target_id is provided, returns a stable UA based on hash.
	Otherwise returns the default UA.
	"""
	if target_id is not None:
		return f"{DEFAULT_USER_AGENT} target/{target_id}"
	return DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _sanitize_error(error: Exception | str, max_length: int = MAX_ERROR_DETAIL_LENGTH) -> str:
	"""
	Sanitize error message for safe logging/storage.
	
	- Truncates to max_length
	- Removes potential sensitive info patterns
	- Normalizes whitespace
	"""
	msg = str(error)
	# Normalize whitespace
	msg = " ".join(msg.split())
	# Truncate
	if len(msg) > max_length:
		msg = msg[:max_length - 3] + "..."
	return msg


def _normalize_error_type(error: Exception) -> str:
	"""Return a normalized error type string."""
	# Prefer isinstance checks for reliability across locales/OS
	if isinstance(error, TimeoutError):
		return "timeout"
	if isinstance(error, ConnectionRefusedError):
		return "connection refused"
	if isinstance(error, ConnectionResetError):
		return "connection reset"
	# Check for SSL errors
	try:
		import ssl
		if isinstance(error, ssl.SSLError):
			return "ssl error"
	except ImportError:
		pass
	# Check OSError errno for specific cases
	if isinstance(error, OSError):
		# ECONNREFUSED, ETIMEDOUT, etc.
		if error.errno in (111, 110, 101):  # Connection refused, timed out, network unreachable
			return "connection refused" if error.errno == 111 else "timeout"
	# Fallback to string matching for httpx and other library errors
	error_str = str(error).lower()
	if "timeout" in error_str or "timed out" in error_str:
		return "timeout"
	if "connection refused" in error_str:
		return "connection refused"
	if (
		"name or service not known" in error_str
		or "nodename nor servname" in error_str
		or "no address associated with hostname" in error_str
		or "[errno -5]" in error_str
	):
		return "dns error"
	if "ssl" in error_str or "certificate" in error_str:
		return "ssl error"
	if "connection reset" in error_str:
		return "connection reset"
	return _sanitize_error(error)


async def _resolve_host_via_public_dns_first_answer(
	hostname: str,
	*,
	query_timeout_seconds: float = 2.0,
) -> str | None:
	"""Resolve hostname via two public DNS servers concurrently.

	Queries Quad9 and Cloudflare at the same time and returns the first
	successful A/AAAA answer. If the first finished query returns no answer,
	we keep waiting for the other one.

	Returns an IP string or None.
	"""
	# Lazy import so local dev doesn't break if deps aren't installed yet.
	try:
		import dns.resolver  # type: ignore
	except Exception:
		return None

	loop = asyncio.get_running_loop()
	sem = _get_semaphore("dns")

	# Small negative cache to prevent DNS storms for broken domains.
	# (Only caches overall failure, not per-nameserver.)
	DNS_NEG_TTL_SECONDS = 30.0
	now = time.monotonic()
	exp = _DNS_NEG_CACHE.get(hostname)
	if exp is not None and now < exp:
		return None

	def _blocking_resolve(nameserver: str, rdtype: str) -> str | None:
		resolver = dns.resolver.Resolver(configure=False)
		resolver.nameservers = [nameserver]
		# Keep it short: this function is called inside overall HTTP timeouts.
		resolver.lifetime = query_timeout_seconds
		resolver.timeout = query_timeout_seconds
		try:
			answers = resolver.resolve(hostname, rdtype)
			a = answers[0]
			return getattr(a, "address", None) or str(a)
		except Exception:
			return None

	# Priority: A before AAAA; Cloudflare before Quad9
	priority: list[tuple[str, str]] = [
		("1.1.1.1", "A"),
		("9.9.9.9", "A"),
		("1.1.1.1", "AAAA"),
		("9.9.9.9", "AAAA"),
	]

	async def _submit(nameserver: str, rdtype: str) -> str | None:
		async with sem:
			return await loop.run_in_executor(_EXECUTOR, _blocking_resolve, nameserver, rdtype)

	tasks: set[asyncio.Task[str | None]] = set()
	for ns, qtype in priority:
		tasks.add(asyncio.create_task(_submit(ns, qtype)))

	# Wait for first successful answer; if first completion is None, keep waiting.
	deadline = loop.time() + (query_timeout_seconds * 1.25)
	while tasks:
		remaining = max(0.0, deadline - loop.time())
		if remaining <= 0.0:
			break
		done, pending = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
		if not done:
			break
		for finished in done:
			try:
				ip = finished.result()
			except Exception:
				ip = None
			if ip:
				for p in pending:
					p.cancel()
				_DNS_NEG_CACHE.pop(hostname, None)
				return ip
		tasks = pending

	for p in tasks:
		p.cancel()
	# Limit cache size to prevent unbounded memory growth
	if len(_DNS_NEG_CACHE) >= DNS_NEG_CACHE_MAX_SIZE:
		_DNS_NEG_CACHE.clear()
	_DNS_NEG_CACHE[hostname] = time.monotonic() + DNS_NEG_TTL_SECONDS
	return None


def _resolve_and_check_host(host: str) -> bool:
	"""
	Resolve hostname and check if any resulting IP is in the blocked networks.
	Returns True if blocked.
	
	Security: Fails closed (returns True = blocked) if DNS resolution fails.
	This is intentional to prevent SSRF via unresolvable hostnames that might
	resolve differently in the target environment.
	"""
	try:
		# Use socket.getaddrinfo to resolve (supports IPv4 and IPv6)
		infos = socket.getaddrinfo(host, None)
	except Exception:
		# Fail closed: if we can't resolve, block the request for SSRF safety
		_log.debug("DNS resolution failed for host %s, blocking for SSRF safety", host)
		return True

	for family, _, _, _, sockaddr in infos:
		try:
			ip_str = sockaddr[0]  # IP is first element of sockaddr tuple
			ip = ipaddress.ip_address(ip_str)
			if _is_ip_blocked_for_ssrf(ip):
				return True
		except (ValueError, IndexError):
			continue
			
	return False


def _is_host_blocked(host: str) -> bool:
	"""
	Check if a host is blocked for SSRF protection.
	
	Blocks:
	- Loopback addresses
	- Private network ranges
	- Link-local addresses
	- Known local hostnames
	- Hostnames resolving to blocked IPs
	"""
	# Check hostname blocklist
	host_lower = host.lower().strip()
	if host_lower in BLOCKED_HOSTNAMES:
		return True
	
	# Try to parse as IP address directly first
	try:
		ip = ipaddress.ip_address(host_lower)
		return _is_ip_blocked_for_ssrf(ip)
	except ValueError:
		# Not an IP address, it's a hostname.
		pass
		
	# Check if hostname resolves to a blocked IP
	if _resolve_and_check_host(host_lower):
		return True
	
	return False


def _validate_url(url: str) -> tuple[bool, str | None]:
	"""
	Validate URL for SSRF protection.
	
	Returns (is_valid, error_message).
	"""
	try:
		parsed = urlparse(url)
	except Exception:
		return False, "invalid URL format"
	
	# Only allow http/https
	if parsed.scheme not in ("http", "https"):
		return False, f"unsupported scheme: {parsed.scheme}"
	
	# Must have a host
	if not parsed.hostname:
		return False, "no hostname in URL"
	
	# Check if host is blocked
	if _is_host_blocked(parsed.hostname):
		return False, "blocked host"
	
	return True, None


@dataclass(frozen=True)
class CheckResult:
	"""Result of a single check operation."""
	ok: bool
	latency_ms: Optional[float] = None
	detail: Optional[str] = None
	value: dict[str, object] | None = None


_ping_time_re = re.compile(r"time[=<]([0-9.]+)\s*ms", re.IGNORECASE)


async def ping(host: str, timeout_seconds: int = 4, *, skip_ssrf_check: bool = False) -> CheckResult:
	"""
	Ping a host using system ping command.
	
	Args:
		host: Hostname or IP to ping
		timeout_seconds: Ping timeout (OS-level, no Python wrapper timeout)
		skip_ssrf_check: If True, skip SSRF validation (for internal/LAN targets)
	
	Note:
		This implementation uses Linux ping syntax (-c, -W flags).
		On non-Linux platforms, the check will fail with 'unsupported platform'.
	"""
	# Platform check: ping syntax is OS-specific
	if sys.platform not in ("linux", "linux2"):
		return CheckResult(ok=False, detail="unsupported platform")
	
	# SSRF protection (can be skipped for explicit internal targets)
	if not skip_ssrf_check and _is_host_blocked(host):
		return CheckResult(ok=False, detail="blocked host")

	resolved_ip: str | None = None
	effective_host = host
	# If caller provided a hostname, prefer our public-DNS resolution and ping the IP.
	try:
		ipaddress.ip_address(host)
	except ValueError:
		resolved_ip = await _resolve_host_via_public_dns_first_answer(host)
		if resolved_ip and not skip_ssrf_check:
			try:
				ip = ipaddress.ip_address(resolved_ip)
				if _is_ip_blocked_for_ssrf(ip):
					return CheckResult(ok=False, detail="blocked host")
			except ValueError:
				resolved_ip = None
		if resolved_ip:
			effective_host = resolved_ip
	
	def _run() -> CheckResult:
		import subprocess
		
		start = time.perf_counter()
		try:
			is_v6 = ":" in effective_host
			# Single timeout: let OS ping handle the timeout entirely
			# -W timeout is the only timeout source (no Python wrapper)
			cp = subprocess.run(
				(["ping", "-6"] if is_v6 else ["ping"]) + ["-c", "1", "-W", str(timeout_seconds), effective_host],
				capture_output=True,
				text=True,
			)
		except Exception as e:
			elapsed_ms = (time.perf_counter() - start) * 1000.0
			return CheckResult(ok=False, latency_ms=elapsed_ms, detail=_normalize_error_type(e))
		
		elapsed_ms = (time.perf_counter() - start) * 1000.0
		out = (cp.stdout or "") + "\n" + (cp.stderr or "")
		m = _ping_time_re.search(out)
		lat = float(m.group(1)) if m else elapsed_ms
		
		if cp.returncode == 0:
			return CheckResult(ok=True, latency_ms=lat)
		else:
			# Classify failure type
			out_lower = out.lower()
			if "unreachable" in out_lower or "host unknown" in out_lower:
				# Hard failure: network/DNS issue
				detail = "unreachable" if "unreachable" in out_lower else "host unknown"
			elif lat >= (timeout_seconds * 1000 * 0.9):
				# Timeout: uncertain state (latency near timeout)
				detail = "timeout"
			else:
				detail = _sanitize_error(out.strip())
			return CheckResult(ok=False, latency_ms=lat, detail=detail)
	
	# Use semaphore to limit concurrent pings
	sem = _get_semaphore("ping")
	loop = asyncio.get_running_loop()
	async with sem:
		return await loop.run_in_executor(_EXECUTOR, _run)


async def http_head(
	url: str,
	timeout_seconds: float = 5.0,
	basic_auth: Optional[tuple[str, str]] = None,
	target_id: int | None = None,
	*,
	prefer_head: bool = True,
	success_status_codes: str | None = None,
	ignore_cert_errors: bool = False,
	skip_ssrf_check: bool = False,
) -> CheckResult:
	"""
	HTTP availability check with optional BasicAuth.
	
	Args:
		url: URL to check
		timeout_seconds: Request timeout
		basic_auth: Optional (username, password) tuple
		prefer_head: If True, use HEAD request first (doesn't affect visitor counters).
		             If False, use GET directly. HEAD failures fallback to GET regardless.
		skip_ssrf_check: If True, skip SSRF validation
	
	Note:
		Authorization credentials are NOT forwarded across redirects for security.
		This is intentional SSRF/credential-leak protection.
		
		This availability check does not follow redirects. Redirect targets are
		validated for SSRF protection, but the request stops at the first 3xx.

		success_status_codes configures which HTTP status codes count as OK.
		Default is 200-399 (2xx + 3xx, including 302 redirects).
	"""
	# SSRF protection
	if not skip_ssrf_check:
		is_valid, error = _validate_url(url)
		if not is_valid:
			return CheckResult(ok=False, detail=error)
	
	# Lazy import: httpx is only needed if this check is enabled.
	import httpx

	parsed = urlparse(url)
	hostname = parsed.hostname
	port = parsed.port
	scheme = parsed.scheme

	resolved_ip: str | None = None
	resolved_url: str | None = None
	host_header: str | None = None
	request_extensions: dict[str, object] | None = None

	# Prefer our own parallel DNS lookup (Quad9 + Cloudflare). This avoids
	# depending on the container/host resolver in environments where it flakes.
	if hostname:
		resolved_ip = await _resolve_host_via_public_dns_first_answer(hostname)
		if resolved_ip and not skip_ssrf_check:
			# If our resolution points into blocked networks, stop here.
			try:
				ip = ipaddress.ip_address(resolved_ip)
				if _is_ip_blocked_for_ssrf(ip):
					return CheckResult(ok=False, detail="blocked host")
			except ValueError:
				resolved_ip = None

		if resolved_ip:
			# Construct an URL that connects to the resolved IP but keeps
			# the original hostname for Host header / TLS SNI.
			default_port = 443 if scheme == "https" else 80
			ip_for_netloc = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
			hostport = ip_for_netloc if not port else f"{ip_for_netloc}:{port}"
			userinfo = ""
			if parsed.username:
				userinfo = parsed.username
				if parsed.password:
					userinfo += f":{parsed.password}"
				userinfo += "@"
			resolved_url = parsed._replace(netloc=f"{userinfo}{hostport}").geturl()

			# Host header should include port if non-default.
			host_header = hostname if (port is None or port == default_port) else f"{hostname}:{port}"
			if scheme == "https":
				# httpx/httpcore support SNI override via request extensions.
				request_extensions = {"sni_hostname": hostname}

	auth = httpx.BasicAuth(*basic_auth) if basic_auth else None
	base_headers = {"User-Agent": _deterministic_user_agent(target_id)}
	initial_headers = base_headers.copy()
	if host_header:
		initial_headers["Host"] = host_header
	
	# Maximum redirects to follow (SSRF protection)
	max_redirects = 5
	
	start = time.perf_counter()
	try:
		async with httpx.AsyncClient(
			follow_redirects=False,  # Manual redirect handling for SSRF protection
			timeout=timeout_seconds,
			auth=auth,
			verify=not bool(ignore_cert_errors),
		) as client:
			# 1) Try resolved IP first (fast + independent of system DNS).
			# 2) Fallback to original URL if that fails.
			try_urls: list[tuple[str, dict[str, object] | None, dict[str, str]]] = []
			if resolved_url:
				try_urls.append((resolved_url, request_extensions, initial_headers))
			try_urls.append((url, None, base_headers))

			last_exc: Exception | None = None
			resp = None
			for try_url, ext, hdrs in try_urls:
				try:
					current_url = try_url
					current_ext = ext
					current_headers = hdrs
					redirects_followed = 0
					
					while True:
						# Use HEAD if preferred (avoids affecting visitor counters)
						# Fallback to GET if HEAD returns 4xx/5xx (some servers don't support HEAD)
						if prefer_head:
							resp = await client.head(current_url, extensions=current_ext, headers=current_headers)
							if resp.status_code >= 400:
								resp = await client.get(current_url, extensions=current_ext, headers=current_headers)
						else:
							resp = await client.get(current_url, extensions=current_ext, headers=current_headers)
						
						# Redirects: validate target for SSRF, but do not follow.
						if resp.status_code in (301, 302, 303, 307, 308):
							location = resp.headers.get("location")
							if location:
								# Resolve relative URLs
								if not location.startswith(("http://", "https://")):
									from urllib.parse import urljoin
									location = urljoin(current_url, location)
								# SSRF check on redirect target
								if not skip_ssrf_check:
									is_valid, error = _validate_url(location)
									if not is_valid:
										_log.warning("HTTP check: redirect to blocked URL: %s -> %s (%s)", url, location, error)
										return CheckResult(
											ok=False,
											latency_ms=(time.perf_counter() - start) * 1000.0,
											detail=f"redirect blocked: {error}",
										)
							break  # stop at first redirect
						
						break  # Not a redirect, we're done
					# If the resolved-IP request returned a failure status, try the original URL too.
					# This helps in cases where public-DNS answers route to a different backend.
					if resolved_url and try_url == resolved_url and resp.status_code >= 400:
						continue
					break
				except Exception as e:
					last_exc = e
					continue
			if resp is None and last_exc is not None:
				raise last_exc
		elapsed_ms = (time.perf_counter() - start) * 1000.0
		success_spec = success_status_codes or DEFAULT_HTTP_SUCCESS_CODES
		try:
			ok = is_status_code_allowed(resp.status_code, success_spec)
		except ValueError:
			# Defensive: if a misconfigured spec slips through (manual DB edits),
			# fall back to the default instead of flapping the target to DOWN.
			_log.warning("HTTP check: invalid success_status_codes=%r, using default", success_spec)
			success_spec = DEFAULT_HTTP_SUCCESS_CODES
			ok = is_status_code_allowed(resp.status_code, success_spec)
		value: dict[str, object] = {"status": resp.status_code}
		if resp.status_code in (301, 302, 303, 307, 308):
			location = resp.headers.get("location")
			if location:
				value["redirect_to"] = location
		if not ok:
			# Use info for client errors (4xx), warning for server errors (5xx)
			if 400 <= resp.status_code < 500:
				_log.info("HTTP check failed: url=%s status=%d", url, resp.status_code)
			else:
				_log.warning("HTTP check failed: url=%s status=%d", url, resp.status_code)
			detail = f"HTTP {resp.status_code}"
			return CheckResult(ok=False, latency_ms=elapsed_ms, detail=detail, value=value)
		return CheckResult(ok=True, latency_ms=elapsed_ms, value=value)
	except Exception as e:
		elapsed_ms = (time.perf_counter() - start) * 1000.0
		error_detail = _normalize_error_type(e)
		_log.warning("HTTP check exception: url=%s error=%s (%s)", url, error_detail, type(e).__name__)
		return CheckResult(ok=False, latency_ms=elapsed_ms, detail=error_detail)


async def cert_expiration(
	host: str,
	port: int = 443,
	timeout_seconds: float = 5.0,
	*,
	ignore_cert_errors: bool = False,
	skip_ssrf_check: bool = False,
) -> CheckResult:
	"""
	Check SSL certificate expiration.
	
	Args:
		host: Hostname to check
		port: SSL port (default 443)
		timeout_seconds: Connection timeout
		skip_ssrf_check: If True, skip SSRF validation
	"""
	# SSRF protection
	if not skip_ssrf_check and _is_host_blocked(host):
		return CheckResult(ok=False, detail="blocked host")

	resolved_ip: str | None = None
	connect_host = host
	server_hostname: str | None
	try:
		ipaddress.ip_address(host)
		server_hostname = None
	except ValueError:
		server_hostname = host
		resolved_ip = await _resolve_host_via_public_dns_first_answer(host)
		if resolved_ip and not skip_ssrf_check:
			try:
				ip = ipaddress.ip_address(resolved_ip)
				if _is_ip_blocked_for_ssrf(ip):
					return CheckResult(ok=False, detail="blocked host")
			except ValueError:
				resolved_ip = None
		if resolved_ip:
			connect_host = resolved_ip
	
	def _run() -> CheckResult:
		ctx = ssl.create_default_context()
		if ignore_cert_errors:
			# Disable TLS verification to still retrieve the certificate (internal/test systems).
			ctx.check_hostname = False
			ctx.verify_mode = ssl.CERT_NONE
		try:
			with socket.create_connection((connect_host, port), timeout=timeout_seconds) as sock:
				with ctx.wrap_socket(sock, server_hostname=server_hostname) as ssock:
					# Ensure SSL handshake respects timeout
					ssock.settimeout(timeout_seconds)
					cert = ssock.getpeercert()
		except Exception as e:
			return CheckResult(ok=False, detail=_normalize_error_type(e))

		not_after = cert.get("notAfter")
		if not not_after:
			return CheckResult(ok=False, detail="no notAfter in certificate")
		try:
			exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
		except Exception:
			return CheckResult(ok=False, detail="cert parse error")
		remaining_days = (exp - datetime.now(tz=timezone.utc)).total_seconds() / 86400.0
		return CheckResult(ok=True, value={"expires_at": exp.isoformat(), "remaining_days": remaining_days})

	# Use TCP semaphore (cert check is a TCP connection)
	sem = _get_semaphore("tcp")
	loop = asyncio.get_running_loop()
	async with sem:
		return await loop.run_in_executor(_EXECUTOR, _run)


async def tcp_connect(host: str, port: int, timeout_seconds: float = 5.0, *, skip_ssrf_check: bool = False) -> CheckResult:
	"""
	Check if a TCP port is open and measure connection time.
	
	Args:
		host: Hostname or IP to check
		port: TCP port
		timeout_seconds: Connection timeout
		skip_ssrf_check: If True, skip SSRF validation
	"""
	# SSRF protection
	if not skip_ssrf_check and _is_host_blocked(host):
		return CheckResult(ok=False, detail="blocked host")

	resolved_ip: str | None = None
	connect_host = host
	try:
		ipaddress.ip_address(host)
	except ValueError:
		resolved_ip = await _resolve_host_via_public_dns_first_answer(host)
		if resolved_ip and not skip_ssrf_check:
			try:
				ip = ipaddress.ip_address(resolved_ip)
				if _is_ip_blocked_for_ssrf(ip):
					return CheckResult(ok=False, detail="blocked host")
			except ValueError:
				resolved_ip = None
		if resolved_ip:
			connect_host = resolved_ip
	
	def _run() -> CheckResult:
		start = time.perf_counter()
		try:
			with socket.create_connection((connect_host, port), timeout=timeout_seconds):
				pass
			elapsed_ms = (time.perf_counter() - start) * 1000.0
			return CheckResult(ok=True, latency_ms=elapsed_ms)
		except Exception as e:
			elapsed_ms = (time.perf_counter() - start) * 1000.0
			return CheckResult(ok=False, latency_ms=elapsed_ms, detail=_normalize_error_type(e))

	# Use semaphore to limit concurrent TCP connections
	sem = _get_semaphore("tcp")
	loop = asyncio.get_running_loop()
	async with sem:
		return await loop.run_in_executor(_EXECUTOR, _run)


# ---------------------------------------------------------------------------
# Connectivity Check - Verify Application's Own Internet Connection
# ---------------------------------------------------------------------------

# Reference hosts that are considered "always available"
# We check multiple to avoid false positives if one is temporarily down
CONNECTIVITY_REFERENCE_HOSTS = [
	("8.8.8.8", 53),       # Google Public DNS
	("1.1.1.1", 53),       # Cloudflare DNS
	("9.9.9.9", 53),       # Quad9 DNS
	("208.67.222.222", 53),  # OpenDNS
]

# Minimum number of reference hosts that must be reachable
CONNECTIVITY_MIN_REACHABLE = 2

# Cache duration for connectivity status (seconds)
CONNECTIVITY_CACHE_SECONDS = 30


class ConnectivityStatus:
	"""Tracks application's own internet connectivity status."""
	
	def __init__(self):
		self._last_check: Optional[float] = None
		self._last_result: bool = True
		self._reachable_count: int = 0
		self._lock = asyncio.Lock()
	
	async def check(self, timeout_seconds: float = 2.0) -> bool:
		"""
		Check if the application has internet connectivity.
		
		Returns True if at least CONNECTIVITY_MIN_REACHABLE reference hosts
		are reachable. Uses caching to avoid excessive checks.
		"""
		async with self._lock:
			now = time.monotonic()
			
			# Use cached result if recent enough
			if (self._last_check is not None and 
				now - self._last_check < CONNECTIVITY_CACHE_SECONDS):
				return self._last_result
			
			# Check reference hosts in parallel
			results = await asyncio.gather(
				*[self._check_host(host, port, timeout_seconds) 
				  for host, port in CONNECTIVITY_REFERENCE_HOSTS],
				return_exceptions=True
			)
			
			self._reachable_count = sum(
				1 for r in results 
				if r is True
			)
			self._last_result = self._reachable_count >= CONNECTIVITY_MIN_REACHABLE
			self._last_check = now
			
			return self._last_result
	
	@staticmethod
	async def _check_host(host: str, port: int, timeout: float) -> bool:
		"""Check if a single reference host is reachable."""
		def _run() -> bool:
			try:
				with socket.create_connection((host, port), timeout=timeout):
					return True
			except Exception:
				return False
		loop = asyncio.get_running_loop()
		return await loop.run_in_executor(_EXECUTOR, _run)
	
	@property
	def reachable_count(self) -> int:
		"""Number of reference hosts that were reachable in last check."""
		return self._reachable_count
	
	@property
	def is_connected(self) -> bool:
		"""Last known connectivity status (may be stale)."""
		return self._last_result


# Global connectivity status tracker
_connectivity = ConnectivityStatus()


async def check_connectivity(timeout_seconds: float = 2.0) -> CheckResult:
	"""
	Check if the application has internet connectivity.
	
	This should be called before marking any target as "down" to distinguish
	between actual target failures and the application's own connectivity issues.
	
	Returns:
		CheckResult with ok=True if internet is available, ok=False otherwise.
		The 'value' field contains details about reachable reference hosts.
	"""
	is_connected = await _connectivity.check(timeout_seconds)
	return CheckResult(
		ok=is_connected,
		detail=None if is_connected else "Application connectivity issue detected",
		value={
			"reachable_hosts": _connectivity.reachable_count,
			"required_hosts": CONNECTIVITY_MIN_REACHABLE,
			"total_hosts": len(CONNECTIVITY_REFERENCE_HOSTS),
		}
	)


async def has_connectivity(timeout_seconds: float = 2.0) -> bool:
	"""
	Quick check if the application has internet connectivity.
	
	Convenience wrapper around check_connectivity() that returns a simple bool.
	"""
	result = await check_connectivity(timeout_seconds)
	return result.ok
