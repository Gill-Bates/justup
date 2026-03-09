#!/usr/bin/env python3
#
# app/services/traceroute_geo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Geolocation service for traceroute hops using MaxMind GeoLite2.

Implementation is in the compiled Cython extension.
"""

from app._cython.traceroute_geo import (
	CONFIDENCE_DESTINATION,
	CONFIDENCE_FALLBACK,
	CONFIDENCE_GEOIP,
	CONFIDENCE_HOSTNAME,
	CONFIDENCE_INTERPOLATED,
	CONFIDENCE_ONLINE,
	GEO_DEDUP_DECIMALS,
	GeoLocation,
	IPV6_DISPLAY_MAX_LENGTH,
	IPV6_TRUNCATION_SUFFIX,
	META_CONFIDENCE,
	META_DESTINATION,
	META_FORWARD_FILLED,
	META_GEO_UNAVAILABLE,
	META_HOP_RANGE,
	META_INTERPOLATED,
	META_IS_IPV6,
	META_IS_PUBLIC,
	META_IS_TERMINAL,
	META_PRIVATE_IP,
	SEGMENT_DASHED,
	SEGMENT_SOLID,
	STATUS_INTERPOLATED,
	STATUS_OK,
	STATUS_PARTIAL,
	STATUS_TIMEOUT,
	geolocate_ip,
	geolocate_with_hostname_hint,
	infer_location_from_hostname,
	is_ipv6_address,
	mtr_hops_to_geo_points,
	shorten_ipv6_for_display,
	traceroute_to_geo_points,
)

__all__ = [
	"CONFIDENCE_DESTINATION",
	"CONFIDENCE_FALLBACK",
	"CONFIDENCE_GEOIP",
	"CONFIDENCE_HOSTNAME",
	"CONFIDENCE_INTERPOLATED",
	"CONFIDENCE_ONLINE",
	"GEO_DEDUP_DECIMALS",
	"GeoLocation",
	"IPV6_DISPLAY_MAX_LENGTH",
	"IPV6_TRUNCATION_SUFFIX",
	"META_CONFIDENCE",
	"META_DESTINATION",
	"META_FORWARD_FILLED",
	"META_GEO_UNAVAILABLE",
	"META_HOP_RANGE",
	"META_INTERPOLATED",
	"META_IS_IPV6",
	"META_IS_PUBLIC",
	"META_IS_TERMINAL",
	"META_PRIVATE_IP",
	"SEGMENT_DASHED",
	"SEGMENT_SOLID",
	"STATUS_INTERPOLATED",
	"STATUS_OK",
	"STATUS_PARTIAL",
	"STATUS_TIMEOUT",
	"geolocate_ip",
	"geolocate_with_hostname_hint",
	"infer_location_from_hostname",
	"is_ipv6_address",
	"mtr_hops_to_geo_points",
	"shorten_ipv6_for_display",
	"traceroute_to_geo_points",
]
