//
// app/static/js/settings.signal-status.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.signal-status.js - Signal status loading and display
 * 
 * Handles:
 * - Status badge display
 * - Device info updates
 * - Linked account section
 * - Mode selection reset
 */

import { DOM, escapeAttr } from './settings.core.js';
import { 
    SIGNAL_API, 
    signalStatusGuard, 
    isSignalBusy,
    maskPhone,
    setOnPanelEnabled
} from './settings.signal-core.js';

// Use global apiFetch for authenticated API calls
const apiFetch = window.apiFetch;

// Dependency injection for circular dependency resolution
let _signalUnlinkFn = null;
let _signalSelectModeFn = null;

/**
 * Set the unlink handler (called from signal-actions.js init)
 * @param {Function} fn
 */
export function setUnlinkHandler(fn) {
    _signalUnlinkFn = fn;
}

/**
 * Set the mode selection handler (called from signal-registration.js init)
 * @param {Function} fn
 */
export function setSelectModeHandler(fn) {
    _signalSelectModeFn = fn;
}

/**
 * Initialize Signal status module
 * Called from signal-actions.js initSignal()
 */
export function initSignalStatus() {
    setOnPanelEnabled(loadSignalStatus);
}

// ─────────────────────────────────────────────────────────────────────────────
// Signal Status Badges
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Load and display Signal connection status badges and device info
 */
export async function loadSignalStatus() {
    // Skip status refresh during mutating operations to prevent UI jumping
    if (isSignalBusy()) {
        return;
    }
    
    return signalStatusGuard.guard(async () => {
        const container = DOM.signalStatusBadges();
        const deviceInfo = DOM.signalDeviceInfo();
        const modeSelection = DOM.signalModeSelection();
        
        if (!container) return;
        
        // Show loading state and hide mode selection until we know the status
        container.innerHTML = '<span class="badge bg-secondary"><span class="spinner-border spinner-border-sm me-1" style="width: 12px; height: 12px;"></span>Checking...</span>';
        if (modeSelection) modeSelection.style.display = 'none';

        try {
            const resp = await apiFetch(SIGNAL_API.status);
            if (!resp.ok) {
                container.innerHTML = '<span class="badge bg-secondary">Status unavailable</span>';
                if (deviceInfo) deviceInfo.textContent = 'Could not connect to signal-cli';
                return;
            }
            
            const status = await resp.json();
            let badges = '';

            // Defensive fallbacks for backend field name variations
            const accounts = status.registered_accounts || status.accounts || [];
            const registered = status.registered ?? accounts.length > 0;
            const errorMsg = status.error || status.exception;
            // Explicit boolean check - don't assume backend always sends reachable
            const reachable = status.reachable === true;

            // Update device info card - use DOM API to avoid XSS
            if (deviceInfo) {
                deviceInfo.replaceChildren();
                if (errorMsg) {
                    var errSpan = document.createElement('span');
                    errSpan.className = 'text-danger';
                    errSpan.textContent = 'signal-cli error: ' + errorMsg;
                    deviceInfo.appendChild(errSpan);
                } else if (registered && accounts.length > 0) {
                    var linkedSpan = document.createElement('span');
                    linkedSpan.className = 'text-success';
                    linkedSpan.textContent = 'Device linked';
                    deviceInfo.appendChild(linkedSpan);
                    deviceInfo.appendChild(document.createTextNode(' · Account: ' + accounts.join(', ')));
                } else if (reachable) {
                    var warnSpan = document.createElement('span');
                    warnSpan.className = 'text-warning';
                    warnSpan.textContent = 'signal-cli ready · No account linked';
                    deviceInfo.appendChild(warnSpan);
                } else {
                    deviceInfo.textContent = 'signal-cli not available';
                }
            }

            // Health badge - build as DocumentFragment to avoid flash
            var badgeFragment = document.createDocumentFragment();
            
            if (errorMsg) {
                var badge = document.createElement('span');
                badge.className = 'badge bg-danger';
                badge.title = errorMsg;
                var badgeIcon = document.createElement('span');
                badgeIcon.className = 'material-icons me-1';
                badgeIcon.textContent = 'cancel';
                badge.appendChild(badgeIcon);
                badge.appendChild(document.createTextNode('Offline'));
                badgeFragment.appendChild(badge);
            } else if (registered) {
                var badge = document.createElement('span');
                badge.className = 'badge bg-success';
                var badgeIcon = document.createElement('span');
                badgeIcon.className = 'material-icons me-1';
                badgeIcon.textContent = 'check_circle';
                badge.appendChild(badgeIcon);
                badge.appendChild(document.createTextNode('Linked'));
                badgeFragment.appendChild(badge);
            } else if (reachable) {
                var badge = document.createElement('span');
                badge.className = 'badge bg-warning text-dark';
                badge.title = 'signal-cli ready but no account linked';
                var badgeIcon = document.createElement('span');
                badgeIcon.className = 'material-icons me-1';
                badgeIcon.textContent = 'link_off';
                badge.appendChild(badgeIcon);
                badge.appendChild(document.createTextNode('Not linked'));
                badgeFragment.appendChild(badge);
                
                // Reset UI to show setup options - only if not busy and no flow active
                if (!isSignalBusy()) {
                    var flowLink = DOM.signalFlowLink();
                    var flowRegister = DOM.signalFlowRegister();
                    var flowActive = (flowLink && !flowLink.classList.contains('d-none'))
                        || (flowRegister && !flowRegister.classList.contains('d-none'));
                    if (!flowActive) {
                        resetModeSelection();
                        if (flowLink) flowLink.classList.add('d-none');
                        if (flowRegister) flowRegister.classList.add('d-none');
                    }
                }
            } else {
                var badge = document.createElement('span');
                badge.className = 'badge bg-secondary';
                var badgeIcon = document.createElement('span');
                badgeIcon.className = 'material-icons me-1';
                badgeIcon.textContent = 'help_outline';
                badge.appendChild(badgeIcon);
                badge.appendChild(document.createTextNode('Unavailable'));
                badgeFragment.appendChild(badge);
            }

            container.replaceChildren(badgeFragment);
            
            // Show/hide test section based on registration status
            const testSection = document.getElementById('signal-test-section');
            if (testSection) {
                testSection.style.display = registered ? 'flex' : 'none';
            }
            
            // Show/hide mode selection based on registration status
            const linkedAccountSection = document.getElementById('signal-linked-account-section');
            const advancedActions = document.getElementById('signal-advanced-actions');
            
            if (registered) {
                // Hide mode selection (Link/Register buttons)
                if (modeSelection) modeSelection.style.display = 'none';
                
                // Show linked account info section (created dynamically if needed)
                showLinkedAccountSection(accounts);
                
                // Show advanced actions (unlink/unregister)
                if (advancedActions) advancedActions.style.display = 'block';
                
                // Hide any active setup flows
                const flowLink = DOM.signalFlowLink();
                const flowRegister = DOM.signalFlowRegister();
                if (flowLink) flowLink.classList.add('d-none');
                if (flowRegister) flowRegister.classList.add('d-none');
            } else {
                // Show mode selection
                if (modeSelection) modeSelection.style.display = 'block';
                
                // Hide linked account section
                if (linkedAccountSection) linkedAccountSection.style.display = 'none';
                
                // Hide advanced actions (no account to unlink)
                if (advancedActions) advancedActions.style.display = 'none';
            }
        } catch (e) {
            console.warn('Failed to load Signal status:', e);
            container.innerHTML = '<span class="badge bg-secondary">Status unavailable</span>';
            if (deviceInfo) deviceInfo.textContent = 'Connection error';
            
            // Hide test section on error
            const testSection = document.getElementById('signal-test-section');
            if (testSection) testSection.style.display = 'none';
            
            // Hide advanced actions on error
            const advancedActions = document.getElementById('signal-advanced-actions');
            if (advancedActions) advancedActions.style.display = 'none';
        }
    });
}

/**
 * Show linked account section with account info and unlink button
 * @param {string[]} accounts - List of registered accounts (masked phone numbers)
 */
function showLinkedAccountSection(accounts) {
    const setupCollapse = document.getElementById('signal-setup-collapse');
    if (!setupCollapse) return;
    
    let section = document.getElementById('signal-linked-account-section');
    if (!section) {
        section = document.createElement('div');
        section.id = 'signal-linked-account-section';
        section.className = 'mb-3';
        // Use ID or fallback to first child (robust selector)
        var setupContent = document.getElementById('signal-setup-content')
            || setupCollapse.querySelector('.border.rounded.p-3')
            || setupCollapse.firstElementChild;
        if (setupContent) {
            setupContent.prepend(section);
        }
    }
    
    section.replaceChildren();

    const maskedAccounts = accounts;

    // Alert box - build with DOM API to prevent XSS
    var alert = document.createElement('div');
    alert.className = 'alert alert-success small py-2 mb-3';
    var alertInner = document.createElement('div');
    alertInner.className = 'd-flex align-items-center';
    var icon = document.createElement('span');
    icon.className = 'material-icons align-middle me-1';
    icon.style.fontSize = '18px';
    icon.textContent = 'check_circle';
    var strong = document.createElement('strong');
    strong.textContent = 'Device linked';
    var acctSpan = document.createElement('span');
    acctSpan.className = 'text-muted ms-2';
    acctSpan.textContent = 'Account: ' + maskedAccounts.join(', ');
    alertInner.append(icon, strong, acctSpan);
    alert.appendChild(alertInner);
    section.appendChild(alert);

    // Advanced actions (details/summary)
    var details = document.createElement('details');
    var summary = document.createElement('summary');
    summary.className = 'small text-muted';
    summary.style.cursor = 'pointer';
    summary.textContent = 'Advanced actions';
    details.appendChild(summary);

    var actionsDiv = document.createElement('div');
    actionsDiv.className = 'mt-3 d-flex flex-column gap-2';

    // Unlink button row - use injected dependency
    var unlinkRow = document.createElement('div');
    unlinkRow.className = 'd-flex align-items-center gap-2';
    var unlinkBtn = document.createElement('button');
    unlinkBtn.type = 'button';
    unlinkBtn.className = 'btn btn-outline-secondary btn-sm';
    unlinkBtn.textContent = 'Unlink Device';
    unlinkBtn.addEventListener('click', function () {
        if (_signalUnlinkFn) {
            _signalUnlinkFn();
        } else {
            console.error('Unlink handler not registered');
        }
    });
    var unlinkDesc = document.createElement('span');
    unlinkDesc.className = 'text-muted small';
    unlinkDesc.textContent = 'Remove Signal connection from justUp! (keeps Signal account)';
    unlinkRow.append(unlinkBtn, unlinkDesc);

    // Unregister button row
    var unregRow = document.createElement('div');
    unregRow.className = 'd-flex align-items-center gap-2';
    var unregBtn = document.createElement('button');
    unregBtn.type = 'button';
    unregBtn.className = 'btn btn-outline-danger btn-sm';
    unregBtn.textContent = 'Unregister from Signal';
    unregBtn.dataset.bsToggle = 'modal';
    unregBtn.dataset.bsTarget = '#unregisterPhoneModal';
    var unregDesc = document.createElement('span');
    unregDesc.className = 'text-muted small';
    unregDesc.textContent = 'Permanently delete account from Signal servers';
    unregRow.append(unregBtn, unregDesc);

    actionsDiv.append(unlinkRow, unregRow);
    details.appendChild(actionsDiv);
    section.appendChild(details);
    section.style.display = 'block';
}

/**
 * Reset the mode selection panel to show setup options (after unregister)
 */
function resetModeSelection() {
    var modeSelection = DOM.signalModeSelection();
    if (!modeSelection) return;

    var linkedSection = document.getElementById('signal-linked-account-section');
    if (linkedSection) linkedSection.remove();

    modeSelection.replaceChildren();

    var container = document.createElement('div');
    container.className = 'd-flex flex-column gap-2';

    // Link button - full DOM API for consistency
    var linkBtn = document.createElement('button');
    linkBtn.type = 'button';
    linkBtn.className = 'btn btn-primary text-start';
    linkBtn.id = 'signal-mode-link-btn';
    
    var linkContent = document.createElement('span');
    linkContent.className = 'd-flex align-items-center';
    var linkIcon = document.createElement('span');
    linkIcon.className = 'material-icons me-2';
    linkIcon.textContent = 'qr_code_2';
    var linkText = document.createElement('span');
    var linkStrong = document.createElement('strong');
    linkStrong.textContent = 'Use your existing Signal app';
    var linkSmall = document.createElement('small');
    linkSmall.className = 'd-block opacity-75';
    linkSmall.textContent = 'Recommended – scan QR code to link';
    linkText.append(linkStrong, linkSmall);
    linkContent.append(linkIcon, linkText);
    linkBtn.appendChild(linkContent);
    
    linkBtn.addEventListener('click', function () {
        if (_signalSelectModeFn) {
            _signalSelectModeFn('link');
        } else {
            console.error('Mode selection handler not registered');
        }
    });

    // Register button - full DOM API
    var regBtn = document.createElement('button');
    regBtn.type = 'button';
    regBtn.className = 'btn btn-outline-secondary text-start';
    regBtn.id = 'signal-mode-register-btn';
    
    var regContent = document.createElement('span');
    regContent.className = 'd-flex align-items-center';
    var regIcon = document.createElement('span');
    regIcon.className = 'material-icons me-2';
    regIcon.textContent = 'phone';
    var regText = document.createElement('span');
    var regStrong = document.createElement('strong');
    regStrong.textContent = 'Register a new number';
    var regSmall = document.createElement('small');
    regSmall.className = 'd-block opacity-75';
    regSmall.textContent = 'For dedicated notification number';
    regText.append(regStrong, regSmall);
    regContent.append(regIcon, regText);
    regBtn.appendChild(regContent);
    
    regBtn.addEventListener('click', function () {
        if (_signalSelectModeFn) {
            _signalSelectModeFn('register');
        } else {
            console.error('Mode selection handler not registered');
        }
    });

    container.append(linkBtn, regBtn);
    modeSelection.appendChild(container);
    modeSelection.classList.remove('d-none');
    modeSelection.style.display = 'block';
}
