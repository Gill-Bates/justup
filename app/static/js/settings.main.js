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
import { initTagInputs } from './utils/tag-input.js';

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
// Tabs: horizontal scroll strip (edge fade state, keep active tab visible)
// ─────────────────────────────────────────────────────────────────────────────

function initSettingsTabStrip() {
    const tabs = document.getElementById('settingsTabs');
    if (!tabs || tabs.dataset.stripBound === '1') return;
    tabs.dataset.stripBound = '1';

    const EDGE_SLOP_PX = 1;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

    const updateEdges = () => {
        const maxScroll = tabs.scrollWidth - tabs.clientWidth;
        if (maxScroll <= EDGE_SLOP_PX) {
            tabs.classList.add('is-at-start', 'is-at-end');
            return;
        }
        tabs.classList.toggle('is-at-start', tabs.scrollLeft <= EDGE_SLOP_PX);
        tabs.classList.toggle('is-at-end', tabs.scrollLeft >= maxScroll - EDGE_SLOP_PX);
    };

    // Scroll only the strip (not the page) so the active tab is fully visible.
    const revealTab = (tab) => {
        if (!tab) return;
        const left = tab.offsetLeft - (tabs.clientWidth - tab.offsetWidth) / 2;
        tabs.scrollTo({ left: Math.max(0, left), behavior: reduceMotion.matches ? 'auto' : 'smooth' });
    };

    tabs.addEventListener('scroll', updateEdges, { passive: true });
    window.addEventListener('resize', updateEdges);
    tabs.addEventListener('shown.bs.tab', (e) => revealTab(e.target));
    tabs.addEventListener('focusin', (e) => {
        if (e.target.matches?.('[role="tab"]')) revealTab(e.target);
    });
    updateEdges();
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
	initSettingsTabStrip();

    // Re-init modules after HTMX fragments load (tabs are lazy-loaded)
    document.body.addEventListener('htmx:afterSettle', () => {
        initTooltips();
        initSignal();
        initStorage();
        initTagInputs();
    });
    
    // Initialize all modules (for non-HTMX loaded content)
    initSignal();
    initStorage();
    initTagInputs();
    
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
