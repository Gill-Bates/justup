#!/usr/bin/env python3
#
# app/utils/whatweb_parser.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
WhatWeb JSON output parser for technology stack detection.

WhatWeb writes JSON Lines format (one JSON object per line).
Multiple entries may exist due to redirects.
This module parses and normalizes the output to a clean tech stack.

Optimizations:
- Uses stdout instead of temp files (--log-json=-)
- Centralized plugin value extraction
- Prioritized CMS detection matrix
- Extended security analysis
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
from typing import Any
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CMS Detection Priority
# ---------------------------------------------------------------------------
# CMS Detection Priority: checked in this order, first match wins.
# Order rationale: Prefer CMS-specific plugins over generic MetaGenerator.
# Within specific plugins, order is arbitrary (typically only one matches).

CMS_PRIORITY = [
    "WordPress",
    "Drupal",
    "Joomla",
    "TYPO3",
    "Magento",
    "Shopify",
    "PrestaShop",
    "Contao",
    "MODX",
    "Craft CMS",
]

# WhatWeb exposes headers as *plugins* with specific names.
# Debug/dev leaks are detected by matching those plugin names (case-insensitive).
DEBUG_PLUGIN_KEYS = tuple(k.lower() for k in (
    "X-Typo3-Cms",
    "X-Debug",
    "X-PHP-Version",
    "X-AspNet-Version",
    "X-Powered-By-Plesk",
))

# Precise pattern to detect debug/dev info in header VALUES
# Avoids false positives from standard tracing headers (X-Trace-ID, etc.)
_DEBUG_HEADER_RE = re.compile(
    r'\b(?:x-debug(?:-token|-id)?|xdebug|debug-mode|staging-mode|test-mode)\b',
    re.IGNORECASE,
)

# Trace-ID headers are NOT security issues – exclude these explicitly
_TRACE_EXCLUSIONS = re.compile(
    r'\b(?:trace-id|traceparent|tracestate|x-trace-id|x-request-id)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Plugin Value Extraction (centralized)
# ---------------------------------------------------------------------------

def _stringify(x: Any) -> str:
    """Convert plugin value to string, handling nested dicts."""
    if isinstance(x, dict):
        # Extract version or string from nested dict
        return str(x.get("version") or x.get("string") or next(iter(x.values()), x))
    return str(x)


def _extract(plugins: dict, plugin: str) -> list[str]:
    """
    Extract all values from a WhatWeb plugin entry.
    
    WhatWeb plugin values can be:
    - dict with 'string' or 'version' keys
    - list of values
    - single string
    
    Returns:
        List of string values (may be empty)
    """
    val = plugins.get(plugin)
    if val is None:
        return []
    
    if isinstance(val, dict):
        # Prefer 'version' over 'string' for better semantic value
        for key in ("version", "string"):
            if key in val:
                v = val[key]
                if isinstance(v, list):
                    return [_stringify(x) for x in v if x]
                return [str(v)] if v else []
        # No semantic keys found → return empty list instead of implicit extraction
        _log.debug("Plugin '%s' has dict without version/string keys: %s", plugin, list(val.keys()))
        return []
    
    if isinstance(val, list):
        return [_stringify(x) for x in val if x]
    
    if isinstance(val, (str, int, float)):
        return [str(val)]
    
    return []


def _get_first(plugins: dict, plugin: str) -> str | None:
    """Extract first value from a plugin entry, or None."""
    values = _extract(plugins, plugin)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# SSRF Protection
# ---------------------------------------------------------------------------

# Private IP ranges (SSRF protection)
_PRIVATE_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),   # Loopback
    ipaddress.ip_network("10.0.0.0/8"),    # Private A
    ipaddress.ip_network("172.16.0.0/12"), # Private B
    ipaddress.ip_network("192.168.0.0/16"),# Private C
    ipaddress.ip_network("169.254.0.0/16"),# Link-local
    ipaddress.ip_network("::1/128"),       # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),      # IPv6 private
    ipaddress.ip_network("fe80::/10"),     # IPv6 link-local
]

# Hostnames that are always private/internal
_PRIVATE_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "metadata.google.internal",  # GCP metadata
    "169.254.169.254",            # AWS/Azure metadata
})


def _is_private_target(url: str) -> bool:
    """Check if URL targets private/internal networks (SSRF protection with DNS resolution)."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        
        if not hostname:
            return True  # No hostname → block
        
        # Check against known private hostnames
        if hostname.lower() in _PRIVATE_HOSTNAMES:
            return True
        
        # Try to parse as IP address
        try:
            ip = ipaddress.ip_address(hostname)
            # Check if IP is in any private range
            return any(ip in net for net in _PRIVATE_RANGES)
        except ValueError:
            pass
        
        # DNS resolution: check all resolved IPs (prevents DNS rebinding attacks)
        try:
            addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in addrinfos:
                resolved_ip = ipaddress.ip_address(sockaddr[0])
                if any(resolved_ip in net for net in _PRIVATE_RANGES):
                    _log.warning(
                        "SSRF blocked: %s resolves to private IP %s",
                        hostname, resolved_ip
                    )
                    return True
        except socket.gaierror:
            # DNS resolution failed → conservatively block
            _log.warning("SSRF check: DNS resolution failed for %s", hostname)
            return True
        
        return False
    except Exception:
        # On any parsing error, be conservative and block
        return True


# ---------------------------------------------------------------------------
# Main Functions
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _find_whatweb_binary() -> str | None:
    """Find whatweb binary in PATH (cached to avoid repeated lookups)."""
    return shutil.which("whatweb")


def run_whatweb(url: str, aggression: int = 3, timeout: int = 30) -> dict[str, Any]:
    """
    Run whatweb against a URL and return parsed tech stack.
    
    Uses stdout directly (--log-json=-) to avoid temp file overhead.
    
    Args:
        url: Target URL to scan (must be http:// or https://)
        aggression: WhatWeb aggression level (1-4, default 3)
        timeout: Command timeout in seconds (5-120)
        
    Returns:
        Parsed tech stack dict, or empty dict on failure
        
    Raises:
        ValueError: If inputs are invalid or URL targets private network
        TypeError: If url is not a string
    """
    # Strict input validation (security: prevent command injection)
    if not isinstance(aggression, int) or not 1 <= aggression <= 4:
        raise ValueError(f"aggression must be int 1-4, got {aggression!r}")
    
    if not isinstance(timeout, int) or not 5 <= timeout <= 120:
        raise ValueError(f"timeout must be int 5-120, got {timeout!r}")
    
    if not isinstance(url, str):
        raise TypeError(f"url must be str, got {type(url).__name__}")
    
    # Null-bytes and newlines can break command parsing
    if '\x00' in url or '\n' in url or '\r' in url:
        raise ValueError("URL contains invalid characters")
    
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid URL scheme: {url}")
    
    # URL length limit (prevent abuse)
    if len(url) > 2048:
        raise ValueError("URL too long (max 2048 chars)")
    
    # SSRF protection with DNS resolution
    if _is_private_target(url):
        raise ValueError(f"URL targets private/internal network (SSRF protection): {url}")
    
    # Find whatweb binary (cached)
    whatweb_bin = _find_whatweb_binary()
    if not whatweb_bin:
        _log.warning("WhatWeb scan failed for %s: whatweb binary not found in PATH. Install with: apt-get install whatweb", url)
        return {}
    
    cmd = [
        whatweb_bin,
        f"-a{aggression}",
        "--no-errors",
        "--log-json=-",  # Output to stdout
        "--",            # End of options, prevents URL weirdness
        url,
    ]
    
    _log.debug("Running whatweb: %s", " ".join(cmd))
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # Don't raise on non-zero exit
        )
        
        if result.returncode != 0:
            stderr_preview = result.stderr[:200] if result.stderr else "(no stderr)"
            _log.warning(
                "WhatWeb scan failed for %s: process exited with code %d. Error: %s",
                url, result.returncode, stderr_preview
            )
        
        # Parse stdout directly
        parsed = parse_whatweb_output(result.stdout)
        
        if not parsed or not _is_meaningful(parsed):
            _log.warning(
                "WhatWeb scan returned no meaningful data for %s (exit code: %d, stdout lines: %d, stderr: %s)",
                url,
                result.returncode,
                len(result.stdout.splitlines()) if result.stdout else 0,
                "present" if result.stderr else "empty"
            )
        
        return parsed
        
    except subprocess.TimeoutExpired:
        _log.warning(
            "WhatWeb scan failed for %s: timeout after %ds. Target may be unreachable or too slow.",
            url, timeout
        )
        return {}
    except PermissionError as e:
        _log.warning(
            "WhatWeb scan failed for %s: permission denied. Error: %s",
            url, e
        )
        return {}
    except OSError as e:
        _log.warning(
            "WhatWeb scan failed for %s: OS error (possibly network/DNS issue). Error: %s",
            url, e
        )
        return {}
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        _log.warning(
            "WhatWeb scan failed for %s: %s: %s",
            url, type(e).__name__, e
        )
        return {}


def parse_whatweb_output(output: str) -> dict[str, Any]:
    """
    Parse whatweb JSON Lines output from string.
    
    Args:
        output: Raw stdout from whatweb (JSON Lines format)
        
    Returns:
        Normalized tech stack dict
    """
    entries = []
    
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Only try to parse lines that look like JSON objects
        if not line.startswith("{"):
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            _log.debug("Skipping malformed JSON line: %s", e)
            continue
    
    if not entries:
        _log.warning("WhatWeb output parsing failed: no valid JSON entries found in output")
        return {}
    
    return _normalize_stack(entries)


def parse_whatweb_json(path: str) -> dict[str, Any]:
    """
    Parse whatweb JSON output from file (legacy compatibility).
    
    Args:
        path: Path to whatweb JSON output file
        
    Returns:
        Normalized tech stack dict
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_whatweb_output(f.read())
    except FileNotFoundError:
        _log.warning("whatweb output file not found: %s", path)
        return {}
    except Exception as e:
        _log.warning("Failed to read whatweb output: %s", e)
        return {}


def _is_meaningful(result: dict[str, Any]) -> bool:
    """Check if parsed result contains any actual data."""
    return bool(
        result.get("webserver")
        or result.get("cms")
        or result.get("title")
        or result.get("programming_language")
    )


def _normalize_stack(entries: list[dict]) -> dict[str, Any]:
    """
    Normalize whatweb entries to a clean tech stack dict.
    
    Prefers the last 2xx (successful) response with plugins.
    Redirects (3xx) often show generic titles like "301 Moved Permanently".
    """
    # Prefer the last 2xx entry with plugins, fallback to any last entry with plugins, fallback to last entry
    final = (
        next((e for e in reversed(entries) if 200 <= e.get("http_status", 0) < 300 and e.get("plugins")), None)
        or next((e for e in reversed(entries) if e.get("plugins")), None)
        or entries[-1]
    )
    
    plugins = final.get("plugins", {})
    target_url = final.get("target", "")

    # Build HTTP info with redirect chain
    http_info = _extract_http_info(final)
    # Add chain of all status codes for debugging/transparency
    http_info["chain"] = [e.get("http_status") for e in entries if e.get("http_status")]
    http_info["final_status"] = final.get("http_status")
    
    # --- Web Server ---
    webserver = _get_first(plugins, "HTTPServer")
    
    # --- CMS Detection (prioritized) ---
    cms = _detect_cms(plugins)
    
    # --- Programming Language ---
    programming_language = _detect_programming_language(plugins)
    
    # Avoid duplication: if CMS starts with programming language, clear the latter
    if cms and programming_language:
        cms_lower = cms.lower()
        lang_lower = programming_language.lower()
        if cms_lower.startswith(lang_lower) or lang_lower in cms_lower:
            programming_language = None
    
    # --- Security Analysis ---
    security = _analyze_security(plugins, target_url)
    
    # --- Build result ---
    ip = _get_request_ip(final)
    content_language = _get_first(plugins, "Content-Language")
    title = _get_first(plugins, "Title")
    
    result = {
        "url": target_url,
        "server_ip": ip,
        "country": _get_first(plugins, "Country"),
        "webserver": webserver,
        "cms": cms,
        # Semantics:
        # - content_language = HTTP Content-Language header (e.g. de, en)
        # - programming_language = app/runtime hint (e.g. PHP, ASP.NET) when detectable
        "content_language": content_language,
        "programming_language": programming_language,
        "security": security,
        "emails": _extract(plugins, "Email"),
        "http": http_info,
        # Website title from HTML <title> tag
        "title": title,
    }
    
    return result


def _detect_cms(plugins: dict) -> str | None:
    """
    Detect CMS using prioritized matrix.
    
    Priority order ensures specific CMS plugins are checked
    before generic MetaGenerator/PoweredBy fallbacks.
    """
    def _norm(s: str) -> str:
        return "".join(ch.lower() for ch in s if ch.isalnum())

    plugin_keys = list(plugins.keys())

    # 1. Check prioritized CMS plugins (robust: exact or startswith)
    for cms_name in CMS_PRIORITY:
        cms_norm = _norm(cms_name)
        # Prefer exact matches first
        if cms_name in plugins:
            version = _get_first(plugins, cms_name)
            return f"{cms_name} {version}" if version else cms_name

        # Then allow whatweb variants like "TYPO3 CMS" / "Craft CMS"
        for key in plugin_keys:
            if _norm(key).startswith(cms_norm):
                version = _get_first(plugins, key)
                return f"{key} {version}" if version else key
    
    # 2. Fallback to MetaGenerator
    meta = _get_first(plugins, "MetaGenerator")
    if meta:
        return meta
    
    # 3. PoweredBy (but filter out generic runtime info)
    powered = _get_first(plugins, "PoweredBy")
    if powered:
        # Skip generic runtime strings
        skip_patterns = ("php", "asp.net", "express", "nginx", "apache")
        if not any(p in powered.lower() for p in skip_patterns):
            return powered
    
    return None


def _analyze_security(plugins: dict, target_url: str) -> dict[str, Any]:
    """
    Analyze security posture from detected headers and configuration.
    
    Returns dict with:
    - https: Whether final URL uses HTTPS
    - hsts: Strict-Transport-Security present
    - xfo: X-Frame-Options present
    - xcto: X-Content-Type-Options present
    - debug_headers: Debug/dev headers detected (bad)
    """
    plugin_keys_lower = {str(k).lower() for k in plugins.keys()}
    debug_detected = any(k in plugin_keys_lower for k in DEBUG_PLUGIN_KEYS)
    
    # Extended: check header VALUES with precise pattern (excludes standard tracing)
    if not debug_detected:
        for key, val in plugins.items():
            if isinstance(val, (str, list, dict)):
                try:
                    s = str(val) if isinstance(val, str) else json.dumps(val)
                    # Skip if it's a standard trace header (not a security issue)
                    if _TRACE_EXCLUSIONS.search(s):
                        continue
                    if _DEBUG_HEADER_RE.search(s):
                        debug_detected = True
                        break
                except (TypeError, ValueError):
                    pass
    
    return {
        "https": target_url.startswith("https://"),
        "hsts": "Strict-Transport-Security" in plugins,
        "xfo": "X-Frame-Options" in plugins,
        "xcto": "X-Content-Type-Options" in plugins,
        "debug_headers": debug_detected,
    }


def _get_request_ip(entry: dict) -> str | None:
    """Extract best-effort IP from a whatweb entry.

    WhatWeb JSON isn't perfectly stable:
    - request.ip may be missing
    - IP is often exposed via the "IP" plugin
    """
    request = entry.get("request")
    if isinstance(request, dict) and request.get("ip"):
        return request["ip"]  # Direct access after guard

    plugins = entry.get("plugins", {})
    if isinstance(plugins, dict):
        ip = _get_first(plugins, "IP")
        return ip

    return None


def _detect_programming_language(plugins: dict) -> str | None:
    """Try to infer application/runtime language from common WhatWeb plugins."""
    # WhatWeb can expose these in different ways depending on plugins enabled.
    lang = _get_first(plugins, "Programming-Language")
    if lang:
        return lang

    php = _get_first(plugins, "PHP")
    if php:
        return f"PHP {php}" if not str(php).lower().startswith("php") else php

    aspnet = _get_first(plugins, "ASP.NET")
    if aspnet:
        return f"ASP.NET {aspnet}" if not str(aspnet).lower().startswith("asp") else aspnet

    # NOTE: PoweredBy fallback removed to avoid duplicating CMS detection.
    # If PoweredBy contains runtime info (e.g. "PHP/8.2"), it should be
    # detected via a dedicated plugin rather than generic PoweredBy.

    return None


def _extract_http_info(entry: dict) -> dict[str, Any]:
    """Extract basic HTTP status/redirect info from a whatweb entry (best effort)."""
    http: dict[str, Any] = {}

    # Common variants seen in whatweb JSON outputs
    for key in ("status", "code", "http_status"):
        if key in entry:
            http["status"] = entry.get(key)
            break

    request = entry.get("request")
    if isinstance(request, dict):
        if request.get("method"):
            http["method"] = request.get("method")
        if request.get("url"):
            http["request_url"] = request.get("url")
        if request.get("redirect"):
            http["redirect"] = request.get("redirect")
        if request.get("status"):
            http["request_status"] = request.get("status")

    if entry.get("redirect"):
        http["redirect"] = entry.get("redirect")

    return http
