//
// app/static/js/settings.storage.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.storage.js - Storage, Backup/Restore, and Purge operations
 * 
 * Handles:
 * - Storage statistics display
 * - Backup creation and download
 * - Backup restoration
 * - TSDB purge
 * - Alerts purge
 * - Auth toggle
 * - Settings form submission
 */

import { downloadBlob } from './utils/blob-download.js';
import {
    DOM,
    withSpinner,
    getErrorMessage,
    notify,
    formatBytes,
    formatNumber,
    isChecked,
    getText,
    getInt,
    hideModal,
    showError
} from './settings.core.js';

import { toggleSignalPanel } from './settings.signal.js';

// Use global apiFetch for authenticated API calls
const apiFetch = window.apiFetch;

// ─────────────────────────────────────────────────────────────────────────────
// API Endpoints
// ─────────────────────────────────────────────────────────────────────────────

const SETTINGS_API = {
    storage: '/settings/storage',
    backup: '/settings/backup',
    restore: '/settings/restore',
    tsdbPurge: '/settings/tsdb/purge',
    alertsPurge: '/settings/alerts/purge',
    authToggle: '/settings/auth/toggle',
    password: '/me/password',
    settings: '/settings'
};

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────

let pendingRestoreFile = null;

/**
 * Global busy flag for destructive operations (Restore, Purge)
 * Prevents concurrent destructive operations that could corrupt data
 */
let destructiveOpBusy = false;

/**
 * Check if a destructive operation is in progress
 * @returns {boolean}
 */
function isDestructiveOpBusy() {
    return destructiveOpBusy;
}

/**
 * Set the destructive operation busy state
 * @param {boolean} busy
 */
function setDestructiveOpBusy(busy) {
    destructiveOpBusy = busy;
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper Functions
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Show a Bootstrap modal safely
 * Named explicitly to avoid shadowing any core imports
 * @param {HTMLElement} el - Modal element
 * @returns {Modal|null} Modal instance
 */
function showBootstrapModal(el) {
    if (!el || !window.bootstrap || !bootstrap.Modal) return null;
    const instance = bootstrap.Modal.getOrCreateInstance(el);
    instance.show();
    return instance;
}

function formatRangeValue(input) {
    const value = Number(input.value);
    if (input.dataset.unitPlural) {
        return value + ' ' + (value === 1 ? input.dataset.unitSingular : input.dataset.unitPlural);
    }
    return value + ' min';
}

function updateRangeActiveTick(input) {
    const wrapper = input?.closest('.range-wrapper');
    if (!wrapper) return;

    const min = Number(input.min);
    const step = Number(input.step) || 1;
    const activeIndex = Math.round((Number(input.value) - min) / step);
    wrapper.dataset.activeIndex = String(Math.max(0, activeIndex));
}

/**
 * Update SMTP status badge based on configuration
 */
function updateSmtpStatusBadge(data) {
    const badge = DOM.smtpStatusBadge();
    if (!badge) return;

    const enabled = data.smtp_enabled === true;
    const configured = data.smtp_host && data.smtp_from;

    badge.textContent = '';
    const pill = document.createElement('span');
    pill.className = 'badge';
    if (!enabled) {
        pill.classList.add('ju-status-badge--neutral');
        pill.textContent = 'Disabled';
    } else if (configured) {
        pill.classList.add('ju-status-badge--success');
        pill.textContent = 'Configured';
    } else {
        pill.classList.add('ju-status-badge--warning');
        pill.textContent = 'Not Configured';
    }
    badge.appendChild(pill);
}

function setSmtpFieldsetEnabled(enabled) {
    const fieldset = document.getElementById('smtp-fieldset');
    if (!fieldset) return;
    fieldset.disabled = !enabled;
}

/**
 * Show the STARTTLS switch for the current port. The backend always uses implicit TLS on one
 * port (data-implicit-port, sent by the server), so the switch is locked "on" there; elsewhere it shows the choice still being saved
 * (data-wanted-value) or else the stored one (data-stored-value), which only a confirmed save changes.
 */
function syncSmtpTlsControl() {
    const toggle = document.getElementById('smtp_use_tls');
    if (!toggle) return;
    const implicitPort = Number.parseInt(toggle.dataset.implicitPort, 10);
    const implicit = Number.parseInt(document.getElementById('smtp_port')?.value, 10) === implicitPort;
    const shown = (toggle.dataset.wantedValue ?? toggle.dataset.storedValue) !== 'false';
    toggle.disabled = implicit;
    toggle.checked = implicit || shown;

    const label = document.getElementById('smtp_use_tls_label');
    if (label) label.textContent = (implicit ? label.dataset.labelImplicit : label.dataset.labelStarttls) || '';
}

let smtpTlsChoiceSeq = 0;

/**
 * Save the STARTTLS choice. saveSetting() orders saves of one key, so the server ends on the last
 * choice; only the newest save settles the switch, and a failed one returns it to the stored state.
 */
async function saveSmtpTlsChoice(toggle) {
    const wanted = toggle.checked;
    const seq = ++smtpTlsChoiceSeq;
    toggle.dataset.wantedValue = String(wanted);
    const saved = await saveSetting('smtp_use_tls', wanted);
    if (saved) toggle.dataset.storedValue = String(wanted);
    if (seq !== smtpTlsChoiceSeq) return;
    delete toggle.dataset.wantedValue;
    syncSmtpTlsControl();
}

function updateSmtpUiFromDom() {
    const enabled = DOM.smtpEnabled()?.checked === true;
    const host = (DOM.smtpHost()?.value || '').trim();
    const from = (DOM.smtpFrom()?.value || '').trim();
    setSmtpFieldsetEnabled(enabled);
    syncSmtpTlsControl();
    updateSmtpStatusBadge({ smtp_enabled: enabled, smtp_host: host, smtp_from: from });
}

// ─────────────────────────────────────────────────────────────────────────────
// Storage Statistics
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Load and display storage statistics
 */
export async function loadStorageStats() {
    const container = DOM.storageStats();
    if (!container) return;

    container.innerHTML = `
        <div class="col-12 py-3">
            <div class="spinner-border spinner-border-sm text-body-secondary" role="status"></div>
        </div>
    `;

    try {
        const resp = await apiFetch(SETTINGS_API.storage);
        if (resp.ok) {
            const data = await resp.json();
            // Safe: all interpolated values are numeric (formatBytes/formatNumber)
            container.innerHTML = `
                <div class="col-12 col-md-4">
                    <div class="border rounded p-3 h-100 d-flex flex-column justify-content-center">
                        <div class="text-body-secondary small">SQL Database</div>
                        <div class="fs-5 fw-bold text-info-emphasis">${formatBytes(data.sql_size_bytes)}</div>
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="border rounded p-3 h-100 d-flex flex-column justify-content-center">
                        <div class="text-body-secondary small">TSDB Storage</div>
                        <div class="fs-5 fw-bold text-info-emphasis">${formatBytes(data.tsdb_size_bytes)}</div>
                        ${data.tsdb_saved_bytes > 0 ? `<div class="small text-body-secondary">${formatBytes(data.tsdb_saved_bytes)} saved by compression</div>` : ''}
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="border rounded p-3 h-100 d-flex flex-column justify-content-center">
                        <div class="text-body-secondary small">Data Points</div>
                        <div class="fs-5 fw-bold text-info-emphasis">${formatNumber(data.total_points)}</div>
                    </div>
                </div>
            `;
        } else {
            container.innerHTML = '<div class="col-12 text-danger">Failed to load storage stats</div>';
        }
    } catch (e) {
        console.warn('Failed to load storage stats:', e);
        container.innerHTML = '<div class="col-12 text-danger">Error loading stats</div>';
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Backup
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Update last backup display
 * @param {string} isoTimestamp 
 */
function updateLastBackupDisplay(isoTimestamp) {
    if (!isoTimestamp) return;

    const date = new Date(isoTimestamp);
    const formatted = date.toLocaleString(undefined, {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });

    const timeEl = DOM.lastBackupTime();
    const infoEl = DOM.lastBackupInfo();

    if (timeEl) timeEl.textContent = formatted;
    if (infoEl) infoEl.classList.remove('d-none');
}

/**
 * Create and download a backup
 */
export async function createBackup() {
    const btn = DOM.backupBtn();

    await withSpinner(btn, async () => {
        try {
            // POST is semantically correct for "create backup" and avoids
            // firewall/proxy issues with side-effect GETs
            const resp = await apiFetch(SETTINGS_API.backup, { method: 'POST' });

            if (resp.ok) {
                // Get filename from Content-Disposition header
                // Handles both quoted (filename="foo.tar.bz2") and unquoted formats
                const disposition = resp.headers.get('Content-Disposition');
                let filename = 'backup.tar.bz2';
                if (disposition) {
                    const match = disposition.match(/filename="?([^";]+)"?/);
                    if (match) filename = match[1].trim();
                }

                const blob = await resp.blob();
                downloadBlob(blob, filename);

                // Reload settings to get updated last_backup_at
                loadSettings();
            } else {
                const error = await getErrorMessage(resp);
                notify('error', error);
            }
        } catch (e) {
            console.warn('Create backup error:', e);
            notify('error', 'Network error: ' + e.message);
        }
    });
}

/**
 * Handle file selection for restore
 * @param {HTMLInputElement} input 
 */
export function restoreBackup(input) {
    const file = input.files[0];
    if (!file) return;

    pendingRestoreFile = file;

    const filenameEl = DOM.restoreFilename();
    if (filenameEl) filenameEl.textContent = file.name;

    const restoreModal = DOM.restoreModal();
    showBootstrapModal(restoreModal);

    // Clear input so same file can be selected again
    input.value = '';
}

/**
 * Execute the restore from pending file
 */
export async function executeRestore() {
    if (!pendingRestoreFile) return;

    // Prevent concurrent destructive operations
    if (isDestructiveOpBusy()) {
        notify('warning', 'Another operation is in progress. Please wait.');
        return;
    }

    const restoreModal = DOM.restoreModal();
    const confirmBtn = DOM.confirmRestoreBtn();
    const passwordInput = document.getElementById('restore-password');
    const errorDiv = document.getElementById('restore-error');

    const password = passwordInput?.value;
    if (!password) {
        if (errorDiv) {
            errorDiv.textContent = 'Please enter your password';
            errorDiv.classList.remove('d-none');
        }
        return;
    }

    if (errorDiv) errorDiv.classList.add('d-none');

    setDestructiveOpBusy(true);
    await withSpinner(confirmBtn, async () => {
        try {
            const formData = new FormData();
            formData.append('file', pendingRestoreFile);
            formData.append('password', password);

            const resp = await apiFetch(SETTINGS_API.restore, {
                method: 'POST',
                body: formData
            });

            if (resp.ok) {
                const data = await resp.json();
                hideModal(restoreModal);
                if (passwordInput) passwordInput.value = '';
                notify('success', data.message || 'Backup restored successfully! Reloading...');
                // Reload to reflect restored settings, targets, and state
                setTimeout(() => location.reload(), data.migrated ? 4000 : 1500); // a migration note needs reading time
            } else {
                const error = await getErrorMessage(resp);
                if (errorDiv) {
                    errorDiv.textContent = error;
                    errorDiv.classList.remove('d-none');
                } else {
                    hideModal(restoreModal);
                    notify('error', error);
                }
            }
        } catch (e) {
            console.warn('Restore backup error:', e);
            if (errorDiv) {
                errorDiv.textContent = 'Network error: ' + e.message;
                errorDiv.classList.remove('d-none');
            } else {
                hideModal(restoreModal);
                notify('error', 'Network error: ' + e.message);
            }
        } finally {
            setDestructiveOpBusy(false);
        }
    }, 'Restoring...');
}

// ─────────────────────────────────────────────────────────────────────────────
// Purge Operations
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize purge metrics handler
 */
function initPurgeMetrics() {
    const confirmBtn = DOM.confirmPurgeBtn();
    const passwordInput = DOM.purgePassword();
    const errorDiv = DOM.purgeError();
    const purgeModal = DOM.purgeModal();
    const form = document.getElementById('purge-form');

    // initStorage() re-runs on every HTMX settle; the modal markup is static
    if (!form || !confirmBtn || form.dataset.purgeBound === '1') return;
    form.dataset.purgeBound = '1';

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        // Prevent concurrent destructive operations
        if (isDestructiveOpBusy()) {
            showError(errorDiv, 'Another operation is in progress. Please wait.');
            return;
        }

        const password = passwordInput?.value;
        if (!password) {
            showError(errorDiv, 'Please enter your password.');
            return;
        }

        showError(errorDiv, null);

        setDestructiveOpBusy(true);
        await withSpinner(confirmBtn, async () => {
            try {
                const resp = await apiFetch(SETTINGS_API.tsdbPurge, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password })
                });

                if (resp.ok) {
                    hideModal(purgeModal);
                    notify('success', 'TSDB data deleted successfully.');
                    loadStorageStats();
                } else {
                    const error = await getErrorMessage(resp);
                    showError(errorDiv, error);
                }
            } catch (e) {
                console.warn('Purge metrics error:', e);
                showError(errorDiv, 'Network error.');
            } finally {
                setDestructiveOpBusy(false);
            }
        }, 'Deleting...');
    });

    // Clear modal state when hidden
    if (purgeModal) {
        purgeModal.addEventListener('hidden.bs.modal', () => {
            if (passwordInput) passwordInput.value = '';
            showError(errorDiv, null);
        });
    }
}

/**
 * Initialize purge alerts handler
 */
function initPurgeAlerts() {
    const confirmBtn = DOM.confirmPurgeAlertsBtn();
    const passwordInput = DOM.purgeAlertsPassword();
    const errorDiv = DOM.purgeAlertsError();
    const purgeModal = DOM.purgeAlertsModal();
    const form = document.getElementById('purge-alerts-form');

    if (!form || !confirmBtn || form.dataset.purgeAlertsBound === '1') return;
    form.dataset.purgeAlertsBound = '1';

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        // Prevent concurrent destructive operations
        if (isDestructiveOpBusy()) {
            showError(errorDiv, 'Another operation is in progress. Please wait.');
            return;
        }

        const password = passwordInput?.value;
        if (!password) {
            showError(errorDiv, 'Please enter your password.');
            return;
        }

        showError(errorDiv, null);

        setDestructiveOpBusy(true);
        await withSpinner(confirmBtn, async () => {
            try {
                const resp = await apiFetch(SETTINGS_API.alertsPurge, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password })
                });

                if (resp.ok) {
                    const data = await resp.json();
                    hideModal(purgeModal);
                    notify('success', `${data.deleted} alerts deleted successfully.`);
                } else {
                    const error = await getErrorMessage(resp);
                    showError(errorDiv, error);
                }
            } catch (e) {
                console.warn('Purge alerts error:', e);
                showError(errorDiv, 'Network error.');
            } finally {
                setDestructiveOpBusy(false);
            }
        }, 'Deleting...');
    });

    // Clear modal state when hidden
    if (purgeModal) {
        purgeModal.addEventListener('hidden.bs.modal', () => {
            if (passwordInput) passwordInput.value = '';
            showError(errorDiv, null);
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Auth Toggle
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize auth toggle handler
 */
function initAuthToggle() {
    const checkbox = DOM.authDisabled();
    const modal = DOM.authToggleModal();
    const passwordInput = DOM.authTogglePassword();
    const errorDiv = DOM.authToggleError();
    const messageEl = DOM.authToggleMessage();
    const confirmBtn = DOM.confirmAuthToggleBtn();

    if (!checkbox || !modal || checkbox.dataset.authToggleBound === '1') return;
    checkbox.dataset.authToggleBound = '1';

    checkbox.addEventListener('change', (e) => {
        e.preventDefault();
        const pendingAuthDisabled = checkbox.checked;
        modal.dataset.pendingDisabled = pendingAuthDisabled ? '1' : '0';

        if (pendingAuthDisabled) {
            messageEl.textContent = 'Are you sure you want to DISABLE authentication? All pages will be publicly accessible!';
        } else {
            messageEl.textContent = 'Are you sure you want to ENABLE authentication?';
        }

        // Revert checkbox until user confirms in modal (prevents premature state change)
        checkbox.checked = !pendingAuthDisabled;

        showBootstrapModal(modal);
    });

    const form = document.getElementById('auth-toggle-form');
    if (form && form.dataset.authToggleBound !== '1') {
        form.dataset.authToggleBound = '1';
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const password = passwordInput?.value;
            if (!password) {
                showError(errorDiv, 'Please enter your password.');
                return;
            }

            showError(errorDiv, null);

            await withSpinner(confirmBtn, async () => {
                try {
                    const pendingAuthDisabled = modal.dataset.pendingDisabled === '1';
                    const resp = await apiFetch(SETTINGS_API.authToggle, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password, disable: pendingAuthDisabled })
                    });

                    if (resp.ok) {
                        hideModal(modal);
                        const currentCheckbox = DOM.authDisabled();
                        if (currentCheckbox) currentCheckbox.checked = pendingAuthDisabled;
                        const warningIcon = document.getElementById('auth-disabled-warning');
                        if (warningIcon) {
                            warningIcon.style.display = pendingAuthDisabled ? '' : 'none';
                        }
                        notify('success', `Authentication ${pendingAuthDisabled ? 'disabled' : 'enabled'} successfully!`);
                    } else {
                        const error = await getErrorMessage(resp);
                        showError(errorDiv, error);
                    }
                } catch (e) {
                    console.warn('Auth toggle error:', e);
                    showError(errorDiv, 'Network error.');
                }
            }, 'Saving...');
        });
    }

    // Clear modal state when hidden
    if (modal.dataset.authToggleHiddenBound !== '1') {
        modal.dataset.authToggleHiddenBound = '1';
        modal.addEventListener('hidden.bs.modal', () => {
            if (passwordInput) passwordInput.value = '';
            showError(errorDiv, null);
            delete modal.dataset.pendingDisabled;
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Settings Loading
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Load settings from API
 */
export async function loadSettings() {
    try {
        const resp = await apiFetch(SETTINGS_API.settings);
        if (!resp.ok) {
            console.warn('Failed to load settings');
            return;
        }

        const data = await resp.json();

        const setChecked = (el, value) => { if (el) el.checked = value; };
        const setValue = (el, value) => { if (el) el.value = value; };

        // Signal
        setChecked(DOM.signalEnabled(), data.signal_enabled === true);

        // SMTP: a field missing from the response is left alone, never read as "off" or empty
        if (typeof data.smtp_enabled === 'boolean') setChecked(DOM.smtpEnabled(), data.smtp_enabled);
        if (typeof data.smtp_host === 'string') setValue(DOM.smtpHost(), data.smtp_host);
        if ('smtp_port' in data) setValue(DOM.smtpPort(), data.smtp_port || '');
        if (typeof data.smtp_from === 'string') setValue(DOM.smtpFrom(), data.smtp_from);
        if (typeof data.smtp_user === 'string') setValue(DOM.smtpUser(), data.smtp_user);
        // Never load the password; the server only reports whether one is stored
        const tlsToggle = document.getElementById('smtp_use_tls');
        if (tlsToggle && typeof data.smtp_use_tls === 'boolean') {
            tlsToggle.dataset.storedValue = String(data.smtp_use_tls);
        }
        if (typeof data.smtp_enabled === 'boolean') updateSmtpUiFromDom();

        // General
        setChecked(DOM.purgeEnabled(), Boolean(data.purge_enabled ?? true));
        setChecked(DOM.useUtcDashboard(), data.use_utc_dashboard === true);
        setChecked(DOM.geoipAutoUpdate(), Boolean(data.geoip_auto_update ?? true));
        setValue(DOM.pdfPageSize(), data.pdf_page_size || 'a4');
        if (typeof data.public_hostname === 'string') showPublicHostname(data.public_hostname);
        setChecked(DOM.authDisabled(), data.auth_disabled === true);

        // Metrics timeout slider
        const metricsTimeout = data.metrics_timeout_minutes || 5;
        setValue(DOM.metricsTimeoutMinutes(), metricsTimeout);
        updateRangeActiveTick(DOM.metricsTimeoutMinutes());
        const metricsTimeoutDisplay = document.getElementById('metrics_timeout_value');
        if (metricsTimeoutDisplay) {
            metricsTimeoutDisplay.textContent = `${metricsTimeout} min`;
        }

        // Session lifetime slider
        const sessionEl = document.getElementById('session_max_days');
        if (sessionEl) {
            setValue(sessionEl, data.session_max_days || 1);
            updateRangeActiveTick(sessionEl);
            const text = formatRangeValue(sessionEl);
            const sessionDisplay = document.getElementById('session_max_days_value');
            if (sessionDisplay) sessionDisplay.textContent = text;
            sessionEl.setAttribute('aria-valuetext', text);
        }

        // Store initial auth state for reload comparison on save
        const authEl = DOM.authDisabled();
        if (authEl) {
            authEl.dataset.initialState = String(data.auth_disabled === true);
        }

        // Toggle Signal panel based on enabled state
        toggleSignalPanel();

        if (data.last_backup_at) {
            updateLastBackupDisplay(data.last_backup_at);
        }
    } catch (e) {
        console.warn('Failed to load settings:', e);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Settings Auto-Save
// ─────────────────────────────────────────────────────────────────────────────

/** Open the collapsed settings section that contains `el` so errors are visible. */
function revealSettingsSection(el) {
    const details = el?.closest?.('details.settings-details');
    if (details && !details.open) details.open = true;
}

/**
 * Switch a write-only secret field (macro `secret_field`) between "stored" and "empty".
 * The mask is a fixed placeholder, so it never reflects the secret or its length.
 * @param {HTMLElement|null} wrapper - The `[data-secret-field]` container
 * @param {boolean} isSet - Whether a secret is stored on the server
 */
function setSecretState(wrapper, isSet) {
    if (!wrapper) return;
    const input = wrapper.querySelector('input');
    if (input) {
        input.value = '';
        input.placeholder = isSet ? '\u2022'.repeat(8) : '';
    }
    const help = wrapper.querySelector('[data-secret-help]');
    if (help) help.textContent = isSet ? wrapper.dataset.helpSet : wrapper.dataset.helpUnset;
    const clearButton = wrapper.querySelector('[data-secret-clear]');
    if (clearButton) clearButton.hidden = !isSet;
}

// Saves of one key run strictly in call order, so out-of-order responses cannot leave an older
// value stored. All unsettled saves are published for callers that must not read stale server state.
const saveChains = new Map();
const unsettledSaves = new Set();

/**
 * Save a single setting to the API
 * @param {string} key - Setting key
 * @param {any} value - Setting value
 * @returns {Promise<boolean>} Whether the server accepted the value (never rejects)
 */
function saveSetting(key, value) {
    const pending = (saveChains.get(key) ?? Promise.resolve()).then(() => sendSetting(key, value));
    saveChains.set(key, pending);
    unsettledSaves.add(pending);
    window.justUpSettingsSavePending = Promise.all(unsettledSaves);
    pending.then(() => {
        if (saveChains.get(key) === pending) saveChains.delete(key);
        unsettledSaves.delete(pending);
        window.justUpSettingsSavePending = unsettledSaves.size ? Promise.all(unsettledSaves) : null;
    });
    return pending;
}

async function sendSetting(key, value) {
    try {
        const resp = await apiFetch(SETTINGS_API.settings, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value })
        });

        if (resp.ok) {
            if (key === 'public_hostname') {
                const saved = await resp.json().catch(() => null);
                if (saved && typeof saved.public_hostname === 'string') showPublicHostname(saved.public_hostname, value);
                notify('success', 'Public hostname updated');
                return true;
            }
            notify('success', 'Settings saved');

            // Keep SMTP UI in sync without a full reload
            if (key === 'smtp_enabled' || key === 'smtp_host' || key === 'smtp_from') {
                updateSmtpUiFromDom();
            }

            // Special handling for auth_disabled - reload page
            if (key === 'auth_disabled') {
                const authEl = DOM.authDisabled();
                const oldAuth = authEl?.dataset.initialState === 'true';
                if (oldAuth !== value) {
                    setTimeout(() => location.reload(), 1000);
                }
            }
            return true;
        } else {
            const error = new Error('Failed to save setting');
            // A 422 carries a user-facing validation message (e.g. an invalid public hostname)
            if (resp.status === 422) {
                const payload = await resp.json().catch(() => null);
                if (typeof payload?.detail === 'string') error.userMessage = payload.detail;
            }
            throw error;
        }
    } catch (e) {
        console.error('Setting save error:', e);
        revealSettingsSection(document.querySelector(`[name="${CSS.escape(key)}"]`));
        notify('error', e.userMessage || 'Failed to save setting');
        return false;
    }
}

/**
 * Show the stored public hostname (as normalised by the server) and the "no URL" hint when it is empty.
 * `sent` is the value the confirmed request carried: a slower reply must not overwrite what the admin
 * typed since, so the field is only rewritten while it still shows that value.
 */
function showPublicHostname(value, sent) {
    const input = DOM.publicHostname();
    if (input) {
        if (sent === undefined || input.value.trim() === String(sent).trim()) input.value = value;
        input.defaultValue = value;  // blur auto-save compares against it
    }
    const hint = document.getElementById('public_hostname_missing_hint');
    if (hint) hint.hidden = value !== '';
}
/** Best guess at the public hostname: the address this page is loaded from, IPv6 brackets stripped. */
function detectPublicHostname() {
    return String(window.location.hostname || '').replace(/^\[|\]$/g, '');
}
async function applyDetectedPublicHostname() {
    const input = DOM.publicHostname();
    if (!input) return;
    const detected = detectPublicHostname();
    if (!detected) {
        notify('warning', 'Could not detect a hostname from the current address');
        return;
    }
    if (input.value.trim() === detected) return;
    input.value = detected;
    const saved = await saveSetting('public_hostname', detected);
    // Links are always https://<hostname> on the default port, whatever address this page was opened on.
    if (saved && (window.location.protocol !== 'https:' || window.location.port !== '')) {
        notify('warning', 'Notification links use https://' + detected + ' without a port. This page is opened via '
            + window.location.origin + ', so check that the link works.');
    }
}
function formatDurationSeconds(value) {
    const seconds = Number.parseInt(value, 10);
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    const units = [
        [86400, 'day'],
        [3600, 'hour'],
        [60, 'minute']
    ];
    for (const [unitSeconds, unitName] of units) {
        if (seconds >= unitSeconds && seconds % unitSeconds === 0) {
            const count = seconds / unitSeconds;
            return `${count} ${unitName}${count === 1 ? '' : 's'}`;
        }
    }
    return `${seconds} second${seconds === 1 ? '' : 's'}`;
}

function updateHumanizedDurations(form) {
    form.querySelectorAll('[data-humanized-output]').forEach((input) => {
        const output = document.getElementById(input.dataset.humanizedOutput);
        if (output) output.textContent = formatDurationSeconds(input.value);
    });
}

/**
 * Auto-save for the "Advanced" card (own form, same PATCH endpoint).
 */
function initAdvancedAutoSave() {
    document.querySelectorAll('form[data-settings-autosave]').forEach(bindAutoSaveForm);
}

function bindAutoSaveForm(form) {
    if (form.dataset.autoSaveBound === '1') return;
    form.dataset.autoSaveBound = '1';
    updateHumanizedDurations(form);
    form.addEventListener('submit', (e) => e.preventDefault());
    let apiKeySaveQueue = Promise.resolve(true);
    const queueApiKeySave = (value) => {
        const pendingSave = apiKeySaveQueue.then(() => saveSetting('ip_api_key', value));
        apiKeySaveQueue = pendingSave.catch(() => false);
        return pendingSave;
    };
    let clearingApiKey = false;
    form.addEventListener('input', (e) => {
        if (e.target.matches('[data-humanized-output]')) updateHumanizedDurations(form);
    });
    form.addEventListener('change', async (e) => {
        const t = e.target;
        if (!t.name) return;
        // auth_disabled is confirmed via modal (initAuthToggle) and saved there
        if (t.name === 'auth_disabled') return;
        if (t.type === 'hidden' && t.hasAttribute('data-tag-input-value')) {
            saveSetting(t.name, t.value);
        } else if (t.type === 'checkbox') {
            saveSetting(t.name, t.checked);
        } else if (t.type === 'range') {
            saveSetting(t.name, parseInt(t.value, 10));
        } else if (t.type === 'number') {
            if (t.value === '' || !t.checkValidity()) {
                revealSettingsSection(t);
                t.reportValidity();
                return;
            }
            saveSetting(t.name, parseInt(t.value, 10));
        } else if (t.name === 'ip_api_key') {
            const keyInput = t;
            const submittedKey = keyInput.value.trim();
            keyInput.disabled = true;
            const saved = await queueApiKeySave(submittedKey);
            // Reset the field only after the server accepted the secret
            if (saved) setSecretState(keyInput.closest('[data-secret-field]'), submittedKey !== '');
            if (!clearingApiKey) keyInput.disabled = false;
        } else if (t.type === 'text') {
            if (!t.checkValidity()) {
                revealSettingsSection(t);
                t.reportValidity();
                return;
            }
            saveSetting(t.name, t.value.trim());
        }
    });

    const clearKeyButton = form.querySelector('#clear-ip-api-key');
    if (clearKeyButton) {
        clearKeyButton.addEventListener('click', async () => {
            const keyInput = form.querySelector('#ip_api_key');
            clearingApiKey = true;
            clearKeyButton.disabled = true;
            if (keyInput) keyInput.disabled = true;
            const saved = await queueApiKeySave('');
            if (saved) {
                setSecretState(clearKeyButton.closest('[data-secret-field]'), false);
            } else {
                clearKeyButton.disabled = false;
            }
            clearingApiKey = false;
            if (keyInput) keyInput.disabled = false;
        });
    }
}

/**
 * Initialize auto-save for settings form fields using event delegation.
 * This handles dynamically loaded HTMX content without needing re-initialization.
 */
function initSettingsAutoSave() {
    const form = DOM.settingsForm();
    if (!form) return;
    initAdvancedAutoSave();

    // Prevent duplicate initialization (htmx:afterSettle fires on every tab switch)
    if (form.dataset.autoSaveBound === '1') return;
    form.dataset.autoSaveBound = '1';

    form.addEventListener('submit', (e) => e.preventDefault());

    // Track last values for blur-based inputs (to detect actual changes)
    const lastValues = new WeakMap();

    // Event delegation for 'change' events (checkboxes, selects, range inputs)
    form.addEventListener('change', (e) => {
        const target = e.target;
        if (!target.name) return;

        // Non-setting fields
        if (target.name === 'smtp_test_recipient') return;

        // Skip elements handled elsewhere
        if (target.closest('form[data-settings-autosave]')) return;
        if (target.closest('#signal-setup-collapse')) return;
        if (target.closest('#signal-flow-link')) return;
        if (target.closest('#signal-flow-register')) return;
        if (target.name === 'auth_disabled') return;  // Handled by initAuthToggle with modal

        // Tag input: the component only fires 'change' for a valid, changed value
        if (target.type === 'hidden' && target.hasAttribute('data-tag-input-value')) {
            saveSetting(target.name, target.value);
            return;
        }

        // Checkboxes
        if (target.type === 'checkbox') {
            if (target.name === 'smtp_use_tls') {
                saveSmtpTlsChoice(target);
                return;
            }
            if (target.name === 'smtp_enabled') {
                setSmtpFieldsetEnabled(target.checked);
            }
            saveSetting(target.name, target.checked);
            return;
        }

        // Select elements
        if (target.tagName === 'SELECT') {
            saveSetting(target.name, target.value);
            return;
        }

        // Range inputs
        if (target.type === 'range') {
            const value = parseInt(target.value, 10);
            saveSetting(target.name, value);
            return;
        }
    });

    form.addEventListener('click', (e) => {
        if (e.target.closest?.('#public_hostname_detect')) applyDetectedPublicHostname();
    });
    // Explicit removal of a stored SMTP secret (ip-api key has its own queued handler)
    form.addEventListener('click', async (e) => {
        const button = e.target.closest?.('[data-secret-clear="smtp_password"]');
        if (!button) return;
        button.disabled = true;
        const saved = await saveSetting('smtp_password', '');
        button.disabled = false;
        if (saved) setSecretState(button.closest('[data-secret-field]'), false);
    });

    // Event delegation for 'input' events (range slider real-time feedback)
    form.addEventListener('input', (e) => {
        const target = e.target;
        if (target.name === 'smtp_port') syncSmtpTlsControl();
        if (target.type === 'range' && target.name) {
            updateRangeActiveTick(target);
            const valueDisplay = document.getElementById(target.name + '_value');
            const text = formatRangeValue(target);
            if (valueDisplay) {
                valueDisplay.textContent = text;
            }
            target.setAttribute('aria-valuetext', text);
        }
    });

    // Event delegation for 'blur' events (number inputs, text inputs)
    form.addEventListener('blur', async (e) => {
        const target = e.target;
        if (!target.name) return;

        // Non-setting fields
        if (target.name === 'smtp_test_recipient') return;

        // Skip elements handled elsewhere
        if (target.closest('form[data-settings-autosave]')) return;
        if (target.closest('#signal-setup-collapse')) return;
        if (target.closest('#signal-flow-link')) return;
        if (target.closest('#signal-flow-register')) return;
        // SMTP password: allow setting when user provides a value
        if (target.type === 'password' || target.autocomplete?.includes('password')) {
            if (target.name === 'smtp_password') {
                const next = (target.value || '').trim();
                if (next) {
                    const saved = await saveSetting('smtp_password', next);
                    // Clear only after a successful save and if the user did not type meanwhile
                    if (saved && (target.value || '').trim() === next) {
                        setSecretState(target.closest('[data-secret-field]'), true);
                    }
                }
            }
            return;
        }

        // Number inputs
        if (target.type === 'number') {
            const lastValue = lastValues.get(target) ?? target.defaultValue;
            if (target.value !== lastValue) {
                // Clamp to valid range
                const parsedMin = parseInt(target.min, 10);
                const parsedMax = parseInt(target.max, 10);
                const min = Number.isNaN(parsedMin) ? 1 : parsedMin;
                const max = Number.isNaN(parsedMax) ? 60 : parsedMax;
                const parsedValue = parseInt(target.value, 10);
                let value = Number.isNaN(parsedValue) ? min : parsedValue;
                value = Math.max(min, Math.min(max, value));
                target.value = value;
                const sent = String(value);
                // Remember the value only after a confirmed save so a failure is retried on the next blur
                if (await saveSetting(target.name, value)) {
                    lastValues.set(target, sent);
                }
            }
            return;
        }

        // Text inputs
        if (target.type === 'text' || target.type === 'tel' || target.type === 'email') {
            const lastValue = lastValues.get(target) ?? target.defaultValue;
            if (target.value !== lastValue) {
                const sent = target.value;
                if (await saveSetting(target.name, sent.trim())) {
                    lastValues.set(target, sent);
                }
            }
        }
    }, true);  // Use capture phase for blur events
}

// ─────────────────────────────────────────────────────────────────────────────
// Restore Modal Handler
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize restore modal handlers
 */
function initRestoreModal() {
    const modal = DOM.restoreModal();
    const form = document.getElementById('restore-form');

    if (form && form.dataset.restoreBound !== '1') {
        form.dataset.restoreBound = '1';
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            executeRestore();
        });
    }

    if (modal && modal.dataset.restoreBound !== '1') {
        modal.dataset.restoreBound = '1';
        modal.addEventListener('hidden.bs.modal', () => {
            pendingRestoreFile = null;
            const passwordInput = document.getElementById('restore-password');
            const errorDiv = document.getElementById('restore-error');
            if (passwordInput) passwordInput.value = '';
            if (errorDiv) errorDiv.classList.add('d-none');
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Initialize Module
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize all storage-related functionality
 */
export function initStorage() {
    initSettingsAutoSave();
    initPurgeMetrics();
    initPurgeAlerts();
    initRestoreModal();
    initAuthToggle();

    // Expose functions to global scope (for onclick handlers)
    window.createBackup = createBackup;
    window.restoreBackup = restoreBackup;
    // Ensure SMTP UI is consistent on initial load / swaps
    updateSmtpUiFromDom();
}
