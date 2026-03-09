#!/usr/bin/env python3
#
# app/monitor/quality_probe.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitoring Quality Probe – measures the monitor's own connectivity.

This module re-exports the compiled Cython implementation.
Implementation is in app/_cython/quality_probe.pyx
"""

from app._cython.quality_probe import (
    DEFAULT_MIN_SCORE,
    DEFAULT_QUALITY_TARGETS,
    DEFAULT_RTT_THRESHOLD_MS,
    MAX_CONCURRENT_PROBES,
    PROBE_TIMEOUT_SECONDS,
    SCORE_SMOOTHING_FACTOR,
    QualityResult,
    get_min_score,
    get_quality_targets,
    get_rtt_threshold,
    run_quality_probe,
    _reset_score_state,
)

__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_QUALITY_TARGETS",
    "DEFAULT_RTT_THRESHOLD_MS",
    "MAX_CONCURRENT_PROBES",
    "PROBE_TIMEOUT_SECONDS",
    "SCORE_SMOOTHING_FACTOR",
    "QualityResult",
    "get_min_score",
    "get_quality_targets",
    "get_rtt_threshold",
    "run_quality_probe",
    "_reset_score_state",
]
