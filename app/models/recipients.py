#!/usr/bin/env python3
#
# app/models/recipients.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pydantic models for alert recipients."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class RecipientBase(BaseModel):
	"""Base recipient model."""
	name: str = Field(min_length=1, max_length=100, description="Display name for the recipient")
	phone: Optional[str] = Field(default=None, max_length=32, pattern=r"^\+[1-9]\d{7,14}$", description="Phone number in E.164 format")
	email: Optional[EmailStr] = Field(default=None, description="Email address")
	is_enabled: bool = Field(default=True, description="Whether alerts are sent to this recipient")
	
	@model_validator(mode="after")
	def at_least_one_channel(self):
		"""Ensure at least one contact method is provided."""
		if not self.phone and not self.email:
			raise ValueError("At least one of phone or email must be set")
		return self


class RecipientCreate(RecipientBase):
	"""Request model for creating a recipient."""
	pass


class RecipientUpdate(BaseModel):
	"""Request model for partially updating a recipient.
	
	Note: Setting both phone and email to empty/null is prevented by the API layer,
	which checks the final state after applying the update to the existing recipient.
	"""
	name: Optional[str] = Field(default=None, min_length=1, max_length=100)
	phone: Optional[str] = Field(default=None, max_length=32, pattern=r"^\+[1-9]\d{7,14}$")
	email: Optional[EmailStr] = Field(default=None)
	is_enabled: Optional[bool] = None


class TargetBrief(BaseModel):
	"""Minimal target info for recipient badges."""
	id: int
	name: str


class RecipientPublic(RecipientBase):
	"""Public recipient model including assigned targets."""
	id: int
	created_at: datetime
	updated_at: datetime
	targets: list[TargetBrief] = Field(default_factory=list, description="Targets assigned to this recipient")


class RecipientResponse(BaseModel):
	"""Response model for recipient operations with optional warnings."""
	recipient: RecipientPublic
	warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings (e.g., phone not registered with Signal)")
