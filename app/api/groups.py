#!/usr/bin/env python3
#
# app/api/groups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Group management API routes."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db import sqlite as sqlite_db
from ..models.groups import Group, GroupCreate, GroupUpdate
from ..utils.deps import get_conn
from .auth import get_current_user, require_admin

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
def create(request: Request, payload: GroupCreate, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Create a new group."""
    try:
        new_id = sqlite_db.create_group(conn, name=payload.name, description=payload.description)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Group name already exists") from exc
    row = sqlite_db.get_group(conn, new_id)

    user_dict = dict(_user) if hasattr(_user, "keys") else _user
    _log.info(
        "GROUP_CREATE id=%d name=%s user=%s ip=%s",
        new_id,
        payload.name,
        user_dict.get("username", "unknown"),
        request.client.host if request.client else "unknown",
    )

    return _row_to_group(row)


@router.patch("/{group_id}", response_model=Group)
def patch(group_id: int, payload: GroupUpdate, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Update a group by id."""
    patch_data = payload.model_dump(exclude_unset=True)
    # The UNIQUE constraint decides name clashes inside the write transaction (no check-then-write race).
    try:
        found = sqlite_db.update_group(conn, group_id=group_id, patch=patch_data)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Group name already exists") from exc
    if not found:
        raise HTTPException(status_code=404, detail="Group not found")

    row = sqlite_db.get_group(conn, group_id)
    return _row_to_group(row)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(request: Request, group_id: int, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Delete a group if unused by any targets."""
    try:
        group_name = sqlite_db.delete_group_if_empty(conn, group_id)
    except sqlite_db.GroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Group not found") from exc
    except sqlite_db.GroupNotEmptyError as exc:
        raise HTTPException(
            status_code=409, detail=f"Cannot delete group: {exc.count} target(s) still use this group"
        ) from exc

    user_dict = dict(_user) if hasattr(_user, "keys") else _user
    _log.info(
        "GROUP_DELETE id=%d name=%s user=%s ip=%s",
        group_id,
        group_name,
        user_dict.get("username", "unknown"),
        request.client.host if request.client else "unknown",
    )
