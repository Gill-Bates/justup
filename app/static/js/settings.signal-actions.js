//
// app/static/js/settings.signal-actions.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.signal-actions.js - Signal actions and initialization
 * 
 * Handles:
 * - Test message sending
 * - Device unlinking
 * - Phone unregistration
 * - Message templates
 * - Module initialization
 */

import {
    DOM,
    withSpinner,
    getErrorMessage,
    notify,
    setSuccess,
    setError,
    setWarning,
    clearStatus,
    showModal,
    hideModal
} from './settings.core.js';

import {
    SIGNAL_API,
    isSignalBusy,
    setSignalBusy,
    toggleSignalPanel
} from './settings.signal-core.js';

import { loadSignalStatus } from './settings.signal-status.js';

// Use global apiFetch for authenticated API calls
const apiFetch = window.apiFetch;

// Dependency injection for status loader (used by registration module)
let _loadSignalStatus = loadSignalStatus;
export function setStatusLoader(fn) { _loadSignalStatus = fn; }
function refreshStatus() { if (_loadSignalStatus) _loadSignalStatus(); }

// ─────────────────────────────────────────────────────────────────────────────
// Test Message
// ─────────────────────────────────────────────────────────────────────────────

let lastTestMessageTime = 0;
const TEST_MESSAGE_COOLDOWN_MS = 10000;

/**
 * Send a test Signal message via local signal-cli
 * 
 * All notifications go through the spooler for consistent visibility.
 */
export async function sendTestMessage() {
    if (isSignalBusy()) {
        return;
    }

    const btn = DOM.sendTestBtn();

    // Check cooldown
    const now = Date.now();
    const timeSinceLastTest = now - lastTestMessageTime;
    if (timeSinceLastTest < TEST_MESSAGE_COOLDOWN_MS) {
        const remainingSec = Math.ceil((TEST_MESSAGE_COOLDOWN_MS - timeSinceLastTest) / 1000);
        notify('warning', `Please wait ${remainingSec}s before sending another test`);
        return;
    }

    lastTestMessageTime = Date.now();

    // Disable button briefly to prevent double-clicks
    if (btn) {
        btn.disabled = true;
        setTimeout(() => { btn.disabled = false; }, 2000);
    }

    try {
        const resp = await apiFetch(SIGNAL_API.test, {
            method: 'POST',
            body: null
        });

        if (resp.ok) {
            const data = await resp.json().catch(() => ({}));
            notify('success', data.message || 'Test message queued');
        } else {
            const data = await resp.json().catch(() => ({}));
            const msg = data.detail || 'Send failed';
            notify('error', msg);
        }
    } catch (e) {
        // Network error - API might be down
        console.warn('Test message send error:', e);
        notify('error', 'Network error - check server connection');
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Unlink Device (Simple disconnect)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Unlink Signal device - shows Bootstrap modal for confirmation
 */
export function signalUnlink() {
    const errorEl = DOM.unlinkDeviceError();
    if (errorEl) {
        errorEl.classList.add('d-none');
        errorEl.textContent = '';
    }

    const passwordEl = DOM.unlinkDevicePassword();
    if (passwordEl) {
        passwordEl.value = '';
    }

    showModal(DOM.unlinkDeviceModal());
}

/**
 * Initialize unlink device modal - handle confirm button click
 */
function initUnlinkDeviceModal() {
    const form = document.getElementById('unlink-device-form');
    const modal = DOM.unlinkDeviceModal();

    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        if (isSignalBusy()) {
            notify('warning', 'Another Signal operation is in progress');
            return;
        }

        const passwordEl = DOM.unlinkDevicePassword();
        const errorEl = DOM.unlinkDeviceError();
        const confirmBtn = DOM.confirmUnlinkDeviceBtn();
        const password = passwordEl?.value?.trim();
        const deleteCacheCheckbox = document.getElementById('unlink-delete-cache');
        const deleteCache = deleteCacheCheckbox?.checked ?? false;

        if (!password) {
            if (errorEl) {
                errorEl.textContent = 'Password is required';
                errorEl.classList.remove('d-none');
            }
            return;
        }

        if (errorEl) errorEl.classList.add('d-none');

        setSignalBusy(true);
        try {
            await withSpinner(confirmBtn, async () => {
                const resp = await apiFetch(SIGNAL_API.unlink, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password, delete_cache: deleteCache })
                });

                if (resp.ok) {
                    hideModal(DOM.unlinkDeviceModal());
                    notify('success', 'Signal device unlinked successfully');

                    const { loadSignalStatus } = await import('./settings.signal-status.js');
                    loadSignalStatus();
                } else {
                    const data = await resp.json().catch(() => ({}));
                    const error = data.detail || 'Failed to unlink device';
                    if (errorEl) {
                        errorEl.textContent = error;
                        errorEl.classList.remove('d-none');
                    }
                }
            }, 'Unlinking...');
        } catch (e) {
            console.error('Signal unlink error:', e);
            if (errorEl) {
                errorEl.textContent = 'Network error while unlinking device';
                errorEl.classList.remove('d-none');
            }
        } finally {
            setSignalBusy(false);
        }
    });

    if (modal) {
        modal.addEventListener('hidden.bs.modal', () => {
            const passwordEl = DOM.unlinkDevicePassword();
            const errorEl = DOM.unlinkDeviceError();
            if (passwordEl) passwordEl.value = '';
            if (errorEl) errorEl.classList.add('d-none');
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Unregister Phone (Full unregister with options)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Unregister phone number from Signal API (full unregister with options)
 */
export async function unregisterPhone() {
    if (isSignalBusy()) {
        notify('warning', 'Another Signal operation is in progress');
        return;
    }

    const password = DOM.unregisterPhonePassword()?.value?.trim();
    const errorEl = DOM.unregisterPhoneError();
    const btn = DOM.confirmUnregisterPhoneBtn();
    const deleteCacheCheckbox = document.getElementById('unregister-delete-cache');
    const deleteCache = deleteCacheCheckbox?.checked ?? true;

    if (!password) {
        setError(errorEl, 'Password is required');
        return;
    }

    clearStatus(errorEl);
    setSignalBusy(true);

    try {
        await withSpinner(btn, async () => {
            const resp = await apiFetch(SIGNAL_API.unregister, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password, delete_cache: deleteCache })
            });

            if (resp.ok) {
                hideModal(DOM.unregisterPhoneModal());
                notify('success', 'Device successfully unlinked');

                refreshStatus();
            } else {
                const error = await getErrorMessage(resp);

                const isNotRegistered = error && (
                    error.toLowerCase().includes('not registered') ||
                    error.toLowerCase().includes('not found') ||
                    error.toLowerCase().includes('unknown number')
                );

                if (isNotRegistered) {
                    hideModal(DOM.unregisterPhoneModal());
                    notify('info', 'Device was already unlinked');

                    refreshStatus();
                } else {
                    setError(errorEl, error);
                }
            }
        }, 'Unregistering...');
    } catch (e) {
        console.warn('Unregister phone error:', e);
        setError(errorEl, 'Network error: ' + e.message);
    } finally {
        setSignalBusy(false);
    }
}

/**
 * Initialize unregister modal
 */
function initUnregisterModal() {
    const modal = DOM.unregisterPhoneModal();
    const form = document.getElementById('unregister-phone-form');

    if (!modal) return;

    modal.addEventListener('show.bs.modal', () => {
        const pwInput = DOM.unregisterPhonePassword();
        if (pwInput) pwInput.value = '';
        const errorEl = DOM.unregisterPhoneError();
        if (errorEl) errorEl.classList.add('d-none');
    });

    if (form) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            unregisterPhone();
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Collapse Toggle
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize Signal setup collapse toggle icon
 */
export function initSignalSetupCollapse() {
    const collapse = DOM.signalSetupCollapse();
    const icon = DOM.signalSetupCollapseIcon();

    if (!collapse) return;

    if (icon) {
        const isShown = collapse.classList.contains('show');
        icon.textContent = isShown ? 'expand_more' : 'chevron_right';
    }

    collapse.addEventListener('shown.bs.collapse', () => {
        if (icon) icon.textContent = 'expand_more';
    });

    collapse.addEventListener('hidden.bs.collapse', () => {
        if (icon) icon.textContent = 'chevron_right';
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Captcha Input
// ─────────────────────────────────────────────────────────────────────────────

let _captchaAbort = null;

/**
 * Initialize captcha token input - auto-submit when valid token is pasted
 */
function initCaptchaInput() {
    if (_captchaAbort) {
        _captchaAbort.abort();
    }
    _captchaAbort = new AbortController();
    const signal = _captchaAbort.signal;

    document.addEventListener('paste', async (e) => {
        const target = e.target;
        if (target && target.id === 'signal-captcha-token') {
            const pastedData = (e.clipboardData || window.clipboardData)?.getData('text')?.trim();

            if (pastedData && pastedData.startsWith('signalcaptcha://')) {
                e.preventDefault();
                target.value = pastedData;
                target.dispatchEvent(new Event('input', { bubbles: true }));

                setTimeout(async () => {
                    if (!isSignalBusy()) {
                        const { signalRegister } = await import('./settings.signal-registration.js');
                        signalRegister();
                    }
                }, 50);
            }
        }
    }, { signal });

    document.addEventListener('input', async (e) => {
        const target = e.target;
        if (target && target.id === 'signal-captcha-token') {
            const value = target.value?.trim();
            if (value && value.startsWith('signalcaptcha://')) {
                clearTimeout(target._captchaDebounce);
                target._captchaDebounce = setTimeout(async () => {
                    if (!isSignalBusy()) {
                        const { signalRegister } = await import('./settings.signal-registration.js');
                        signalRegister();
                    }
                }, 100);
            }
        }
    }, { signal });
}

// ─────────────────────────────────────────────────────────────────────────────
// Enable Toggle
// ─────────────────────────────────────────────────────────────────────────────

let enableToggleAbort = null;

function initEnableToggle() {
    const enabledEl = DOM.signalEnabled();
    const panel = DOM.signalPanel();
    const badges = DOM.signalStatusBadges();
    if (!enabledEl) return;

    if (enableToggleAbort) {
        enableToggleAbort.abort();
    }
    enableToggleAbort = new AbortController();

    if (panel) {
        if (enabledEl.checked) {
            panel.classList.add('show');
            if (badges) badges.style.display = 'flex';
            refreshStatus();
        } else {
            panel.classList.remove('show');
            if (badges) badges.style.display = 'none';
        }
    }

    enabledEl.addEventListener('change', async function () {
        toggleSignalPanel();

        try {
            const resp = await apiFetch(SIGNAL_API.settings, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ signal_enabled: this.checked })
            });
            if (resp.ok) {
                notify('success', 'Signal ' + (this.checked ? 'enabled' : 'disabled'));
            } else {
                notify('error', 'Failed to save Signal setting');
            }
        } catch (e) {
            console.warn('Failed to save signal_enabled:', e);
            notify('error', 'Failed to save Signal setting');
        }
    }, { signal: enableToggleAbort.signal });
}

// ─────────────────────────────────────────────────────────────────────────────
// Signal Template Preview
// ─────────────────────────────────────────────────────────────────────────────

const TEMPLATE_PLACEHOLDERS = new Set([
    'target.name', 'target.host', 'target.group', 'target.check_type',
    'alert.status', 'alert.started_at', 'alert.resolved_at', 'alert.duration', 'alert.reason',
    'status.emoji', 'status.color', 'status.text',
    'downtime.minutes', 'downtime.human',
    'sla.threshold', 'sla.actual',
    'system.name', 'system.url', 'system.timestamp', 'system.github_url',
]);
const PLACEHOLDER_RE = /\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}/g;

function pad2(n) {
    return String(n).padStart(2, '0');
}

function fmtUtc(d) {
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} UTC`;
}

function sampleContext(variant) {
    const now = new Date();
    const started = new Date(now.getTime() - 12 * 60 * 1000);
    const resolved = new Date(now.getTime() - 1 * 60 * 1000);

    const isUp = variant === 'up';
    return {
        status: {
            emoji: isUp ? '🟢' : '🔴',
            color: isUp ? '#16a34a' : '#dc2626',
            text: isUp ? 'Service RESOLVED' : 'Service DOWN',
        },
        target: {
            name: 'Example API',
            host: 'api.example.com',
            group: 'Production',
            check_type: 'HTTP',
        },
        alert: {
            status: isUp ? 'RESOLVED' : 'OPEN',
            started_at: fmtUtc(started),
            resolved_at: isUp ? fmtUtc(resolved) : 'Ongoing',
            duration: isUp ? '11m' : '',
            reason: isUp ? 'HTTP 200' : 'HTTP 500',
        },
        downtime: {
            minutes: isUp ? '11' : '0',
            human: isUp ? '11m' : 'None',
        },
        sla: {
            threshold: '99.90%',
            actual: isUp ? '99.95%' : '99.40%',
        },
        system: {
            name: 'justUp',
            url: 'https://justup.local/ui',
            timestamp: fmtUtc(now),
            github_url: 'https://github.com/Gill-Bates/justup',
        },
    };
}

function getPlaceholderValue(ctx, dottedKey) {
    const parts = String(dottedKey || '').split('.');
    let value = ctx;
    for (const part of parts) {
        if (value && typeof value === 'object' && part in value) value = value[part];
        else return '';
    }
    return value == null ? '' : String(value);
}

function renderTemplatePreview(template, ctx) {
    if (!template || template.indexOf('{{') === -1) return template || '';
    return String(template).replace(PLACEHOLDER_RE, (_, key) => {
        const p = String(key || '').trim();
        if (!TEMPLATE_PLACEHOLDERS.has(p)) return '';
        return getPlaceholderValue(ctx, p);
    });
}

function updateTemplatePreviews() {
    const downEl = document.getElementById('signal-template-down');
    const upEl = document.getElementById('signal-template-up');
    const downPreviewEl = document.getElementById('signal-template-down-preview');
    const upPreviewEl = document.getElementById('signal-template-up-preview');

    if (downPreviewEl && downEl) {
        downPreviewEl.textContent = renderTemplatePreview(downEl.value, sampleContext('down'));
    }
    if (upPreviewEl && upEl) {
        upPreviewEl.textContent = renderTemplatePreview(upEl.value, sampleContext('up'));
    }
}

let templatePreviewTimer = null;
function schedulePreviewUpdate() {
    if (templatePreviewTimer) clearTimeout(templatePreviewTimer);
    templatePreviewTimer = setTimeout(updateTemplatePreviews, 120);
}

async function saveSignalTemplates() {
    const downEl = document.getElementById('signal-template-down');
    const upEl = document.getElementById('signal-template-up');
    const saveBtn = document.getElementById('save-signal-templates-btn');

    if (!downEl || !upEl || !saveBtn) return;

    try {
        await withSpinner(saveBtn, async () => {
            const resp = await apiFetch(SIGNAL_API.templateSave, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ down: downEl.value, up: upEl.value }),
            });

            if (resp.ok) {
                notify('success', 'Signal templates saved');
            } else {
                const data = await resp.json().catch(() => ({}));
                notify('error', data.detail || data.error || 'Failed to save templates');
            }
        }, 'Saving...');
    } catch (e) {
        notify('error', 'Network error while saving templates');
        console.warn('Signal template save failed:', e);
    }
}

async function resetSignalTemplates() {
    const downEl = document.getElementById('signal-template-down');
    const upEl = document.getElementById('signal-template-up');
    const resetBtn = document.getElementById('reset-signal-templates-btn');

    if (!downEl || !upEl || !resetBtn) return;

    try {
        await withSpinner(resetBtn, async () => {
            const resp = await apiFetch(SIGNAL_API.templateReset, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });

            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                notify('error', data.detail || data.error || 'Failed to reset templates');
                return;
            }

            const data = await resp.json().catch(() => null);
            if (data && typeof data === 'object') {
                if (typeof data.down === 'string') downEl.value = data.down;
                if (typeof data.up === 'string') upEl.value = data.up;
                updateTemplatePreviews();
            }
            notify('info', 'Signal templates reset to defaults');
        }, 'Resetting...');
    } catch (e) {
        notify('error', 'Network error while resetting templates');
        console.warn('Signal template reset failed:', e);
    }
}

function initTemplatePreview() {
    const downEl = document.getElementById('signal-template-down');
    const upEl = document.getElementById('signal-template-up');
    const saveBtn = document.getElementById('save-signal-templates-btn');
    const resetBtn = document.getElementById('reset-signal-templates-btn');

    if (!downEl || !upEl) return;

    downEl.addEventListener('input', schedulePreviewUpdate);
    upEl.addEventListener('input', schedulePreviewUpdate);

    if (saveBtn) saveBtn.addEventListener('click', saveSignalTemplates);
    if (resetBtn) resetBtn.addEventListener('click', resetSignalTemplates);

    updateTemplatePreviews();
}



// ─────────────────────────────────────────────────────────────────────────────
// Cleanup
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Cleanup event listeners and intervals for HTMX navigation
 */
export function cleanupSignal() {
    if (_captchaAbort) {
        _captchaAbort.abort();
        _captchaAbort = null;
    }
    if (enableToggleAbort) {
        enableToggleAbort.abort();
        enableToggleAbort = null;
    }
    if (templatePreviewTimer) {
        clearTimeout(templatePreviewTimer);
        templatePreviewTimer = null;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Initialize Module
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize all Signal-related functionality
 */
export async function initSignal() {
    initSignalSetupCollapse();
    initUnregisterModal();
    initUnlinkDeviceModal();
    initCaptchaInput();
    initEnableToggle();
    initTemplatePreview();

    // Import and initialize status module
    const statusModule = await import('./settings.signal-status.js');
    statusModule.initSignalStatus();

    // Register dependency injection handlers
    statusModule.setUnlinkHandler(signalUnlink);

    // Import registration functions for window exports and dependency injection
    const registration = await import('./settings.signal-registration.js');
    statusModule.setSelectModeHandler(registration.signalSelectMode);

    // Window exports for onclick handlers in HTML
    window.signalSelectMode = registration.signalSelectMode;
    window.signalBackToModeSelection = registration.signalBackToModeSelection;
    window.signalGenerateQrCode = registration.signalGenerateQrCode;
    window.signalConfirmLink = registration.signalConfirmLink;
    window.signalSetProfileNameLink = registration.signalSetProfileNameLink;
    window.signalRegister = registration.signalRegister;
    window.signalVerify = registration.signalVerify;
    window.signalSetProfileName = registration.signalSetProfileName;
    window.signalSkipProfile = registration.signalSkipProfile;
    window.sendTestMessage = sendTestMessage;
    window.sendSignalTestMessage = sendTestMessage;  // Alias for onclick
    window.unregisterPhone = unregisterPhone;
    window.signalUnlink = signalUnlink;
}
