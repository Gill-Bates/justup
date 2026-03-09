#!/usr/bin/env python3
#
# app/utils/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Rate limiting configuration using slowapi.

Implementation is in the compiled Cython extension.
"""

from app._cython.rate_limit import (
	RATE_LIMIT_AUTH,
	RATE_LIMIT_DEFAULT,
	RATE_LIMIT_HEAVY,
	RATE_LIMIT_METRICS,
	RATE_LIMIT_PDF,
	limiter,
)

__all__ = [
	"RATE_LIMIT_AUTH",
	"RATE_LIMIT_DEFAULT",
	"RATE_LIMIT_HEAVY",
	"RATE_LIMIT_METRICS",
	"RATE_LIMIT_PDF",
	"limiter",
]

