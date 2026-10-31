//
// app/static/js/settings.signal.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.signal.js - Signal alerting configuration (Modular Wrapper)
 * 
 * This file re-exports all Signal functionality from modular components:
 * - settings.signal-core.js: API endpoints, validation, state management
 * - settings.signal-status.js: Status badges, device info display
 * - settings.signal-registration.js: QR code flow, phone registration
 * - settings.signal-actions.js: Test message, unlink, templates, init
 * 
 * Import this file as the main entry point, or import specific modules
 * for tree-shaking in bundled builds.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Re-export from Core module
// ─────────────────────────────────────────────────────────────────────────────

export {
    SIGNAL_API,
    normalizeE164,
    isValidE164,
    signalStatusGuard,
    qrCodeGuard,
    isSignalBusy,
    setSignalBusy,
    maskPhone,
    getCurrentQrBlobUrl,
    setCurrentQrBlobUrl,
    revokeCurrentQrBlobUrl,
    setOnPanelEnabled,
    toggleSignalPanel,
    autoSetProfileName,
    ensureSignalEnabled
} from './settings.signal-core.js';

// ─────────────────────────────────────────────────────────────────────────────
// Re-export from Status module
// ─────────────────────────────────────────────────────────────────────────────

export {
    loadSignalStatus
} from './settings.signal-status.js';

// ─────────────────────────────────────────────────────────────────────────────
// Re-export from Registration module
// ─────────────────────────────────────────────────────────────────────────────

export {
    signalSelectMode,
    signalBackToModeSelection,
    signalGenerateQrCode,
    signalLoadQrCode,
    signalConfirmLink,
    signalRegister,
    signalVerify,
    signalSetProfileName,
    signalSetProfileNameLink,
    signalSkipProfile
} from './settings.signal-registration.js';

// ─────────────────────────────────────────────────────────────────────────────
// Re-export from Actions module
// ─────────────────────────────────────────────────────────────────────────────

export {
    sendTestMessage,
    signalUnlink,
    unregisterPhone,
    initSignalSetupCollapse,
    initSignal
} from './settings.signal-actions.js';
