#!/usr/bin/env python3
#
# app/services/notifications/signal_backend.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Abstract Signal backend interface and factory.

Design principles:
- No HTTP logic
- No JSON dependency
- Exceptions instead of status codes
- Backend is instantiated once per process and reused (internally stateful)

Thread-safety:
- Factory (get_signal_backend) is thread-safe
- Backend methods are NOT guaranteed to be thread-safe
- Callers using asyncio.to_thread() should be aware that signal-cli
  may use lockfiles internally; concurrent calls may block or fail
- For high-concurrency scenarios, consider external serialization

Lifespan integration:
    For proper cleanup on shutdown, integrate with FastAPI lifespan events:
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        backend = get_signal_backend()
        backend.close()
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional

from .signal_types import SignalStatus

_log = logging.getLogger(__name__)


class SignalBackend(ABC):
    """Abstract base class for Signal backends.
    
    Implementations must handle:
    - subprocess management (signal-cli)
    - error translation to SignalError subclasses
    - timeout handling
    
    Thread-safety:
        Methods are NOT thread-safe by default. signal-cli uses lockfiles
        that may cause concurrent calls to block or fail. Callers should
        serialize access if using from multiple threads/coroutines.
    """
    
    def __repr__(self) -> str:
        """Return repr for debugging and logging."""
        return f"<{self.__class__.__name__}>"
    
    @abstractmethod
    def status(self, force_refresh: bool = False) -> SignalStatus:
        """Get current Signal status.
        
        Args:
            force_refresh: If True, bypass cache and fetch fresh status
        
        Returns:
            SignalStatus with reachable, registered, linked, accounts, error.
            Never raises - errors are reported via SignalStatus.error field.
            
        Note:
            This method uses a different error-handling strategy than other
            methods (returns errors in status object vs raising exceptions)
            because it's a health check that should always succeed to allow
            UI to show diagnostics even when signal-cli is unavailable.
        """
        pass
    
    @abstractmethod
    def invalidate_cache(self) -> None:
        """Invalidate cached status, forcing fresh fetch on next status() call."""
        pass
    
    @abstractmethod
    def send_message(self, recipient: str, message: str, sender: str | None = None) -> None:
        """Send a message to a recipient.
        
        Args:
            recipient: Phone number in E.164 format
            message: Message text
            sender: Sender phone number (optional, uses configured sender if not provided)
            
        Raises:
            SignalNotRegistered: if no account is registered
            SignalRecipientInvalid: if recipient format is invalid
            SignalSendFailed: if sending fails
            SignalUnavailable: if signal-cli is not available
        """
        pass
    
    @abstractmethod
    def get_link_qr(self, device_name: str = "justUp") -> tuple[bytes | None, str | None]:
        """Generate QR code for device linking.
        
        Args:
            device_name: Name to display on linked device
            
        Returns:
            Tuple of (PNG image bytes, session_id) or (None, None) if QR not available.
            Returns None if a link process is already active or generation fails.
            
        Raises:
            SignalUnavailable: if signal-cli binary is missing or not executable
            SignalTimeout: if QR generation times out
        """
        pass
    
    @abstractmethod
    def wait_for_link(self, session_id: str, timeout: int = 60) -> bool:
        """Wait for the link process to complete after QR scan.
        
        Args:
            session_id: Session ID returned by get_link_qr()
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if link completed successfully, False otherwise
        """
        pass
    
    @abstractmethod
    def get_link_session_id(self) -> str | None:
        """Get the current link session ID.
        
        Returns:
            Current session ID if a link process is active, None otherwise
        """
        pass
    
    @abstractmethod
    def cancel_link(self) -> None:
        """Cancel any running link process."""
        pass
    
    @abstractmethod
    def unregister(
        self,
        sender_number: str | None = None,
        delete_cache: bool = True,
    ) -> None:
        """Unregister the current account (deletes account from Signal servers).
        
        Args:
            sender_number: Phone number to unregister. If None, uses default
                          or the first registered account.
            delete_cache: If True, delete local data directory after unregister.
            
        Raises:
            SignalNotRegistered: if no account is registered
            SignalUnavailable: if signal-cli is not available
            
        See Also:
            unlink_device() - Remove device without deleting account
        """
        pass
    
    def close(self) -> None:
        """Release resources (optional lifecycle hook).
        
        Called by factory on reset. Override in implementations
        that need cleanup (e.g., terminate subprocesses).
        
        Note:
            Currently only called via reset_signal_backend() which is
            intended for testing. For production shutdown, integrate
            with FastAPI's lifespan events if cleanup is needed.
        """
        pass
    
    @abstractmethod
    def list_accounts(self) -> list[str]:
        """List all registered accounts.
        
        Returns:
            List of phone numbers in E.164 format
            
        Raises:
            SignalUnavailable: if signal-cli is not available
        """
        pass

    @abstractmethod
    def check_user_registered(self, recipient: str, sender: str | None = None) -> bool:
        """Check if a phone number is registered with Signal.
        
        Uses getUserStatus to query the Signal server for recipient's
        registration status.
        
        Args:
            recipient: Phone number to check (E.164 format)
            sender: Sender account to use for the query (optional)
            
        Returns:
            True if recipient is registered with Signal, False otherwise
            
        Raises:
            SignalUnavailable: if signal-cli is not available
            SignalNotRegistered: if sender account is not registered
        """
        pass

    @abstractmethod
    def register(self, number: str, use_voice: bool = False, captcha: str | None = None) -> None:
        """Start registration process for a phone number.
        
        Sends a verification code via SMS (default) or voice call.
        
        Args:
            number: Phone number in E.164 format (e.g., +49123456789)
            use_voice: If True, send code via voice call instead of SMS
            captcha: Captcha token if required by Signal
            
        Raises:
            SignalUnavailable: if signal-cli is not available
            SignalSendFailed: if registration request fails (e.g., captcha needed)
        """
        pass

    @abstractmethod
    def verify(self, number: str, code: str) -> None:
        """Verify registration with the received code.
        
        Args:
            number: Phone number being verified (E.164 format)
            code: 6-digit verification code
            
        Raises:
            SignalUnavailable: if signal-cli is not available
            SignalSendFailed: if verification fails (wrong code, expired, etc.)
        """
        pass

    @abstractmethod
    def get_valid_sender(self, configured_sender: str | None = None) -> str:
        """Get a valid sender number, validated against signal-cli accounts.
        
        Args:
            configured_sender: Optional sender from DB (may be stale)
            
        Returns:
            A valid phone number from signal-cli
            
        Raises:
            SignalNotRegistered: If no accounts are registered
        """
        pass

    @abstractmethod
    def unlink_device(
        self,
        sender_number: str | None = None,
        delete_cache: bool = False,
    ) -> None:
        """Unlink device (removes device from account but keeps account active).
        
        Args:
            sender_number: Phone number to unlink. If None, affects ALL local accounts.
            delete_cache: If True, deletes local cache data.
            
        Raises:
            SignalNotRegistered: if no account is registered
            SignalUnavailable: if signal-cli is not available
            
        See Also:
            unregister() - Delete account from Signal servers entirely
        """
        pass

    @abstractmethod
    def update_profile(
        self,
        name: str,
        avatar_path: str | None = None,
    ) -> None:
        """Update Signal profile name and avatar.
        
        Args:
            name: Display name shown in Signal chats
            avatar_path: Optional path to avatar image
            
        Raises:
            SignalNotRegistered: if no account is registered
            SignalSendFailed: if profile update fails
        """
        pass


# ─── Factory ────────────────────────────────────────────────────────────────────

_backend_instance: SignalBackend | None = None
_backend_lock = threading.Lock()


def get_signal_backend() -> SignalBackend:
    """Get the Signal backend singleton.
    
    Thread-safe using double-checked locking pattern. The first check avoids
    lock acquisition on the hot path after initialization.
    """
    global _backend_instance
    
    # Fast path: return existing instance without lock
    if _backend_instance is not None:
        return _backend_instance
    
    # Slow path: acquire lock and initialize
    with _backend_lock:
        if _backend_instance is None:
            from .signal_cli_backend import SignalCliBackend
            _backend_instance = SignalCliBackend()
        return _backend_instance


def reset_signal_backend() -> None:
    """Reset the backend singleton (testing only).
    
    WARNING: Not safe to call while requests are in flight.
    Only allowed in test environments to prevent accidental production use.
    
    Raises:
        RuntimeError: If called outside test environment
    """
    global _backend_instance
    
    # Guard: Only allow in test environments
    if os.environ.get("TESTING") != "1" and os.environ.get("PYTEST_CURRENT_TEST") is None:
        raise RuntimeError(
            "reset_signal_backend() is only allowed in test environments. "
            "Set TESTING=1 or run under pytest."
        )
    
    with _backend_lock:
        if _backend_instance is not None:
            try:
                _backend_instance.close()
            except Exception as e:
                _log.warning("Error closing backend during reset: %s", e)
            _backend_instance = None
