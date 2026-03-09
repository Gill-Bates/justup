#!/usr/bin/env python3
#
# app/services/route_interpolator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route interpolation for ICMP-blocked hops.

Implementation is in the compiled Cython extension.
"""

from app._cython.route_interpolator import (
	STATUS_INTERPOLATED,
	STATUS_OK,
	STATUS_PARTIAL,
	STATUS_TIMEOUT,
	collapse_interpolated_hops,
	ensure_destination_hop,
	interpolate_route,
)

__all__ = [
	"STATUS_INTERPOLATED",
	"STATUS_OK",
	"STATUS_PARTIAL",
	"STATUS_TIMEOUT",
	"collapse_interpolated_hops",
	"ensure_destination_hop",
	"interpolate_route",
]
