#!/usr/bin/env python3
#
# app/utils/scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Lightweight async background scheduler for periodic tasks."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypedDict

_log = logging.getLogger(__name__)

_MIN_INTERVAL = 1.0


class JobStatus(TypedDict):
	name: str
	interval_seconds: float
	last_success: str | None
	last_attempt: str | None
	is_running: bool
	run_count: int
	fail_count: int


@dataclass
class _Job:
	name: str
	interval_seconds: float
	func: Callable[[], Awaitable[None]]
	run_on_start: bool = False
	initial_delay: float = 0.0
	timeout: float | None = None
	jitter_pct: float = 0.0
	last_success: datetime | None = None
	last_attempt: datetime | None = None
	run_count: int = 0
	fail_count: int = 0


class Scheduler:

	def __init__(self) -> None:
		self._jobs: dict[str, _Job] = {}
		self._tasks: dict[str, asyncio.Task] = {}
		self._stop_event: asyncio.Event | None = None
		self._started = False

	def add(
		self,
		name: str,
		interval_seconds: float,
		func: Callable[[], Awaitable[None]],
		run_on_start: bool = False,
		initial_delay: float = 0.0,
		timeout: float | None = None,
		jitter_pct: float = 0.0,
	) -> None:
		if interval_seconds < _MIN_INTERVAL:
			raise ValueError(f"Interval must be >= {_MIN_INTERVAL}s")
		self._jobs[name] = _Job(
			name=name,
			interval_seconds=interval_seconds,
			func=func,
			run_on_start=run_on_start,
			initial_delay=initial_delay,
			timeout=timeout,
			jitter_pct=max(0.0, min(0.5, jitter_pct)),
		)

	async def start(self) -> None:
		if self._started:
			return
		self._stop_event = asyncio.Event()
		self._started = True
		for name, job in self._jobs.items():
			self._tasks[name] = asyncio.create_task(self._run_loop(job))

	async def stop_graceful(self, timeout: float = 10.0) -> None:
		if not self._started:
			return
		self._started = False
		if self._stop_event:
			self._stop_event.set()
		tasks = list(self._tasks.values())
		if tasks:
			await asyncio.wait(tasks, timeout=timeout)
			for t in tasks:
				if not t.done():
					t.cancel()
		self._tasks.clear()

	async def _run_loop(self, job: _Job) -> None:
		if job.initial_delay > 0:
			await self._sleep(job.initial_delay)
		if job.run_on_start:
			await self._execute(job)
		while self._started:
			jitter = 0.0
			if job.jitter_pct > 0:
				jitter = random.uniform(-job.jitter_pct, job.jitter_pct) * job.interval_seconds
			await self._sleep(job.interval_seconds + jitter)
			if not self._started:
				break
			await self._execute(job)

	async def _execute(self, job: _Job) -> None:
		job.last_attempt = datetime.now(timezone.utc)
		try:
			if job.timeout:
				await asyncio.wait_for(job.func(), timeout=job.timeout)
			else:
				await job.func()
			job.run_count += 1
			job.last_success = datetime.now(timezone.utc)
		except asyncio.CancelledError:
			raise
		except Exception:
			job.fail_count += 1
			_log.exception("Scheduler job '%s' failed", job.name)

	async def _sleep(self, seconds: float) -> None:
		if self._stop_event:
			try:
				await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
			except asyncio.TimeoutError:
				pass

	def status(self) -> list[JobStatus]:
		return [
			JobStatus(
				name=j.name,
				interval_seconds=j.interval_seconds,
				last_success=j.last_success.isoformat() if j.last_success else None,
				last_attempt=j.last_attempt.isoformat() if j.last_attempt else None,
				is_running=j.name in self._tasks and not self._tasks[j.name].done(),
				run_count=j.run_count,
				fail_count=j.fail_count,
			)
			for j in self._jobs.values()
		]
