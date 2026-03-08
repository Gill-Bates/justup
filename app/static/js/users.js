//
// app/static/js/users.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Wrap in IIFE to avoid polluting global scope
(function () {
    'use strict';

    let usersTable;
    let userModal;
    let userSaveBtn;
    let editingUserId = null;
    let otpModal;
    let passkeysModal;

    // Helper: Get current logged-in user ID from page data attribute
    function getCurrentUserId() {
        const raw = document.querySelector('.ju-page')?.dataset?.currentUserId;
        return raw ? parseInt(raw, 10) : null;
    }

    document.addEventListener('DOMContentLoaded', () => {
        usersTable = document.getElementById('users-body');
        userSaveBtn = document.getElementById('saveUserBtn');
        passkeysListEl = document.getElementById('passkeys-list');

        const modalEl = document.getElementById('userModal');
        if (modalEl) userModal = new bootstrap.Modal(modalEl);

        // OTP Modal
        const otpModalEl = document.getElementById('otpModal');
        if (otpModalEl) otpModal = new bootstrap.Modal(otpModalEl);

        // Passkeys Modal
        const passkeysModalEl = document.getElementById('passkeysModal');
        if (passkeysModalEl) passkeysModal = new bootstrap.Modal(passkeysModalEl);

        const addBtn = document.getElementById('addUserBtn');
        if (addBtn) addBtn.addEventListener('click', () => openUserModal());

        if (userSaveBtn) userSaveBtn.addEventListener('click', saveUser);

        // OTP buttons
        const enableOtpBtn = document.getElementById('enable-otp-btn');
        if (enableOtpBtn) enableOtpBtn.addEventListener('click', enableOtp);

        const disableOtpBtn = document.getElementById('disable-otp-btn');
        if (disableOtpBtn) disableOtpBtn.addEventListener('click', disableOtp);

        const confirmOtpBtn = document.getElementById('confirm-otp-btn');
        if (confirmOtpBtn) confirmOtpBtn.addEventListener('click', confirmOtp);

        // Passkeys
        const addPasskeyBtn = document.getElementById('add-passkey-btn');
        if (addPasskeyBtn) addPasskeyBtn.addEventListener('click', registerPasskey);

        // Event delegation for edit and delete buttons
        document.addEventListener('click', (e) => {
            const editBtn = e.target.closest('.edit-user');
            if (editBtn) {
                e.preventDefault();
                const id = parseInt(editBtn.dataset.id, 10);
                if (!Number.isNaN(id)) editUser(id);
                return;
            }

            const deleteBtn = e.target.closest('.delete-user');
            if (deleteBtn) {
                e.preventDefault();
                const id = parseInt(deleteBtn.dataset.id, 10);
                const username = deleteBtn.dataset.username;
                if (!Number.isNaN(id)) deleteUser(id, username);
                return;
            }

            const otpBtn = e.target.closest('.manage-otp');
            if (otpBtn) {
                e.preventDefault();
                openOtpModal();
                return;
            }

            const passkeysBtn = e.target.closest('.manage-passkeys');
            if (passkeysBtn) {
                e.preventDefault();
                openPasskeysModal();
                return;
            }
        });

        loadUsers();
    });

    function openUserModal(user = null) {
        editingUserId = user ? user.id : null;
        const title = document.getElementById('userModalTitle');
        title.textContent = user ? 'Edit User' : 'Add User';

        const currentUserId = getCurrentUserId();

        // Show change password section only when editing own user
        const changePasswordSection = document.getElementById('change-password-section');
        const isEditingOwnUser = user && currentUserId && user.id === currentUserId;

        if (changePasswordSection) {
            if (isEditingOwnUser) {
                changePasswordSection.classList.remove('d-none');
            } else {
                changePasswordSection.classList.add('d-none');
            }
        }

        document.getElementById('user-username').value = user?.username || '';
        document.getElementById('user-password').value = '';
        document.getElementById('user-is-admin').checked = user ? user.is_admin : false;

        // Clear password change fields
        const currentPwField = document.getElementById('user-current-password');
        const newPwField = document.getElementById('user-new-password');
        if (currentPwField) currentPwField.value = '';
        if (newPwField) newPwField.value = '';

        const pwField = document.getElementById('user-password');
        pwField.required = !user;
        pwField.placeholder = user ? '(leave empty to keep)' : 'Password';

        userModal.show();
    }



    function renderUsers(users) {
        if (!usersTable) return;

        if (users.length === 0) {
            usersTable.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4">No users found.</td></tr>';
            return;
        }

        const currentUserId = getCurrentUserId();

        usersTable.innerHTML = users.map(u => {
            const adminBadge = u.is_admin
                ? '<span class="badge bg-warning text-dark">Admin</span>'
                : '<span class="badge bg-secondary">User</span>';

            const statusBadge = u.is_active
                ? '<span class="badge bg-success">Active</span>'
                : '<span class="badge bg-secondary">Inactive</span>';

            // Fix: Escape last_login_at to prevent XSS
            const lastLogin = escapeHtml(u.last_login_at || '–');

            // Admin user (ID 1) cannot be deleted
            const isSystemAdmin = u.id === 1;
            const deleteBtn = isSystemAdmin
                ? '<button class="btn btn-sm btn-outline-secondary" disabled title="System admin cannot be deleted"><span class="material-icons icon-sm">delete</span></button>'
                : `<button class="btn btn-sm btn-outline-danger delete-user" data-id="${u.id}" data-username="${escapeHtml(u.username)}" title="Delete"><span class="material-icons icon-sm">delete</span></button>`;

            // Security icons only for current user
            const isCurrentUser = currentUserId && u.id === currentUserId;

            // OTP icon: info if enabled, secondary outline if disabled  
            const otpIconClass = u.otp_enabled ? 'btn-info' : 'btn-outline-secondary';
            const otpTitle = u.otp_enabled ? '2FA Enabled - Click to manage' : '2FA Disabled - Click to enable';

            const securityIcons = isCurrentUser
                ? `<button class="btn btn-sm ${otpIconClass} manage-otp" data-id="${u.id}" title="${otpTitle}">
                        <span class="material-icons icon-sm">security</span>
                    </button>
                    <button class="btn btn-sm btn-outline-success manage-passkeys" data-id="${u.id}" title="Passkeys">
                        <span class="material-icons icon-sm">key</span>
                    </button>`
                : '';

            return `<tr>
                <td>${escapeHtml(u.username)}</td>
                <td>${adminBadge}</td>
                <td>${statusBadge}</td>
                <td>${lastLogin}</td>
                <td>
                    <button class="btn btn-sm btn-outline-primary edit-user" data-id="${u.id}" title="Edit">
                        <span class="material-icons icon-sm">edit</span>
                    </button>
                    ${securityIcons}
                    ${deleteBtn}
                </td>
            </tr>`;
        }).join('');
    }

    // Robust HTML escaping without DOM manipulation
    function escapeHtml(text) {
        const str = String(text ?? '');
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    async function saveUser() {
        const username = document.getElementById('user-username').value.trim();
        const password = document.getElementById('user-password').value;
        const is_admin = document.getElementById('user-is-admin').checked;

        // Get password change fields
        const currentPassword = document.getElementById('user-current-password')?.value || '';
        const newPassword = document.getElementById('user-new-password')?.value || '';

        // Validation
        if (!username) {
            juToast('Username is required', 'warning');
            return;
        }

        // Client-side username validation (alphanumeric, dots, dashes, underscores, 3-64 chars)
        if (!/^[a-zA-Z0-9_.-]{3,64}$/.test(username)) {
            juToast('Username must be 3-64 characters (letters, numbers, dots, dashes, underscores only)', 'warning');
            return;
        }

        try {
            if (editingUserId) {
                const currentUserId = getCurrentUserId();
                const isEditingOwnUser = currentUserId && editingUserId === currentUserId;

                // Handle password change separately to avoid partial-failure state
                if (isEditingOwnUser && currentPassword && newPassword) {
                    // Validate new password length
                    if (newPassword.length < 8) {
                        juToast('New password must be at least 8 characters', 'warning');
                        return;
                    }

                    // Change password first (separate transaction)
                    try {
                        await api('POST', `/api/users/${editingUserId}/change-password`, {
                            current_password: currentPassword,
                            new_password: newPassword,
                        });
                        // Don't show success yet - wait for full save
                    } catch (err) {
                        juToast(`Password change failed: ${err.message}`, 'danger');
                        return;
                    }
                }

                // Update user profile
                const data = { username, is_admin };
                if (password) data.password = password;

                await api('PATCH', `/api/users/${editingUserId}`, data);

                // Show combined success message
                if (isEditingOwnUser && currentPassword && newPassword) {
                    juToast('User updated and password changed successfully', 'success');
                } else {
                    juToast('User updated', 'success');
                }
            } else {
                if (!password) {
                    juToast('Password is required for new users', 'warning');
                    return;
                }
                if (password.length < 8) {
                    juToast('Password must be at least 8 characters', 'warning');
                    return;
                }
                await api('POST', '/api/users', { username, password, is_admin });
                juToast('User created', 'success');
            }
            userModal.hide();
            await loadUsers();
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    // Performance fix: Use dedicated GET /api/users/{id} endpoint
    async function editUser(id) {
        try {
            const user = await api('GET', `/api/users/${id}`);
            if (!user) {
                juToast('User not found', 'danger');
                return;
            }
            openUserModal(user);
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    async function deleteUser(id, username) {
        const confirmed = await juConfirm(`Delete user "${username}"?`, 'danger');
        if (!confirmed) return;

        try {
            await api('DELETE', `/api/users/${id}`);
            juToast('User deleted', 'success');
            await loadUsers();
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    async function loadUsers() {
        try {
            const users = await api('GET', '/api/users');
            renderUsers(users || []);
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    // ─── OTP / 2FA Management ──────────────────────────────────────

    function openOtpModal() {
        if (!otpModal) {
            const modalEl = document.getElementById('otpModal');
            if (modalEl) otpModal = new bootstrap.Modal(modalEl);
        }

        // Setup event listeners if not already done
        const enableBtn = document.getElementById('enable-otp-btn');
        const disableBtn = document.getElementById('disable-otp-btn');
        const confirmBtn = document.getElementById('confirm-otp-btn');

        if (enableBtn && !enableBtn.dataset.listenerAttached) {
            enableBtn.addEventListener('click', enableOtp);
            enableBtn.dataset.listenerAttached = 'true';
        }
        if (disableBtn && !disableBtn.dataset.listenerAttached) {
            disableBtn.addEventListener('click', disableOtp);
            disableBtn.dataset.listenerAttached = 'true';
        }
        if (confirmBtn && !confirmBtn.dataset.listenerAttached) {
            confirmBtn.addEventListener('click', confirmOtp);
            confirmBtn.dataset.listenerAttached = 'true';
        }

        otpModal.show();
        loadOtpStatus();
    }

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
        const setupSection = document.getElementById('otp-setup');
        const statusText = document.getElementById('otp-status');

        if (enabled) {
            if (enableBtn) enableBtn.classList.add('d-none');
            if (disableBtn) disableBtn.classList.remove('d-none');
            if (setupSection) setupSection.classList.add('d-none');
            if (statusText) {
                statusText.textContent = 'Two-factor authentication is enabled.';
                statusText.classList.remove('text-muted');
                statusText.classList.add('text-success');
            }
        } else {
            if (enableBtn) enableBtn.classList.remove('d-none');
            if (disableBtn) disableBtn.classList.add('d-none');
            if (setupSection) setupSection.classList.add('d-none');
            if (statusText) {
                statusText.textContent = 'Two-factor authentication is not enabled.';
                statusText.classList.remove('text-success');
                statusText.classList.add('text-muted');
            }
        }
    }

    async function enableOtp() {
        const btn = document.getElementById('enable-otp-btn');
        if (btn) btn.disabled = true;

        try {
            const data = await api('POST', '/api/users/me/otp/enable');
            const setupSection = document.getElementById('otp-setup');
            const qrImg = document.getElementById('otp-qr');
            const secretEl = document.getElementById('otp-secret');

            if (qrImg && data?.qr_code) {
                qrImg.src = `data:image/png;base64,${data.qr_code}`;
            }
            if (secretEl && data?.secret) {
                secretEl.textContent = data.secret;
            }
            if (setupSection) setupSection.classList.remove('d-none');
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

            // Reload users table to update MFA badge
            await loadUsers();
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

            // Reload users table to update MFA badge
            await loadUsers();
        } catch (err) {
            juToast(err.message, 'danger');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ─── Passkeys Management ───────────────────────────────────────

    function openPasskeysModal() {
        if (!passkeysModal) {
            const modalEl = document.getElementById('passkeysModal');
            if (modalEl) passkeysModal = new bootstrap.Modal(modalEl);
        }

        // Setup event listener if not already done
        const addBtn = document.getElementById('add-passkey-btn');
        if (addBtn && !addBtn.dataset.listenerAttached) {
            addBtn.addEventListener('click', registerPasskey);
            addBtn.dataset.listenerAttached = 'true';
        }

        passkeysModal.show();
        loadPasskeys();
    }

    async function loadPasskeys() {
        const passkeysListEl = document.getElementById('passkeys-list');
        if (!passkeysListEl) return;

        try {
            const passkeys = await api('GET', '/api/passkeys');
            renderPasskeys(passkeys || []);
        } catch (err) {
            console.warn('Failed to load passkeys:', err);
            passkeysListEl.innerHTML = '<p class="text-danger mb-0">Failed to load passkeys.</p>';
        }
    }

    function renderPasskeys(passkeys) {
        const passkeysListEl = document.getElementById('passkeys-list');
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

})(); // End IIFE
