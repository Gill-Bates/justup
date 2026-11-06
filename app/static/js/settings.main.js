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
import { initPublicEgress } from './settings.public-egress.js';


function initTooltips() {
    if (!window.bootstrap || !bootstrap.Tooltip) return;
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
        bootstrap.Tooltip.getOrCreateInstance(el);
    });
}

// Tabs: horizontal scroll strip (keep the active tab visible)

function initSettingsTabStrip() {
    const tabs = document.getElementById('settingsTabs');
    if (!tabs || tabs.dataset.stripBound === '1') return;
    tabs.dataset.stripBound = '1';

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

    // Scroll only the strip (not the page) so the active tab is fully visible.
    const revealTab = (tab) => {
        if (!tab) return;
        const left = tab.offsetLeft - (tabs.clientWidth - tab.offsetWidth) / 2;
        tabs.scrollTo({ left: Math.max(0, left), behavior: reduceMotion.matches ? 'auto' : 'smooth' });
    };

    tabs.addEventListener('shown.bs.tab', (e) => revealTab(e.target));
    // Keyboard focus only: scrolling on a mouse press moves the tab away before mouseup and the click is lost.
    tabs.addEventListener('focusin', (e) => {
        if (e.target.matches?.('[role="tab"]:focus-visible')) revealTab(e.target);
    });
    // Initial tab may have been restored from the URL hash.
    const active = tabs.querySelector('[role="tab"].active');
    if (active) tabs.scrollLeft = Math.max(0, active.offsetLeft - (tabs.clientWidth - active.offsetWidth) / 2);
}


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


document.addEventListener('DOMContentLoaded', () => {
    initTooltips();
    initSettingsTabStrip();

    // Re-init modules after HTMX fragments load (tabs are lazy-loaded)
    document.body.addEventListener('htmx:afterSettle', () => {
        initTooltips();
        initSignal();
        initStorage();
        initTagInputs();
        initPublicEgress();
    });
    
    // Initialize all modules (for non-HTMX loaded content)
    initSignal();
    initStorage();
    initTagInputs();
    initPublicEgress();
    
    // Note: Settings and storage stats are loaded via HTMX fragments,
    // not via direct API calls (which would require auth)
    
    // Expose functions needed by inline handlers to global scope
    // (these are also exported by their respective modules)
    window.toggleSignalPanel = toggleSignalPanel;
    window.loadSignalStatus = loadSignalStatus;
});


export { initRegisteredAccountDisplay };
