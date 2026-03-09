#!/usr/bin/env python3
#
# docker/setup_cython.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import os
import sys

__all__ = ["CYTHON_MODULES", "CYTHON_DB_MODULES"]

# Unified format: (module_name, source_path)
CYTHON_MODULES = (
    ("app._cython.auth_helpers", "app/_cython/auth_helpers.pyx"),
    ("app._cython.checks", "app/_cython/checks.pyx"),
    ("app._cython.crypto", "app/_cython/crypto.pyx"),
    ("app._cython.telemetry", "app/_cython/telemetry.pyx"),
    ("app._cython.pop_codes", "app/_cython/pop_codes.pyx"),
    ("app._cython.pop_locations", "app/_cython/pop_locations.pyx"),
    ("app._cython.provider_lookup", "app/_cython/provider_lookup.pyx"),
    ("app._cython.quality_probe", "app/_cython/quality_probe.pyx"),
    ("app._cython.rate_limit", "app/_cython/rate_limit.pyx"),
    ("app._cython.route_interpolator", "app/_cython/route_interpolator.pyx"),
    ("app._cython.scheduler", "app/_cython/scheduler.pyx"),
    ("app._cython.settings_cache", "app/_cython/settings_cache.pyx"),
    ("app._cython.status_helper", "app/_cython/status_helper.pyx"),
    ("app._cython.traceroute_geo", "app/_cython/traceroute_geo.pyx"),
    ("app._cython.users", "app/_cython/users.pyx"),
)

# Database modules compiled separately
CYTHON_DB_MODULES = (
    ("app.db._pl", "app/db/_pl.pyx"),
)

# Python 3.13 required for ABI compatibility with runtime image (python:3.13-slim)
# Prevents CPython struct layout mismatches between build/runtime
_REQUIRED_PYTHON = (3, 13)

# Security-critical modules that require runtime safety checks enabled
# (auth, crypto, rate limiting, user management, geolocation with math operations)
# These modules get boundscheck=True, wraparound=True, cdivision=False
_SAFETY_CRITICAL = {
    "app._cython.auth_helpers",
    "app._cython.crypto",
    "app._cython.rate_limit",
    "app._cython.users",
    "app._cython.traceroute_geo",  # Bus error with cdivision=True on division by zero
}

# Shared Cython compiler macros
# CYTHON_USE_PYTHON_INTERNALS=0 prevents reliance on CPython struct layouts
# that change between versions, ensuring ABI compatibility
_DEFINE_MACROS = [
    ("CYTHON_FAST_THREAD_STATE", "1"),
    ("CYTHON_USE_PYTHON_INTERNALS", "0"),
]


def _extensions() -> tuple[list["Extension"], list["Extension"]]:
    """
    Build extension lists, separated by safety requirements.
    
    Returns:
        (safe_extensions, fast_extensions) - Two lists for different compiler directives
    """
    from setuptools import Extension

    # Combine all modules into unified list
    all_modules = list(CYTHON_MODULES) + list(CYTHON_DB_MODULES)
    
    safe_extensions = []
    fast_extensions = []
    
    for module_name, pyx_path in all_modules:
        ext = Extension(
            module_name,
            [pyx_path],
            define_macros=list(_DEFINE_MACROS),
        )
        
        # Separate by safety requirements
        if module_name in _SAFETY_CRITICAL:
            safe_extensions.append(ext)
        else:
            fast_extensions.append(ext)
    
    # Validate all source files exist before attempting build
    for ext in safe_extensions + fast_extensions:
        for src in ext.sources:
            if not os.path.isfile(src):
                raise FileNotFoundError(
                    f"Cython source file missing: {src}\n"
                    f"Expected path: {os.path.abspath(src)}"
                )
    
    return safe_extensions, fast_extensions


def main() -> None:
    strict = os.getenv("JUSTUP_CYTHON_STRICT_PYTHON", "1").strip().lower() not in {"0", "false", "no"}
    if strict and sys.version_info[:2] != _REQUIRED_PYTHON:
        raise SystemExit(
            f"Cython build expects Python {_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]}.x "
            f"(to match the Docker runtime image). Detected: {sys.version.split()[0]}. "
            "Install the expected Python version or set JUSTUP_CYTHON_STRICT_PYTHON=0 to bypass."
        )

    try:
        from setuptools import setup
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "setuptools is required to build Cython extensions. "
            "Install with: pip install setuptools wheel"
        ) from e

    try:
        from Cython.Build import cythonize
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "Cython build requested but Cython is not installed. "
            "Install with: pip install cython setuptools wheel"
        ) from e

    # Strip docstrings to reduce binary size (does NOT meaningfully obfuscate code)
    # Note: .so files still contain function names, module names, exception messages
    from Cython.Compiler import Options as cython_options

    cython_options.docstrings = False

    # Allow incremental builds during development
    force_rebuild = os.getenv("CYTHON_FORCE_REBUILD", "1").strip().lower() not in {"0", "false", "no"}

    # Split extensions by safety requirements
    safe_exts, fast_exts = _extensions()

    # Compile safety-critical modules with runtime checks enabled
    safe_compiled = cythonize(
        safe_exts,
        annotate=False,
        force=force_rebuild,
        nthreads=os.cpu_count() or 1,
        compiler_directives={
            "language_level": 3,
            "boundscheck": True,        # Enable bounds checking
            "wraparound": True,         # Enable negative index checks
            "initializedcheck": True,   # Enable uninitialized variable checks
            "cdivision": False,         # Python division with ZeroDivisionError
            "embedsignature": False,    # No Python signatures in docstrings
            "binding": False,           # Faster function calls
        },
    )

    # Compile performance-focused modules with checks disabled
    fast_compiled = cythonize(
        fast_exts,
        annotate=False,
        force=force_rebuild,
        nthreads=os.cpu_count() or 1,
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,       # Disable bounds checking
            "wraparound": False,        # Disable negative index checks
            "initializedcheck": False,  # Disable uninitialized variable checks
            "cdivision": True,          # C-style division (faster, no exception)
            "embedsignature": False,    # No Python signatures in docstrings
            "binding": False,           # Faster function calls
        },
    )

    setup(
        name="justup-cython",
        packages=[],  # Explicit: extension-only build, no Python packages
        ext_modules=safe_compiled + fast_compiled,
    )


if __name__ == "__main__":
    main()
