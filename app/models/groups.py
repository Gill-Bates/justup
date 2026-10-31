#!/usr/bin/env python3
#
# app/models/groups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for groups."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _strip_nonblank(v: str) -> str:
    out = v.strip()
    if not out:
        raise ValueError("name must not be blank")
    return out


class GroupBase(BaseModel):
    """Base group model."""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class GroupCreate(GroupBase):
    """Request model for creating a group."""

    # Input-only validation: stored legacy names (e.g. whitespace-only) must stay readable in responses.
    @field_validator("name")
    @classmethod
    def _name_nonblank(cls, v: str) -> str:
        return _strip_nonblank(v)


class GroupUpdate(BaseModel):
    """Request model for partially updating a group."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_nonblank(cls, v: str | None) -> str | None:
        return None if v is None else _strip_nonblank(v)


class Group(GroupBase):
    """Group model including identifiers and timestamps."""

    id: int
    created_at: datetime
    updated_at: datetime
