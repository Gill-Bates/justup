//
// app/static/js/settings.core.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.core.js - Core utilities and shared helpers for settings page
 * 
 * This module provides:
 * - DOM element selectors (centralized IDs)
 * - Spinner/button state management
 * - Error parsing utilities
 * - Toast notifications
 * - FormData helpers
 * - LocalStorage wrapper with error handling
 */

// ─────────────────────────────────────────────────────────────────────────────
// DOM Element Selectors (centralized to avoid magic strings)
// ─────────────────────────────────────────────────────────────────────────────

// Lazy-cached selectors for performance
const _cache = {};

export function resetDomCache() {
	for (const k of Object.keys(_cache)) delete _cache[k];
}

// HTMX fragment swaps can replace DOM nodes, making cached references stale.
// Clear cache AFTER swaps so subsequent DOM getters re-resolve fresh elements.
function _bindHtmxCacheInvalidation() {
	const bind = () => {
		if (!document.body) return;
		document.body.addEventListener('htmx:afterSwap', () => resetDomCache());
	};
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', bind);
	} else {
		bind();
	}
}

try { _bindHtmxCacheInvalidation(); } catch { /* no-op */ }

export const DOM = {
    // Settings form
    settingsForm: () => _cache.settingsForm ??= document.getElementById('settings-form'),
    
    // General settings
    purgeEnabled: () => _cache.purgeEnabled ??= document.getElementById('purge_enabled'),
    useUtcDashboard: () => _cache.useUtcDashboard ??= document.getElementById('use_utc_dashboard'),
    geoipAutoUpdate: () => _cache.geoipAutoUpdate ??= document.getElementById('geoip_auto_update'),
    pdfExportEnabled: () => _cache.pdfExportEnabled ??= document.getElementById('pdf_export_enabled'),
    pdfPageSize: () => _cache.pdfPageSize ??= document.getElementById('pdf_page_size'),
    publicHostname: () => _cache.publicHostname ??= document.getElementById('public_hostname'),
    authRequired: () => _cache.authRequired ??= document.getElementById('auth_required'),
    
    // Password
    currentPassword: () => _cache.currentPassword ??= document.getElementById('current_password'),
    newPassword: () => _cache.newPassword ??= document.getElementById('new_password'),
    confirmPassword: () => _cache.confirmPassword ??= document.getElementById('confirm_password'),
    passwordChangeStatus: () => _cache.passwordChangeStatus ??= document.getElementById('password-change-status'),
    
    // Storage
    storageStats: () => _cache.storageStats ??= document.getElementById('storage-stats'),
    
    // Backup
    backupBtn: () => _cache.backupBtn ??= document.getElementById('backup-btn'),
    restoreBtn: () => _cache.restoreBtn ??= document.getElementById('restore-btn'),
    restoreFile: () => _cache.restoreFile ??= document.getElementById('restore-file'),
    lastBackupInfo: () => _cache.lastBackupInfo ??= document.getElementById('last-backup-info'),
    lastBackupTime: () => _cache.lastBackupTime ??= document.getElementById('last-backup-time'),
    
    // Signal
    signalEnabled: () => _cache.signalEnabled ??= document.getElementById('signal_enabled'),
    signalPanel: () => _cache.signalPanel ??= document.getElementById('signal-panel'),
    signalStatusBadges: () => _cache.signalStatusBadges ??= document.getElementById('signal-status-badges'),
    signalDeviceInfo: () => _cache.signalDeviceInfo ??= document.getElementById('signal-device-info'),
    signalSetupCollapse: () => _cache.signalSetupCollapse ??= document.getElementById('signal-setup-collapse'),
    signalSetupCollapseIcon: () => _cache.signalSetupCollapseIcon ??= document.getElementById('signal-setup-collapse-icon'),
    signalModeSelection: () => _cache.signalModeSelection ??= document.getElementById('signal-mode-selection'),
    signalFlowLink: () => _cache.signalFlowLink ??= document.getElementById('signal-flow-link'),
    signalFlowRegister: () => _cache.signalFlowRegister ??= document.getElementById('signal-flow-register'),
    signalQrContainerLink: () => _cache.signalQrContainerLink ??= document.getElementById('signal-qr-container-link'),
    signalLinkNumber: () => _cache.signalLinkNumber ??= document.getElementById('signal-link-number'),
    signalLinkConfirmBtn: () => _cache.signalLinkConfirmBtn ??= document.getElementById('signal-link-confirm-btn'),
    signalLinkStatus: () => _cache.signalLinkStatus ??= document.getElementById('signal-link-status'),
    signalLinkProfileSection: () => _cache.signalLinkProfileSection ??= document.getElementById('signal-link-profile-section'),
    signalLinkProfileName: () => _cache.signalLinkProfileName ??= document.getElementById('signal-link-profile-name'),
    signalLinkProfileStatus: () => _cache.signalLinkProfileStatus ??= document.getElementById('signal-link-profile-status'),
    signalRegisterNumber: () => _cache.signalRegisterNumber ??= document.getElementById('signal-register-number'),
    signalRegisterBtn: () => _cache.signalRegisterBtn ??= document.getElementById('signal-register-btn'),
    signalUseVoice: () => _cache.signalUseVoice ??= document.getElementById('signal-use-voice'),
    signalCaptchaSection: () => _cache.signalCaptchaSection ??= document.getElementById('signal-captcha-section'),
    signalCaptchaToken: () => _cache.signalCaptchaToken ??= document.getElementById('signal-captcha-token'),
    signalRegisterStatus: () => _cache.signalRegisterStatus ??= document.getElementById('signal-register-status'),
    signalVerifyCode: () => _cache.signalVerifyCode ??= document.getElementById('signal-verify-code'),
    signalVerifyBtn: () => _cache.signalVerifyBtn ??= document.getElementById('signal-verify-btn'),
    signalVerifyStatus: () => _cache.signalVerifyStatus ??= document.getElementById('signal-verify-status'),
    signalVerifyBoxes: () => _cache.signalVerifyBoxes ??= document.getElementById('signal-verify-boxes'),
    signalVerifyHint: () => _cache.signalVerifyHint ??= document.getElementById('signal-verify-hint'),
    signalProfileName: () => _cache.signalProfileName ??= document.getElementById('signal-profile-name'),
    signalSetNameBtn: () => _cache.signalSetNameBtn ??= document.getElementById('signal-set-name-btn'),
    signalProfileStatus: () => _cache.signalProfileStatus ??= document.getElementById('signal-profile-status'),
    signalUnregisterBtn: () => _cache.signalUnregisterBtn ??= document.getElementById('signal-unregister-btn'),
    sendTestBtn: () => _cache.sendTestBtn ??= document.getElementById('send-signal-test-btn'),
    testStatus: () => _cache.testStatus ??= document.getElementById('test-status'),
    
    // Signal steps
    signalRegStep1: () => _cache.signalRegStep1 ??= document.getElementById('signal-reg-step-1'),
    signalRegStep2: () => _cache.signalRegStep2 ??= document.getElementById('signal-reg-step-2'),
    signalRegStep3: () => _cache.signalRegStep3 ??= document.getElementById('signal-reg-step-3'),
    
    // SMTP
    smtpEnabled: () => _cache.smtpEnabled ??= document.getElementById('smtp_enabled'),
    smtpHost: () => _cache.smtpHost ??= document.getElementById('smtp_host'),
    smtpPort: () => _cache.smtpPort ??= document.getElementById('smtp_port'),
    smtpFrom: () => _cache.smtpFrom ??= document.getElementById('smtp_from'),
    smtpUser: () => _cache.smtpUser ??= document.getElementById('smtp_user'),
    smtpPassword: () => _cache.smtpPassword ??= document.getElementById('smtp_password'),
    smtpUseTls: () => _cache.smtpUseTls ??= document.getElementById('smtp_use_tls'),
    smtpStatusBadge: () => _cache.smtpStatusBadge ??= document.getElementById('smtp-status-badge'),
    
    // Modals
    purgeModal: () => _cache.purgeModal ??= document.getElementById('purgeModal'),
    purgePassword: () => _cache.purgePassword ??= document.getElementById('purge-password'),
    purgeError: () => _cache.purgeError ??= document.getElementById('purge-error'),
    confirmPurgeBtn: () => _cache.confirmPurgeBtn ??= document.getElementById('confirm-purge-btn'),
    purgeAlertsModal: () => _cache.purgeAlertsModal ??= document.getElementById('purgeAlertsModal'),
    purgeAlertsPassword: () => _cache.purgeAlertsPassword ??= document.getElementById('purge-alerts-password'),
    purgeAlertsError: () => _cache.purgeAlertsError ??= document.getElementById('purge-alerts-error'),
    confirmPurgeAlertsBtn: () => _cache.confirmPurgeAlertsBtn ??= document.getElementById('confirm-purge-alerts-btn'),
    restoreModal: () => _cache.restoreModal ??= document.getElementById('restoreModal'),
    restoreFilename: () => _cache.restoreFilename ??= document.getElementById('restore-filename'),
    confirmRestoreBtn: () => _cache.confirmRestoreBtn ??= document.getElementById('confirm-restore-btn'),
    authToggleModal: () => _cache.authToggleModal ??= document.getElementById('authToggleModal'),
    authTogglePassword: () => _cache.authTogglePassword ??= document.getElementById('auth-toggle-password'),
    authToggleError: () => _cache.authToggleError ??= document.getElementById('auth-toggle-error'),
    authToggleMessage: () => _cache.authToggleMessage ??= document.getElementById('auth-toggle-message'),
    confirmAuthToggleBtn: () => _cache.confirmAuthToggleBtn ??= document.getElementById('confirm-auth-toggle-btn'),
    unregisterPhoneModal: () => _cache.unregisterPhoneModal ??= document.getElementById('unregisterPhoneModal'),
    unregisterPhoneNumber: () => _cache.unregisterPhoneNumber ??= document.getElementById('unregister-phone-number'),
    unregisterPhonePassword: () => _cache.unregisterPhonePassword ??= document.getElementById('unregister-phone-password'),
    unregisterPhoneError: () => _cache.unregisterPhoneError ??= document.getElementById('unregister-phone-error'),
    deleteLocalData: () => _cache.deleteLocalData ??= document.getElementById('delete-local-data'),
    deleteAccount: () => _cache.deleteAccount ??= document.getElementById('delete-account'),
    confirmUnregisterPhoneBtn: () => _cache.confirmUnregisterPhoneBtn ??= document.getElementById('confirm-unregister-phone-btn'),
    // Unlink Device Modal
    unlinkDeviceModal: () => _cache.unlinkDeviceModal ??= document.getElementById('unlinkDeviceModal'),
    unlinkDevicePassword: () => _cache.unlinkDevicePassword ??= document.getElementById('unlink-device-password'),
    unlinkDeviceError: () => _cache.unlinkDeviceError ??= document.getElementById('unlink-device-error'),
    confirmUnlinkDeviceBtn: () => _cache.confirmUnlinkDeviceBtn ??= document.getElementById('confirm-unlink-device-btn'),
};


// ─────────────────────────────────────────────────────────────────────────────
// Button Spinner Utility
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Execute an async function while showing a spinner on a button
 * @param {HTMLButtonElement} btn - The button to show spinner on
 * @param {Function} fn - Async function to execute
 * @param {string} [loadingText] - Optional text to show while loading
 * @returns {Promise<any>} Result of fn
 */
export async function withSpinner(btn, fn, loadingText = null) {
    if (!btn) return fn();
    
    const originalHtml = btn.innerHTML;
    const wasDisabled = btn.disabled;
    
    btn.disabled = true;
    
    // Use DOM API instead of innerHTML to prevent XSS
    btn.replaceChildren();
    const spinner = document.createElement('span');
    spinner.className = 'spinner-border spinner-border-sm';
    btn.appendChild(spinner);
    if (loadingText) {
        btn.append(' ', loadingText);  // .append() is text-safe (no HTML parsing)
    }
    
    try {
        return await fn();
    } finally {
        btn.disabled = wasDisabled;
        btn.innerHTML = originalHtml;
    }
}


// ─────────────────────────────────────────────────────────────────────────────
// Error Parsing Utilities
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Render an API error `detail` (string or FastAPI 422 array) as readable text
 * @param {*} detail - Value of the response `detail` field
 * @returns {string} Message, or '' when nothing readable is present
 */
export function formatErrorDetail(detail) {
    return window.JuApiError.message(detail, Array.isArray(detail) ? 'Invalid input' : '');
}

/**
 * Extract error message from a Response object
 * @param {Response} resp - Fetch Response object
 * @returns {Promise<string>} Error message
 */
export async function getErrorMessage(resp) {
    return (await getErrorInfo(resp)).message;
}

/**
 * Message plus the machine-readable `detail.code` (or null) of an error response.
 * @param {Response} resp
 * @returns {Promise<{message: string, code: string|null}>}
 */
export async function getErrorInfo(resp) {
    // Read body as text once, then try JSON parsing
    try {
        const text = await resp.text();
        try {
            const data = JSON.parse(text);
            return {
                message: window.JuApiError.fromBody(data, text.trim() || 'Unknown error'),
                code: window.JuApiError.code(data?.detail),
            };
        } catch {
            // Not JSON - use plain text error message
            return { message: text.trim() || 'Unknown error', code: null };
        }
    } catch {
        return { message: 'Unknown error', code: null };
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Toast Notifications
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Show a toast notification
 * @param {'success'|'error'|'warning'|'info'} type - Notification type
 * @param {string} message - Message to display
 */
export function notify(type, message) {
    const api = window.justUpToast;
    const show = api && (api[type] || api.info);
    if (show) show(String(message ?? ''));
    else console.warn('Toast unavailable:', message);
}


// ─────────────────────────────────────────────────────────────────────────────
// FormData Helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Check if a checkbox is checked in FormData
 * @param {FormData} formData 
 * @param {string} name 
 * @returns {boolean}
 */
export const isChecked = (formData, name) => formData.has(name);

/**
 * Get text value from FormData with optional default
 * @param {FormData} formData 
 * @param {string} name 
 * @param {string} defaultValue 
 * @returns {string}
 */
export const getText = (formData, name, defaultValue = '') => formData.get(name)?.trim() || defaultValue;

/**
 * Get integer value from FormData with optional default
 * @param {FormData} formData 
 * @param {string} name 
 * @param {number} defaultValue 
 * @returns {number}
 */
export const getInt = (formData, name, defaultValue = 0) => {
    const val = parseInt(formData.get(name), 10);
    return Number.isFinite(val) ? val : defaultValue;
};


// ─────────────────────────────────────────────────────────────────────────────
// LocalStorage Wrapper
// ─────────────────────────────────────────────────────────────────────────────

export const storage = {
    /**
     * Get value from localStorage with default fallback
     * @param {string} key 
     * @param {any} defaultValue 
     * @returns {any}
     */
    get(key, defaultValue = null) {
        try {
            const value = localStorage.getItem(key);
            return value !== null ? value : defaultValue;
        } catch (e) {
            console.warn(`localStorage.get failed for "${key}":`, e);
            return defaultValue;
        }
    },
    
	    /**
	     * Set value in localStorage (value is stringified via String()).
	     * Use JSON.stringify / JSON.parse for structured data.
	     * @param {string} key 
	     * @param {any} value
	     * @returns {boolean} Success
	     */
	    set(key, value) {
        try {
            localStorage.setItem(key, String(value));
            return true;
        } catch (e) {
            console.warn(`localStorage.set failed for "${key}":`, e);
            return false;
        }
    },
    
    /**
     * Remove value from localStorage
     * @param {string} key 
     * @returns {boolean} Success
     */
    remove(key) {
        try {
            localStorage.removeItem(key);
            return true;
        } catch (e) {
            console.warn(`localStorage.remove failed for "${key}":`, e);
            return false;
        }
    }
};


// ─────────────────────────────────────────────────────────────────────────────
// Formatting Utilities
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Format bytes to human-readable string (supports up to TB)
 * @param {number} bytes 
 * @returns {string}
 */
export function formatBytes(bytes) {
	    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
	    const k = 1024;
	    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
	    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
	    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

/**
 * Format number with k/m suffixes for large numbers
 * @param {number} num 
 * @returns {string}
 */
export function formatNumber(num) {
    if (num >= 1_000_000) {
        return (num / 1_000_000).toFixed(1) + 'm';
    }
    if (num >= 1_000) {
        return (num / 1_000).toFixed(1) + 'k';
    }
    return num.toLocaleString();
}


// ─────────────────────────────────────────────────────────────────────────────
// Race Condition Guards
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Create a guard function to prevent concurrent execution
 * @returns {{guard: (fn: Function) => Promise<any>, isRunning: () => boolean}}
 */
export function createGuard() {
    let running = false;
    
    return {
        async guard(fn) {
            if (running) return;
            running = true;
            try {
                return await fn();
            } finally {
                running = false;
            }
        },
        isRunning: () => running
    };
}


// ─────────────────────────────────────────────────────────────────────────────
// Status Display Helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Set status message on an element
 * @param {HTMLElement} el 
 * @param {string} message 
 * @param {'success'|'danger'|'warning'|'muted'} type 
 */
export function setStatus(el, message, type = 'muted') {
    if (!el) return;
    el.textContent = message;
    el.className = `small text-${type}`;
}

export function setSuccess(el, message) {
    setStatus(el, `✓ ${message}`, 'success');
}

export function setError(el, message) {
    setStatus(el, `✗ ${message}`, 'danger');
}

export function setWarning(el, message) {
    if (!el) return;
    el.className = 'small';
    el.replaceChildren(window.JuWarning.create(message));
}

export function clearStatus(el) {
    if (!el) return;
    el.textContent = '';
    el.className = 'small';
}


// ─────────────────────────────────────────────────────────────────────────────
// Modal Helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Show a Bootstrap modal
 * @param {string|HTMLElement} modalOrId 
 */
export function showModal(modalOrId) {
    const el = typeof modalOrId === 'string' ? document.getElementById(modalOrId) : modalOrId;
    if (el && window.bootstrap) {
        bootstrap.Modal.getOrCreateInstance(el).show();
    }
}

/**
 * Hide a Bootstrap modal
 * @param {string|HTMLElement} modalOrId 
 */
export function hideModal(modalOrId) {
    const el = typeof modalOrId === 'string' ? document.getElementById(modalOrId) : modalOrId;
    if (el && window.bootstrap) {
        bootstrap.Modal.getInstance(el)?.hide();
    }
}

/**
 * Show/hide an error message div
 * @param {HTMLElement} el 
 * @param {string|null} message - null to hide
 */
export function showError(el, message) {
    if (!el) return;
    if (message) {
        el.textContent = message;
        el.style.display = 'block';
        el.classList.remove('d-none');
    } else {
        el.textContent = '';
        el.style.display = 'none';
        el.classList.add('d-none');
    }
}
