//
// app/static/js/settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dependencies:
//   - api.js (api function)
//   - theme.js (base64UrlToArrayBuffer, arrayBufferToBase64Url)
//   - toast.js (juToast, juConfirm, juPrompt, juAlert)

let otpSection;
let passkeysSection;
let passkeysListEl;

document.addEventListener('DOMContentLoaded', () => {
    otpSection = document.getElementById('otp-section');
    passkeysSection = document.getElementById('passkeys-section');
    passkeysListEl = document.getElementById('passkeys-list');

    // General settings
    const saveSettingsBtn = document.getElementById('saveSettingsBtn');
    if (saveSettingsBtn) saveSettingsBtn.addEventListener('click', saveSettings);

    // Password change
    const changePwBtn = document.getElementById('changePasswordBtn');
    if (changePwBtn) changePwBtn.addEventListener('click', changePassword);

    // OTP
    const enableOtpBtn = document.getElementById('enable-otp-btn');
    if (enableOtpBtn) enableOtpBtn.addEventListener('click', enableOtp);

    const disableOtpBtn = document.getElementById('disable-otp-btn');
    if (disableOtpBtn) disableOtpBtn.addEventListener('click', disableOtp);

    const confirmOtpBtn = document.getElementById('confirm-otp-btn');
    if (confirmOtpBtn) confirmOtpBtn.addEventListener('click', confirmOtp);

    // Passkeys
    const addPasskeyBtn = document.getElementById('add-passkey-btn');
    if (addPasskeyBtn) addPasskeyBtn.addEventListener('click', registerPasskey);

    // Swagger toggle
    const swaggerToggle = document.getElementById('enable-swagger');
    if (swaggerToggle) {
        swaggerToggle.addEventListener('change', saveSwaggerSetting);
    }
    const copySwaggerBtn = document.getElementById('btn-copy-swagger-url');
    if (copySwaggerBtn) {
        copySwaggerBtn.addEventListener('click', copySwaggerUrl);
    }

    loadSettings();
    loadOtpStatus();
    loadPasskeys();
});

// ─── General Settings ──────────────────────────────────────────

async function loadSettings() {
    try {
        const settings = await api('GET', '/api/settings');
        const portField = document.getElementById('setting-port');
        const intervalField = document.getElementById('setting-interval');
        const retentionField = document.getElementById('setting-retention');

        if (portField) portField.value = settings?.gui_port || '8000';
        if (intervalField) intervalField.value = settings?.check_interval_default || '60';
        if (retentionField) retentionField.value = settings?.tsdb_retention_days || '90';

        // Swagger toggle
        const swaggerToggle = document.getElementById('enable-swagger');
        if (swaggerToggle) {
            swaggerToggle.style.transition = 'none';
            swaggerToggle.checked = toBool(settings?.enable_swagger);
            swaggerToggle.offsetHeight; // force reflow
            swaggerToggle.style.transition = '';
        }
        updateSwaggerUrl();
    } catch (err) {
        console.warn('Failed to load settings:', err);
    }
}

function toBool(val) {
    return ['1', 'true', 'yes', 'on'].includes(String(val || '').trim().toLowerCase());
}

function updateSwaggerUrl() {
    const target = document.getElementById('swagger-url');
    if (!target) return;
    const port = document.getElementById('setting-port')?.value || location.port || '8000';
    const protocol = location.protocol;
    const urlHost = location.hostname;
    target.textContent = `${protocol}//${urlHost}:${port}/swagger`;
}

async function saveSettings() {
    const btn = document.getElementById('saveSettingsBtn');
    if (btn) btn.disabled = true;

    try {
        const port = parseInt(document.getElementById('setting-port')?.value, 10) || 8000;
        const interval = parseInt(document.getElementById('setting-interval')?.value, 10) || 60;
        const retention = parseInt(document.getElementById('setting-retention')?.value, 10) || 90;

        await api('PUT', '/api/settings', {
            gui_port: String(port),
            check_interval_default: String(interval),
            tsdb_retention_days: String(retention),
        });
        juToast('Settings saved', 'success');
        updateSwaggerUrl();
    } catch (err) {
        juToast(err.message, 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function saveSwaggerSetting() {
    const swaggerToggle = document.getElementById('enable-swagger');
    if (!swaggerToggle) return;
    try {
        await api('PUT', '/api/settings', {
            enable_swagger: swaggerToggle.checked ? '1' : '0',
        });
        juToast(swaggerToggle.checked ? 'Swagger UI enabled' : 'Swagger UI disabled', 'success');
    } catch (err) {
        swaggerToggle.checked = !swaggerToggle.checked;
        juToast(err.message, 'danger');
    }
}

function copySwaggerUrl() {
    const target = document.getElementById('swagger-url');
    if (!target) return;
    navigator.clipboard.writeText(target.textContent).then(() => {
        juToast('URL copied', 'info');
    }).catch(() => {
        juToast('Failed to copy URL', 'warning');
    });
}

// ─── Password Change ───────────────────────────────────────────

async function changePassword() {
    const btn = document.getElementById('changePasswordBtn');
    if (btn) btn.disabled = true;

    try {
        const currentPw = document.getElementById('current-password').value;
        const newPw = document.getElementById('new-password').value;
        const confirmPw = document.getElementById('confirm-password').value;

        if (!currentPw || !newPw) {
            juToast('Please fill in all password fields', 'warning');
            return;
        }
        if (newPw.length < 8) {
            juToast('Password must be at least 8 characters', 'warning');
            return;
        }
        if (newPw !== confirmPw) {
            juToast('New passwords do not match', 'warning');
            return;
        }

        await api('POST', '/api/users/me/password', {
            current_password: currentPw,
            new_password: newPw,
        });
        juToast('Password changed successfully', 'success');
        document.getElementById('current-password').value = '';
        document.getElementById('new-password').value = '';
        document.getElementById('confirm-password').value = '';
    } catch (err) {
        juToast(err.message, 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ─── OTP / 2FA ─────────────────────────────────────────────────

async function loadOtpStatus() {
    try {
        const resp = await api('GET', '/api/auth/me');
        updateOtpUI(resp?.data?.otp_enabled || false);
    } catch (err) {
        console.warn('Failed to load OTP status:', err);
    }
}

function updateOtpUI(enabled) {
    const enableBtn = document.getElementById('enable-otp-btn');
    const disableBtn = document.getElementById('disable-otp-btn');
    const otpSetup = document.getElementById('otp-setup');
    const otpStatus = document.getElementById('otp-status');

    if (enabled) {
        if (enableBtn) enableBtn.classList.add('d-none');
        if (disableBtn) disableBtn.classList.remove('d-none');
        if (otpSetup) otpSetup.classList.add('d-none');
        if (otpStatus) {
            otpStatus.textContent = 'Two-factor authentication is enabled.';
            otpStatus.classList.remove('d-none');
        }
    } else {
        if (enableBtn) enableBtn.classList.remove('d-none');
        if (disableBtn) disableBtn.classList.add('d-none');
        if (otpStatus) otpStatus.textContent = 'Two-factor authentication is not enabled.';
    }
}

async function enableOtp() {
    const btn = document.getElementById('enable-otp-btn');
    if (btn) btn.disabled = true;

    try {
        const data = await api('POST', '/api/users/me/otp/enable');
        const otpSetup = document.getElementById('otp-setup');
        const qrImg = document.getElementById('otp-qr');
        const secretEl = document.getElementById('otp-secret');

        if (qrImg && data?.qr_code) {
            qrImg.src = `data:image/png;base64,${data.qr_code}`;
        }
        if (secretEl && data?.secret) {
            secretEl.textContent = data.secret;
        }
        if (otpSetup) otpSetup.classList.remove('d-none');
    } catch (err) {
        juToast(err.message, 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function confirmOtp() {
    const code = document.getElementById('otp-confirm-code')?.value?.trim();
    if (!code) {
        juToast('Please enter the verification code', 'warning');
        return;
    }

    try {
        const data = await api('POST', '/api/users/me/otp/confirm', { code });
        updateOtpUI(true);
        juToast('Two-factor authentication enabled', 'success');

        if (data?.recovery_codes) {
            const codesList = data.recovery_codes.join('\n');
            await juAlert(
                'Save these recovery codes in a safe place. They can be used to access your account if you lose your authenticator:\n\n' + codesList,
                'warning'
            );
        }
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

async function disableOtp() {
    const confirmed = await juConfirm('Disable two-factor authentication?', 'warning');
    if (!confirmed) return;

    const btn = document.getElementById('disable-otp-btn');
    if (btn) btn.disabled = true;

    try {
        await api('POST', '/api/users/me/otp/disable');
        updateOtpUI(false);
        juToast('Two-factor authentication disabled', 'success');
    } catch (err) {
        juToast(err.message, 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ─── Passkeys ──────────────────────────────────────────────────

async function loadPasskeys() {
    if (!passkeysListEl) return;

    try {
        const passkeys = await api('GET', '/api/passkeys');
        renderPasskeys(passkeys || []);
    } catch (err) {
        console.warn('Failed to load passkeys:', err);
    }
}

function renderPasskeys(passkeys) {
    if (!passkeysListEl) return;

    if (passkeys.length === 0) {
        passkeysListEl.innerHTML = '<p class="text-muted mb-0">No passkeys registered.</p>';
        return;
    }

    passkeysListEl.innerHTML = '';
    passkeys.forEach(pk => {
        const row = document.createElement('div');
        row.className = 'd-flex justify-content-between align-items-center border rounded p-2 mb-2';

        const info = document.createElement('div');
        const icon = document.createElement('span');
        icon.className = 'material-icons me-2';
        icon.style.cssText = 'vertical-align:middle;font-size:18px';
        icon.textContent = 'key';

        const nameEl = document.createElement('strong');
        nameEl.textContent = pk.name || 'Passkey';

        const dateEl = document.createElement('small');
        dateEl.className = 'text-muted ms-2';
        dateEl.textContent = pk.created_at || '';

        info.append(icon, nameEl, dateEl);

        const delBtn = document.createElement('button');
        delBtn.className = 'btn btn-sm btn-outline-danger';
        const delIcon = document.createElement('span');
        delIcon.className = 'material-icons';
        delIcon.style.fontSize = '16px';
        delIcon.textContent = 'delete';
        delBtn.appendChild(delIcon);
        delBtn.addEventListener('click', () => deletePasskey(pk.id, pk.name || 'Passkey'));

        row.append(info, delBtn);
        passkeysListEl.appendChild(row);
    });
}

async function registerPasskey() {
    const btn = document.getElementById('add-passkey-btn');
    if (btn) btn.disabled = true;

    try {
        const name = await juPrompt('Enter a name for this passkey:', { placeholder: 'e.g. MacBook Touch ID' });
        if (name === null) return;

        const startData = await api('POST', '/api/passkeys/register/start');
        const options = startData;
        if (!options || !options.challenge) throw new Error('Invalid server response');

        const publicKeyOptions = {
            challenge: base64UrlToArrayBuffer(options.challenge),
            rp: { name: options.rp.name, id: options.rp.id },
            user: {
                id: base64UrlToArrayBuffer(options.user.id),
                name: options.user.name,
                displayName: options.user.display_name,
            },
            pubKeyCredParams: options.pub_key_cred_params.map(p => ({
                type: 'public-key',
                alg: p.alg,
            })),
            authenticatorSelection: {
                authenticatorAttachment: options.authenticator_selection?.authenticator_attachment,
                residentKey: options.authenticator_selection?.resident_key || 'preferred',
                userVerification: options.authenticator_selection?.user_verification || 'preferred',
            },
            timeout: options.timeout || 60000,
            attestation: options.attestation || 'none',
        };

        if (options.exclude_credentials) {
            publicKeyOptions.excludeCredentials = options.exclude_credentials.map(cred => ({
                type: 'public-key',
                id: base64UrlToArrayBuffer(cred.id),
            }));
        }

        const credential = await navigator.credentials.create({ publicKey: publicKeyOptions });
        if (!credential) throw new Error('Passkey creation cancelled');

        const credentialJSON = {
            id: credential.id,
            rawId: arrayBufferToBase64Url(credential.rawId),
            type: credential.type,
            response: {
                clientDataJSON: arrayBufferToBase64Url(credential.response.clientDataJSON),
                attestationObject: arrayBufferToBase64Url(credential.response.attestationObject),
            },
        };

        await api('POST', '/api/passkeys/register/finish', {
            name: name.trim() || 'Passkey',
            credential: credentialJSON,
        });

        juToast('Passkey registered successfully', 'success');
        await loadPasskeys();
    } catch (err) {
        if (err.name === 'NotAllowedError') return;
        juToast(err.message || 'Failed to register passkey', 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function deletePasskey(id, name) {
    // Validate ID format to prevent path injection
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
        juToast('Invalid passkey ID', 'danger');
        return;
    }

    const confirmed = await juConfirm(`Delete passkey "${name}"?`, 'danger');
    if (!confirmed) return;

    try {
        await api('DELETE', `/api/passkeys/${encodeURIComponent(id)}`);
        juToast('Passkey deleted', 'success');
        await loadPasskeys();
    } catch (err) {
        juToast(err.message, 'danger');
    }
}
