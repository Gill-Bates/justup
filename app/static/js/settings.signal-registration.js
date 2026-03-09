//
// app/static/js/settings.signal-registration.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.signal-registration.js - Signal registration flows
 * 
 * Handles:
 * - Mode selection (Link vs Register)
 * - QR code flow (link existing account)
 * - Phone registration flow (new number)
 * - Verification code handling
 * - Profile name setting
 */

import { 
    DOM, 
    withSpinner, 
    getErrorMessage, 
    notify,
    setSuccess, 
    setError, 
    setWarning,
    clearStatus
} from './settings.core.js';

import { 
    SIGNAL_API, 
    qrCodeGuard,
    normalizeE164,
    isValidE164,
    isSignalBusy,
    setSignalBusy,
    maskPhone,
    autoSetProfileName,
    ensureSignalEnabled,
    getCurrentQrBlobUrl,
    setCurrentQrBlobUrl,
    revokeCurrentQrBlobUrl
} from './settings.signal-core.js';

// Use global apiFetch for authenticated API calls
const apiFetch = window.apiFetch;

// Dependency injection for status refresh
let _loadSignalStatus = null;

/**
 * Set the status loader function (called from signal-actions.js init)
 * @param {Function} fn
 */
export function setStatusLoader(fn) {
    _loadSignalStatus = fn;
}

/**
 * Refresh status (uses injected dependency)
 */
function refreshStatus() {
    if (_loadSignalStatus) {
        _loadSignalStatus();
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Mode Selection (Link vs Register)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Select Signal setup mode
 * @param {'link'|'register'} mode 
 */
export function signalSelectMode(mode) {
    const modeSelection = DOM.signalModeSelection();
    const flowLink = DOM.signalFlowLink();
    const flowRegister = DOM.signalFlowRegister();
    
    if (!modeSelection) return;
    
    modeSelection.classList.add('d-none');
    
    if (mode === 'link') {
        flowLink?.classList.remove('d-none');
    } else if (mode === 'register') {
        flowRegister?.classList.remove('d-none');
    }
}

/**
 * Go back to mode selection from current flow
 */
export function signalBackToModeSelection() {
    const modeSelection = DOM.signalModeSelection();
    const flowLink = DOM.signalFlowLink();
    const flowRegister = DOM.signalFlowRegister();
    
    flowLink?.classList.add('d-none');
    flowRegister?.classList.add('d-none');
    modeSelection?.classList.remove('d-none');
    
    // Reset link flow UI
    const stepNumber = document.getElementById('signal-link-step-number');
    const stepQr = document.getElementById('signal-link-step-qr');
    if (stepNumber) stepNumber.style.display = 'block';
    if (stepQr) stepQr.style.display = 'none';
    
    // Cleanup QR resources
    const qrContainer = DOM.signalQrContainerLink();
    if (qrContainer) qrContainer.innerHTML = '';
    revokeCurrentQrBlobUrl();
}

// ─────────────────────────────────────────────────────────────────────────────
// Flow 1: Link to existing account (QR Code)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Generate QR code for linking (phone number is optional)
 */
export async function signalGenerateQrCode() {
    if (isSignalBusy()) return;
    
    const numberInput = DOM.signalLinkNumber();
    const statusEl = document.getElementById('signal-link-number-status');
    const stepNumber = document.getElementById('signal-link-step-number');
    const stepQr = document.getElementById('signal-link-step-qr');
    const number = numberInput?.value?.trim();
    
    // Validate phone number only if provided
    if (number && !isValidE164(number)) {
        setError(statusEl, 'Please enter the full number with country code (e.g., +49 123 456789)');
        return;
    }
    
    clearStatus(statusEl);
    
    // Hide number input, show QR step
    if (stepNumber) stepNumber.style.display = 'none';
    if (stepQr) stepQr.style.display = 'block';
    
    // Load QR code (number is optional)
    await signalLoadQrCode(number || null);
}

/**
 * Load QR code for linking to existing Signal account
 * @param {string} number - Phone number for the QR code
 */
export async function signalLoadQrCode(number) {
    if (isSignalBusy()) return;
    
    return qrCodeGuard.guard(async () => {
        const container = DOM.signalQrContainerLink();
        if (!container) return;
        
        container.innerHTML = `
            <div class="spinner-border spinner-border-sm text-muted" role="status">
                <span class="visually-hidden">Loading QR...</span>
            </div>
        `;
        
        /**
         * Reset UI back to phone number step on error
         */
        const resetToNumberStep = (errorMessage) => {
            const stepNumber = document.getElementById('signal-link-step-number');
            const stepQr = document.getElementById('signal-link-step-qr');
            const statusEl = DOM.signalLinkStatus();
            
            if (stepQr) stepQr.style.display = 'none';
            if (stepNumber) stepNumber.style.display = 'block';
            if (statusEl) setError(statusEl, errorMessage);
            container.innerHTML = '';
            
            revokeCurrentQrBlobUrl();
        };
        
        try {
            revokeCurrentQrBlobUrl();
            
            // Pass number as query param if provided
            const url = number ? `${SIGNAL_API.qr}?number=${encodeURIComponent(number)}` : SIGNAL_API.qr;
            const resp = await apiFetch(url);
            if (resp.ok) {
                // Response is raw PNG binary data, convert to blob URL
                const blob = await resp.blob();
                const blobUrl = URL.createObjectURL(blob);
                setCurrentQrBlobUrl(blobUrl);
                
                // Build with DOM API to avoid innerHTML issues
                container.replaceChildren();
                const img = document.createElement('img');
                img.src = blobUrl;
                img.alt = 'Signal QR Code';
                img.className = 'img-fluid mb-2';
                img.style.maxWidth = '200px';
                const hint = document.createElement('p');
                hint.className = 'text-muted small mb-0';
                hint.textContent = 'Scan with Signal app on your phone';
                container.append(img, hint);
            } else {
                const error = await getErrorMessage(resp);
                resetToNumberStep(error);
            }
        } catch (e) {
            console.warn('Failed to load QR code:', e);
            resetToNumberStep('Network error loading QR code');
        }
    });
}

/**
 * Confirm successful QR code link
 */
export async function signalConfirmLink() {
    if (isSignalBusy()) return;
    
    const btn = DOM.signalLinkConfirmBtn();
    const statusEl = DOM.signalLinkStatus();
    const numberInput = DOM.signalLinkNumber();
    const number = numberInput?.value?.trim();
    
    clearStatus(statusEl);
    setSignalBusy(true);
    
    try {
        await withSpinner(btn, async () => {
            // Step 1: Wait for link process to complete
            statusEl.innerHTML = '⏳ Waiting for Signal to confirm linking... (this may take up to 60 seconds)';
            statusEl.className = 'small text-info';
            
            const confirmResp = await apiFetch(SIGNAL_API.linkConfirm, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            
            if (!confirmResp.ok) {
                const errorMsg = await getErrorMessage(confirmResp);
                if (confirmResp.status === 408) {
                    setError(statusEl, 'Timeout waiting for Signal. Please ensure you scanned the QR code with your Signal app.');
                } else {
                    setError(statusEl, errorMsg);
                }
                return;
            }
            
            const confirmData = await confirmResp.json();
            const accounts = confirmData.accounts || [];
            
            if (accounts.length === 0) {
                setError(statusEl, 'No account was linked. Please try again.');
                return;
            }
            
            // Use the first linked account, or match user-provided number if given
            let selectedAccount = accounts[0];
            if (number) {
                const normalizedNumber = normalizeE164(number);
                const found = accounts.find(acc => {
                    const accNum = normalizeE164(acc.number || acc);
                    return accNum === normalizedNumber;
                });
                if (found) {
                    selectedAccount = found;
                }
            }
            
            const accountNumber = selectedAccount.number || selectedAccount;
            
            // Save the sender number via PATCH
            await apiFetch(SIGNAL_API.settings, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ signal_api_sender_number: normalizeE164(accountNumber) })
            });
            
            await ensureSignalEnabled();
            autoSetProfileName();
            
            statusEl.innerHTML = '✓ <strong>Successfully linked!</strong> justUp! can now send alerts via Signal.';
            statusEl.className = 'small text-success';
            notify('success', 'Signal account linked');
            
            // Hide QR code step
            const stepQr = document.getElementById('signal-link-step-qr');
            if (stepQr) stepQr.style.display = 'none';
            
            // Clear QR code container and cleanup blob URL
            const qrContainer = DOM.signalQrContainerLink();
            if (qrContainer) qrContainer.innerHTML = '';
            revokeCurrentQrBlobUrl();
            
            // Refresh status
            refreshStatus();
        });
    } catch (e) {
        console.warn('Signal confirm link error:', e);
        setError(statusEl, 'Network error during confirmation');
    } finally {
        setSignalBusy(false);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Flow 2: Register new phone number
// ─────────────────────────────────────────────────────────────────────────────

// Track verification code boxes initialization to prevent duplicate listeners
let _codeBoxesInitialized = false;

/**
 * Enable a registration step
 * @param {string} stepId 
 */
function signalStepEnable(stepId) {
    const step = document.getElementById(stepId);
    if (step) {
        step.classList.remove('signal-step-disabled');
        step.classList.add('signal-step-active');
        step.querySelectorAll('input, button').forEach(el => el.disabled = false);
        
        if (stepId === 'signal-reg-step-2') {
            initVerificationCodeBoxes();
        }
    }
}

/**
 * Mark a registration step as complete
 * @param {string} stepId 
 */
function signalStepComplete(stepId) {
    const step = document.getElementById(stepId);
    if (step) {
        step.classList.remove('signal-step-disabled', 'signal-step-active');
        step.classList.add('signal-step-complete');
    }
}

/**
 * Advance from one step to the next
 * @param {string} currentStepId 
 * @param {string} nextStepId 
 */
function signalAdvanceToStep(currentStepId, nextStepId) {
    signalStepComplete(currentStepId);
    signalStepEnable(nextStepId);
}

/**
 * Initialize 6-digit verification code input boxes
 * Only binds listeners once to prevent duplicates on retry
 */
function initVerificationCodeBoxes() {
    if (_codeBoxesInitialized) return;
    
    const container = DOM.signalVerifyBoxes();
    if (!container) return;
    
    const boxes = container.querySelectorAll('.signal-code-box');
    if (boxes.length !== 6) return;
    
    _codeBoxesInitialized = true;
    
    boxes.forEach((box, index) => {
        box.addEventListener('input', (e) => {
            const value = e.target.value;
            
            if (!/^\d*$/.test(value)) {
                e.target.value = value.replace(/\D/g, '');
                return;
            }
            
            if (value.length > 1) {
                e.target.value = value[0];
            }
            
            if (value.length === 1 && index < 5) {
                boxes[index + 1].focus();
            }
            
            checkAndSubmitCode(boxes);
        });
        
        box.addEventListener('keydown', (e) => {
            if (e.key === 'Backspace') {
                if (box.value === '' && index > 0) {
                    boxes[index - 1].focus();
                    boxes[index - 1].value = '';
                    e.preventDefault();
                }
            } else if (e.key === 'ArrowLeft' && index > 0) {
                boxes[index - 1].focus();
                e.preventDefault();
            } else if (e.key === 'ArrowRight' && index < 5) {
                boxes[index + 1].focus();
                e.preventDefault();
            }
        });
        
        box.addEventListener('paste', (e) => {
            e.preventDefault();
            const pastedData = (e.clipboardData || window.clipboardData).getData('text');
            const digits = pastedData.replace(/\D/g, '').slice(0, 6);
            
            if (digits.length > 0) {
                for (let i = 0; i < 6; i++) {
                    boxes[i].value = digits[i] || '';
                }
                
                const nextEmptyIndex = digits.length < 6 ? digits.length : 5;
                boxes[nextEmptyIndex].focus();
                
                checkAndSubmitCode(boxes);
            }
        });
        
        box.addEventListener('focus', () => {
            box.select();
        });
    });
    
    setTimeout(() => boxes[0].focus(), 100);
}

/**
 * Check if all 6 digits are entered and auto-submit
 * Only submits if not already busy
 * @param {NodeList} boxes 
 */
async function checkAndSubmitCode(boxes) {
    const code = Array.from(boxes).map(b => b.value).join('');
    
    if (code.length !== 6 || !/^\d{6}$/.test(code)) return;
    
    // Don't auto-submit while another operation is running
    if (isSignalBusy()) {
        return;
    }
    
    const hiddenInput = DOM.signalVerifyCode();
    if (hiddenInput) {
        hiddenInput.value = code;
    }
    
    boxes.forEach(b => b.disabled = true);
    
    const statusEl = DOM.signalVerifyStatus();
    if (statusEl) {
        statusEl.innerHTML = '<span class="text-info"><span class="spinner-border spinner-border-sm me-1"></span>Verifying...</span>';
    }
    
    await signalVerify();
    
    // If verify returned early (busy guard), re-enable boxes
    if (!isSignalBusy()) {
        boxes.forEach(b => b.disabled = false);
    }
}

/**
 * Clear all verification code boxes (called on error)
 */
function clearVerificationCodeBoxes() {
    const container = DOM.signalVerifyBoxes();
    if (!container) return;
    
    const boxes = container.querySelectorAll('.signal-code-box');
    boxes.forEach(box => {
        box.value = '';
        box.disabled = false;
        box.classList.remove('is-invalid');
    });
    
    if (boxes[0]) {
        setTimeout(() => boxes[0].focus(), 100);
    }
}

/**
 * Show error state on verification code boxes
 */
function showVerificationCodeError() {
    const container = DOM.signalVerifyBoxes();
    if (!container) return;
    
    const boxes = container.querySelectorAll('.signal-code-box');
    boxes.forEach(box => {
        box.classList.add('is-invalid');
    });
    
    setTimeout(() => {
        clearVerificationCodeBoxes();
    }, 600);
}

/**
 * Register a new phone number with Signal
 */
export async function signalRegister() {
    if (isSignalBusy()) return;
    
    const btn = DOM.signalRegisterBtn();
    const statusEl = DOM.signalRegisterStatus();
    const numberInput = DOM.signalRegisterNumber();
    const useVoice = DOM.signalUseVoice()?.checked || false;
    const captchaSection = DOM.signalCaptchaSection();
    const captchaInput = DOM.signalCaptchaToken();
    const number = numberInput?.value?.trim();
    const captcha = captchaInput?.value?.trim() || null;

    if (!number) {
        setError(statusEl, 'Please enter a phone number');
        return;
    }

    if (!isValidE164(number)) {
        setError(statusEl, 'Please enter the full number with country code (e.g., +49 123 456789)');
        return;
    }

    clearStatus(statusEl);
    const normalizedNumber = normalizeE164(number);

    setSignalBusy(true);
    try {
        await withSpinner(btn, async () => {
            const payload = { number: normalizedNumber, use_voice: useVoice };
            if (captcha) {
                payload.captcha = captcha;
            }
            
            const resp = await apiFetch(SIGNAL_API.register, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (resp.ok) {
                setSuccess(statusEl, `Verification code sent! Check your ${useVoice ? 'phone' : 'SMS'}`);
                notify('success', 'Verification code sent');
                if (captchaSection) captchaSection.classList.add('d-none');
                if (captchaInput) captchaInput.value = '';
                
                const verifyHint = document.getElementById('signal-verify-hint');
                if (verifyHint) {
                    const maskedNum = maskPhone(normalizedNumber);
                    verifyHint.textContent = `Enter the code sent to ${maskedNum}`;
                }
                
                signalAdvanceToStep('signal-reg-step-1', 'signal-reg-step-2');
            } else {
                const error = await getErrorMessage(resp);
                
                const needsCaptcha = error && (
                    error.toLowerCase().includes('captcha') ||
                    error.toLowerCase().includes('challenge')
                );
                
                if (needsCaptcha && captchaSection) {
                    captchaSection.classList.remove('d-none');
                    setWarning(statusEl, 'Captcha required - solve it and paste the token below, then try again');
                } else {
                    const alreadyRegistered = error && (
                        error.toLowerCase().includes('already registered') ||
                        error.toLowerCase().includes('existing account') ||
                        error.toLowerCase().includes('already linked')
                    );
                    
                    if (alreadyRegistered) {
                        notify('info', 'This number is already registered');
                        refreshStatus();
                    } else {
                        setError(statusEl, error);
                    }
                }
            }
        });
    } catch (e) {
        console.warn('Signal register error:', e);
        setError(statusEl, 'Network error');
    } finally {
        setSignalBusy(false);
    }
}

/**
 * Verify the registration code
 */
export async function signalVerify() {
    if (isSignalBusy()) return;

    const btn = DOM.signalVerifyBtn();
    const statusEl = DOM.signalVerifyStatus();
    const codeInput = DOM.signalVerifyCode();
    const numberInput = DOM.signalRegisterNumber();
    const code = codeInput?.value?.trim();
    const number = numberInput?.value?.trim();

    if (!number) {
        setError(statusEl, 'Phone number required (step 1)');
        return;
    }
    if (!code) {
        setError(statusEl, 'Please enter the verification code');
        return;
    }

    clearStatus(statusEl);
    setSignalBusy(true);

    const normalizedNumber = normalizeE164(number);

    try {
        await withSpinner(btn, async () => {
            const resp = await apiFetch(SIGNAL_API.verify, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ number: normalizedNumber, code })
            });

            if (resp.ok) {
                setSuccess(statusEl, 'Verified! Number registered successfully');
                notify('success', 'Phone number verified');
                
                await apiFetch(SIGNAL_API.settings, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ signal_api_sender_number: normalizedNumber })
                });
                
                await ensureSignalEnabled();
                autoSetProfileName();
                
                signalAdvanceToStep('signal-reg-step-2', 'signal-reg-step-3');
                
                refreshStatus();
                
                const statusBadge = document.getElementById('signal-link-status-badge');
                if (window.htmx && statusBadge) {
                    htmx.trigger(statusBadge, 'load');
                }
            } else {
                const error = await getErrorMessage(resp);
                setError(statusEl, error);
                showVerificationCodeError();
            }
        });
    } catch (e) {
        console.warn('Signal verify error:', e);
        setError(statusEl, 'Network error');
        showVerificationCodeError();
    } finally {
        setSignalBusy(false);
    }
}

/**
 * Shared implementation for setting profile name
 * @param {HTMLElement} btn
 * @param {HTMLElement} statusEl
 * @param {HTMLInputElement} nameInput
 * @returns {Promise<boolean>} Success flag
 */
async function setProfileNameImpl(btn, statusEl, nameInput) {
    const name = nameInput?.value?.trim();
    
    if (!name) {
        setError(statusEl, 'Please enter a display name');
        return false;
    }

    clearStatus(statusEl);

    try {
        let success = false;
        await withSpinner(btn, async () => {
            const resp = await apiFetch(SIGNAL_API.profileName, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });

            if (resp.ok) {
                setSuccess(statusEl, `Display name set to "${name}"`);
                notify('success', 'Profile name updated');
                success = true;
            } else {
                const error = await getErrorMessage(resp);
                setError(statusEl, error);
            }
        });
        return success;
    } catch (e) {
        console.warn('Set profile name error:', e);
        setError(statusEl, 'Network error');
        return false;
    }
}

/**
 * Set profile name after registration
 */
export async function signalSetProfileName() {
    const success = await setProfileNameImpl(
        DOM.signalSetNameBtn(),
        DOM.signalProfileStatus(),
        DOM.signalProfileName()
    );
    
    if (success) {
        signalStepComplete('signal-reg-step-3');
        signalFinishRegistration();
    }
}

/**
 * Set profile name after linking (uses different DOM elements)
 */
export async function signalSetProfileNameLink() {
    await setProfileNameImpl(
        document.querySelector('#signal-link-profile-section button'),
        document.getElementById('signal-link-profile-status'),
        document.getElementById('signal-link-profile-name')
    );
}

/**
 * Skip profile name setting and finish registration
 */
export async function signalSkipProfile() {
    notify('info', 'Profile name skipped');
    signalFinishRegistration();
}

/**
 * Finish registration: collapse flow, refresh status, show success
 */
async function signalFinishRegistration() {
    const flowRegister = DOM.signalFlowRegister();
    if (flowRegister) flowRegister.classList.add('d-none');
    
    refreshStatus();
    
    notify('success', 'Signal setup complete!');
}
