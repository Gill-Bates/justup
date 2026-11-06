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
