#!/usr/bin/env python3
#
# app/_cython/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Cythonized implementations of selected security-/logic-sensitive modules.

This package is optional at runtime. Callers should import via the wrappers in
`app.utils.*`, which fall back to the pure-Python implementations when the
extensions are not built.
"""

