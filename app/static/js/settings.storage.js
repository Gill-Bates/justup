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
 * - Password change
 * - Settings form submission
 */

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
		pill.classList.add('bg-secondary');
		pill.textContent = 'Disabled';
	} else if (configured) {
		pill.classList.add('bg-success');
		pill.textContent = 'Configured';
	} else {
		pill.classList.add('bg-warning', 'text-dark');
		pill.textContent = 'Not Configured';
	}
	badge.appendChild(pill);
}

function setSmtpFieldsetEnabled(enabled) {
    const fieldset = document.getElementById('smtp-fieldset');
    if (!fieldset) return;
    fieldset.disabled = !enabled;
}

function updateSmtpUiFromDom() {
    const enabled = DOM.smtpEnabled()?.checked === true;
    const host = (DOM.smtpHost()?.value || '').trim();
    const from = (DOM.smtpFrom()?.value || '').trim();
    setSmtpFieldsetEnabled(enabled);
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
            <div class="spinner-border spinner-border-sm text-secondary" role="status"></div>
        </div>
    `;

    try {
        const resp = await apiFetch(SETTINGS_API.storage);
        if (resp.ok) {
            const data = await resp.json();
            // Safe: all interpolated values are numeric (formatBytes/formatNumber)
            container.innerHTML = `
                <div class="col-4">
                    <div class="border rounded p-3">
                        <div class="text-muted small">SQL Database</div>
                        <div class="fs-5 fw-bold text-info">${formatBytes(data.sql_size_bytes)}</div>
                    </div>
                </div>
                <div class="col-4">
                    <div class="border rounded p-3">
                        <div class="text-muted small">TSDB Storage</div>
                        <div class="fs-5 fw-bold text-info">${formatBytes(data.tsdb_size_bytes)}</div>
                    </div>
                </div>
                <div class="col-4">
                    <div class="border rounded p-3">
                        <div class="text-muted small">Data Points</div>
                        <div class="fs-5 fw-bold text-info">${formatNumber(data.total_points)}</div>
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
    const formatted = date.toLocaleString('de-DE', {
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

                // Download the file
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);

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

    // Store file and show modal
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

    // Validate password
    const password = passwordInput?.value?.trim();
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
                setTimeout(() => location.reload(), 1500);
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

    if (!form || !confirmBtn) return;

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

    if (!form || !confirmBtn) return;

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

    if (!checkbox || !modal) return;

    checkbox.addEventListener('change', (e) => {
        e.preventDefault();
		const pendingAuthDisabled = checkbox.checked;
		modal.dataset.pendingDisabled = pendingAuthDisabled ? '1' : '0';

        if (pendingAuthDisabled) {
            messageEl.textContent = 'Are you sure you want to DISABLE authentication? All pages will be publicly accessible!';
            modal.querySelector('.modal-header').classList.remove('bg-success');
            modal.querySelector('.modal-header').classList.add('bg-warning');
        } else {
            messageEl.textContent = 'Are you sure you want to ENABLE authentication?';
            modal.querySelector('.modal-header').classList.remove('bg-warning');
            modal.querySelector('.modal-header').classList.add('bg-success');
        }

        // Revert checkbox until user confirms in modal (prevents premature state change)
        checkbox.checked = !pendingAuthDisabled;

		showBootstrapModal(modal);
    });

    const form = document.getElementById('auth-toggle-form');
    if (form) {
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
                        checkbox.checked = pendingAuthDisabled;
                        // Update warning icon visibility
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
    modal.addEventListener('hidden.bs.modal', () => {
        if (passwordInput) passwordInput.value = '';
        showError(errorDiv, null);
		delete modal.dataset.pendingDisabled;
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Password Change
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialize password change handler
 */
function initPasswordChange() {
    const btn = DOM.changePasswordBtn();
    if (!btn) return;

    btn.addEventListener('click', async () => {
        const statusEl = DOM.passwordChangeStatus();
        const currentPw = DOM.currentPassword()?.value;
        const newPw = DOM.newPassword()?.value;
        const confirmPw = DOM.confirmPassword()?.value;

        // Validation
        if (!currentPw || !newPw || !confirmPw) {
            statusEl.textContent = 'Please fill in all fields';
            statusEl.className = 'mt-2 small text-danger';
            return;
        }
        if (newPw.length < 6) {
            statusEl.textContent = 'New password must be at least 6 characters';
            statusEl.className = 'mt-2 small text-danger';
            return;
        }
        if (newPw !== confirmPw) {
            statusEl.textContent = 'Passwords do not match';
            statusEl.className = 'mt-2 small text-danger';
            return;
        }

        statusEl.textContent = '';

        await withSpinner(btn, async () => {
            try {
                const resp = await apiFetch(SETTINGS_API.password, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        current_password: currentPw,
                        new_password: newPw
                    })
                });

                if (resp.ok) {
                    statusEl.textContent = '✓ Password changed successfully';
                    statusEl.className = 'mt-2 small text-success';
                    // No toast - inline status is sufficient for form-local feedback
                    
                    // Clear fields
                    DOM.currentPassword().value = '';
                    DOM.newPassword().value = '';
                    DOM.confirmPassword().value = '';
                } else {
                    const error = await getErrorMessage(resp);
                    statusEl.textContent = `✗ ${error}`;
                    statusEl.className = 'mt-2 small text-danger';
                    // No toast - inline status is sufficient for form-local feedback
                }
            } catch (e) {
                console.warn('Password change error:', e);
                statusEl.textContent = '✗ Network error';
                statusEl.className = 'mt-2 small text-danger';
            }
        });
    });
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
        
        // Set form values
        const setChecked = (el, value) => { if (el) el.checked = value; };
        const setValue = (el, value) => { if (el) el.value = value; };
        
        // Signal
        setChecked(DOM.signalEnabled(), data.signal_enabled === true);
        
        // SMTP
        setChecked(DOM.smtpEnabled(), data.smtp_enabled === true);
        setValue(DOM.smtpHost(), data.smtp_host || '');
        setValue(DOM.smtpPort(), data.smtp_port || '');
        setValue(DOM.smtpFrom(), data.smtp_from || '');
        setValue(DOM.smtpUser(), data.smtp_user || '');
        // Don't load password - security best practice (show placeholder instead)
        setChecked(DOM.smtpUseTls(), data.smtp_use_tls === true);
        updateSmtpStatusBadge(data);
        
        // General
        setChecked(DOM.purgeEnabled(), Boolean(data.purge_enabled ?? true));
        setChecked(DOM.useUtcDashboard(), data.use_utc_dashboard === true);
        setChecked(DOM.downsampleEnabled(), Boolean(data.downsample_enabled ?? true));
        setChecked(DOM.geoipAutoUpdate(), Boolean(data.geoip_auto_update ?? true));
        setValue(DOM.pdfPageSize(), data.pdf_page_size || 'a4');
        setChecked(DOM.authDisabled(), data.auth_disabled === true);
        
        // Metrics timeout slider
        const metricsTimeout = data.metrics_timeout_minutes || 5;
        setValue(DOM.metricsTimeoutMinutes(), metricsTimeout);
        const metricsTimeoutDisplay = document.getElementById('metrics_timeout_value');
        if (metricsTimeoutDisplay) {
            metricsTimeoutDisplay.textContent = metricsTimeout;
        }

        // Store initial auth state for reload comparison on save
        const authEl = DOM.authDisabled();
        if (authEl) {
            authEl.dataset.initialState = String(data.auth_disabled === true);
        }

        // Toggle Signal panel based on enabled state
        toggleSignalPanel();

        // Update last backup display
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

/**
 * Save a single setting to the API
 * @param {string} key - Setting key
 * @param {any} value - Setting value
 */
async function saveSetting(key, value) {
    try {
        const resp = await apiFetch(SETTINGS_API.settings, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value })
        });

        if (resp.ok) {
            // Show success toast
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
        } else {
            throw new Error('Failed to save setting');
        }
    } catch (e) {
        console.error('Setting save error:', e);
        notify('error', 'Failed to save setting');
    }
}

/**
 * Initialize auto-save for settings form fields using event delegation.
 * This handles dynamically loaded HTMX content without needing re-initialization.
 */
function initSettingsAutoSave() {
    const form = DOM.settingsForm();
    if (!form) return;
    
    // Prevent duplicate initialization (htmx:afterSettle fires on every tab switch)
    if (form.dataset.autoSaveBound === '1') return;
    form.dataset.autoSaveBound = '1';

    // Prevent default form submission
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
        if (target.closest('#signal-setup-collapse')) return;
        if (target.closest('#signal-flow-link')) return;
        if (target.closest('#signal-flow-register')) return;
        if (target.name === 'auth_disabled') return;  // Handled by initAuthToggle with modal

        // Checkboxes
        if (target.type === 'checkbox') {
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

    // Event delegation for 'input' events (range slider real-time feedback)
    form.addEventListener('input', (e) => {
        const target = e.target;
        if (target.type === 'range' && target.name) {
            const valueDisplay = document.getElementById(target.name + '_value');
            if (valueDisplay) {
                valueDisplay.textContent = target.value + ' min';
            }
        }
    });

    // Event delegation for 'blur' events (number inputs, text inputs)
    form.addEventListener('blur', (e) => {
        const target = e.target;
        if (!target.name) return;

        // Non-setting fields
        if (target.name === 'smtp_test_recipient') return;

        // Skip elements handled elsewhere
        if (target.closest('#signal-setup-collapse')) return;
        if (target.closest('#signal-flow-link')) return;
        if (target.closest('#signal-flow-register')) return;
        // SMTP password: allow setting when user provides a value
        if (target.type === 'password' || target.autocomplete?.includes('password')) {
            if (target.name === 'smtp_password') {
                const next = (target.value || '').trim();
                if (next) {
                    saveSetting('smtp_password', next);
                    // Clear after save to avoid leaving secrets in the DOM
                    target.value = '';
                }
            }
            return;
        }

        // Number inputs
        if (target.type === 'number') {
            const lastValue = lastValues.get(target) ?? target.defaultValue;
            if (target.value !== lastValue) {
                lastValues.set(target, target.value);
                // Clamp to valid range
                const min = parseInt(target.min, 10) || 1;
                const max = parseInt(target.max, 10) || 60;
                let value = parseInt(target.value, 10) || min;
                value = Math.max(min, Math.min(max, value));
                target.value = value;
                saveSetting(target.name, value);
            }
            return;
        }

        // Text inputs
        if (target.type === 'text' || target.type === 'tel' || target.type === 'email') {
            const lastValue = lastValues.get(target) ?? target.defaultValue;
            if (target.value !== lastValue) {
                lastValues.set(target, target.value);
                saveSetting(target.name, target.value.trim());
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
    
    if (form) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            executeRestore();
        });
    }
    
    if (modal) {
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
    initPasswordChange();
    
    // Expose functions to global scope (for onclick handlers)
    window.createBackup = createBackup;
    window.restoreBackup = restoreBackup;
    // Ensure SMTP UI is consistent on initial load / swaps
    updateSmtpUiFromDom();
}
