#!/usr/bin/env python3
#
# app/models/groups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for groups."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class GroupBase(BaseModel):
	"""Base group model."""
	name: str = Field(min_length=1, max_length=100)
	description: Optional[str] = Field(default=None, max_length=500)


class GroupCreate(GroupBase):
	"""Request model for creating a group."""
	pass


class GroupUpdate(BaseModel):
	"""Request model for partially updating a group."""
	name: Optional[str] = Field(default=None, min_length=1, max_length=100)
	description: Optional[str] = Field(default=None, max_length=500)


class Group(GroupBase):
	"""Group model including identifiers and timestamps."""
	id: int
	created_at: datetime
	updated_at: datetime
