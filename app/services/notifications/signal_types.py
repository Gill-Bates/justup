#!/usr/bin/env python3
#
# app/services/notifications/signal_types.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal status data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class SignalStatus:
    """Status information from signal-cli backend.
    
    Semantics:
        reachable: signal-cli binary is executable
        registered: at least one account exists
        linked: identical to registered (separated for UI clarity)
        can_send: operationally ready to send (reachable + registered + no errors)
        accounts: list of masked phone numbers
        error: human-readable, UI-safe error message
    """
    reachable: bool
    registered: bool
    linked: bool
    accounts: List[str]
    error: Optional[str] = None
    
    @property
    def can_send(self) -> bool:
        """Check if Signal is operationally ready to send messages.
        
        This is more reliable than just checking reachable/registered,
        as it also considers error state.
        """
        return self.reachable and self.registered and self.error is None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reachable": self.reachable,
            "registered": self.registered,
            "linked": self.linked,
            "can_send": self.can_send,
            "accounts": self.accounts,
            "error": self.error,
        }
