#!/usr/bin/env python3
#
# app/monitor/scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Async monitoring scheduler that runs target checks and updates cached status/metrics.

Implementation is in the compiled Cython extension.
"""

from app._cython.scheduler import (
	AlertStatus,
	HOUSEKEEPING_INTERVAL_SECONDS,
	HOUSEKEEPING_JITTER_SECONDS,
	MAX_BACKOFF_SECONDS,
	MAX_JITTER_SECONDS,
	MonitorScheduler,
	PortStatus,
	QualityState,
	TargetRuntime,
	TargetStatus,
)

__all__ = [
	"AlertStatus",
	"HOUSEKEEPING_INTERVAL_SECONDS",
	"HOUSEKEEPING_JITTER_SECONDS",
	"MAX_BACKOFF_SECONDS",
	"MAX_JITTER_SECONDS",
	"MonitorScheduler",
	"PortStatus",
	"QualityState",
	"TargetRuntime",
	"TargetStatus",
]
