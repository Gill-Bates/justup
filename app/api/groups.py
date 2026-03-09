#!/usr/bin/env python3
#
# app/api/groups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Group management API routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db import sqlite as sqlite_db
from ..models.groups import Group, GroupCreate, GroupUpdate
from ..utils.deps import get_conn
from .auth import get_current_user

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/groups", tags=["groups"])


def _row_to_group(row) -> Group:
	return Group(
		id=int(row["id"]),
		name=row["name"],
		description=row["description"],
		created_at=row["created_at"],
		updated_at=row["updated_at"],
	)


@router.get("", response_model=list[Group])
def list_all(conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""List all groups."""
	rows = sqlite_db.list_groups(conn)
	return [_row_to_group(r) for r in rows]


@router.get("/{group_id}", response_model=Group)
def get_one(group_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Get a single group by id."""
	row = sqlite_db.get_group(conn, group_id)
	if row is None:
		raise HTTPException(status_code=404, detail="Group not found")
	return _row_to_group(row)


@router.post("", response_model=Group, status_code=status.HTTP_201_CREATED)
def create(request: Request, payload: GroupCreate, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Create a new group."""
	existing = sqlite_db.get_group_by_name(conn, payload.name)
	if existing:
		raise HTTPException(status_code=409, detail="Group name already exists")
	
	new_id = sqlite_db.create_group(conn, name=payload.name, description=payload.description)
	row = sqlite_db.get_group(conn, new_id)
	
	user_dict = dict(_user) if hasattr(_user, "keys") else _user
	_log.info("GROUP_CREATE id=%d name=%s user=%s ip=%s", new_id, payload.name, user_dict.get("username", "unknown"), request.client.host if request.client else "unknown")
	
	return _row_to_group(row)


@router.patch("/{group_id}", response_model=Group)
def patch(group_id: int, payload: GroupUpdate, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Update a group by id."""
	existing = sqlite_db.get_group(conn, group_id)
	if existing is None:
		raise HTTPException(status_code=404, detail="Group not found")
	
	patch_data = payload.model_dump(exclude_unset=True)
	
	if "name" in patch_data and patch_data["name"] != existing["name"]:
		name_check = sqlite_db.get_group_by_name(conn, patch_data["name"])
		if name_check:
			raise HTTPException(status_code=409, detail="Group name already exists")
	
	if patch_data:
		sqlite_db.update_group(conn, group_id=group_id, patch=patch_data)
	
	row = sqlite_db.get_group(conn, group_id)
	return _row_to_group(row)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(request: Request, group_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
	"""Delete a group if unused by any targets."""
	existing = sqlite_db.get_group(conn, group_id)
	if existing is None:
		raise HTTPException(status_code=404, detail="Group not found")

	targets_count = sqlite_db.count_targets_in_group(conn, existing["name"])
	if targets_count > 0:
		raise HTTPException(
			status_code=409, 
			detail=f"Cannot delete group: {targets_count} target(s) still use this group"
		)
	
	group_name = existing["name"]
	sqlite_db.delete_group(conn, group_id)

	user_dict = dict(_user) if hasattr(_user, "keys") else _user
	_log.info("GROUP_DELETE id=%d name=%s user=%s ip=%s", group_id, group_name, user_dict.get("username", "unknown"), request.client.host if request.client else "unknown")
	
	return
