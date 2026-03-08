#!/usr/bin/env python3
#
# app/api/monitors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitor management API routes."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..db.sqlite_monitors import (
	create_monitor as db_create_monitor,
	delete_monitor as db_delete_monitor,
	get_all_monitor_statuses,
	get_monitor_by_id,
	get_monitor_status,
	update_monitor as db_update_monitor,
)
from ..db.sqlite_incidents import get_incidents_for_monitor, get_recent_incidents
from ..db.tsdb import query as tsdb_query, delete_monitor_metrics, list_monitor_metrics
from ..models.monitors import MonitorCreate, MonitorUpdate
from ..utils.deps import get_conn, get_config
from .auth import get_current_user, require_admin
from .response import ok_response

_log = logging.getLogger(__name__)

router = APIRouter(tags=["monitors"])


@router.get("")
def list_monitors(
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	rows = get_all_monitor_statuses(conn)
	return ok_response(data=[dict(row) for row in rows])


@router.post("", status_code=201)
def create_monitor(
	payload: MonitorCreate,
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	monitor_id = db_create_monitor(
		conn,
		name=payload.name,
		monitor_type=payload.monitor_type,
		url=payload.url,
		hostname=payload.hostname,
		port=payload.port,
		method=payload.method,
		expected_status_code=payload.expected_status_code,
		keyword=payload.keyword,
		timeout_seconds=payload.timeout_seconds,
		interval_seconds=payload.interval_seconds,
		retries=payload.retries,
		retry_interval_seconds=payload.retry_interval_seconds,
		verify_ssl=payload.verify_ssl,
		follow_redirects=payload.follow_redirects,
		max_redirects=payload.max_redirects,
		headers_json=payload.headers_json,
		body=payload.body,
		description=payload.description,
		tags=payload.tags,
		notification_group_id=payload.notification_group_id,
		created_by=user["id"],
		is_active=payload.is_active,
	)
	monitor = get_monitor_by_id(conn, monitor_id)
	return ok_response(data=dict(monitor))


@router.get("/{monitor_id}")
def get_monitor(
	monitor_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	monitor = get_monitor_by_id(conn, monitor_id)
	if not monitor:
		raise HTTPException(status_code=404, detail="Monitor not found")
	status = get_monitor_status(conn, monitor_id)
	data = dict(monitor)
	if status:
		data.update({k: status[k] for k in status.keys() if k != "monitor_id"})
	return ok_response(data=data)


@router.patch("/{monitor_id}")
def update_monitor(
	monitor_id: int,
	payload: MonitorUpdate,
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	if not get_monitor_by_id(conn, monitor_id):
		raise HTTPException(status_code=404, detail="Monitor not found")
	updates = payload.model_dump(exclude_unset=True)
	if not updates:
		raise HTTPException(status_code=400, detail="No fields to update")
	db_update_monitor(conn, monitor_id, **updates)
	monitor = get_monitor_by_id(conn, monitor_id)
	return ok_response(data=dict(monitor))


@router.delete("/{monitor_id}", status_code=204, response_class=Response)
def delete_monitor(
	monitor_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(require_admin),
):
	if not db_delete_monitor(conn, monitor_id):
		raise HTTPException(status_code=404, detail="Monitor not found")
	return Response(status_code=204)


@router.get("/{monitor_id}/incidents")
def monitor_incidents(
	monitor_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	if not get_monitor_by_id(conn, monitor_id):
		raise HTTPException(status_code=404, detail="Monitor not found")
	incidents = get_incidents_for_monitor(conn, monitor_id)
	return ok_response(data=[dict(row) for row in incidents])


@router.get("/incidents/recent")
def recent_incidents(
	conn: sqlite3.Connection = Depends(get_conn),
	user: sqlite3.Row = Depends(get_current_user),
):
	incidents = get_recent_incidents(conn)
	return ok_response(data=[dict(row) for row in incidents])


@router.get("/{monitor_id}/metrics")
def get_monitor_metrics(
	monitor_id: int,
	metric: str = Query(default="response_time", description="Metric name to query"),
	hours: int = Query(default=24, ge=1, le=720, description="Hours of data to fetch"),
	limit: Optional[int] = Query(default=None, ge=1, le=10000, description="Max points"),
	conn: sqlite3.Connection = Depends(get_conn),
	cfg = Depends(get_config),
	user: sqlite3.Row = Depends(get_current_user),
):
	"""Get metrics for a monitor (response_time, is_up, status_code)."""
	if not get_monitor_by_id(conn, monitor_id):
		raise HTTPException(status_code=404, detail="Monitor not found")

	end = datetime.now(timezone.utc)
	start = end - timedelta(hours=hours)

	points = tsdb_query(cfg.tsdb_dir, monitor_id, metric, start=start, end=end, limit=limit)

	return ok_response(data={
		"metric": metric,
		"monitor_id": monitor_id,
		"points": [{"ts": p.ts.isoformat(), "value": p.value} for p in points],
		"available_metrics": list_monitor_metrics(cfg.tsdb_dir, monitor_id),
	})


@router.delete("/{monitor_id}/metrics", status_code=204, response_class=Response)
def delete_metrics(
	monitor_id: int,
	conn: sqlite3.Connection = Depends(get_conn),
	cfg = Depends(get_config),
	user: sqlite3.Row = Depends(require_admin),
):
	"""Delete all metrics for a monitor (admin only)."""
	if not get_monitor_by_id(conn, monitor_id):
		raise HTTPException(status_code=404, detail="Monitor not found")

	delete_monitor_metrics(cfg.tsdb_dir, monitor_id)
	return Response(status_code=204)
