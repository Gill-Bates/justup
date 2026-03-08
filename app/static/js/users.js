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

    // Helper: Get current logged-in user ID from page data attribute
    function getCurrentUserId() {
        const raw = document.querySelector('.ju-page')?.dataset?.currentUserId;
        return raw ? parseInt(raw, 10) : null;
    }

    document.addEventListener('DOMContentLoaded', () => {
        usersTable = document.getElementById('users-body');
        userSaveBtn = document.getElementById('saveUserBtn');

        const modalEl = document.getElementById('userModal');
        if (modalEl) userModal = new bootstrap.Modal(modalEl);

        const addBtn = document.getElementById('addUserBtn');
        if (addBtn) addBtn.addEventListener('click', () => openUserModal());

        if (userSaveBtn) userSaveBtn.addEventListener('click', saveUser);

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
            usersTable.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No users found.</td></tr>';
            return;
        }

        usersTable.innerHTML = users.map(u => {
            const adminBadge = u.is_admin
                ? '<span class="badge bg-warning text-dark">Admin</span>'
                : '<span class="badge bg-secondary">User</span>';

            const statusBadge = u.is_active
                ? '<span class="badge bg-success">Active</span>'
                : '<span class="badge bg-secondary">Inactive</span>';

            const mfaBadge = u.otp_enabled
                ? '<span class="badge bg-info">Enabled</span>'
                : '<span class="badge bg-secondary">Disabled</span>';

            // Fix: Escape last_login_at to prevent XSS
            const lastLogin = escapeHtml(u.last_login_at || '–');

            // Admin user (ID 1) cannot be deleted
            const isSystemAdmin = u.id === 1;
            const deleteBtn = isSystemAdmin
                ? '<button class="btn btn-sm btn-outline-secondary" disabled title="System admin cannot be deleted"><span class="material-icons icon-sm">delete</span></button>'
                : `<button class="btn btn-sm btn-outline-danger delete-user" data-id="${u.id}" data-username="${escapeHtml(u.username)}" title="Delete"><span class="material-icons icon-sm">delete</span></button>`;

            return `<tr>
                <td>${escapeHtml(u.username)}</td>
                <td>${adminBadge}</td>
                <td>${statusBadge}</td>
                <td>${mfaBadge}</td>
                <td>${lastLogin}</td>
                <td>
                    <button class="btn btn-sm btn-outline-primary edit-user" data-id="${u.id}" title="Edit">
                        <span class="material-icons icon-sm">edit</span>
                    </button>
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

})(); // End IIFE
