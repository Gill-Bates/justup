//
// app/static/js/settings.signal-core.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.signal-core.js - Signal core utilities
 * 
 * Provides:
 * - API endpoints configuration
 * - E.164 phone number validation
 * - Race condition guards
 * - Busy state management
 * - Utility functions (maskPhone, ensureSignalEnabled, etc.)
 */

import { 
    DOM, 
    notify,
    createGuard,
} from './settings.core.js';

// Use global apiFetch for authenticated API calls
const apiFetch = window.apiFetch;

// ─────────────────────────────────────────────────────────────────────────────
// API Endpoints (centralized for maintainability)
// ─────────────────────────────────────────────────────────────────────────────

export const SIGNAL_API = {
    status: '/settings/signal/status',
    qr: '/signal/qr',
    linkConfirm: '/signal/link/confirm',
    linkCancel: '/signal/link/cancel',
    accounts: '/signal/accounts',
    register: '/signal/register',
    verify: '/signal/verify',
    profileName: '/signal/profile-name',
    test: '/settings/signal/test',
    unlink: '/settings/signal/unlink',
    unregister: '/settings/signal/unregister',
    settings: '/settings',
    templateSave: '/settings/signal/template',
    templateReset: '/settings/signal/template/reset'
};

// ─────────────────────────────────────────────────────────────────────────────
// E.164 Phone Number Validation & Normalization
// ─────────────────────────────────────────────────────────────────────────────

/**
 * E.164 phone number regex: +[country][subscriber]
 * - Starts with +
 * - First digit after + is 1-9 (no leading zero)
 * - Total 8-15 digits after +
 */
const E164_REGEX = /^\+[1-9]\d{7,14}$/;

/**
 * Normalize phone number to E.164 format.
 * Strips all non-digits and ensures exactly one leading +.
 * 
 * Examples:
 *   "+49 170 123 4567" → "+491701234567"
 *   "+1 (555) 123-4567" → "+15551234567"
 *   "++491701234567" → "+491701234567"
 * 
 * @param {string} phone - Phone number to normalize
 * @returns {string} E.164 normalized phone number
 */
export function normalizeE164(phone) {
    var cleaned = (phone || '').replace(/[^\d+]/g, '');
    // Strip all + signs, then re-add exactly one at the start if original had one
    var digits = cleaned.replace(/\+/g, '');
    if (cleaned.startsWith('+')) {
        return '+' + digits;
    }
    return digits;
}

/**
 * Validate E.164 phone number format
 * @param {string} phone - Phone number to validate
 * @returns {boolean} True if valid E.164 format
 */
export function isValidE164(phone) {
    return phone && E164_REGEX.test(normalizeE164(phone));
}

// ─────────────────────────────────────────────────────────────────────────────
// Race condition guards
// ─────────────────────────────────────────────────────────────────────────────

export const signalStatusGuard = createGuard();
export const qrCodeGuard = createGuard();

// Global busy state - prevents concurrent operations that could conflict
let signalBusy = false;

/**
 * Check if Signal operations are currently busy
 * @returns {boolean}
 */
export function isSignalBusy() {
    return signalBusy;
}

/**
 * Set busy state and disable action buttons during operations.
 * Only disables on busy=true. Re-enabling is handled by loadSignalStatus()
 * or the calling function to avoid enabling buttons that should stay disabled.
 * 
 * @param {boolean} busy
 */
export function setSignalBusy(busy) {
    signalBusy = busy;
    
    if (busy) {
        var actionButtons = [
            DOM.signalLinkConfirmBtn?.(),
            DOM.signalRegisterBtn?.(),
            DOM.signalVerifyBtn?.(),
            DOM.confirmUnlinkDeviceBtn?.(),
            DOM.confirmUnregisterPhoneBtn?.(),
            DOM.sendTestBtn?.()
        ].filter(Boolean);
        
        actionButtons.forEach(function (btn) {
            btn.disabled = true;
        });
    }
    // On busy=false: don't re-enable — let loadSignalStatus() or the
    // calling function determine which buttons should be active.
}

/**
 * Mask phone number for GDPR-compliant display.
 * Consistent format: +CC****XX (country code + last 2 digits)
 * Supports country codes from 1-4 digits (+1 USA, +49 DE, +358 FI, +1868 TT)
 * 
 * @param {string} phone - Phone number to mask
 * @returns {string} Masked phone number
 */
export function maskPhone(phone) {
    if (!phone || phone.length < 6) return '***';
    
    var result = phone.replace(/^(\+\d{1,4})\d+(\d{2})$/, '$1****$2');
    
    // If regex didn't match (result unchanged), mask everything after +CC
    if (result === phone) {
        return phone.slice(0, Math.min(3, phone.length)) + '****';
    }
    
    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
// QR Code Blob URL Management
// ─────────────────────────────────────────────────────────────────────────────

let _currentQrBlobUrl = null;

/**
 * Get current QR blob URL
 * @returns {string|null}
 */
export function getCurrentQrBlobUrl() {
    return _currentQrBlobUrl;
}

/**
 * Set current QR blob URL
 * @param {string|null} url
 */
export function setCurrentQrBlobUrl(url) {
    _currentQrBlobUrl = url;
}

/**
 * Revoke current QR blob URL and clear reference
 */
export function revokeCurrentQrBlobUrl() {
    if (_currentQrBlobUrl) {
        URL.revokeObjectURL(_currentQrBlobUrl);
        _currentQrBlobUrl = null;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Signal Enable Toggle
// ─────────────────────────────────────────────────────────────────────────────

// Callback when Signal panel is enabled
let _onPanelEnabled = null;

/**
 * Set callback to be called when Signal panel is enabled
 * @param {Function} callback
 */
export function setOnPanelEnabled(callback) {
    _onPanelEnabled = callback;
}

/**
 * Toggle Signal panel visibility based on enabled checkbox
 */
export function toggleSignalPanel() {
    const enabledEl = DOM.signalEnabled();
    const panel = DOM.signalPanel();
    const badges = DOM.signalStatusBadges();

    if (!enabledEl || !panel || !badges) return;

    if (enabledEl.checked) {
        panel.classList.add('show');
        badges.style.display = 'flex';
        
        // Call registered callback (usually loadSignalStatus)
        if (_onPanelEnabled) _onPanelEnabled();
    } else {
        panel.classList.remove('show');
        badges.innerHTML = '';
        badges.style.display = 'none';

        // Cleanup QR-code blob URL if the panel is disabled while a QR is shown
        revokeCurrentQrBlobUrl();
        var qrContainer = DOM.signalQrContainerLink?.();
        if (qrContainer) qrContainer.innerHTML = '';
    }
}

/**
 * Auto-set profile name if empty using current timestamp.
 * Used during link verification and QR code linking.
 */
export function autoSetProfileName() {
    var profileInput = DOM.signalProfileName?.() 
        || document.getElementById('signal-profile-name');
    
    if (!profileInput) return;
    
    if (!profileInput.value.trim()) {
        var currentTime = new Date().toISOString().slice(0, 16).replace('T', ' ');
        profileInput.value = 'justUp ' + currentTime;
    }
}

/**
 * Ensure Signal is enabled when settings are configured.
 * Updates UI only after backend confirms the change.
 */
export async function ensureSignalEnabled() {
    var enabledEl = DOM.signalEnabled();
    if (!enabledEl || enabledEl.checked) return;

    try {
        var resp = await apiFetch(SIGNAL_API.settings, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ signal_enabled: true })
        });
        
        if (resp.ok) {
            enabledEl.checked = true;
            toggleSignalPanel();
        } else {
            console.warn('Failed to enable Signal:', resp.status);
        }
    } catch (e) {
        console.warn('Failed to save signal_enabled:', e);
    }
}
