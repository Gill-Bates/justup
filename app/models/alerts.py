#!/usr/bin/env python3
#
# app/models/alerts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Alert models for downtime and incident tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


AlertStatus = Literal["open", "resolved"]


class AlertBase(BaseModel):
    """Base alert fields for downtime events."""
    target_id: int
    target_name: str
    failed_service: str = Field(description="Which service failed: http, ping, tcp, cert")
    started_at: datetime
    ended_at: Optional[datetime] = None
    recipients_notified: list[str] = Field(default_factory=list)
    is_acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None


class Alert(AlertBase):
    """Full alert model with ID and computed fields."""
    id: UUID
    status: AlertStatus = "open"
    duration_seconds: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AlertPublic(BaseModel):
    """Public response model for alerts."""
    id: str  # UUID as string
    target_id: int
    target_name: str
    failed_service: str
    status: AlertStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    recipients_notified: list[str] = []
    is_acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None


class AlertListResponse(BaseModel):
    """Paginated alert list response."""
    items: list[AlertPublic]
    total: int
    page: int
    page_size: int
    total_pages: int
