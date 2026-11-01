#!/usr/bin/env python3
#
# app/utils/maintenance.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Process-wide maintenance state shared by restore and background workers.

Components that write the data directory (scheduler iterations, PDF jobs, backup) run inside
``work_section()``. ``begin_maintenance()`` refuses new sections and waits, bounded, until the
running ones finished, so a restore never swaps files under a live TSDB/PDF/SQLite writer.

The state is per process; cross-process safety of single files stays with the TSDB file locks.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


class MaintenanceActiveError(RuntimeError):
    """A new unit of work was refused because maintenance (restore) is in progress."""


class StaleWorkError(MaintenanceActiveError):
    """Work based on a pre-restore snapshot was refused after a successful restore."""


_COND = threading.Condition()
_ACTIVE = False
_INFLIGHT = 0
_GENERATION = 0


def is_active() -> bool:
    """True while maintenance is active or being established."""
    with _COND:
        return _ACTIVE


def current_generation() -> int:
    """Return the current data generation for work that spans network or thread boundaries."""
    with _COND:
        return _GENERATION


@contextmanager
def work_section(expected_generation: int | None = None) -> Iterator[None]:
    """Register one unit of data-directory work, optionally tied to a data generation.

    Sections must not be nested in the same thread of control: an inner section could be refused
    while the outer one is still being drained. A stale generation rejects work computed before a
    successful restore, even after maintenance has ended.
    """
    global _INFLIGHT
    with _COND:
        if _ACTIVE:
            raise MaintenanceActiveError("Maintenance in progress")
        if expected_generation is not None and expected_generation != _GENERATION:
            raise StaleWorkError("Work snapshot belongs to a previous data generation")
        _INFLIGHT += 1
    try:
        yield
    finally:
        with _COND:
            _INFLIGHT -= 1
            if _INFLIGHT == 0:
                _COND.notify_all()


def begin_maintenance(timeout: float = 30.0) -> bool:
    """Refuse new work and wait until running work finished.

    Returns False without keeping any state when running work did not finish within ``timeout``
    seconds (the refusal is lifted again) or when maintenance is already active. Must not be
    called from inside a work section.

    The data generation is not advanced here: a drained maintenance window may still end without
    replacing any data (failed quiescence, rolled-back restore). The generation advances only when
    ``confirm_data_replaced()`` confirms the data was actually swapped.
    """
    global _ACTIVE
    deadline = time.monotonic() + timeout
    with _COND:
        if _ACTIVE:
            return False
        _ACTIVE = True
        while _INFLIGHT > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _ACTIVE = False
                _COND.notify_all()
                return False
            _COND.wait(remaining)
        return True


def confirm_data_replaced() -> int:
    """Advance the data generation after a restore confirmed that the data was replaced.

    Call this only once the swap succeeded, while maintenance is still active, so that in-flight
    work computed against the pre-restore snapshot is rejected as stale when it later persists. A
    maintenance window that ends without replacing data must not call this.

    Raises ``RuntimeError`` when maintenance is not active: the generation must only advance from
    inside a live restore window, never on a stray or out-of-order call.
    """
    global _GENERATION
    with _COND:
        if not _ACTIVE:
            raise RuntimeError("confirm_data_replaced() requires an active maintenance window")
        _GENERATION += 1
        return _GENERATION


def end_maintenance() -> None:
    """Allow new work again (idempotent)."""
    global _ACTIVE
    with _COND:
        _ACTIVE = False
        _COND.notify_all()
