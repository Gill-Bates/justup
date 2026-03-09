//
// app/static/js/settings.main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.main.js - Main entry point for settings page
 * 
 * This module initializes all sub-modules and sets up the page.
 */

import { DOM } from './settings.core.js';
import { initSignal, toggleSignalPanel, loadSignalStatus } from './settings.signal.js';
import { initStorage } from './settings.storage.js';

// ─────────────────────────────────────────────────────────────────────────────
// Bootstrap Tooltips
// ─────────────────────────────────────────────────────────────────────────────

function initTooltips() {
    if (!window.bootstrap || !bootstrap.Tooltip) return;
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
        bootstrap.Tooltip.getOrCreateInstance(el);
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Tabs: Sync mobile nav and desktop tabs
// ─────────────────────────────────────────────────────────────────────────────

function initSettingsTabSync() {
    if (!document.body || document.body.dataset.settingsTabSyncBound === '1') return;
    document.body.dataset.settingsTabSyncBound = '1';

    document.addEventListener('shown.bs.tab', (e) => {
        const target = e.target?.dataset?.bsTarget;
        if (!target) return;

        // Sync mobile nav
        document.querySelectorAll('#settingsMobileNav .list-group-item').forEach(el => {
            el.classList.toggle('active', el.dataset.bsTarget === target);
        });

        // Sync desktop tabs
        document.querySelectorAll('#settingsTabs .nav-link').forEach(el => {
            const active = el.dataset.bsTarget === target;
            el.classList.toggle('active', active);
            el.setAttribute('aria-selected', active ? 'true' : 'false');
        });
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Initialize Registered Account Display
// ─────────────────────────────────────────────────────────────────────────────

function initRegisteredAccountDisplay(senderNumber) {
    if (!senderNumber) return;
    
    const modeSelection = DOM.signalModeSelection();
    if (modeSelection) {
        modeSelection.innerHTML = '';
        const alert = document.createElement('div');
        alert.className = 'alert alert-success py-2 mb-0';

        const icon = document.createElement('span');
        icon.className = 'material-icons align-middle me-1';
        icon.style.fontSize = '16px';
        icon.textContent = 'check_circle';

        const strong = document.createElement('strong');
        strong.textContent = 'Connected:';

        alert.appendChild(icon);
        alert.appendChild(strong);
        alert.appendChild(document.createTextNode(' ' + String(senderNumber)));
        modeSelection.appendChild(alert);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Page Initialization
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Initialize Bootstrap tooltips
    initTooltips();
	initSettingsTabSync();

    // Re-init modules after HTMX fragments load (tabs are lazy-loaded)
    document.body.addEventListener('htmx:afterSettle', () => {
        initTooltips();
        initSignal();
        initStorage();
    });
    
    // Initialize all modules (for non-HTMX loaded content)
    initSignal();
    initStorage();
    
    // Note: Settings and storage stats are loaded via HTMX fragments,
    // not via direct API calls (which would require auth)
    
    // Expose functions needed by inline handlers to global scope
    // (these are also exported by their respective modules)
    window.toggleSignalPanel = toggleSignalPanel;
    window.loadSignalStatus = loadSignalStatus;
});

// ─────────────────────────────────────────────────────────────────────────────
// Export for use in templates
// ─────────────────────────────────────────────────────────────────────────────

export { initRegisteredAccountDisplay };
